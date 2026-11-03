"""Check the engine against the 13 TSLA scenarios that run the tools one command at a time.

    python -B paper-trading-scenarios/feature-search/validate.py

Every session of every scenario must come out the same: the action, the shares, the fill, the exit and the
result. Exit code 0 when all 507 sessions match.
"""

import csv
import json
import os
import sys

import engine

SCENARIOS = os.path.dirname(engine.HERE)
FIXTURE = os.path.join(engine.REPO, "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")
BASE = {"setup": "orb", "or_minutes": 15, "stop": "or-mid", "target": "1R", "entry": "market", "volume": None,
        "cutoff": "15:30", "risk": 50}
CHANGES = {
    "S01": {}, "S02": {"stop": "or-low"}, "S03": {"stop": "atr:1.5"}, "S04": {"stop": "pct:1"},
    "S05": {"target": "1.5R"}, "S06": {"target": "2R"}, "S07": {"entry": "cap"}, "S08": {"or_minutes": 30},
    "S09": {"volume": 1.5}, "S10": {"setup": "pdh", "stop": "pct:1"}, "S11": {"risk": 100},
    "S12": {"cutoff": "11:00"}, "S13": {"tm_filter": True},
}


def main():
    spec = json.load(open(os.path.join(SCENARIOS, "scenarios.json"), encoding="utf-8"))
    data = engine.load_symbol(FIXTURE, "TSLA")
    days = spec["sessions"]
    machine = engine.TimeMachine(FIXTURE, "TSLA")
    failures = 0
    for scenario in spec["scenarios"]:
        key = scenario["id"][:3]
        combo = dict(BASE, **CHANGES[key])
        signals = {day: engine.signal(data, day, combo["setup"], combo["or_minutes"], combo["volume"],
                                      combo["cutoff"]) for day in days}
        skip = None
        if combo.get("tm_filter"):
            def skip(row):
                odds = machine.odds(row["date"], row["plan_entry"], row["stop"], row["target"],
                                    signals[row["date"]]["bar_index"])
                return odds is None or odds[0] <= odds[1]
        mine = engine.run_account(data, days, combo, signals, skip=skip)
        with open(os.path.join(SCENARIOS, scenario["id"], "trades.csv"), encoding="utf-8") as file:
            published = list(csv.DictReader(file))
        bad = []
        for row, pub in zip(mine, published):
            if row["action"] != pub["action"]:
                bad.append((row["date"], "action", row["action"], pub["action"]))
                continue
            if row["action"] == "traded":
                for field in ("shares", "fill_price", "exit_price", "pnl"):
                    if abs(float(row[field]) - float(pub[field])) > 0.005:
                        bad.append((row["date"], field, row[field], pub[field]))
                if row["exit_reason"] != pub["exit_reason"]:
                    bad.append((row["date"], "exit_reason", row["exit_reason"], pub["exit_reason"]))
            if abs(row["cash_after"] - float(pub["cash_after"])) > 0.005:
                bad.append((row["date"], "cash_after", row["cash_after"], pub["cash_after"]))
        failures += len(bad)
        print("%-24s %2d sessions, %2d trades: %s" % (scenario["id"], len(mine),
                                                      sum(1 for row in mine if row["action"] == "traded"),
                                                      "match" if not bad else "%d DIFFERENCES" % len(bad)))
        for item in bad[:5]:
            print("    ", item)
    print("all sessions match" if not failures else "%d differences" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
