"""The large backtest's universe rules (PLAN.md): which assets, the daily statistics, and the stocks in play.

Pure functions on plain data, so the self-test can check them on made-up series. Standard library only.
"""

import collections
import re

import plan


def keep_asset(asset, status):
    """An Alpaca asset worth fetching: a US equity, a plain ticker, not a warrant, right, unit or preferred.
    Active assets must trade on a listed exchange; inactive (delisted) ones may sit anywhere, often OTC."""
    if not isinstance(asset, dict):
        return False
    symbol = str(asset.get("symbol") or "")
    name = str(asset.get("name") or "")
    if asset.get("class", "us_equity") != "us_equity" or not re.match(plan.SYMBOL_PATTERN, symbol):
        return False
    if re.search(plan.NAME_EXCLUDE, name.lower()):
        return False
    return status != "active" or asset.get("exchange") in plan.EXCHANGES


def daily_features(raw, split, lookback=plan.LOOKBACK):
    """One symbol's daily bars, raw and split-adjusted, each {date: (open, high, low, close, volume)} ->
    {date: {"factor", "prev_close", "atr", "avg_volume"}} for each date with `lookback` sessions before it.

    factor is today's raw open over today's split-adjusted open: a split is known before the open, so this only
    converts units. The statistics come from the split-adjusted bars of the `lookback` sessions before today and
    are put in today's raw units, so a split inside the window never distorts them."""
    dates = sorted(day for day in split if day in raw)
    out = {}
    for index in range(lookback, len(dates)):
        day = dates[index]
        raw_open, split_open = raw[day][0], split[day][0]
        if not raw_open or not split_open or raw_open <= 0 or split_open <= 0:
            continue
        factor = raw_open / split_open
        true_ranges, volumes = [], []
        for position in range(index - lookback, index):
            _, high, low, close, volume = split[dates[position]]
            true_range = high - low
            if position > 0:
                previous_close = split[dates[position - 1]][3]
                true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
            true_ranges.append(true_range)
            volumes.append(volume)
        out[day] = {"factor": factor, "prev_close": split[dates[index - 1]][3] * factor,
                    "atr": sum(true_ranges) / lookback * factor, "avg_volume": sum(volumes) / lookback / factor}
    return out


def is_liquid(features):
    return (features["prev_close"] >= plan.MIN_PRICE and features["avg_volume"] >= plan.MIN_AVG_VOLUME
            and features["atr"] >= plan.MIN_ATR)


class InPlay(object):
    """The stocks in play, one session at a time, oldest first (early closes included: they count as sessions).

    step(liquid, openings) takes the symbols liquid today and today's 9:30 bar dollar volumes {symbol: dollars}
    and returns today's picks [(symbol, relative volume), ...], best first. Relative volume is today's dollar
    volume over the average of the same bar in the `lookback` sessions before (at least `min_prior` of them).
    Two symbols with the very same bar are one company under two tickers: only the first, by symbol, counts."""

    def __init__(self, top=plan.IN_PLAY_TOP, min_relative=plan.MIN_RELATIVE_VOLUME,
                 min_prior=plan.MIN_PRIOR_OPENINGS, lookback=plan.LOOKBACK):
        self.top, self.min_relative, self.min_prior, self.lookback = top, min_relative, min_prior, lookback
        self.history = {}
        self.index = 0

    def step(self, liquid, openings):
        ranked, seen = [], set()
        for symbol in sorted(liquid):
            dollars = openings.get(symbol)
            if not dollars or dollars <= 0 or dollars in seen:
                continue
            seen.add(dollars)
            prior = [value for at, value in self.history.get(symbol, ())
                     if self.index - self.lookback <= at < self.index]
            if len(prior) < self.min_prior:
                continue
            average = sum(prior) / len(prior)
            if average <= 0:
                continue
            relative = dollars / average
            if relative >= self.min_relative:
                ranked.append((-relative, symbol))
        ranked.sort()
        for symbol, dollars in openings.items():
            if dollars and dollars > 0:
                self.history.setdefault(symbol, collections.deque(maxlen=self.lookback)).append((self.index,
                                                                                                 dollars))
        self.index += 1
        return [(symbol, -negative) for negative, symbol in ranked[:self.top]]
