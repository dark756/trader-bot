#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # Nasdaq Trader lists -> trader/universe.txt
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols TSLA,NVDA,AMD     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:50 AM ET: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM ET
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-19 10:15" --min-price 0

universe  Downloads Nasdaq Trader's symbol directories (nasdaqlisted.txt and
          otherlisted.txt) and keeps US-listed common stocks, ordinary shares
          and ADRs. The rule, in order:
            1. not a test issue, not an ETF, not NextShares
            2. a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one
               class letter after a dot (BRK.B); a .U / .W / .R suffix
               (units, warrants, rights) is out, and so is a $ preferred
            3. out, whatever else the name says: warrants; SPAC units and
               rights ("Units, each consisting of", "Rights to receive");
               notes, ETNs, debentures; a % coupon; "Depositary Shares" that
               are not American or Global (those carry a preferred)
            4. in: the name calls it common stock / shares / units, ordinary
               shares, shares of beneficial interest, ADS / ADR / American or
               Global depositary shares, or partnership / trust units
            5. out: the name still says unit, right, bond, preferred or preference
            6. in: everything else (plain names like "AMETEK, Inc.")

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (5) and regularMarketVolume >=
          --min-volume (1,000,000). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol (1 day of 5-minute bars, 20 a request); a chart goes on only
          if a completed bar after the range closed at or above the highest
          close of the range (a necessary condition). Stage 2: each candidate's
          v8 chart (real highs and lows): range high = the highest high of the
          range bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time. Stage 2 fetches at most --max-confirm (200) charts,
          the most traded candidates first (price x volume so far), and says
          so when it stops short. A chart is usable only when every 5-minute
          bar from 9:30 to the cutoff is there (a missing bar is a halt or no
          trades). Floors: --min-price 5 (last close), --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are New York (ET). --now pins the
clock ("2026-10-19 10:15", New York time; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with nasdaqlisted.txt and otherlisted.txt; daily and orb take a folder of
<T>.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"TSLA": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MINUTES = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

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

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
SETUP_LABEL = dict(SETUPS)


class ScanError(Exception):
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


def ts_to_eastern(timestamp):
    """Unix seconds -> naive New York time. Never uses the machine's time zone."""
    return to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(timestamp)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    text_iso = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text_iso)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-19 10:15" (New York) or ...Z (UTC)')
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text if isinstance(text, str) else "")
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ScanError("%s wants a New York time like 10:15" % flag)
    return datetime.time(int(match.group(1)), int(match.group(2)))


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


def clock_label(clock_time):
    """datetime or time -> '10:15 AM'."""
    hour12 = clock_time.hour % 12 or 12
    return "%d:%02d %s" % (hour12, clock_time.minute, "AM" if clock_time.hour < 12 else "PM")


def day_label(day):
    return "%s %s" % (day.strftime("%a"), day.isoformat())


def calendar_note(day):
    """A warning when day is past the holiday list (a holiday would look like a quiet day), else None."""
    if day.year in CALENDAR_YEARS:
        return None
    return "Holiday list covers %d to %d only: check nyse.com for %s." % (CALENDAR_YEARS[0], CALENDAR_YEARS[-1],
                                                                          day.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as error:
                last = "HTTP %d" % error.code
                try:
                    body = (error.read().decode("utf-8", "replace") if 400 <= error.code < 500 and error.code != 429
                            else "")
                except Exception:
                    body = ""
                finally:
                    error.close()
                if error.code == 429:
                    saw_429 = True
                    continue
                if error.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as error:
                last = str(getattr(error, "reason", error))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(symbol):
    return symbol.upper().replace(".", "-")


def spark_path(symbols, period, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(urllib.parse.quote(to_yahoo(symbol), safe="") for symbol in symbols), period, interval)


def chart_path(symbol, period, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(to_yahoo(symbol), safe=""), period, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(result):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    stamps = result.get("timestamp") or []
    count = len(stamps)

    def column(key):
        values = quote.get(key)
        return list(values) + [None] * (count - len(values)) if isinstance(values, list) else None
    return {"t": list(stamps), "c": column("close") or [None] * count, "o": column("open"), "h": column("high"),
            "l": column("low"), "v": column("volume"), "meta": result.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (BRK-B -> BRK.B)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    spark = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(spark, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    spark_error = spark.get("error")
    out = {}
    for item in spark.get("result") or []:
        response = item.get("response") or []
        if not response:
            continue
        yahoo_sym = item.get("symbol") or (response[0].get("meta") or {}).get("symbol") or ""
        symbol = (wanted or {}).get(yahoo_sym.upper(), yahoo_sym.upper().replace("-", "."))
        out[symbol] = series_from_result(response[0])
    if spark_error and not out:
        raise ScanError("Yahoo spark: %s" % (spark_error.get("description") or spark_error.get("code") or spark_error))
    return out


def parse_chart(text_or_doc, symbol=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % symbol)
    results = chart.get("result")
    if not results:
        chart_error = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (symbol, chart_error.get("description", "unknown ticker")))
    return series_from_result(results[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return file.read()
    with open(path, encoding="utf-8") as file:
        return file.read()


def file_stem(name):
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def absorb_doc(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        symbol = name_hint
        if not symbol:
            try:
                symbol = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        symbol = symbol.upper().replace("-", ".")
        try:
            data[symbol] = parse_chart(doc, symbol)
        except ScanError as error:
            errors[symbol] = str(error)
    elif isinstance(doc, dict):
        for key, value in doc.items():
            absorb_doc(value, key, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(name for name in os.listdir(path) if name.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for name in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, name)))
            except ValueError as error:
                raise ScanError("%s is not valid JSON: %s" % (name, error))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                absorb_doc(doc, "" if "spark" in doc else file_stem(name), data, errors)
            else:
                absorb_doc(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as error:
            raise ScanError("%s is not valid JSON: %s" % (path, error))
        absorb_doc(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = [
    ("warrant", re.compile(r"\bwarrants?\b")),
    ("unit or right", re.compile(r"\b(units?|rights?),? (each|that|entitling)\b|(?<!the )\brights? to\b")),
    ("note", re.compile(r"\b(notes?|etns?|debentures?)\b")),
    ("coupon", re.compile(r"%")),
    ("depositary", re.compile(r"(?<!american )(?<!global )\bdepositary shares?\b")),
]
COMMON = re.compile(
    r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b"
    r"|\b(limited partnership|partnership|trust) units?\b|\bl\.?p\.? units?\b"
    r"|\bunits? (of|representing)\b.*\b(limited partner|partnership|beneficial interest|limited liability)")
SOFT_OUT = [
    ("unit", re.compile(r"\bunits?\b")),
    ("right", re.compile(r"\brights?\b")),
    ("bond", re.compile(r"\bbonds?\b")),
    ("preferred", re.compile(r"\bpreferred\b|\bpreference\b")),
]


def classify(row):
    """One symbol-directory row -> 'keep' or the rule that drops it."""
    symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
    if (row.get("Test Issue") or "").strip() != "N":
        return "test issue"
    if (row.get("ETF") or "").strip() != "N":
        return "ETF"
    if (row.get("NextShares") or "N").strip() != "N":
        return "NextShares"
    if not TICKER.match(symbol):
        return "not a plain ticker"
    if re.search(r"\.[UWR]$", symbol):
        return "unit / warrant / right suffix"
    name = " ".join((row.get("Security Name") or "").lower().split())
    for tag, pattern in ALWAYS_OUT:
        if pattern.search(name):
            return tag
    if COMMON.search(name):
        return "keep"
    for tag, pattern in SOFT_OUT:
        if pattern.search(name):
            return tag
    return "keep"


def parse_symdir(text):
    """Nasdaq Trader pipe file -> (rows, file_created). The last line is 'File Creation Time: ...'."""
    lines = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    if not lines or "|" not in lines[0]:
        raise ScanError("not a Nasdaq Trader symbol directory")
    headers = [name.strip() for name in lines[0].split("|")]
    if "Security Name" not in headers or not ({"Symbol", "ACT Symbol"} & set(headers)):
        raise ScanError("not a Nasdaq Trader symbol directory (header %s)" % lines[0][:60])
    rows, created = [], None
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            match = re.search(r"(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})", line)
            if match:
                created = "%s-%s-%s %s:%s" % (match.group(3), match.group(1), match.group(2), match.group(4),
                                              match.group(5))
            continue
        rows.append(dict(zip(headers, line.split("|"))))
    return rows, created


def build_universe(texts):
    """{filename: text} -> (kept [(SYM, name)], counts, created)."""
    kept, counts, created = {}, {}, None
    for fname in NASDAQ_FILES:
        if fname not in texts:
            raise ScanError("missing %s" % fname)
        rows, file_created = parse_symdir(texts[fname])
        created = created or file_created
        counts["rows"] = counts.get("rows", 0) + len(rows)
        for row in rows:
            why = classify(row)
            counts[why] = counts.get(why, 0) + 1
            if why == "keep":
                symbol = (row.get("Symbol") or row.get("ACT Symbol")).strip()
                kept[symbol] = " ".join((row.get("Security Name") or "").split())
    return sorted(kept.items()), counts, created


# ---------------------------------------------------------------- daily setups

def rsi(closes, period=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < period + 1:
        return None
    gains = [max(0.0, closes[index] - closes[index - 1]) for index in range(1, len(closes))]
    losses = [max(0.0, closes[index - 1] - closes[index]) for index in range(1, len(closes))]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    return 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_eastern(now_utc)
    today = now.date()
    hours = session(today)
    today_done = hours is None or now.time() >= hours[1]
    out = {}
    for timestamp, close in zip(series["t"], series["c"]):
        if close is None:
            continue
        day = ts_to_eastern(timestamp).date()
        if day > today or (day == today and not today_done):
            continue
        out[day] = float(close)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers). A zero or negative
    close in the last 21 is bad data, not a setup: no keys."""
    if min(closes[-21:]) <= 0:
        return [], {}
    last_close = closes[-1]
    high20 = max(closes[-21:-1])
    ma20 = sum(closes[-20:]) / 20.0
    ma20_prev = sum(closes[-21:-1]) / 20.0
    run_up = closes[-6] / closes[-16] - 1
    range5 = (max(closes[-5:]) - min(closes[-5:])) / last_close
    rsi_now = rsi(closes)
    r_prev = rsi(closes[:-1])
    tags = []
    if last_close > high20:
        tags.append("breakout_20d")
    if run_up > 0.10 and range5 < 0.05:
        tags.append("bull_flag")
    if closes[-2] < ma20_prev and last_close > ma20:
        tags.append("reclaim_ma20")
    if rsi_now is not None and rsi_now < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last_close > closes[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": rsi_now, "rsi_prev": r_prev, "high20": high20, "ma20": ma20,
                  "run_pct": run_up * 100, "range5_pct": range5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for symbol in symbols:
        series = data.get(symbol)
        if not series:
            continue
        counts["fetched"] += 1
        bars = daily_closes(series, now_utc)
        meta = series.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        volume = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (volume is None or volume < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [close for _, close in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": symbol, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(volume) if volume is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most dollars traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot_time(day, index):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MINUTES * index)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive New York time),
    on the grid, with a close. Sorted, one per slot."""
    hours = session(day)
    close_t = hours[1] if hours else CLOSE
    out = {}
    highs, lows = series.get("h"), series.get("l")
    for index, timestamp in enumerate(series["t"]):
        close = series["c"][index]
        if close is None:
            continue
        eastern = ts_to_eastern(timestamp)
        if eastern.date() != day or eastern.second or eastern.minute % BAR_MINUTES:
            continue  # another day, or the live tick off the grid
        if eastern.time() < OPEN or eastern.time() >= close_t:
            continue  # pre-market, after hours, the 4:00 PM closing print
        if eastern + datetime.timedelta(minutes=BAR_MINUTES) > cutoff:
            continue  # still in progress at the cutoff
        volumes = series.get("v")
        out[eastern] = {"start": eastern, "c": float(close),
                        "h": None if not highs or highs[index] is None else float(highs[index]),
                        "l": None if not lows or lows[index] is None else float(lows[index]),
                        "v": (volumes[index] or 0) if volumes else 0}
    return [out[slot_index] for slot_index in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:30 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot_time(day, 0)).total_seconds() // (BAR_MINUTES * 60))
    hours = session(day)
    if hours:
        want = min(want, int((datetime.datetime.combine(day, hours[1]) - slot_time(day, 0)).total_seconds()
                             // (BAR_MINUTES * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(bar["start"] == slot_time(day, index) for index, bar in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(bar["c"] for bar in bars[:n_range])
    return any(bar["c"] >= top for bar in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    range_bars = bars[:n_range]
    if any(bar["h"] is None or bar["l"] is None for bar in range_bars):
        return None
    range_high = max(bar["h"] for bar in range_bars)
    range_low = min(bar["l"] for bar in range_bars)
    for index in range(n_range, len(bars)):
        bar = bars[index]
        if bar["c"] > range_high:
            closes_at = bar["start"] + datetime.timedelta(minutes=BAR_MINUTES)
            return {"price": round(bar["c"], 2), "at": clock_label(closes_at),
                    "bar_start": bar["start"].strftime("%H:%M"), "bar_index": index,
                    "or_high": round(range_high, 2), "or_low": round(range_low, 2),
                    "pct_over": round((bar["c"] / range_high - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive ET) or raise ScanError with the plain message."""
    now = to_eastern(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MINUTES or or_minutes % BAR_MINUTES or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    hours = session(day)
    range_end = slot_time(day, or_minutes // BAR_MINUTES)
    first_close = range_end + datetime.timedelta(minutes=BAR_MINUTES)
    close_dt = datetime.datetime.combine(day, hours[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s ET on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s ET: the %d-minute opening range runs 9:30 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s ET, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(symbol) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {symbol: why})."""
    n_range = or_minutes // BAR_MINUTES
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, candidates = [], {}, []
    for symbol in symbols:
        series = stage1.get(symbol)
        if not series:
            continue
        counts["fetched"] += 1
        bars = session_bars(series, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        volume = (sum(bar["v"] for bar in bars) if series.get("v")
                  else (series.get("meta") or {}).get("regularMarketVolume") or 0)
        if bars[-1]["c"] < min_price or volume < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            candidates.append((symbol, bars, bars[-1]["c"] * volume))
    counts["candidates"] = len(candidates)
    if confirm is not None and max_confirm and len(candidates) > max_confirm:
        candidates.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(candidates) - max_confirm
        candidates = candidates[:max_confirm]
    for symbol, bars, _ in candidates:
        full = bars
        if confirm is not None:
            try:
                chart = confirm(symbol)
            except ScanError as error:
                failed[symbol] = str(error)
                continue
            full = session_bars(chart, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[symbol] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        hit = orb_hit(full, n_range)
        if hit:
            hits.append(dict({"symbol": symbol, "setups": ["orb"]}, **hit))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for hit in hits:
        del hit["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(args, msg):
    if not getattr(args, "json", False) and not getattr(args, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, period, interval, pause, args=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[start:start + SPARK_MAX] for start in range(0, len(symbols), SPARK_MAX)]
    for batch_number, batch in enumerate(batches):
        if batch_number and pause:
            SLEEP(pause)
        wanted = {to_yahoo(symbol): symbol for symbol in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, period, interval)), wanted)
        except (ScanError, ValueError) as error:
            for symbol in batch:
                failed[symbol] = str(error)
            continue
        for symbol in batch:
            if symbol in got:
                data[symbol] = got[symbol]
            else:
                failed[symbol] = "no data from Yahoo"
        if args is not None and len(batches) > 25 and (batch_number + 1) % 50 == 0:
            progress(args, "  spark %d of %d requests" % (batch_number + 1, len(batches)))
    return data, failed


def read_symbols(args, offline_syms=None):
    if args.symbols:
        syms = [symbol for symbol in re.split(r"[,\s]+", args.symbols.upper()) if symbol]
    elif args.watchlist:
        syms = symbols_from_file(args.watchlist)
    elif offline_syms is not None and not args.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = args.universe or home("universe.txt")
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for symbol in syms:
        if symbol not in out:
            out.append(symbol)
    if args.limit is not None and args.limit < 0:
        raise ScanError("--limit wants a positive number of symbols (0 or none scans them all)")
    if args.limit:
        out = out[:args.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()
    except (OSError, ValueError) as error:      # ValueError: not UTF-8 text
        raise ScanError("cannot read %s: %s" % (path, error))
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as error:
            raise ScanError("%s is not valid JSON: %s" % (path, error))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        if not isinstance(items, list):
            raise ScanError("%s: \"hits\" should be a list" % path)
        out = []
        for item in items:
            symbol = item.get("symbol") if isinstance(item, dict) else item
            if isinstance(symbol, str) and symbol.strip():          # a hit without a symbol is skipped, never "NONE"
                out.append(symbol.strip().upper())
        return out
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            config = json.load(file)
    except ValueError as error:
        raise ScanError("%s is not valid JSON: %s" % (path, error))
    if not isinstance(config, dict):
        raise ScanError("%s should be one JSON object, { ... }" % path)
    return config


def write_json(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def dollars(amount):
    return "${:,.2f}".format(amount)


# ---------------------------------------------------------------- commands

def cmd_universe(args):
    started = time.time()
    texts = {}
    for index, fname in enumerate(NASDAQ_FILES):
        if args.offline:
            path = os.path.join(args.offline, fname)
            if not os.path.exists(path):
                raise ScanError("no %s in %s" % (fname, args.offline))
            with open(path, encoding="utf-8", errors="replace") as file:
                texts[fname] = file.read()
        else:
            if index and args.pause:
                SLEEP(args.pause)
            try:
                texts[fname] = http_get(NASDAQ_URL + fname, timeout=60)
            except urllib.error.HTTPError as error:
                raise ScanError("nasdaqtrader.com answered HTTP %d for %s" % (error.code, fname))
            except (urllib.error.URLError, OSError) as error:
                raise ScanError("could not reach nasdaqtrader.com: %s" % getattr(error, "reason", error))
    kept, counts, created = build_universe(texts)
    if not args.no_write:
        folder = os.path.dirname(args.out)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.out + ".tmp", "w", encoding="utf-8") as file:
            for symbol, name in kept:
                file.write("%s|%s\n" % (symbol, name))
        os.replace(args.out + ".tmp", args.out)
    dropped = {reason: count for reason, count in counts.items() if reason not in ("keep", "rows")}
    if args.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "files_created": created,
                          "dropped": dropped, "out": None if args.no_write else args.out,
                          "seconds": round(time.time() - started, 1)}, indent=2))
    else:
        print("Nasdaq Trader symbol directories%s: %s rows." % (
            (", files of %s ET" % created) if created else "", "{:,}".format(counts.get("rows", 0))))
        print("Kept %s US-listed common stocks, ordinary shares and ADRs." % "{:,}".format(len(kept)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(count), reason)
                                      for reason, count in sorted(dropped.items(), key=lambda pair: -pair[1])) + ".")
        if not args.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % args.out)
    return 0 if kept else 1


def cmd_daily(args):
    started = time.time()
    now = parse_now(args.now)
    offline_syms = None
    if args.offline:
        data, failed = load_offline(args.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(args, offline_syms)
    if not args.offline:
        progress(args, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", args.pause, args)
    hits, counts, last_date = scan_daily(data, symbols, now, args.min_price, args.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": args.min_price, "min_volume": args.min_volume},
           "note": NOTE, "hits": hits}
    if args.offline:
        doc["offline"] = os.path.basename(args.offline.rstrip("/"))
    if not args.no_write:
        write_json(args.out, doc)
    local = to_eastern(now)
    note = calendar_note(local.date())
    if args.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - started, 1), calendar_note=note),
                         indent=2))
        return 0 if hits else 1
    print("Daily scan, %s ET, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    if note:
        print(note)
    print("Fetched %s of %s charts%s. %s passed the liquidity filter ($%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), number_text(args.min_price),
                           "{:,}".format(args.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(setup_key, sum(1 for hit in hits if setup_key in hit["setups"])) for setup_key, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (count, SETUP_LABEL[setup_key]) for setup_key,
                                                            count in per if count)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for hit in hits[:args.show]:
        print("  %-6s %10s  RSI %5.1f  %s" % (hit["symbol"], dollars(hit["price"]), hit["rsi"],
                                              ", ".join(SETUP_LABEL[setup_key] for setup_key in hit["setups"])))
    if len(hits) > args.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - args.show))
    if not args.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (args.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def number_text(number):
    number = float(number)
    return str(int(number)) if number == int(number) else "%.2f" % number


def cmd_orb(args):
    started = time.time()
    now = parse_now(args.now)
    at_time = parse_hhmm(args.at) if args.at else None
    config = read_config(args.config)
    until = parse_hhmm(args.until or config.get("no_entries_after") or "15:30", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if args.offline:
        data, failed = load_offline(args.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not args.now:
            days = [ts_to_eastern(symbol["t"][-1]).date() for symbol in data.values() if symbol["t"]]
            if days:
                explicit_day = max(days)
                if at_time is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at_time, args.or_minutes, until, explicit_day)
    symbols = read_symbols(args, offline_syms)
    confirm = None
    if not args.offline:
        today = to_eastern(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        period = "1d" if day == today else "5d"
        progress(args, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, period, "%dm" % BAR_MINUTES, args.pause, args)
        done = []

        def confirm(symbol):
            if args.pause:
                SLEEP(args.pause)
            done.append(symbol)
            if len(done) % 50 == 0:
                progress(args, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(symbol, period, "%dm" % BAR_MINUTES)), symbol)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, args.or_minutes, args.min_price, args.min_volume,
                                     confirm,
                                     args.max_confirm)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([symbol for symbol in symbols if symbol in failed])
    as_of = "%s %s ET" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": args.or_minutes,
           "interval": "%dm" % BAR_MINUTES,
           "filters": {"min_price": args.min_price, "min_volume": args.min_volume}, "note": NOTE, "hits": hits}
    if args.offline:
        doc["offline"] = os.path.basename(args.offline.rstrip("/"))
    if not args.no_write:
        write_json(args.out, doc)
    note = calendar_note(day)
    if args.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - started, 1), calendar_note=note),
                         indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s ET: %d-minute range (9:30 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), args.or_minutes,
             clock_label(slot_time(day, args.or_minutes // BAR_MINUTES))))
    if note:
        print(note)
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(args.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for hit in hits[:args.show]:
            print("  %8s  %-6s %10s  range %s to %s  %+.2f%% over" % (
                hit["at"], hit["symbol"], dollars(hit["price"]), price_text(hit["or_low"]), price_text(hit["or_high"]),
                                                                      hit["pct_over"]))
        if len(hits) > args.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - args.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not args.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (args.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def price_text(price):
    return "{:,.2f}".format(price)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    commands = parser.add_subparsers(dest="cmd")

    command = commands.add_parser("universe", help="download Nasdaq Trader's lists -> trader/universe.txt")
    command.add_argument("--out", default=home("universe.txt"))
    command.add_argument("--offline", metavar="DIR", help="a folder with nasdaqlisted.txt and otherlisted.txt")
    command.add_argument("--no-write", action="store_true")
    command.add_argument("--pause", type=float, default=PAUSE)
    command.add_argument("--json", action="store_true")

    def common(command, min_price, min_volume):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        group.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        command.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)"
                             % home("universe.txt"))
        command.add_argument("--limit", type=int, help="scan only the first N symbols")
        command.add_argument("--min-price", type=float, default=min_price)
        command.add_argument("--min-volume", type=int, default=min_volume)
        command.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        command.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ...Z)')
        command.add_argument("--out", default=home("watchlist.json"))
        command.add_argument("--no-write", action="store_true")
        command.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        command.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        command.add_argument("--quiet", action="store_true")
        command.add_argument("--json", action="store_true")

    command = commands.add_parser("daily", help="pre-market setups from daily closes")
    common(command, 5.0, 1000000)
    command = commands.add_parser("orb", help="opening-range breakouts so far today")
    common(command, 5.0, 0)
    command.add_argument("--or-minutes", type=int, default=15)
    command.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                         help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    command.add_argument("--at", help="cut the clock at this New York time, HH:MM")
    command.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 15:30)")
    command.add_argument("--config", default=home("config.json"))
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[args.cmd](args)
    except ScanError as error:
        print("scan.py: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
