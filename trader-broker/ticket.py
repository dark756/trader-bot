#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON Claude will send to Alpaca's place_stock_order tool.
            A bracket order shows its stop-loss, take-profit and the max loss
            at the stop. An order is READY only if trader-risk passed THIS
            exact order (matched by fingerprint) in the last 15 minutes on the
            real clock, nobody has decided on it yet, and (Alpaca) today's
            account check matches the account pinned in your config.
  account   records today's check of the Alpaca account: the account_number
            from get_account_info must match "alpaca_account" in your config.
            The PAPER / LIVE label is only what your config says; the Alpaca
            server's own setting decides where an order goes, and its account
            info does not say paper or live. The account number does.
  approve   records your explicit yes for ONE order, with your reply word for
            word. Only "yes" (or "y") counts, followed by nothing but this
            order's own symbol, side, numbers and ticket words. It refuses
            "yes to all", "sure", "ok", a yes with a doubt, another stock or a
            different amount in it, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with the broker's order id), denied, or failed. A failed,
            denied or declined order can be tried again the same day, but only
            after a new trader-risk check (newer than that record) and a new yes.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py account --number PA1234ABCD
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 61e6...
    python3 ticket.py fill --symbol TSLA --side buy --qty 8 --price 364.20
    python3 ticket.py flatten --symbol TSLA --qty 8
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
New York dates. --now "2026-10-19 10:15" or --today pins the clock: that is a
practice run, for replays and tests. It reads only practice verdicts, its yes
is never an order to place, and it keeps its own log, verdicts and account in
trader/practice/ unless you pass the paths. trader/ is the folder you run in,
or $TRADER_HOME when that is set.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "opg": "at the open",
             "cls": "at the close", "ioc": "immediate or cancel", "fok": "fill or kill"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "never", "mind", "nevermind", "jk", "kidding", "joking", "joke",
               "lol", "actually", "instead", "rather", "scratch", "change", "or", "except", "until",
               "nah", "nope", "undo", "skip", "abort", "hmm", "unsure", "sorry", "oops", "wrong", "mistake"}
# the only other words a yes may carry: the ones on the ticket
TICKET_WORDS = {"place", "it", "this", "order", "the", "a", "of", "share", "shares", "at", "for", "please",
                "go", "ahead", "confirm", "confirmed", "max", "loss", "target", "take-profit", "stop-loss",
                "limit", "market", "bracket", "now"}
SIDE_WORDS = {"buy", "sell"}
REPLY_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?|[a-z]+(?:['.\-][a-z]+)*")
ALLOWED_PUNCTUATION = set(" \t\r\n.,!;:'\"()-$")
VERDICT_MINUTES = 15     # a trader-risk PASS counts this long, on the clock it was checked on
RETRY_STATUSES = ("failed", "denied", "declined")   # a risk check newer than one of these makes the order READY again
BROKERS = ("alpaca", "ibkr")
ACCOUNT_NUMBER = re.compile(r"^[A-Za-z0-9_\-]{4,32}$")


class TicketError(Exception):
    pass


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


def utc_now():
    """The real clock, naive UTC (the tests swap it)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return utc_now()
    text_iso = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text_iso)
    except ValueError:
        raise TicketError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:15:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = (moment - moment.utcoffset()).replace(tzinfo=None)
    return moment


def ny_iso(now_utc):
    """naive UTC -> "2026-10-19T10:15:00-04:00" (New York wall clock with its offset)."""
    off = eastern_offset(now_utc)
    return "%s%+03d:00" % (to_eastern(now_utc).replace(microsecond=0).isoformat(), off)


# ---------------------------------------------------------------- files

def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


def read_json(path, what, required=True, kind=None):
    """The JSON at path, or None when it is missing and not required. kind=dict:
    anything but one JSON object there is an error, not a traceback later."""
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as file:
            doc = json.load(file)
    except ValueError as error:
        raise TicketError("%s is not valid JSON: %s" % (path, error))
    except OSError as error:
        raise TicketError("cannot read %s: %s" % (path, error))
    if kind is dict and not isinstance(doc, dict):
        raise TicketError("%s should be one JSON object, { ... }" % path)
    return doc


def write_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def record_day(record):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(record.get("date") or str(record.get("at", ""))[:10])


def rules(config):
    """The config keys this tool reads, checked: a value it cannot read is an
    error, never a quiet default."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise TicketError("config: trader/config.json must be one JSON object, { ... }")
    settings = dict(config)
    settings.setdefault("paper", True)
    settings.setdefault("broker", "alpaca")
    bad = []
    if not isinstance(settings["paper"], bool):
        bad.append("paper must be true or false, without quotes (it is %s)" % json.dumps(settings["paper"]))
    if isinstance(settings["broker"], str) and settings["broker"].strip().lower() in BROKERS:
        settings["broker"] = settings["broker"].strip().lower()
    else:
        bad.append("broker must be \"alpaca\" or \"ibkr\" (it is %s)" % json.dumps(settings["broker"]))
    acct = settings.get("alpaca_account")
    if acct is not None and not (isinstance(acct, str) and ACCOUNT_NUMBER.match(acct.strip())):
        bad.append("alpaca_account must be your Alpaca account number in quotes, like \"PA1234ABCD\" (it is %s)"
                   % json.dumps(acct))
    budget = settings.get("budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or not math.isfinite(budget) or budget < 0):
        bad.append("budget must be a number like 5000, without quotes or $ (it is %s)" % json.dumps(budget))
    if bad:
        raise TicketError("config: %s. Fix trader/config.json first" % "; ".join(bad))
    return settings


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    fields = {field: order[field] for field in ALPACA_FIELDS if field in order and order[field] is not None}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def number_string(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        number = float(value)
        return str(int(number)) if number == int(number) else repr(number)
    return str(value)


def finite_number(value):
    """A finite number, or None (NaN and infinity are not prices or share counts)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def tool_input(order):
    """The exact arguments for place_stock_order: Alpaca's parameter names,
    numbers as strings (its signature types qty, notional and prices as str).
    "why" and "plan" are ours and are never sent."""
    out = {}
    for field in ALPACA_FIELDS:
        if field in order and order[field] is not None:
            out[field] = number_string(order[field]) if field in STRING_FIELDS else order[field]
    return out


def dollars(amount):
    number = float(amount)
    text = "{:,.2f}".format(number)
    return "$" + (text[:-3] if text.endswith(".00") else text)


def price_text(price):
    return "%.2f" % float(price)


def is_bracket(order):
    order_class = str(order.get("order_class") or "").lower()
    has_legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return order_class == "bracket" or (not order_class and has_legs)


def max_loss(order, verdict=None):
    """(dollars, stop) lost if the stop fills exactly at the stop, or (None, stop).
    verdict is trader-risk's verdict: its figure wins, because it measures from the
    higher of the plan's entry and a fresh price."""
    if verdict and finite_number(verdict.get("max_loss")) is not None:
        return (round(finite_number(verdict["max_loss"]), 2),
                finite_number(verdict.get("stop")) or finite_number(order.get("stop_loss_stop_price")))
    stop = finite_number(order.get("stop_loss_stop_price")) or finite_number((order.get("plan") or {}).get("stop"))
    qty = finite_number(order.get("qty"))
    if not stop or not qty:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and finite_number(order.get("limit_price")):
        entry = finite_number(order["limit_price"])
    elif finite_number(plan.get("max_loss")) is not None:
        return round(finite_number(plan["max_loss"]), 2), stop
    else:
        entry = finite_number(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(qty * int(round(risk * 100)) / 100.0, 2), stop


def plain_parts(order, verdict=None):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    symbol = order.get("symbol", "?")
    order_type = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    if is_bracket(order):
        how = ("limit %s" % price_text(order["limit_price"]) if order_type == "limit" and order.get("limit_price")
               else order_type)
        parts = ["%s %s %s, %s, %s" % (side, number_string(order.get("qty")), symbol, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg_text = "stop-loss %s" % price_text(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg_text += " (limit %s)" % price_text(order["stop_loss_limit_price"])
            parts.append(leg_text)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % price_text(order["take_profit_limit_price"]))
        loss, stop = max_loss(order, verdict)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (dollars(loss), price_text(stop)))
        return parts
    if order.get("notional") is not None:
        what = "%s of %s" % (dollars(order["notional"]), symbol)
    else:
        qty_text = number_string(order.get("qty"))
        what = "%s share%s of %s" % (qty_text, "" if qty_text == "1" else "s", symbol)
    if order_type == "limit":
        how = "limit order at %s" % dollars(order.get("limit_price"))
    elif order_type == "stop":
        how = "stop order at %s" % dollars(order.get("stop_price"))
    elif order_type == "stop_limit":
        how = "stop %s, limit %s" % (dollars(order.get("stop_price")), dollars(order.get("limit_price")))
    else:
        how = "%s order" % order_type.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order, verdict=None):
    """BUY $1,250 of AAPL, market order, good for today
    BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about $46.88 ..."""
    return " / ".join(plain_parts(order, verdict))


def amount_numbers(order, verdict=None):
    numbers = []
    for field in ("notional", "qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price",
                  "stop_loss_limit_price"):
        if order.get(field) is not None:
            number = finite_number(order[field])
            if number is not None:
                numbers.append(number)
    loss, _ = max_loss(order, verdict)
    if loss is not None:
        numbers.append(loss)
    return numbers


def parse_yes(reply, order, batch_symbols=(), verdict=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why).

    It starts with "yes" (or "y"). After that only this order's own words may
    follow: its symbol, its side, its numbers and the words on its ticket. A
    doubt, a batch, a question, another stock, a number this order does not
    have, or any other word, and it is not a yes."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    lowered = raw.lower()
    tokens = REPLY_TOKEN.findall(lowered)
    if not tokens or tokens[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if set(REPLY_TOKEN.sub(" ", lowered)) - ALLOWED_PUNCTUATION:
        return False, "the reply has characters that are not part of a plain yes; ask again and reply just \"yes\""
    words = [token for token in tokens if token[0].isalpha()]
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    symbol = str(order.get("symbol", "")).upper()
    others = {other.upper() for other in batch_symbols} - {symbol}
    named = {word.upper() for word in words} & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), symbol)
    side = str(order.get("side", "")).lower()
    wrong = [word for word in words if word in SIDE_WORDS and word != side]
    if wrong:
        return False, "the reply says %s, but this order is a %s" % (wrong[0], side)
    extra = sorted({word for word in words
                    if word not in YES_WORDS and word not in TICKET_WORDS and word not in SIDE_WORDS
                    and word != symbol.lower()})
    if extra:
        return False, ("the reply says %s, which is not part of a plain yes for %s; ask again and reply just "
                       "\"yes\"" % (", ".join("\"%s\"" % word for word in extra), symbol))
    numbers = [float(token.lstrip("$").replace(",", "")) for token in tokens if not token[0].isalpha()]
    order_numbers = amount_numbers(order, verdict)
    unknown_numbers = [number for number in numbers
                       if not any(abs(number - wanted) < 0.005 for wanted in order_numbers)]
    if unknown_numbers:
        return False, "the reply says %s, but this order is %s" % (
            ", ".join(number_string(number) for number in unknown_numbers), plain(order, verdict))
    return True, "explicit yes for %s" % plain(order, verdict)


# ---------------------------------------------------------------- state

def load_state(path):
    state = read_json(path, "broker state", required=False, kind=dict)
    if state is None:
        return {"orders": []}
    for key in ("orders", "accounts"):
        if (not isinstance(state.get(key, []), list)
                or not all(isinstance(record, dict) for record in state.get(key, []))):
            raise TicketError("%s is not trader-broker's log (\"%s\" should be a list of records); "
                              "move it aside and check the broker for today's orders" % (path, key))
    state.setdefault("orders", [])
    return state


def latest(state, order_fingerprint, today, practice=False):
    """The latest decision recorded today for this fingerprint, or None. A
    practice run (--now or --today) sees only practice decisions and a real run
    only real ones, so a practice yes never blocks or stands in for a real one."""
    found = None
    for record in state.get("orders", []):
        if (record.get("fingerprint") == order_fingerprint and record_day(record) == today
                and bool(record.get("practice")) == practice):
            found = record
    return found


def checked_after(risk, record):
    """True when the risk check ran after this decision was recorded."""
    checked_at, decided_at = parse_utc((risk or {}).get("now_utc")), parse_utc(record.get("at"))
    return checked_at is not None and decided_at is not None and checked_at > decided_at


def risk_verdict(risk, order_fingerprint, today, now=None, pinned=False):
    """trader-risk's result for this fingerprint, or (None, why not). A verdict
    counts for VERDICT_MINUTES, and only on the kind of clock it was checked on:
    a real run never takes a practice verdict (--now or --today), and back."""
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if not isinstance(risk, dict) or not isinstance(risk.get("results", []), list):
        return None, "the risk check file is not one check.py wrote; run check.py again"
    if risk.get("error"):
        return None, "the last risk check stopped with an error (%s); fix it and run check.py again" % risk["error"]
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    clock = risk.get("clock")
    if clock not in ("real", "pinned"):
        return None, "the risk check does not say which clock it ran on (an older check.py); run check.py again"
    if pinned and clock == "real":
        return None, "this is a practice run (--now or --today) and the risk check ran on the real clock"
    if not pinned and clock == "pinned":
        return None, ("the risk check was a practice run (--now or --today), so it cannot pass a real order; "
                      "run check.py without them")
    checked_at = parse_utc(risk.get("now_utc"))
    if checked_at is None:
        return None, "the risk check has no time on it; run check.py again"
    age = ((now or utc_now()) - checked_at).total_seconds()
    if age < -60:
        return None, "the risk check is dated after now (%s New York); run check.py again" % ny_iso(checked_at)[11:16]
    if age > VERDICT_MINUTES * 60:
        return None, ("the risk check is %d minutes old, and a PASS counts for %d; run check.py again"
                      % (age // 60, VERDICT_MINUTES))
    for row in risk.get("results", []):
        if isinstance(row, dict) and row.get("fingerprint") == order_fingerprint:
            return row, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def account_check(state, today):
    """Today's latest Alpaca account check (ticket.py account), or None."""
    found = None
    for record in state.get("accounts", []):
        if record_day(record) == today:
            found = record
    return found


def account_gap(config, state, today, pinned=False):
    """Why the Alpaca account is not confirmed for a real order today, or None.

    The PAPER / LIVE label comes from your config. Where an order really goes is
    the Alpaca server's own ALPACA_PAPER_TRADE and keys, and its account info
    does not say paper or live. So the account you checked is pinned by number
    ("alpaca_account") and checked against the server once a day."""
    config = config or {}
    if pinned or config.get("broker", "alpaca") != "alpaca":
        return None
    want = str(config.get("alpaca_account") or "").strip()
    if not want:
        return ("no Alpaca account pinned yet: run get_account_info, have the user check its account_number "
                "in the Alpaca dashboard, then the user puts it in trader/config.json as \"alpaca_account\"")
    record = account_check(state, today)
    if record is None:
        return ("the Alpaca account is not checked today: run get_account_info, then "
                "ticket.py account --number <its account_number>")
    if str(record.get("account_number", "")).upper() != want.upper():
        return "the account checked today (%s) is not the pinned one (%s)" % (record.get("account_number"), want)
    return None


def status_of(order, risk, state, today, now=None, pinned=False, config=None):
    """(ready, label, risk_result)"""
    order_fingerprint = fingerprint(order)
    verdict, why = risk_verdict(risk, order_fingerprint, today, now, pinned)
    if verdict is None:
        return False, "NOT READY: " + why, None
    if verdict.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(verdict.get("reasons", [])), verdict
    record = latest(state, order_fingerprint, today, pinned)
    status = (record or {}).get("status")
    if record and not (status in RETRY_STATUSES and checked_after(risk, record)):
        if status == "approved":
            return False, "APPROVED by you at %s, waiting for the place_stock_order call" % record.get("at"), verdict
        again = "; a new risk check (check.py) makes it READY for a new yes" if status in RETRY_STATUSES else ""
        return False, "DONE: %s at %s%s" % (status, record.get("at"), again), verdict
    gap = account_gap(config, state, today, pinned)
    if gap:
        return False, "NOT READY: " + gap, verdict
    retry = " (a new try: it was %s at %s)" % (status, record.get("at")) if record else ""
    return True, "READY: trader-risk PASS, waiting for your yes" + retry, verdict


def order_kind(order, verdict):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if verdict and verdict.get("kind"):
        return verdict["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def render(order, index, total, config, risk, state, today, now=None, pinned=False):
    ready, label, verdict = status_of(order, risk, state, today, now, pinned, config)
    broker = config.get("broker", "alpaca")
    acct = "PAPER account" if config.get("paper", True) else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s per your config, %s"
             % (index, total, acct, "Interactive Brokers" if broker == "ibkr" else "Alpaca")]
    for part in plain_parts(order, verdict):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for warning in (verdict or {}).get("warnings", []):
        lines.append("  note: " + warning)
    if broker == "ibkr":
        lines.append("  Claude drafts this with the Interactive Brokers connector. IBKR sends it to your")
        lines.append("  IBKR account, and it only goes to the market after you approve it there.")
    else:
        account_record = None if pinned else account_check(state, today)
        if account_record:
            lines.append("  account: %s, checked against your config today at %s New York"
                         % (account_record.get("account_number"), str(account_record.get("at", ""))[11:16]))
        if pinned:
            lines.append("  Practice run: in a real one, Claude would call place_stock_order with exactly:")
        else:
            lines.append("  Claude will call place_stock_order with exactly:")
        for line in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + line)
        if is_bracket(order):
            lines.append("  Alpaca places the entry, then the stop-loss and the take-profit as two orders")
            lines.append("  that cancel each other: when one fills, the other is canceled.")
    if ready and pinned:
        lines.append("  Reply \"yes\" to practice the yes, or \"no\" to skip it. Nothing is placed.")
    elif ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if broker != "ibkr":
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, index, config, risk, state, today, now=None, pinned=False):
    ready, label, verdict = status_of(order, risk, state, today, now, pinned, config)
    loss, stop = max_loss(order, verdict)
    return {"index": index, "ready": ready, "status": label, "plain": plain(order, verdict),
            "fingerprint": fingerprint(order), "paper": bool(config.get("paper", True)),
            "broker": config.get("broker", "alpaca"), "tool": "place_stock_order",
            "tool_input": tool_input(order), "why": order.get("why", ""),
            "bracket": is_bracket(order), "max_loss": loss, "stop": stop, "practice": pinned,
            "account": None if pinned else account_check(state, today)}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, config, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
    portfolio = read_json(portfolio_path, "portfolio", required=False, kind=dict)
    if portfolio is None:
        budget = config.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        portfolio = {"cash": float(budget), "positions": []}
    if not isinstance(portfolio.setdefault("positions", []),
                      list) or not all(isinstance(row, dict) for row in portfolio["positions"]):
        raise TicketError("%s: \"positions\" should be a list of { \"ticker\": ..., \"shares\": ... }" % portfolio_path)
    symbol = symbol.upper()
    qty, price = finite_number(qty), finite_number(price)
    if not qty or not price or qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    position = None
    for row in portfolio["positions"]:
        if str(row.get("ticker") or row.get("symbol") or "").upper() == symbol:     # either name, like the readers
            position = row
    if side == "buy":
        if position is None:
            position = {"ticker": symbol, "shares": 0.0, "lots": []}
            portfolio["positions"].append(position)
        lots = position.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        position["shares"] = round(float(position.get("shares", 0)) + qty, 6)
        portfolio["cash"] = round(float(portfolio.get("cash", 0)) - cost, 2)
    else:
        if position is None or float(position.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (number_string(qty), symbol, position.get("shares", 0) if position else 0))
        left = qty
        for lot in sorted(position.get("lots", []), key=lambda lot: lot.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        position["lots"] = [lot for lot in position.get("lots", []) if float(lot["shares"]) > 1e-9]
        position["shares"] = round(float(position["shares"]) - qty, 6)
        if position["shares"] <= 1e-9:
            portfolio["positions"].remove(position)
        portfolio["cash"] = round(float(portfolio.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, portfolio)
    return portfolio


def log_fill(state_path, now, today, symbol, side, qty, price, practice=False):
    """A fill in the log, so trader-risk can count day trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    record = {"at": ny_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
              "qty": number_string(float(qty)), "price": float(price),
              "plain": "%s %s %s at %s" % (side.upper(), number_string(float(qty)), symbol.upper(), price_text(price))}
    if practice:
        record["practice"] = True
    state.setdefault("orders", []).append(record)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def run_clock(args):
    """(now, today, pinned). A pinned clock (--now or --today) is a practice run."""
    now = parse_now(getattr(args, "now", None))
    today = getattr(args, "today", None) or to_eastern(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-19")
    return now, today, bool(getattr(args, "now", None) or getattr(args, "today", None))


def load_orders(args):
    doc = read_json(args.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % args.orders)
    if not all(isinstance(order, dict) for order in orders):
        raise TicketError("%s: every order should be one JSON object, { ... }" % args.orders)
    config = rules(read_json(args.config, "config", required=False))
    risk = read_json(args.risk, "risk check", required=False)
    state = load_state(args.state)
    now, today, pinned = run_clock(args)
    return orders, config, risk, state, today, now, pinned


def pick_order(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


PRACTICE = ("PRACTICE RUN: the clock is pinned with --now or --today, so nothing here goes to the broker and a yes "
            "here is not an order. Run check.py and ticket.py without them for a real order.")


def cmd_show(args):
    orders, config, risk, state, today, now, pinned = load_orders(args)
    picks = [(args.index, pick_order(orders, args.index))] if args.index else list(enumerate(orders, 1))
    if args.json:
        out = [ticket_json(order, index, config, risk, state, today, now, pinned) for index, order in picks]
        print(json.dumps(out[0] if args.index else out, indent=2))
        return 0 if any(ticket["ready"] for ticket in out) else 1
    blocks, any_ready = [], False
    for index, order in picks:
        text, ready = render(order, index, len(orders), config, risk, state, today, now, pinned)
        blocks.append(text)
        any_ready = any_ready or ready
    if pinned:
        print(PRACTICE + "\n")
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(args):
    orders, config, risk, state, today, now, pinned = load_orders(args)
    order = pick_order(orders, args.index)
    ready, label, verdict = status_of(order, risk, state, today, now, pinned, config)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (args.index, label))
    ok, why = parse_yes(args.reply, order, [other.get("symbol", "") for other in orders], verdict)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, args.index))
        return 1
    loss, stop = max_loss(order, verdict)
    record = {"at": ny_iso(now), "date": today, "index": args.index, "fingerprint": fingerprint(order),
              "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
              "qty": order.get("qty"), "kind": order_kind(order, verdict), "max_loss": loss, "stop": stop,
              "plain": plain(order, verdict), "status": "approved", "reply": args.reply,
              "paper": bool(config.get("paper", True)), "broker": config.get("broker", "alpaca")}
    if pinned:
        record["practice"] = True
    else:
        account_record = account_check(state, today)
        if account_record:
            record["account"] = account_record.get("account_number")
    state.setdefault("orders", []).append(record)
    write_json(args.state, state)
    print("Recorded your yes for order %d: %s." % (args.index, plain(order, verdict)))
    if pinned:
        print(PRACTICE)
        print("There is nothing to place. Practice the rest with: ticket.py result --index %d --status "
              "placed|denied|failed --now ..." % args.index)
    elif config.get("broker", "alpaca") == "ibkr":
        print("Now draft it with the Interactive Brokers connector; approve it in your IBKR account.")
    else:
        print("Now call place_stock_order with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % args.index)
    return 0


def cmd_account(args):
    """Today's check of the Alpaca account the server is connected to: the
    account_number from get_account_info against "alpaca_account" in the config."""
    config = rules(read_json(args.config, "config", required=False))
    now, today, pinned = run_clock(args)
    got = args.number.strip()
    kind = "paper" if config["paper"] else "LIVE"
    want = str(config.get("alpaca_account") or "").strip()
    if not ACCOUNT_NUMBER.match(got):
        raise TicketError("--number wants the account_number from get_account_info, like PA1234ABCD")
    if not want:
        raise TicketError("no \"alpaca_account\" in %s yet. Show the user this account number (%s) and ask them "
                          "to find it in the Alpaca dashboard: only if it is their %s account, they add "
                          "\"alpaca_account\": \"%s\" to the config themselves" % (args.config, got, kind, got))
    if got.upper() != want.upper():
        raise TicketError("STOP: the Alpaca server is connected to account %s, but %s expects %s (your %s "
                          "account). Place nothing. The keys and ALPACA_PAPER_TRADE in the Alpaca server's own "
                          "config decide which account it uses" % (got, args.config, want, kind))
    state = load_state(args.state)
    state.setdefault("accounts", []).append({"at": ny_iso(now), "date": today, "account_number": got,
                                             "paper": config["paper"]})
    write_json(args.state, state)
    print("Alpaca account %s is the one pinned in your config (your %s account). Checked for %s."
          % (got, kind, today))
    return 0


def cmd_decline(args):
    orders, config, risk, state, today, now, pinned = load_orders(args)
    order = pick_order(orders, args.index)
    record = {"at": ny_iso(now), "date": today, "index": args.index, "fingerprint": fingerprint(order),
              "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
              "plain": plain(order), "status": "declined", "reply": args.reply or "no"}
    if pinned:
        record["practice"] = True
    state.setdefault("orders", []).append(record)
    write_json(args.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (args.index, plain(order)))
    return 0


def cmd_result(args):
    orders, config, risk, state, today, now, pinned = load_orders(args)
    order = pick_order(orders, args.index)
    order_fingerprint = fingerprint(order)
    record = latest(state, order_fingerprint, today, pinned)
    if not record or record.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % args.index)
    new = {"at": ny_iso(now), "date": today, "index": args.index, "fingerprint": order_fingerprint,
           "symbol": record["symbol"],
           "side": record["side"], "plain": record["plain"], "status": args.status}
    for key in ("qty", "kind", "max_loss", "stop", "practice", "account"):
        if record.get(key) is not None:
            new[key] = record[key]
    if args.broker_id:
        new["broker_order_id"] = args.broker_id
    if args.note:
        new["note"] = args.note
    worse = None
    fill, qty, stop = (finite_number(args.filled_price), finite_number(record.get("qty")),
                       finite_number(record.get("stop")))
    if args.status == "placed" and record.get("kind") == "entry" and fill and qty and stop:
        new["filled_price"] = fill
        at_fill = round(qty * int(round(abs(fill - stop) * 100)) / 100.0, 2)
        if new.get("max_loss") is None or at_fill > new["max_loss"] + 0.005:
            new["max_loss"] = worse = at_fill    # the daily loss limit counts what really is at risk
    state["orders"].append(new)
    write_json(args.state, state)
    print("Recorded order %d as %s." % (args.index, args.status))
    if worse is not None:
        print("It filled at %s, so the max loss at the stop %s is %s now; today's loss limit counts that."
              % (price_text(fill), price_text(stop), dollars(worse)))
    if args.status == "placed" and args.filled_qty and args.filled_price:
        apply_fill(args.portfolio, config, record["symbol"], record["side"], args.filled_qty, args.filled_price, today)
        log_fill(args.state, now, today, record["symbol"], record["side"], args.filled_qty, args.filled_price, pinned)
        print("Added the fill to %s." % args.portfolio)
    elif args.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (record["symbol"], record["side"]))
        if record.get("kind") == "entry" and is_bracket(order):
            print("When the stop-loss or the take-profit fills later, record that sell the same way.")
    elif args.status in RETRY_STATUSES:
        print("To try it again: fix what stopped it, run check.py again, then ask the user for a new yes.")
    return 0


def cmd_fill(args):
    config = rules(read_json(args.config, "config", required=False))
    now = parse_now(args.now)
    date = args.date or to_eastern(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-19")
    portfolio = apply_fill(args.portfolio, config, args.symbol, args.side, args.qty, args.price, date)
    log_fill(args.state, now, date, args.symbol, args.side, args.qty, args.price)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (args.side, args.qty, args.symbol.upper(),
                                                                dollars(args.price), date, args.portfolio,
                                                                dollars(portfolio["cash"])))
    return 0


def cmd_flatten(args):
    config = rules(read_json(args.config, "config", required=False))
    symbol = args.symbol.strip().upper()
    now, today, pinned = run_clock(args)
    source = "the broker, get_open_position"
    if args.qty is not None:
        qty = finite_number(args.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 8")
    else:
        portfolio = read_json(args.portfolio, "portfolio", required=False, kind=dict) or {}
        rows = portfolio.get("positions")
        qty = sum(finite_number(position.get("shares", position.get("qty"))) or 0
                  for position in (rows if isinstance(rows, list) else [])
                  if isinstance(position, dict)
                  and str(position.get("ticker") or position.get("symbol") or "").upper() == symbol)
        source = args.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_open_position "
                              "%s) and pass --qty if you do hold some" % (symbol, args.portfolio, symbol))
    plan = read_json(args.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == symbol else None) \
        or config.get("flat_by") or "15:55"
    order = {"symbol": symbol, "side": "sell", "qty": number_string(qty), "type": "market", "time_in_force": "day",
             "why": "flat by %s New York: closes the %s day trade before the close (qty from %s)"
             % (flat_by, symbol, source)}
    old = read_json(args.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(config.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (symbol, today), "orders": [order]}
    write_json(args.orders, doc)
    print("Flatten %s: %s (flat by %s New York)." % (symbol, plain(order), flat_by))
    print("Wrote it to %s%s." % (args.orders, (" (it replaced %d earlier proposal%s)"
                                                % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if args.qty is None:
        print("The qty comes from %s. Check it against get_open_position %s before you say yes." % (source, symbol))
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. If the bracket's stop-loss and take-profit are still open, Alpaca holds these shares for them")
    print("     and rejects a new sell. Cancel them first: get_orders (status open, nested true), then")
    print("     cancel_order_by_id for each open leg (Needs approval). Then place_stock_order with the ticket.")
    return 0


def cmd_log(args):
    state = load_state(args.state)
    now, today, pinned = run_clock(args)
    rows = [record for record in state.get("orders", []) if args.all or record_day(record) == today]
    for record in state.get("accounts", []):
        if args.all or record_day(record) == today:
            print("%s     account   Alpaca %s checked against your config"
                  % (record.get("at"), record.get("account_number")))
    if not rows:
        print("No decisions recorded%s." % ("" if args.all else " today"))
        return 1
    for record in rows:
        extra = (" id " + record["broker_order_id"]) if record.get("broker_order_id") else ""
        reply = ((' reply "%s"' % record["reply"])
                 if record.get("reply") and record["status"] in ("approved", "declined") else "")
        loss = ((" max loss %s" % dollars(record["max_loss"]))
                if record.get("max_loss") is not None and record["status"] == "placed" else "")
        index_text = ("#%s" % record["index"]) if record.get("index") is not None else "  "
        extra += " (practice)" if record.get("practice") else ""
        print("%s  %s %-9s %s%s%s%s" % (record.get("at"), index_text, record.get("status"), record.get("plain"), reply,
                                        loss, extra))
    placed = sum(1 for record in rows if record.get("status") == "placed")
    print("%d placed%s." % (placed, "" if args.all else " today"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    commands = parser.add_subparsers(dest="cmd")

    def clock(command):
        command.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\" (practice)")
        command.add_argument("--today", help="YYYY-MM-DD (default: today in New York; practice)")

    def common(command):
        command.add_argument("--orders", default=home("orders.json"))
        command.add_argument("--config", default=home("config.json"))
        command.add_argument("--risk", help="trader-risk's verdicts (default trader/state/risk.json)")
        command.add_argument("--state", help="the decision log (default trader/state/broker.json)")
        clock(command)

    command = commands.add_parser("show", help="render the tickets")
    common(command)
    command.add_argument("--index", type=int)
    command.add_argument("--json", action="store_true")
    command = commands.add_parser("account", help="record today's check of the Alpaca account (get_account_info)")
    command.add_argument("--number", required=True, help="the account_number get_account_info shows")
    command.add_argument("--config", default=home("config.json"))
    command.add_argument("--state")
    clock(command)
    command = commands.add_parser("approve", help="record the user's explicit yes for one order")
    common(command)
    command.add_argument("--index", type=int, required=True)
    command.add_argument("--reply", required=True, help="the user's reply, word for word")
    command = commands.add_parser("decline", help="record a no")
    common(command)
    command.add_argument("--index", type=int, required=True)
    command.add_argument("--reply")
    command = commands.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(command)
    command.add_argument("--index", type=int, required=True)
    command.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    command.add_argument("--broker-id")
    command.add_argument("--note")
    command.add_argument("--filled-qty")
    command.add_argument("--filled-price")
    command.add_argument("--portfolio")
    command = commands.add_parser("fill",
                                  help="record a fill from your broker in trader/portfolio.json (never practice)")
    command.add_argument("--symbol", required=True)
    command.add_argument("--side", required=True, choices=["buy", "sell"])
    command.add_argument("--qty", required=True)
    command.add_argument("--price", required=True)
    command.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    command.add_argument("--now")
    command.add_argument("--portfolio", default=home("portfolio.json"))
    command.add_argument("--config", default=home("config.json"))
    command.add_argument("--state", default=home("state", "broker.json"))
    command = commands.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    command.add_argument("--symbol", required=True)
    command.add_argument("--qty", help="shares you hold, from get_open_position (default: trader/portfolio.json)")
    command.add_argument("--orders", default=home("orders.json"), help="where the ticket is written")
    command.add_argument("--portfolio")
    command.add_argument("--plan", default=home("plan.json"))
    command.add_argument("--config", default=home("config.json"))
    clock(command)
    command = commands.add_parser("log", help="today's decisions")
    command.add_argument("--state")
    clock(command)
    command.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    if args.cmd != "fill":
        # a practice run (--now or --today) keeps its own verdicts, log and account
        room = ("practice",) if (args.now or args.today) else ()
        for key, parts in (("risk", ("state", "risk.json")), ("state", ("state", "broker.json")),
                           ("portfolio", ("portfolio.json",))):
            if hasattr(args, key) and getattr(args, key) is None:
                setattr(args, key, home(*room, *parts))
    try:
        return {"show": cmd_show, "account": cmd_account, "approve": cmd_approve, "decline": cmd_decline,
                "result": cmd_result, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[args.cmd](args)
    except TicketError as error:
        print("ticket.py: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
