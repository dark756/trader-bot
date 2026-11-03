"""A small client for Alpaca's REST API: market data bars, the asset list and the market calendar.

Read-only: it never touches orders or the account. The keys come from the environment, never from a file:
APCA_API_KEY_ID and APCA_API_SECRET_KEY (Alpaca's own names), or ALPACA_API_KEY and ALPACA_SECRET_KEY (the
names Alpaca's MCP server uses). Paper keys are enough. Standard library only.
"""

import calendar
import collections
import datetime
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"
PAGE_LIMIT = 10000                   # the most bars Alpaca sends in one page
TRIES = 8


class AlpacaError(Exception):
    pass


def keys_from_environment():
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise AlpacaError("no Alpaca keys: set APCA_API_KEY_ID and APCA_API_SECRET_KEY (your paper keys, from the "
                          "Alpaca dashboard) in this terminal first; see the README")
    return key, secret


def epoch_of(stamp):
    """Alpaca's RFC 3339 time ("2021-01-04T14:30:00Z") -> Unix seconds."""
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = text[:-1]
    elif len(text) > 6 and text[-6] in "+-" and text[-3] == ":":
        offset = (int(text[-5:-3]) * 60 + int(text[-2:])) * (1 if text[-6] == "+" else -1)
        return epoch_of(text[:-6]) - offset * 60
    text = text.split(".")[0]
    moment = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(moment.timetuple())


def rfc3339(moment):
    """A naive UTC datetime -> "2021-01-04T14:30:00Z"."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Client(object):
    def __init__(self, key, secret, data_url=DATA_URL, trading_url=TRADING_URL, per_minute=180, feed="sip",
                 log=print, max_requests=None):
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
                        "User-Agent": "day-trader-skill-large-backtest/1.0"}
        self.data_url = data_url.rstrip("/")
        self.trading_url = trading_url.rstrip("/")
        self.per_minute = per_minute
        self.feed = feed
        self.log = log
        self.max_requests = max_requests      # for the self-test: stop after this many, as a crash would
        self.requests = 0
        self.stamps = collections.deque()

    def wait_turn(self):
        """Keep under per_minute requests in any 60 seconds (Alpaca's free plan allows 200)."""
        while True:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] >= 60:
                self.stamps.popleft()
            if len(self.stamps) < self.per_minute:
                self.stamps.append(now)
                return
            time.sleep(60 - (now - self.stamps[0]) + 0.05)

    def get(self, base, path, params):
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise KeyboardInterrupt("the self-test's request budget is spent")
        url = base + path + "?" + urllib.parse.urlencode(params, safe=",:")
        last = None
        for attempt in range(TRIES):
            self.wait_turn()
            self.requests += 1
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                body = error.read()[:400].decode("utf-8", "replace").strip()
                if error.code == 429 or error.code >= 500:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = min(60.0, 2.0 ** attempt)
                    self.log("  Alpaca answered HTTP %d; trying again in %.0f s" % (error.code, wait))
                    time.sleep(wait)
                    last = "HTTP %d" % error.code
                    continue
                if error.code == 401:
                    raise AlpacaError("Alpaca refused the keys (HTTP 401): check that APCA_API_KEY_ID and "
                                      "APCA_API_SECRET_KEY hold your paper keys")
                if error.code == 403 and "subscription" in body.lower():
                    raise AlpacaError("your Alpaca plan does not allow this feed (HTTP 403: %s); try --feed iex" % body)
                raise AlpacaError("Alpaca answered HTTP %d for %s: %s" % (error.code, path, body))
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
                wait = min(60.0, 2.0 ** attempt)
                self.log("  could not reach Alpaca (%s); trying again in %.0f s" % (error, wait))
                time.sleep(wait)
                last = str(error) or type(error).__name__
        raise AlpacaError("gave up on %s after %d tries (%s)" % (path, TRIES, last))

    def bars(self, symbols, timeframe, start, end, adjustment):
        """Every bar for these symbols between start and end (RFC 3339, both inclusive), every page:
        {symbol: [{"t", "o", "h", "l", "c", "v", "vw", ...}, ...]}, oldest first."""
        out = {}
        token = None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
                      "limit": PAGE_LIMIT, "adjustment": adjustment, "feed": self.feed, "sort": "asc"}
            if token:
                params["page_token"] = token
            doc = self.get(self.data_url, "/v2/stocks/bars", params)
            if not isinstance(doc, dict):
                raise AlpacaError("Alpaca's bars answer is not the shape this tool knows")
            for symbol, rows in (doc.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(rows or [])
            token = doc.get("next_page_token")
            if not token:
                return out

    def assets(self, status):
        doc = self.get(self.trading_url, "/v2/assets", {"status": status, "asset_class": "us_equity"})
        if not isinstance(doc, list):
            raise AlpacaError("Alpaca's asset list is not the shape this tool knows")
        return doc

    def calendar(self, start, end):
        doc = self.get(self.trading_url, "/v2/calendar", {"start": start, "end": end})
        if not isinstance(doc, list):
            raise AlpacaError("Alpaca's calendar is not the shape this tool knows")
        return doc
