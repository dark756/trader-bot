# The large backtest: the plan, written before any data

Written 2026-11-03, before a single bar was downloaded. The commit that added this file is the proof of that. Every number here is also in [plan.py](plan.py), which the code reads, so the code can't drift from the plan.

> **Not financial advice.** This is a test of rules on past sessions with simulated fills. A rule that passes every check here has earned a paper-trading trial, nothing more.

## Why a plan first

The feature search tried 852 combinations on 59 sessions, and the best of them was luck: its in-sample and out-of-sample ranks didn't agree at all. Testing more ideas on the same sessions would make that worse. So this test fixes in advance:

- a few rules, chosen for a reason before seeing any result;
- the period, with the last part locked away;
- what counts as a pass.

The plan changes only before the first download. After that, a new idea needs a new plan, and later sessions to test it on.

## The data

Alpaca's market data (SIP feed, all US exchanges), from 2 Jan 2019 to 7 Aug 2026:

| period | sessions | what it is for |
| --- | --- | --- |
| 2 Jan 2019 to 29 Dec 2023 | about 1,258 | **development**: every rule is scored here, and here only |
| 2 Jan 2024 to 7 Aug 2026 | about 650 | **holdout**: locked; opened once, and only for the rules that pass development |
| 10 Aug 2026 on | | **not used**: the feature search already searched these sessions |

Bars are raw (as traded, not adjusted). The daily statistics come from split-adjusted daily bars converted to each day's raw units, so a split never distorts them. A session counts only if it has every 5-minute bar from 9:30 to 15:55. That drops the early-close days, and any stock-day with a halt or a 5-minute stretch with no trades.

## Two universes

- **Stocks in play (IN).** Each morning at 9:35, from the US stocks and ETFs on NYSE, Nasdaq, NYSE American, NYSE Arca and Cboe BZX:
  1. keep those whose previous close is at least $5, whose average volume over the 14 sessions before is at least 1,000,000 shares, and whose 14-session daily ATR is at least $0.50;
  2. score each by its **relative volume**: the dollar volume of today's 9:30 to 9:35 bar over its average for the same bar in the 14 sessions before (at least 10 of them needed);
  3. keep the 20 highest, if their relative volume is at least 1.

  Everything it uses is known at 9:35: nothing from later in the day. Dollar volume, not shares, so a split doesn't change it. Delisted stocks are included where Alpaca still lists them (as inactive), so the test isn't only of the survivors.
- **The fixed list (FX).** The feature search's 30 large stocks and ETFs, every session. This is the control: does picking the day's active stocks matter?

## Two rules, and their no-signal twins

Both are the pack's own tools (`entry.evaluate`, `exit.compute`, `size.size_trade`, `check.check`, the simulated paper broker), long only, market entries, entries from 9:45 to 15:30, $50 max loss, everything still open sold at 15:55.

| id | rule | why it is here |
| --- | --- | --- |
| **A** | the README rules: a 15-minute opening range, the stop at its middle, a 1R target | the pack's own example, on far more data than before |
| **B** | a 5-minute opening range, the stop at 10 % of the 14-session daily ATR under the signal, no real target (100R), held to 15:55 | the idea from published opening-range research: many small losses and a few trend days that pay for them |
| **A0, B0** | no signal at all: buy at 9:45 with A's or B's exits | the twin each rule has to beat, or its signal adds nothing |

A 2024 paper by Zarattini, Barbon and Aziz reported rule B's kind of trade on stocks in play. B is not a copy of their method (they also traded short, and entered differently); it is the closest the pack's long-only tools can come.

That makes **4 tested runs** (A-IN, A-FX, B-IN, B-FX) and 4 baselines.

## The accounts and costs

- **Every candidate.** Each trade is sized as if it were the only one in a $5,000 account. This measures the rule, trade by trade; the main scores come from it.
- **One account.** One $5,000 account that takes the day's first signal only (ties go to the higher relative volume, then the symbol), as one person would trade it. Reported, not used for the verdict.
- **Costs.** 1 cent a share each way. Also reported at 0 and 3 cents.
- **Alpaca's rule.** A market bracket whose stop is already at or above the fill price is rejected, as Alpaca does.

## What counts as a pass

A tested run passes development only if all five hold, at 1 cent a share:

1. its net over development is above zero;
2. the t-stat of its R per trade is at least **2.24** (a one-sided p of 0.0125: 0.05 split over the 4 tests);
3. it made money in at least **4 of the 5** development years;
4. it still makes money at **3 cents** a share;
5. its R per trade beats its no-signal twin's.

**The walk-forward.** One more procedure, scored the same way from 2020 on (rules 1 to 4): each month, trade whichever of the 4 tested runs had the best R per trade over the 12 months before (at least 30 trades, else stay out that month). This is the honest version of "pick the best": it only ever chooses from the past.

**The holdout** opens only if a run or the walk-forward passes. It runs once, and the code logs every time it is opened. A passing run holds up if, on the holdout, at 1 cent a share, its net is above zero and its t-stat is at least **2.0**. If nothing passes development, the holdout stays locked, and fresh for the next plan.

## Reported but not judged

- the deflated Sharpe ratio over the 4 tested runs;
- each year on its own;
- a 90 % range for the net, resampling whole days;
- results by price ($5 to $20, $20 to $100, above $100), because 1 cent a share is kind to cheap stocks with wide spreads;
- the exits (stop, target, 15:55) and the rejected orders.

## What this test can't tell you

- **The fills are simulated** on 5-minute bars: at the next bar's open, with no spread beyond the per-share cost, and no partial fills.
- **There's no short side**, because the pack trades long only.
- **The universe has gaps.** It holds stocks Alpaca still lists, active or inactive; some delisted stocks may be missing, and a ticker that changed hands may carry another company's history.
- **The future is different.** A pass says these rules held on these years. Paper trading comes before anything else.

## Not in this plan

- The Time Machine filter, the one choice that helped in the feature search. It needs 20 earlier sessions of 5-minute bars for every trade, so it is left for a second plan once this one has a result.
- Any other stop, target, opening range or filter. Testing more of them here would bring back the problem this plan is for.
