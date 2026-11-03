#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: stop 358.32, target 370.04
    python3 exit.py plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47
    python3 exit.py plan --stop atr:1.5 --offline TSLA.json # ATR needs the session's bars
    python3 exit.py plan --symbol NVDA --entry 190.25 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the cent, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the cent):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (v2
plans longs only).

flat_by (default 15:55, or "flat_by" in trader/config.json): out by then, win
or lose. On a 1:00 p.m. early close it moves up with the close (12:55).
"min_reward_risk" in the config (default 1.0) only warns here; trader-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

USER_AGENT = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:55", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

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


class PlanError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, nth):
    day = datetime.date(year, month, 1)
    day += datetime.timedelta(days=(6 - day.weekday()) % 7)
    return day + datetime.timedelta(weeks=nth - 1)


def eastern_offset(utc_time):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
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
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(timestamp):
    return EPOCH + datetime.timedelta(seconds=int(timestamp))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is New York time."""
    if not text:
        return utc_now()
    text_iso = text.strip()
    if text_iso.upper().endswith(" ET"):
        text_iso = text_iso[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", text_iso):
        text_iso = "%s %s" % (to_eastern(utc_now()).date().isoformat(), text_iso.zfill(5))
    text_iso = text_iso.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text_iso)
    except ValueError:
        raise PlanError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise PlanError("%s wants a New York time like 15:55, not %r" % (what, text))
    return datetime.time(int(match.group(1)), int(match.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not match:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    hour, minute = int(match.group(1)), int(match.group(2))
    if match.group(3):
        if not 1 <= hour <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        hour = hour % 12 + (12 if match.group(3).upper() == "PM" else 0)
    if hour > 23 or minute > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(hour, minute)


def hhmm_text(clock_time):
    return "%02d:%02d" % (clock_time.hour, clock_time.minute)


def ampm_text(clock_time):
    hour12 = clock_time.hour % 12 or 12
    return "%d:%02d %s" % (hour12, clock_time.minute, "AM" if clock_time.hour < 12 else "PM")


def session(day):
    """(open_time, close_time) in New York time, or None if closed all day."""
    if day.weekday() >= 5 or day.isoformat() in NYSE_HOLIDAYS:
        return None
    return OPEN, (EARLY if day.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


# ---------------------------------------------------------------- money math (Decimal, to the cent)

def to_decimal(value, what="a number"):
    try:
        if isinstance(value, float):
            return Decimal(repr(value))
        return Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, value))


def round_to_cent(price):
    """Round half up to the cent."""
    return to_decimal(price).quantize(CENT, rounding=ROUND_HALF_UP)


def round_down_to_cent(price):
    """A long's stop: round down, away from the entry."""
    return to_decimal(price).quantize(CENT, rounding=ROUND_FLOOR)


def dollars(amount):
    return "$%s" % format(to_decimal(amount).quantize(CENT), ",f")


def number_text(number):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    text = format(to_decimal(number).normalize(), "f")
    return text


def ratio_text(value):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    text = "%.2f" % float(value)
    return text[:-1] if text.endswith("0") else text


NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    rule_text = str(text).strip().lower()
    if rule_text in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if rule_text in ("or-low", "or_low", "low"):
        return "or_low", None
    match = re.match(r"^(price|pct|atr):\s*\$?([0-9][0-9,]*\.?[0-9]*)$", rule_text)
    if not match:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, value = match.group(1), to_decimal(match.group(2), "--stop")
    if value <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and value >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, value


def parse_target(text):
    rule_text = str(text).strip()
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", rule_text)
    if match:
        multiple = to_decimal(match.group(1), "--target")
        if multiple <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", multiple
    match = re.match(r"^price:\s*\$?([0-9][0-9,]*\.?[0-9]*)$", rule_text.lower())
    if match:
        return "price", to_decimal(match.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_multiple_label(multiple):
    whole = int(multiple) if multiple == multiple.to_integral_value() else None
    return "%sR (%s times the risk)" % (number_text(multiple), NUMBER_WORDS.get(whole, number_text(multiple)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None):
    """The stop, the target and the risk, in exact cents. Raises PlanError with the reason."""
    entry_price = round_to_cent(entry)
    if entry_price <= 0:
        raise PlanError("the entry price must be above $0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        range_high, range_low = round_to_cent(or_high), round_to_cent(or_low)
        if range_high <= range_low:
            raise PlanError("the opening-range high %s must be above its low %s"
                            % (dollars(range_high), dollars(range_low)))
        if stop_rule == "or_mid":
            raw_stop = (range_high + range_low) / 2
            stop_label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = $%s" % (dollars(range_high), dollars(range_low), number_text(raw_stop))
        else:
            raw_stop, stop_label = range_low, "opening-range low"
    elif stop_rule == "price":
        raw_stop, stop_label = stop_param, "your price"
    elif stop_rule == "pct":
        raw_stop = entry_price * (1 - stop_param / 100)
        stop_label = "%s%% under the entry" % number_text(stop_param)
        detail = "%s x (1 - %s%%) = $%s" % (dollars(entry_price), number_text(stop_param), number_text(raw_stop))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        atr_value = to_decimal(round(atr, 6))
        raw_stop = entry_price - stop_param * atr_value
        stop_label = "%s x ATR(%d) under the entry" % (number_text(stop_param), atr_bars)
        detail = "ATR $%s over the %d bars to the entry%s" % (
            number_text(atr_value.quantize(Decimal("0.0001"))), atr_bars,
                                                              " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = round_down_to_cent(raw_stop)
    if stop != to_decimal(raw_stop):
        detail = (detail + ", rounded down" if detail else "$%s rounded down" % number_text(raw_stop))
    if stop >= entry_price:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (dollars(stop), stop_label, dollars(entry_price)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above $0" % dollars(stop))
    risk = entry_price - stop
    floor_risk = entry_price * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (dollars(risk), dollars(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)),
                           dollars(entry_price)))
    if target_rule == "r":
        target = round_to_cent(entry_price + target_param * risk)
        target_label = r_multiple_label(target_param)
    else:
        target = round_to_cent(target_param)
        target_label = None
    if target <= entry_price:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (dollars(target), dollars(entry_price)))
    reward_risk = ((target - entry_price) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if target_label is None:
        target_label = "your price (%sR)" % ratio_text(reward_risk)
    return {"entry": entry_price, "stop": stop, "stop_label": stop_label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else reward_risk, "target_label": target_label,
            "risk": risk, "reward_risk": reward_risk}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def _open_url(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as error:
                code, body = error.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = error.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    error.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as error:
                last = "could not connect: %s" % (error.reason,)
                break
            except OSError as error:
                last = "network error: %s" % (error,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return file.read()
    with open(path, encoding="utf-8") as file:
        return file.read()


def load_chart(symbol, interval, day, offline=None):
    yahoo_sym = symbol.upper().replace(".", "-")
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, yahoo_sym + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as error:
            raise PlanError("could not read %s: %s" % (path, error))
    else:
        back = (to_eastern(utc_now()).date() - day).days
        period = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (
            urllib.parse.quote(yahoo_sym, safe=""), period, interval))
    try:
        data = json.loads(text)
        results = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % symbol)
    if not results:
        chart_error = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (symbol, chart_error.get("description") or "unknown ticker"))
    result = results[0] or {}
    meta = result.get("meta") or {}
    if str(meta.get("symbol") or yahoo_sym).upper() != yahoo_sym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), symbol))
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def value_at(key, index):
        values = quote.get(key) or []
        return values[index] if index < len(values) else None

    bars = []
    for index, timestamp in enumerate(result.get("timestamp") or []):
        close = value_at("close", index)
        if timestamp is None or close is None:
            continue
        high, low = value_at("high", index), value_at("low", index)
        bars.append({"t": int(timestamp), "h": float(close if high is None else high),
                     "l": float(close if low is None else low),
                     "c": float(close)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None):
    """Closed, on-grid bars of one New York session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    hours = session(day)
    if not hours:
        return []
    open_dt = datetime.datetime.combine(day, hours[0])
    close_dt = datetime.datetime.combine(day, hours[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_eastern(now_utc) if now_utc else None
    out = {}
    for bar in chart["bars"]:
        if bar["t"] % step:
            continue
        eastern = to_eastern(utc_of(bar["t"]))
        if eastern.date() != day or not (open_dt <= eastern < close_dt):
            continue
        if local and eastern + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((eastern - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = bar
    return [out[slot] for slot in sorted(out)]


def atr_of(bars, count=ATR_BARS):
    """Average true range of the last count bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    true_ranges, prev = [], None
    for bar in bars:
        true_range = bar["h"] - bar["l"]
        if prev is not None:
            true_range = max(true_range, abs(bar["h"] - prev), abs(bar["l"] - prev))
        true_ranges.append(true_range)
        prev = bar["c"]
    use = true_ranges[-count:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    """One JSON object from path; anything else is a one-line error."""
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as file:
            doc = json.load(file)
    except ValueError as error:
        raise PlanError("%s is not valid JSON: %s" % (path, error))
    except OSError as error:
        raise PlanError("cannot read %s: %s" % (path, error))
    if not isinstance(doc, dict):
        raise PlanError("%s should be one JSON object, { ... }" % path)
    return doc


def write_json(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def read_config(path):
    config = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for key in DEFAULTS:
            if doc.get(key) is not None:
                config[key] = doc[key]
    return config


def resolve_flat_by(day, explicit, config, entry_time):
    """(HH:MM, note). The config's flat-by moves up with a 1:00 p.m. early close."""
    flat_time = parse_hhmm(explicit or config.get("flat_by") or DEFAULTS["flat_by"],
                           "--flat-by" if explicit else "flat_by")
    note = ""
    hours = session(day) if day else None
    if hours:
        close_dt = datetime.datetime.combine(day, hours[1])
        flat_at = datetime.datetime.combine(day, flat_time)
        if flat_at >= close_dt:
            if explicit:
                raise PlanError("--flat-by %s is at or after the %s close on %s"
                                % (hhmm_text(flat_time), ampm_text(hours[1]), day))
            moved = flat_at - (datetime.datetime.combine(day, CLOSE) - close_dt)
            if moved.time() <= OPEN:
                raise PlanError("flat_by %s in the config is at or after the close" % hhmm_text(flat_time))
            flat_time = moved.time()
            note = "moved up for the 1:00 p.m. early close (%s)" % NYSE_EARLY_CLOSE.get(day.isoformat(), "")
    if entry_time and entry_time >= flat_time:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm_text(entry_time), hhmm_text(flat_time)))
    return hhmm_text(flat_time), note


def gather(args):
    """The entry facts, from trader/entry.json or the flags."""
    if args.symbol and args.from_path:
        raise PlanError("use --from or --symbol, not both")
    if args.side in ("sell", "short"):
        raise PlanError("v2 plans longs only: no stops above a short entry. Plan a buy instead.")
    if args.symbol:
        if args.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (args.or_high is None) != (args.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": args.symbol.upper(), "entry": args.entry,
                "setup": args.setup or ("orb" if args.or_high is not None else "level"),
                "date": args.date, "entry_time": args.time, "entry_bar": args.entry_bar,
                "interval": args.interval or "5m",
                "or_high": args.or_high, "or_low": args.or_low, "or_minutes": args.or_minutes, "source": "flags"}
    else:
        path = args.from_path or home("entry.json")
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v2 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and args.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to "
                            "plan ahead" % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        opening_range = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(),
                "entry": args.entry if args.entry is not None else doc.get("price"),
                "setup": args.setup or doc.get("setup") or "orb", "date": args.date or doc.get("date"),
                "entry_time": args.time or (doc.get("time") if args.entry is None else None),
                "entry_bar": (args.entry_bar if args.entry_bar is not None
                              else doc.get("bar_index") if args.entry is None else None),
                "interval": args.interval or doc.get("interval") or "5m",
                "or_high": args.or_high if args.or_high is not None else opening_range.get("high"),
                "or_low": args.or_low if args.or_low is not None else opening_range.get("low"),
                "or_minutes": opening_range.get("minutes") or args.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(args, now_utc):
    info = gather(args)
    config = read_config(args.config)
    stop_text = args.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(args.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_eastern(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, args.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(args.now) if args.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    levels = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                     atr, atr_n)
    flat_by, flat_note = resolve_flat_by(day, args.flat_by, config, entry_time)
    opening_range = None
    if info["or_high"] is not None and info["or_low"] is not None:
        opening_range = {"high": float(round_to_cent(info["or_high"])), "low": float(round_to_cent(info["or_low"])),
                         "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(levels["entry"]),
            "entry_time": ampm_text(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": opening_range,
            "stop": float(levels["stop"]), "stop_rule": stop_rule, "stop_label": levels["stop_label"],
            "target": float(levels["target"]), "target_r": float(levels["target_r"]),
            "target_label": levels["target_label"],
            "risk_per_share": float(levels["risk"]), "reward_risk": float(levels["reward_risk"]), "flat_by": flat_by,
            "stop_detail": levels["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    min_reward_risk = config.get("min_reward_risk")
    try:
        if min_reward_risk is not None and levels["reward_risk"] < to_decimal(min_reward_risk):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio_text(levels["reward_risk"]), number_text(to_decimal(min_reward_risk))))
    except PlanError:
        pass
    today = to_eastern(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    if day.year not in CALENDAR_YEARS:
        warnings.append("Holiday list covers %d to %d only: check nyse.com for %s (a holiday or a 1:00 p.m. "
                        "close moves the flat-by)." % (CALENDAR_YEARS[0], CALENDAR_YEARS[-1], day.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    entry_price = to_decimal(plan["entry"])
    risk = to_decimal(plan["risk_per_share"])
    risk_pct = (risk / entry_price * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (dollars(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (dollars(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (dollars(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (dollars(plan["risk_per_share"]), risk_pct))
    out.append("  Reward:risk   %s" % ratio_text(plan["reward_risk"]))
    out.append("  Flat by       %s ET, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(args):
    now = parse_now(args.now)
    plan, warnings = build_plan(args, now)
    if args.write:
        write_json(args.write, plan)
    if args.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for warning in warnings:
        print("Note: " + warning)
    if args.write:
        print("Wrote %s." % args.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(args):
    if not os.path.exists(args.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % args.plan)
        return 1
    plan = read_json(args.plan, "plan")
    for key in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(key) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (args.plan, key))
    if args.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_eastern(parse_now(args.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    commands = parser.add_subparsers(dest="cmd")
    command = commands.add_parser("plan", help="set the stop and target")
    command.add_argument("--from", dest="from_path", metavar="PATH",
                         help="entry.json to plan from (default trader/entry.json)")
    command.add_argument("--symbol")
    command.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    command.add_argument("--or-high", type=float)
    command.add_argument("--or-low", type=float)
    command.add_argument("--or-minutes", type=int, default=15)
    command.add_argument("--setup", choices=["orb", "level"])
    command.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    command.add_argument("--time", help="entry time, e.g. 10:15 AM")
    command.add_argument("--entry-bar", type=int)
    command.add_argument("--interval", choices=["5m", "1m"])
    command.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    command.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    command.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    command.add_argument("--flat-by", help="HH:MM New York (default: config flat_by, 15:55)")
    command.add_argument("--config", default=home("config.json"))
    command.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    command.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
    command.add_argument("--write", nargs="?", const=home("plan.json"), metavar="PATH",
                         help="save the plan (default trader/plan.json)")
    command.add_argument("--json", action="store_true")
    command = commands.add_parser("show", help="print the current plan")
    command.add_argument("--plan", default=home("plan.json"))
    command.add_argument("--now")
    command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[args.cmd](args)
    except PlanError as error:
        print("exit.py: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
