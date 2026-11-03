"""The backtest engine for the feature search: the pack's own rules, called in-process.

One rule set (a "combo") on one symbol's sessions, day by day, the way a scenario runs:

  entry.evaluate      the signal (the first closed bar over the range high or the level, in the window)
  exit.compute        the stop and the target, in exact cents (and exit.atr_of for an ATR stop)
  size.size_trade     whole shares inside the max loss, position cap, max order and cash limits;
                      size.limit_cap for a limit entry at the cap
  check.check         trader-risk's PASS or BLOCK for that exact order
  paper_broker        run_scenarios.py's simulated broker fills the bracket on the session's bars

Each symbol is its own $5,000 practice account: the cash carries from day to day and sizes the next
trade. A per-share cost (spread and slippage) comes off every fill. The tools never write a file here.
validate.py checks that this reproduces the 13 TSLA scenarios trade for trade.

Standard library only.
"""

import datetime
import gzip
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
START_CASH = 5000.0
SESSION_BARS = 78
FLAT_BY = "15:55"


def tool(folder, name):
    spec = importlib.util.spec_from_file_location("fs_" + name, os.path.join(REPO, folder, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = tool("trader-entry", "entry")
exitplan = tool("trader-exit", "exit")
size = tool("trader-budget", "size")
check = tool("trader-risk", "check")
ticket = tool("trader-broker", "ticket")
runner = tool("paper-trading-scenarios", "run_scenarios")
timemachine = tool("trader-timemachine", "timemachine")

with open(os.path.join(REPO, "trader-budget", "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE_CONFIG = json.load(_file)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return file.read()
    with open(path, encoding="utf-8") as file:
        return file.read()


def load_symbol(path, symbol):
    """One symbol's file -> {symbol, days: [iso, ...] (full sessions, oldest first), per day: the entry chart,
    the broker bars and the exit chart}. New York time comes from the tools' own clock."""
    text = read_text(path)
    chart = entry.parse_chart(text, symbol)
    exit_chart = exitplan.load_chart(symbol, "5m", None, offline=path)
    by_day = {}
    for bar in chart["bars"]:
        if bar["t"] % 300:
            continue
        eastern = entry.to_eastern(entry.utc_of(bar["t"]))
        clock = eastern.strftime("%H:%M")
        if "09:30" <= clock <= "15:55":
            by_day.setdefault(eastern.date().isoformat(), []).append((clock, bar))
    exit_by_day = {}                                   # exit.session_bars reads one day: hand it only that day
    for bar in exit_chart["bars"]:
        exit_by_day.setdefault(entry.to_eastern(entry.utc_of(bar["t"])).date().isoformat(), []).append(bar)
    days, sessions = [], {}
    for day in sorted(by_day):
        rows = sorted(by_day[day])
        if len(rows) != SESSION_BARS or len({clock for clock, _ in rows}) != SESSION_BARS:
            continue                                   # a short or gappy session: left out
        sessions[day] = {
            "chart": dict(chart, bars=[bar for _, bar in rows]),
            "bars": [{"start": clock, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}
                     for clock, bar in rows],
            "exit_chart": dict(exit_chart, bars=exit_by_day.get(day, [])),
        }
        days.append(day)
    return {"symbol": symbol, "days": days, "sessions": sessions}


def utc_at(day, clock):
    hour, minute = int(clock[:2]), int(clock[3:])
    return entry.eastern_to_utc(datetime.datetime.combine(datetime.date.fromisoformat(day),
                                                          datetime.time(hour, minute)))


def signal(data, day, setup, or_minutes, min_volume_x, cutoff):
    """entry.evaluate over the whole session: the first trigger, as entry.py watch finds it, or None."""
    config = dict(EXAMPLE_CONFIG, no_entries_after=cutoff)
    level = None
    if setup == "pdh":
        before = data["days"].index(day) - 1
        if before < 0:
            return None
        level = float("%.2f" % max(bar["h"] for bar in data["sessions"][data["days"][before]]["bars"]))
    result = entry.evaluate(data["sessions"][day]["chart"], utc_at(day, "16:00"),
                            setup="level" if setup == "pdh" else "orb", or_minutes=or_minutes, interval="5m",
                            level=level, min_volume_x=min_volume_x, config=config)
    if result["status"] != "triggered":
        return None
    trigger = result["trigger"]
    orr = result["opening_range"] or {}
    return {"price": trigger["price"], "bar_start": trigger["bar_start"], "bar_index": trigger["bar_index"],
            "time": trigger["close_time"], "or_high": orr.get("high"), "or_low": orr.get("low"), "level": level,
            "setup": "level" if setup == "pdh" else "orb"}


def config_for(combo):
    risk = combo.get("risk", 50)
    return dict(EXAMPLE_CONFIG, no_entries_after=combo["cutoff"], max_loss_per_trade_usd=risk,
                max_daily_loss_usd=3 * risk)


def trade_day(data, day, combo, cash, sig, cost_per_share=0.0, delay_bars=0):
    """One session under one combo. Returns a row: action, and for a trade the fill, exit and results."""
    row = {"symbol": data["symbol"], "date": day, "action": "no signal", "net": 0.0}
    if sig is None:
        return row
    config = config_for(combo)
    row.update(signal_time=sig["time"], signal_price=sig["price"])
    stop_rule, stop_param = exitplan.parse_stop(combo["stop"])
    target_rule, target_param = exitplan.parse_target(combo["target"])
    atr = atr_bars = None
    now = utc_at(day, sig["time"])
    try:
        if stop_rule == "atr":
            bars = exitplan.session_bars(data["sessions"][day]["exit_chart"], datetime.date.fromisoformat(day), "5m",
                                         sig["bar_index"], now)
            if len(bars) < 3:
                raise exitplan.PlanError("an atr stop needs at least 3 closed bars")
            atr, atr_bars = exitplan.atr_of(bars)
        levels = exitplan.compute(sig["price"], stop_rule, stop_param, target_rule, target_param,
                                  sig["or_high"], sig["or_low"], atr, atr_bars)
    except exitplan.PlanError as error:
        row.update(action="no plan", reason=str(error))
        return row
    plan = {"symbol": data["symbol"], "side": "buy", "date": day, "setup": sig["setup"],
            "entry": float(levels["entry"]), "stop": float(levels["stop"]), "target": float(levels["target"]),
            "entry_time": exitplan.ampm_text(datetime.time(int(sig["time"][:2]), int(sig["time"][3:]))),
            "stop_label": levels["stop_label"], "target_label": levels["target_label"], "flat_by": FLAT_BY}
    try:
        sizing = size.size_trade(plan, config, None, None, None, cash, day)
        if combo["entry"] == "cap" and not sizing["refusal"]:
            sizing = size.size_trade(plan, config, None, size.limit_cap(sizing, config), None, cash, day)
    except size.SizeError as error:
        row.update(action="not sized", reason=str(error))
        return row
    order = size.bracket_order(sizing)
    if order is None:
        row.update(action="not sized", reason=sizing["refusal"])
        return row
    verdict = check.check([order], config, {"cash": cash, "positions": []}, None, None, day, now)["results"][0]
    row.update(plan_entry=plan["entry"], stop=plan["stop"], target=plan["target"], shares=int(order["qty"]),
               gate_max_loss=verdict["max_loss"])
    if verdict["verdict"] != "PASS":
        row.update(action="blocked by risk", reason="; ".join(verdict["reasons"]))
        return row
    bars = data["sessions"][day]["bars"]
    signal_index = [bar["start"] for bar in bars].index(sig["bar_start"]) + delay_bars
    fill = runner.paper_broker(ticket.tool_input(order), bars, signal_index, FLAT_BY, combo["cutoff"])
    if not fill["filled"]:
        row.update(action="not filled", reason=fill["events"][0])
        return row
    if order["type"] == "market" and fill["fill_price"] - plan["stop"] < 0.01 - 1e-9:
        # Alpaca rejects a bracket whose stop is not at least $0.01 under the market price when it arrives
        row.update(action="rejected", reason="the price was at or under the stop when the order went in")
        return row
    cost = round(cost_per_share * fill["qty"] * 2, 2)
    row.update(action="traded", fill_time=fill["fill_time"], fill_price=fill["fill_price"],
               exit_time=fill["exit_time"], exit_price=fill["exit_price"], exit_reason=fill["exit_reason"],
               pnl=fill["pnl"], cost=cost, net=round(fill["pnl"] - cost, 2),
               r_multiple=round(fill["pnl"] / verdict["max_loss"], 4) if verdict["max_loss"] else None,
               max_open_loss=fill["max_open_loss"])
    return row


class TimeMachine(object):
    """The Time Machine's Monte Carlo odds for a plan, the way timemachine.py run computes them (10,000 replays,
    seed 19, until the close) from the full sessions before the day. The pools are built once per day."""

    def __init__(self, path, symbol):
        meta, bars, _ = timemachine.parse_bars(timemachine.chart_result(read_text(path), symbol),
                                               datetime.datetime(2100, 1, 1))
        self.days = timemachine.split_sessions(bars)
        self.pools = {}

    def odds(self, day, entry_price, stop, target, entry_bar):
        if day not in self.pools:
            sessions, _ = timemachine.full_sessions(self.days, day)
            self.pools[day] = timemachine.build_pool(sessions) if len(sessions) >= timemachine.MIN_SESSIONS else None
        if self.pools[day] is None:
            return None
        last_bar = (timemachine.CLOSE_MIN - timemachine.OPEN_MIN) // 5 - 1
        monte_carlo = timemachine.simulate(self.pools[day], entry_price, stop, target, entry_bar, last_bar)
        return monte_carlo["p_target"], monte_carlo["p_stop"]


def run_account(data, days, combo, signals, cost_per_share=0.0, delay_bars=0, start_cash=START_CASH, skip=None):
    """One symbol's account through the given days: a row per day, cash carried forward.
    skip(row) -> True drops a trade before it is placed (a filter such as the Time Machine)."""
    cash, rows = start_cash, []
    for day in days:
        sig = signals.get(day)
        row = trade_day(data, day, combo, cash, sig, cost_per_share, delay_bars)
        if row["action"] == "traded" and skip is not None and skip(row):
            row = {"symbol": data["symbol"], "date": day, "action": "skipped by the filter", "net": 0.0}
        cash = round(cash + row["net"], 2)
        row["cash_after"] = cash
        rows.append(row)
    return rows
