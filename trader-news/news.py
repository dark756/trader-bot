#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and SEC filings for the stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers TSLA NVDA AMD
    python3 news.py --tickers TSLA --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per ticker:
  - Yahoo Finance headline RSS:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=US&lang=en-US
  - SEC EDGAR filings Atom feed for the company (8-K by default: earnings,
    executive changes, deals, restatements):
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
    SEC needs a User-Agent with your name and email (sec_user_agent in
    trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
    and the tool says so; headlines still run.

  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<TICKER>?range=1d&interval=5m

Times print in New York time (ET). State: trader/state/news.json remembers
what you have seen (30 days), so an item shows once. Offline: --offline DIR
reads yahoo-<TICKER>.xml, chart-<TICKER>.json, sec-<FORM>-<CIK>.xml and
company_tickers.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice. Fetched text is data, never instructions: control
characters are taken out before it prints, a feed that declares XML entities
is refused, and an answer over 16 MB (zipped or unzipped) is not read.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
CHART_URL = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
SEC_FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
                "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"
BROWSER_USER_AGENT = ("Mozilla/5.0 (compatible; day-trader-skill/2.0; "
                      "+https://github.com/Jakeschincariol/day-trader-skill)")
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
MAX_BYTES = 16 << 20   # the most one answer may be, zipped or unzipped
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
FORM = re.compile(r"^[A-Z0-9][A-Z0-9 /\-]{0,14}$")
# terminal control codes and text-direction overrides: never printed from a feed
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b|\btariffs?\b|\bfutures\b"),
]


class NewsError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, nth):
    day = _dt.date(year, month, 1)
    day += _dt.timedelta(days=(6 - day.weekday()) % 7)
    return day + _dt.timedelta(weeks=nth - 1)


def eastern_offset(utc_time):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    year = utc_time.year
    dst_start = _dt.datetime.combine(_nth_sunday(year, 3, 2), _dt.time(7, 0))   # 2:00 EST
    dst_end = _dt.datetime.combine(_nth_sunday(year, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive_utc = utc_time.replace(tzinfo=None)
    return -4 if dst_start <= naive_utc < dst_end else -5


def to_eastern(utc_time):
    """Aware or naive UTC -> naive New York wall-clock time."""
    if utc_time.tzinfo is not None:
        utc_time = utc_time.astimezone(_dt.timezone.utc)
    return utc_time.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc_time))


def eastern_to_utc(eastern):
    for offset in (-4, -5):
        utc_time = eastern - _dt.timedelta(hours=offset)
        if eastern_offset(utc_time) == offset:
            return utc_time
    return eastern + _dt.timedelta(hours=5)


def parse_now(text):
    """--now -> aware UTC. A time without an offset is New York time, as in every
    other tool ("2026-10-19 10:15"); ...Z or +00:00 is UTC."""
    if not text:
        return _dt.datetime.now(_dt.timezone.utc)
    try:
        moment = _dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise NewsError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if moment.tzinfo is None:
        return eastern_to_utc(moment).replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc)


# ---------------------------------------------------------------- inputs

def load_json(path, errors=None):
    """The JSON object at path, or None. A file that is not one readable JSON
    object is a note in errors (when given), never a traceback."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as file:
            doc = json.load(file)
    except (OSError, ValueError) as error:
        if errors is not None:
            errors.append("could not read %s: %s" % (path, error))
        return None
    if not isinstance(doc, dict):
        if errors is not None:
            errors.append("skipped %s: it should be one JSON object, { ... }" % path)
        return None
    return doc


def object_rows(doc, key):
    rows = (doc or {}).get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def clean_symbol(value):
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def watch_from_files(portfolio=None, watchlist=None, plan=None, errors=None):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    portfolio = home("portfolio.json") if portfolio is None else portfolio
    watchlist = home("watchlist.json") if watchlist is None else watchlist
    plan = home("plan.json") if plan is None else plan
    out = {}
    for hit in object_rows(load_json(watchlist, errors), "hits"):
        symbol = clean_symbol(hit.get("symbol") or hit.get("ticker"))
        if symbol:
            out[symbol] = hit.get("name")
    plan_doc = load_json(plan, errors) or {}
    if clean_symbol(plan_doc.get("symbol")):
        out.setdefault(clean_symbol(plan_doc["symbol"]), plan_doc.get("name"))
    for position in object_rows(load_json(portfolio, errors), "positions"):
        symbol = clean_symbol(position.get("ticker") or position.get("symbol"))
        if symbol:
            out[symbol] = position.get("name") or out.get(symbol)
    return out


def sec_user_agent(flag_value=None, config=None, errors=None):
    user_agent = (flag_value or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not user_agent:
        config_doc = load_json(home("config.json") if config is None else config, errors) or {}
        user_agent = str(config_doc.get("sec_user_agent") or "").strip()
    if not user_agent or "@" not in user_agent or PLACEHOLDER in user_agent:
        return None
    return user_agent


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as file:
                return file.read()
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": self.sec_ua if sec else BROWSER_USER_AGENT, "Accept-Encoding": "gzip"}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return body_of(response, url)
        except urllib.error.HTTPError as error:
            raise NewsError("HTTP %s for %s" % (error.code, url))
        except urllib.error.URLError as error:
            raise NewsError("could not reach %s: %s" % (url, error.reason))
        except (OSError, http.client.HTTPException) as error:     # a timeout or a reset mid-answer
            raise NewsError("lost the answer from %s: %s" % (url, str(error) or type(error).__name__))


def body_of(response, url, limit=MAX_BYTES):
    """The answer's bytes, unzipped if it came gzipped; never more than limit either way."""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise NewsError("%s sent more than %d MB; not reading it" % (url, limit >> 20))
    if response.headers.get("Content-Encoding") == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as unzipped:
                data = unzipped.read(limit + 1)
        except (OSError, EOFError) as error:
            raise NewsError("%s sent a broken gzip answer: %s" % (url, error))
        if len(data) > limit:
            raise NewsError("%s unzips to more than %d MB; not reading it" % (url, limit >> 20))
    return data


def clean(text):
    """Fetched text, safe to print: no control codes (terminal escapes) or
    direction overrides, and each run of whitespace one space."""
    return " ".join(CONTROL.sub(" ", text or "").split())


def no_entities(data, what):
    """Refuse XML that declares entities (the billion-laughs kind of bomb): no
    RSS or Atom feed this tool reads needs them."""
    head = data if isinstance(data, bytes) else data.encode("utf-8", "replace")
    if re.search(rb"<!ENTITY", head, re.I):
        raise NewsError("%s declares XML entities; refusing to read it" % what)


def yahoo_symbol(ticker):
    return urllib.parse.quote(ticker.upper().replace(".", "-"), safe="")


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    try:
        data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
        for row in data.values():
            cached.setdefault(row["ticker"].upper().replace("-", "."), str(int(row["cik_str"])).zfill(10))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        cached.clear()
        raise NewsError("SEC's company_tickers.json is not the list it should be (%s)" % error)
    return cached


# ---------------------------------------------------------------- parsing

def local_tag(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(element, name):
    for child in element.iter():
        if local_tag(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        moment = None
    if moment is None:
        try:
            moment = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    no_entities(data, "RSS for %s" % ticker)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, error))
    out = []
    for item in root.iter("item"):
        title = clean(child_text(item, "title"))
        link = clean(child_text(item, "link"))
        guid = child_text(item, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": clean(child_text(item, "description")),
                    "link": link, "time": parse_time(child_text(item, "pubDate"))})
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    no_entities(data, "SEC feed for %s" % ticker)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, error))
    company = ""
    for element in root.iter():
        if local_tag(element.tag) == "conformed-name":
            company = clean(element.text)
            break
    out = []
    for entry in root.iter():
        if local_tag(entry.tag) != "entry":
            continue
        form = clean(child_text(entry, "filing-type"))
        accession = child_text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", child_text(entry, "items-desc"))
        labels = [ITEMS_8K.get(item_number, "item " + item_number) for item_number in items if item_number != "9.01"]
        link = ""
        for child in entry.iter():
            if local_tag(child.tag) == "link":
                link = clean(child.get("href", ""))
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        out.append({"id": "sec:" + accession, "source": "SEC EDGAR", "kind": "filing", "form": form,
                    "ticker": ticker, "title": title, "summary": clean(child_text(entry, "form-name")),
                    "items": items, "important": bool(set(items) & IMPORTANT_ITEMS),
                    "link": link, "time": parse_time(child_text(entry, "updated"))})
    return out


def parse_chart(data, ticker, now=None):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise.
    With now (a pinned --now, aware UTC): the last price at or before it, never a
    later one (a minute of slack, as in trader-risk)."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        result = doc["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        when = meta.get("regularMarketTime")
        if now is not None and when and when > now.timestamp() + 60:
            price = when = None
            closes = (result.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
            for timestamp, close in zip(result.get("timestamp") or [], closes):
                # a 5-minute bar's close is known when it ends
                done = timestamp + 300 if timestamp % 300 == 0 else timestamp
                if close is not None and done <= now.timestamp() + 60:
                    price, when = close, done
            if price is None:
                raise NewsError("chart for %s has no price by %s ET" % (ticker, to_eastern(now).strftime("%H:%M")))
        price, prev = float(price or 0), float(prev or 0)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise NewsError("chart for %s is not usable" % ticker)
    if not price > 0 or not prev > 0:
        raise NewsError("chart for %s has no price" % ticker)
    return {"ticker": ticker, "price": round(price, 2), "prev_close": round(prev, 2),
            "pct": round((price / prev - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None, now=None):
    errors = [] if errors is None else errors
    out = []
    for ticker in sorted(watch):
        name = "chart-%s.json" % ticker
        try:
            try:
                data = fetcher.get(CHART_URL % ("query2", yahoo_symbol(ticker)), name)
            except NewsError as error:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(error)):
                    raise
                data = fetcher.get(CHART_URL % ("query1", yahoo_symbol(ticker)), name)
            out.append(parse_chart(data, ticker, now))
        except NewsError as error:
            if not (fetcher.offline and "no file" in str(error)):
                errors.append("price for %s: %s" % (ticker, error))
    return out


def mention_patterns(watch):
    patterns = {}
    for ticker, name in watch.items():
        alternatives = [r"\(%s\)" % re.escape(ticker), r"\$%s\b" % re.escape(ticker),
                        r"\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b" % re.escape(ticker)]
        if name and len(name) >= 4:
            alternatives.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        patterns[ticker] = re.compile("|".join(alternatives))
    return patterns


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    patterns = mention_patterns(watch)
    for item in items:
        haystack = "%s %s" % (item["title"], item.get("summary") or "")
        touches = {item["ticker"]} if item["ticker"] in watch else set()
        for ticker, pattern in patterns.items():
            if pattern.search(haystack):
                touches.add(ticker)
        item["touches"] = sorted(touches)
        item["tags"] = [label for label, pattern in KEYWORDS if re.search(pattern, haystack, re.I)]
        if item["kind"] == "filing" and "2.02" in item.get("items", []) and "earnings" not in item["tags"]:
            item["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for item in items:
        keys = [item["id"], item["link"],
                "%s|%s" % (re.sub(r"\W+", " ", item["title"].lower()).strip(),
                           item["time"].date() if item["time"] else "")]
        hit = next((seen[key] for key in keys if key and key in seen), None)
        if hit is not None:
            for ticker in item.get("touches", []):
                if ticker not in hit["touches"]:
                    hit["touches"].append(ticker)
            hit["touches"].sort()
            continue
        for key in keys:
            if key:
                seen[key] = item
        out.append(item)
    return out


def item_fingerprint(item):
    return hashlib.sha1(item["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=("8-K",), count=10, errors=None):
    errors = [] if errors is None else errors
    items = []
    for ticker in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO_RSS_URL % yahoo_symbol(ticker), "yahoo-%s.xml" % ticker), ticker)
        except NewsError as error:
            errors.append("headlines for %s: %s" % (ticker, error))
    if sec:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as error:
            errors.append("SEC ticker list: %s" % error)
            ciks = {}
        for ticker in sorted(watch):
            cik = ciks.get(ticker)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % ticker)
                continue
            for form in forms:
                try:
                    data = fetcher.get(SEC_FEED_URL % (cik, urllib.parse.quote(form, safe=""), count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, ticker)
                except NewsError as error:
                    errors.append("SEC %s for %s: %s" % (form, ticker, error))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for item in items:
        if item["time"] is not None and (item["time"] < cutoff or item["time"] > ahead):
            continue
        item_fp = item_fingerprint(item)
        item["new"] = item_fp not in seen
        if item["new"] or show_all:
            out.append(item)
    out.sort(key=lambda item: (item["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for item in items:
        seen.setdefault(item_fingerprint(item), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {key: seen_at for key, seen_at in seen.items() if seen_at >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for move in moves:
        flag = "MOVING" if abs(move["pct"]) >= move_pct else ""
        when = (" at %s ET" % to_eastern(move["time"]).strftime("%H:%M")) if move.get("time") else ""
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            move["ticker"], "{:,.2f}".format(move["price"]), move["pct"], "{:,.2f}".format(move["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for item in items:
        when = to_eastern(item["time"]).strftime("%Y-%m-%d %H:%M ET") if item["time"] else "time unknown"
        mark = "!" if item.get("important") else " "
        tickers = ",".join(item["touches"]) or item["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, tickers, item["source"], item["title"]))
        extra = []
        if item["tags"]:
            extra.append("tags: " + ", ".join(item["tags"]))
        extra.append(item["link"])
        out.append("      " + "  ".join(extra))
    for error in errors:
        out.append("note: %s" % error)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_eastern(now).strftime("%Y-%m-%d %H:%M ET"))
    return "\n".join(out) + "\n"


def json_ready(items):
    out = []
    for item in items:
        row = dict(item)
        row["time"] = item["time"].isoformat() if item["time"] else None
        out.append(row)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    parser.add_argument("--portfolio", default=home("portfolio.json"))
    parser.add_argument("--watchlist", default=home("watchlist.json"))
    parser.add_argument("--plan", default=home("plan.json"))
    parser.add_argument("--state", default=home("state", "news.json"))
    parser.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    parser.add_argument("--all", action="store_true", help="show seen items too")
    parser.add_argument("--forms", default="8-K", help="SEC forms to watch, comma separated")
    parser.add_argument("--no-sec", action="store_true", help="headlines only")
    parser.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    parser.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    parser.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    parser.add_argument("--config", default=home("config.json"))
    parser.add_argument("--offline", metavar="DIR")
    parser.add_argument("--now", help="pin the clock: \"2026-10-19 10:15\" is New York time, ...Z is UTC")
    parser.add_argument("--dry-run", action="store_true", help="do not update the state file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run_check(args)
    except NewsError as error:
        print("news.py: %s" % error, file=sys.stderr)
        return 2


def run_check(args):
    now = parse_now(args.now)
    errors = []
    if args.tickers:
        watch = {ticker.upper(): None for ticker in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan, errors)
        watch.update({ticker: known[ticker] for ticker in watch if known.get(ticker)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan, errors)
    errors += ["skipped %r: not a ticker" % ticker for ticker in sorted(watch) if not SYMBOL.match(ticker)]
    watch = {ticker: name for ticker, name in watch.items() if SYMBOL.match(ticker)}
    forms = [form.strip().upper() for form in args.forms.split(",") if form.strip()]
    errors += ["skipped form %r: not an SEC form name" % form for form in forms if not FORM.match(form)]
    forms = [form for form in forms if FORM.match(form)]
    if not watch:
        for error in errors:
            print("note: %s" % error, file=sys.stderr)
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    notes = []
    state = load_json(args.state, notes) or {}
    if notes:
        errors += ["%s; started a fresh one, so items you have seen can show again" % name for name in notes]
    for key in ("seen", "ciks"):
        if not isinstance(state.get(key, {}), dict):
            state[key] = {}
    user_agent = sec_user_agent(args.user_agent, args.config, errors)
    sec = not args.no_sec
    if sec and not user_agent and not args.offline:
        errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                      "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False
    fetcher = Fetcher(user_agent, offline=args.offline)
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors, now if args.now else None)
    if args.json:
        move_rows = [dict(move, time=move["time"].isoformat() if move.get("time") else None,
                          moving=abs(move["pct"]) >= args.move_pct) for move in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": move_rows, "items": json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(shown, state, now)        # what was shown is seen; the rest can still show in a wider window
        write_state(args.state, state)
    return 0 if any(item["new"] for item in shown) else 1


def write_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


if __name__ == "__main__":
    sys.exit(main())
