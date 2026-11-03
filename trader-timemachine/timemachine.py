#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Replays a planned day trade before you risk a dollar: 10,000 simulated runs of
the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:55            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol TSLA --date 2026-10-19 --entry 364.18 \\
        --stop 358.32 --target 370.04 --entry-time "10:15 AM"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the reel's sim.py, same algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m;
     one request). Keep only FULL sessions before the plan's date: 78 bars,
     9:30 to 4:00 New York time. Half days, gaps and today are left out.
  2. The pool: for every bar slot 1..77 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..77, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
     --block N (default 1, the reel's way) draws runs of N bars from one past
     session instead of each bar on its own, so a fast morning stays fast for
     N bars: calm and wild stretches come in clumps, as they do in real days.
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 16:00, the close, or
     13:00 on an NYSE early-close day).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.
     Like trader-entry, a close only counts inside the entry window
     (no_entries_before / no_entries_after in trader/config.json, 09:45 and
     15:30 by default) and, with --min-volume-x, on enough volume.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: earnings, news and halts can do what the last 60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<T>?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are New York (ET) wall-clock; --now "2026-10-19 10:15" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 78    # 9:30 to 16:00 in 5-minute bars
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
ENTRY_WINDOW = ("09:45", "15:30")   # trader-entry's defaults: no new entries before / after, New York
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET, from https://www.nyse.com/markets/hours-calendars (read 2026-10-13)
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
    "2028-07-03": "day before Independence Day",
    "2028-11-24": "day after Thanksgiving",
}
EARLY_MIN = 13 * 60


class TMError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- time (copied from trader-alerts)

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
        raise TMError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment)
    return (moment - moment.utcoffset()).replace(tzinfo=None)


def utc_of(timestamp):
    return EPOCH + datetime.timedelta(seconds=int(timestamp))


def parse_clock(text, what):
    """'10:15 AM', '10:15am', '15:55' or '3:55' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not match:
        raise TMError("%s wants a New York time like 10:15 AM or 15:55, not %r" % (what, text))
    hour, minute, am_pm = int(match.group(1)), int(match.group(2)), (match.group(3) or "").lower()
    if minute > 59 or hour > 23 or (am_pm and not 1 <= hour <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if am_pm == "p" and hour != 12:
        hour += 12
    elif am_pm == "a" and hour == 12:
        hour = 0
    elif not am_pm and 1 <= hour <= 8:
        hour += 12
    return hour * 60 + minute


def clock12(minutes):
    hour, minute = divmod(minutes, 60)
    return "%d:%02d %s" % (hour % 12 or 12, minute, "AM" if hour < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(symbol, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(symbol.upper().replace(".", "-")))


def _sleep(seconds):
    time.sleep(seconds)


def fetch_text(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_chart(symbol):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for host_index, host in enumerate(HOSTS):
        if host_index:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(symbol, host)
            try:
                return fetch_text(url)
            except urllib.error.HTTPError as error:
                last = error.code
                try:
                    error.close()
                except Exception:
                    pass
                if error.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if error.code == 429 or error.code >= 500:
                    break
                if error.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the ticker" % symbol.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (error.code, symbol.upper()))
            except urllib.error.URLError as error:
                raise TMError("could not reach Yahoo: %s" % error.reason)
            except OSError as error:
                raise TMError("could not reach Yahoo: %s" % error)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, symbol.upper()))


def read_offline(path, symbol):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % symbol.upper(), "%s-60d5m.json.gz" % symbol.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (symbol.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as file:
            return file.read()
    except OSError as error:
        raise TMError("could not read %s: %s" % (path, error))


def chart_result(text, symbol):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % symbol.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % symbol.upper())
    results = chart.get("result")
    if not results:
        chart_error = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (symbol.upper(),
                                                        chart_error.get("description") or "unknown ticker"))
    return results[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:49:45), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 9:30 to 16:00 New York time."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [quote.get(field) or [] for field in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    market_time = meta.get("regularMarketTime")
    if isinstance(market_time, (int, float)) and utc_of(market_time) < as_of:
        as_of = utc_of(market_time)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for index, timestamp in enumerate(stamps):
        open_price, high, low, close, volume = [column[index] if index < len(column) else None for column in cols]
        if timestamp is None or None in (open_price, high, low, close):
            dropped["no_price"] += 1
            continue
        timestamp = int(timestamp)
        if timestamp % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(timestamp)
        eastern = to_eastern(start)
        minutes_after_open = eastern.hour * 60 + eastern.minute - OPEN_MIN
        if minutes_after_open < 0 or minutes_after_open >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[timestamp] = {"date": eastern.date().isoformat(), "slot": minutes_after_open // 5,
                           "start": eastern.strftime("%H:%M"),
                           "o": open_price, "h": high, "l": low, "c": close, "v": volume or 0}
    return meta, [bars[timestamp] for timestamp in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for bar in bars:
        days.setdefault(bar["date"], {})[bar["slot"]] = bar
    return days


def full_sessions(days, before):
    """Full sessions (all 78 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (half days, gaps)."""
    full, short = [], []
    for day in sorted(days):
        if day >= before:
            continue
        slots = days[day]
        if len(slots) == SESSION_BARS:
            full.append((day, [slots[slot] for slot in range(SESSION_BARS)]))
        else:
            short.append({"date": day, "bars": len(slots)})
    return full, short


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[slot] = every past session's bar at that slot as (o, h, l, c) / the previous bar's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, bars in sessions:
        for slot in range(1, SESSION_BARS):
            prev_close = bars[slot - 1]["c"]
            pool[slot].append((bars[slot]["o"] / prev_close, bars[slot]["h"] / prev_close, bars[slot]["l"] / prev_close,
                               bars[slot]["c"] / prev_close))
    return pool


def percentile(ordered, quantile):
    return ordered[min(len(ordered) - 1, int(quantile * len(ordered)))]


def simulate(pool, entry, stop, target, entry_bar, last_bar, replays=10000, seed=19, window=2, side="buy", block=1):
    """Monte Carlo replays of bars entry_bar+1..last_bar. block 1: each bar drawn on its own,
    the same draws and order as the reel's sim.py. block N: runs of N bars from
    one past session (a block bootstrap), shifted together by the window."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    sessions = len(pool[1])

    def slot(random_source, bar):
        return bar if window == 0 else min(SESSION_BARS - 1, max(1, bar + random_source.randint(-window, window)))

    def draw(random_source):
        """One replay's bars, entry_bar+1..last_bar, as (open, high, low, close) ratios to the bar before."""
        if block <= 1:
            return [random_source.choice(pool[slot(random_source, bar)]) for bar in range(entry_bar + 1, last_bar + 1)]
        out, past_session, shift = [], 0, 0
        for bar in range(entry_bar + 1, last_bar + 1):
            if (bar - entry_bar - 1) % block == 0:
                past_session = random_source.randrange(sessions)
                shift = random_source.randint(-window, window) if window else 0
            out.append(pool[min(SESSION_BARS - 1, max(1, bar + shift))][past_session])
        return out

    random_source = random.Random(seed)
    outcomes, closes, paths = [], [], []
    for replay in range(replays):
        price = entry
        outcome = None
        keep = replay < SAMPLE_PATHS
        path = [entry]
        for open_ratio, high_ratio, low_ratio, close_ratio in draw(random_source):
            bar_high, bar_low, bar_close = price * high_ratio, price * low_ratio, price * close_ratio
            if outcome is None:
                if long:
                    if bar_low <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif bar_high >= target:
                        outcome = "target"
                else:
                    if bar_high >= stop:
                        outcome = "stop"
                    elif bar_low <= target:
                        outcome = "target"
            price = bar_close
            if keep:
                path.append(round(price, 2))
        outcomes.append(outcome or "close")
        closes.append(price)
        if keep:
            paths.append(path)
    counts = {kind: outcomes.count(kind) for kind in ("target", "stop", "close")}
    green = sum(1 for result, close_price in zip(outcomes, closes)
                if result == "target" or (result == "close" and (close_price > entry if long else close_price < entry)))
    r_multiples = [r_target if result == "target" else -1.0 if result == "stop"
                   else ((close_price - entry) if long else (entry - close_price)) / risk
                   for result, close_price in zip(outcomes, closes)]
    sorted_closes = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    random_source = random.Random(seed)
    fan_paths = []
    for replay in range(BAND_PATHS):
        price = entry
        path = [entry]
        for open_ratio, high_ratio, low_ratio, close_ratio in draw(random_source):
            price = price * close_ratio
            path.append(price)
        fan_paths.append(path)
    bands = []
    for bar in range(last_bar - entry_bar + 1):
        column = sorted(fan_path[bar] for fan_path in fan_paths)
        bands.append([round(percentile(column, quantile), 2) for quantile in BAND_QUANTILES])
    hist_low, hist_high = percentile(sorted_closes, 0.01), percentile(sorted_closes, 0.99)
    bin_width = (hist_high - hist_low) / HIST_BINS
    bins = [0] * HIST_BINS
    for close_price in sorted_closes:
        bin_index = int((close_price - hist_low) / bin_width) if bin_width > 0 else 0
        if 0 <= bin_index < HIST_BINS:
            bins[bin_index] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "block": block, "bars_left": last_bar - entry_bar,
        "p_target": round(counts["target"] / replays, 4), "p_stop": round(counts["stop"] / replays, 4),
        "p_neither": round(counts["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(percentile(sorted_closes, 0.5), 2), "p5_close": round(percentile(sorted_closes, 0.05), 2),
        "p95_close": round(percentile(sorted_closes, 0.95), 2), "avg_R": round(statistics.mean(r_multiples), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(hist_low, 2), "hi": round(hist_high, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def cents_away(price, long):
    """Round a stop to the cent, away from the entry: down for a buy, up for a sell."""
    cents = round(price * 100, 4)
    return (math.floor(cents) if long else math.ceil(cents)) / 100.0


def opening_range(bars, count):
    """(high, low) of the first count bars of a session given as {slot: bar} or a list."""
    rows = [bars[slot] for slot in range(count)]
    return max(bar["h"] for bar in rows), min(bar["l"] for bar in rows)


def rule_stop(rule, range_high, range_low, entry, long):
    stop_rule = rule["stop_rule"]
    if stop_rule == "or_mid":
        return cents_away((range_high + range_low) / 2, long)
    if stop_rule in ("or_low", "or_high"):
        return cents_away(range_low if long else range_high, long)
    frac = rule["stop_pct"] / 100.0
    return cents_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def history_replay(sessions, rule, last_bar, side="buy", entry_window=ENTRY_WINDOW, min_volume_x=None):
    """The plan's rule on every past full session, the way trader-entry fires it:
    entry = the first bar after the opening range to close beyond it, with its close
    inside the entry window and (with min_volume_x) on that many times the range's
    average volume, up to the --until bar; then stop first, then target, bar by bar,
    else the close at --until."""
    long = side == "buy"
    range_bars = rule["or_minutes"] // 5
    first, cutoff = (parse_clock(bound, "the entry window") for bound in entry_window)
    trades = []
    for day, bars in sessions:
        range_high, range_low = opening_range(bars, range_bars)
        avg_volume = sum(bars[bar]["v"] for bar in range(range_bars)) / float(range_bars)
        entry_bar = None
        for bar in range(range_bars, last_bar + 1):
            if not first <= OPEN_MIN + 5 * (bar + 1) <= cutoff:
                continue                       # outside the entry window, as trader-entry skips it
            if not (bars[bar]["c"] > range_high if long else bars[bar]["c"] < range_low):
                continue
            if min_volume_x and not (avg_volume and bars[bar]["v"] / avg_volume >= min_volume_x):
                continue                       # thin volume: trader-entry waits for the next close
            entry_bar = bar
            break
        if entry_bar is None:
            continue
        entry_price = round(bars[entry_bar]["c"], 2)
        stop = rule_stop(rule, range_high, range_low, entry_price, long)
        risk = (entry_price - stop) if long else (stop - entry_price)
        if risk <= 0:
            continue
        target = round(entry_price + rule["target_r"] * risk,
                       2) if long else round(entry_price - rule["target_r"] * risk, 2)
        outcome = None
        for later_bar in bars[entry_bar + 1:last_bar + 1]:
            if (later_bar["l"] <= stop) if long else (later_bar["h"] >= stop):
                outcome = "stop"
                break
            if (later_bar["h"] >= target) if long else (later_bar["l"] <= target):
                outcome = "target"
                break
        close_price = bars[last_bar]["c"]
        r_multiple = (((target - entry_price) if long else (entry_price - target)) / risk if outcome == "target"
                      else -1.0 if outcome == "stop"
                      else ((close_price - entry_price) if long else (entry_price - close_price)) / risk)
        trades.append({"date": day, "time": clock12(OPEN_MIN + 5 * (entry_bar + 1)), "entry": entry_price, "stop": stop,
                       "target": target,
                       "outcome": outcome or "close", "R": round(r_multiple, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for trade in trades if trade["outcome"] == "target"),
        "stop": sum(1 for trade in trades if trade["outcome"] == "stop"),
        "close": sum(1 for trade in trades if trade["outcome"] == "close"),
        "green": sum(1 for trade in trades if trade["R"] > 0),
        "avg_R": round(statistics.mean(trade["R"] for trade in trades), 3) if trades else None,
        "rule": rule, "entry_window": "%s to %s" % (clock24(first), clock24(cutoff)), "min_volume_x": min_volume_x,
        "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def positive_price(value, what):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, value))
    if not number > 0 or math.isinf(number):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, value))
    return number


def load_plan(args, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if args.symbol:
        missing = [field for field in ("entry", "stop", "target", "entry_time") if getattr(args, field) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + name.replace("_", "-") for name in missing))
        raw = {"symbol": args.symbol, "side": args.side, "date": args.date or to_eastern(now_utc).date().isoformat(),
               "entry": args.entry, "stop": args.stop, "target": args.target, "entry_time": args.entry_time,
               "setup": args.setup, "opening_range": {"minutes": args.or_minutes}, "stop_rule": args.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(args.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % args.plan)
        try:
            with open(args.plan, encoding="utf-8") as file:
                raw = json.load(file)
        except ValueError as error:
            raise TMError("%s is not valid JSON: %s" % (args.plan, error))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % args.plan)
        src = args.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", symbol):
        raise TMError("the plan needs a ticker symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    entry, stop, target = (positive_price(raw.get(field), field) for field in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    entry_bar = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15 AM)"
                          % raw["entry_time"])
        entry_bar = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            given_bar = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if entry_bar is not None and given_bar != entry_bar:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (given_bar, raw["entry_time"], entry_bar))
        entry_bar = given_bar
    if entry_bar is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15 AM) or entry_bar")
    if not 0 <= entry_bar <= SESSION_BARS - 2:
        raise TMError("the entry must close between 9:35 AM and 3:55 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (entry_bar + 1)))
    range_spec = raw.get("opening_range") or {}
    or_min = range_spec.get("minutes", 15) if isinstance(range_spec, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 390:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": symbol, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (entry_bar + 1)), "entry_bar": entry_bar,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(range_spec, minutes=or_min) if isinstance(range_spec, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    range_bars = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(slot in entry_day for slot in range(range_bars)):
            range_high, range_low = opening_range(entry_day, range_bars)
            if abs(cents_away((range_high + range_low) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(cents_away(range_low if long else range_high, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def until_text(monte_carlo):
    until = monte_carlo.get("until") or "16:00"
    return clock12(int(until[:2]) * 60 + int(until[3:]))


def lean(monte_carlo):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    p_target, p_stop = monte_carlo["p_target"], monte_carlo["p_stop"]
    if p_target + p_stop <= 0:
        return None
    r_target = monte_carlo.get("r_at_target") or 1.0
    share, even = p_target / (p_target + p_stop), 1.0 / (1.0 + r_target)
    return share - even, share, even


def verdict(monte_carlo):
    """One line from the numbers, see the docstring for the thresholds."""
    p_target, p_stop = monte_carlo["p_target"], monte_carlo["p_stop"]
    lean_result = lean(monte_carlo)
    if lean_result is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            until_text(monte_carlo), until_text(monte_carlo))
    edge, share, even = lean_result
    r_target = monte_carlo.get("r_at_target") or 1.0
    one_r = abs(r_target - 1.0) < 0.005
    if abs(edge) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(edge) < STRONG:
        head = "Leans toward the target" if edge > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if edge > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * p_target, 100 * p_stop)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r_target, 2), 100 * even, 100 * share, odds))


def verdict_notes(monte_carlo):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    p_neither = monte_carlo.get("p_neither", 0)
    if p_neither > MANY_NEITHER and lean(monte_carlo) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * p_neither, until_text(monte_carlo), until_text(monte_carlo)))
    lean_result = lean(monte_carlo)
    avg_r = monte_carlo.get("avg_R")
    if (lean_result and abs(lean_result[0]) >= COIN_FLIP and avg_r is not None and avg_r != 0
            and (avg_r > 0) != (lean_result[0] > 0)):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg_r)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo", block=1,
                 entry_window=ENTRY_WINDOW, min_volume_x=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    if not 1 <= block <= SESSION_BARS:
        raise TMError("--block must be between 1 (each bar on its own) and %d bars" % SESSION_BARS)
    if min_volume_x is not None and not min_volume_x > 0:
        raise TMError("--min-volume-x must be above 0")
    symbol = plan["symbol"]
    result = chart_result(text, symbol)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != symbol.replace(".", "-"):
        raise TMError("the data is for %s but the plan is for %s" % (msym, symbol))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % symbol)
    entry_bar = plan["entry_bar"]
    early = plan["date"] in NYSE_EARLY_CLOSE
    end_min = EARLY_MIN if early else CLOSE_MIN
    until_min = end_min if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock12(end_min), " (an early close)" if early else ""))
    last_bar = (until_min - OPEN_MIN) // 5 - 1
    if last_bar <= entry_bar:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock12(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if entry_bar not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * entry_bar), plan["entry_time"], plan["date"], symbol, reason))
    entry_row = entry_day[entry_bar]
    warn = []
    if not entry_row["l"] - 0.01 <= plan["entry"] <= entry_row["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], entry_row["l"], entry_row["h"]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), symbol, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    monte_carlo = simulate(pool, plan["entry"], plan["stop"], plan["target"], entry_bar, last_bar, replays, seed,
                           window, plan["side"],
                           block)
    monte_carlo = dict(monte_carlo, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
                       pool_to=sessions[-1][0],
                       band_times=[clock24(OPEN_MIN + 5 * (bar + 1)) for bar in range(entry_bar, last_bar + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {key: value for key, value in plan.items() if value is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last_bar, plan["side"], entry_window, min_volume_x)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    newest_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or symbol, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s ET" % (newest_bar["date"], newest_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": monte_carlo,
        "history": history,
        "verdict": verdict(monte_carlo),
        "verdict_notes": verdict_notes(monte_carlo),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: earnings, "
                 "news and halts can do things those sessions never did. Same seed, same numbers."
                 % (symbol, len(sessions))),
    }


# ---------------------------------------------------------------- output

def percent_text(fraction):
    return "%.1f %%" % (100.0 * fraction)


def report(doc):
    plan, monte_carlo, history = doc["plan"], doc["mc"], doc["history"]
    long = plan["side"] == "buy"
    until = clock12(int(monte_carlo["until"][:2]) * 60 + int(monte_carlo["until"][3:]))
    weekday = WEEKDAYS[datetime.date.fromisoformat(plan["date"]).weekday()]
    r_target = plan["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (plan["symbol"], plan["side"], weekday, plan["date"], plan["entry"], plan["entry_time"], plan["entry_bar"],
           plan["stop"],
           plan["target"], ("%g" % r_target), plan["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars), seed %d."
        % ("{:,}".format(monte_carlo["replays"]), plan["entry_time"], until, monte_carlo["bars_left"],
           monte_carlo["seed"]),
        ("Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
         "%d full sessions (%s to %s)." if monte_carlo.get("block", 1) <= 1 else
         "Bars are drawn in runs of %d from one past session (a block bootstrap), %s's own 5-min bars at the same "
         "time of day (+-%d bars) over its last %d full sessions (%s to %s).")
        % ((() if monte_carlo.get("block", 1) <= 1 else (monte_carlo["block"],))
           + (plan["symbol"], monte_carlo["window"], monte_carlo["pool_sessions"], monte_carlo["pool_from"],
              monte_carlo["pool_to"])),
        "",
        "  target first   %s" % percent_text(monte_carlo["p_target"]),
        "  stop first     %s" % percent_text(monte_carlo["p_stop"]),
        "  neither        %-8s still open at %s" % (percent_text(monte_carlo["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (percent_text(monte_carlo["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, monte_carlo["median_close"], monte_carlo["p5_close"], monte_carlo["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (monte_carlo["avg_R"], plan["risk_per_share"], ("+%g" % monte_carlo["r_at_target"]), ),
        "",
    ]
    if history.get("rule"):
        rule = history["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(rule["stop_rule"],
                                                           "%.2f %% from the entry" % rule.get("stop_pct", 0))
        when = ""
        if history.get("entry_window"):
            when = ", entries %s New York%s" % (history["entry_window"],
                                                (" on %gx the range's volume" % history["min_volume_x"])
                                                if history.get("min_volume_x") else "")
        if history["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target%s) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (rule["or_minutes"], stop_words, rule["target_r"], when, history["sessions"],
                            history["triggered"],
                            history["target"], history["stop"], history["close"], history["avg_R"]))
        else:
            lines.append("History: the same rule%s never fired in those %d sessions."
                         % ((" (%s)" % when[2:]) if when else "", history["sessions"]))
    else:
        lines.append("History: " + history.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for warning in doc.get("warnings") or []:
        lines.append("Note: " + warning + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(session["date"] for session in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % monte_carlo["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    plan, monte_carlo = doc["plan"], doc["mc"]
    bands, times = monte_carlo["bands"], monte_carlo["band_times"]
    low = min([band[0] for band in bands] + [plan["stop"], plan["target"]])
    high = max([band[4] for band in bands] + [plan["stop"], plan["target"]])
    pad = (high - low) * 0.04 or 0.01
    low, high = low - pad, high + pad
    step = (high - low) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((high - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for column, band in enumerate(bands):
        p5_row, p25_row, p50_row, p75_row, p95_row = [row(price) for price in band]
        for line_row in range(p95_row, p5_row + 1):
            grid[line_row][column] = "."
        for line_row in range(p75_row, p25_row + 1):
            grid[line_row][column] = ":"
        grid[p50_row][column] = "*"
    marks = {row(plan["target"]): "target", row(plan["stop"]): "stop"}
    if row(plan["entry"]) not in marks:
        marks[row(plan["entry"])] = "entry"
    for line_row in (row(plan["target"]), row(plan["stop"])):
        grid[line_row] = ["-" if char == " " else char for char in grid[line_row]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        plan["symbol"], plan["date"], "{:,}".format(monte_carlo.get("band_paths", BAND_PATHS)), plan["entry_time"],
                                                                   monte_carlo["seed"])]
    for line_row in range(height):
        label = marks.get(line_row)
        price = {"target": plan["target"], "stop": plan["stop"],
                 "entry": plan["entry"]}.get(label, high - line_row * step)
        left = ("%9.2f |" % price) if (label or line_row in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[line_row]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for column in sorted({0, len(bands) // 2, len(bands) - 1}):
        time_text = times[column]
        hour, minute_text = int(time_text[:2]), time_text[3:]
        tick_label = "%d:%s" % (hour % 12 or 12, minute_text)
        start = max(0, min(column, len(bands) - len(tick_label)))
        axis[start:start + len(tick_label)] = list(tick_label)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def write_json(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=1)
        file.write("\n")
    os.replace(tmp_path, path)


def entry_window(path):
    """(no_entries_before, no_entries_after) from the config, as trader-entry reads
    them: a missing or empty value keeps its default (09:45, 15:30)."""
    config = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as file:
                config = json.load(file)
        except (OSError, ValueError) as error:
            raise TMError("%s is not readable JSON: %s" % (path, error))
        if not isinstance(config, dict):
            raise TMError("%s should be one JSON object, { ... }" % path)
    out = tuple(config.get(key) or default for key,
                default in zip(("no_entries_before", "no_entries_after"), ENTRY_WINDOW))
    for key, value in zip(("no_entries_before", "no_entries_after"), out):
        parse_clock(value, key)
    return out


def compute(args):
    now = parse_now(args.now)
    plan = load_plan(args, now)
    window = entry_window(args.config)
    if args.offline:
        text, source = read_offline(args.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, args.replays, args.seed, args.window, args.until, source, args.block, window,
                        args.min_volume_x)


def cmd_run(args):
    doc = compute(args)
    if args.write:
        write_json(args.write, doc)
    if args.json:
        slim = dict(doc, mc={key: value for key, value in doc["mc"].items() if key != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if args.write:
            print("Wrote %s." % args.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(args):
    if args.from_file:
        if not os.path.exists(args.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % args.from_file)
        try:
            with open(args.from_file, encoding="utf-8") as file:
                doc = json.load(file)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % args.from_file)
    else:
        doc = compute(args)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Time Machine: replay a planned day trade (trader-timemachine).")
    commands = parser.add_subparsers(dest="cmd")

    def common(command):
        command.add_argument("--plan", default=home("plan.json"), help="the plan from trader-exit")
        command.add_argument("--symbol", help="or give the plan as flags: ticker")
        command.add_argument("--side", default="buy", choices=["buy", "sell"])
        command.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, New York)")
        command.add_argument("--entry", type=float)
        command.add_argument("--stop", type=float)
        command.add_argument("--target", type=float)
        command.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15 AM"')
        command.add_argument("--setup", default="orb")
        command.add_argument("--or-minutes", type=int, default=15)
        command.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        command.add_argument("--replays", type=int, default=10000)
        command.add_argument("--seed", type=int, default=19)
        command.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        command.add_argument("--block", type=int, default=1,
                             help="draw runs of this many bars from one past session (default 1: each bar on its own)")
        command.add_argument("--min-volume-x", type=float,
                             help="history: a breakout counts on this many times the range's volume, "
                                  "as in trader-entry")
        command.add_argument("--config", default=home("config.json"), help="the entry window for the history")
        command.add_argument("--until", help="end each replay at this time (default 16:00, the close)")
        command.add_argument("--offline", metavar="FILE",
                             help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        command.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ISO with Z)')

    command = commands.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(command)
    command.add_argument("--json", action="store_true")
    command.add_argument("--write", nargs="?", const=home("timemachine.json"), metavar="PATH",
                         help="save the result (default trader/timemachine.json)")
    command = commands.add_parser("chart", help="ASCII fan chart of the replays")
    common(command)
    command.add_argument("--from", dest="from_file", nargs="?", const=home("timemachine.json"), metavar="PATH",
                         help="draw a saved result instead of running again")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[args.cmd](args)
    except TMError as error:
        print("timemachine.py: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
