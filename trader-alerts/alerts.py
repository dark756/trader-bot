#!/usr/bin/env python3
"""Price alerts and the NYSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NYSE open? next open / close
    python3 alerts.py quote TSLA --write             # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol TSLA --stop 358.32
    python3 alerts.py add --symbol TSLA --target 370.04
    python3 alerts.py add --at 15:55 --label "flat by close" --before-close
    python3 alerts.py add --symbol KO --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-19 15:55" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset tsla-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NYSE opened today (rings once per trading day)
  time            the New York clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  3 hours earlier on a 1:00 p.m. early close, so a 15:55
                  "flat by close" rings at 12:55 on those days

A rule with "date" is only watched on that New York date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day. A rule
check cannot read is skipped and reported; every other rule is still checked,
so one typo never silences the stop alert.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/TSLA (falls back to
          query1 on a 429 or a 5xx; one retry with a pause on a 429; one
          request per ticker, half a second apart; the default)
  stooq   https://stooq.com/q/l/?s=tsla.us&f=sd2t2ohlcv&h&e=csv (Stooq's free
          CSV; it answered 404 on 2026-10-13, kept for when it is back)
  --offline DIR reads DIR/<source>-<TICKER>.json|csv (or .json.gz) instead.

NYSE calendar: the 2026 to 2028 holidays and 1:00 p.m. early closes as
published at https://www.nyse.com/markets/hours-calendars (read 2026-10-13;
2028 from NYSE's release of 2025-12-23). Outside those years the clock says
the holiday list is unknown.

trader/ is the folder you run in, or $TRADER_HOME when that is set.
"""

import argparse
import csv
import datetime
import gzip
import http.client
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

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


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

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
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def trading_day(day):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if day.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(day.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(day):
    """(open_time, close_time) in New York time, or None if closed all day."""
    is_open, _ = trading_day(day)
    if not is_open:
        return None
    return OPEN, (EARLY if day.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def market_status(now_utc):
    eastern = to_eastern(now_utc)
    day = eastern.date()
    ok, reason = trading_day(day)
    status = {"now_new_york": eastern.strftime("%Y-%m-%d %H:%M"), "date": day.isoformat(),
              "weekday": eastern.strftime("%A"), "calendar_known": day.year in CALENDAR_YEARS,
              "open": False, "reason": reason, "early_close": NYSE_EARLY_CLOSE.get(day.isoformat())}
    hours = session(day)
    if hours:
        open_time, close_time = hours
        status["opens"], status["closes"] = open_time.strftime("%H:%M"), close_time.strftime("%H:%M")
        if open_time <= eastern.time() < close_time:
            status["open"] = True
            status["reason"] = "regular session"
        elif eastern.time() < open_time:
            status["reason"] = "before the open"
        else:
            status["reason"] = "after the close"
    # next open
    next_day = day if (hours and eastern.time() < hours[0]) else day + datetime.timedelta(days=1)
    for _ in range(15):
        if session(next_day):
            status["next_open"] = "%s 09:30" % next_day.isoformat()
            break
        next_day += datetime.timedelta(days=1)
    return status


# ---------------------------------------------------------------- quotes

def stooq_url(symbol):
    return "https://stooq.com/q/l/?s=%s.us&f=sd2t2ohlcv&h&e=csv" % (
        urllib.parse.quote(symbol.lower().replace(".", "-"), safe=""))


def yahoo_url(symbol, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (
        host, urllib.parse.quote(symbol.upper().replace(".", "-"), safe=""))


def parse_stooq(text, symbol):
    """Symbol,Date,Time,Open,High,Low,Close,Volume -> quote. N/D means no data."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise AlertError("Stooq returned no rows for %s" % symbol)
    row = {key.strip().lower(): (value or "").strip() for key, value in rows[0].items() if key}
    close = row.get("close", "")
    if not close or close.upper() == "N/D":
        raise AlertError("Stooq has no data for %s (check the ticker)" % symbol)

    def field(key):
        text = row.get(key, "")
        try:
            return float(text)
        except ValueError:
            return None
    return {"symbol": symbol.upper(), "price": float(close), "open": field("open"), "high": field("high"),
            "low": field("low"), "prev_close": None,
            "time": ("%s %s" % (row.get("date", ""), row.get("time", ""))).strip(),
            "source": "stooq"}


def parse_yahoo(text, symbol):
    try:
        data = json.loads(text)
        results = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % symbol)
    if not results:
        chart_error = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (symbol, chart_error.get("description", "unknown ticker")))
    meta = results[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % symbol)
    market_time = meta.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(market_time, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if market_time else "")
    quote = (results[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [price for price in (quote.get("open") or []) if price is not None]
    return {"symbol": symbol.upper(), "price": float(price), "open": opens[0] if opens else None,
            "high": meta.get("regularMarketDayHigh"), "low": meta.get("regularMarketDayLow"),
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"), "time": when,
            "currency": meta.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def http_get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _urlopen(request, timeout=timeout) as response:
            return 200, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                error.close()
            except Exception:
                pass
        return error.code, body
    except urllib.error.URLError as error:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], error.reason))
    except (OSError, http.client.HTTPException) as error:   # a timeout or a reset while reading the answer
        raise AlertError("lost the answer from %s: %s" % (url.split("/")[2], str(error) or type(error).__name__))


def fetch(url, timeout=20):
    code, text = http_get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(symbol, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(symbol, host)
        code, text = http_get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = http_get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def offline_text(folder, source, symbol):
    suffix = "csv" if source == "stooq" else "json"
    base_path = os.path.join(folder, "%s-%s.%s" % (source, symbol, suffix))
    if os.path.exists(base_path):
        with open(base_path, encoding="utf-8") as file:
            return file.read()
    if os.path.exists(base_path + ".gz"):
        with gzip.open(base_path + ".gz", "rt", encoding="utf-8") as file:
            return file.read()
    raise AlertError("no offline quote file %s" % base_path)


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for index, symbol in enumerate(symbols):
        symbol = symbol.upper()
        try:
            if offline:
                text = offline_text(offline, source, symbol)
            else:
                if index and pause:
                    _sleep(pause)
                text = fetch(stooq_url(symbol)) if source == "stooq" else fetch_yahoo(symbol)
            quotes[symbol] = parse_stooq(text, symbol) if source == "stooq" else parse_yahoo(text, symbol)
        except AlertError as error:
            errors.append(str(error))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as file:
                old = json.load(file)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for symbol, quote in quotes.items():
        prices[symbol] = quote["price"]
        detail[symbol] = {"prev_close": quote.get("prev_close"), "time": quote.get("time"),
                          "source": quote.get("source"),
                          "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    write_json(path, doc)
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


def read_json(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except ValueError as error:
        raise AlertError("%s is not valid JSON: %s" % (path, error))
    except OSError as error:
        raise AlertError("cannot read %s: %s" % (path, error))


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def parse_hhmm(text):
    match = HHMM.match(str(text or "").strip())
    if not match:
        return None
    return datetime.time(int(match.group(1)), int(match.group(2)))


def is_number(value):
    """A finite number that is not true / false."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(rule):
    if not isinstance(rule, dict):
        raise AlertError("a rule should be one JSON object, got %s" % json.dumps(rule)[:60])
    rule_type = rule.get("type")
    if rule_type not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), rule_type))
    symbol = rule.get("symbol")
    if rule_type not in NO_QUOTE and not (isinstance(symbol, str) and symbol.strip()):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if symbol is not None and not isinstance(symbol, str):
        raise AlertError("rule %s: symbol must be a ticker in quotes" % rule.get("id"))
    if rule_type in ("above", "below", "stop", "target") and not is_number(rule.get("price")):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if rule_type == "pct_move" and not is_number(rule.get("pct")):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if rule_type == "pct_move" and rule.get("ref") is not None and not (is_number(rule["ref"]) and rule["ref"] > 0):
        raise AlertError("rule %s: ref must be a price" % rule.get("id"))
    if rule_type == "time" and parse_hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a New York time like \"15:55\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def usable(rules):
    """(the rules that check out, a message for each one that does not). One
    bad rule is skipped, never the whole check: the stop alert still rings."""
    good, bad = [], []
    for rule in rules if isinstance(rules, list) else []:
        try:
            validate(rule)
        except AlertError as error:
            bad.append("skipped a rule: %s. The other rules were checked; fix it in alerts.json" % error)
            continue
        good.append(rule)
    return good, bad


def active(rule, today):
    """A rule with a "date" is watched on that New York date only."""
    return not rule.get("date") or str(rule["date"]) == today


def rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this New York date?"""
    if not stamp:
        return False
    try:
        rang_at = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_eastern(rang_at).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today, moved 3 hours earlier on an early close if "before_close"."""
    ring_at = parse_hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        ring_at = (datetime.datetime.combine(datetime.date(2000, 1, 1), ring_at) - datetime.timedelta(hours=3)).time()
    return ring_at


def evaluate(rules, quotes, status, state, now_utc, problems=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.
    A rule it cannot read is skipped; its message goes into problems (a list)."""
    state = json.loads(json.dumps(state if isinstance(state, dict) else {}))
    if not isinstance(state.get("rung"), dict):
        state["rung"] = {}
    rung = state["rung"]
    today = status["date"]
    local = to_eastern(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    good, bad = usable(rules)
    if problems is not None:
        problems.extend(bad)
    for rule in good:
        rule_id = rule.get("id") or "%s-%s" % (rule.get("symbol", "market"), rule["type"])
        if not active(rule, today):
            continue
        rule_type = {"stop": "below", "target": "above"}.get(rule["type"], rule["type"])
        if rule_type == "opening_bell":
            if status["open"] and not rang_today(rung.get(rule_id), today):
                rings.append({"id": rule_id, "symbol": "", "message": "Opening bell: the NYSE is open (%s New York)%s."
                              % (status["now_new_york"],
                                 (", early close at 1:00 p.m. today, " + status["early_close"])
                                 if status.get("early_close") else "")})
                rung[rule_id] = stamp
            continue
        if rule_type == "time":
            ring_at = ring_time(rule, status)
            if status.get("opens") and local.time() >= ring_at and not rang_today(rung.get(rule_id), today):
                label = rule.get("label") or "time alert"
                msg = "%s%s: it is %s New York (set for %s)" % (label[:1].upper(), label[1:],
                                                                 local.strftime("%H:%M"), ring_at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                symbol = str(rule.get("symbol") or "").upper()
                if symbol:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (symbol, symbol))
                rings.append({"id": rule_id, "symbol": symbol, "message": msg})
                rung[rule_id] = stamp
            continue
        symbol = rule["symbol"].upper()
        quote = quotes.get(symbol)
        if not quote:
            continue
        price = quote["price"]
        if rule_type in ("above", "below"):
            hit = price >= rule["price"] if rule_type == "above" else price <= rule["price"]
            if hit and rule_id not in rung:
                word = "at or above" if rule_type == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(rule["type"], "alert level")
                rings.append({"id": rule_id, "symbol": symbol, "message": "%s %s $%.2f: %s your %s $%s%s."
                              % (symbol, "is" if status["open"] else "last", price, word, label,
                                 number_text(rule["price"]),
                                 (" (" + rule["note"] + ")") if rule.get("note") else "")})
                rung[rule_id] = stamp
            elif not hit and rule_id in rung and rule.get("rearm"):
                del rung[rule_id]
        elif rule_type == "pct_move":
            ref_price = rule.get("ref") or quote.get("prev_close") or quote.get("open")
            if not ref_price:
                continue
            move = 100.0 * (price / float(ref_price) - 1)
            want = rule.get("direction")
            hit = abs(move) >= rule["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not rang_today(rung.get(rule_id), today):
                rings.append({"id": rule_id, "symbol": symbol, "message": "%s moved %+.2f%% to $%.2f (from $%.2f)."
                              % (symbol, move, price, float(ref_price))})
                rung[rule_id] = stamp
    state["last_check"] = stamp
    return rings, state


def number_text(number):
    number = float(number)
    return str(int(number)) if number == int(number) else "%.2f" % number


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    symbol = str(plan["symbol"]).upper()
    out = []
    for key, rule_type, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        price = plan.get(key)
        if not isinstance(price, (int, float)) or price <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (symbol.lower(), key), "symbol": symbol, "type": rule_type,
                    "price": float(price),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "15:55"
    if parse_hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:55" % flat)
    out.append({"id": "%s-flat-by" % symbol.lower(), "symbol": symbol, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    if plan.get("date"):
        for rule in out:
            rule["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(args):
    syms = []
    portfolio = read_json(args.portfolio, {})
    rows = portfolio.get("positions") if isinstance(portfolio, dict) else None
    for position in rows if isinstance(rows, list) else []:
        if isinstance(position, dict):
            syms.append(str(position.get("ticker") or position.get("symbol") or "").upper())
    plan = read_json(args.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [symbol for symbol in syms if symbol]


def dedupe(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def cmd_clock(args):
    status = market_status(parse_now(args.now))
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print("New York %s (%s): NYSE %s, %s." % (status["now_new_york"], status["weekday"],
                                                  "OPEN" if status["open"] else "closed", status["reason"]))
        if status.get("opens"):
            print("Today's session: %s to %s%s." % (status["opens"], status["closes"],
                                                    (" (early close: %s)" % status["early_close"])
                                                    if status.get("early_close") else ""))
        if not status["open"] and status.get("next_open"):
            print("Next open: %s New York." % status["next_open"])
        if not status["calendar_known"]:
            print("Holiday list covers %d to %d only: check nyse.com for this date."
                  % (CALENDAR_YEARS[0], CALENDAR_YEARS[-1]))
    return 0 if status["open"] else 1


def cmd_quote(args):
    syms = dedupe([symbol.upper() for symbol in args.symbols] + (holdings(args) if args.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote TSLA KO, or --holdings")
    now = parse_now(args.now)
    quotes, errors = get_quotes(syms, args.source, args.offline)
    if args.write and quotes:
        write_prices(args.write, quotes, now, args.source)
    if args.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for symbol in syms:
            quote = quotes.get(symbol)
            if quote:
                change = ""
                if quote.get("prev_close"):
                    change = " (%+.2f%% vs prev close $%.2f)" % (100.0 * (quote["price"] / quote["prev_close"] - 1),
                                                                 quote["prev_close"])
                print("%-6s $%.2f%s  %s  [%s]" % (symbol, quote["price"], change, quote.get("time", ""),
                                                  quote["source"]))
        for error in errors:
            print("error: " + error)
        if args.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), args.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(args):
    doc = read_json(args.rules, None)
    if not isinstance(doc, dict) or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % args.rules)
    if not isinstance(doc["rules"], list):
        raise AlertError("%s: \"rules\" should be a list of rules" % args.rules)
    rules, bad = usable(doc["rules"])
    now = parse_now(args.now)
    status = market_status(now)
    watched = [rule for rule in rules if active(rule, status["date"])]
    syms = dedupe([str(rule.get("symbol", "")).upper() for rule in watched if rule.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, args.source, args.offline) if syms else ({}, []))
    try:
        state = read_json(args.state, {})
    except AlertError as error:      # a broken memory of what rang: start again rather than ring nothing
        state = {}
        bad.append("%s; started a fresh one, so an alert that rang today can ring again" % error)
    if not isinstance(state, dict):
        state = {}
    rings, state = evaluate(rules, quotes, status, state, now)
    errors = bad + errors
    if not args.no_write:
        write_json(args.state, state)
        if quotes:
            write_prices(args.prices_out, quotes, now, args.source)
    if args.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {symbol: quote["price"] for symbol, quote in quotes.items()}}, indent=2))
    else:
        print("NYSE %s (%s New York, %s)." % ("OPEN" if status["open"] else "closed", status["now_new_york"],
                                              status["reason"]))
        for ring in rings:
            print("ALERT " + ring["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s $%.2f" % (symbol, quotes[symbol]["price"])
                                                for symbol in syms if symbol in quotes))
                if quotes else ""))
        for error in errors:
            print("error: " + error)
        if rings:
            print("An alert is information, not an order. A bracket's stop-loss and take-profit already sit at "
                  "the broker: check get_open_position and record any fill (ticket.py fill). To close a "
                  "position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def read_rules_doc(path):
    """alerts.json as {"rules": [...]}; anything else there is an error."""
    doc = read_json(path, {"rules": []})
    if not isinstance(doc, dict) or not isinstance(doc.setdefault("rules", []), list):
        raise AlertError("%s should be {\"rules\": [ ... ]}" % path)
    return doc


def read_alert_state(path):
    """What rang so far, {"rung": {...}} (check starts a fresh one when it is broken)."""
    state = read_json(path, {})
    if not isinstance(state, dict) or not isinstance(state.setdefault("rung", {}), dict):
        raise AlertError("%s should be {\"rung\": { ... }}" % path)
    return state


def cmd_add(args):
    doc = read_rules_doc(args.rules)
    if args.from_plan:
        plan = read_json(args.plan, None)
        if not isinstance(plan, dict):
            raise AlertError("no plan at %s (run trader-exit first)" % args.plan)
        config = read_json(args.config, {}) if args.config else {}
        new = rules_from_plan(plan, (config if isinstance(config, dict) else {}).get("flat_by"))
        ids = {added["id"] for added in new}
        doc["rules"] = [existing for existing in doc["rules"]
                        if not (isinstance(existing, dict) and existing.get("id") in ids)] + new
        state = read_alert_state(args.state)
        if state.get("rung"):
            for rule_id in ids:
                state["rung"].pop(rule_id, None)
            write_json(args.state, state)
        write_json(args.rules, doc)
        for added in new:
            print("Added %s: %s" % (added["id"], json.dumps(added)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the broker's bracket orders are what actually stop you out.")
        return 0
    if args.opening_bell:
        rule = {"id": args.id or "opening-bell", "type": "opening_bell"}
    elif args.at:
        if parse_hhmm(args.at) is None:
            raise AlertError("--at wants a New York time like 15:55")
        rule = {"id": args.id or ("time-%s" % args.at.replace(":", "")), "type": "time", "at": args.at,
                "label": args.label or "time alert"}
        if args.symbol:
            rule["symbol"] = args.symbol.upper()
        if args.before_close:
            rule["before_close"] = True
    else:
        if not args.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        symbol = args.symbol.upper()
        if args.above is not None:
            rule = {"type": "above", "price": args.above}
        elif args.below is not None:
            rule = {"type": "below", "price": args.below}
        elif args.stop is not None:
            rule = {"type": "stop", "price": args.stop}
        elif args.target is not None:
            rule = {"type": "target", "price": args.target}
        elif args.pct is not None:
            rule = {"type": "pct_move", "pct": args.pct}
            if args.direction:
                rule["direction"] = args.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        value = rule.get("price", rule.get("pct"))
        rule = dict({"id": args.id or ("%s-%s-%s" % (symbol.lower(), rule["type"], number_text(value)))}, symbol=symbol,
                    **rule)
        if args.rearm:
            rule["rearm"] = True
        if args.note:
            rule["note"] = args.note
    if args.date:
        rule["date"] = args.date
    validate(rule)
    if any(isinstance(existing, dict) and existing.get("id") == rule["id"] for existing in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    write_json(args.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(args):
    doc = read_rules_doc(args.rules)
    state = read_alert_state(args.state)
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    _, bad = usable(doc["rules"])
    for rule in doc["rules"]:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id")
        rung = state["rung"].get(rule_id)
        print("%-24s %s%s" % (rule_id, json.dumps({key: value for key, value in rule.items() if key != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    for problem in bad:
        print("error: " + problem)
    return 0


def cmd_reset(args):
    state = read_alert_state(args.state)
    if args.id not in state["rung"]:
        print("%s has not rung (nothing to reset)." % args.id)
        return 1
    del state["rung"][args.id]
    write_json(args.state, state)
    print("Re-armed %s." % args.id)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Price alerts and the NYSE clock (trader-alerts).")
    commands = parser.add_subparsers(dest="cmd")

    def net(command):
        command.add_argument("--source", default="yahoo", choices=["yahoo", "stooq"])
        command.add_argument("--offline", metavar="DIR", help="read <source>-<TICKER>.json|csv files, no network")
        command.add_argument("--now", help="pretend it is this time (no offset = New York time)")
        command.add_argument("--json", action="store_true")

    command = commands.add_parser("clock", help="is the NYSE open?")
    command.add_argument("--now")
    command.add_argument("--json", action="store_true")
    command = commands.add_parser("quote", help="latest delayed quotes")
    net(command)
    command.add_argument("symbols", nargs="*")
    command.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    command.add_argument("--portfolio", default=home("portfolio.json"))
    command.add_argument("--plan", default=home("plan.json"))
    command.add_argument("--write", nargs="?", const=home("prices.json"), metavar="PATH",
                         help="save to prices.json (default trader/prices.json)")
    command = commands.add_parser("check", help="check every rule, ring what hit")
    net(command)
    command.add_argument("--rules", default=home("alerts.json"))
    command.add_argument("--state", default=home("state", "alerts.json"))
    command.add_argument("--prices-out", default=home("prices.json"))
    command.add_argument("--no-write", action="store_true")
    command = commands.add_parser("add", help="add a rule")
    command.add_argument("--rules", default=home("alerts.json"))
    command.add_argument("--state", default=home("state", "alerts.json"))
    command.add_argument("--symbol")
    group = command.add_mutually_exclusive_group()
    group.add_argument("--above", type=float)
    group.add_argument("--below", type=float)
    group.add_argument("--stop", type=float)
    group.add_argument("--target", type=float)
    group.add_argument("--pct", type=float)
    group.add_argument("--opening-bell", action="store_true")
    group.add_argument("--at", metavar="HH:MM", help="a time alert, New York time")
    group.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    command.add_argument("--plan", default=home("plan.json"))
    command.add_argument("--config", default=home("config.json"), help="flat_by when the plan has none")
    command.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    command.add_argument("--before-close", action="store_true",
                         help="a time alert that moves 3 hours earlier on a 1:00 p.m. early close")
    command.add_argument("--date", help="watch this rule on this New York date only (YYYY-MM-DD)")
    command.add_argument("--direction", choices=["up", "down"])
    command.add_argument("--rearm", action="store_true")
    command.add_argument("--note")
    command.add_argument("--id")
    command = commands.add_parser("list", help="list the rules")
    command.add_argument("--rules", default=home("alerts.json"))
    command.add_argument("--state", default=home("state", "alerts.json"))
    command = commands.add_parser("reset", help="re-arm a rule that rang")
    command.add_argument("id")
    command.add_argument("--state", default=home("state", "alerts.json"))
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[args.cmd](args)
    except AlertError as error:
        print("alerts.py: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
