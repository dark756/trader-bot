"""Download the bars the feature search runs on: 60 days of 5-minute bars per symbol from Yahoo,
saved as the chart JSON the tools read with --offline (data/<SYMBOL>.json.gz).

    python -B paper-trading-scenarios/feature-search/fetch_data.py            # the universe below
    python -B paper-trading-scenarios/feature-search/fetch_data.py AAPL MSFT  # some

Yahoo serves 5-minute bars for the last 60 days only, so a later run downloads a later window.
Standard library only.
"""

import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
# liquid US names across sectors, plus three index ETFs, all priced so a $5,000 account can buy a few shares
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "NFLX",
            "PLTR", "COIN", "MU", "INTC", "UBER", "JPM", "BAC", "XOM", "CVX", "UNH",
            "LLY", "PFE", "WMT", "COST", "KO", "NKE", "DIS", "SPY", "QQQ", "IWM"]
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def fetch(symbol):
    path = "/v8/finance/chart/%s?interval=5m&range=60d&includePrePost=false" % urllib.parse.quote(symbol, safe="")
    last_error = None
    for attempt in range(4):
        for host in HOSTS:
            try:
                request = urllib.request.Request("https://" + host + path, headers={
                    "User-Agent": "Mozilla/5.0 (day-trader-skill feature search)", "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    text = response.read().decode("utf-8")
                result = json.loads(text)["chart"]["result"][0]
                if not result.get("timestamp"):
                    raise ValueError("no bars")
                return text, result
            except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
                last_error = error
        time.sleep(2 ** attempt)
    raise SystemExit("%s: %s" % (symbol, last_error))


def main(argv):
    os.makedirs(DATA, exist_ok=True)
    for symbol in argv or UNIVERSE:
        text, result = fetch(symbol)
        with gzip.open(os.path.join(DATA, symbol + ".json.gz"), "wt", encoding="utf-8") as file:
            file.write(text)
        stamps = result["timestamp"]
        first, last = (time.strftime("%Y-%m-%d", time.gmtime(stamp)) for stamp in (stamps[0], stamps[-1]))
        print("%-6s %5d bars  %s to %s  last %s" % (symbol, len(stamps), first, last,
                                                   result["meta"].get("regularMarketPrice")))
        time.sleep(0.5)


if __name__ == "__main__":
    main(sys.argv[1:])
