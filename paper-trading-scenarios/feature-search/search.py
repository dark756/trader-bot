"""The feature search: every combination of the pack's trading choices, on 30 stocks, scored the honest way.

    python -B paper-trading-scenarios/feature-search/search.py      # about 2 minutes on 4 cores

1. Each combo (setup, opening range, stop, target, entry order, volume check, entry window) trades every
   symbol in its own $5,000 practice account through 59 sessions, with the tools' own rules (engine.py).
2. A cost of 1 cent a share each way (spread and slippage) comes off every trade.
3. The sessions are split in time: the first 39 are IN-SAMPLE, where the picks are chosen; the last 20 are
   OUT-OF-SAMPLE, never looked at while choosing. A pick that only shines in-sample was luck.
4. With 852 combos tried, the best in-sample Sharpe is inflated by chance: the deflated Sharpe ratio
   (Bailey and Lopez de Prado) says how likely it is to be real after that many tries.

Writes results/combos.csv (every combo, both halves and the full period), results/features.csv (each
choice averaged over every combo that has it), results/picks.json, and RESULTS.md. Standard library only.
"""

import csv
import datetime
import itertools
import json
import math
import multiprocessing
import os
import random
import statistics
import sys
import time

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "results")
COST = 0.01                   # dollars a share, each way
IN_SAMPLE_DAYS = 39           # of the 59 trading sessions; the rest are out-of-sample
MIN_TRADES = 60               # a combo needs this many in-sample trades to be picked
NORMAL = statistics.NormalDist()

STOPS_ORB = ["or-mid", "or-low", "pct:0.5", "pct:1", "atr:1", "atr:1.5", "atr:2"]
STOPS_LEVEL = ["pct:0.5", "pct:1", "atr:1", "atr:1.5", "atr:2"]
TARGETS = ["1R", "1.5R", "2R", "3R"]
SETUPS = [("orb", 5), ("orb", 15), ("orb", 30), ("pdh", 15)]
BASELINE_STOPS = ["pct:0.5", "pct:1", "atr:1", "atr:1.5", "atr:2"]


def combo_id(combo):
    setup = "pdh" if combo["setup"] == "pdh" else "always" if combo["setup"] == "always" else \
        "orb%d" % combo["or_minutes"]
    return "%s %s %s %s %s %s" % (setup, combo["stop"], combo["target"], combo["entry"],
                                  "vol%.1fx" % combo["volume"] if combo["volume"] else "novol",
                                  "until" + combo["cutoff"].replace(":", ""))


def grid():
    combos = []
    for (setup, minutes), target, entry, volume, cutoff in itertools.product(
            SETUPS, TARGETS, ["market", "cap"], [None, 1.5], ["15:30", "11:00"]):
        for stop in (STOPS_LEVEL if setup == "pdh" else STOPS_ORB):
            combos.append({"setup": setup, "or_minutes": minutes, "stop": stop, "target": target, "entry": entry,
                           "volume": volume, "cutoff": cutoff, "risk": 50})
    # the baseline: no signal at all, buy every session at the first allowed time with the same exits
    for stop, target in itertools.product(BASELINE_STOPS, TARGETS):
        combos.append({"setup": "always", "or_minutes": 15, "stop": stop, "target": target, "entry": "market",
                       "volume": None, "cutoff": "15:30", "risk": 50})
    for combo in combos:
        combo["id"] = combo_id(combo)
    return combos


def always_signal(data, day):
    """The baseline's 'signal': the bar that closes at 9:45, the first time the config allows an entry."""
    bars = data["sessions"][day]["bars"]
    index = [bar["start"] for bar in bars].index("09:40")
    first = bars[:3]
    return {"price": round(bars[index]["c"], 2), "bar_start": "09:40", "bar_index": index, "time": "09:45",
            "or_high": round(max(bar["h"] for bar in first), 2), "or_low": round(min(bar["l"] for bar in first), 2),
            "level": None, "setup": "orb"}


# ---------------------------------------------------------------- running the grid

STATE = {}


def load_universe():
    symbols = sorted(name[:-len(".json.gz")] for name in os.listdir(DATA) if name.endswith(".json.gz"))
    universe = {symbol: engine.load_symbol(os.path.join(DATA, symbol + ".json.gz"), symbol) for symbol in symbols}
    common = sorted(set.intersection(*(set(data["days"]) for data in universe.values())))
    return universe, common


def signal_key(combo):
    return (combo["setup"], combo["or_minutes"], combo["volume"], combo["cutoff"])


def build_signals(universe, days, combos):
    signals = {}
    for key in sorted({signal_key(combo) for combo in combos}, key=str):
        setup, minutes, volume, cutoff = key
        signals[key] = {symbol: {day: (always_signal(data, day) if setup == "always" else
                                       engine.signal(data, day, setup, minutes, volume, cutoff)) for day in days}
                        for symbol, data in universe.items()}
    return signals


def run_one(index):
    combo = STATE["combos"][index]
    trades, counts = [], {}
    for symbol, data in STATE["universe"].items():
        for row in engine.run_account(data, STATE["days"], combo, STATE["signals"][signal_key(combo)][symbol]):
            counts[row["action"]] = counts.get(row["action"], 0) + 1
            if row["action"] == "traded":
                trades.append((symbol, STATE["day_index"][row["date"]], row["pnl"], row["shares"],
                               row["gate_max_loss"], row["exit_reason"], row["max_open_loss"]))
    return index, trades, counts


def make_pool(loader, *args):
    """A pool of workers that have STATE. Where the OS can fork (Linux, macOS) they are copies of this process;
    on Windows each starts fresh and runs loader(*args) to load the bars and the signals itself."""
    if "fork" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("fork").Pool(os.cpu_count())
    return multiprocessing.get_context("spawn").Pool(os.cpu_count(), initializer=loader, initargs=args)


def load_state():
    """Everything a grid worker needs, into STATE: the bars, the sessions, the combos and their signals."""
    universe, common = load_universe()
    days = common[1:]                                  # the first session is history only (prior-day high)
    combos = grid()
    STATE.update(universe=universe, days=days, combos=combos, signals=build_signals(universe, days, combos),
                 day_index={day: number for number, day in enumerate(days)})


def run_grid():
    results = [None] * len(STATE["combos"])
    with make_pool(load_state) as pool:
        for index, trades, counts in pool.imap_unordered(run_one, range(len(STATE["combos"])), chunksize=4):
            results[index] = (trades, counts)
    return results


# ---------------------------------------------------------------- the metrics

def moments(values):
    """mean, sample standard deviation, skewness, kurtosis (3 for a normal curve)."""
    count = len(values)
    mean = sum(values) / count
    deviation = math.sqrt(sum((value - mean) ** 2 for value in values) / (count - 1)) if count > 1 else 0.0
    if deviation == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum(((value - mean) / deviation) ** 3 for value in values) / count
    kurt = sum(((value - mean) / deviation) ** 4 for value in values) / count
    return mean, deviation, skew, kurt


def probabilistic_sharpe(sharpe, benchmark, count, skew, kurt):
    """P(the true per-day Sharpe > benchmark), given the estimate from `count` days (Bailey and Lopez de Prado)."""
    spread = 1 - skew * sharpe + (kurt - 1) / 4.0 * sharpe ** 2
    if count < 2 or spread <= 0:
        return None
    return NORMAL.cdf((sharpe - benchmark) * math.sqrt(count - 1) / math.sqrt(spread))


def expected_max_sharpe(sharpes):
    """The best per-day Sharpe that luck alone would give among this many tries with this spread."""
    trials = len(sharpes)
    if trials < 2:
        return 0.0
    euler = 0.5772156649
    spread = statistics.pstdev(sharpes)
    return spread * ((1 - euler) * NORMAL.inv_cdf(1 - 1.0 / trials)
                     + euler * NORMAL.inv_cdf(1 - 1.0 / (trials * math.e)))


def metrics(trades, days, symbols, cost=COST):
    """Scores for one combo over a set of day numbers. The book is every symbol's $5,000 account together."""
    capital = engine.START_CASH * len(symbols)
    wanted = set(days)
    daily = dict.fromkeys(days, 0.0)
    by_symbol = dict.fromkeys(symbols, 0.0)
    nets, r_multiples, exits = [], [], {}
    for symbol, day, pnl, shares, gate, exit_reason, _ in trades:
        if day not in wanted:
            continue
        net = pnl - 2 * cost * shares
        daily[day] += net
        by_symbol[symbol] += net
        nets.append(net)
        r_multiples.append(net / gate if gate else 0.0)
        exits[exit_reason] = exits.get(exit_reason, 0) + 1
    returns = [daily[day] / capital for day in days]
    mean, deviation, skew, kurt = moments(returns)
    downside = math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
    equity = peak = drawdown = 0.0
    for day in days:
        equity += daily[day]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins, losses = [net for net in nets if net > 0], [net for net in nets if net < 0]
    total = sum(nets)
    r_mean = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
    r_sd = statistics.stdev(r_multiples) if len(r_multiples) > 1 else 0.0
    sharpe_day = mean / deviation if deviation else 0.0
    return {
        "days": len(days), "trades": len(nets), "net": round(total, 2),
        "return_pct": round(100.0 * total / capital, 3),
        "sharpe": round(sharpe_day * math.sqrt(252), 3), "sharpe_day": sharpe_day,
        "sortino": round(mean / downside * math.sqrt(252), 3) if downside else None,
        "max_drawdown": round(drawdown, 2), "max_drawdown_pct": round(100.0 * drawdown / capital, 3),
        "return_to_drawdown": round(total / drawdown, 3) if drawdown else None,
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "avg_r": round(r_mean, 4), "t_stat": round(r_mean / (r_sd / math.sqrt(len(r_multiples))), 3)
        if r_sd and len(r_multiples) > 1 else None,
        "symbols_up": sum(1 for value in by_symbol.values() if value > 0),
        "psr": probabilistic_sharpe(sharpe_day, 0.0, len(days), skew, kurt),
        "skew": skew, "kurt": kurt,
        "targets": exits.get("target", 0), "stops": exits.get("stop", 0), "flat_by": exits.get("flat by", 0),
    }


def bootstrap_range(trades, days, symbols, cost=COST, draws=5000, seed=7):
    """The 5th to 95th percentile of the period's net, resampling whole days (they keep each day's trades together)."""
    daily = dict.fromkeys(days, 0.0)
    wanted = set(days)
    for symbol, day, pnl, shares, _, _, _ in trades:
        if day in wanted:
            daily[day] += pnl - 2 * cost * shares
    values = [daily[day] for day in days]
    chance = random.Random(seed)
    totals = sorted(sum(chance.choice(values) for _ in values) for _ in range(draws))
    return round(totals[int(0.05 * draws)], 2), round(totals[int(0.95 * draws)], 2)


def spearman(first, second):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda index: values[index])
        out = [0.0] * len(values)
        for rank, index in enumerate(order):
            out[index] = rank
        return out
    if len(first) < 3:
        return None
    one, two = ranks(first), ranks(second)
    mean = (len(one) - 1) / 2.0                        # ranks 0..n-1 have the same mean and spread
    spread = sum((rank - mean) ** 2 for rank in one)
    return sum((a - mean) * (b - mean) for a, b in zip(one, two)) / spread


# ---------------------------------------------------------------- choosing, the honest way

CATEGORIES = [
    ("Highest net return", "net", True, None),
    ("Highest Sharpe ratio", "sharpe", True, None),
    ("Highest Sortino ratio", "sortino", True, None),
    ("Best return per dollar of drawdown", "return_to_drawdown", True, lambda m: m["net"] > 0),
    ("Smallest drawdown while making money", "max_drawdown", False, lambda m: m["net"] > 0),
    ("Highest profit factor", "profit_factor", True, None),
    ("Highest win rate while making money", "win_rate", True, lambda m: m["net"] > 0),
    ("Strongest evidence (t-stat of R per trade)", "t_stat", True, None),
    ("Most stocks profitable", "symbols_up", True, lambda m: m["net"] > 0),
    ("Steadiest (best worse quarter in-sample)", "steady", True, None),
]
FEATURE_ORDER = ["setup", "stop", "target", "entry", "volume check", "entry window"]


def steadiness(rows, in_days, out_days, symbols):
    """The worse of the two halves of a period: a combo that only made money in one stretch scores low."""
    for row in rows:
        for part, span in (("is", in_days), ("oos", out_days)):
            middle = len(span) // 2
            row[part]["steady"] = round(min(metrics(row["trades"], span[:middle], symbols)["net"],
                                            metrics(row["trades"], span[middle:], symbols)["net"]), 2)
        row["all"]["steady"] = round(min(row["is"]["net"], row["oos"]["net"]), 2)


def feature_by_feature(rows):
    """Build one combo from the choices that did best in-sample, one feature at a time (each choice by the
    median in-sample net of the combos that still fit the choices made so far)."""
    candidates = list(rows)
    chosen = {}
    for feature in FEATURE_ORDER:
        values = {}
        for row in candidates:
            values.setdefault(feature_of(row["combo"])[feature], []).append(row["is"]["net"])
        best = max(values, key=lambda value: statistics.median(values[value]))
        chosen[feature] = best
        candidates = [row for row in candidates if feature_of(row["combo"])[feature] == best]
    return candidates[0], chosen


def pick(rows, key, high, condition, count=5):
    pool = [row for row in rows if row["is"]["trades"] >= MIN_TRADES and row["is"][key] is not None
            and (condition is None or condition(row["is"]))]
    pool.sort(key=lambda row: (row["is"][key], row["is"]["net"]), reverse=high)
    return pool[:count]


def percentile_rank(rows, row, key, high=True):
    values = [other["oos"][key] for other in rows if other["oos"][key] is not None]
    mine = row["oos"][key]
    if mine is None or not values:
        return None
    better = sum(1 for value in values if (value > mine if high else value < mine))
    return round(100.0 * (1 - better / float(len(values))), 1)


def main(argv):
    started = time.time()
    os.makedirs(OUT, exist_ok=True)
    load_state()
    universe, days, combos = STATE["universe"], STATE["days"], STATE["combos"]
    in_days, out_days = list(range(IN_SAMPLE_DAYS)), list(range(IN_SAMPLE_DAYS, len(days)))
    symbols = sorted(universe)
    print("%d symbols, %d sessions %s to %s: in-sample %s to %s, out-of-sample %s to %s; %d combos; signals "
          "ready after %.0f s" % (len(symbols), len(days), days[0], days[-1], days[0], days[IN_SAMPLE_DAYS - 1],
                                  days[IN_SAMPLE_DAYS], days[-1], len(combos), time.time() - started), flush=True)
    results = run_grid()
    print("grid done after %.0f s" % (time.time() - started), flush=True)

    rows = []
    for combo, (trades, counts) in zip(combos, results):
        rows.append({"combo": combo, "trades": trades, "counts": counts,
                     "is": metrics(trades, in_days, symbols), "oos": metrics(trades, out_days, symbols),
                     "all": metrics(trades, in_days + out_days, symbols),
                     # the same trades with no cost at all: how much of the result is the cost
                     "is0": metrics(trades, in_days, symbols, 0.0), "oos0": metrics(trades, out_days, symbols, 0.0),
                     "all0": metrics(trades, in_days + out_days, symbols, 0.0)})
    strategy_rows = [row for row in rows if row["combo"]["setup"] != "always"]
    baseline_rows = [row for row in rows if row["combo"]["setup"] == "always"]
    for part in ("is", "oos", "all"):
        bar = expected_max_sharpe([row[part]["sharpe_day"] for row in strategy_rows])
        for row in rows:
            moment = row[part]
            moment["dsr"] = probabilistic_sharpe(moment["sharpe_day"], bar, moment["days"], moment["skew"],
                                                 moment["kurt"])
            moment["luck_sharpe"] = round(bar * math.sqrt(252), 3)

    steadiness(rows, in_days, out_days, symbols)
    write_combos(rows)
    features = feature_table(strategy_rows)
    built, chosen = feature_by_feature(strategy_rows)
    picks = []
    for title, key, high, condition in CATEGORIES + [("Best choice per feature (in-sample medians)", "net", True,
                                                     "built")]:
        best = [built] if condition == "built" else pick(strategy_rows, key, high, condition)
        if not best:
            continue
        top = best[0]
        picks.append({"category": title, "key": key, "high": high, "id": top["combo"]["id"], "combo": top["combo"],
                      "chosen": chosen if condition == "built" else None,
                      "is": slim(top["is"]), "oos": slim(top["oos"]), "all": slim(top["all"]),
                      "no_cost": {part: slim(top[part + "0"]) for part in ("is", "oos", "all")},
                      "oos_rank_pct": percentile_rank(strategy_rows, top, key, high),
                      "oos_range90": bootstrap_range(top["trades"], out_days, symbols),
                      "runners_up": [{"id": row["combo"]["id"], "is": row["is"][key], "oos": row["oos"][key],
                                      "oos_net": row["oos"]["net"]} for row in best[1:]]})
    summary = {
        "symbols": symbols, "sessions": days, "in_sample": [days[0], days[IN_SAMPLE_DAYS - 1]],
        "out_of_sample": [days[IN_SAMPLE_DAYS], days[-1]], "combos": len(strategy_rows),
        "baselines": len(baseline_rows), "cost_per_share_each_way": COST, "min_trades": MIN_TRADES,
        "rank_correlation_sharpe": spearman([row["is"]["sharpe"] for row in strategy_rows],
                                            [row["oos"]["sharpe"] for row in strategy_rows]),
        "rank_correlation_net": spearman([row["is"]["net"] for row in strategy_rows],
                                         [row["oos"]["net"] for row in strategy_rows]),
        "share_positive": {part: round(sum(1 for row in strategy_rows if row[part]["net"] > 0)
                                       / float(len(strategy_rows)), 3) for part in ("is", "oos", "all")},
        "share_positive_no_cost": {part: round(sum(1 for row in strategy_rows if row[part + "0"]["net"] > 0)
                                               / float(len(strategy_rows)), 3) for part in ("is", "oos", "all")},
        "up_in_both_halves": [row["combo"]["id"] for row in strategy_rows
                              if row["is"]["net"] > 0 and row["oos"]["net"] > 0],
        "up_in_both_halves_no_cost": [row["combo"]["id"] for row in strategy_rows
                                      if row["is0"]["net"] > 0 and row["oos0"]["net"] > 0],
        "median_avg_r": {"cost": round(statistics.median(row["all"]["avg_r"] for row in strategy_rows), 4),
                         "no_cost": round(statistics.median(row["all0"]["avg_r"] for row in strategy_rows), 4)},
        "best_all_period": [{"id": row["combo"]["id"], "all": slim(row["all"]), "all_no_cost": slim(row["all0"])}
                            for row in sorted(strategy_rows, key=lambda row: -row["all"]["net"])[:5]],
        "luck_sharpe": {part: strategy_rows[0][part]["luck_sharpe"] for part in ("is", "oos", "all")},
        "best_baseline": {part: slim(max(baseline_rows, key=lambda row: row[part]["sharpe"])[part])
                          for part in ("is", "oos", "all")},
        "baseline_ids": {part: max(baseline_rows, key=lambda row: row[part]["sharpe"])["combo"]["id"]
                         for part in ("is", "oos", "all")},
        "buy_and_hold": buy_and_hold(universe, days, in_days, out_days),
        "picks": picks, "features": features,
        "seconds": round(time.time() - started),
    }
    with open(os.path.join(OUT, "picks.json"), "w", encoding="utf-8", newline="\n") as file:
        json.dump(summary, file, indent=2)
        file.write("\n")
    print("wrote results/ after %.0f s" % (time.time() - started))
    return 0


def slim(moment):
    return {key: (round(value, 4) if isinstance(value, float) else value) for key, value in moment.items()
            if key not in ("sharpe_day", "skew", "kurt")}


COLUMNS = ["trades", "net", "return_pct", "sharpe", "sortino", "max_drawdown", "max_drawdown_pct",
           "return_to_drawdown", "win_rate", "profit_factor", "avg_r", "t_stat", "symbols_up", "psr", "dsr",
           "targets", "stops", "flat_by", "steady"]
GROSS_COLUMNS = ["net", "sharpe", "avg_r", "profit_factor"]


def write_combos(rows):
    fields = ["id", "setup", "or_minutes", "stop", "target", "entry", "volume", "cutoff", "signals", "blocked",
              "not_sized", "no_plan", "not_filled"] + ["%s_%s" % (part, column) for part in ("is", "oos", "all")
                                                       for column in COLUMNS] + \
        ["%s_no_cost_%s" % (part, column) for part in ("is", "oos", "all") for column in GROSS_COLUMNS]
    with open(os.path.join(OUT, "combos.csv"), "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            combo, counts = row["combo"], row["counts"]
            out = {"id": combo["id"], "setup": combo["setup"], "or_minutes": combo["or_minutes"], "stop": combo["stop"],
                   "target": combo["target"], "entry": combo["entry"], "volume": combo["volume"] or "",
                   "cutoff": combo["cutoff"], "signals": sum(counts.values()) - counts.get("no signal", 0),
                   "blocked": counts.get("blocked by risk", 0), "not_sized": counts.get("not sized", 0),
                   "no_plan": counts.get("no plan", 0), "not_filled": counts.get("not filled", 0)}
            for part in ("is", "oos", "all"):
                for column in COLUMNS:
                    value = row[part][column]
                    out["%s_%s" % (part, column)] = round(value, 4) if isinstance(value, float) else value
                for column in GROSS_COLUMNS:
                    value = row[part + "0"][column]
                    out["%s_no_cost_%s" % (part, column)] = round(value, 4) if isinstance(value, float) else value
            writer.writerow(out)


def feature_of(combo):
    stop_family = combo["stop"].split(":")[0]
    return {"setup": "pdh" if combo["setup"] == "pdh" else "orb%d" % combo["or_minutes"],
            "stop": combo["stop"], "stop family": stop_family, "target": combo["target"], "entry": combo["entry"],
            "volume check": "1.5x" if combo["volume"] else "none", "entry window": "until " + combo["cutoff"]}


def feature_table(rows):
    """Each choice, over every combo that has it: the median Sharpe and net in both halves, and how often
    a combo with it made money out-of-sample."""
    groups = {}
    for row in rows:
        for feature, value in feature_of(row["combo"]).items():
            groups.setdefault((feature, value), []).append(row)
    table = []
    for (feature, value), members in sorted(groups.items()):
        table.append({
            "feature": feature, "value": value, "combos": len(members),
            "is_median_sharpe": round(statistics.median(row["is"]["sharpe"] for row in members), 3),
            "oos_median_sharpe": round(statistics.median(row["oos"]["sharpe"] for row in members), 3),
            "all_median_sharpe": round(statistics.median(row["all"]["sharpe"] for row in members), 3),
            "is_median_net": round(statistics.median(row["is"]["net"] for row in members), 2),
            "oos_median_net": round(statistics.median(row["oos"]["net"] for row in members), 2),
            "all_median_net": round(statistics.median(row["all"]["net"] for row in members), 2),
            "is_share_up": round(sum(1 for row in members if row["is"]["net"] > 0) / float(len(members)), 3),
            "oos_share_up": round(sum(1 for row in members if row["oos"]["net"] > 0) / float(len(members)), 3),
            "both_up": round(sum(1 for row in members if row["is"]["net"] > 0 and row["oos"]["net"] > 0)
                             / float(len(members)), 3),
            "median_trades": statistics.median(row["all"]["trades"] for row in members),
        })
    with open(os.path.join(OUT, "features.csv"), "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(table[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(table)
    return table


def buy_and_hold(universe, days, in_days, out_days):
    """Context, not a like-for-like rival: $5,000 in each stock at one close, sold at a later close."""
    def leg(first, last):
        total = 0.0
        for data in universe.values():
            start = data["sessions"][first]["bars"][-1]["c"]
            end = data["sessions"][last]["bars"][-1]["c"]
            total += engine.START_CASH * (end / start - 1)
        return round(total, 2), round(100.0 * total / (engine.START_CASH * len(universe)), 3)
    before = sorted(universe[next(iter(universe))]["days"])
    start_day = before[before.index(days[0]) - 1]
    return {"is": leg(start_day, days[in_days[-1]]), "oos": leg(days[in_days[-1]], days[out_days[-1]]),
            "all": leg(start_day, days[-1])}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
