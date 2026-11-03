"""The large backtest's data: one SQLite file that fetch.py writes and backtest.py reads.

It also turns a stored session into the dicts the feature search's engine hands the pack's tools, the same
shape engine.load_symbol builds from a Yahoo file (selftest.py checks that, bar for bar). Standard library only.
"""

import datetime
import json
import os
import sqlite3
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "data", "alpaca.sqlite")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "feature-search"))

import engine  # noqa: E402  (the feature search's engine: the pack's tools, loaded in-process)

SCHEMA = """
CREATE TABLE IF NOT EXISTS done (step TEXT, key TEXT, PRIMARY KEY (step, key));
CREATE TABLE IF NOT EXISTS assets (symbol TEXT PRIMARY KEY, name TEXT, exchange TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS calendar (date TEXT PRIMARY KEY, open TEXT, close TEXT);
CREATE TABLE IF NOT EXISTS coverage (symbol TEXT PRIMARY KEY, daily_bars INTEGER);
CREATE TABLE IF NOT EXISTS features (symbol TEXT, date TEXT, factor REAL, prev_close REAL, atr REAL,
                                     avg_volume REAL, liquid INTEGER, PRIMARY KEY (symbol, date));
CREATE INDEX IF NOT EXISTS features_by_date ON features (date, liquid);
CREATE TABLE IF NOT EXISTS opening (date TEXT, symbol TEXT, dollar_volume REAL, PRIMARY KEY (date, symbol));
CREATE TABLE IF NOT EXISTS selection (date TEXT, rank INTEGER, symbol TEXT, relative_volume REAL,
                                      PRIMARY KEY (date, rank));
CREATE TABLE IF NOT EXISTS intraday (date TEXT, symbol TEXT, bars BLOB, PRIMARY KEY (date, symbol));
"""
SESSION_OPEN, SESSION_CLOSE = "09:30", "16:00"
LAST_BAR = "15:55"


def connect(path=DEFAULT_DB):
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        os.makedirs(folder)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def is_done(db, step, key):
    return db.execute("SELECT 1 FROM done WHERE step = ? AND key = ?", (step, key)).fetchone() is not None


def mark_done(db, step, key):
    db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (step, key))


def done_keys(db, step):
    return {row[0] for row in db.execute("SELECT key FROM done WHERE step = ?", (step,))}


def sessions(db, start, end, full_only=False):
    """Every market session from start to end (New York dates), oldest first; full_only drops early closes."""
    rows = db.execute("SELECT date, open, close FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                      (start, end)).fetchall()
    return [day for day, opens, closes in rows
            if not full_only or (opens == SESSION_OPEN and closes == SESSION_CLOSE)]


def utc_at(day, clock):
    """A New York date and "HH:MM" -> naive UTC, by the tools' own clock (no machine time zone)."""
    return engine.utc_at(day, clock)


def epoch_at(day, clock):
    return int((utc_at(day, clock) - datetime.datetime(1970, 1, 1)).total_seconds())


def new_york_date(epoch):
    return engine.entry.to_eastern(engine.entry.utc_of(epoch)).date().isoformat()


def pack_bars(rows):
    """[[epoch, open, high, low, close, volume], ...] -> compressed bytes."""
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode("utf-8"))


def unpack_bars(blob):
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def session_data(symbol, day, rows):
    """One stored session -> {symbol, days, sessions: {day: {chart, bars, exit_chart}}} for engine.signal and
    engine.trade_day, or None unless every 5-minute bar from 9:30 to 15:55 is there (engine.load_symbol's rule)."""
    picked = {}
    for epoch, open_price, high, low, close, volume in rows:
        epoch = int(epoch)
        if epoch % 300:
            continue
        eastern = engine.entry.to_eastern(engine.entry.utc_of(epoch))
        clock = eastern.strftime("%H:%M")
        if eastern.date().isoformat() == day and SESSION_OPEN <= clock <= LAST_BAR:
            picked[clock] = {"t": epoch, "o": float(open_price), "h": float(high), "l": float(low),
                             "c": float(close), "v": int(volume or 0)}
    if len(picked) != engine.SESSION_BARS:
        return None
    clocks = sorted(picked)
    bars = [picked[clock] for clock in clocks]
    data_time = epoch_at(day, SESSION_CLOSE)
    chart = {"symbol": symbol, "meta": {"symbol": symbol, "regularMarketTime": data_time}, "bars": bars,
             "prev_close": None, "data_time": data_time}
    return {"symbol": symbol, "days": [day], "sessions": {day: {
        "chart": chart,
        "bars": [{"start": clock, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}
                 for clock, bar in zip(clocks, bars)],
        "exit_chart": {"bars": [{"t": bar["t"], "h": bar["h"], "l": bar["l"], "c": bar["c"]} for bar in bars],
                       "data_time": data_time},
    }}}


def load_day(db, day, symbols=None):
    """{symbol: session data} for one day's stored sessions (all of them, or just these symbols)."""
    out = {}
    for symbol, blob in db.execute("SELECT symbol, bars FROM intraday WHERE date = ?", (day,)):
        if symbols is not None and symbol not in symbols:
            continue
        data = session_data(symbol, day, unpack_bars(blob))
        if data is not None:
            out[symbol] = data
    return out
