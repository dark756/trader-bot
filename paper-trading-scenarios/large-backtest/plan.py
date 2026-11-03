"""Every number the pre-registered plan (PLAN.md) fixes, in one place.

Change this file only before the data is fetched, and commit the change. After the first fetch, a new idea
gets a new plan, because these sessions will have been looked at. Standard library only.
"""

# ---------------------------------------------------------------- the periods (New York dates)

DAILY_FROM = "2018-11-01"        # daily bars from here: the 14-session lookbacks need history before 2019
OPENING_FROM = "2018-12-03"      # opening bars from here: relative volume needs 14 sessions before 2019
DEVELOPMENT = ("2019-01-02", "2023-12-29")
HOLDOUT = ("2024-01-02", "2026-08-07")
# From 2026-08-10 on is left out on purpose: the feature search already searched those sessions.

# ---------------------------------------------------------------- the universe

EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "ARCA", "NYSEARCA", "BATS")   # for active assets; inactive ones on any
SYMBOL_PATTERN = r"^[A-Z]{1,5}(\.[A-Z])?$"                 # no warrants (.WS), units (.U) or preferreds
NAME_EXCLUDE = r"\b(warrants?|rights?|units?|preferred)\b|%"
LOOKBACK = 14                    # sessions, for the ATR, the average volume and the relative volume
MIN_PRICE = 5.0                  # the previous close, in dollars
MIN_AVG_VOLUME = 1000000         # shares a day, over the 14 sessions before
MIN_ATR = 0.50                   # dollars, the 14-session average true range of the daily bars

IN_PLAY_TOP = 20                 # stocks in play: the 20 with the highest relative volume each morning
MIN_RELATIVE_VOLUME = 1.0        # the 9:30 bar's dollar volume over its 14-session average, at least this
MIN_PRIOR_OPENINGS = 10          # of the 14 sessions before, at least this many with a 9:30 bar

# The control list: the feature search's 30 stocks and ETFs, unchanged
FIXED = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "NFLX",
         "PLTR", "COIN", "MU", "INTC", "UBER", "JPM", "BAC", "XOM", "CVX", "UNH",
         "LLY", "PFE", "WMT", "COST", "KO", "NKE", "DIS", "SPY", "QQQ", "IWM"]

# ---------------------------------------------------------------- the rules tested

# "atr-daily:K" is this plan's stop: K times the 14-session daily ATR under the signal price, handed to the
# pack's exit.compute as a price stop. Everything else is the pack's own syntax.
CONFIGS = {
    "A": {"name": "the README rules", "setup": "orb", "or_minutes": 15, "stop": "or-mid", "target": "1R",
          "entry": "market", "volume": None, "cutoff": "15:30", "risk": 50},
    "B": {"name": "a 5-minute breakout held to the close", "setup": "orb", "or_minutes": 5,
          "stop": "atr-daily:0.1", "target": "100R", "entry": "market", "volume": None, "cutoff": "15:30",
          "risk": 50},
}
# The no-signal twin of each: buy every stock at 9:45 (the first entry the config allows). B0 keeps B's exits.
# A0 keeps A's opening range and 1R target but stops at the range's low: A's stop, the range's middle, can sit
# above the 9:45 price, and buying only when it doesn't would be a signal of its own.
CONFIGS["A0"] = dict(CONFIGS["A"], name="no signal, A's range and target", stop="or-low")
CONFIGS["B0"] = dict(CONFIGS["B"], name="no signal, B's exits")
BASELINE_OF = {"A": "A0", "B": "B0"}
UNIVERSES = {"IN": "stocks in play", "FX": "the fixed list of 30"}
TESTED = ["A-IN", "A-FX", "B-IN", "B-FX"]
BASELINES = ["A0-IN", "A0-FX", "B0-IN", "B0-FX"]

# ---------------------------------------------------------------- costs and accounts

COST = 0.01                      # dollars a share, each way (spread and slippage)
COST_STRESS = (0.0, 0.01, 0.03)
START_CASH = 5000.0              # every trade is sized as if it were the only one in a $5,000 account

# ---------------------------------------------------------------- the verdicts

T_MIN = 2.24                     # t-stat of R per trade: one-sided p of 0.0125, which is 0.05 over 4 tests
MIN_YEARS_UP = 4                 # of the 5 development years
HOLDOUT_T_MIN = 2.0
WALK_FORWARD_MONTHS = 12         # trailing months that choose next month's rule
WALK_FORWARD_MIN_TRADES = 30     # in that window, or the month stays in cash
