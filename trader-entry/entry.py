#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check TSLA                          # opening-range breakout, 5-minute bars
    python3 entry.py check TSLA --now "2026-10-19 10:15" --offline TSLA.json
    python3 entry.py check TSLA --setup level --level 371.20    # first close above a price
    python3 entry.py check TSLA --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch TSLA --every 30 --until 15:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (v2 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:30, 9:35 and 9:40 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 9:30 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 10:10 bar closes at
10:15 AM) and its price is that close, to the cent. Only bars on the interval
grid count (Yahoo's live tick, e.g. 10:49:45, is dropped) and a bar counts only
once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 15:30;
on a 1:00 p.m. early close the cutoff moves up three hours with the close);
no-data (the market is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:45) and
"no_entries_after" (15:30), New York time.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<TICKER>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
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

USER_AGENT = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30"}
EPOCH = datetime.datetime(1970, 1, 1)

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


class EntryError(Exception):
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
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(timestamp):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(timestamp))


def ts_of(utc_time):
    return int((utc_time.replace(tzinfo=None) - EPOCH).total_seconds())


def utc_iso(utc_time):
    return utc_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is New York time; an offset or Z makes it absolute."""
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
        raise EntryError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise EntryError("%s wants a New York time like 15:30, not %r" % (what, text))
    return datetime.time(int(match.group(1)), int(match.group(2)))


def hhmm_text(clock_time):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (clock_time.hour, clock_time.minute)


def clock_text(clock_time):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (clock_time.hour, clock_time.minute)


def ampm_text(clock_time):
    hour12 = clock_time.hour % 12 or 12
    return "%d:%02d %s" % (hour12, clock_time.minute, "AM" if clock_time.hour < 12 else "PM")


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


def next_open(day):
    next_day = day + datetime.timedelta(days=1)
    for _ in range(15):
        if session(next_day):
            return "%s 09:30" % next_day.isoformat()
        next_day += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(symbol):
    return symbol.upper().replace(".", "-")


def chart_path(symbol, period, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(yahoo_symbol(symbol), safe=""), period, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
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
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as error:
                last = "could not connect: %s" % (error.reason,)
                break
            except OSError as error:
                last = "network error: %s" % (error,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, symbol):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        results = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % symbol)
    if not results:
        chart_error = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (symbol, chart_error.get("description") or "unknown ticker"))
    result = results[0] or {}
    meta = result.get("meta") or {}
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def value_at(key, index):
        values = quote.get(key) or []
        return values[index] if index < len(values) else None

    bars = []
    for index, timestamp in enumerate(result.get("timestamp") or []):
        close = value_at("close", index)
        if timestamp is None or close is None:
            continue
        open_price, high, low = value_at("open", index), value_at("high", index), value_at("low", index)
        bars.append({"t": int(timestamp), "o": float(close if open_price is None else open_price),
                     "h": float(close if high is None else high),
                     "l": float(close if low is None else low), "c": float(close),
                     "v": int(value_at("volume", index) or 0)})
    return {"symbol": str(meta.get("symbol") or symbol).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return file.read()
    with open(path, encoding="utf-8") as file:
        return file.read()


def offline_file(path, symbol):
    if os.path.isdir(path):
        for name in (symbol + ".json", symbol + ".json.gz", yahoo_symbol(symbol) + ".json",
                     yahoo_symbol(symbol) + ".json.gz"):
            candidate = os.path.join(path, name)
            if os.path.exists(candidate):
                return candidate
        raise EntryError("no offline file for %s in %s (want %s.json)" % (symbol, path, symbol))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(symbol, interval, day=None, offline=None, today=None):
    """Bars for one ticker, from Yahoo (range=1d for today) or a saved file."""
    symbol = symbol.upper()
    if offline:
        file_path = offline_file(offline, symbol)
        try:
            text = read_text(file_path)
        except (OSError, ValueError) as error:
            raise EntryError("could not read %s: %s" % (file_path, error))
    else:
        today = today or to_eastern(utc_now()).date()
        text = yahoo_get(chart_path(symbol, chart_range(day or today, today, interval), interval))
    chart = parse_chart(text, symbol)
    if chart["symbol"] != yahoo_symbol(symbol):
        raise EntryError("the chart file holds %s, not %s" % (chart["symbol"], symbol))
    gran = chart["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (symbol, gran, interval, gran))
    return chart


# ---------------------------------------------------------------- the rule

def read_config(path):
    config = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as file:
                doc = json.load(file)
        except ValueError as error:
            raise EntryError("%s is not valid JSON: %s" % (path, error))
        except OSError as error:
            raise EntryError("cannot read %s: %s" % (path, error))
        if not isinstance(doc, dict):
            raise EntryError("%s should be one JSON object, { ... }" % path)
        for key in DEFAULTS:
            if doc.get(key):
                config[key] = doc[key]
    for key in DEFAULTS:
        parse_hhmm(config[key], key)
    return config


def round_price(value):
    return None if value is None else round(float(value), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def early_cutoff(day, cutoff, close_dt):
    """A cutoff on an early-close day moves up with the close: 15:30 (30 minutes
    before a 16:00 close) becomes 12:30 before a 1:00 p.m. close. A cutoff that
    is already before the early close stays."""
    cutoff_at = datetime.datetime.combine(day, cutoff)
    if cutoff_at < close_dt:
        return cutoff_at
    moved = cutoff_at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved if moved.time() > OPEN else close_dt


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, config=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    config = config or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    range_bar_count = or_minutes * 60 // step
    local = to_eastern(now_utc)
    day = local.date()
    result = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": day.isoformat(),
              "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"),
              "status": "waiting",
              "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
              "last": None, "forming": None, "min_volume_x": min_volume_x,
              "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS, "_bars": []}
    data_time = chart.get("data_time")
    result["data_time"] = to_eastern(utc_of(data_time)).strftime("%Y-%m-%d %H:%M:%S") if data_time else None
    ok, why = trading_day(day)
    if not ok:
        result.update(status="no-data", phase="closed", reason="the NYSE is closed today (%s)" % why,
                      next_open=next_open(day))
        return result
    open_time, close_time = session(day)
    open_dt = datetime.datetime.combine(day, open_time)
    close_dt = datetime.datetime.combine(day, close_time)
    early = close_time != CLOSE
    before = datetime.datetime.combine(day, parse_hhmm(config["no_entries_before"], "no_entries_before"))
    cutoff = early_cutoff(day, parse_hhmm(config["no_entries_after"], "no_entries_after"), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    result.update(opens=hhmm_text(open_time), closes=hhmm_text(close_time),
                  early_close=NYSE_EARLY_CLOSE.get(day.isoformat()),
                  no_entries_before=hhmm_text(before), no_entries_after=hhmm_text(cutoff))

    by_slot = {}
    for bar in chart["bars"]:
        if bar["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        eastern = to_eastern(utc_of(bar["t"]))
        if eastern.date() != day or not (open_dt <= eastern < close_dt):
            continue
        slot = int((eastern - open_dt).total_seconds()) // step
        by_slot[slot] = dict(bar, et=eastern, end=eastern + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        bar = by_slot[slot]
        end_ts = bar["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if data_time is None or data_time >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(bar)
            else:
                pending = bar
                result["forming"] = {"bar_start": hhmm_text(bar["et"]), "closes": ampm_text(bar["end"]),
                                     "price": round_price(bar["c"])}
                result["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                      "catches up (a minute at most)"
                                      % (to_eastern(utc_of(data_time)).strftime("%H:%M:%S"), clock_text(bar["et"])))
        elif bar["et"] <= local:
            result["forming"] = {"bar_start": hhmm_text(bar["et"]), "closes": ampm_text(bar["end"]),
                                 "price": round_price(bar["c"])}
    result["_bars"] = bars
    result["bars_closed"] = len(bars)

    if local < open_dt:
        result.update(phase="pre-open", reason="the NYSE opens at 9:30%s" % (
               " and the %d-minute range is set at %s" % (or_minutes, clock_text(or_end)) if setup == "orb" else ""))
        return result

    or_bars = [bar for bar in bars if bar["slot"] < range_bar_count]
    avg_volume = (sum(bar["v"] for bar in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        result["opening_range"] = {"high": round_price(max(bar["h"] for bar in or_bars)),
                                   "low": round_price(min(bar["l"] for bar in or_bars)),
                                   "minutes": or_minutes, "bars": len(or_bars), "of": range_bar_count,
                                   "complete": local >= or_end, "avg_volume": int(round(avg_volume))}
    if setup == "orb":
        if local < or_end:
            result.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                   len(or_bars), range_bar_count, clock_text(or_end)))
            return result
        if not or_bars:
            result.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return result
        trigger_level = max(bar["h"] for bar in or_bars)
    else:
        trigger_level = float(level)
    result["level"] = round_price(trigger_level)

    first_slot = range_bar_count if setup == "orb" else 0
    trigger, thin = None, []
    for bar in bars:
        if bar["slot"] < first_slot or bar["end"] < before or bar["end"] > cutoff:
            continue
        if bar["c"] > trigger_level:
            volume_x = (bar["v"] / avg_volume) if avg_volume else None
            if min_volume_x and (volume_x is None or volume_x < min_volume_x):
                thin.append(bar)
                continue
            trigger = bar
            break
    last = bars[-1] if bars else None
    if last:
        result["last"] = {"bar_start": hhmm_text(last["et"]), "close": round_price(last["c"]),
                          "vs_level_pct": round_price(100.0 * (last["c"] / trigger_level - 1))}

    if trigger:
        volume_x = (trigger["v"] / avg_volume) if avg_volume else None
        result.update(status="triggered", phase="open" if local < close_dt else "after-close",
                      reason="the %s bar closed above %s" % (clock_text(trigger["et"]),
                                                             "the range high" if setup == "orb" else "the level"))
        result["trigger"] = {"time": ampm_text(trigger["end"]), "close_time": hhmm_text(trigger["end"]),
                             "bar_start": hhmm_text(trigger["et"]),
                             "bar_index": trigger["slot"], "price": round_price(trigger["c"]), "close": trigger["c"],
                             "volume": trigger["v"], "volume_x": round_price(volume_x),
                             "pct_over": round_price(100.0 * (trigger["c"] / trigger_level - 1))}
        result["minutes_since"] = int((local - trigger["end"]).total_seconds() // 60)
        if data_time and utc_of(data_time) < eastern_to_utc(trigger["end"]):
            result["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a "
                                   "cent or two" % to_eastern(utc_of(data_time)).strftime("%H:%M:%S"))
        return result
    if not bars:
        result.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return result
    what = "close above the range high $%.2f" % trigger_level if setup == "orb" else "close above $%.2f" % trigger_level
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        result.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                      reason="no %s by %s, the last time for a new entry%s" % (
                       what, clock_text(cutoff), " (1:00 p.m. early close today)" if early else ""))
        return result
    result["phase"] = "open"
    if last["c"] > trigger_level and last in thin:
        result["reason"] = "the %s bar closed above at $%.2f but on %.2fx the range's volume, under your %.2fx" % (
               clock_text(last["et"]), last["c"], last["v"] / avg_volume if avg_volume else 0, min_volume_x)
    elif last["c"] > trigger_level and last["end"] < before:
        result["reason"] = "the %s bar closed above at $%.2f, but no entries before %s (config)" % (
               clock_text(last["et"]), last["c"], clock_text(before))
    else:
        gap = trigger_level - last["c"]
        result["reason"] = "last close $%.2f (%s bar), $%.2f %s %s (%+.2f%%)" % (
               last["c"], clock_text(last["et"]), abs(gap), "under" if gap >= 0 else "over",
               "the range high $%.2f" % trigger_level if setup == "orb" else "the level $%.2f" % trigger_level,
               100.0 * (last["c"] / trigger_level - 1))
    return result


def public(result):
    """The result without the internal bar list, ready for JSON."""
    return {key: value for key, value in result.items() if not key.startswith("_")}


def entry_doc(result, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    trigger = result.get("trigger") or {}
    opening_range = result.get("opening_range")
    return {"created_by": "trader-entry", "symbol": result["symbol"], "setup": result["setup"], "side": "buy",
            "date": result["date"], "status": result["status"], "time": trigger.get("time"),
            "bar_start": trigger.get("bar_start"),
            "bar_index": trigger.get("bar_index"), "price": trigger.get("price"), "level": result.get("level"),
            "interval": result["interval"],
            "opening_range": {"high": opening_range["high"], "low": opening_range["low"],
                              "minutes": opening_range["minutes"]} if opening_range else None,
            "source": "yahoo", "checked_at": utc_iso(now_utc), "reason": result.get("reason")}


def write_json(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------- words

STATUS_LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
PHASE_SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(result):
    if result["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % result["or_minutes"]
    return "close above $%.2f" % result["level"] if result["level"] is not None else "close above a level"


def describe(result):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(result["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if result["interval"] == "5m" else "1-minute"
    head = "%s  %s %s ET  %s on %s bars (Yahoo, free, can lag)" % (
        result["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(result), bars)
    if result.get("data_time") and result["data_time"] <= result["now"] and result["data_time"][:10] == result["date"]:
        head += ", data to %s" % result["data_time"][11:]
    out = [head]
    opening_range = result.get("opening_range")
    if opening_range:
        start = datetime.datetime.strptime(result["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=opening_range["minutes"])
        out.append("Opening range %s-%s: high $%.2f, low $%.2f (%d of %d bars%s)." % (
            clock_text(start), clock_text(end), opening_range["high"], opening_range["low"], opening_range["bars"],
            opening_range["of"], "" if opening_range["complete"] else ", still forming"))
    label = PHASE_LABEL.get(result["phase"]) or STATUS_LABEL.get(result["status"], result["status"].upper())
    trigger = result.get("trigger")
    if trigger:
        out.append("TRIGGERED %s at $%.2f: the %s bar (bar %d) closed above %s $%.2f (%+.2f%%%s)." % (
            trigger["time"], trigger["price"], clock_text(datetime.datetime.strptime(trigger["bar_start"], "%H:%M")),
            trigger["bar_index"], "the range high" if result["setup"] == "orb" else "the level", result["level"],
            trigger["pct_over"],
            (", %.2fx the range's volume" % trigger["volume_x"])
            if result.get("min_volume_x") and trigger.get("volume_x") else ""))
        step = INTERVALS[result["interval"]] // 60
        if result.get("minutes_since", 0) > step and result.get("last"):
            out.append("That was %d minutes ago; the last close is $%.2f (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           result["minutes_since"], result["last"]["close"], result["last"]["bar_start"]))
        if result.get("data_note"):
            out.append(result["data_note"] + ".")
        if result.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, result["reason"]))
        forming = result.get("forming")
        if result.get("lag_note") and result["status"] == "waiting":
            out.append(result["lag_note"] + ".")
        elif forming and result["status"] == "waiting" and result["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming ($%.2f so far, closes %s); it does not count until it closes." % (
                forming["bar_start"].lstrip("0"), forming["price"], forming["closes"]))
        if result["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if result.get("next_open"):
            out.append("Next open: %s New York." % result["next_open"])
    if result.get("early_close"):
        out.append("Early close today at 1:00 p.m. (%s): no new entries after %s." % (
            result["early_close"], result["no_entries_after"]))
    if not result.get("calendar_known"):
        out.append("Holiday list covers %d to %d only: check nyse.com for this date."
                   % (CALENDAR_YEARS[0], CALENDAR_YEARS[-1]))
    return out


def bar_line(bar, result):
    """One watch line for a bar that just closed."""
    start = clock_text(bar["et"])
    opening_range = result.get("opening_range") or {}
    trigger_level = result.get("level")
    trigger = result.get("trigger")
    range_bar_count = opening_range.get("of") or 0
    if result["setup"] == "orb" and bar["slot"] < range_bar_count:
        seen = [seen_bar for seen_bar in result["_bars"] if seen_bar["slot"] <= bar["slot"]]
        note = "range so far $%.2f to $%.2f" % (min(seen_bar["l"] for seen_bar in seen),
                                                max(seen_bar["h"] for seen_bar in seen))
        if bar["slot"] == range_bar_count - 1:
            note = "opening range set: high $%.2f, low $%.2f" % (opening_range["high"], opening_range["low"])
    elif trigger and trigger["bar_index"] == bar["slot"]:
        note = "TRIGGERED: closed above $%.2f (%+.2f%%). Get in now if you take it: %s, $%.2f" % (
            trigger_level, trigger["pct_over"], trigger["time"], trigger["price"])
    elif trigger_level is None:
        note = ""
    elif bar["c"] > trigger_level:
        note = "above $%.2f" % trigger_level
        if result["setup"] == "level" or bar["slot"] >= range_bar_count:
            avg_volume = opening_range.get("avg_volume")
            if result.get("min_volume_x") and avg_volume:
                note += ", but volume %.2fx is under %.2fx" % (bar["v"] / float(avg_volume), result["min_volume_x"])
            elif bar["end"] < datetime.datetime.combine(bar["et"].date(), parse_hhmm(result["no_entries_before"], "x")):
                note += ", but no entries before %s" % result["no_entries_before"]
    else:
        note = "$%.2f under $%.2f" % (trigger_level - bar["c"], trigger_level)
    return "%5s bar  closed $%.2f  %s" % (start, bar["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_eastern(now)
        day = local.date()
        hours = session(day)
        if not hours:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, hours[0])
        close_dt = datetime.datetime.combine(day, hours[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s ET  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    clock_text(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = eastern_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as error:
            errors += 1
            emit("%s ET  could not read bars: %s" % (local.strftime("%H:%M:%S"), error))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        result = evaluate(chart, now, **opts)
        bars = result["_bars"]
        lines = describe(result)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not result.get("trigger"):
                emit(bar_line(bars[-1], result))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for bar in bars:
                if bar["slot"] > printed:
                    emit(bar_line(bar, result))
                    printed = bar["slot"]
        if result["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(result, now)
            return 0, result
        if result["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (result["reason"] or "the market closed without a trigger"))
            return 1, result
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (clock_text(until), result["reason"]))
            return 1, result
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            next_check = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (next_check - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(result.get("no_entries_after") or "15:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def evaluate_options(args, config):
    if args.side in ("sell", "short"):
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if args.setup == "orb" and args.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if args.min_volume_x is not None and args.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if args.level is not None and args.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": args.setup, "or_minutes": args.or_minutes, "interval": args.interval, "level": args.level,
            "min_volume_x": args.min_volume_x, "config": config}


def load_chart(args, symbol, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_eastern(utc_now()).date()
    if args.offline and now is None:
        chart = get_chart(symbol, args.interval, offline=args.offline)
        return chart, (utc_of(chart["data_time"]) if chart.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(symbol, args.interval, to_eastern(now).date(), args.offline, real_today), now


def cmd_check(args):
    config = read_config(args.config)
    opts = evaluate_options(args, config)
    symbol = args.symbol.upper()
    chart, now = load_chart(args, symbol, parse_now(args.now) if args.now else None)
    result = evaluate(chart, now, **opts)
    if args.write:
        write_json(args.write, entry_doc(result, now))
    if args.json:
        print(json.dumps(public(result), indent=2))
    else:
        for line in describe(result):
            print(line)
        if args.write:
            print("Wrote %s (status %s)." % (args.write, result["status"]))
    return 0 if result["status"] == "triggered" else 1


def cmd_watch(args):
    config = read_config(args.config)
    opts = evaluate_options(args, config)
    symbol = args.symbol.upper()
    if args.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(args.now) if args.now else None
    if args.offline or start is not None:
        if start is None:        # an offline file without --now: replay its whole day from the open
            day = to_eastern(load_chart(args, symbol, None)[1]).date()
            start = eastern_to_utc(datetime.datetime.combine(day, OPEN))
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_eastern(utc_now()).date()

    def provider(now):
        return get_chart(symbol, args.interval, to_eastern(now).date(), args.offline, real_today)

    until = None
    if args.until:
        until_time = parse_hhmm(args.until, "--until")
        until = datetime.datetime.combine(to_eastern(now_fn()).date(), until_time)

    def on_trigger(result, now):
        write_json(args.out, entry_doc(result, now))
        print("Wrote %s." % args.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=args.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(args):
    config = read_config(args.config)
    opts = evaluate_options(args, config)
    if not os.path.exists(args.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % args.watchlist)
    try:
        with open(args.watchlist, encoding="utf-8") as file:
            doc = json.load(file)
    except ValueError as error:
        raise EntryError("%s is not valid JSON: %s" % (args.watchlist, error))
    rows = doc.get("hits") if isinstance(doc, dict) else None
    hits = [hit for hit in (rows if isinstance(rows, list) else [])
            if isinstance(hit, dict) and isinstance(hit.get("symbol"), str) and hit["symbol"].strip()]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % args.watchlist)
    if args.offline and not os.path.isdir(args.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(args.now) if args.now else None
    rows = []
    for index, hit in enumerate(hits):
        symbol = str(hit["symbol"]).upper()
        options = dict(opts)
        if args.setup == "level" and options["level"] is None:
            options["level"] = hit.get("level")
        try:
            if args.setup == "level" and options["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if index and not args.offline:
                _sleep(PAUSE)
            chart, when = load_chart(args, symbol, now)
            rows.append((symbol, evaluate(chart, when, **options), None))
        except EntryError as error:
            rows.append((symbol, None, str(error)))
    if args.json:
        print(json.dumps([{"symbol": row_symbol, "result": public(result) if result else None,
                           "error": error} for row_symbol, result, error in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST",
                                                          "VS LEVEL"))
        for row_symbol, result, error in rows:
            if error:
                print("%-6s  error: %s" % (row_symbol, error))
                continue
            trigger = result.get("trigger") or {}
            last = result.get("last") or {}
            status = (PHASE_SHORT.get(result["phase"], result["status"]) if result["status"] != "triggered"
                      else "triggered")
            print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % (
                row_symbol, status[:9], trigger.get("time") or "-", ("$%.2f" % trigger["price"]) if trigger else "-",
                ("$%.2f" % result["level"]) if result.get("level") is not None else "-",
                ("$%.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        triggered = sum(1 for _, result, _ in rows if result and result["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit."
              % (triggered, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(result and result["status"] == "triggered" for _, result, _ in rows) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    commands = parser.add_subparsers(dest="cmd")

    def common(command):
        command.add_argument("--setup", choices=["orb", "level"], default="orb")
        command.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        command.add_argument("--interval", choices=["5m", "1m"], default="5m")
        command.add_argument("--level", type=float, help="the price to close above (--setup level)")
        command.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        command.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
        command.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
        command.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        command.add_argument("--config", default=home("config.json"))

    command = commands.add_parser("check", help="the setup's status right now")
    command.add_argument("symbol")
    common(command)
    command.add_argument("--write", nargs="?", const=home("entry.json"), metavar="PATH",
                         help="save the result (default trader/entry.json)")
    command.add_argument("--json", action="store_true")
    command = commands.add_parser("watch", help="poll bar by bar until it triggers or expires")
    command.add_argument("symbol")
    common(command)
    command.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    command.add_argument("--until", help="stop at this New York time (HH:MM)")
    command.add_argument("--out", default=home("entry.json"), help="where a trigger is written")
    command = commands.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(command)
    command.add_argument("--watchlist", default=home("watchlist.json"))
    command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[args.cmd](args)
    except EntryError as error:
        print("entry.py: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
