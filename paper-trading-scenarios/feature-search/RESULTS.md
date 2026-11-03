# Feature search: which of the bot's choices made money on 30 stocks

> **Not financial advice.** A replay of 30 US stocks and ETFs over 59 sessions (2026-08-11 to 2026-11-02) with simulated fills. It says what these rules did on these days, not what they will do next.

## The answer in short

- **No combination showed a real edge.** Of 832 combinations of the bot's choices, 0.8 % made money in-sample (2026-08-11 to 2026-10-05) after costs and 36.8 % out-of-sample (2026-10-06 to 2026-11-02). **0 made money in both halves.**
- **Picking the in-sample winner did not help.** The rank of a combination's in-sample Sharpe ratio and its out-of-sample one correlate at -0.04 (0 is no relation). The in-sample winner of 8 of the 11 categories was the same combination, 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00: +$317.58 in-sample, -$290.46 out-of-sample.
- **The best in-sample Sharpe ratio, 1.25, is within luck.** From 39 sessions a Sharpe ratio's own standard error is about 2.5 (annualized), so one try lands that high by chance easily, and this was the best of 832. Its deflated Sharpe ratio, the chance it is real after that many tries, is 3.5 %.
- **The typical trade lost money.** The median combination averaged -0.071R a trade over the whole period after costs (-0.053R before costs), where 1R is the loss the risk check allowed at the stop.
- **Costs matter but are not the whole story.** With no cost at all, 2.3 % of combinations made money in-sample and 44.0 % out-of-sample; 11 made money in both halves.
- **The Time Machine filter was the only choice that clearly made trades better.** Tested on its own across 26 combinations, it skipped 46 % of the trades, and the ones it kept were better: R per trade improved in 23 and the net in 25. Still, with it 0 of the 26 made money over its 40 sessions (0 without it).

So the honest pick in every category is a rule that lost least, not one that makes money. The tables below show the in-sample winners anyway, next to what they did afterwards.

## What was tested

- **Stocks:** AAPL, AMD, AMZN, AVGO, BAC, COIN, COST, CVX, DIS, GOOGL, INTC, IWM, JPM, KO, LLY, META, MSFT, MU, NFLX, NKE, NVDA, PFE, PLTR, QQQ, SPY, TSLA, UBER, UNH, WMT, XOM. Each one trades in its own $5,000 practice account, so the book is $150,000.
- **Sessions:** 59 (the session before them is history for the prior-day high). In-sample: the first 39, 2026-08-11 to 2026-10-05, where the picks are made. Out-of-sample: the last 20, 2026-10-06 to 2026-11-02, never looked at while picking.
- **Choices (832 combinations):** setup (5, 15 or 30-minute opening range, or the prior-day high); stop (range midpoint, range low, 0.5 or 1 % under, 1, 1.5 or 2 x ATR); target (1R, 1.5R, 2R, 3R); entry (market, or a limit at the cap); volume check (none or 1.5x); entry window (until 15:30 or 11:00). The risk per trade is the example config's $50; the Time Machine filter and $100 risk are in the stress tests, because they are slow or only scale the same trades.
- **The rules are the pack's own:** `engine.py` calls `entry.evaluate`, `exit.compute`, `size.size_trade`, `check.check` and the scenario runner's simulated broker. `validate.py` checks it reproduces all 13 TSLA scenarios (507 sessions) exactly.
- **Costs:** 1 cent a share each way (spread and slippage). Alpaca charges no commission. Fills come from 5-minute bars; a bracket whose stop was already crossed when it arrived counts as rejected, as Alpaca rejects it.
- **Metrics:** net after costs; return on the $150,000 book; Sharpe and Sortino ratios (annualized from daily returns, no-trade days included); maximum drawdown; return per dollar of drawdown; win rate; profit factor (gains over losses); average R per trade and its t-statistic; how many of the 30 stocks made money; the deflated Sharpe ratio for the number of combinations tried.

## The picks, one per category (chosen in-sample, then tested out-of-sample)

A combination needs 60 in-sample trades to be picked. "Rank" is where the pick landed out-of-sample among all 832 (100 = the best).

| Category | In-sample winner | Metric in-sample | Metric out-of-sample | Net in | Net out | Net, whole period | Rank out |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Highest net return | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | +$317.58 | -$290.46 | +$317.58 | -$290.46 | +$27.12 | 20 |
| Highest Sharpe ratio | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 1.25 | -2.43 | +$317.58 | -$290.46 | +$27.12 | 37 |
| Highest Sortino ratio | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 1.89 | -2.94 | +$317.58 | -$290.46 | +$27.12 | 40 |
| Best return per dollar of drawdown | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 0.58 | -0.47 | +$317.58 | -$290.46 | +$27.12 | 38 |
| Smallest drawdown while making money | 5-minute range, stop 1 x ATR, target 1R, limit at the cap, no volume check, entries until 11:00 | $534.87 | $660.35 | +$139.36 | -$256.84 | -$117.48 | 23 |
| Highest profit factor | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 1.06 | 0.91 | +$317.58 | -$290.46 | +$27.12 | 49 |
| Highest win rate while making money | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 51.6 % | 49.8 % | +$317.58 | -$290.46 | +$27.12 | 77 |
| Strongest evidence (t-stat of R per trade) | prior-day high, stop 1.5 x ATR, target 2R, limit at the cap, volume 1.5x, entries until 15:30 | 0.36 | -0.30 | -$234.82 | +$52.15 | -$182.67 | 42 |
| Most stocks profitable | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 15 of 30 | 11 of 30 | +$317.58 | -$290.46 | +$27.12 | 65 |
| Steadiest (best worse quarter in-sample) | 30-minute range, stop 1 x ATR, target 1R, market entry, volume 1.5x, entries until 15:30 | -$59.99 | -$5.09 | -$97.12 | +$56.99 | -$40.13 | 87 |
| Best choice per feature (in-sample medians) | 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | +$317.58 | -$290.46 | +$317.58 | -$290.46 | +$27.12 | 20 |

The full numbers of every pick (trades, Sharpe, drawdown, profit factor, deflated Sharpe, both halves) are in `results/picks.json`, and every combination is a row of `results/combos.csv`.

Whole period, the five combinations with the most net, for the record (picked with hindsight, so they prove nothing):

| Combination | Trades | Net | Sharpe | Max drawdown | Profit factor | Avg R | Net with no cost |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 15-minute range, stop 1 x ATR, target 2R, market entry, no volume check, entries until 11:00 | 647 | +$235.23 | 0.56 | $722.10 | 1.03 | 0.037 | +$510.19 |
| 15-minute range, stop 1.5 x ATR, target 1.5R, limit at the cap, no volume check, entries until 11:00 | 662 | +$88.35 | 0.19 | $1,024.84 | 1.01 | 0.012 | +$394.19 |
| 15-minute range, stop 1 x ATR, target 2R, limit at the cap, no volume check, entries until 11:00 | 620 | +$75.27 | 0.20 | $619.67 | 1.01 | 0.021 | +$349.05 |
| 15-minute range, stop 2 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 697 | +$74.34 | 0.18 | $713.17 | 1.01 | 0.016 | +$386.08 |
| 15-minute range, stop 1.5 x ATR, target 1.5R, market entry, no volume check, entries until 11:00 | 678 | +$46.87 | 0.10 | $975.19 | 1.00 | 0.015 | +$360.23 |

## Each choice on its own

Every combination that has the choice. R per trade is the fairer measure: a choice that trades less also loses less when trades lose, without making the trades any better. A choice that really helps should help in both halves.

| Choice | Value | Combos | Median trades | R per trade in | R per trade out | R per trade, whole | Median net in | Median net out |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| entry | cap | 416 | 310 | -0.096 | -0.007 | -0.061 | -$381.79 | -$60.03 |
| entry | market | 416 | 316 | -0.118 | -0.008 | -0.081 | -$392.15 | -$73.81 |
| entry window | until 11:00 | 416 | 250 | -0.117 | -0.011 | -0.069 | -$309.63 | -$57.09 |
| entry window | until 15:30 | 416 | 370 | -0.103 | -0.006 | -0.074 | -$505.34 | -$103.66 |
| setup | orb15 | 224 | 374 | -0.104 | -0.038 | -0.080 | -$384.83 | -$85.06 |
| setup | orb30 | 224 | 311 | -0.116 | 0.048 | -0.067 | -$523.62 | +$40.70 |
| setup | orb5 | 224 | 418 | -0.084 | -0.061 | -0.080 | -$301.45 | -$259.38 |
| setup | pdh | 160 | 309 | -0.105 | 0.047 | -0.066 | -$462.51 | +$30.25 |
| stop | atr:1 | 128 | 313 | -0.107 | 0.038 | -0.070 | -$305.27 | -$47.80 |
| stop | atr:1.5 | 128 | 314 | -0.106 | 0.034 | -0.061 | -$464.47 | +$8.48 |
| stop | atr:2 | 128 | 322 | -0.081 | -0.014 | -0.065 | -$366.83 | -$77.48 |
| stop | or-low | 96 | 350 | -0.100 | -0.015 | -0.074 | -$482.47 | -$236.48 |
| stop | or-mid | 96 | 326 | -0.134 | -0.003 | -0.075 | -$398.28 | -$80.91 |
| stop | pct:0.5 | 128 | 320 | -0.156 | -0.007 | -0.093 | -$433.62 | -$9.79 |
| stop | pct:1 | 128 | 305 | -0.092 | -0.035 | -0.067 | -$367.46 | -$161.12 |
| target | 1.5R | 208 | 311 | -0.112 | -0.008 | -0.073 | -$369.41 | -$58.05 |
| target | 1R | 208 | 323 | -0.102 | 0.003 | -0.065 | -$319.00 | -$41.77 |
| target | 2R | 208 | 317 | -0.114 | -0.010 | -0.074 | -$429.86 | -$92.03 |
| target | 3R | 208 | 316 | -0.102 | -0.022 | -0.075 | -$451.66 | -$103.86 |
| volume check | 1.5x | 416 | 76 | -0.151 | -0.055 | -0.134 | -$260.06 | -$45.36 |
| volume check | none | 416 | 686 | -0.080 | 0.001 | -0.049 | -$953.34 | -$154.01 |

What the two halves agree on, by the median R per trade:

- **Setup:** no stable winner. Best in-sample orb5, best out-of-sample orb30.
- **Stop:** no stable winner. Best in-sample atr:2, best out-of-sample atr:1.
- **Target: 1R** was the best in both halves (-0.102R in, 0.003R out a trade).
- **Entry: cap** was the best in both halves (-0.096R in, -0.007R out a trade).
- **Volume check: none** was the best in both halves (-0.080R in, 0.001R out a trade).
- **Entry window: until 15:30** was the best in both halves (-0.103R in, -0.006R out a trade).
- **The margins are small,** a few hundredths of R a trade, and every one of these "best" choices still lost money per trade over the whole period.
- **The volume check makes each trade worse** (-0.134R against -0.049R a trade over the whole period) and lowers the losses only by trading about 9 times less (a median 76 trades against 686).

## Baselines

- **No signal at all:** buy every stock every session at 9:45, with the same stop and target. Each of the 640 combinations with a matching stop and target is set against that baseline:
  - in dollars, 93 % of them lost less in-sample and 62 % out-of-sample, because they trade far less (a median 307 trades against 1413);
  - per trade, only 28 % beat it in-sample and 49 % out-of-sample. The breakout signals did not pick better trades than buying at 9:45 every day.
- **Buy and hold** $5,000 of each stock (not a like-for-like rival: it holds overnight and takes the whole market move): +$3,342.51 (2.23 %) in-sample, -$4,590.03 (-3.06 %) out-of-sample, -$3,123.25 over the whole period.

## Stress tests of the picks

Each in-sample winner, and the README's own rules (15-minute range, midpoint stop, 1R, market, until 15:30) for comparison. Net over the whole period on the $150,000 book.

| Combination | No cost | 1 cent (base) | 3 cents | Yes 5 min later | $100 risk |
| --- | --- | --- | --- | --- | --- |
| 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | +$393.00 | +$27.12 | -$704.64 | -$215.10 | -$28.55 |
| 5-minute range, stop 1 x ATR, target 1R, limit at the cap, no volume check, entries until 11:00 | +$235.68 | -$117.48 | -$823.80 | -$432.90 | -$127.40 |
| prior-day high, stop 1.5 x ATR, target 2R, limit at the cap, volume 1.5x, entries until 15:30 | -$115.87 | -$182.67 | -$316.27 | -$210.13 | -$184.46 |
| 30-minute range, stop 1 x ATR, target 1R, market entry, volume 1.5x, entries until 15:30 | +$44.43 | -$40.13 | -$209.25 | -$48.29 | -$40.13 |
| 15-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | -$1,070.53 | -$1,429.97 | -$2,148.85 | -$1,147.21 | -$1,526.04 |

The Time Machine filter (trade only when it has target-first above stop-first), from 2026-09-08, when it has 20 earlier sessions to build on, with and without the filter on the same sessions:

| Combination | Trades without / with | Net without | Net with | R per trade without | R per trade with |
| --- | --- | --- | --- | --- | --- |
| 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 543 / 314 | -$651.31 | -$96.42 | -0.057 | -0.021 |
| 5-minute range, stop 1 x ATR, target 1R, limit at the cap, no volume check, entries until 11:00 | 538 / 310 | -$730.06 | -$134.00 | -0.067 | -0.025 |
| prior-day high, stop 1.5 x ATR, target 2R, limit at the cap, volume 1.5x, entries until 15:30 | 88 / 0 | -$66.88 | +$0.00 | -0.017 | 0.000 |
| 30-minute range, stop 1 x ATR, target 1R, market entry, volume 1.5x, entries until 15:30 | 115 / 57 | -$16.29 | -$8.72 | 0.112 | 0.081 |
| 15-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | 570 / 298 | -$1,414.88 | -$406.02 | -0.083 | -0.041 |

| Combination | Stocks up in-sample | Out-of-sample, those stocks only | Out-of-sample, all 30 | One account: trades | Net | In | Out | Sharpe | Max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 11:00 | 15 | +$38.11 | -$290.46 | 59 | +$92.92 | -$40.22 | +$133.14 | 0.86 | $109.83 |
| 5-minute range, stop 1 x ATR, target 1R, limit at the cap, no volume check, entries until 11:00 | 15 | +$46.34 | -$256.84 | 58 | +$106.67 | -$29.87 | +$136.54 | 1.00 | $103.21 |
| prior-day high, stop 1.5 x ATR, target 2R, limit at the cap, volume 1.5x, entries until 15:30 | 11 | +$96.81 | +$52.15 | 43 | -$11.42 | -$96.52 | +$85.10 | -0.17 | $134.78 |
| 30-minute range, stop 1 x ATR, target 1R, market entry, volume 1.5x, entries until 15:30 | 13 | -$23.70 | +$56.99 | 53 | -$6.48 | -$32.51 | +$26.03 | -0.13 | $107.59 |
| 15-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | 9 | -$573.58 | -$338.66 | 59 | -$155.49 | -$77.99 | -$77.50 | -1.45 | $272.40 |

- **Costs decide the sign.** At no cost the in-sample winner made +$393.00; at 1 cent a share +$27.12; at 3 cents -$704.64.
- **Stock picking** keeps only the stocks a combination made money on in-sample. If good stocks stayed good, that column would beat the next one in every row; it does in 3 of 5.
- **One account** is a single $5,000 account trading the whole list the way one person would: each session the first signal among the 30 stocks, one trade a day. Over the 59 sessions these ended between -$155.49 and +$106.67. The in-sample winner's account lost in-sample and made money out-of-sample, the opposite of the 30-account book: at this size the result is mostly which trades happened to come first.

## The Time Machine filter, tested wider

It was the one choice that made trades better in the stress tests, so it got a test of its own: every setup and stop with a 1R target, market entry, no volume check and entries until 15:30 (26 combos), with and without the filter, over the 40 sessions from 2026-09-08 to 2026-11-02.

| Combination | Trades without / with | Net without | Net with | R per trade without | R per trade with | Sharpe without | Sharpe with |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | 647 / 362 | -$1,276.26 | -$306.24 | -0.083 | -0.020 | -5.60 | -1.62 |
| 5-minute range, stop at the range low, target 1R, market entry, no volume check, entries until 15:30 | 664 / 374 | -$1,400.18 | -$829.02 | -0.042 | -0.036 | -4.39 | -3.65 |
| 5-minute range, stop 0.5% under, target 1R, market entry, no volume check, entries until 15:30 | 635 / 301 | -$889.98 | -$308.38 | -0.079 | -0.058 | -4.61 | -2.28 |
| 5-minute range, stop 1% under, target 1R, market entry, no volume check, entries until 15:30 | 590 / 327 | -$737.43 | -$766.36 | -0.036 | -0.066 | -2.03 | -3.02 |
| 5-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 637 / 361 | -$698.07 | -$154.95 | -0.073 | -0.040 | -2.89 | -0.79 |
| 5-minute range, stop 1.5 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 648 / 389 | -$1,252.40 | -$704.68 | -0.045 | -0.012 | -3.81 | -2.89 |
| 5-minute range, stop 2 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 669 / 379 | -$1,008.36 | -$277.63 | -0.027 | 0.003 | -2.79 | -1.21 |
| 15-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | 570 / 298 | -$1,414.88 | -$406.02 | -0.083 | -0.041 | -5.09 | -1.83 |
| 15-minute range, stop at the range low, target 1R, market entry, no volume check, entries until 15:30 | 605 / 283 | -$1,273.21 | -$340.66 | -0.041 | -0.016 | -3.98 | -1.66 |
| 15-minute range, stop 0.5% under, target 1R, market entry, no volume check, entries until 15:30 | 570 / 290 | -$972.53 | -$129.99 | -0.096 | -0.027 | -5.23 | -0.99 |
| 15-minute range, stop 1% under, target 1R, market entry, no volume check, entries until 15:30 | 518 / 282 | -$1,135.01 | -$243.96 | -0.061 | -0.024 | -3.59 | -1.09 |
| 15-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 603 / 319 | -$449.16 | -$212.78 | -0.088 | -0.081 | -2.03 | -1.26 |
| 15-minute range, stop 1.5 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 585 / 323 | -$972.51 | -$207.38 | -0.059 | 0.015 | -3.34 | -1.02 |
| 15-minute range, stop 2 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 597 / 311 | -$825.67 | -$80.39 | -0.054 | -0.018 | -2.50 | -0.35 |
| 30-minute range, stop at the range midpoint, target 1R, market entry, no volume check, entries until 15:30 | 485 / 238 | -$1,281.38 | -$435.82 | -0.073 | -0.038 | -4.09 | -2.05 |
| 30-minute range, stop at the range low, target 1R, market entry, no volume check, entries until 15:30 | 529 / 233 | -$1,218.31 | -$329.91 | -0.053 | -0.022 | -3.64 | -1.80 |
| 30-minute range, stop 0.5% under, target 1R, market entry, no volume check, entries until 15:30 | 483 / 242 | -$1,506.80 | -$558.66 | -0.175 | -0.130 | -7.83 | -4.35 |
| 30-minute range, stop 1% under, target 1R, market entry, no volume check, entries until 15:30 | 450 / 215 | -$1,691.24 | -$620.41 | -0.105 | -0.081 | -4.85 | -2.90 |
| 30-minute range, stop 1 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 508 / 277 | -$1,052.32 | -$334.72 | -0.177 | -0.085 | -5.51 | -2.27 |
| 30-minute range, stop 1.5 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 484 / 260 | -$1,337.25 | -$181.67 | -0.130 | -0.004 | -5.06 | -0.96 |
| 30-minute range, stop 2 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 501 / 240 | -$1,089.94 | -$179.72 | -0.106 | -0.047 | -3.49 | -0.90 |
| prior-day high, stop 0.5% under, target 1R, market entry, no volume check, entries until 15:30 | 419 / 211 | -$720.82 | -$553.61 | -0.094 | -0.142 | -4.08 | -4.83 |
| prior-day high, stop 1% under, target 1R, market entry, no volume check, entries until 15:30 | 420 / 249 | -$828.34 | -$388.46 | -0.056 | -0.044 | -2.46 | -1.62 |
| prior-day high, stop 1 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 418 / 256 | -$1,146.67 | -$775.80 | -0.118 | -0.117 | -5.75 | -4.97 |
| prior-day high, stop 1.5 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 424 / 270 | -$1,412.44 | -$1,064.07 | -0.062 | -0.068 | -4.48 | -4.49 |
| prior-day high, stop 2 x ATR, target 1R, market entry, no volume check, entries until 15:30 | 431 / 267 | -$1,147.11 | -$257.05 | -0.027 | 0.012 | -3.69 | -1.19 |

- **R per trade was better with the filter in 23 of 26 combos**, the net in 25. With the filter 0 of 26 made money.
- Both columns are the same 40 sessions and the filter's rule was fixed in advance (it is the pack's own), so this is a fair A/B test, not a pick. It is still one stretch of market, and the filter was not part of the in-sample search.

## What this means for the bot

- **Don't expect these rules to make money.** On these 30 stocks and 59 sessions, no choice of setup, stop, target, entry, volume check or window made money in both halves, or picked better trades than buying at 9:45 with no signal at all.
- **Raising the risk per trade only scales the result.** It moves the losses and the gains together; it is not a way to profit.
- **If you keep paper trading it,** the choices the two halves agreed on per trade were a 1R target, a limit entry at the cap, no volume check and entries until 15:30. Treat them as the least-bad defaults, not as a strategy.
- **The Time Machine filter is worth keeping on** (the pack's S13 scenario): it improved R per trade in 23 of 26 combinations. It did not turn the rules into a money-maker, and each check takes about a second.
- **What was not tested, and could change the picture:**
  - the scanner's choice of stocks: `scan.py orb` picks the day's movers from the whole market, and the repo has a market-wide scan for one morning only, so this search used a fixed list of 30 large names;
  - more history: Yahoo keeps 5-minute bars for 60 days only, so this is one stretch of market. A longer test needs another data source;
  - short trades (the pack is long only), news and earnings days, and other markets.

## Caveats

- 59 sessions is one market regime. A Sharpe ratio estimated from 20 days has a standard error of about 3.5 (annualized), which is why the deflated Sharpe and the out-of-sample half matter more than any single figure.
- The 30 names are today's well-known stocks, which is survivor bias of its own.
- Fills are simulated from 5-minute bars at the next bar's open, the stop or the target, with a flat cost a share; Alpaca's paper fills (at the bid and ask, with random partial fills) and live fills differ.

## Run it again

```bash
python -B paper-trading-scenarios/feature-search/fetch_data.py      # new 60-day window from Yahoo
python -B paper-trading-scenarios/feature-search/validate.py        # the engine vs the 13 scenarios
python -B paper-trading-scenarios/feature-search/search.py          # the grid, about 2 minutes
python -B paper-trading-scenarios/feature-search/stress.py          # the stress tests, about 10 minutes
python -B paper-trading-scenarios/feature-search/tm_filter.py       # the filter, about 80 minutes
python -B paper-trading-scenarios/feature-search/write_results.py   # this file
```

A new download is a new 60-day window, so the numbers will change; with the same `data/` the runs repeat exactly.
