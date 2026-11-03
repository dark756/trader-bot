"""Does the Time Machine filter pick better trades? A wider test than stress.py's.

    python -B paper-trading-scenarios/feature-search/tm_filter.py     # about 80 minutes on 4 cores

Every setup and stop with a 1R target, market entry, no volume check and entries until 15:30 (26 combos),
each with and without the filter (trade only when the Time Machine has target-first above stop-first), on
the same sessions: those with 20 earlier sessions for the Time Machine's pool. A 1R target because with a
farther target the Time Machine almost always has stop-first ahead, so the filter skips nearly every trade.

Each Time Machine call is the tool's own 10,000 replays (about 1.3 s), so the work goes one stock at a time
and each stock is saved to results/tm_filter-parts/ when it is done: a run that is stopped picks up where it
was. Writes results/tm_filter.json at the end. Standard library only.
"""

import json
import os
import shutil
import sys
import time

import engine
import search

PARTS = os.path.join(search.OUT, "tm_filter-parts")
STATE = {}


def compact(rows):
    return [(row["symbol"], STATE["day_index"][row["date"]], row["pnl"], row["shares"], row["gate_max_loss"],
             row["exit_reason"], row["max_open_loss"]) for row in rows if row["action"] == "traded"]


def symbol_task(symbol):
    machine = engine.TimeMachine(os.path.join(search.DATA, symbol + ".json.gz"), symbol)
    data = STATE["universe"][symbol]
    out = {}
    for combo in STATE["combos"]:
        signals = STATE["signals"][search.signal_key(combo)][symbol]

        def skip(row, signals=signals):
            odds = machine.odds(row["date"], row["plan_entry"], row["stop"], row["target"],
                                signals[row["date"]]["bar_index"])
            return odds is None or odds[0] <= odds[1]
        out[combo["id"]] = {
            "without": compact(engine.run_account(data, STATE["days"], combo, signals)),
            "with": compact(engine.run_account(data, STATE["days"], combo, signals, skip=skip)),
        }
    path = os.path.join(PARTS, symbol + ".json")
    with open(path + ".tmp", "w", encoding="utf-8") as file:
        json.dump(out, file)
    os.replace(path + ".tmp", path)
    return symbol


def load_state():
    """Everything a worker needs, into STATE: the bars, the 26 combos and their signals on the filter's sessions."""
    universe, common = search.load_universe()
    days = common[1:]
    tm_from = next(index for index, day in enumerate(days)
                   if len([d for d in common if d < day]) >= engine.timemachine.MIN_SESSIONS)
    combos = []
    for setup, minutes in search.SETUPS:
        for stop in (search.STOPS_LEVEL if setup == "pdh" else search.STOPS_ORB):
            combo = {"setup": setup, "or_minutes": minutes, "stop": stop, "target": "1R", "entry": "market",
                     "volume": None, "cutoff": "15:30", "risk": 50}
            combo["id"] = search.combo_id(combo)
            combos.append(combo)
    run_days = days[tm_from:]
    STATE.update(universe=universe, days=run_days, combos=combos,
                 signals=search.build_signals(universe, run_days, combos),
                 day_index={day: number for number, day in enumerate(run_days)})


def main(argv):
    started = time.time()
    os.makedirs(PARTS, exist_ok=True)
    load_state()
    universe, run_days, combos = STATE["universe"], STATE["days"], STATE["combos"]
    symbols = sorted(universe)
    todo = [symbol for symbol in symbols if not os.path.exists(os.path.join(PARTS, symbol + ".json"))]
    print("%d combos x %d stocks, %d sessions from %s; %d stocks left to run"
          % (len(combos), len(symbols), len(run_days), run_days[0], len(todo)), flush=True)
    with search.make_pool(load_state) as pool:
        for done, symbol in enumerate(pool.imap_unordered(symbol_task, todo), 1):
            print("  %s done (%d of %d) after %.0f s" % (symbol, done, len(todo), time.time() - started), flush=True)
    parts = {}
    for symbol in symbols:
        with open(os.path.join(PARTS, symbol + ".json"), encoding="utf-8") as file:
            parts[symbol] = json.load(file)
    span = list(range(len(run_days)))
    report = {"from": run_days[0], "to": run_days[-1], "sessions": len(run_days), "combos": []}
    for combo in combos:
        trades = {side: [tuple(trade) for symbol in symbols for trade in parts[symbol][combo["id"]][side]]
                  for side in ("without", "with")}
        item = {"id": combo["id"], "without": search.slim(search.metrics(trades["without"], span, symbols)),
                "with": search.slim(search.metrics(trades["with"], span, symbols))}
        report["combos"].append(item)
        print("%-44s without: %4d trades %9.2f %7.3fR   with: %4d trades %9.2f %7.3fR"
              % (combo["id"], item["without"]["trades"], item["without"]["net"], item["without"]["avg_r"],
                 item["with"]["trades"], item["with"]["net"], item["with"]["avg_r"]))
    report.update(count=len(combos),
                  better_r=sum(1 for item in report["combos"] if item["with"]["avg_r"] > item["without"]["avg_r"]),
                  better_net=sum(1 for item in report["combos"] if item["with"]["net"] > item["without"]["net"]),
                  positive_with=sum(1 for item in report["combos"] if item["with"]["net"] > 0),
                  positive_without=sum(1 for item in report["combos"] if item["without"]["net"] > 0))
    with open(os.path.join(search.OUT, "tm_filter.json"), "w", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    shutil.rmtree(PARTS)
    print("R per trade better with the filter in %d of %d combos, net better in %d; above zero: %d with, %d "
          "without; %.0f s" % (report["better_r"], report["count"], report["better_net"], report["positive_with"],
                               report["positive_without"], time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
