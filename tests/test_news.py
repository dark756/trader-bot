import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Apple may have a way to dominate the smart home space")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 13, 19, 46, 52,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_sec_atom_8k(self):
        items = news.parse_sec_atom(raw("sec-8-K-0000320193.xml"), "AAPL")
        self.assertEqual([i["form"] for i in items], ["8-K/A", "8-K", "8-K"])
        earn = items[1]
        self.assertEqual(earn["items"], ["2.02", "9.01"])
        self.assertTrue(earn["important"])
        self.assertEqual(earn["title"], "Apple Inc. filed a 8-K: earnings results")
        self.assertEqual(earn["id"], "sec:0000320193-26-000018")
        self.assertIn("000032019326000018", earn["link"])
        # 16:30:28 New York time is 20:30:28 UTC
        self.assertEqual(earn["time"].isoformat(), "2026-07-30T20:30:28+00:00")
        self.assertEqual(items[0]["title"], "Apple Inc. filed a 8-K/A: director or officer change")

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-09-01T16:30:35-04:00").hour, 20)
        self.assertIsNone(news.parse_time("soon"))

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-KO.xml"), "KO")
        watch = {"KO": "Coca-Cola", "CVX": "Chevron", "AAPL": "Apple"}
        news.tag(items, watch)
        by = {i["title"][:20]: i for i in items}
        chevron = by["Coca-Cola or Chevron"]
        self.assertEqual(chevron["touches"], ["CVX", "KO"])
        self.assertIn("capital", chevron["tags"])  # dividend
        self.assertEqual(by["Is Coca-Cola (KO) St"]["touches"], ["KO"])

    def test_earnings_filings_are_tagged(self):
        items = news.tag(news.parse_sec_atom(raw("sec-8-K-0000021344.xml"), "KO"), {"KO": None})
        self.assertEqual(items[0]["tags"][0], "earnings")
        self.assertFalse(items[1]["important"])  # 8.01 other events

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        b = news.parse_rss(raw("yahoo-AAPL.xml"), "GOOGL")
        items = news.dedupe(news.tag(a + b, {"AAPL": None, "GOOGL": None}))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["touches"], ["AAPL", "GOOGL"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "AAPL", "KO", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 10)  # 6 Apple + 4 Coca-Cola headlines, filings are older
        code, out, _ = run(["--tickers", "AAPL", "KO", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "AAPL", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-07-30 16:30 ET  AAPL", out)  # 20:30 UTC, New York time
        self.assertIn("earnings results", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "KO", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "AAPL"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["AAPL", "KO"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"KO"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_sec_needs_a_user_agent(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            self.assertIsNone(news.sec_user_agent(None, "/nonexistent.json"))
            self.assertIsNone(news.sec_user_agent("Your Name you@example.com"))
            self.assertEqual(news.sec_user_agent("Ada ada@example.org"), "Ada ada@example.org")
            with self.assertRaises(news.NewsError):
                news.Fetcher(None).get("https://www.sec.gov/x", "x", sec=True)
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_the_reel_tsla_morning(self):
        # The reel's real feed: Yahoo Finance headlines for TSLA, Monday Oct 19 2026, checked at 10:15 AM ET
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--no-moves",
                              "--now", "2026-10-19T10:15:00-04:00"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-19 08:04 ET  TSLA", out)
        self.assertNotIn("10:32 ET", out)  # published after the pinned 10:15 clock
        self.assertIn("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", out)
        merger = [l for l in out.splitlines() if "Merger Mania" in l]
        tags = out.splitlines()[out.splitlines().index(merger[0]) + 1]
        self.assertIn("earnings", tags)
        self.assertIn("deal", tags)
        self.assertIn("Checked 2026-10-19 10:15 ET", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  2026-10-19")]), 16)  # 18 in the feed, 2 after 10:15
        # the price line, from the chart fetched at 10:52
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--dry-run",
                              "--now", "2026-10-19T10:52:00-04:00"] + self.base[:4])
        self.assertRegex(out, r"TSLA\s+366\.4[78] \+3\.31% vs yesterday's close 354\.72 at 10:52 ET  MOVING")

    def test_only_what_was_shown_is_remembered(self):
        tsla = ["--tickers", "TSLA", "--no-sec", "--no-moves", "--now", "2026-10-19 10:15"] + self.base[:4]
        code, out, err = run(["--hours", "1"] + tsla)
        self.assertEqual(code, 0, err)
        self.assertIn("09:51 ET", out)
        self.assertNotIn("08:04 ET", out)                  # outside the hour: not shown, so not seen
        code, out, _ = run(["--hours", "6"] + tsla)
        self.assertEqual(code, 0)
        self.assertIn("2026-10-19 08:04 ET  TSLA", out)    # still new in the wider window
        self.assertNotIn("09:51 ET", out)                  # shown once, then seen
        code, out, _ = run(["--hours", "6"] + tsla)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_now_without_an_offset_is_new_york_time(self):
        utc = datetime.datetime(2026, 10, 19, 14, 15, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.parse_now("2026-10-19 10:15"), utc)
        self.assertEqual(news.parse_now("2026-10-19T14:15:00Z"), utc)
        self.assertEqual(news.parse_now("2026-12-01 10:15").hour, 15)          # standard time
        code, out, _ = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--no-moves", "--dry-run",
                            "--now", "2026-10-19 10:15"] + self.base[:4])
        self.assertEqual(code, 0)
        self.assertIn("Checked 2026-10-19 10:15 ET", out)
        self.assertIn("08:04 ET", out)

    def test_a_bad_now_is_one_line(self):
        code, _, err = run(["--tickers", "TSLA", "--now", "noon", "--offline", DIR, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("--now wants a time like", err)
        self.assertNotIn("Traceback", err)

    def test_a_broken_file_is_a_note_not_a_traceback(self):
        wl, plan = os.path.join(self.tmp.name, "wl.json"), os.path.join(self.tmp.name, "plan.json")
        with open(wl, "w") as fh:
            fh.write("{not json")
        with open(plan, "w") as fh:
            json.dump(["TSLA"], fh)
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            fh.write("[1, 2")
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        args[args.index("--plan") + 1] = plan
        code, out, err = run(["--tickers", "KO", "--hours", "24", "--no-sec", "--no-moves"] + args)
        self.assertEqual(code, 0, err)
        self.assertIn("note: could not read %s" % wl, out)
        self.assertIn("note: skipped %s: it should be one JSON object" % plan, out)
        self.assertIn("started a fresh one, so items you have seen can show again", out)
        with open(self.state) as fh:
            self.assertIn("seen", json.load(fh))          # rewritten whole
        with open(os.path.join(self.tmp.name, "company_tickers.json"), "w") as fh:
            json.dump([{"ticker": "TSLA"}], fh)
        with self.assertRaises(news.NewsError) as e:
            news.cik_map(news.Fetcher(offline=self.tmp.name), {})
        self.assertIn("company_tickers.json is not the list it should be", str(e.exception))

    def test_a_pinned_clock_never_shows_a_later_price(self):
        data = raw("chart-TSLA.json")                       # fetched at 10:52
        m = news.parse_chart(data, "TSLA", news.parse_now("2026-10-19 10:15"))
        self.assertEqual((m["price"], news.to_eastern(m["time"]).strftime("%H:%M")), (364.18, "10:15"))
        self.assertEqual(news.parse_chart(data, "TSLA", news.parse_now("2026-10-19 10:52"))["price"],
                         news.parse_chart(data, "TSLA")["price"])
        with self.assertRaises(news.NewsError) as e:
            news.parse_chart(data, "TSLA", news.parse_now("2026-10-19 09:30"))
        self.assertIn("has no price by 09:30 ET", str(e.exception))
        code, out, _ = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--dry-run",
                            "--now", "2026-10-19 10:15"] + self.base[:4])
        self.assertIn("TSLA   364.18 +2.67% vs yesterday's close 354.72 at 10:15 ET", out)

    def test_moves(self):
        m = news.parse_chart(raw("chart-TSLA.json"), "TSLA")
        self.assertEqual(m["prev_close"], 354.72)
        self.assertEqual(m["pct"], 3.31)
        self.assertEqual(news.to_eastern(m["time"]).strftime("%H:%M"), "10:52")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "KO", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_new_york_time_follows_daylight_saving(self):
        utc = datetime.datetime(2026, 1, 15, 15, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EST, UTC-5
        utc = datetime.datetime(2026, 10, 19, 14, 15, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EDT, UTC-4
        utc = datetime.datetime(2026, 11, 1, 6, 30)       # 1:30 EST, just after the switch back
        self.assertEqual(news.to_eastern(utc).hour, 1)

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "TSLA",
                  "title": "Trading halted in XYZ after FDA approval; futures rise before CPI",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TSLA": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro"):
            self.assertIn(t, tags)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


class FakeAnswer(object):
    """Just enough of an HTTP response for body_of."""

    def __init__(self, data, gzipped=False):
        self.data, self.headers = data, {"Content-Encoding": "gzip"} if gzipped else {}

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]


class FetchedTextIsData(unittest.TestCase):
    def test_control_codes_and_direction_overrides_are_taken_out(self):
        # XML cannot carry most control codes at all; these are the ones it can: C1 (\x9b is a
        # one-byte terminal escape) and the right-to-left override
        rss = ("<rss><channel><item><title>Tesla \x9b2J beats\n  estimates \u202e</title>"
               "<link>https://x.example/a\x9b0m</link><guid>g1</guid><description>buy \u2066now</description>"
               "<pubDate>Mon, 19 Oct 2026 12:04:00 GMT</pubDate></item></channel></rss>").encode("utf-8")
        it = news.parse_rss(rss, "TSLA")[0]
        self.assertEqual(it["title"], "Tesla 2J beats estimates")
        self.assertEqual((it["link"], it["summary"]), ("https://x.example/a 0m", "buy now"))
        self.assertEqual(news.clean("a\x1b[2J\x07b"), "a [2J b")

    def test_xml_that_declares_entities_is_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
                b"<rss><channel><item><title>&b;</title></item></channel></rss>")
        for parse in (news.parse_rss, news.parse_sec_atom):
            with self.assertRaises(news.NewsError) as e:
                parse(bomb, "TSLA")
            self.assertIn("declares XML entities", str(e.exception))

    def test_answers_are_capped_zipped_and_unzipped(self):
        import gzip
        self.assertEqual(news.body_of(FakeAnswer(gzip.compress(b"<rss/>"), True), "u"), b"<rss/>")
        with self.assertRaises(news.NewsError) as e:
            news.body_of(FakeAnswer(b"x" * 101), "u", limit=100)
        self.assertIn("more than", str(e.exception))
        bomb = gzip.compress(b"\x00" * 5000)          # 5,000 bytes from a few dozen: a tiny zip bomb
        self.assertLess(len(bomb), 100)
        with self.assertRaises(news.NewsError) as e:
            news.body_of(FakeAnswer(bomb, True), "u", limit=1000)
        self.assertIn("unzips to more than", str(e.exception))
        with self.assertRaises(news.NewsError):
            news.body_of(FakeAnswer(b"not gzip", True), "u")

    def test_tickers_and_forms_are_checked_and_encoded(self):
        self.assertEqual(news.yahoo_symbol("brk.b"), "BRK-B")
        self.assertEqual(news.yahoo_symbol("A&S=X"), "A%26S%3DX")
        code, _, err = run(["--tickers", "TSLA&region=XX", "../etc", "--offline", DIR, "--no-sec", "--no-moves",
                            "--now", NOW, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)
        code, text, _ = run(["--tickers", "TSLA", "TSLA&x=1", "--forms", "8-K,8-K&type=4", "--offline", DIR,
                             "--no-moves", "--now", "2026-10-19T14:15:00Z", "--dry-run"])
        self.assertIn("note: skipped 'TSLA&X=1': not a ticker", text)
        self.assertIn("note: skipped form '8-K&TYPE=4': not an SEC form name", text)


if __name__ == "__main__":
    unittest.main()
