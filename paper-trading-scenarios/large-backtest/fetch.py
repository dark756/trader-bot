"""Downloads the large backtest's data from Alpaca (PLAN.md) into data/alpaca.sqlite.

    python -B paper-trading-scenarios/large-backtest/fetch.py            # every step; rerun it to resume
    python -B paper-trading-scenarios/large-backtest/fetch.py status     # what is done so far

Needs your Alpaca paper keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY, set in the terminal or written in a
.env.local file in the repo root (git ignores it); see the README.
It only reads market data, the asset list and the calendar: no order, no account.

The steps, each resumable (a stop loses at most the batch in flight):
  assets     US equities, active and inactive, without warrants, rights, units and preferreds
  calendar   the market sessions
  daily      daily bars, raw and split-adjusted, 50 symbols a request; only the statistics are kept (the
             previous close, the 14-session ATR and average volume), for the stocks that pass the liquidity
             floors and for the fixed list
  opening    each session's 9:30 bar for every stock liquid that day or in the 20 sessions after
  selection  the stocks in play, each session (computed here, no requests)
  intraday   each full session's 5-minute bars for its stocks in play and the fixed list

On Alpaca's free plan (200 requests a minute; this keeps to 180) it takes about one and a half to two hours,
most of it the opening step. The file ends up around 1 GB at most. Standard library only.
"""

import argparse
import sys
import time

import alpaca
import plan
import store
import universe

DAILY_BATCH = 50           # symbols a request: about 100,000 daily bars a batch in memory
OPENING_BATCH = 400          # symbols a request: keeps the URL short
LOOKAHEAD = 20               # sessions: a day's 9:30 bars are fetched for stocks liquid now or soon after
STEPS = ("assets", "calendar", "daily", "opening", "selection", "intraday")


def settings_from_plan():
    return {"daily_from": plan.DAILY_FROM, "opening_from": plan.OPENING_FROM, "start": plan.DEVELOPMENT[0],
            "end": plan.HOLDOUT[1], "fixed": list(plan.FIXED)}


def chunks(items, size):
    return [items[index:index + size] for index in range(0, len(items), size)]


class Progress(object):
    def __init__(self, log, name, total):
        self.log, self.name, self.total, self.started, self.last = log, name, total, time.time(), 0.0

    def tick(self, count, client, force=False):
        now = time.time()
        if not force and now - self.last < 30:
            return
        self.last = now
        elapsed = now - self.started
        left = (self.total - count) * elapsed / count if count else 0
        self.log("  %s: %d of %d, %d requests so far, about %.0f min left"
                 % (self.name, count, self.total, client.requests, left / 60.0))


def step_assets(client, db, settings, log):
    if store.is_done(db, "assets", "all"):
        return
    rows = {}
    counts = {}
    for status in ("inactive", "active"):          # active last, so a reused ticker keeps its live listing
        for asset in client.assets(status):
            if universe.keep_asset(asset, status):
                rows[asset["symbol"]] = (asset["symbol"], str(asset.get("name") or ""),
                                         str(asset.get("exchange") or ""), status)
        counts[status] = sum(1 for row in rows.values() if row[3] == status)
    for symbol in settings["fixed"]:
        rows.setdefault(symbol, (symbol, "", "", "fixed list"))
    with db:
        db.executemany("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?)", sorted(rows.values()))
        store.mark_done(db, "assets", "all")
    log("assets: %d symbols (%d active, %d inactive)" % (len(rows), counts.get("active", 0),
                                                         counts.get("inactive", 0)))


def step_calendar(client, db, settings, log):
    if store.is_done(db, "calendar", "all"):
        return
    days = client.calendar(settings["daily_from"], settings["end"])
    rows = [(str(day["date"]), str(day.get("open")), str(day.get("close"))) for day in days
            if isinstance(day, dict) and day.get("date")]
    with db:
        db.executemany("INSERT OR REPLACE INTO calendar VALUES (?, ?, ?)", rows)
        store.mark_done(db, "calendar", "all")
    log("calendar: %d sessions from %s to %s" % (len(rows), rows[0][0] if rows else "-", rows[-1][0] if rows else "-"))


def daily_rows(bars):
    """Alpaca daily bars -> {New York date: (open, high, low, close, volume)}."""
    out = {}
    for bar in bars:
        try:
            day = store.new_york_date(alpaca.epoch_of(bar["t"]))
            out[day] = (float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"]), float(bar["v"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def step_daily(client, db, settings, log):
    symbols = [row[0] for row in db.execute("SELECT symbol FROM assets ORDER BY symbol")]
    batches = chunks(symbols, DAILY_BATCH)
    finished = store.done_keys(db, "daily")
    todo = [(number, batch) for number, batch in enumerate(batches) if "%05d" % number not in finished]
    if not todo:
        return
    fixed = set(settings["fixed"])
    start = alpaca.rfc3339(store.utc_at(settings["daily_from"], "00:00"))
    end = alpaca.rfc3339(store.utc_at(settings["end"], "23:59"))
    progress = Progress(log, "daily", len(batches))
    log("daily: %d symbols in %d batches of %d, %d left" % (len(symbols), len(batches), DAILY_BATCH, len(todo)))
    for count, (number, batch) in enumerate(todo, len(batches) - len(todo)):
        raw = client.bars(batch, "1Day", start, end, "raw")
        split = client.bars(batch, "1Day", start, end, "split")
        rows, coverage = [], []
        for symbol in batch:
            raw_days, split_days = daily_rows(raw.get(symbol, [])), daily_rows(split.get(symbol, []))
            coverage.append((symbol, len(raw_days)))
            for day, item in sorted(universe.daily_features(raw_days, split_days).items()):
                liquid = universe.is_liquid(item)
                if liquid or symbol in fixed:
                    rows.append((symbol, day, item["factor"], item["prev_close"], item["atr"], item["avg_volume"],
                                 1 if liquid else 0))
        with db:
            db.executemany("INSERT OR REPLACE INTO features VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            db.executemany("INSERT OR REPLACE INTO coverage VALUES (?, ?)", coverage)
            store.mark_done(db, "daily", "%05d" % number)
        progress.tick(count + 1, client)
    progress.tick(len(batches), client, force=True)


def step_opening(client, db, settings, log):
    days = store.sessions(db, settings["opening_from"], settings["end"])
    finished = store.done_keys(db, "opening")
    todo = [index for index, day in enumerate(days) if day not in finished]
    if not todo:
        return
    fixed = set(settings["fixed"])
    progress = Progress(log, "opening", len(days))
    log("opening: %d sessions, %d left" % (len(days), len(todo)))
    for count, index in enumerate(todo, len(days) - len(todo)):
        day = days[index]
        last = days[min(index + LOOKAHEAD, len(days) - 1)]
        symbols = {row[0] for row in db.execute(
            "SELECT DISTINCT symbol FROM features WHERE liquid = 1 AND date BETWEEN ? AND ?", (day, last))} | fixed
        first = store.epoch_at(day, store.SESSION_OPEN)
        start = alpaca.rfc3339(store.utc_at(day, "09:30"))
        end = alpaca.rfc3339(store.utc_at(day, "09:34"))
        rows = []
        for batch in chunks(sorted(symbols), OPENING_BATCH):
            for symbol, bars in client.bars(batch, "5Min", start, end, "raw").items():
                for bar in bars:
                    try:
                        if alpaca.epoch_of(bar["t"]) != first:
                            continue
                        price = float(bar.get("vw") or bar["c"])
                        rows.append((day, symbol, float(bar["v"]) * price))
                    except (KeyError, TypeError, ValueError):
                        continue
        with db:
            db.executemany("INSERT OR REPLACE INTO opening VALUES (?, ?, ?)", rows)
            store.mark_done(db, "opening", day)
        progress.tick(count + 1, client)
    progress.tick(len(days), client, force=True)


def step_selection(db, settings, log):
    days = store.sessions(db, settings["opening_from"], settings["end"])
    if not days or not set(days) <= store.done_keys(db, "opening"):
        raise alpaca.AlpacaError("the opening step is not finished: run fetch.py again")
    picker = universe.InPlay()
    rows = []
    for day in days:
        liquid = {row[0] for row in db.execute("SELECT symbol FROM features WHERE date = ? AND liquid = 1", (day,))}
        openings = dict(db.execute("SELECT symbol, dollar_volume FROM opening WHERE date = ?", (day,)).fetchall())
        picks = picker.step(liquid, openings)
        if day >= settings["start"]:
            rows.extend((day, rank, symbol, relative) for rank, (symbol, relative) in enumerate(picks, 1))
    with db:
        db.execute("DELETE FROM selection")
        db.executemany("INSERT INTO selection VALUES (?, ?, ?, ?)", rows)
        store.mark_done(db, "selection", "all")
    picked_days = len({row[0] for row in rows})
    log("selection: %d picks over %d sessions (%.1f a session)" % (len(rows), picked_days,
                                                                    len(rows) / float(picked_days or 1)))


def step_intraday(client, db, settings, log):
    days = store.sessions(db, settings["start"], settings["end"], full_only=True)
    finished = store.done_keys(db, "intraday")
    todo = [day for day in days if day not in finished]
    if not todo:
        return
    if not store.is_done(db, "selection", "all"):
        raise alpaca.AlpacaError("the selection step is not finished: run fetch.py again")
    fixed = set(settings["fixed"])
    progress = Progress(log, "intraday", len(days))
    log("intraday: %d full sessions, %d left" % (len(days), len(todo)))
    for count, day in enumerate(todo, len(days) - len(todo)):
        symbols = {row[0] for row in db.execute("SELECT symbol FROM selection WHERE date = ?", (day,))} | fixed
        got = client.bars(sorted(symbols), "5Min", alpaca.rfc3339(store.utc_at(day, store.SESSION_OPEN)),
                          alpaca.rfc3339(store.utc_at(day, store.LAST_BAR)), "raw")
        rows = []
        for symbol, bars in got.items():
            packed = []
            for bar in bars:
                try:
                    packed.append([alpaca.epoch_of(bar["t"]), float(bar["o"]), float(bar["h"]), float(bar["l"]),
                                   float(bar["c"]), int(bar["v"])])
                except (KeyError, TypeError, ValueError):
                    continue
            rows.append((day, symbol, store.pack_bars(packed)))
        with db:
            db.executemany("INSERT OR REPLACE INTO intraday VALUES (?, ?, ?)", rows)
            store.mark_done(db, "intraday", day)
        progress.tick(count + 1, client)
    progress.tick(len(days), client, force=True)


def run(client, db, settings, log=print, steps=STEPS):
    for step in steps:
        started = time.time()
        if step == "selection":
            step_selection(db, settings, log)
        else:
            globals()["step_" + step](client, db, settings, log)
        log("%s: done (%.0f s)" % (step, time.time() - started))


def status(db, settings, log=print):
    days_all = store.sessions(db, settings["opening_from"], settings["end"])
    full = store.sessions(db, settings["start"], settings["end"], full_only=True)
    assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    batches = (assets + DAILY_BATCH - 1) // DAILY_BATCH
    seen = db.execute("SELECT COUNT(*), SUM(daily_bars > 0) FROM coverage").fetchone()
    inactive = db.execute("SELECT COUNT(*) FROM coverage JOIN assets USING (symbol) "
                          "WHERE status = 'inactive' AND daily_bars > 0").fetchone()[0]
    lines = [
        ("assets", "done, %d symbols" % assets if store.is_done(db, "assets", "all") else "not yet"),
        ("calendar", "done" if store.is_done(db, "calendar", "all") else "not yet"),
        ("daily", "%d of %d batches; %d of %d symbols had bars (%d of them inactive)"
         % (len(store.done_keys(db, "daily")), batches, seen[1] or 0, seen[0] or 0, inactive)),
        ("opening", "%d of %d sessions" % (len(store.done_keys(db, "opening")), len(days_all))),
        ("selection", "done" if store.is_done(db, "selection", "all") else "not yet"),
        ("intraday", "%d of %d full sessions" % (len(store.done_keys(db, "intraday")), len(full))),
    ]
    for name, text in lines:
        log("%-10s %s" % (name, text))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download the large backtest's data from Alpaca.")
    parser.add_argument("what", nargs="*", default=[], help="status, or step names (default: every step)")
    parser.add_argument("--db", default=store.DEFAULT_DB)
    parser.add_argument("--feed", default="sip", choices=("sip", "iex"))
    parser.add_argument("--per-minute", type=int, default=180, help="requests a minute (the free plan allows 200)")
    parser.add_argument("--data-url", default=alpaca.DATA_URL)
    parser.add_argument("--trading-url", default=alpaca.TRADING_URL)
    args = parser.parse_args(argv)
    db = store.connect(args.db)
    settings = settings_from_plan()
    if args.what == ["status"]:
        status(db, settings)
        return 0
    steps = args.what or list(STEPS)
    for step in steps:
        if step not in STEPS:
            print("fetch.py: unknown step %r (the steps are %s)" % (step, ", ".join(STEPS)))
            return 2
    try:
        key, secret, where = alpaca.find_keys()
        print("keys: from %s" % where)
        client = alpaca.Client(key, secret, args.data_url, args.trading_url, args.per_minute, args.feed)
        run(client, db, settings, steps=[step for step in STEPS if step in steps])
    except alpaca.AlpacaError as error:
        print("fetch.py: %s" % error)
        return 1
    except KeyboardInterrupt:
        print("\nfetch.py: stopped; run it again to carry on from here")
        return 1
    status(db, settings)
    print("Next: python -B paper-trading-scenarios/large-backtest/backtest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
