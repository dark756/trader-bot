# Paper-trading scenarios

Thirteen paper-trading runs of the Day Trader skill, each over the same 39 recorded TSLA sessions (Mon 2026-08-24 to Fri 2026-10-16), made on 2026-11-02. The comparison is in [paper-trading-comparison.pdf](paper-trading-comparison.pdf).

> **Not financial advice.** These are replays of one stock over eight weeks, with simulated fills. They say what these rules did on these days, not what they will do next.

The same choices on 30 stocks, in 852 combinations with an out-of-sample test, are in [feature-search/](feature-search/RESULTS.md).

## What was run

- **One choice per scenario.** S01 is the README example's rules. Each other scenario changes exactly one of the nine choices a user makes (stop rule, target, entry order, opening-range length, volume check, setup, risk per trade, entry window, Time Machine filter) and keeps everything else, so a difference traces back to that choice. [scenarios.json](scenarios.json) lists them.
- **The real tools, every session.** For each session, in the scenario's own trader folder (`TRADER_HOME`), on a pinned clock (practice runs):
  1. `entry.py watch` replays the session bar by bar from 9:30 and stops at the trigger;
  2. `exit.py plan` sets the stop and target;
  3. `timemachine.py run` replays the trade 10,000 times from the full sessions before it in the file (21 to 58 of them, never the day itself);
  4. `size.py` sizes the bracket order against the account's cash;
  5. `check.py` gives PASS or BLOCK;
  6. `ticket.py show` shows the ticket, and `ticket.py approve --auto-yes` gives the yes;
  7. the simulated paper broker (below) fills it, and `ticket.py result` records the placement and the entry fill;
  8. the exit: `ticket.py fill --practice` records a stop-loss or take-profit sell; a position still open at 15:55 goes through `ticket.py flatten`, `check.py`, `ticket.py show`, its own automatic yes and `ticket.py result`, the way a real day closes it.
- **The data.** `tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz`, the repo's 60 days of TSLA 5-minute bars (59 full sessions). The first 20 sessions are history only, because the Time Machine needs 20 sessions before the day it replays. No network was used.
- **An automatic yes, and no order.** `ticket.py approve --auto-yes` stands in for the user's typed yes. The tool accepts it only on a practice run (a pinned clock) with `"paper": true`, so nothing can be placed from it, and the log marks it `auto_yes`, never as a reply. Where the ticket said READY, the simulated paper broker took the exact `place_stock_order` arguments the ticket showed and filled them against that session's bars; it stands in for the Allow once card and Alpaca paper. Nothing was sent to a broker.
- **The log, as on a real day.** Every yes, placement and fill lands in the scenario's practice log (`trader-home/practice/state/broker.json`) and portfolio, written by the tools themselves. So trader-risk counts them: today's trades and loss, duplicates, and (with `"pdt_guard": true`) day trades in the last five sessions. Each evening the runner checks that the tools' cash matches the simulated broker's (`books` in `trades.csv`).

## The simulated paper broker

- **Market entry:** fills at the open of the bar after the signal, the first price after the trigger bar closed.
- **Limit entry:** fills at the open if that is at or under the limit, else at the limit once a bar trades down to it. If it has not filled by the end of the entry window, it is canceled.
- **Stop-loss leg:** sells at the stop, or at the bar's open when a bar opens below the stop (a gap).
- **Take-profit leg:** sells at the target, or at the open when a bar opens above it.
- **Both legs in one bar:** the stop is assumed to have filled first (5-minute bars do not say which came first).
- **Flat by:** anything still open at the plan's flat-by time (15:55) sells at that bar's open, after the bracket's two legs are canceled (on Alpaca, `cancel_order_by_id` for each). The sell itself is the flatten ticket the tools wrote, checked and approved.
- **Open loss:** marked at each bar's low (or the stop fill), so it is the worst the position showed at any moment.
- **Not modeled:** no commissions or fees, no spread, and no partial fills. Fills come from 5-minute bars, not from ticks.
- **The account:** starts with $5,000 cash. From then on the tools keep it (`ticket.py result` and `fill`, in `trader-home/practice/portfolio.json`), and size.py and check.py read it as the portfolio.

## Files

| path | what it holds |
| --- | --- |
| `paper-trading-comparison.pdf` | the comparison report |
| `summary.csv` | one row per scenario |
| `scenarios.json` | the scenario definitions, the data file and the 39 sessions |
| `S01-baseline/` ... `S13-time-machine-filter/` | one folder per scenario |
| `S*/config.json` | the rules that scenario ran under (the example config plus its change) |
| `S*/run.log` | every command, its full output and exit code, session by session, plus the simulated broker's fills |
| `S*/trades.csv` | one row per session: signal, plan, forecast, size, verdict, fill, exit, P&L, deepest open loss, cash, and how the yes and the exit were recorded, the tools' own cash and whether the books agree |
| `S*/summary.json` | the scenario's totals and its worst moments, plus how many yeses and exits went through the tools, any books that disagreed, and the practice log's records by status |
| `S*/days/<date>/` | what the tools wrote that day (entry, plan, Time Machine (trimmed), orders, risk verdict, ticket), `paper-broker.json` with every bar's mark, `decisions.json` (the day's records in the practice log) and, after a 15:55 exit, `flatten-orders.json`, `flatten-risk.json` and `flatten-ticket.json` |
| `S*/trader-home/` | the scenario's trader folder as the last session left it, with the whole practice log and portfolio |
| `run_scenarios.py` | runs the scenarios (standard library only) |
| `make_report.py` | builds the PDF from the results (needs matplotlib, and Microsoft Edge or the Chrome-family program that `REPORT_BROWSER` names) |

## Run it again

From the repo root:

```bash
python -B paper-trading-scenarios/run_scenarios.py          # all 13, about 7 minutes; or name some: S01 S07
python -B paper-trading-scenarios/make_report.py            # rebuilds the PDF
```

The tools are deterministic here (pinned clock, offline data, the Time Machine's fixed seed), so a rerun gives the same numbers.
