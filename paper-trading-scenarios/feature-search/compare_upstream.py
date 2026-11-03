"""The original day-trader-skill against this copy: the same backtest, then a few of the bugs CLAUDE.md lists.

    git clone --depth 1 https://github.com/Jakeschincariol/day-trader-skill /tmp/day-trader-skill
    python -B paper-trading-scenarios/feature-search/compare_upstream.py /tmp/day-trader-skill

1. Trading results. The original's own entry, exit, size, check and ticket code goes through engine.py on the
   same 30 stocks, sessions and costs as search.py, for every combination it can run (it has no --limit cap,
   so the limit entries are left out). Each combination is compared with this copy's row in results/combos.csv.
2. Safety. A few of the bugs CLAUDE.md lists, run against both versions' own functions: a config the original
   reads as "no limit", a reply it takes as a yes, and one broken alert rule.

Writes results/upstream.json. Standard library only.
"""

import csv
import importlib.util
import json
import os
import subprocess
import sys
import time
import io
import contextlib

import engine
import search

TOOLS = [("entry", "trader-entry", "entry"), ("exitplan", "trader-exit", "exit"), ("size", "trader-budget", "size"),
         ("check", "trader-risk", "check"), ("ticket", "trader-broker", "ticket"),
         ("alerts", "trader-alerts", "alerts")]


def load(root, tag):
    modules = {}
    for key, folder, name in TOOLS:
        spec = importlib.util.spec_from_file_location("%s_%s" % (tag, name), os.path.join(root, folder, name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[key] = module
    # names it had before this copy's renames: exit.ampm, and evaluate()'s cfg= (now config=)
    if not hasattr(modules["exitplan"], "ampm_text"):
        modules["exitplan"].ampm_text = modules["exitplan"].ampm
    evaluate = modules["entry"].evaluate
    if "cfg" in evaluate.__code__.co_varnames[:evaluate.__code__.co_argcount]:
        modules["entry"].evaluate = lambda chart, now, config=None, **options: evaluate(chart, now, cfg=config,
                                                                                         **options)
    return modules


def use(modules):
    """Point engine.py at another copy of the tools."""
    for key in ("entry", "exitplan", "size", "check", "ticket"):
        setattr(engine, key, modules[key])


def load_state(root):
    use(load(root, "upstream"))
    universe, common = search.load_universe()
    days = common[1:]
    combos = [combo for combo in search.grid() if combo["entry"] != "cap"]
    search.STATE.update(universe=universe, days=days, combos=combos,
                        signals=search.build_signals(universe, days, combos),
                        day_index={day: number for number, day in enumerate(days)})


def trading(root):
    load_state(root)
    combos = search.STATE["combos"]
    results = [None] * len(combos)
    with search.make_pool(load_state, root) as pool:
        for index, trades, counts in pool.imap_unordered(search.run_one, range(len(combos)), chunksize=4):
            results[index] = (trades, counts)
    days = search.STATE["days"]
    symbols = sorted(search.STATE["universe"])
    in_days = list(range(search.IN_SAMPLE_DAYS))
    out_days = list(range(search.IN_SAMPLE_DAYS, len(days)))
    mine = {row["id"]: row for row in csv.DictReader(open(os.path.join(search.OUT, "combos.csv"), encoding="utf-8"))}
    rows = []
    for combo, (trades, counts) in zip(combos, results):
        theirs = {part: search.metrics(trades, span, symbols) for part, span in
                  (("is", in_days), ("oos", out_days), ("all", in_days + out_days))}
        ours = mine[combo["id"]]
        rows.append({"id": combo["id"], "upstream_trades": theirs["all"]["trades"],
                     "this_trades": int(ours["all_trades"]),
                     "upstream_net": theirs["all"]["net"], "this_net": float(ours["all_net"]),
                     "upstream_is_net": theirs["is"]["net"], "upstream_oos_net": theirs["oos"]["net"],
                     "upstream_sharpe": theirs["all"]["sharpe"], "this_sharpe": float(ours["all_sharpe"])})
    same = [row for row in rows if row["upstream_trades"] == row["this_trades"]
            and abs(row["upstream_net"] - row["this_net"]) < 0.005]
    strategy = [row for row in rows if not row["id"].startswith("always")]
    return {"combos": len(rows), "identical": len(same), "different": [row for row in rows if row not in same],
            "upstream_positive_all": sum(1 for row in strategy if row["upstream_net"] > 0),
            "upstream_positive_both": sum(1 for row in strategy if row["upstream_is_net"] > 0
                                          and row["upstream_oos_net"] > 0),
            "upstream_best": sorted(strategy, key=lambda row: -row["upstream_sharpe"])[:5],
            "strategy_combos": len(strategy)}


def quietly(function, *args, **kwargs):
    """(result, error text) without letting the function print."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return function(*args, **kwargs), None
        except Exception as error:                       # either version may raise its own error class
            return None, "%s: %s" % (type(error).__name__, str(error).split("\n")[0][:160])


def safety(modules):
    """Three repros from CLAUDE.md's findings, on one version's own functions."""
    check, ticket, alerts = modules["check"], modules["ticket"], modules["alerts"]
    order = {"symbol": "TSLA", "side": "buy", "qty": "40", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32",
             "plan": {"entry": 364.18, "stop": 358.32, "target": 370.04}}
    now = check.eastern_to_utc(__import__("datetime").datetime(2026, 10, 19, 10, 15))
    out = {}
    # V1: a max loss of 0 or "50$" (a typo) for a trade that risks 40 x $5.86 = $234.40
    for label, value in (("max loss 0", 0), ("max loss \"50$\"", "50$")):
        config = dict(engine.EXAMPLE_CONFIG, max_loss_per_trade_usd=value, max_daily_loss_usd=100000,
                      max_position_pct=100, max_order_usd=100000, cash_buffer_pct=0)
        result, error = quietly(check.check, [order], config, {"cash": 100000.0, "positions": []}, None, None,
                                "2026-10-19", now)
        if error:
            out["V1 " + label] = "stops with an error: " + error
        else:
            row = result["results"][0]
            out["V1 " + label] = "%s, risk $%s%s" % (row["verdict"], row["max_loss"], "; " + "; ".join(
                reason.split(":")[0] for reason in row["reasons"]) if row["reasons"] else "")
    # V3: replies that are not a yes
    yes_order = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day"}
    for reply in ("yes never mind", "yes buy NVDA", "yes 8 shares and 500 NVDA"):
        result, error = quietly(ticket.parse_yes, reply, yes_order)
        out["V3 reply %r" % reply] = error or ("taken as a yes" if result and result[0] else "not a yes")
    # B5: one rule with a typo next to a good stop alert
    rules = [{"id": "stop", "type": "stop", "symbol": "TSLA", "price": 400.0},
             {"id": "typo", "type": "stop", "symbol": "TSLA", "price": "abc"}]
    quotes = {"TSLA": {"price": 360.0, "prev_close": 354.72, "time": now, "source": "test"}}
    status = alerts.market_status(now)
    result, error = quietly(alerts.evaluate, rules, quotes, status, {"rung": {}}, now)
    if error:
        out["B5 one bad alert rule"] = "the stop alert did not ring: " + error
    else:
        fired = result[0] if isinstance(result, tuple) else result
        names = [item.get("id") for item in (fired or []) if isinstance(item, dict)]
        out["B5 one bad alert rule"] = "the stop alert rang" if "stop" in names else "no alert rang (%s)" % names
    return out


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    root = os.path.abspath(argv[0])
    started = time.time()
    commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    report = {"upstream": {"repo": "https://github.com/Jakeschincariol/day-trader-skill", "commit": commit or None}}
    report["safety"] = {"upstream": safety(load(root, "up_safety")), "this copy": safety(load(engine.REPO, "me"))}
    for name, value in report["safety"]["upstream"].items():
        print("%-36s original: %-60s this copy: %s" % (name, value[:60], report["safety"]["this copy"][name][:70]))
    report["trading"] = trading(root)
    trade = report["trading"]
    print("trading: %d of %d combos identical to this copy (%.0f s)" % (trade["identical"], trade["combos"],
                                                                        time.time() - started))
    for row in trade["different"][:10]:
        print("  differs:", row)
    print("original, after costs: %d of %d strategy combos up over the 59 sessions, %d up in both halves"
          % (trade["upstream_positive_all"], trade["strategy_combos"], trade["upstream_positive_both"]))
    with open(os.path.join(search.OUT, "upstream.json"), "w", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, indent=2, default=str)
        file.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
