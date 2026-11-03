"""Stress tests for the feature search's picks (run search.py first).

    python -B paper-trading-scenarios/feature-search/stress.py      # about 10 minutes on 4 cores

For every combo that won a category in-sample, and the README's own rules for comparison:
  cost          the same trades at 0, 1 (the base) and 3 cents a share each way
  slower yes    the fill 5 minutes later (the open of the bar after next), as if the yes took that long
  time machine  skip the trade when the Time Machine has stop-first at or above target-first (sessions
                with 20 earlier sessions only, so both sides run on the same days)
  risk $100     a $100 max loss a trade ($300 a day) instead of $50
  stock picking trade out-of-sample only the stocks the combo made money on in-sample
  one account   one $5,000 account for the whole universe: each day the first signal of the 30 stocks
                (ties by symbol), as one person with one account would trade it

Writes results/stress.json. Standard library only.
"""

import json
import os
import sys
import time

import engine
import search

OUT = search.OUT
README_RULES = {"setup": "orb", "or_minutes": 15, "stop": "or-mid", "target": "1R", "entry": "market",
                "volume": None, "cutoff": "15:30", "risk": 50}
STATE = {}


def compact(rows):
    return [(row["symbol"], STATE["day_index"][row["date"]], row["pnl"], row["shares"], row["gate_max_loss"],
             row["exit_reason"], row["max_open_loss"]) for row in rows if row["action"] == "traded"]


def account_task(task):
    """One (combo, symbol, variant) account. Returns (task key, compact trades, actions)."""
    combo_index, symbol, variant = task
    combo = dict(STATE["combos"][combo_index])
    if variant == "risk100":
        combo["risk"] = 100
    data = STATE["universe"][symbol]
    signals = STATE["signals"][search.signal_key(combo)][symbol]
    skip = None
    days = STATE["days"]
    if variant in ("tm", "tm_off"):
        days = STATE["days"][STATE["tm_from"]:]
    if variant == "tm":
        machine = STATE["machines"].setdefault(symbol, engine.TimeMachine(
            os.path.join(search.DATA, symbol + ".json.gz"), symbol))

        def skip(row):
            odds = machine.odds(row["date"], row["plan_entry"], row["stop"], row["target"],
                                signals[row["date"]]["bar_index"])
            return odds is None or odds[0] <= odds[1]
    rows = engine.run_account(data, days, combo, signals, delay_bars=1 if variant == "delay" else 0, skip=skip)
    actions = {}
    for row in rows:
        actions[row["action"]] = actions.get(row["action"], 0) + 1
    return task, compact(rows), actions


def one_account(combo):
    """One $5,000 account trading the universe: each day the earliest signal across the symbols."""
    cash, trades, picked = engine.START_CASH, [], {}
    signals = STATE["signals"][search.signal_key(combo)]
    for day in STATE["days"]:
        candidates = sorted((sig["time"], symbol) for symbol, by_day in signals.items()
                            for sig in [by_day.get(day)] if sig)
        for _, symbol in candidates:
            row = engine.trade_day(STATE["universe"][symbol], day, combo, cash, signals[symbol][day])
            if row["action"] in ("no plan", "not sized", "blocked by risk", "rejected"):
                continue                              # the next stock's signal gets the account instead
            if row["action"] == "traded":
                net = row["pnl"] - 2 * search.COST * row["shares"]
                cash = round(cash + net, 2)
                trades.append((symbol, STATE["day_index"][day], row["pnl"], row["shares"], row["gate_max_loss"],
                               row["exit_reason"], row["max_open_loss"]))
                picked[symbol] = picked.get(symbol, 0) + 1
            break
    return trades, picked


def account_metrics(trades, days, cost=search.COST):
    """Metrics for one $5,000 account (the search's metrics count one account per symbol)."""
    return search.metrics([("ONE",) + tuple(trade[1:]) for trade in trades], days, ["ONE"], cost)


def load_state(argv):
    """Everything a worker needs, into STATE: the bars, the picks (and the README's rules) and their signals."""
    summary = json.load(open(os.path.join(OUT, "picks.json"), encoding="utf-8"))
    universe, common = search.load_universe()
    if argv:                                           # a quick check on a few symbols: stress.py AAPL BAC
        universe = {symbol: universe[symbol] for symbol in argv}
    days = common[1:]
    ids, combos = [], []
    for item in summary["picks"]:
        if item["id"] not in ids:
            ids.append(item["id"])
            combos.append(item["combo"])
    readme = dict(README_RULES)
    readme["id"] = search.combo_id(readme)
    if readme["id"] not in ids:
        ids.append(readme["id"])
        combos.append(readme)
    tm_from = next(index for index, day in enumerate(days)
                   if len([d for d in common if d < day]) >= engine.timemachine.MIN_SESSIONS)
    STATE.update(summary=summary, universe=universe, days=days, combos=combos, machines={}, tm_from=tm_from,
                 signals=search.build_signals(universe, days, combos),
                 day_index={day: number for number, day in enumerate(days)})


def main(argv):
    started = time.time()
    load_state(argv)
    summary, universe, days, combos = STATE["summary"], STATE["universe"], STATE["days"], STATE["combos"]
    symbols = sorted(universe)
    tm_from = STATE["tm_from"]
    variants = ["base", "delay", "risk100", "tm", "tm_off"]
    tasks = [(index, symbol, variant) for index in range(len(combos)) for symbol in symbols for variant in variants]
    results = {}
    with search.make_pool(load_state, argv) as pool:
        for task, trades, actions in pool.imap_unordered(account_task, tasks, chunksize=1):
            results[task] = (trades, actions)
    print("accounts done after %.0f s" % (time.time() - started), flush=True)

    in_days = list(range(search.IN_SAMPLE_DAYS))
    out_days = list(range(search.IN_SAMPLE_DAYS, len(days)))
    all_days = in_days + out_days
    tm_days = list(range(tm_from, len(days)))
    report = {"tm_from": days[tm_from], "combos": []}
    for index, combo in enumerate(combos):
        def gather(variant):
            trades, actions = [], {}
            for symbol in symbols:
                part, counts = results[(index, symbol, variant)]
                trades.extend(part)
                for key, value in counts.items():
                    actions[key] = actions.get(key, 0) + value
            return trades, actions
        base, base_actions = gather("base")
        delay, delay_actions = gather("delay")
        risk100, _ = gather("risk100")
        tm, tm_actions = gather("tm")
        tm_off, _ = gather("tm_off")
        by_symbol = {}
        for symbol, day, pnl, shares, _, _, _ in base:
            entry = by_symbol.setdefault(symbol, {"is": 0.0, "oos": 0.0})
            entry["is" if day < search.IN_SAMPLE_DAYS else "oos"] += pnl - 2 * search.COST * shares
        winners = sorted(symbol for symbol, value in by_symbol.items() if value["is"] > 0)
        picked_trades = [trade for trade in base if trade[0] in winners]
        single, picked = one_account(combo)
        item = {
            "id": combo["id"], "categories": [p["category"] for p in summary["picks"] if p["id"] == combo["id"]],
            "base": {part: search.slim(search.metrics(base, span, symbols)) for part, span in
                     (("is", in_days), ("oos", out_days), ("all", all_days))},
            "rejected": base_actions.get("rejected", 0),
            "cost": {str(cost): search.slim(search.metrics(base, all_days, symbols, cost))
                     for cost in (0.0, 0.01, 0.03)},
            "delay": {part: search.slim(search.metrics(delay, span, symbols)) for part, span in
                      (("oos", out_days), ("all", all_days))},
            "delay_rejected": delay_actions.get("rejected", 0),
            "risk100": search.slim(search.metrics(risk100, all_days, symbols)),
            "tm_off": search.slim(search.metrics(tm_off, tm_days, symbols)),
            "tm": search.slim(search.metrics(tm, tm_days, symbols)),
            "tm_skipped": tm_actions.get("skipped by the filter", 0),
            "stock_picking": {"in_sample_winners": winners, "count": len(winners),
                              "oos_winners_only": search.slim(search.metrics(picked_trades, out_days, winners))
                              if winners else None,
                              "oos_all_stocks": search.slim(search.metrics(base, out_days, symbols))},
            "by_symbol": {symbol: {"is": round(value["is"], 2), "oos": round(value["oos"], 2)}
                          for symbol, value in sorted(by_symbol.items())},
            "one_account": {"all": search.slim(account_metrics(single, all_days)),
                            "is": search.slim(account_metrics(single, in_days)),
                            "oos": search.slim(account_metrics(single, out_days)),
                            "trades_by_symbol": picked},
        }
        report["combos"].append(item)
        print("%-44s all %8.2f  oos %8.2f  delay oos %8.2f  tm %8.2f vs %8.2f  one account %8.2f"
              % (combo["id"], item["base"]["all"]["net"], item["base"]["oos"]["net"], item["delay"]["oos"]["net"],
                 item["tm"]["net"], item["tm_off"]["net"], item["one_account"]["all"]["net"]), flush=True)
    name = "stress.json" if not argv else "stress-check.json"
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    print("wrote results/%s after %.0f s" % (name, time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
