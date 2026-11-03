"""The large backtest (PLAN.md): the pre-registered rules on Alpaca's data. Development first; the holdout once.

    python -B paper-trading-scenarios/large-backtest/backtest.py              # development, about 5 to 15 min
    python -B paper-trading-scenarios/large-backtest/backtest.py --holdout    # once, only if something passed

Run fetch.py first. For each session it loads the stocks in play and the fixed list, finds each rule's signal
with the pack's own entry.evaluate (through the feature search's engine.py), and trades it with exit.compute,
size.size_trade, check.check and the simulated paper broker:

  every candidate  each trade sized as if it were the only one in a $5,000 account (the verdicts use this)
  one account      one $5,000 account that takes the day's first signal (reported only)

Writes results/development.json (or holdout.json), results/*-trades.csv.gz with every trade, and RESULTS.md.
Standard library only.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time

import plan
import store

import engine  # noqa: E402  (on the path through store.py)
import search  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
RUNS = plan.TESTED + plan.BASELINES
SKIP = ("no signal", "no plan", "no daily ATR", "not sized", "blocked by risk", "rejected")
PRICE_BUCKETS = ((0.0, 20.0, "$5 to $20"), (20.0, 100.0, "$20 to $100"), (100.0, float("inf"), "above $100"))


def settings_from_plan():
    return {"development": plan.DEVELOPMENT, "holdout": plan.HOLDOUT, "fixed": list(plan.FIXED),
            "tested": list(plan.TESTED), "baselines": list(plan.BASELINES)}


def plan_hash():
    with open(os.path.join(HERE, "plan.py"), "rb") as file:
        return hashlib.sha256(file.read().replace(b"\r\n", b"\n")).hexdigest()


# ---------------------------------------------------------------- one session

def config_key(run):
    return run.split("-")[0]


def signals_for(data, day):
    """Each rule's signal on one stock-day: A's and B's from entry.evaluate, and the twins' fixed 9:45 one."""
    out = {}
    for key in plan.BASELINE_OF:
        config = plan.CONFIGS[key]
        out[key] = engine.signal(data, day, config["setup"], config["or_minutes"], config["volume"],
                                 config["cutoff"])
    out["always"] = search.always_signal(data, day)
    return out


def trade(data, day, key, sig, features, cash):
    """One rule on one stock-day through the engine. key is A, B, A0 or B0."""
    combo = dict(plan.CONFIGS[key])
    if sig is not None and combo["stop"].startswith("atr-daily:"):
        if features is None:
            return {"symbol": data["symbol"], "date": day, "action": "no daily ATR", "net": 0.0}
        multiple = float(combo["stop"].split(":")[1])
        stop = math.floor((sig["price"] - multiple * features["atr"]) * 100 + 1e-9) / 100.0
        if stop <= 0:
            return {"symbol": data["symbol"], "date": day, "action": "no plan", "net": 0.0}
        combo["stop"] = "price:%.2f" % stop
    return engine.trade_day(data, day, combo, cash, sig)


def compact(row, rank):
    return {"day": row["date"], "symbol": row["symbol"], "rank": rank, "signal_time": row.get("signal_time"),
            "fill_price": row["fill_price"], "shares": row["shares"], "pnl": row["pnl"],
            "gate": row["gate_max_loss"], "exit_reason": row["exit_reason"]}


def run_day(db, day, settings, state):
    """Every run on one session. Adds to state: trades, one-account trades and cash, and counts."""
    picks = db.execute("SELECT rank, symbol FROM selection WHERE date = ? ORDER BY rank", (day,)).fetchall()
    fixed = settings["fixed"]
    members = {"IN": picks, "FX": [(number, symbol) for number, symbol in enumerate(fixed, 1)]}
    wanted = {symbol for _, symbol in picks} | set(fixed)
    sessions = store.load_day(db, day, wanted)
    features = {}
    for symbol, atr in db.execute("SELECT symbol, atr FROM features WHERE date = ?", (day,)):
        if symbol in wanted:
            features[symbol] = {"atr": atr}
    signals = {symbol: signals_for(data, day) for symbol, data in sessions.items()}
    for universe, listed in members.items():
        counts = state["coverage"][universe]
        counts["listed"] += len(listed)
        counts["full sessions"] += sum(1 for _, symbol in listed if symbol in sessions)
    for run in settings["tested"] + settings["baselines"]:
        key, universe = run.split("-")
        kind = "always" if key.endswith("0") else key
        counts = state["actions"][run]
        candidates = []
        for rank, symbol in members[universe]:
            data = sessions.get(symbol)
            if data is None:
                counts["no full session"] = counts.get("no full session", 0) + 1
                continue
            sig = signals[symbol][kind]
            row = trade(data, day, key, sig, features.get(symbol), plan.START_CASH)
            counts[row["action"]] = counts.get(row["action"], 0) + 1
            if row["action"] == "traded":
                state["trades"][run].append(compact(row, rank))
            if sig is not None:
                candidates.append((sig["time"], rank, symbol))
        # one account: the day's first signal (then the higher-ranked stock) that the tools would trade
        cash = state["cash"][run]
        for _, rank, symbol in sorted(candidates):
            row = trade(sessions[symbol], day, key, signals[symbol][kind], features.get(symbol), cash)
            if row["action"] in SKIP:
                continue
            if row["action"] == "traded":
                state["one_account"][run].append(compact(row, rank))
                state["cash"][run] = round(cash + row["pnl"] - 2 * plan.COST * row["shares"], 2)
            break


def run_period(db, days, settings, log=print):
    state = {"trades": {run: [] for run in RUNS}, "one_account": {run: [] for run in RUNS},
             "cash": {run: plan.START_CASH for run in RUNS}, "actions": {run: {} for run in RUNS},
             "coverage": {"IN": {"listed": 0, "full sessions": 0}, "FX": {"listed": 0, "full sessions": 0}}}
    started = last = time.time()
    for number, day in enumerate(days, 1):
        run_day(db, day, settings, state)
        if time.time() - last > 30:
            last = time.time()
            log("  %s: %d of %d sessions, about %.0f min left" % (day, number, len(days),
                                                                (len(days) - number) * (last - started) / number / 60))
    return state


# ---------------------------------------------------------------- scores

def net_of(trade_row, cost):
    return trade_row["pnl"] - 2 * cost * trade_row["shares"]


def score(trades, days, cost=plan.COST, capital=None):
    """Scores for one run over these sessions. Sharpe and drawdown come from the summed daily P&L."""
    wanted = set(days)
    daily = dict.fromkeys(days, 0.0)
    nets, r_multiples, exits, years, buckets = [], [], {}, {}, {}
    for row in trades:
        if row["day"] not in wanted:
            continue
        net = net_of(row, cost)
        r_multiple = net / row["gate"] if row["gate"] else 0.0
        daily[row["day"]] += net
        nets.append(net)
        r_multiples.append(r_multiple)
        exits[row["exit_reason"]] = exits.get(row["exit_reason"], 0) + 1
        year = years.setdefault(row["day"][:4], {"trades": 0, "net": 0.0, "r_sum": 0.0})
        year["trades"] += 1
        year["net"] += net
        year["r_sum"] += r_multiple
        for low, high, label in PRICE_BUCKETS:
            if low <= row["fill_price"] < high:
                bucket = buckets.setdefault(label, {"trades": 0, "net": 0.0, "r_sum": 0.0})
                bucket["trades"] += 1
                bucket["net"] += net
                bucket["r_sum"] += r_multiple
    values = [daily[day] for day in days]
    if len(values) > 1:
        mean, deviation, skew, kurt = search.moments(values)
    else:
        mean, deviation, skew, kurt = (values[0] if values else 0.0), 0.0, 0.0, 3.0
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins, losses = [net for net in nets if net > 0], [net for net in nets if net < 0]
    r_mean = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
    r_sd = statistics.stdev(r_multiples) if len(r_multiples) > 1 else 0.0
    sharpe_day = mean / deviation if deviation else 0.0

    def tidy(groups):
        return {name: {"trades": group["trades"], "net": round(group["net"], 2),
                       "avg_r": round(group["r_sum"] / group["trades"], 4) if group["trades"] else None}
                for name, group in sorted(groups.items())}
    out = {
        "days": len(days), "trades": len(nets), "net": round(sum(nets), 2),
        "avg_r": round(r_mean, 4),
        "t_stat": round(r_mean / (r_sd / math.sqrt(len(r_multiples))), 3) if r_sd else None,
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "sharpe": round(sharpe_day * math.sqrt(252), 3), "sharpe_day": sharpe_day, "skew": skew, "kurt": kurt,
        "psr": search.probabilistic_sharpe(sharpe_day, 0.0, len(days), skew, kurt),
        "max_drawdown": round(drawdown, 2), "exits": exits, "years": tidy(years), "prices": tidy(buckets),
    }
    if capital:
        out["return_pct"] = round(100.0 * sum(nets) / capital, 2)
    return out


def bootstrap_range(trades, days, cost=plan.COST, draws=2000, seed=7):
    """The 5th to 95th percentile of the period's net, resampling whole sessions."""
    wanted = set(days)
    daily = dict.fromkeys(days, 0.0)
    for row in trades:
        if row["day"] in wanted:
            daily[row["day"]] += net_of(row, cost)
    values = [daily[day] for day in days]
    if not values:
        return None
    chance = random.Random(seed)
    totals = sorted(sum(chance.choice(values) for _ in values) for _ in range(draws))
    return [round(totals[int(0.05 * draws)], 2), round(totals[int(0.95 * draws)], 2)]


def month_back(month, count):
    total = int(month[:4]) * 12 + int(month[5:7]) - 1 - count
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def walk_forward(trades_by_run, all_days, report_days, tested, cost=plan.COST):
    """Each month of report_days, the tested run with the best R per trade over the 12 months before (all_days
    holds that history). Returns {month: run or None}, the trades it took, and the sessions it covered."""
    by_month = {run: {} for run in tested}
    for run in tested:
        for row in trades_by_run[run]:
            by_month[run].setdefault(row["day"][:7], []).append(row)
    first_month = min(all_days)[:7]
    picks, taken, covered = {}, [], []
    for month in sorted({day[:7] for day in report_days}):
        start = month_back(month, plan.WALK_FORWARD_MONTHS)
        if start < first_month:
            continue                                   # not 12 months of history yet
        best = None
        for run in tested:
            window = [row for past, rows in by_month[run].items() if start <= past < month for row in rows]
            if len(window) < plan.WALK_FORWARD_MIN_TRADES:
                continue
            value = sum(net_of(row, cost) / row["gate"] for row in window if row["gate"]) / len(window)
            if best is None or value > best[0]:
                best = (value, run)
        picks[month] = best[1] if best else None
        if best:
            taken.extend(by_month[best[1]].get(month, []))
        covered.extend(day for day in report_days if day[:7] == month)
    return picks, taken, covered


def years_up(result):
    return sum(1 for year in result["years"].values() if year["net"] > 0)


def development_verdict(run_score, at_three_cents, twin_score, years_needed):
    checks = {
        "net above zero": run_score["net"] > 0,
        "t-stat at least %.2f" % plan.T_MIN: (run_score["t_stat"] or 0) >= plan.T_MIN,
        "up in at least %d years" % years_needed: years_up(run_score) >= years_needed,
        "net above zero at 3 cents": at_three_cents["net"] > 0,
    }
    if twin_score is not None:
        checks["R per trade above its twin's"] = run_score["avg_r"] > twin_score["avg_r"]
    return {"checks": checks, "pass": all(checks.values())}


def holdout_verdict(run_score):
    checks = {"net above zero": run_score["net"] > 0,
              "t-stat at least %.2f" % plan.HOLDOUT_T_MIN: (run_score["t_stat"] or 0) >= plan.HOLDOUT_T_MIN}
    return {"checks": checks, "pass": all(checks.values())}


def run_name(run):
    key, universe = run.split("-")
    return "%s, %s" % (plan.CONFIGS[key]["name"], plan.UNIVERSES[universe])


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def summarize(state, days, settings):
    capital = plan.START_CASH
    runs = {}
    for run in settings["tested"] + settings["baselines"]:
        trades = state["trades"][run]
        runs[run] = {
            "name": run_name(run),
            "costs": {"%.2f" % cost: score(trades, days, cost) for cost in plan.COST_STRESS},
            "range_90": bootstrap_range(trades, days),
            "actions": state["actions"][run],
            "one_account": score(state["one_account"][run], days, plan.COST, capital),
        }
    tested = settings["tested"]
    sharpes = [runs[run]["costs"]["%.2f" % plan.COST]["sharpe_day"] for run in tested]
    benchmark = search.expected_max_sharpe(sharpes)
    for run in tested:
        base = runs[run]["costs"]["%.2f" % plan.COST]
        base["dsr"] = search.probabilistic_sharpe(base["sharpe_day"], benchmark, len(days), base["skew"], base["kurt"])
    return runs


def write_trades(path, state, settings):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["run", "book", "date", "symbol", "rank", "signal_time", "fill_price", "shares", "pnl",
                         "gate_max_loss", "exit_reason"])
        for run in settings["tested"] + settings["baselines"]:
            for book, rows in (("every candidate", state["trades"][run]), ("one account", state["one_account"][run])):
                for row in rows:
                    writer.writerow([run, book, row["day"], row["symbol"], row["rank"], row["signal_time"],
                                     row["fill_price"], row["shares"], row["pnl"], row["gate"], row["exit_reason"]])


def write_json(path, doc):
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as file:
        json.dump(doc, file, indent=2, sort_keys=False)
        file.write("\n")
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- the two runs

def development(db, settings, out_dir, log=print):
    days = store.sessions(db, settings["development"][0], settings["development"][1], full_only=True)
    if not days or not set(days) <= store.done_keys(db, "intraday"):
        raise SystemExit("backtest.py: the data is not all there yet: run fetch.py "
                         "(fetch.py status shows what is left)")
    log("development: %d full sessions, %s to %s" % (len(days), days[0], days[-1]))
    state = run_period(db, days, settings, log)
    runs = summarize(state, days, settings)
    years_needed = plan.MIN_YEARS_UP
    for run in settings["tested"]:
        twin = plan.BASELINE_OF[config_key(run)] + "-" + run.split("-")[1]
        runs[run]["twin"] = twin
        runs[run]["verdict"] = development_verdict(runs[run]["costs"]["0.01"], runs[run]["costs"]["0.03"],
                                                   runs[twin]["costs"]["0.01"] if twin in runs else None,
                                                   years_needed)
    picks, taken, covered = walk_forward(state["trades"], days, days, settings["tested"])
    walk = {"picks": picks, "costs": {"%.2f" % cost: score(taken, covered, cost) for cost in plan.COST_STRESS}}
    walk_years = len({day[:4] for day in covered})
    walk["verdict"] = development_verdict(walk["costs"]["0.01"], walk["costs"]["0.03"], None, max(walk_years - 1, 1))
    doc = {"period": "development", "sessions": len(days), "first": days[0], "last": days[-1],
           "plan_sha256": plan_hash(), "made": utc_stamp(),
           "coverage": state["coverage"], "runs": runs, "walk_forward": walk}
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    write_json(os.path.join(out_dir, "development.json"), doc)
    write_trades(os.path.join(out_dir, "development-trades.csv.gz"), state, settings)
    return doc


def holdout(db, settings, out_dir, again=False, log=print):
    path = os.path.join(out_dir, "development.json")
    if not os.path.exists(path):
        raise SystemExit("backtest.py: run the development period first (backtest.py without --holdout)")
    with open(path, encoding="utf-8") as file:
        dev = json.load(file)
    if dev.get("plan_sha256") != plan_hash():
        raise SystemExit("backtest.py: plan.py changed after the development run; the holdout only opens for the "
                         "plan it was scored on")
    passed = [run for run in settings["tested"] if dev["runs"][run]["verdict"]["pass"]]
    if dev["walk_forward"]["verdict"]["pass"]:
        passed.append("walk-forward")
    if not passed:
        log("Nothing passed development, so the holdout stays locked (and fresh for the next plan).")
        return None
    dev_days = store.sessions(db, settings["development"][0], settings["development"][1], full_only=True)
    days = store.sessions(db, settings["holdout"][0], settings["holdout"][1], full_only=True)
    if not days or not set(days) <= store.done_keys(db, "intraday"):
        raise SystemExit("backtest.py: the holdout's data is not all there yet: run fetch.py")
    log_path = os.path.join(out_dir, "holdout-log.json")
    opened = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as file:
            opened = json.load(file)
    if opened and not again:
        raise SystemExit("backtest.py: the holdout was already opened (%s). A second look is not a fresh test; "
                         "pass --again to run it anyway, and it is logged as a repeat." % opened[0]["opened_at"])
    opened.append({"opened_at": utc_stamp(), "for": passed, "repeat": bool(opened)})
    write_json(log_path, opened)
    log("holdout: %d full sessions, %s to %s, for %s" % (len(days), days[0], days[-1], ", ".join(passed)))
    state = run_period(db, days, settings, log)
    runs = summarize(state, days, settings)
    for run in settings["tested"]:
        runs[run]["verdict"] = holdout_verdict(runs[run]["costs"]["0.01"]) if run in passed else None
    # the walk-forward's first holdout months choose from the end of development: rerun that history's trades
    with gzip.open(os.path.join(out_dir, "development-trades.csv.gz"), "rt", encoding="utf-8") as file:
        history = {run: [] for run in settings["tested"]}
        for row in csv.DictReader(file):
            if row["book"] == "every candidate" and row["run"] in history:
                history[row["run"]].append({"day": row["date"], "pnl": float(row["pnl"]), "shares": int(row["shares"]),
                                            "gate": float(row["gate_max_loss"]), "exit_reason": row["exit_reason"],
                                            "fill_price": float(row["fill_price"]), "symbol": row["symbol"]})
    combined = {run: history[run] + state["trades"][run] for run in settings["tested"]}
    picks, taken, covered = walk_forward(combined, dev_days + days, days, settings["tested"])
    walk = {"picks": picks, "costs": {"%.2f" % cost: score(taken, covered, cost) for cost in plan.COST_STRESS}}
    walk["verdict"] = holdout_verdict(walk["costs"]["0.01"]) if "walk-forward" in passed else None
    doc = {"period": "holdout", "sessions": len(days), "first": days[0], "last": days[-1], "for": passed,
           "repeat": opened[-1]["repeat"], "plan_sha256": plan_hash(),
           "made": opened[-1]["opened_at"], "coverage": state["coverage"], "runs": runs, "walk_forward": walk}
    write_json(os.path.join(out_dir, "holdout.json"), doc)
    write_trades(os.path.join(out_dir, "holdout-trades.csv.gz"), state, settings)
    return doc


# ---------------------------------------------------------------- RESULTS.md

def money(value):
    return ("-$%s" if value < 0 else "$%s") % "{:,.2f}".format(abs(value))


def number(value, digits=3):
    return "n/a" if value is None else ("%." + str(digits) + "f") % value


def results_text(dev, hold, settings):
    lines = ["# The large backtest: results", "",
             "Made by `backtest.py` from `results/`; the rules and the pass marks are in [PLAN.md](PLAN.md), fixed "
             "before any data was fetched.", "",
             "> **Not financial advice.** Simulated fills on past 5-minute bars. A pass here earns a paper-trading "
             "trial, nothing more.", ""]
    lines += ["## Development: %d sessions, %s to %s" % (dev["sessions"], dev["first"], dev["last"]), ""]
    coverage = dev["coverage"]
    for universe in ("IN", "FX"):
        item = coverage[universe]
        lines.append("- **%s:** %d stock-days listed, %d with every 5-minute bar (%.1f %%)." % (
            plan.UNIVERSES[universe], item["listed"], item["full sessions"],
            100.0 * item["full sessions"] / item["listed"] if item["listed"] else 0.0))
    lines += ["", "Every candidate, 1 cent a share each way:", "",
              "| run | trades | net | R per trade | t-stat | Sharpe | years up | net at 3 cents | twin's R | pass |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for run in settings["tested"]:
        item = dev["runs"][run]
        base, three = item["costs"]["0.01"], item["costs"]["0.03"]
        twin = dev["runs"].get(item["twin"])
        lines.append("| %s (%s) | %d | %s | %s | %s | %s | %d of %d | %s | %s | **%s** |" % (
            run, item["name"], base["trades"], money(base["net"]), number(base["avg_r"], 4), number(base["t_stat"], 2),
            number(base["sharpe"], 2), years_up(base), len(base["years"]), money(three["net"]),
            number(twin["costs"]["0.01"]["avg_r"], 4) if twin else "n/a", "yes" if item["verdict"]["pass"] else "no"))
    walk = dev["walk_forward"]
    wbase, wthree = walk["costs"]["0.01"], walk["costs"]["0.03"]
    lines.append("| walk-forward | %d | %s | %s | %s | %s | %d of %d | %s | n/a | **%s** |" % (
        wbase["trades"], money(wbase["net"]), number(wbase["avg_r"], 4), number(wbase["t_stat"], 2),
        number(wbase["sharpe"], 2), years_up(wbase), len(wbase["years"]), money(wthree["net"]),
        "yes" if walk["verdict"]["pass"] else "no"))
    lines += ["", "The no-signal twins (buy every stock at 9:45; see PLAN.md for their exits):", "",
              "| run | trades | net | R per trade | t-stat |", "| --- | ---: | ---: | ---: | ---: |"]
    for run in settings["baselines"]:
        base = dev["runs"][run]["costs"]["0.01"]
        lines.append("| %s (%s) | %d | %s | %s | %s |" % (run, dev["runs"][run]["name"], base["trades"],
                                                         money(base["net"]), number(base["avg_r"], 4),
                                                         number(base["t_stat"], 2)))
    lines += ["", "### What failed", ""]
    for run in settings["tested"]:
        failed = [name for name, ok in dev["runs"][run]["verdict"]["checks"].items() if not ok]
        lines.append("- **%s:** %s." % (run, "passed every check" if not failed else "failed " + "; ".join(failed)))
    failed = [name for name, ok in walk["verdict"]["checks"].items() if not ok]
    lines.append("- **walk-forward:** %s." % ("passed every check" if not failed else "failed " + "; ".join(failed)))
    lines += ["", "### Year by year (net, every candidate, 1 cent)", ""]
    years = sorted({year for run in settings["tested"] for year in dev["runs"][run]["costs"]["0.01"]["years"]})
    lines += ["| run | " + " | ".join(years) + " |", "| --- |" + " ---: |" * len(years)]
    for run in settings["tested"]:
        by_year = dev["runs"][run]["costs"]["0.01"]["years"]
        lines.append("| %s | %s |" % (run, " | ".join(money(by_year[year]["net"]) if year in by_year else "n/a"
                                                     for year in years)))
    lines += ["", "### More detail", "",
              "| run | 90 % range of the net | deflated Sharpe | win rate | profit factor | max drawdown | "
              "one account: trades, net, return |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for run in settings["tested"]:
        item = dev["runs"][run]
        base, one = item["costs"]["0.01"], item["one_account"]
        span = item["range_90"]
        lines.append("| %s | %s | %s | %s | %s | %s | %d, %s, %s %% |" % (
            run, "%s to %s" % (money(span[0]), money(span[1])) if span else "n/a",
            "%.1f %%" % (100 * base["dsr"]) if base.get("dsr") is not None else "n/a",
            number(base["win_rate"], 3), number(base["profit_factor"], 2), money(base["max_drawdown"]),
            one["trades"], money(one["net"]), number(one.get("return_pct"), 2)))
    lines += ["", "R per trade by fill price (1 cent a share is kind to cheap stocks with wide spreads):", "",
              "| run | " + " | ".join(label for _, _, label in PRICE_BUCKETS) + " |",
              "| --- |" + " ---: |" * len(PRICE_BUCKETS)]
    for run in settings["tested"]:
        prices = dev["runs"][run]["costs"]["0.01"]["prices"]
        lines.append("| %s | %s |" % (run, " | ".join(
            "%s (%d)" % (number(prices[label]["avg_r"], 4), prices[label]["trades"]) if label in prices else "n/a"
            for _, _, label in PRICE_BUCKETS)))
    if hold is None:
        passed = [run for run in settings["tested"] if dev["runs"][run]["verdict"]["pass"]]
        if walk["verdict"]["pass"]:
            passed.append("walk-forward")
        lines += ["", "## The holdout", ""]
        if passed:
            lines.append("Not opened yet. %s passed development: run `backtest.py --holdout` once." % ", ".join(passed))
        else:
            lines.append("Locked: nothing passed development, so it stays fresh for the next plan.")
    else:
        repeat = " (a repeat look: not a fresh test)" if hold["repeat"] else ""
        lines += ["", "## The holdout: %d sessions, %s to %s%s" % (hold["sessions"], hold["first"], hold["last"],
                                                                   repeat),
            "", "| run | trades | net | R per trade | t-stat | Sharpe | holds up |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
        for run in settings["tested"]:
            item = hold["runs"][run]
            base = item["costs"]["0.01"]
            verdict = item["verdict"]
            lines.append("| %s | %d | %s | %s | %s | %s | %s |" % (
                run, base["trades"], money(base["net"]), number(base["avg_r"], 4), number(base["t_stat"], 2),
                number(base["sharpe"], 2), "not tested (failed development)" if verdict is None
                else "**%s**" % ("yes" if verdict["pass"] else "no")))
        walk_hold = hold["walk_forward"]
        base = walk_hold["costs"]["0.01"]
        verdict = walk_hold["verdict"]
        lines.append("| walk-forward | %d | %s | %s | %s | %s | %s |" % (
            base["trades"], money(base["net"]), number(base["avg_r"], 4), number(base["t_stat"], 2),
            number(base["sharpe"], 2), "not tested (failed development)" if verdict is None
            else "**%s**" % ("yes" if verdict["pass"] else "no")))
    lines.append("")
    return "\n".join(lines)


def write_results(out_dir, settings, results_path):
    with open(os.path.join(out_dir, "development.json"), encoding="utf-8") as file:
        dev = json.load(file)
    hold = None
    if os.path.exists(os.path.join(out_dir, "holdout.json")):
        with open(os.path.join(out_dir, "holdout.json"), encoding="utf-8") as file:
            hold = json.load(file)
    with open(results_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(results_text(dev, hold, settings))


def main(argv=None):
    parser = argparse.ArgumentParser(description="The large backtest: development, then the holdout once.")
    parser.add_argument("--holdout", action="store_true", help="open the holdout (only if something passed)")
    parser.add_argument("--again", action="store_true", help="open the holdout a second time (logged as a repeat)")
    parser.add_argument("--db", default=store.DEFAULT_DB)
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print("backtest.py: no data at %s yet: run fetch.py first" % args.db)
        return 1
    db = store.connect(args.db)
    settings = settings_from_plan()
    started = time.time()
    if args.holdout:
        holdout(db, settings, args.out, args.again)
    else:
        development(db, settings, args.out)
    results_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "RESULTS.md")
    if os.path.exists(os.path.join(args.out, "development.json")):
        write_results(args.out, settings, results_path)
        print("wrote %s after %.0f s" % (results_path, time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
