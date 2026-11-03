# The large backtest

The pack's rules on 7.5 years of Alpaca data, judged by a plan written before any data: [PLAN.md](PLAN.md). It tests two rules on two universes against their no-signal twins:

- **the rules:** the README rules, and a 5-minute opening-range breakout held to the close;
- **the universes:** each morning's 20 stocks in play, and the feature search's fixed list of 30;
- **the scoring:** development on 2019 to 2023, then a holdout (2024 to Aug 2026) that opens only for a rule that passed, and only once.

> **Not financial advice.** Simulated fills on past 5-minute bars. A pass here earns a paper-trading trial, nothing more.

## Run it on your PC

You need your Alpaca **paper** API keys (Alpaca dashboard, paper account, API keys), Python 3.8 or later, about 2 GB of free disk and about two hours for the download. Never paste the keys into a chat, a committed file or the config.

**The keys.** Put them in a file named `.env.local` in the repo root, two lines:

```
APCA_API_KEY_ID=your paper key id
APCA_API_SECRET_KEY=your paper secret key
```

Git ignores `.env.local` and `.env` (the root `.gitignore`), so they never land in a commit. `fetch.py` reads the keys from there when the terminal has none set, and says which file it used, never the keys. The file may also sit in this folder or the folder you run from. It takes quotes, `export NAME=...`, PowerShell's `$env:NAME = "..."` and `#` comments, and other names work too: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (what Alpaca's MCP server uses), `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `ALPACA_KEY_ID`, `ALPACA_KEY` and `ALPACA_SECRET`. Keys set in the terminal (`$env:APCA_API_KEY_ID = "..."` in PowerShell, `export` in Git Bash) come first.

Then, from the repo root:

```powershell
python -B paper-trading-scenarios/large-backtest/selftest.py
python -B paper-trading-scenarios/large-backtest/fetch.py
python -B paper-trading-scenarios/large-backtest/backtest.py
```

1. **`selftest.py`** (about a minute, no keys, no network) checks the code against a fake Alpaca server on your machine. It should end with `all checks passed`.
2. **`fetch.py`** downloads the data into `data/alpaca.sqlite`. If it stops (network, sleep, Ctrl+C), run it again: it carries on where it stopped. `fetch.py status` shows what is done. It only reads market data, the asset list and the calendar, never your orders or account. If Alpaca says your plan doesn't allow the SIP feed, add `--feed iex`; IEX has far fewer trades, so more sessions will have gaps and be left out.
3. **`backtest.py`** scores the development period (about 5 minutes) and writes `RESULTS.md`.
4. **Only if `RESULTS.md` says something passed:** `python -B paper-trading-scenarios/large-backtest/backtest.py --holdout`, once. It refuses a second time, and refuses if `plan.py` changed after the development run.

## What the download does

| step | what | requests (free plan: 180 a minute) |
| --- | --- | --- |
| assets | every US equity Alpaca lists, active and delisted, without warrants, rights, units and preferreds | 2 |
| calendar | the market sessions from Nov 2018 | 1 |
| daily | daily bars, raw and split-adjusted; only the statistics are kept (previous close, 14-session ATR and average volume), for the stocks over the liquidity floors and the fixed list | a few thousand |
| opening | each session's 9:30 bar for every stock liquid that day or in the next 20 sessions | about 10,000, most of the time |
| selection | the stocks in play, each session | none |
| intraday | each full session's 5-minute bars for its stocks in play and the fixed list | about 1,900 |

## Files

| path | what it holds |
| --- | --- |
| `PLAN.md`, `plan.py` | the plan in words, and every number in it (the code reads `plan.py`) |
| `alpaca.py` | the read-only Alpaca client: keys from the terminal or `.env.local`, 180 requests a minute, retries |
| `universe.py` | the asset filter, the split-proof daily statistics, the stocks-in-play ranking |
| `store.py` | the SQLite file, and stored sessions turned into what the feature search's engine hands the tools |
| `fetch.py`, `backtest.py`, `selftest.py` | the download, the backtest, the checks |
| `data/alpaca.sqlite` | the download (not committed; `fetch.py` can always rebuild it) |
| `results/development.json`, `results/holdout.json` | every score: costs at 0, 1 and 3 cents, years, price ranges, exits, the one-account book, the walk-forward's monthly picks |
| `results/*-trades.csv.gz` | every trade |
| `results/holdout-log.json` | each time the holdout was opened |
| `RESULTS.md` | the verdicts, written by `backtest.py` |

The trades go through the same in-process engine as the feature search (`../feature-search/engine.py`), so they're the pack's own tools: `entry.evaluate`, `exit.compute`, `size.size_trade`, `check.check` and the scenario runner's simulated paper broker. The self-test checks every link: it fetches from the fake server, stops part way and resumes, and then requires:

- every stored session to match `engine.load_symbol`'s bar for bar;
- four of the feature search's combinations to give exactly its trades and net;
- a session moved to 2021 to trade exactly as in 2026 (the tools' holiday list starts in 2026, and only warns outside it).
