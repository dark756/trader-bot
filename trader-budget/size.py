#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, take-profit) to trader/orders.json in the
shape of Alpaca's place_stock_order. It never places anything: trader-risk
checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 364.00 --write           # a limit order at 364.00
    python3 size.py --limit cap --write              # a limit at the highest price your limits allow
    python3 size.py --now "2026-10-19 10:15" --json  # pin the clock (New York time)

How many shares. Whole shares only: Alpaca takes no fractions in a bracket.

    shares = floor of the smallest of
      max loss       max_loss_per_trade_usd / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order_usd / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

--limit cap is a limit order at the highest price where those shares still
risk at most your max loss at the stop and keep your min reward:risk. A fill
can never come in worse than that; if the price runs past it, the order waits.

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight.

    python3 size.py --weights AAPL=22.04,AXP=17.14,GOOGL=12.62,KO=10.86,BAC=9.20
    python3 size.py --targets my-weights.json --budget 5000 --write
    python3 size.py --weights ... --whole-shares --prices trader/prices.json

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_usd is dropped and its weight
                   shared out, so you never place a $2 order
  5. rounding      whole dollars by default (--cents for cents); the rounding
                   dollar goes to the biggest position that is not capped
  6. whole shares  with --whole-shares or "fractional": false; needs prices

Money that cannot be invested is reported as "left idle", never hidden.

Your config is checked first: a value this tool cannot read (like "50$", or
"false" in quotes) is an error, never a limit that is quietly off. null
switches a limit off; 0 means zero (max_loss_per_trade_usd 0: no order).
"""

import argparse
import datetime
import json
import math
import os
import sys

# v2 defaults: the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 4000,
    "min_order_usd": 5,
    "max_loss_per_trade_usd": 50,
    "max_daily_loss_usd": 150,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "fractional": False,
    "broker": "alpaca",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
}
BOOL_KEYS = ("require_stop", "fractional", "paper", "allow_options", "allow_margin", "allow_short", "pdt_guard")
# the numbers sizing reads; null switches a limit off, 0 means zero
NUMBER_KEYS = ("budget", "cash_buffer_pct", "max_position_pct", "max_order_usd", "min_order_usd",
               "max_loss_per_trade_usd", "min_reward_risk")
EPSILON = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, nth):
    day = datetime.date(year, month, 1)
    day += datetime.timedelta(days=(6 - day.weekday()) % 7)
    return day + datetime.timedelta(weeks=nth - 1)


def eastern_offset(utc_time):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    year = utc_time.year
    dst_start = datetime.datetime.combine(_nth_sunday(year, 3, 2), datetime.time(7, 0))   # 2:00 EST
    dst_end = datetime.datetime.combine(_nth_sunday(year, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive_utc = utc_time.replace(tzinfo=None)
    return -4 if dst_start <= naive_utc < dst_end else -5


def to_eastern(utc_time):
    return utc_time.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc_time))


def eastern_to_utc(eastern):
    for offset in (-4, -5):
        utc_time = eastern - datetime.timedelta(hours=offset)
        if eastern_offset(utc_time) == offset:
            return utc_time
    return eastern + datetime.timedelta(hours=5)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    text_iso = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text_iso)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = (moment - moment.utcoffset()).replace(tzinfo=None)
    return moment


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except ValueError as error:
        raise SizeError("%s is not valid JSON: %s" % (path, error))
    except OSError as error:
        raise SizeError("cannot read %s: %s" % (path, error))


def load_config(path):
    return rules(read_json(path, "config") if path and os.path.exists(path) else {})


def rules(config):
    """DEFAULTS plus your config, checked. A value this tool cannot read is an
    error (SizeError), never a limit that is quietly off."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise SizeError("config: trader/config.json must be one JSON object, { ... }")
    settings = dict(DEFAULTS)
    settings.update(config)
    bad = []
    for key in BOOL_KEYS:
        if not isinstance(settings[key], bool):
            bad.append("%s must be true or false, without quotes (it is %s)" % (key, json.dumps(settings[key])))
    for key in NUMBER_KEYS:
        value = settings[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            like = json.dumps(DEFAULTS[key]) if DEFAULTS[key] is not None else "5000"
            bad.append("%s must be a number like %s, without quotes or $, or null to switch it off (it is %s)"
                       % (key, like, json.dumps(value)))
    if settings["blocked"] is None:
        settings["blocked"] = []
    if not isinstance(settings["blocked"], list) or not all(isinstance(ticker, str) for ticker in settings["blocked"]):
        bad.append("blocked must be a list of tickers like [\"GME\"] (it is %s)" % json.dumps(settings["blocked"]))
    if bad:
        raise SizeError("config: %s. Fix trader/config.json and run it again" % "; ".join(bad))
    return settings


def finite_number(value):
    """A finite number, or None (NaN and infinity are not prices or share counts)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for ticker, weight in data.items():
            rows.append({"ticker": ticker, "name": "", "weight": weight})
    elif isinstance(data, list):
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise SizeError("weights[%d] is not an object" % index)
            ticker = row.get("ticker") or row.get("symbol")
            weight = row.get("weight", row.get("weight_pct", row.get("pct")))
            if not ticker or weight is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % index)
            rows.append({"ticker": ticker, "name": row.get("name", ""), "weight": weight})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for row in rows:
        try:
            weight = float(row["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (row["ticker"], row["weight"]))
        out.append({"ticker": str(row["ticker"]).strip().upper(), "name": row["name"] or "", "weight": weight})
    return out


def weights_from_arg(text):
    """AAPL=22.04,AXP=17.14 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        ticker, weight = part.split("=", 1)
        out[ticker.strip().upper()] = weight.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for ticker, price in (prices.items() if isinstance(prices, dict) else []):
        if finite_number(price) is not None:
            out[str(ticker).upper()] = finite_number(price)
    return out


def price_time(doc, symbol):
    """When this symbol's price is from: the quote's own time (detail[symbol].time), else when it
    was fetched (detail[symbol].fetched), else the file's as_of."""
    detail = (doc or {}).get("detail")
    symbol_detail = detail.get(symbol) if isinstance(detail, dict) else None
    symbol_detail = symbol_detail if isinstance(symbol_detail, dict) else {}
    for key in ("time", "fetched"):
        moment = parse_utc(symbol_detail.get(key))
        if moment:
            return moment
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, symbol, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for symbol from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    prices = doc.get("prices")
    price = finite_number(prices.get(symbol)) if isinstance(prices, dict) else None
    if price is None or price <= 0:
        return None, None, False
    price_at = price_time(doc, symbol)
    if price_at is None:
        return price, None, False
    age = (now_utc - price_at).total_seconds()
    return price, price_at, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_to_cent(price):
    """Round a price to the cent (half up)."""
    return math.floor(float(price) * 100 + 0.5 + EPSILON) / 100.0


def shares_for(amount_usd, per_share):
    """Whole shares that fit in amount_usd at per_share, in exact integer cents."""
    amount_cents, price_cents = int(round(float(amount_usd) * 100)), int(round(float(per_share) * 100))
    if price_cents <= 0 or amount_cents <= 0:
        return 0
    return amount_cents // price_cents


def alpaca_price(price):
    """358.32 -> "358.32": Alpaca wants prices as strings."""
    return "%.2f" % round_to_cent(price)


def alpaca_number(number):
    """1250 -> "1250", 1118.13 -> "1118.13" (Alpaca wants these as strings)."""
    number = round(float(number), 2)
    if abs(number - round(number)) < 0.005:
        return str(int(round(number)))
    return "%.2f" % number


def dollars(amount):
    amount = float(amount)
    if abs(amount - round(amount)) < 0.005:
        return "$" + "{:,.0f}".format(round(amount))
    return "$" + "{:,.2f}".format(amount)


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, config, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  New York date (YYYY-MM-DD); a plan for another day is refused"""
    settings = rules(config)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in New York: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = (finite_number(plan.get("entry")), finite_number(plan.get("stop")),
                                finite_number(plan.get("target")))
    for name, value in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if value is None or value <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {ticker.upper() for ticker in settings["blocked"]}
    if symbol in blocked:
        raise SizeError("%s is on your blocked list" % symbol)

    budget = float(budget if budget is not None else (settings["budget"] or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buffer_pct = float(settings["cash_buffer_pct"] or 0)
    if not 0 <= buffer_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        entry_ref, entry_ref_from = round_to_cent(limit), "limit"
    elif price is not None and round_to_cent(price) > round_to_cent(entry_plan):
        entry_ref, entry_ref_from = round_to_cent(price), "price"
    else:
        entry_ref, entry_ref_from = round_to_cent(entry_plan), "plan"
    stop, target = round_to_cent(stop), round_to_cent(target)
    if stop >= entry_ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (alpaca_price(stop), alpaca_price(entry_ref)))
    if target <= entry_ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (alpaca_price(target), alpaca_price(entry_ref)))
    risk_cents = int(round((entry_ref - stop) * 100))
    reward_cents = int(round((target - entry_ref) * 100))
    risk = risk_cents / 100.0

    cash_buffer = round(budget * buffer_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (dollars(budget), dollars(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (dollars(cash), dollars(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = settings["max_loss_per_trade_usd"]
    if max_loss is not None:
        limits.append({"name": "max loss", "usd": max_loss, "per": risk, "shares": shares_for(max_loss, risk),
                       "how": "%s max loss / %s risk a share" % (dollars(max_loss), alpaca_price(risk))})
    position_cap_pct = settings["max_position_pct"]
    if position_cap_pct is not None:
        position_cap = round(budget * position_cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "usd": position_cap, "per": entry_ref,
                       "shares": shares_for(position_cap, entry_ref),
                       "how": "%s%% of %s = %s / %s" % (alpaca_number(position_cap_pct), dollars(budget),
                                                        dollars(position_cap), alpaca_price(entry_ref))})
    max_order = settings["max_order_usd"]
    if max_order is not None:
        limits.append({"name": "max order", "usd": max_order, "per": entry_ref,
                       "shares": shares_for(max_order, entry_ref),
                       "how": "%s / %s" % (dollars(max_order), alpaca_price(entry_ref))})
    limits.append({"name": "cash", "usd": available, "per": entry_ref, "shares": shares_for(available, entry_ref),
                   "how": "%s = %s / %s" % (cash_note, dollars(available), alpaca_price(entry_ref))})

    qty = max(0, min(limit_row["shares"] for limit_row in limits))
    binding = [limit_row["name"] for limit_row in limits if limit_row["shares"] == qty]
    for limit_row in limits:
        limit_row["binds"] = limit_row["name"] == binding[0]
    cost = round(qty * entry_ref, 2)
    loss = round(qty * risk_cents / 100.0, 2)
    refusal = None
    if qty == 0:
        if binding[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (dollars(risk), alpaca_price(entry_ref), alpaca_price(stop), dollars(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (symbol, dollars(entry_ref), binding[0],
                          limits[[limit_row["name"] for limit_row in limits].index(binding[0])]["how"]))
    elif settings["min_reward_risk"] and reward_cents < settings["min_reward_risk"] * risk_cents - EPSILON:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_cents / float(risk_cents), alpaca_price(target), alpaca_price(entry_ref),
                      alpaca_price(stop), alpaca_number(settings["min_reward_risk"])))
    elif cost < float(settings["min_order_usd"] or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", symbol, dollars(cost), dollars(settings["min_order_usd"])))
    return {
        "symbol": symbol, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_to_cent(entry_plan),
        "entry": entry_ref, "entry_from": entry_ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_cents / float(risk_cents), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buffer_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_usd": max_loss, "limits": limits, "qty": qty, "bound_by": binding[0],
        "cost": cost, "max_loss": loss, "flat_by": plan.get("flat_by") or settings.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def limit_cap(sizing, config):
    """--limit cap: the highest limit price, to the cent, where s's shares still
    risk at most max_loss_per_trade_usd at the stop and reward:risk stays at
    min_reward_risk or better (None when neither is set). A limit order never
    fills above its price, so the loss at the stop cannot grow past the limit."""
    settings = rules(config)
    stop_cents, target_cents = int(round(sizing["stop"] * 100)), int(round(sizing["target"] * 100))
    caps = []
    if settings["max_loss_per_trade_usd"] is not None and sizing["qty"] > 0:
        caps.append(stop_cents + int(round(settings["max_loss_per_trade_usd"] * 100)) // int(sizing["qty"]))
    min_reward_risk = settings["min_reward_risk"]
    if min_reward_risk:
        caps.append(int(math.floor((target_cents + min_reward_risk * stop_cents) / (1.0 + min_reward_risk) + 1e-9)))
    return min(caps) / 100.0 if caps else None


def bracket_order(sizing):
    """The one proposed order, Alpaca place_stock_order fields (strings) plus why and plan."""
    if sizing["refusal"] or sizing["qty"] <= 0:
        return None
    order = {"symbol": sizing["symbol"], "side": "buy", "qty": str(int(sizing["qty"])), "type": sizing["order_type"],
             "time_in_force": "day"}
    if sizing["order_type"] == "limit":
        order["limit_price"] = alpaca_price(sizing["entry"])
    setup = SETUP_WORDS.get(sizing["setup"], sizing["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, sizing["symbol"], sizing["qty"],
              {"max loss": "%s max loss per trade" % dollars(sizing["max_loss_per_trade_usd"] or 0),
               "position cap": "position cap", "max order": "max order",
               "cash": "cash after the buffer"}[sizing["bound_by"]],
              sizing["qty"], alpaca_price(sizing["risk_per_share"]), dollars(sizing["max_loss"])))
    order.update({
            "order_class": "bracket",
            "take_profit_limit_price": alpaca_price(sizing["target"]),
            "stop_loss_stop_price": alpaca_price(sizing["stop"]),
            "why": why,
            "plan": {"entry": sizing["entry"], "stop": sizing["stop"], "target": sizing["target"],
                     "risk_per_share": sizing["risk_per_share"], "max_loss": sizing["max_loss"],
                     "setup": sizing["setup"], "entry_time": sizing["entry_time"], "flat_by": sizing["flat_by"]},
        })
    return order


def plan_report(sizing, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(sizing["setup"], sizing["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (sizing["symbol"], setup, alpaca_price(sizing["entry_plan"]),
                                            (" at " + sizing["entry_time"]) if sizing["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (alpaca_price(sizing["stop"]),
                                              (": " + sizing["stop_label"]) if sizing["stop_label"] else "",
                                             alpaca_price(sizing["target"]),
                                             (": " + sizing["target_label"]) if sizing["target_label"] else ""))
    for note in notes:
        lines.append(note)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[sizing["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (alpaca_price(sizing["entry"]), src, alpaca_price(sizing["risk_per_share"]),
                    alpaca_price(sizing["entry"]), alpaca_price(sizing["stop"]),
                    sizing["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for limit_row in sizing["limits"]:
        lines.append("  %-13s %-58s %3d shares%s" % (limit_row["name"], limit_row["how"], limit_row["shares"],
                                                      "  <- binds" if limit_row["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + sizing["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                 % (sizing["qty"], sizing["symbol"], dollars(sizing["cost"]), alpaca_price(sizing["entry"]),
                    sizing["qty"],
                    alpaca_price(sizing["risk_per_share"]), dollars(sizing["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s."
                 % alpaca_price(sizing["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (place_stock_order arguments, nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if sizing["flat_by"]:
        lines.append("Flat by %s New York: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten)." % sizing["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(ticker).upper() for ticker in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for row in rows:
        if row["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % row["ticker"])
        seen.add(row["ticker"])
        if row["ticker"] in blocked:
            dropped.append({"ticker": row["ticker"], "reason": "on your blocked list"})
        elif row["weight"] <= 0:
            dropped.append({"ticker": row["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(row))
    keep.sort(key=lambda row: -row["weight"])
    if top:
        for row in keep[top:]:
            dropped.append({"ticker": row["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(row["weight"] for row in keep)
    for row in keep:
        row["source_weight"] = row["weight"]
        row["weight"] = 100.0 * row["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, position_cap):
    """Exact (unrounded) dollars per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: dollars}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [row for row in rows]
    money_left = investable
    while active:
        wsum = sum(row["weight"] for row in active)
        trial = {row["ticker"]: money_left * row["weight"] / wsum for row in active}
        over = [row for row in active if trial[row["ticker"]] > position_cap + EPSILON]
        if not over:
            alloc.update(trial)
            break
        for row in over:
            alloc[row["ticker"]] = position_cap
            capped.add(row["ticker"])
            money_left -= position_cap
        active = [row for row in active if row["ticker"] not in capped]
        if money_left <= EPSILON:
            for row in active:
                alloc[row["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, position_cap, unit):
    """Round to whole dollars (unit 1) or cents (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(amount):
        return int(math.floor(amount / unit + EPSILON))

    cap_units = units(position_cap)
    total_units = units(sum(exact.values()))
    out = {}
    for ticker, amount in exact.items():
        out[ticker] = cap_units if ticker in capped else int(math.floor(amount / unit + 0.5 + EPSILON))
    diff = total_units - sum(out.values())
    takers = [ticker for ticker in order if ticker not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for ticker in takers:
            if diff == 0:
                break
            if diff > 0 and out[ticker] + 1 <= cap_units:
                out[ticker] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[ticker] > 0:
                out[ticker] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {ticker: round(unit_count * unit, 2) for ticker, unit_count in out.items()}


def whole_shares(slices, prices, order, position_cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for ticker in order:
        shares[ticker] = int(math.floor(slices[ticker] / prices[ticker] + EPSILON)) if slices[ticker] > 0 else 0
    spent = sum(shares[ticker] * prices[ticker] for ticker in order)
    left = cash - spent
    while True:
        best, best_shortfall = None, None
        for ticker in order:
            price = prices[ticker]
            if price > left + EPSILON or (shares[ticker] + 1) * price > position_cap + EPSILON:
                continue
            shortfall = slices[ticker] - shares[ticker] * price
            if best is None or shortfall > best_shortfall + EPSILON:
                best, best_shortfall = ticker, shortfall
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, config, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    budget = float(budget if budget is not None else (config.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buffer_pct = float(config.get("cash_buffer_pct", 10) or 0)
    position_cap_pct = config.get("max_position_pct", 25)
    position_cap_pct = 100.0 if position_cap_pct is None else float(position_cap_pct)     # null: no cap
    min_order = float(config.get("min_order_usd", 5) or 0)
    if not 0 <= buffer_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < position_cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not config.get("fractional", True)
    prices = prices or {}

    keep, dropped = normalise(rows, config.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buffer_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    position_cap = round(budget * position_cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [row["ticker"] for row in keep if not prices.get(row["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(row) for row in keep]
        total_weight = sum(row["source_weight"] for row in live)
        for row in live:
            row["weight"] = 100.0 * row["source_weight"] / total_weight
        exact, capped = allocate(live, investable, position_cap)
        order = [row["ticker"] for row in sorted(live, key=lambda row: -exact[row["ticker"]])]
        slices = round_to_unit(exact, capped, order, position_cap, unit)
        shares = None
        if whole:
            shares = whole_shares(slices, prices, order, position_cap, sum(slices.values()))
            slices = {ticker: round(shares[ticker] * prices[ticker], 2) for ticker in order}
        small = [row for row in live if slices[row["ticker"]] < min_order - EPSILON]
        if not small:
            break
        worst = min(small, key=lambda row: (row["source_weight"], row["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs $%s, more than its slice; it cannot be bought in whole shares"
                      % alpaca_number(prices[worst["ticker"]]))
        else:
            reason = "its slice ($%s) is under your $%s minimum order" % (
                alpaca_number(slices[worst["ticker"]]), alpaca_number(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [row for row in keep if row["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for row in sorted(live, key=lambda row: -row["weight"]):
        ticker = row["ticker"]
        position = {"ticker": ticker, "name": row["name"], "weight": round(row["weight"], 2),
                    "source_weight": row["source_weight"], "dollars": slices[ticker],
                    "capped": ticker in capped}
        if prices.get(ticker):
            position["price"] = prices[ticker]
            position["shares"] = shares[ticker] if whole else round(slices[ticker] / prices[ticker], 4)
        positions.append(position)
    invested = round(sum(position["dollars"] for position in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buffer_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": position_cap_pct, "cap": position_cap,
        "min_order_usd": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: proposed buy orders, one per position, in place_stock_order shape."""
    out = []
    for position in result["positions"]:
        if position["dollars"] <= 0:
            continue
        why = "%.2f%% of the split" % position["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if position["capped"]:
            why += "; capped at %s%% of the %s budget" % (alpaca_number(result["max_position_pct"]),
                                                          dollars(result["budget"]))
        order = {"symbol": position["ticker"], "side": "buy"}
        if result["whole_shares"]:
            order["qty"] = str(int(position["shares"]))
        else:
            order["notional"] = alpaca_number(position["dollars"])
        order.update({"type": "market", "time_in_force": "day", "why": why})
        out.append(order)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (dollars(result["budget"]), alpaca_number(result["cash_buffer_pct"]), dollars(result["cash_buffer"]),
                    dollars(result["investable"]), alpaca_number(result["max_position_pct"]), dollars(result["cap"])))
    lines.append("")
    has_price = any("price" in position for position in result["positions"])
    head = "%-7s %-22s %8s %11s" % ("ticker", "name", "weight", "dollars")
    if has_price:
        head += " %10s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for position in result["positions"]:
        row = "%-7s %-22s %7.2f%% %11s" % (position["ticker"], (position["name"] or "")[:22], position["weight"],
                                           dollars(position["dollars"]))
        if has_price:
            row += " %10s %10s" % (dollars(position["price"]) if "price" in position else "",
                                   position.get("shares", ""))
        if position["capped"]:
            row += "  capped at %s%%" % alpaca_number(result["max_position_pct"])
        lines.append(row)
    lines.append("%-7s %-22s %8s %11s" % ("", "invested", "", dollars(result["invested"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "cash buffer", "", dollars(result["cash_buffer"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "left idle", "", dollars(result["idle"])))
    if result["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (dollars(result["idle"]), (
              "whole shares do not divide evenly" if result["whole_shares"]
              else "every stock is at its cap; add stocks or raise max_position_pct")))
    for dropped in result["dropped"]:
        lines.append("Dropped %s: %s" % (dropped["ticker"], dropped["reason"]))
    lines.append("")
    lines.append("Proposed orders (place_stock_order arguments, nothing has been placed):")
    for order in orders:
        amount = ("notional \"%s\"" % order["notional"]) if "notional" in order else ("qty \"%s\"" % order["qty"])
        lines.append("  %s %s %s, %s, %s" % (order["side"], order["symbol"], amount, order["type"],
                                             order["time_in_force"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, config, source="", now_utc=None):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(config.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------- main

def run_plan(args, config):
    now = parse_now(args.now)
    today = to_eastern(now).date().isoformat()
    plan = read_json(args.plan, "plan (run trader-exit first, or use --weights for a split)")
    if not isinstance(plan, dict):
        raise SizeError("%s should be one JSON object, { ... } (the plan from trader-exit)" % args.plan)
    limit = None
    cap_mode = args.limit == "cap"
    if args.limit is not None and not cap_mode:
        if args.limit == "plan":
            limit = finite_number((plan or {}).get("entry"))
        else:
            limit = finite_number(args.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 364.00, or plan, or cap")
    symbol = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    prices_path = args.prices or (home("prices.json") if os.path.exists(home("prices.json")) else None)
    if prices_path and limit is None and symbol:
        prices_doc = read_json(prices_path, "prices")
        latest, latest_at, fresh = fresh_price(prices_doc, symbol, now)
        if latest is not None and fresh:
            price = latest
            if round_to_cent(latest) > round_to_cent(finite_number(plan.get("entry")) or 0) and not cap_mode:
                notes.append("%s last %s (as of %s New York) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (symbol, alpaca_price(latest),
                                                          to_eastern(latest_at).strftime("%H:%M"),
                                                          alpaca_price(plan.get("entry")), alpaca_price(latest)))
        elif latest is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (symbol, prices_path,
                            ("from " + to_eastern(latest_at).strftime("%Y-%m-%d %H:%M") + " New York")
                            if latest_at else "undated", FRESH_MINUTES))
    cash = None
    if args.portfolio and os.path.exists(args.portfolio):
        portfolio = read_json(args.portfolio, "portfolio")
        cash = finite_number(portfolio.get("cash")) if isinstance(portfolio, dict) else None
    # a capped limit never fills above its price, so it is sized from the plan, not from a price that ran
    sizing = size_trade(plan, config, args.budget, limit, None if cap_mode else price, cash, today)
    if cap_mode and not sizing["refusal"]:
        cap_price = limit_cap(sizing, config)
        if cap_price is None:
            raise SizeError("--limit cap needs max_loss_per_trade_usd or min_reward_risk in your config")
        sizing = size_trade(plan, config, args.budget, cap_price, None, cash, today)
        if not sizing["refusal"]:
            notes.append("Limit %s: the highest price where %d share%s risk at most %s at the stop and reward:risk "
                         "stays at your minimum or better. It never fills above that; if the price runs away it "
                         "does not fill, so cancel it once the setup is gone.%s"
                         % (alpaca_price(cap_price), sizing["qty"], "" if sizing["qty"] == 1 else "s",
                            dollars(sizing["max_loss"]),
                            (" It is under %s now, so it waits for a dip." % alpaca_price(price))
                            if price is not None and cap_price < round_to_cent(price) else ""))
    order = bracket_order(sizing)
    if order and args.write:
        src = "trader/plan.json (%s %s %s)" % (sizing["symbol"], sizing["setup"] or "plan", sizing["date"] or today)
        write_orders(args.write, [order], config, src, now)
    if args.json:
        print(json.dumps({"sizing": sizing, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(sizing, order, notes))
        if order and args.write:
            print("Wrote 1 proposed bracket order to %s" % args.write)
    return 0 if order else 1


def run_weights(args, config):
    source = ""
    if args.weights:
        rows = parse_weights(weights_from_arg(args.weights))
    else:
        data = read_json(args.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(args.prices)
    if not prices and os.path.exists(home("prices.json")) and not args.prices:
        prices = load_prices(home("prices.json"))
    whole = True if args.whole_shares else None
    result = size(rows, config, args.budget, prices, whole, args.cents, args.top)
    orders = orders_from(result, source)
    if args.write:
        write_orders(args.write, orders, config, source, parse_now(args.now))
    if args.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if args.write:
            print("Wrote %d proposed orders to %s" % (len(orders), args.write))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    parser.add_argument("--plan", default=home("plan.json"), help="the trade plan (default trader/plan.json)")
    parser.add_argument("--config", default=home("config.json"), help="rules (default trader/config.json)")
    parser.add_argument("--budget", type=float, help="override the budget in the config")
    parser.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                        help="a limit order instead of market: at the plan's entry, at PRICE, or cap (the "
                             "highest price where the max loss and min reward:risk still hold)")
    parser.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    parser.add_argument("--portfolio", default=home("portfolio.json"),
                        help="your account; its cash caps the size if it exists")
    parser.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\"")
    parser.add_argument("--write", nargs="?", const=home("orders.json"), metavar="PATH",
                        help="write the proposed order (default trader/orders.json)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the report")
    weights_group = parser.add_argument_group("weights mode (split a budget across a list)")
    weights_group.add_argument("--weights", help="inline weights: AAPL=22.04,AXP=17.14")
    weights_group.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    weights_group.add_argument("--top", type=int, help="keep only the N biggest weights")
    weights_group.add_argument("--whole-shares", action="store_true", help="whole shares only (needs --prices)")
    weights_group.add_argument("--cents", action="store_true", help="size to the cent instead of whole dollars")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.weights or args.targets:
            return run_weights(args, config)
        if args.top or args.whole_shares or args.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(args, config)
    except SizeError as error:
        print("size.py: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
