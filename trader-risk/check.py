#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 10:15"        # pin the clock (New York time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order (Alpaca's rule),
                a limit order without a limit price, unknown type; a bracket
                that is not whole shares, not day / gtc, or misses a leg
  asset         options (OCC symbols) unless "allow_options": true; crypto
                and anything that is not a stock or ETF
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_usd, over max_order_usd
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) over max_loss_per_trade_usd. The entry is
                the limit price for a limit order, else the HIGHER of the plan's
                entry and a fresh price (under 15 minutes old), so a stale plan
                cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_usd (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (New York
                time), or the market is closed (weekend, NYSE holiday, outside
                9:30 to the close; on a 1:00 p.m. early close the window ends
                3 hours earlier too)
  pdt           only with "pdt_guard": true: 3 day trades already in the last
                5 business days (a 4th would trip the old pattern-day-trader rule)

Your config is checked before any order. A value this tool cannot read stops
the check, so a typo never switches a limit off: numbers are plain numbers
(50, not "50$"), true and false have no quotes. null switches a limit off and
the report says so every time; 0 means zero (max_trades_per_day 0: no new
entries today).

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order
and the time they were checked, so trader-broker only shows an order as ready
if THIS exact order passed in the last 15 minutes. A check that stops with an
error clears the verdicts it finds.

--now and --today make it a practice run: the verdicts are marked practice
(trader-broker never turns those into a real order), and it keeps its own
account, log and verdicts in trader/practice/ unless you pass the paths.

trader/ is the folder you run in; set TRADER_HOME to use one folder from
anywhere (TRADER_HOME=C:/trading/trader puts every file there).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
    "min_order_usd": 5, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": False,
}
BOOL_KEYS = ("require_stop", "paper", "allow_options", "allow_margin", "allow_short", "pdt_guard")
# null switches one of these off (and the report says so); 0 means zero
LIMIT_KEYS = ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day", "max_orders_per_day",
              "min_reward_risk", "max_position_pct", "max_order_usd", "cash_buffer_pct")
NUMBER_KEYS = ("budget", "min_order_usd") + LIMIT_KEYS
WHOLE_KEYS = ("max_trades_per_day", "max_orders_per_day")
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
ORDER_TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIME_IN_FORCE_VALUES = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
HALF_CENT = 0.005
FRESH_MINUTES = 15
ENTRY_REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
                   "stop price": "the stop price", "stale price": "an old price"}

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
    # 2028: NYSE's release of 2025-12-23. No New Year's Day holiday: January 1 2028 is a Saturday.
    "2028-01-17": "Martin Luther King, Jr. Day",
    "2028-02-21": "Washington's Birthday",
    "2028-04-14": "Good Friday",
    "2028-05-29": "Memorial Day",
    "2028-06-19": "Juneteenth National Independence Day",
    "2028-07-04": "Independence Day",
    "2028-09-04": "Labor Day",
    "2028-11-23": "Thanksgiving Day",
    "2028-12-25": "Christmas Day",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
    "2028-07-03": "day before Independence Day",
    "2028-11-24": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027, 2028)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class RiskError(Exception):
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


def utc_iso(utc_time):
    return utc_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return utc_now()
    text_iso = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text_iso)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = (moment - moment.utcoffset()).replace(tzinfo=None)
    return moment


def trading_day(day):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if day.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(day.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def parse_window_time(value, key):
    """"09:45" -> time(9, 45); null -> None (that bound is off)."""
    if value is None:
        return None
    match = HHMM.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise RiskError("%s must be a New York time like \"09:45\", or null to switch it off, got %s"
                        % (key, json.dumps(value)))
    return datetime.time(int(match.group(1)), int(match.group(2)))


def hours_earlier(clock_time, hours):
    moment = datetime.datetime.combine(datetime.date(2000, 1, 1), clock_time) - datetime.timedelta(hours=hours)
    return moment.time()


def window_check(now_et, settings):
    """Reasons an entry cannot be placed now (New York time). Empty when the window is off."""
    before = parse_window_time(settings.get("no_entries_before"), "no_entries_before")
    after = parse_window_time(settings.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warnings = []
    day, clock_time = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    if day.year not in CALENDAR_YEARS:
        warnings.append("the holiday list covers %d to %d only: check nyse.com for %s"
                        % (CALENDAR_YEARS[0], CALENDAR_YEARS[-1], day.isoformat()))
    is_open, why = trading_day(day)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    if not is_open:
        return ["window: the market is closed today (%s, %s New York)" % (why, stamp)], warnings
    close = EARLY if day.isoformat() in NYSE_EARLY_CLOSE else CLOSE
    if clock_time < OPEN or now_et.time() >= close:
        return ["window: the market is closed (%s New York; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warnings
    if close == EARLY and after is not None:
        after = hours_earlier(after, 3)
        warnings.append("early close at 1:00 p.m. today (%s): no new entries after %s"
                        % (NYSE_EARLY_CLOSE[day.isoformat()], after.strftime("%H:%M")))
    if before is not None and clock_time < before:
        return ["window: no new entries before %s New York (it is %s)" % (before.strftime("%H:%M"),
                                                                          clock_time.strftime("%H:%M"))], warnings
    if after is not None and clock_time > after:
        return ["window: no new entries after %s New York (it is %s)" % (after.strftime("%H:%M"),
                                                                         clock_time.strftime("%H:%M"))], warnings
    return [], warnings


def business_days(today, count=5):
    """The last count NYSE trading days up to and including today, newest first."""
    days, day = [], today
    for _ in range(40):
        if trading_day(day)[0]:
            days.append(day.isoformat())
            if len(days) == count:
                break
        day -= datetime.timedelta(days=1)
    return days


# ---------------------------------------------------------------- files and helpers

def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


def read_json(path, what, required=True, kind=None):
    """The JSON at path, or None when it is missing and not required. kind=dict
    or list: anything else there is an error, not a traceback later."""
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as file:
            doc = json.load(file)
    except ValueError as error:
        raise RiskError("%s is not valid JSON: %s" % (path, error))
    except OSError as error:
        raise RiskError("cannot read %s: %s" % (path, error))
    if kind is dict and not isinstance(doc, dict):
        raise RiskError("%s should be one JSON object, { ... }" % path)
    return doc


def real_only(state):
    """The broker log without practice decisions: a practice run (--now or
    --today) never counts toward the real day's limits."""
    if not state:
        return state
    return dict(state, orders=[record for record in state.get("orders", []) if not record.get("practice")])


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    fields = {field: order[field] for field in ALPACA_FIELDS if field in order and order[field] is not None}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def finite_number(value):
    """A finite number, or None (NaN and infinity are not prices or share counts)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def dollars(amount):
    return "$" + "{:,.2f}".format(float(amount)).replace(".00", "")


def round_to_cent(price):
    """A price to the cent, half up (366.475 -> 366.48)."""
    return int(float(price) * 100 + 0.5 + 1e-9) / 100.0


def price_text(price):
    return "%.2f" % float(price)


def starting_account(settings, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = finite_number(settings.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = finite_number(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    positions = portfolio.get("positions") or []
    if not isinstance(positions, list) or not all(isinstance(position, dict) for position in positions):
        raise RiskError("portfolio.json: \"positions\" should be a list of { \"ticker\": ..., \"shares\": ... }")
    shares, values = {}, {}
    for position in positions:
        ticker = str(position.get("ticker") or position.get("symbol") or "").upper()
        if not ticker:
            continue
        count = finite_number(position.get("shares", position.get("qty")))
        if count is None:
            count = sum(finite_number(lot.get("shares")) or 0 for lot in position.get("lots", []))
        shares[ticker] = shares.get(ticker, 0) + count
        if prices.get(ticker):
            values[ticker] = shares[ticker] * prices[ticker]
        else:
            basis = sum(finite_number(lot.get("cost")) or 0 for lot in position.get("lots", []))
            values[ticker] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (ticker, dollars(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def record_day(record):
    return str(record.get("date") or str(record.get("at", ""))[:10])


def is_entry_record(record):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    kind = record.get("kind")
    if kind:
        return kind == "entry"
    return str(record.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for record in (state or {}).get("orders", []):
        if record.get("status") == "placed" and record_day(record) == today:
            out.append(record)
    return out


def entry_loss(record, fills):
    """The max loss at the stop of an entry placed today: the logged one, or more
    if a fill logged today came in worse (a market order can fill above the price
    it was checked at). None when the log has neither."""
    max_loss = finite_number(record.get("max_loss"))
    stop, qty = finite_number(record.get("stop")), finite_number(record.get("qty"))
    if not stop or not qty:
        return max_loss
    symbol, side = str(record.get("symbol", "")).upper(), str(record.get("side", "")).lower()
    for fill in fills:
        price = finite_number(fill.get("price"))
        if price and str(fill.get("symbol", "")).upper() == symbol and str(fill.get("side", "")).lower() == side:
            worst = round(qty * int(round(abs(price - stop) * 100)) / 100.0, 2)
            if max_loss is None or worst > max_loss + HALF_CENT:
                max_loss = worst
    return max_loss


def day_trades(state, today):
    """Day trades in the last 5 business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for record in (state or {}).get("orders", []):
        if record.get("status") not in ("placed", "filled"):
            continue
        day = record_day(record)
        if day not in days:
            continue
        key = (day, str(record.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(record.get("side", "")).lower())
        if day == today and record.get("status") == "placed" and is_entry_record(record):
            sides[key].add("pending")
    hits = sorted("%s %s" % day_symbol for day_symbol, day_sides in sides.items()
                  if ("buy" in day_sides and "sell" in day_sides)
                  or (day_symbol[0] == today and "pending" in day_sides))
    return len(hits), hits


# ---------------------------------------------------------------- your rules

def rules(config):
    """DEFAULTS plus your config, checked. A value this tool cannot read stops the
    check (RiskError), so a typo never switches a limit off. null switches a limit
    off; 0 means zero."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise RiskError("config: trader/config.json must be one JSON object, { ... }")
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
        elif key in WHOLE_KEYS and value != int(value):
            bad.append("%s must be a whole number (it is %s)" % (key, json.dumps(value)))
    if settings["blocked"] is None:
        settings["blocked"] = []
    if not isinstance(settings["blocked"], list) or not all(isinstance(ticker, str) for ticker in settings["blocked"]):
        bad.append("blocked must be a list of tickers like [\"GME\"] (it is %s)" % json.dumps(settings["blocked"]))
    for key in ("no_entries_before", "no_entries_after"):
        try:
            parse_window_time(settings[key], key)
        except RiskError as error:
            bad.append(str(error))
    if bad:
        raise RiskError("config: %s. Nothing was checked: fix trader/config.json and run it again"
                        % "; ".join(bad))
    return settings


def switched_off(settings):
    """Warnings naming every check the config switches off, and every risk it switches on."""
    off = ["%s (null)" % key for key in LIMIT_KEYS if settings[key] is None]
    if not settings["require_stop"]:
        off.append("require_stop (false)")
    if settings["no_entries_before"] is None and settings["no_entries_after"] is None:
        off.append("the entry window and market hours (no_entries_before and no_entries_after are null)")
    else:
        off.extend("%s (null)" % key for key in ("no_entries_before", "no_entries_after") if settings[key] is None)
    out = []
    if off:
        out.append("these checks are off in your config: " + ", ".join(off))
    switched_on = [key for key in ("allow_options", "allow_margin", "allow_short") if settings[key]]
    if switched_on:
        out.append("switched on in your config: " + ", ".join(switched_on))
    return out


# ---------------------------------------------------------------- the check

def check(orders, config, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its New York date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    settings = rules(config)
    prices = {str(symbol).upper(): float(price) for symbol, price in (prices or {}).items() if finite_number(price)}
    now_utc = now or parse_now(None)
    now_et = to_eastern(now_utc)
    today = today or now_et.date().isoformat()
    account, top_warnings = starting_account(settings, portfolio, prices)
    blocked = {ticker.upper() for ticker in settings["blocked"]}
    placed_list = placed_today(state, today)
    count_today = len(placed_list)
    entries_today = [record for record in placed_list if is_entry_record(record)]
    fills = [record for record in (state or {}).get("orders", [])
             if record.get("status") == "filled" and record_day(record) == today]
    loss_today = 0.0
    for record in entries_today:
        entry_max_loss = entry_loss(record, fills)
        if entry_max_loss is None:
            top_warnings.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                                "the daily loss" % (record.get("side"), record.get("symbol")))
        else:
            loss_today += entry_max_loss
    placed_keys = {(str(record.get("symbol", "")).upper(), record.get("side")) for record in placed_list}
    window_reasons, window_warnings = window_check(now_et, settings)
    day_trade_count, day_trade_list = day_trades(state, today) if settings["pdt_guard"] else (0, [])
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not settings["paper"]:
        top_warnings.append("LIVE account (\"paper\": false): these orders use real money")
    top_warnings.extend(switched_off(settings))
    top_warnings.extend(window_warnings)

    def fresh(symbol):
        if symbol not in prices:
            return None
        if price_times is None:
            return prices[symbol]
        price_time = price_times.get(symbol)
        if price_time is None:
            return None
        age = (now_utc - price_time).total_seconds()
        return prices[symbol] if -60 <= age <= FRESH_MINUTES * 60 else None

    for number, order in enumerate(orders, 1):
        reasons, warnings, info = [], [], []
        entry_reasons = []                       # entry-only reasons, turned into warnings for an exit
        symbol = str(order.get("symbol", "")).strip().upper()
        side = str(order.get("side", "")).strip().lower()
        order_type = str(order.get("type", "market")).strip().lower()
        time_in_force = str(order.get("time_in_force", "day")).strip().lower()
        qty, notional = order.get("qty"), order.get("notional")
        plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
        order_class = str(order.get("order_class") or "").strip().lower()
        has_legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
        bracket = order_class == "bracket" or (not order_class and has_legs)
        value = None

        # format
        if not symbol:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % order.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for field in PRICE_FIELDS:
            if order.get(field) is not None:
                field_value = finite_number(order.get(field))
                if field_value is None or field_value <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (field, order.get(field)))
                elif not isinstance(order.get(field), str):
                    warnings.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                    % (field, order.get(field)))
        if order_type not in ORDER_TYPES:
            reasons.append("format: unknown order type %r" % order_type)
        if time_in_force not in TIME_IN_FORCE_VALUES:
            reasons.append("format: unknown time_in_force %r" % time_in_force)
        if notional is not None and (order_type != "market" or time_in_force != "day"):
            reasons.append("format: a notional (dollar) order must be a market order with time_in_force day")
        if order_type in ("limit", "stop_limit") and order.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % order_type)
        if order_type in ("stop", "stop_limit") and order.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % order_type)
        if order.get("extended_hours") and not (order_type == "limit" and time_in_force in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if order.get("stop_loss_limit_price") is not None and order.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if notional is not None:
                reasons.append("format: a bracket order needs a whole-share qty, not a notional amount")
            elif finite_number(qty) is not None and abs(finite_number(qty) - round(finite_number(qty))) > 1e-9:
                reasons.append("format: a bracket order takes whole shares only, got qty %r" % qty)
            if time_in_force not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % time_in_force)
            if order.get("take_profit_limit_price") is None or order.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif order_class or order.get("advanced_instructions"):
            warnings.append("advanced order (%s): check every leg before you say yes"
                            % (order_class or "advanced_instructions"))

        # asset
        if symbol and OCC_OPTION_SYMBOL.match(symbol.replace(" ", "")):
            if not settings["allow_options"]:
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % symbol)
            else:
                warnings.append("option order: options can lose 100%% of what you pay")
        elif symbol and ("/" in symbol or not TICKER.match(symbol)):
            reasons.append("asset: %s does not look like a US stock or ETF ticker; "
                           "this pack trades stocks and ETFs only" % symbol)

        # the reference price: what one share costs to get in
        plan_entry = finite_number(plan.get("entry"))
        live = fresh(symbol)
        entry_ref, entry_ref_from = None, None
        if order_type in ("limit", "stop_limit"):
            entry_ref, entry_ref_from = finite_number(order.get("limit_price")), "limit"
        elif order_type == "stop":
            entry_ref, entry_ref_from = finite_number(order.get("stop_price")), "stop price"
        if entry_ref is None:
            candidates = [(price, source) for price, source in ((plan_entry, "plan"), (live, "price")) if price]
            if candidates:
                entry_ref, entry_ref_from = max(candidates, key=lambda candidate: candidate[0])
                entry_ref = round_to_cent(entry_ref)
                if live and entry_ref_from == "price" and plan_entry and live > plan_entry + HALF_CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (symbol, price_text(entry_ref), price_text(plan_entry), price_text(entry_ref)))
            elif symbol in prices:
                entry_ref, entry_ref_from = prices[symbol], "stale price"
        if plan_entry and live is None and symbol in prices and price_times is not None and order_type == "market":
            warnings.append("the %s price in trader/prices.json is older than %d minutes: not used"
                            % (symbol, FRESH_MINUTES))
        share_count = finite_number(qty)
        if notional is not None and finite_number(notional):
            value = finite_number(notional)
        elif share_count:
            if entry_ref:
                value = share_count * entry_ref

        # entry or exit
        held = account["shares"].get(symbol, 0.0)
        held_value = account["values"].get(symbol, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if share_count is not None:
                exiting = share_count <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + HALF_CENT
        elif side == "buy" and held < -1e-9 and share_count is not None:
            exiting = share_count <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and share_count:
            entry_reasons.append("price: no price for %s to size a %s-share order; add it to trader/prices.json "
                                 "(trader-alerts: alerts.py quote %s --write)" % (symbol, qty, symbol))

        # blocked list and size
        if symbol in blocked:
            entry_reasons.append("blocked: %s is on your blocked list" % symbol)
        if value is not None:
            if settings["min_order_usd"] is not None and value < settings["min_order_usd"] - HALF_CENT:
                entry_reasons.append("size: %s is under your %s minimum order"
                                     % (dollars(value), dollars(settings["min_order_usd"])))
            if settings["max_order_usd"] is not None and value > settings["max_order_usd"] + HALF_CENT:
                entry_reasons.append("size: %s is over your %s max order"
                                     % (dollars(value), dollars(settings["max_order_usd"])))

        # account effects, simulated in order
        cash = account["cash"]
        total = cash + sum(account["values"].values())
        buffer_pct = settings["cash_buffer_pct"] or 0
        buffer_usd = total * buffer_pct / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -HALF_CENT and not settings["allow_margin"]:
                entry_reasons.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                                     % (dollars(value), dollars(cash)))
            elif after_cash < buffer_usd - HALF_CENT:
                entry_reasons.append("cash: leaves %s cash, under your %s%% buffer of %s"
                                     % (dollars(max(after_cash, 0)), percent_text(buffer_pct), dollars(buffer_usd)))
            position_after = held_value + value
            if settings["max_position_pct"] is not None and total > 0:
                limit = total * settings["max_position_pct"] / 100.0
                if position_after > limit + HALF_CENT:
                    entry_reasons.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                                         % (symbol, dollars(position_after), 100.0 * position_after / total,
                                            dollars(total), percent_text(settings["max_position_pct"])))
        if side == "sell" and symbol and kind == "entry":
            if share_count is not None:
                over = share_count > held + 1e-9
            elif value is not None:
                over = value > held_value + HALF_CENT
            else:
                over = held <= 0
            if over and not settings["allow_short"]:
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (symbol, number_text(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_per_share = reward_risk = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            is_long = side == "buy"
            max_loss_cap, min_reward_risk, daily_loss_cap = (settings["max_loss_per_trade_usd"],
                                                             settings["min_reward_risk"],
                                                             settings["max_daily_loss_usd"])
            max_trades = None if settings["max_trades_per_day"] is None else int(settings["max_trades_per_day"])
            leg_stop, plan_stop = finite_number(order.get("stop_loss_stop_price")), finite_number(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = finite_number(order.get("take_profit_limit_price")) or finite_number(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > HALF_CENT:
                warnings.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                                "the broker uses" % (price_text(leg_stop), price_text(plan_stop)))
            if stop and not leg_stop:
                warnings.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                                "(trader-alerts: alerts.py add --from-plan)" % price_text(stop))
            if not stop:
                if settings["require_stop"]:
                    entry_reasons.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                         "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warnings.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif entry_ref:
                if (is_long and stop >= entry_ref - HALF_CENT) or (not is_long and stop <= entry_ref + HALF_CENT):
                    entry_reasons.append("stop: the stop %s must be %s the entry %s for a %s"
                                         % (price_text(stop), "below" if is_long else "above", price_text(entry_ref),
                                            side))
                else:
                    risk_cents = int(round(abs(entry_ref - stop) * 100))
                    risk_per_share = risk_cents / 100.0
                    shares = share_count if share_count is not None else ((value or 0) / entry_ref)
                    max_loss = round(shares * risk_per_share, 2)
                    if max_loss_cap is not None and max_loss > max_loss_cap + HALF_CENT:
                        entry_reasons.append("loss: %s shares x %s risk a share = %s at the stop, over your %s "
                                             "max loss per trade" % (number_text(shares), price_text(risk_per_share),
                                                                     dollars(max_loss), dollars(max_loss_cap)))
                    if target:
                        if ((is_long and target <= entry_ref + HALF_CENT)
                                or (not is_long and target >= entry_ref - HALF_CENT)):
                            entry_reasons.append("target: the target %s must be %s the entry %s"
                                                 % (price_text(target), "above" if is_long else "below",
                                                    price_text(entry_ref)))
                        else:
                            reward_risk = round(int(round(abs(target - entry_ref) * 100)) / float(risk_cents), 2)
                            if min_reward_risk is not None and reward_risk < min_reward_risk - 1e-9:
                                entry_reasons.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                                     "minimum %s" % (reward_risk, price_text(target),
                                                                     price_text(entry_ref), price_text(stop),
                                                                     number_text(min_reward_risk)))
                    elif min_reward_risk:
                        entry_reasons.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                             % number_text(min_reward_risk))
            if daily_loss_cap is not None and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily_loss_cap + HALF_CENT:
                    entry_reasons.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                         "daily loss limit" % (dollars(used), dollars(max_loss),
                                                               dollars(used + max_loss), dollars(daily_loss_cap)))
            if max_trades is not None and len(entries_today) + batch_entries >= max_trades:
                entry_reasons.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                                     "is %d a day" % (len(entries_today), batch_entries, max_trades))
            entry_reasons.extend(window_reasons)
            if settings["pdt_guard"] and day_trade_count >= 3:
                entry_reasons.append("pdt: %d day trades in the last 5 business days (%s); a 4th trips the "
                                     "pattern-day-trader rule your broker applies (\"pdt_guard\": true)"
                                     % (day_trade_count, ", ".join(day_trade_list)))
            if stop and entry_ref and risk_per_share:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (price_text(entry_ref), ENTRY_REF_WORDS.get(entry_ref_from, entry_ref_from),
                               price_text(stop),
                               price_text(target) if target else "none", price_text(risk_per_share)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (dollars(max_loss),
                               (" (limit %s)" % dollars(max_loss_cap)) if max_loss_cap is not None else "",
                               ("%.2f" % reward_risk) if reward_risk is not None else "n/a",
                               (" (min %s)" % number_text(min_reward_risk)) if min_reward_risk is not None else ""))
            if daily_loss_cap is not None or max_trades is not None:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries,
                               max_trades if max_trades is not None else "unlimited",
                               dollars(loss_today + batch_loss),
                               dollars(daily_loss_cap) if daily_loss_cap is not None else "no limit on"))

        # duplicates and the daily order limit
        key = (symbol, side)
        if key in seen:
            entry_reasons.append("duplicate: a second %s %s in this batch" % (side, symbol))
        if key in placed_keys:
            entry_reasons.append("duplicate: a %s %s was already placed today" % (side, symbol))
        passed_so_far = sum(1 for row in results if row["verdict"] == "PASS")
        max_orders = None if settings["max_orders_per_day"] is None else int(settings["max_orders_per_day"])
        if max_orders is not None and count_today + passed_so_far >= max_orders:
            entry_reasons.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                                 "your limit is %d a day" % (count_today, passed_so_far, max_orders))
        seen.add(key)

        if kind == "entry":
            reasons.extend(entry_reasons)
        else:
            warnings.extend("exit, not blocked: " + reason for reason in entry_reasons)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    account["cash"] -= value
                    account["values"][symbol] = account["values"].get(symbol, 0.0) + value
                if share_count is not None:
                    account["shares"][symbol] = held + share_count
                elif entry_ref and value is not None:
                    account["shares"][symbol] = held + value / entry_ref
            elif side == "sell":
                if value is not None:
                    account["cash"] += value
                    account["values"][symbol] = max(account["values"].get(symbol, 0.0) - value, 0.0)
                if share_count is not None:
                    account["shares"][symbol] = held - share_count
                elif entry_ref and value is not None:
                    account["shares"][symbol] = held - value / entry_ref
        results.append({
            "index": number, "symbol": symbol, "side": side, "kind": kind,
            "amount": dollars(finite_number(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_usd": round(value, 2) if value is not None else None,
            "entry_ref": round(entry_ref, 2) if entry_ref else None, "entry_ref_from": entry_ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_per_share, "max_loss": max_loss,
            "reward_risk": reward_risk, "fingerprint": fingerprint(order), "verdict": verdict,
            "reasons": reasons, "warnings": warnings, "info": info,
        })

    return {
        "date": today,
        "now_new_york": now_et.strftime("%Y-%m-%d %H:%M"),
        "now_utc": utc_iso(now_utc),
        "paper": settings["paper"],
        "results": results,
        "pass": sum(1 for row in results if row["verdict"] == "PASS"),
        "block": sum(1 for row in results if row["verdict"] == "BLOCK"),
        "warnings": top_warnings,
        "cash_after": round(account["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2),
                  "day_trades_5d": day_trade_count if settings["pdt_guard"] else None},
    }


def percent_text(percent):
    percent = float(percent)
    return str(int(percent)) if percent == int(percent) else ("%.2f" % percent)


def number_text(number):
    number = float(number)
    return str(int(number)) if number == int(number) else ("%.4f" % number).rstrip("0")


def report(result):
    lines = []
    for warning in result["warnings"]:
        lines.append("WARNING: " + warning)
    if result["warnings"]:
        lines.append("")
    for row in result["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (row["verdict"], row["index"], row["side"], row["symbol"], row["amount"],
                                                row["kind"]))
        for line in row.get("info", []):
            lines.append("          " + line)
        for line in row["reasons"]:
            lines.append("        - " + line)
        for line in row["warnings"]:
            lines.append("        ~ " + line)
    lines.append("")
    lines.append("%d passed, %d blocked at %s New York. Cash after the passed orders: %s (%s account)."
                 % (result["pass"], result["block"], result["now_new_york"], dollars(result["cash_after"]),
                    "paper" if result["paper"] else "LIVE"))
    if result["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    if result.get("clock") == "pinned":
        lines.append("PRACTICE: the clock is pinned (--now or --today), so trader-broker treats these verdicts "
                     "as practice, never as a real PASS.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(prices_doc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(prices_doc, dict):
        return {}, {}
    prices = prices_doc.get("prices", prices_doc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(prices_doc.get("as_of"))
    detail = prices_doc.get("detail") if isinstance(prices_doc.get("detail"), dict) else {}
    for symbol, price in prices.items():
        if finite_number(price) is None:
            continue
        symbol = str(symbol).upper()
        out[symbol] = float(price)
        symbol_detail = detail.get(symbol) if isinstance(detail.get(symbol), dict) else {}
        times[symbol] = parse_utc(symbol_detail.get("time")) or parse_utc(symbol_detail.get("fetched")) or as_of
    return out, times


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    parser.add_argument("--orders", default=home("orders.json"))
    parser.add_argument("--config", default=home("config.json"))
    parser.add_argument("--portfolio", help="your account; if missing, a fresh account funded with the budget "
                                            "(default trader/portfolio.json; a practice run: trader/practice/)")
    parser.add_argument("--prices", default=home("prices.json"))
    parser.add_argument("--state", help="what trader-broker placed today (default trader/state/broker.json)")
    parser.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\" (a practice run)")
    parser.add_argument("--today", help="YYYY-MM-DD (default: today in New York; a practice run)")
    parser.add_argument("--out", help="where the verdicts are recorded (default trader/state/risk.json)")
    parser.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    pinned = bool(args.now or args.today)
    clock = "pinned" if pinned else "real"
    room = ("practice",) if pinned else ()     # a practice run keeps its own account, log and verdicts
    args.portfolio = args.portfolio or home(*room, "portfolio.json")
    args.state = args.state or home(*room, "state", "broker.json")
    args.out = args.out or home(*room, "state", "risk.json")
    try:
        now = parse_now(args.now)
        if args.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", args.today):
            raise RiskError("--today wants a date like 2026-10-19")
        doc = read_json(args.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % args.orders)
        if not all(isinstance(order, dict) for order in orders):
            raise RiskError("%s: every order should be one JSON object, { ... }" % args.orders)
        config = read_json(args.config, "config", required=False)
        portfolio = read_json(args.portfolio, "portfolio", required=False, kind=dict)
        prices_doc = read_json(args.prices, "prices", required=False, kind=dict) or {}
        prices, times = load_prices(prices_doc)
        state = read_json(args.state, "broker state", required=False, kind=dict)
        if state is not None and not (isinstance(state.get("orders", []), list)
                                      and all(isinstance(record, dict) for record in state.get("orders", []))):
            raise RiskError("%s is not trader-broker's log (an \"orders\" list of records)" % args.state)
        result = check(orders, config, portfolio, prices, state if pinned else real_only(state), args.today, now, times)
        result["clock"] = clock
        if state is None and not pinned:
            result["warnings"].append("nothing logged yet at %s: the daily limits count only what trader-broker "
                                      "logs there, so run every skill from this same folder (or set TRADER_HOME)"
                                      % os.path.abspath(args.state))
        if prices_doc.get("as_of"):
            result["prices_as_of"] = prices_doc["as_of"]
        if not args.no_write:
            risk_record = dict(result)
            risk_record["checked_at"] = utc_iso(utc_now())
            risk_record["orders_file"] = args.orders
            write_verdicts(args.out, risk_record)
    except RiskError as error:
        print("check.py: %s" % error, file=sys.stderr)
        if not args.no_write and os.path.exists(args.out):
            # nothing stays READY on rules or orders that no longer check out
            try:
                write_verdicts(args.out, {"error": str(error), "clock": clock, "checked_at": utc_iso(utc_now()),
                                          "results": []})
            except OSError:
                pass
        return 1
    print(json.dumps(result, indent=2) if args.json else report(result))
    return 1 if result["block"] else 0


def write_verdicts(path, risk_record):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(risk_record, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


if __name__ == "__main__":
    sys.exit(main())
