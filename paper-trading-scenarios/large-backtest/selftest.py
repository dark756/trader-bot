"""Checks the large backtest's code without Alpaca: no keys, no network, about a minute.

    python -B paper-trading-scenarios/large-backtest/selftest.py           # --keep keeps its files

A fake Alpaca server on this machine serves the feature search's 30 stocks (feature-search/data, 59 sessions)
in Alpaca's format, with a 429 and a 500 thrown in, and the checks are:

  1. the universe rules on made-up series: a split never moves the daily statistics, the relative-volume
     ranking, the asset filter, Alpaca's time stamps; and the key lookup (the terminal, then .env.local or
     .env) on made-up keys;
  2. fetch.py against the fake server, stopped part way and run again, ends with exactly the data of one
     clean run;
  3. every stored session is the one engine.load_symbol builds from the Yahoo file, bar for bar;
  4. through the stored sessions, four of the feature search's combos give exactly its trades and net
     (results/combos.csv), so the store and the harness are the validated engine;
  5. a session moved to 2021 (outside the tools' 2026 to 2028 holiday list) trades exactly as in 2026;
  6. backtest.py runs both periods end to end, and the holdout lock refuses a second look and a changed plan.

The numbers it prints are plumbing checks, not results: these 59 sessions are the feature search's.
Standard library only.
"""

import base64
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import http.server
import urllib.parse

import alpaca
import backtest
import fetch
import plan
import store
import universe

import engine  # noqa: E402  (on the path through store.py)
import search  # noqa: E402

FEATURE_SEARCH = os.path.join(os.path.dirname(store.HERE), "feature-search")
SPLIT_SYMBOL, SPLIT_RATIO = "TSLA", 3.0      # served as if it split 3 for 1 after the data ends
PAGE_CAP = 1000                              # the fake pages at 1,000 bars, so every step pages
CHECK_COMBOS = ["orb15 or-mid 1R market novol until1530", "orb5 atr:1 1R market novol until1530",
                "orb30 pct:1 2R cap vol1.5x until1100", "always pct:1 1R market novol until1530"]
FAILURES = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, (": " + detail) if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------- the fake Alpaca

def load_fake_market():
    """The feature search's bars, as Alpaca would serve them: {symbol: [(epoch, o, h, l, c, v), ...]}."""
    market = {}
    for symbol in plan.FIXED:
        path = os.path.join(FEATURE_SEARCH, "data", symbol + ".json.gz")
        chart = engine.entry.parse_chart(engine.read_text(path), symbol)
        rows = []
        for bar in chart["bars"]:
            if bar["t"] % 300:
                continue
            clock = engine.entry.to_eastern(engine.entry.utc_of(bar["t"])).strftime("%H:%M")
            if store.SESSION_OPEN <= clock <= store.LAST_BAR:
                rows.append((bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]))
        market[symbol] = rows
    return market


def daily_of(rows):
    days = {}
    for epoch, open_price, high, low, close, volume in rows:
        day = store.new_york_date(epoch)
        if day not in days:
            days[day] = [open_price, high, low, close, volume]
        else:
            item = days[day]
            item[1], item[2], item[3], item[4] = max(item[1], high), min(item[2], low), close, item[4] + volume
    return days


class FakeAlpaca(object):
    def __init__(self, market):
        self.market = market
        self.daily = {symbol: daily_of(rows) for symbol, rows in market.items()}
        self.days = sorted({day for by_day in self.daily.values() for day in by_day})
        self.requests = 0
        self.faults = {3: 429, 8: 500}

    def bars(self, query):
        symbols = sorted(query["symbols"].split(","))
        start, end = alpaca.epoch_of(query["start"]), alpaca.epoch_of(query["end"])
        out = []
        for symbol in symbols:
            if query["timeframe"] == "1Day":
                ratio = SPLIT_RATIO if symbol == SPLIT_SYMBOL and query.get("adjustment") == "split" else 1.0
                for day, (open_price, high, low, close, volume) in sorted(self.daily.get(symbol, {}).items()):
                    epoch = store.epoch_at(day, "00:00")
                    if start <= epoch <= end:
                        out.append((symbol, {"t": stamp(epoch), "o": open_price / ratio, "h": high / ratio,
                                             "l": low / ratio, "c": close / ratio, "v": volume * ratio,
                                             "vw": close / ratio}))
            elif query["timeframe"] == "5Min":
                for epoch, open_price, high, low, close, volume in self.market.get(symbol, []):
                    if start <= epoch <= end:
                        out.append((symbol, {"t": stamp(epoch), "o": open_price, "h": high, "l": low, "c": close,
                                             "v": volume, "vw": (high + low + close) / 3.0}))
        offset = int(base64.b64decode(query["page_token"]).decode()) if query.get("page_token") else 0
        size = min(int(query.get("limit", 1000)), PAGE_CAP)
        page = out[offset:offset + size]
        doc = {"bars": {}, "next_page_token": None}
        for symbol, bar in page:
            doc["bars"].setdefault(symbol, []).append(bar)
        if offset + size < len(out):
            doc["next_page_token"] = base64.b64encode(str(offset + size).encode()).decode()
        return doc

    def assets(self, status):
        listed = {"AAPL": "NASDAQ", "SPY": "ARCA", "JPM": "NYSE"}
        if status == "active":
            rows = [{"symbol": symbol, "name": symbol + " Inc. Common Stock", "class": "us_equity",
                     "exchange": listed.get(symbol, "NASDAQ"), "status": "active"} for symbol in plan.FIXED]
            rows += [{"symbol": "ABCD.WS", "name": "ABCD Corp", "class": "us_equity", "exchange": "NYSE"},
                     {"symbol": "WXYZ", "name": "WXYZ Acquisition Corp Warrants", "class": "us_equity",
                      "exchange": "NASDAQ"},
                     {"symbol": "OTCX", "name": "OTCX Inc", "class": "us_equity", "exchange": "OTC"}]
            return rows
        return [{"symbol": "OLDCO", "name": "Old Co", "class": "us_equity", "exchange": "OTC", "status": "inactive"}]

    def calendar(self):
        return [{"date": day, "open": "09:30", "close": "16:00"} for day in self.days]


def stamp(epoch):
    return alpaca.rfc3339(engine.entry.utc_of(epoch))


def serve(fake):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            fake.requests += 1
            fault = fake.faults.get(fake.requests)
            if fault:
                self.send_response(fault)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            if not self.headers.get("APCA-API-KEY-ID") or not self.headers.get("APCA-API-SECRET-KEY"):
                self.send_response(401)
                self.end_headers()
                return
            parsed = urllib.parse.urlparse(self.path)
            query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            if parsed.path == "/v2/stocks/bars":
                doc = fake.bars(query)
            elif parsed.path == "/v2/assets":
                doc = fake.assets(query.get("status"))
            elif parsed.path == "/v2/calendar":
                doc = fake.calendar()
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(doc).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def client_for(url, max_requests=None):
    return alpaca.Client("test-key", "test-secret", url, url, per_minute=100000, log=lambda *args: None,
                         max_requests=max_requests)


def table(db, name):
    return db.execute("SELECT * FROM %s ORDER BY 1, 2" % name).fetchall()


# ---------------------------------------------------------------- the checks

def check_rules():
    stamps = {"2021-01-04T14:30:00Z": 1609770600, "2021-01-04T09:30:00-05:00": 1609770600,
              "2021-01-04T14:30:00.123Z": 1609770600}
    check("Alpaca time stamps", all(alpaca.epoch_of(text) == value for text, value in stamps.items()))
    # a stock that split 2 for 1 on day 20: the raw prices halve there, the split-adjusted series is smooth
    smooth, raw = {}, {}
    for number in range(40):
        day = "2021-02-%02d" % (number + 1) if number < 28 else "2021-03-%02d" % (number - 27)
        price = 50.0 + number
        bar = (price, price + 1.5, price - 1.0, price + 0.5, 2000000.0)
        smooth[day] = bar
        ratio = 2.0 if number < 20 else 1.0        # before the split, raw prices were twice the adjusted ones
        raw[day] = (bar[0] * ratio, bar[1] * ratio, bar[2] * ratio, bar[3] * ratio, bar[4] / ratio)
    features = universe.daily_features(raw, smooth)
    days = sorted(smooth)
    after = features[days[25]]
    before = features[days[18]]
    check("a split never moves the daily statistics",
          abs(after["factor"] - 1.0) < 1e-12 and abs(before["factor"] - 2.0) < 1e-12
          and abs(after["avg_volume"] - 2000000.0) < 1e-6 and abs(before["avg_volume"] - 1000000.0) < 1e-6
          and abs(after["prev_close"] - smooth[days[24]][3]) < 1e-9
          and abs(before["prev_close"] - 2 * smooth[days[17]][3]) < 1e-9
          and abs(after["atr"] - 2.5) < 1e-9 and abs(before["atr"] - 5.0) < 1e-9,
          "%r %r" % (before, after))
    picker = universe.InPlay(top=2, min_relative=1.0, min_prior=3, lookback=4)
    history = [{"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}] * 4
    for openings in history:
        picker.step({"AAA", "BBB", "CCC"}, openings)
    today = picker.step({"AAA", "BBB", "CCC", "DDD"}, {"AAA": 300.0, "BBB": 50.0, "CCC": 150.0, "DDD": 900.0})
    check("stocks in play: ranked by relative volume, floors and history kept", today == [("AAA", 3.0), ("CCC", 1.5)],
          repr(today))
    twin = universe.InPlay(top=5, min_relative=1.0, min_prior=1, lookback=4)
    twin.step({"OLD", "NEW"}, {"OLD": 100.0, "NEW": 100.0})
    check("one company under two tickers counts once", [s for s, _ in twin.step({"OLD", "NEW"}, {"OLD": 200.0,
                                                                                                  "NEW": 200.0})]
          == ["NEW"])
    assets = [({"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ"}, "active", True),
              ({"symbol": "BRK.B", "name": "Berkshire Hathaway Inc. Class B", "exchange": "NYSE"}, "active", True),
              ({"symbol": "ABCD.WS", "name": "ABCD", "exchange": "NYSE"}, "active", False),
              ({"symbol": "UNITY", "name": "Unity Software Inc.", "exchange": "NYSE"}, "active", True),
              ({"symbol": "XYZ", "name": "XYZ Corp Units", "exchange": "NYSE"}, "active", False),
              ({"symbol": "PFX", "name": "PFX 5.25% Notes", "exchange": "NYSE"}, "active", False),
              ({"symbol": "OTCX", "name": "OTCX Inc", "exchange": "OTC"}, "active", False),
              ({"symbol": "GONE", "name": "Gone Inc", "exchange": "OTC"}, "inactive", True)]
    check("the asset filter", all(universe.keep_asset(asset, status) == want for asset, status, want in assets))


def check_keys(folder):
    """The key lookup on made-up keys: the terminal first, then .env.local, then .env, in the folders given."""
    first, second = os.path.join(folder, "keys-one"), os.path.join(folder, "keys-two")
    for path in (first, second):
        os.makedirs(path)
    with open(os.path.join(first, ".env.local"), "w", encoding="utf-8-sig", newline="\r\n") as file:
        file.write("# Alpaca paper keys\nexport APCA_API_KEY_ID=\"fake-id-1\"  # from the dashboard\n"
                   "APCA_API_SECRET_KEY = 'fake-secret-1'\n")
    with open(os.path.join(first, ".env"), "w", encoding="utf-8") as file:
        file.write("APCA_API_KEY_ID=not-this-one\nAPCA_API_SECRET_KEY=not-this-one\n")
    with open(os.path.join(second, ".env"), "w", encoding="utf-8") as file:
        file.write('$env:ALPACA_API_KEY = "fake-id-2"\n$env:ALPACA_SECRET_KEY = "fake-secret-2"\n')
    found = alpaca.find_keys(environ={}, folders=[first, second])
    check(".env.local: a byte-order mark, Windows line ends, export, quotes and comments",
          found == ("fake-id-1", "fake-secret-1", os.path.join(first, ".env.local")), repr(found[2:]))
    found = alpaca.find_keys(environ={}, folders=[second])
    check(".env with PowerShell's $env: lines and the MCP server's names", found[:2] == ("fake-id-2", "fake-secret-2"))
    found = alpaca.find_keys(environ={"APCA_API_KEY_ID": "from-terminal", "APCA_API_SECRET_KEY": "from-terminal"},
                             folders=[first])
    check("keys set in the terminal come before any file", found == ("from-terminal", "from-terminal",
                                                                     "the terminal's environment"))
    with open(os.path.join(second, ".env"), "w", encoding="utf-8") as file:
        file.write("MY_KEY=fake-id-3\nMY_SECRET=fake-secret-3\n")
    try:
        alpaca.find_keys(environ={}, folders=[second])
        message = ""
    except alpaca.AlpacaError as error:
        message = str(error)
    check("keys under other names: a one-line error that names the file and never the values",
          "2 NAME=value lines" in message and "fake-" not in message and "MY_KEY" not in message, message)


def fetch_settings(fake):
    return {"daily_from": fake.days[0], "opening_from": fake.days[0], "start": fake.days[0], "end": fake.days[-1],
            "fixed": list(plan.FIXED)}


def check_fetch(fake, url, folder):
    settings = fetch_settings(fake)
    clean = store.connect(os.path.join(folder, "clean.sqlite"))
    fetch.run(client_for(url), clean, settings, log=lambda *args: None)
    broken_path = os.path.join(folder, "resumed.sqlite")
    broken = store.connect(broken_path)
    stopped = False
    for budget in (5, 40, 120):                       # stops part way through daily, opening and intraday
        try:
            fetch.run(client_for(url, max_requests=budget), broken, settings, log=lambda *args: None)
        except KeyboardInterrupt:
            stopped = True
    fetch.run(client_for(url), broken, settings, log=lambda *args: None)
    same = all(table(clean, name) == table(broken, name)
               for name in ("assets", "calendar", "coverage", "features", "opening", "selection", "intraday"))
    check("fetch.py stopped part way and resumed ends with one clean run's data", stopped and same)
    kept = {row[0] for row in clean.execute("SELECT symbol FROM assets")}
    check("fetch.py keeps the listed stocks and the delisted one, and drops warrants and OTC listings",
          kept == set(plan.FIXED) | {"OLDCO"}, repr(sorted(kept - set(plan.FIXED))))
    plain = {symbol: daily_of(rows) for symbol, rows in fake.market.items()}
    expected = universe.daily_features({day: tuple(bar) for day, bar in plain[SPLIT_SYMBOL].items()},
                                       {day: tuple(bar) for day, bar in plain[SPLIT_SYMBOL].items()})
    stored = {day: (prev_close, atr, avg_volume) for day, prev_close, atr, avg_volume in clean.execute(
        "SELECT date, prev_close, atr, avg_volume FROM features WHERE symbol = ?", (SPLIT_SYMBOL,))}
    close = all(abs(stored[day][0] - item["prev_close"]) < 1e-6 and abs(stored[day][1] - item["atr"]) < 1e-6
                and abs(stored[day][2] - item["avg_volume"]) < 1e-3 for day, item in expected.items())
    check("a split served by Alpaca's split-adjusted bars leaves the statistics in raw units",
          close and len(stored) == len(expected))
    picks = clean.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM selection").fetchone()
    check("stocks in play were picked", picks[0] > 0, repr(picks))
    return clean


def merged(db, symbol, days):
    data = {"symbol": symbol, "days": [], "sessions": {}}
    for day in days:
        one = store.load_day(db, day, {symbol}).get(symbol)
        if one:
            data["days"].append(day)
            data["sessions"][day] = one["sessions"][day]
    return data


def check_sessions(db):
    mismatches, compared = [], 0
    for symbol in plan.FIXED:
        reference = engine.load_symbol(os.path.join(FEATURE_SEARCH, "data", symbol + ".json.gz"), symbol)
        ours = merged(db, symbol, store.sessions(db, "0000", "9999"))
        if ours["days"] != reference["days"]:
            mismatches.append((symbol, "days"))
            continue
        for day in reference["days"]:
            compared += 1
            theirs, mine = reference["sessions"][day], ours["sessions"][day]
            close = store.epoch_at(day, store.SESSION_CLOSE)  # exit.session_bars drops a 16:00 closing tick
            exit_bars = [{"t": bar["t"], "h": bar["h"], "l": bar["l"], "c": bar["c"]}
                         for bar in theirs["exit_chart"]["bars"] if bar["t"] % 300 == 0 and bar["t"] < close]
            if (theirs["chart"]["bars"] != mine["chart"]["bars"] or theirs["bars"] != mine["bars"]
                    or exit_bars != mine["exit_chart"]["bars"]):
                mismatches.append((symbol, day))
    check("every stored session is engine.load_symbol's, bar for bar (%d stock-days)" % compared, not mismatches,
          repr(mismatches[:5]))


def check_trades(db):
    rows = {row["id"]: row for row in csv.DictReader(open(os.path.join(FEATURE_SEARCH, "results", "combos.csv"),
                                                          encoding="utf-8"))}
    universe_data = {symbol: merged(db, symbol, store.sessions(db, "0000", "9999")) for symbol in plan.FIXED}
    common = sorted(set.intersection(*(set(data["days"]) for data in universe_data.values())))
    days = common[1:]
    combos = {combo["id"]: combo for combo in search.grid()}
    for combo_id in CHECK_COMBOS:
        combo = combos[combo_id]
        trades, net = 0, 0.0
        for symbol, data in universe_data.items():
            if combo["setup"] == "always":
                signals = {day: search.always_signal(data, day) for day in days}
            else:
                signals = {day: engine.signal(data, day, combo["setup"], combo["or_minutes"], combo["volume"],
                                              combo["cutoff"]) for day in days}
            for row in engine.run_account(data, days, combo, signals):
                if row["action"] == "traded":
                    trades += 1
                    net += row["pnl"] - 2 * search.COST * row["shares"]
        want = rows[combo_id]
        check("%s: %d trades, net %.2f, as in the feature search" % (combo_id, trades, net),
              trades == int(want["all_trades"]) and abs(round(net, 2) - float(want["all_net"])) < 0.005,
              "want %s trades, %s" % (want["all_trades"], want["all_net"]))


def check_old_dates(db):
    day, old_day = "2026-10-19", "2021-10-18"           # both Mondays in daylight time
    shift = store.epoch_at(day, "09:30") - store.epoch_at(old_day, "09:30")
    blob = db.execute("SELECT bars FROM intraday WHERE date = ? AND symbol = 'TSLA'", (day,)).fetchone()
    if blob is None:
        check("a 2021 session trades as in 2026", False, "no TSLA session on %s" % day)
        return
    rows = store.unpack_bars(blob[0])
    new = store.session_data("TSLA", day, rows)
    old = store.session_data("TSLA", old_day, [[row[0] - shift] + list(row[1:]) for row in rows])
    same = True
    for key in ("A", "B", "A0", "B0"):
        kind = "always" if key.endswith("0") else key
        results = []
        for data, when in ((new, day), (old, old_day)):
            sig = backtest.signals_for(data, when)[kind]
            row = backtest.trade(data, when, key, sig, {"atr": 9.0}, plan.START_CASH)
            results.append((row["action"], row.get("shares"), row.get("fill_price"), row.get("pnl"),
                            row.get("exit_reason")))
        same = same and results[0] == results[1] and results[0][0] != "no signal"
    check("a session moved to 2021 trades exactly as in 2026, for A, B and their twins", same)


def check_end_to_end(fake, db_path, folder):
    days = fake.days
    settings = dict(backtest.settings_from_plan(), development=(days[15], days[44]), holdout=(days[45], days[-1]))
    out = os.path.join(folder, "run", "results")
    db = store.connect(db_path)
    quiet = lambda *args: None                        # noqa: E731
    dev = backtest.development(db, settings, out, log=quiet)
    backtest.write_results(out, settings, os.path.join(folder, "run", "RESULTS.md"))
    with open(os.path.join(folder, "run", "RESULTS.md"), encoding="utf-8") as file:
        text = file.read()
    check("backtest.py development runs end to end and writes RESULTS.md",
          dev["sessions"] == 30 and "| A-IN" in text and chr(0x2014) not in text)
    nothing = all(not dev["runs"][run]["verdict"]["pass"] for run in plan.TESTED) and \
        not dev["walk_forward"]["verdict"]["pass"]
    if nothing:
        check("with nothing passed, the holdout stays locked", backtest.holdout(db, settings, out, log=quiet) is None
              and not os.path.exists(os.path.join(out, "holdout-log.json")))
    # pretend A-IN passed, to try the lock
    path = os.path.join(out, "development.json")
    with open(path, encoding="utf-8") as file:
        doc = json.load(file)
    doc["runs"]["A-IN"]["verdict"]["pass"] = True
    backtest.write_json(path, doc)
    first = backtest.holdout(db, settings, out, log=quiet)
    refused = False
    try:
        backtest.holdout(db, settings, out, log=quiet)
    except SystemExit:
        refused = True
    second = backtest.holdout(db, settings, out, again=True, log=quiet)
    with open(os.path.join(out, "holdout-log.json"), encoding="utf-8") as file:
        opened = json.load(file)
    check("the holdout runs once, refuses a second look, and logs a forced one as a repeat",
          first is not None and first["sessions"] == len(days) - 45 and refused and second["repeat"]
          and [item["repeat"] for item in opened] == [False, True])
    doc["plan_sha256"] = "0" * 64
    backtest.write_json(path, doc)
    changed = False
    try:
        backtest.holdout(db, settings, out, again=True, log=quiet)
    except SystemExit:
        changed = True
    check("the holdout refuses after plan.py changed", changed)


def main(argv):
    keep = "--keep" in argv                           # keep the fake data and the run, and say where
    folder = tempfile.mkdtemp(prefix="large-backtest-selftest-")
    try:
        check_rules()
        check_keys(folder)
        fake = FakeAlpaca(load_fake_market())
        server, url = serve(fake)
        try:
            db = check_fetch(fake, url, folder)
        finally:
            server.shutdown()
        check_sessions(db)
        check_trades(db)
        check_old_dates(db)
        check_end_to_end(fake, os.path.join(folder, "clean.sqlite"), folder)
    finally:
        if keep:
            print("kept in %s" % folder)
        else:
            shutil.rmtree(folder, ignore_errors=True)
    print()
    print("all checks passed" if not FAILURES else "%d failed: %s" % (len(FAILURES), "; ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
