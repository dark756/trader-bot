"""Paper-trade the pack over recorded TSLA sessions: one scenario per trading choice.

Each scenario runs the real tools, one session at a time, on a pinned clock
(practice runs), in its own trader folder (TRADER_HOME):

  entry.py watch -> exit.py plan -> timemachine.py run -> size.py -> check.py -> ticket.py show

Nobody says yes and nothing is placed: CLAUDE.md forbids approving an order on
the user's behalf, even a practice one. Where the ticket is READY, a simulated
paper broker takes the exact place_stock_order arguments the ticket shows and
fills the bracket against that session's own 5-minute bars (rules in README.md).

    python -B paper-trading-scenarios/run_scenarios.py            # all 13
    python -B paper-trading-scenarios/run_scenarios.py S01 S07    # some

Standard library only. It rewrites the folders of the scenarios it runs.
"""

import concurrent.futures
import csv
import datetime
import gzip
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA_REL = "tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz"
DATA = os.path.join(REPO, DATA_REL)
EXAMPLE_CONFIG = os.path.join(REPO, "trader-budget", "config.example.json")
SYMBOL = "TSLA"
SESSION_BARS = 78         # 9:30 to 15:55, five minutes each
HISTORY_NEEDED = 20       # the time machine wants 20 full sessions before the day
START_CASH = 5000.0
DAY_FILES = ["entry.json", "plan.json", "timemachine.json", "orders.json", os.path.join("practice", "state", "risk.json")]

SCENARIOS = [
    {"id": "S01-baseline", "feature": "Baseline",
     "choice": "The README's rules: 15-minute range, stop at the range midpoint, 1R target, market entry, $50 a trade",
     "exit": ["--target", "1R"]},
    {"id": "S02-stop-range-low", "feature": "Stop rule", "choice": "Stop at the opening-range low (wider)",
     "exit": ["--stop", "or-low", "--target", "1R"]},
    {"id": "S03-stop-atr", "feature": "Stop rule", "choice": "Stop 1.5 x ATR(14) of the 5-minute bars under the entry",
     "exit": ["--stop", "atr:1.5", "--target", "1R", "--offline", DATA_REL]},
    {"id": "S04-stop-1pct", "feature": "Stop rule", "choice": "Stop 1 % under the entry",
     "exit": ["--stop", "pct:1", "--target", "1R"]},
    {"id": "S05-target-1.5R", "feature": "Target", "choice": "Target at 1.5R",
     "exit": ["--target", "1.5R"]},
    {"id": "S06-target-2R", "feature": "Target", "choice": "Target at 2R (trader-exit's own default)",
     "exit": ["--target", "2R"]},
    {"id": "S07-limit-entry", "feature": "Entry order", "choice": "Limit entry at the cap (size.py --limit cap)",
     "exit": ["--target", "1R"], "size": ["--limit", "cap"]},
    {"id": "S08-30min-range", "feature": "Opening range", "choice": "30-minute opening range",
     "entry": ["--or-minutes", "30"], "exit": ["--target", "1R"]},
    {"id": "S09-volume-check", "feature": "Volume check",
     "choice": "Breakout bar needs 1.5 x the range bars' average volume",
     "entry": ["--min-volume-x", "1.5"], "exit": ["--target", "1R"], "tm": ["--min-volume-x", "1.5"]},
    {"id": "S10-prior-day-high", "feature": "Setup",
     "choice": "Level setup: a close above the previous session's high, stop 1 % under",
     "level": "prior_high", "exit": ["--stop", "pct:1", "--target", "1R"]},
    {"id": "S11-risk-100", "feature": "Risk per trade", "choice": "$100 max loss a trade ($300 a day)",
     "exit": ["--target", "1R"], "config": {"max_loss_per_trade_usd": 100, "max_daily_loss_usd": 300}},
    {"id": "S12-morning-only", "feature": "Entry window", "choice": "No new entries after 11:00 (morning only)",
     "exit": ["--target", "1R"], "config": {"no_entries_after": "11:00"}},
    {"id": "S13-time-machine-filter", "feature": "Time Machine filter",
     "choice": "Trade only when the Time Machine has target-first above stop-first",
     "exit": ["--target", "1R"], "tm_filter": True},
]

CSV_FIELDS = ["date", "weekday", "action", "reason", "signal_time", "signal_price", "or_high", "or_low", "level",
              "plan_entry", "stop", "target", "reward_risk", "tm_p_target", "tm_p_stop", "tm_p_neither", "tm_verdict",
              "order_type", "limit_price", "shares", "limit_binding", "risk_verdict", "gate_max_loss",
              "ticket_status", "fill_time", "fill_price", "fill_minus_plan", "exit_time", "exit_price",
              "exit_reason", "stop_and_target_same_bar", "pnl", "r_multiple", "loss_at_stop_after_fill",
              "max_open_loss", "max_open_loss_time", "max_open_gain", "cash_after"]


def load_sessions():
    """The data file -> ({date: [bar, ...]}, the dates with all 78 bars), New York time."""
    with gzip.open(DATA, "rt", encoding="utf-8") as file:
        result = json.load(file)["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    sessions = {}
    for index, stamp in enumerate(result["timestamp"]):
        values = [quote[key][index] for key in ("open", "high", "low", "close", "volume")]
        if None in values[:4]:
            continue
        # every session in this file falls in daylight-saving time, so New York is UTC-4
        eastern = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(stamp) - 4 * 3600)
        if not datetime.date(2026, 3, 8) < eastern.date() < datetime.date(2026, 11, 1):
            raise SystemExit("%s is outside daylight-saving time: this script assumes UTC-4" % eastern.date())
        clock = eastern.strftime("%H:%M")
        if "09:30" <= clock <= "15:55" and eastern.minute % 5 == 0:
            open_price, high, low, close, volume = values
            sessions.setdefault(eastern.date().isoformat(), []).append(
                {"start": clock, "o": open_price, "h": high, "l": low, "c": close, "v": volume or 0})
    full = [day for day in sorted(sessions) if len(sessions[day]) == SESSION_BARS]
    return sessions, full


def read_json(path):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")


def run(log, home, args):
    """Run one tool from the repo root with TRADER_HOME set, and log it word for word."""
    env = dict(os.environ, TRADER_HOME=home, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-B"] + args, cwd=REPO, env=env, capture_output=True, text=True,
                          encoding="utf-8")
    shown = ['"%s"' % arg if " " in arg else arg for arg in ["python"] + args]
    log.write("$ %s\n" % " ".join(shown).replace(home.replace("\\", "/"), "$TRADER_HOME").replace(home, "$TRADER_HOME"))
    log.write(done.stdout)
    if done.stderr:
        log.write(done.stderr)
    log.write("[exit %d]\n\n" % done.returncode)
    return done


def last_line(done):
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[-1] if lines else "exit %d" % done.returncode


def clock_of(ampm):
    return datetime.datetime.strptime(ampm, "%I:%M %p").strftime("%H:%M")


def paper_broker(order, bars, signal_index, flat_by, cancel_at):
    """Fill a bracket order on the bars after the signal, the way a paper broker would.

    The entry: a market order fills at the next bar's open, the first price after
    the signal. A buy limit fills at the open when the open is at or under the
    limit, else at the limit when the bar trades down to it; it is canceled if it
    has not filled by the end of the entry window. Then the two exit legs: the
    stop sells at the stop, or at the open when a bar opens below it (a gap); the
    take-profit sells at the target, or at the open when a bar opens above it.
    When one bar reaches both, the stop is taken (bars do not say which came
    first). Anything still open at the flat-by time sells at that bar's open.
    The open loss of each bar is marked at its low, or at the stop fill."""
    qty = int(order["qty"])
    stop = float(order["stop_loss_stop_price"])
    target = float(order["take_profit_limit_price"])
    limit = float(order["limit_price"]) if order.get("type") == "limit" else None
    fill_index = fill_price = None
    mid_bar = False
    for index in range(signal_index + 1, len(bars)):
        bar = bars[index]
        if limit is None:
            fill_index, fill_price = index, bar["o"]
            break
        if bar["start"] >= cancel_at:
            break
        if bar["o"] <= limit:
            fill_index, fill_price = index, bar["o"]
            break
        if bar["l"] <= limit:
            fill_index, fill_price, mid_bar = index, limit, True
            break
    if fill_index is None:
        return {"filled": False, "events": ["the limit %.2f never traded by %s: canceled, no position" % (limit, cancel_at)]}
    fill_price = round(fill_price, 2)
    how = "market: the %s bar's open" % bars[fill_index]["start"] if limit is None else (
        "limit %.2f, mid-bar" % limit if mid_bar else "limit %.2f: the %s bar opened under it" % (limit, bars[fill_index]["start"]))
    events = ["%s filled: bought %d %s at %.2f (%s)" % (bars[fill_index]["start"], qty, SYMBOL, fill_price, how),
              "bracket legs working: stop-loss %.2f, take-profit %.2f" % (stop, target)]
    marks = []
    exit_index = exit_price = exit_reason = None
    both = False
    for index in range(fill_index, len(bars)):
        bar = bars[index]
        if bar["start"] >= flat_by:
            exit_index, exit_price, exit_reason = index, round(bar["o"], 2), "flat by"
            marks.append({"time": bar["start"], "low": bar["o"], "high": bar["o"], "close": bar["o"],
                          "worst": exit_price, "best": exit_price})
            events.append("%s flat-by: sold %d at the open, %.2f" % (bar["start"], qty, exit_price))
            break
        first_bar = index == fill_index
        filled_mid_bar = first_bar and mid_bar         # its high came before the fill
        open_price = fill_price if first_bar else bar["o"]
        stop_hit = bar["l"] <= stop
        target_hit = bar["h"] >= target and not filled_mid_bar
        worst, best = bar["l"], fill_price if filled_mid_bar else min(bar["h"], target)
        if stop_hit:
            both = target_hit
            exit_price = stop if filled_mid_bar else min(open_price, stop)
            exit_index, exit_reason = index, "stop"
            worst, best = exit_price, open_price
            events.append("%s stop-loss filled: sold %d at %.2f%s%s" % (
                bar["start"], qty, exit_price, " (the bar opened under the stop: a gap)" if exit_price < stop else "",
                "; the same bar also reached the target, the stop is assumed first" if both else ""))
        elif target_hit:
            exit_price = max(open_price, target)
            exit_index, exit_reason = index, "target"
            best = exit_price
            events.append("%s take-profit filled: sold %d at %.2f" % (bar["start"], qty, exit_price))
        marks.append({"time": bar["start"], "low": bar["l"], "high": bar["h"], "close": bar["c"],
                      "worst": round(worst, 2), "best": round(best, 2)})
        if exit_reason:
            exit_price = round(exit_price, 2)
            break
    if exit_reason is None:
        exit_index, exit_price, exit_reason = len(bars) - 1, round(bars[-1]["c"], 2), "last bar"
    for mark in marks:
        mark["open_pnl_worst"] = round((mark["worst"] - fill_price) * qty, 2)
        mark["open_pnl_best"] = round((mark["best"] - fill_price) * qty, 2)
    deepest = min(marks, key=lambda mark: mark["open_pnl_worst"])
    highest = max(marks, key=lambda mark: mark["open_pnl_best"])
    pnl = round((exit_price - fill_price) * qty, 2)
    events.append("P&L %+.2f; deepest open loss %.2f at %s; best open gain %+.2f at %s" % (
        pnl, min(0.0, deepest["open_pnl_worst"]), deepest["time"], max(0.0, highest["open_pnl_best"]),
        highest["time"]))
    return {"filled": True, "qty": qty, "fill_time": bars[fill_index]["start"], "fill_price": fill_price,
            "stop": stop, "target": target, "exit_time": bars[exit_index]["start"], "exit_price": exit_price,
            "exit_reason": exit_reason, "stop_and_target_same_bar": both, "pnl": pnl,
            "max_open_loss": min(0.0, deepest["open_pnl_worst"]), "max_open_loss_time": deepest["time"],
            "max_open_gain": max(0.0, highest["open_pnl_best"]),
            "loss_at_stop_after_fill": round((stop - fill_price) * qty, 2), "events": events, "marks": marks}


def trimmed_time_machine(doc):
    """timemachine.json without its 2,000 drawn paths and bands (about 160 KB a day)."""
    doc = json.loads(json.dumps(doc))
    for key in ("bands", "band_times", "sample_paths", "hist"):
        (doc.get("mc") or {}).pop(key, None)
    doc["trimmed"] = "mc.bands, mc.band_times, mc.sample_paths and mc.hist removed to save space"
    return doc


def archive(home, day_folder, extra):
    os.makedirs(day_folder, exist_ok=True)
    for name in DAY_FILES:
        doc = read_json(os.path.join(home, name))
        if doc is not None:
            if name == "timemachine.json":
                doc = trimmed_time_machine(doc)
            write_json(os.path.join(day_folder, os.path.basename(name)), doc)
    for name, doc in extra.items():
        write_json(os.path.join(day_folder, name), doc)


def run_day(scenario, config, home, folder, log, day, bars, previous_bars, cash):
    weekday = datetime.date.fromisoformat(day).strftime("%a")
    log.write("=" * 100 + "\n%s %s  account cash $%.2f\n" % (weekday, day, cash) + "=" * 100 + "\n")
    for name in DAY_FILES:                      # a new morning: no file from yesterday may be planned again
        if os.path.exists(os.path.join(home, name)):
            os.remove(os.path.join(home, name))
    write_json(os.path.join(home, "practice", "portfolio.json"), {"cash": round(cash, 2), "positions": []})
    row = {"date": day, "weekday": weekday, "cash_after": round(cash, 2)}
    entry_args = list(scenario.get("entry", []))
    if scenario.get("level") == "prior_high":
        row["level"] = round(max(bar["h"] for bar in previous_bars), 2)
        entry_args += ["--setup", "level", "--level", "%.2f" % row["level"]]
    done = run(log, home, ["trader-entry/entry.py", "watch", SYMBOL, "--offline", DATA_REL, "--now", day + " 09:30",
                           "--until", config["no_entries_after"]] + entry_args)
    entry = read_json(os.path.join(home, "entry.json"))
    if done.returncode != 0 or not entry or entry.get("status") != "triggered":
        row.update(action="no signal", reason=last_line(done))
        return row, None
    clock = clock_of(entry["time"])
    signal_index = [bar["start"] for bar in bars].index(entry["bar_start"])
    now = "%s %s" % (day, clock)
    opening_range = entry.get("opening_range") or {}
    row.update(signal_time=clock, signal_price=entry.get("price"), or_high=opening_range.get("high"),
               or_low=opening_range.get("low"))
    day_folder = os.path.join(folder, "days", day)

    done = run(log, home, ["trader-exit/exit.py", "plan", "--now", now, "--write"] + scenario.get("exit", []))
    plan = read_json(os.path.join(home, "plan.json"))
    if done.returncode != 0 or not plan:
        row.update(action="no plan", reason=last_line(done))
        archive(home, day_folder, {})
        return row, None
    row.update(plan_entry=plan["entry"], stop=plan["stop"], target=plan["target"], reward_risk=plan["reward_risk"])

    done = run(log, home, ["trader-timemachine/timemachine.py", "run", "--plan", home + "/plan.json", "--offline",
                           DATA_REL, "--now", now, "--write"] + scenario.get("tm", []))
    forecast = read_json(os.path.join(home, "timemachine.json")) if done.returncode == 0 else None
    if forecast:
        row.update(tm_p_target=forecast["mc"]["p_target"], tm_p_stop=forecast["mc"]["p_stop"],
                   tm_p_neither=forecast["mc"]["p_neither"], tm_verdict=forecast.get("verdict"))
    if scenario.get("tm_filter"):
        if not forecast or forecast["mc"]["p_target"] <= forecast["mc"]["p_stop"]:
            row.update(action="skipped by the filter", reason="the Time Machine had stop-first at or above "
                       "target-first" if forecast else "no Time Machine forecast: " + last_line(done))
            log.write("[filter] %s: no order today.\n\n" % row["reason"])
            archive(home, day_folder, {})
            return row, None

    done = run(log, home, ["trader-budget/size.py", "--portfolio", home + "/practice/portfolio.json", "--now", now,
                           "--write"] + scenario.get("size", []))
    orders = read_json(os.path.join(home, "orders.json"))
    if done.returncode != 0 or not orders:
        row.update(action="not sized", reason=last_line(done))
        archive(home, day_folder, {})
        return row, None
    order = orders["orders"][0]
    binding = re.search(r"bound by your (.+?) \(", order.get("why") or "")
    row.update(order_type=order.get("type"), limit_price=order.get("limit_price"), shares=int(order["qty"]),
               limit_binding=binding.group(1) if binding else None)

    done = run(log, home, ["trader-risk/check.py", "--now", now])
    risk = read_json(os.path.join(home, "practice", "state", "risk.json")) or {}
    result = (risk.get("results") or [{}])[0]
    row.update(risk_verdict=result.get("verdict") or "error", gate_max_loss=result.get("max_loss"))
    if result.get("verdict") != "PASS":
        row.update(action="blocked by risk", reason="; ".join(result.get("reasons") or [last_line(done)]))
        archive(home, day_folder, {})
        return row, None

    run(log, home, ["trader-broker/ticket.py", "show", "--now", now])
    env = dict(os.environ, TRADER_HOME=home, PYTHONIOENCODING="utf-8")
    shown = subprocess.run([sys.executable, "-B", "trader-broker/ticket.py", "show", "--now", now, "--json"],
                           cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
    tickets = json.loads(shown.stdout) if shown.returncode == 0 else []
    ticket = tickets[0] if tickets else {}
    row["ticket_status"] = ticket.get("status") or last_line(shown)
    if not ticket.get("ready"):
        row.update(action="ticket not ready", reason=row["ticket_status"])
        archive(home, day_folder, {"ticket.json": tickets})
        return row, None

    log.write("[paper broker, simulated] nobody typed yes: this stands in for the yes, Allow once and Alpaca paper.\n")
    log.write("place_stock_order %s\n" % json.dumps(ticket["tool_input"]))
    fill = paper_broker(ticket["tool_input"], bars, signal_index, plan["flat_by"], config["no_entries_after"])
    for event in fill["events"]:
        log.write("  %s\n" % event)
    log.write("\n")
    if not fill["filled"]:
        row.update(action="not filled", reason=fill["events"][0])
        archive(home, day_folder, {"ticket.json": tickets, "paper-broker.json": fill})
        return row, None
    cash += fill["pnl"]
    gate = row["gate_max_loss"] or 0
    row.update(action="traded", fill_time=fill["fill_time"], fill_price=fill["fill_price"],
               fill_minus_plan=round(fill["fill_price"] - plan["entry"], 2), exit_time=fill["exit_time"],
               exit_price=fill["exit_price"], exit_reason=fill["exit_reason"],
               stop_and_target_same_bar=fill["stop_and_target_same_bar"], pnl=fill["pnl"],
               r_multiple=round(fill["pnl"] / gate, 3) if gate else None,
               loss_at_stop_after_fill=fill["loss_at_stop_after_fill"], max_open_loss=fill["max_open_loss"],
               max_open_loss_time=fill["max_open_loss_time"], max_open_gain=fill["max_open_gain"],
               cash_after=round(cash, 2))
    write_json(os.path.join(home, "practice", "portfolio.json"), {"cash": round(cash, 2), "positions": []})
    archive(home, day_folder, {"ticket.json": tickets, "paper-broker.json": fill})
    return row, fill


def summarize(scenario, config, rows, fills):
    trades = [row for row in rows if row.get("action") == "traded"]
    count = lambda action: sum(1 for row in rows if row.get("action") == action)
    pnls = [row["pnl"] for row in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    peak, peak_day, drawdown = START_CASH, "the start", (0.0, None, None)
    for row in rows:
        equity = row["cash_after"]
        if equity > peak:
            peak, peak_day = equity, row["date"]
        if peak - equity > drawdown[0]:
            drawdown = (round(peak - equity, 2), peak_day, row["date"])
    streak = longest = 0
    for row in trades:
        streak = streak + 1 if row["pnl"] < 0 else 0
        longest = max(longest, streak)
    worst = min(trades, key=lambda row: row["pnl"]) if trades else None
    deepest = min(trades, key=lambda row: row["max_open_loss"]) if trades else None
    widest = min(trades, key=lambda row: row["loss_at_stop_after_fill"]) if trades else None
    over = [row for row in trades if row["pnl"] < 0 and -row["pnl"] > (row["gate_max_loss"] or 0) + 0.005]
    over_limit = [row for row in trades if row["pnl"] < -config["max_loss_per_trade_usd"] - 0.005]
    targets = sum(1 for row in trades if row["exit_reason"] == "target")
    stops = sum(1 for row in trades if row["exit_reason"] == "stop")
    with_tm = [row for row in trades if row.get("tm_p_target") is not None]
    mean = lambda values: round(sum(values) / len(values), 2) if values else None
    return {
        "id": scenario["id"], "feature": scenario["feature"], "choice": scenario["choice"],
        "config_changes": scenario.get("config", {}), "sessions": len(rows),
        "signals": len(rows) - count("no signal"), "no_signal": count("no signal"), "no_plan": count("no plan"),
        "skipped_by_filter": count("skipped by the filter"), "not_sized": count("not sized"),
        "blocked_by_risk": count("blocked by risk"), "ticket_not_ready": count("ticket not ready"),
        "not_filled": count("not filled"), "trades": len(trades), "targets": targets, "stops": stops,
        "flat_by": sum(1 for row in trades if row["exit_reason"] == "flat by"),
        "stop_and_target_same_bar": sum(1 for row in trades if row["stop_and_target_same_bar"]),
        "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "net_pnl": round(sum(pnls), 2), "avg_pnl": mean(pnls), "avg_win": mean(wins), "avg_loss": mean(losses),
        "avg_r": round(sum(row["r_multiple"] for row in trades) / len(trades), 3) if trades else None,
        "avg_shares": mean([row["shares"] for row in trades]),
        "avg_position_usd": mean([row["shares"] * row["fill_price"] for row in trades]),
        "avg_gate_max_loss": mean([row["gate_max_loss"] for row in trades]),
        "avg_fill_minus_plan": round(sum(row["fill_minus_plan"] for row in trades) / len(trades), 3) if trades else None,
        "worst_trade": {"date": worst["date"], "pnl": worst["pnl"], "exit_reason": worst["exit_reason"],
                        "gate_max_loss": worst["gate_max_loss"]} if worst else None,
        "best_trade": max(pnls) if pnls else None,
        "deepest_open_loss": {"date": deepest["date"], "time": deepest["max_open_loss_time"],
                              "loss": deepest["max_open_loss"], "trade_pnl": deepest["pnl"],
                              "exit_reason": deepest["exit_reason"], "shares": deepest["shares"],
                              "gate_max_loss": deepest["gate_max_loss"]} if deepest else None,
        "widest_loss_at_stop_after_fill": {"date": widest["date"], "loss": widest["loss_at_stop_after_fill"],
                                           "gate_max_loss": widest["gate_max_loss"]} if widest else None,
        "losses_over_gate_max_loss": len(over), "losses_over_config_limit": len(over_limit),
        "max_drawdown": {"amount": drawdown[0], "from": drawdown[1], "to": drawdown[2]},
        "longest_losing_streak": longest, "end_cash": rows[-1]["cash_after"] if rows else START_CASH,
        "tm_avg_p_target": round(sum(row["tm_p_target"] for row in with_tm) / len(with_tm), 3) if with_tm else None,
        "tm_avg_p_stop": round(sum(row["tm_p_stop"] for row in with_tm) / len(with_tm), 3) if with_tm else None,
        "actual_target_share": round(targets / (targets + stops), 3) if targets + stops else None,
    }


def run_scenario(scenario, sessions, days, full):
    folder = os.path.join(HERE, scenario["id"])
    if os.path.isdir(folder):              # empty it, not remove it: Windows refuses while a window has it open
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    home = os.path.join(folder, "trader-home")
    os.makedirs(os.path.join(home, "practice"))
    with open(EXAMPLE_CONFIG, encoding="utf-8") as file:
        config = json.load(file)
    config.update(scenario.get("config", {}))
    write_json(os.path.join(folder, "config.json"), config)
    write_json(os.path.join(home, "config.json"), config)
    cash, rows, fills = START_CASH, [], {}
    with open(os.path.join(folder, "run.log"), "w", encoding="utf-8", newline="\n") as log:
        log.write("%s: %s\n%s\nEvery command below ran from the repo root with TRADER_HOME=%s.\n"
                  "Practice runs (pinned clock); nothing was approved or placed.\n\n"
                  % (scenario["id"], scenario["feature"], scenario["choice"], os.path.relpath(home, REPO)))
        for day in days:
            previous_bars = sessions[full[full.index(day) - 1]]
            row, fill = run_day(scenario, config, home, folder, log, day, sessions[day], previous_bars, cash)
            rows.append(row)
            cash = row["cash_after"]
            if fill:
                fills[day] = fill
    with open(os.path.join(folder, "trades.csv"), "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
    summary = summarize(scenario, config, rows, fills)
    write_json(os.path.join(folder, "summary.json"), summary)
    return summary


def main(argv):
    sessions, full = load_sessions()
    days = full[HISTORY_NEEDED:]
    chosen = [s for s in SCENARIOS if not argv or any(s["id"].startswith(arg) for arg in argv)]
    print("%d full sessions %s to %s; trading the %d from %s (20 earlier sessions for the Time Machine)"
          % (len(full), full[0], full[-1], len(days), days[0]))
    write_json(os.path.join(HERE, "scenarios.json"), {"data": DATA_REL, "symbol": SYMBOL, "sessions": days,
                                                      "start_cash": START_CASH, "scenarios": SCENARIOS})
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chosen), os.cpu_count() or 4)) as pool:
        summaries = list(pool.map(lambda s: run_scenario(s, sessions, days, full), chosen))
    for summary in summaries:
        print("%-24s trades %2d  target %2d  stop %2d  flat %2d  net %+9.2f  worst %+8.2f  deepest open %+8.2f"
              % (summary["id"], summary["trades"], summary["targets"], summary["stops"], summary["flat_by"],
                 summary["net_pnl"], summary["worst_trade"]["pnl"] if summary["worst_trade"] else 0,
                 summary["deepest_open_loss"]["loss"] if summary["deepest_open_loss"] else 0))
    every = [read_json(os.path.join(HERE, s["id"], "summary.json")) for s in SCENARIOS]
    every = [summary for summary in every if summary]
    with open(os.path.join(HERE, "summary.csv"), "w", encoding="utf-8", newline="") as file:
        fields = ["id", "feature", "choice", "sessions", "signals", "trades", "targets", "stops", "flat_by",
                  "not_filled", "skipped_by_filter", "blocked_by_risk", "win_rate", "net_pnl", "avg_pnl", "avg_r",
                  "avg_shares", "avg_gate_max_loss", "worst_trade_pnl", "deepest_open_loss", "max_drawdown",
                  "longest_losing_streak", "losses_over_gate_max_loss", "end_cash"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for summary in every:
            flat = {key: summary.get(key) for key in fields}
            flat["worst_trade_pnl"] = summary["worst_trade"]["pnl"] if summary["worst_trade"] else ""
            flat["deepest_open_loss"] = summary["deepest_open_loss"]["loss"] if summary["deepest_open_loss"] else ""
            flat["max_drawdown"] = summary["max_drawdown"]["amount"]
            writer.writerow(flat)


if __name__ == "__main__":
    main(sys.argv[1:])
