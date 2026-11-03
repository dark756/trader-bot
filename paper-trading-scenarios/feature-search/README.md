# Feature search

Which of the bot's choices made money, tested on 30 stocks instead of one. **The results are in [RESULTS.md](RESULTS.md).**

> **Not financial advice.** A replay of recorded sessions with simulated fills. It says what these rules did on these days, not what they will do next.

## How it works

- **The data.** `fetch_data.py` saves 60 days of 5-minute bars from Yahoo for 30 liquid US stocks and ETFs into `data/`, as the chart JSON the tools read with `--offline`. Yahoo keeps 5-minute bars for 60 days only.
- **The rules are the pack's own.** `engine.py` calls the tools' functions in-process instead of running them one command at a time. That is 25 times faster, which is what makes 852 combinations on 30 stocks possible:
  - `entry.evaluate` gives the signal;
  - `exit.compute` sets the stop and target;
  - `size.size_trade` and `size.limit_cap` size the order;
  - `check.check` is the risk gate;
  - the scenario runner's `paper_broker` fills it.
- **Checked against the slow way.** `validate.py` reruns the 13 TSLA scenarios of `paper-trading-scenarios/` through the engine. Every one of the 507 sessions comes out the same: action, shares, fill, exit and result.
- **The search.** `search.py` runs every combination on every stock, each stock in its own $5,000 account, with a cost of 1 cent a share each way.
  - **The choices:** setup, opening range, stop, target, entry order, volume check and entry window.
  - **How it picks:** the winners of each category are chosen on the first 39 sessions only (in-sample) and then scored on the last 20 (out-of-sample).
  - **What it reports:** net, return, Sharpe, Sortino, drawdown, return per dollar of drawdown, win rate, profit factor, R per trade with its t-statistic, how many stocks made money, and the deflated Sharpe ratio for the number of combinations tried.
- **The stress tests.** `stress.py` takes each pick, and the README's own rules, through other costs, a yes that takes 5 minutes, the Time Machine filter, $100 risk, in-sample stock picking, and one realistic $5,000 account that trades the whole list.
- **The write-up.** `write_results.py` writes `RESULTS.md` from the result files.

## Files

| path | what it holds |
| --- | --- |
| `RESULTS.md` | the findings, with every table |
| `data/<SYMBOL>.json.gz` | the 60 days of 5-minute bars each run used |
| `results/combos.csv` | one row per combination: in-sample, out-of-sample and whole-period metrics, with and without costs |
| `results/features.csv` | each choice over every combination that has it |
| `results/picks.json` | the run's summary: the split, the checks for luck, the baselines and every category's pick |
| `results/stress.json` | the stress tests of the picks, and their results stock by stock |
| `engine.py`, `validate.py`, `search.py`, `stress.py`, `write_results.py`, `fetch_data.py` | the code (standard library only) |

## Run it again

From the repo root:

```bash
python -B paper-trading-scenarios/feature-search/validate.py        # the engine vs the 13 scenarios, about 20 s
python -B paper-trading-scenarios/feature-search/search.py          # the grid, about 2 minutes on 4 cores
python -B paper-trading-scenarios/feature-search/stress.py          # the stress tests, about 10 minutes
python -B paper-trading-scenarios/feature-search/write_results.py   # RESULTS.md
```

With the same `data/` the numbers repeat exactly. `fetch_data.py` downloads a new 60-day window, so after it the numbers change.
