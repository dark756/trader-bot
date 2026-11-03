# How to use the Day Trader skill

This guide gets the 11 trading skills running on this Windows PC, checks them offline, and walks you through a trading day. It is written for the copy of the repo at `C:\Users\gurka\Desktop\day-trader-skill-main`.

> **Not financial advice.** This is software that reads free public data and does arithmetic. Most people who day trade lose money, and you can too. Stay on a paper (practice) account until you have watched it work for weeks.

## What you are setting up

- **The skills.** There are 11 skills you call from Claude Code (`/trader-scanner`, `/trader-entry`, and so on). Each one is a set of instructions for Claude plus a small Python tool that Claude runs for you.
- **How they connect.** The skills pass work to each other through files in a `trader\` folder. For example, the scanner writes a watchlist, the exit planner writes a plan, and the budget manager writes an order.
- **Nothing trades on its own.** An order reaches your broker only after all three of these happen:
  1. the risk check passes that exact order (a pass counts for 15 minutes);
  2. you type `yes` for it;
  3. you click **Allow once** on Claude's own permission card.

  With Alpaca, a ticket is also only ready after the day's check that Alpaca is connected to the account you pinned (section 4).

Contents: [1 Setup](#1-one-time-setup) · [2 Check it works](#2-check-that-it-works-offline-about-5-minutes) · [3 A trading day](#3-a-real-trading-day) · [4 Paper broker](#4-connect-a-paper-broker-optional) · [5 Saying yes](#5-saying-yes-to-an-order) · [6 Other tools](#6-the-other-tools) · [7 Run tools yourself](#7-running-the-tools-yourself) · [8 Troubleshooting](#8-when-something-goes-wrong) · [9 Before real money](#9-before-you-ever-use-real-money)

## 1. One-time setup

> **Done on this PC on 2 November 2026:** 1.1 (the `python3.exe` copy), 1.2 A (the skills are in `%USERPROFILE%\.claude\skills`), 1.3 (`%USERPROFILE%\trading-desk\trader`, with `TRADER_HOME` set to it) and 1.4 (the example rules copied to `config.json`). Still yours to do: put your name and email in `sec_user_agent`, and section 4 when you want a paper broker. Start a new Claude Code session so it picks up `TRADER_HOME`.

### 1.1 Make `python3` work on this PC

The skills tell Claude to run `python3 ...`. On this PC, `python3` opens a Microsoft Store placeholder ("Python was not found"), while `python` is your real Python 3.13.5. Fix it once in PowerShell:

```powershell
$dir = "$env:LOCALAPPDATA\Programs\Python\Python313"
Copy-Item "$dir\python.exe" "$dir\python3.exe"
python3 --version          # should print: Python 3.13.5
```

Check it in Git Bash too, because Claude Code runs its commands there: run `python3 --version`. If it still says "Python was not found", switch off the placeholder: Windows Settings > Apps > Advanced app settings > App execution aliases, then turn off **python3.exe** (App Installer). If you later move to a new Python version (for example 3.14), repeat this in that version's folder.

Rather not copy files? Tell Claude instead. Open or create `C:\Users\gurka\.claude\CLAUDE.md` and add the line `On this PC, run Python as "python", not "python3".` Claude reads that file in every session.

### 1.2 Install the skills

Pick one way.

**A. Copy the folders (recommended).** You get short names like `/trader-scanner`. Run this in PowerShell:

```powershell
$T = "C:\Users\gurka\Desktop\day-trader-skill-main"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
Copy-Item "$T\trader-*" "$env:USERPROFILE\.claude\skills\" -Recurse -Force
Get-ChildItem "$env:USERPROFILE\.claude\skills" -Directory -Name    # lists the 11 trader-* folders
```

Start a new Claude Code session and type `/trader`: the 11 skills should appear. Claude runs these copies, not the repo, so after you change code in the repo, run the `Copy-Item` line again.

**B. Install as a plugin from this folder.** Type this in Claude Code. The skill names get a prefix: `/day-trader-skill:trader-scanner`.

```
/plugin marketplace add C:\Users\gurka\Desktop\day-trader-skill-main
/plugin install day-trader-skill@day-trader-skill
```

**C. Install the upstream version from GitHub.** This gets the author's latest copy, not this folder, so it may differ from what this guide describes.

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

### 1.3 Make one workspace folder, and always use it

Every skill reads and writes the `trader\` folder inside whatever folder Claude Code is opened in. So make one dedicated folder:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\trading-desk\trader" | Out-Null
```

Open Claude Code in `C:\Users\gurka\trading-desk` every time. In VS Code, use File > Open Folder; in a terminal, run `cd ~\trading-desk` and then `claude`.

Why this matters:
- **Your daily limits live in that folder.** The risk manager counts today's trades and losses from `trader\state\broker.json`. Open Claude Code somewhere else and it starts a fresh `trader\` folder that knows nothing about today, so your daily limits stop protecting you.
- **Or pin it once with `TRADER_HOME`.** Set this environment variable to your trader folder and every tool uses it, whichever folder Claude Code is opened in. In PowerShell, once: `setx TRADER_HOME "$env:USERPROFILE\trading-desk\trader"`, then open a new terminal and restart Claude Code. It points at the `trader` folder itself, not the folder around it.
- **Keep it out of the repo folder.** The repo's tests scan every text file in it, so your saved headlines could make them fail.
- **Keep it out of git.** `trader\` holds your account, your orders and your name and email. If your workspace folder is a git repository, add a `.gitignore` file to it with the line `trader/` (the repo folder already has one).

### 1.4 Set your rules in `trader\config.json`

The easiest way is to type `/trader-budget set my rules` in Claude Code and answer Claude's questions. Or copy the example and edit it yourself:

```powershell
$T = "C:\Users\gurka\Desktop\day-trader-skill-main"
Copy-Item "$T\trader-budget\config.example.json" "$env:USERPROFILE\trading-desk\trader\config.json"
notepad "$env:USERPROFILE\trading-desk\trader\config.json"
```

| key | example | what it means |
| --- | --- | --- |
| `budget` | `5000` | Dollars this is for. Only money you can afford to lose. |
| `cash_buffer_pct` | `10` | Percent of the budget that always stays in cash. |
| `max_loss_per_trade_usd` | `50` | The most one trade may lose if the stop fills at the stop. This decides the share count. |
| `max_daily_loss_usd` | `150` | Today's max losses added up; no new entries past this. |
| `max_trades_per_day` | `3` | New entries a day. |
| `max_orders_per_day` | `10` | All orders a day, including sells. |
| `max_position_pct` | `75` | The biggest single position, as a percent of the account. |
| `max_order_usd` / `min_order_usd` | `4000` / `5` | The biggest and smallest single order. |
| `min_reward_risk` | `1.0` | The distance to the target divided by the distance to the stop must be at least this. |
| `require_stop` | `true` | Every entry needs a stop. Keep it `true`. |
| `no_entries_before` / `no_entries_after` | `"09:45"` / `"15:30"` | The entry window, New York time. |
| `flat_by` | `"15:55"` | Be out of everything by this New York time. |
| `paper` | `true` | Marks the account as paper. Keep it `true` (also see section 4). |
| `allow_options` / `allow_margin` / `allow_short` | `false` | Off. |
| `pdt_guard` | `false` | Set to `true` if your broker still applies the old $25,000 day-trader rule. |
| `blocked` | `[]` | Tickers you never trade, for example `["GME"]`. |
| `fractional` | `false` | Whole shares only (bracket orders need whole shares). |
| `broker` | `"alpaca"` | Or `"ibkr"`. |
| `alpaca_account` | `"PA1234ABCD"` | Not in the example file: add it yourself once you have checked your paper account's number (section 4, step 5). Until then, Alpaca tickets say NOT READY. |
| `sec_user_agent` | `"Your Name you@example.com"` | Your real name and email. The SEC asks for it; the news filings and the earnings analyst need it. |

How the tools read this file. A value they can't read stops them with a one-line error that names the key, and nothing is checked or sized until you fix it:
1. **Plain numbers.** Write `50`, not `"50"`, `"$50"` or `"50 USD"`. No negative numbers.
2. **No quotes on `true` and `false`.** `"false"` in quotes is an error.
3. **Lists stay lists.** `"blocked": ["GME"]`, not `"blocked": "GME"`.
4. **`null` switches a limit off; `0` means zero.** `"max_trades_per_day": 0` means no new entries today (selling what you hold still works), and `"max_loss_per_trade_usd": 0` means no order at all. A limit set to `null` is off, and the risk check lists every limit that is off each time it runs. Keep them all on.

Check that the file is valid JSON. This prints the file back if it's valid, or points at the broken line:

```powershell
python -m json.tool "$env:USERPROFILE\trading-desk\trader\config.json"
```

## 2. Check that it works (offline, about 5 minutes)

### 2.1 Run the tests

```powershell
cd C:\Users\gurka\Desktop\day-trader-skill-main
python -B -m unittest discover -s tests
```

You should see `Ran 384 tests` and `OK`. Any failure is new: tell Claude what it says.

### 2.2 Replay the example trade, with no internet and nothing written

The repo keeps real data from Monday 19 October 2026, when TSLA broke out at 10:15 AM New York time. The commands below replay that whole trade. They use `--no-write` or `--dry-run`, or leave out `--write`, so they change nothing. Run them in PowerShell:

```powershell
cd C:\Users\gurka\Desktop\day-trader-skill-main
$F = "tests\fixtures"
```

1. **Scanner**
   ```powershell
   python trader-scanner\scan.py orb --offline "$F\trader-scanner\wall5m-2026-10-19.json.gz" --now "2026-10-19 10:15" --min-price 0 --no-write
   ```
   Look for `42 of 231 charts closed above their opening-range high by 10:15 AM:` and the line `10:15 AM  TSLA  $364.18  range 354.47 to 362.18  +0.55% over`.

2. **Entry timer**, at 10:12 and then at 10:15
   ```powershell
   python trader-entry\entry.py check TSLA --offline "$F\trader-entry\TSLA-2026-10-19-5m.json" --now "2026-10-19 10:12"
   python trader-entry\entry.py check TSLA --offline "$F\trader-entry\TSLA-2026-10-19-5m.json" --now "2026-10-19 10:15"
   ```
   The first prints `WAITING  last close $361.68 (10:05 bar), $0.50 under the range high $362.18`, and the second `TRIGGERED 10:15 AM at $364.18`.

3. **Exit planner**
   ```powershell
   python trader-exit\exit.py plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47 --date 2026-10-19 --time "10:15 AM" --entry-bar 8 --stop or-mid --target 1R --now "2026-10-19 10:15"
   ```
   Look for Stop `$358.32`, Target `$370.04` and Risk a share `$5.86`.

4. **Time Machine**
   ```powershell
   python trader-timemachine\timemachine.py run --symbol TSLA --date 2026-10-19 --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM" --offline "$F\trader-timemachine\TSLA-60d5m.json.gz" --now "2026-10-19 10:15"
   ```
   Look for target first `29.4 %`, stop first `25.5 %`, neither `45.1 %`, and the verdict `Close to a coin flip`.

5. **Budget manager**
   ```powershell
   python trader-budget\size.py --plan "$F\trader-budget\plan-tsla.json" --config trader-budget\config.example.json --now "2026-10-19 10:15"
   ```
   Look for `Size: 8 shares of TSLA = $2,913.44` and `Max loss at the stop: 8 x 5.86 = $46.88`. The `<- binds` mark shows which limit decided the size: the $50 max loss.

6. **Risk manager**
   ```powershell
   python trader-risk\check.py --orders "$F\trader-risk\orders-tsla.json" --config trader-budget\config.example.json --now "2026-10-19 10:15" --no-write
   ```
   Look for `PASS  #1 buy TSLA 8 shares`, and then `PRACTICE: the clock is pinned`: `--now` turns any run into practice. The order ticket never treats a practice pass as a real one, and a practice run keeps its own files in `trader\practice\`, so it never touches your real ones.

7. **Order ticket**
   ```powershell
   python trader-broker\ticket.py show --orders "$F\trader-risk\orders-tsla.json" --config trader-budget\config.example.json --risk nothing-here.json --now "2026-10-19 10:15"
   ```
   It starts with `PRACTICE RUN` (the `--now` again) and prints the ticket with `NOT READY: trader-risk has not checked these orders`, because step 6 didn't save its verdict. In real use, without `--now`, the ticket says `READY` and asks for your yes.

8. **Alerts**
   ```powershell
   python trader-alerts\alerts.py check --rules trader-alerts\alerts.example.json --offline "$F\trader-alerts" --now "2026-10-19 10:53" --no-write
   ```
   Look for `ALERT TSLA moved +3.31% to $366.48 (from $354.72).`

9. **News.** Like every other tool, `--now` here is New York time (add `Z` for UTC).
   ```powershell
   python trader-news\news.py --tickers TSLA --offline "$F\trader-news" --now "2026-10-19 10:15" --no-sec --dry-run
   ```
   Look for `TSLA 364.18 +2.67% vs yesterday's close 354.72 at 10:15 ET` (the price at 10:15, never a later one) and that morning's headlines.

10. **Earnings analyst** (Apple, from saved SEC data). It prints a table of quarters, starting `2026-06-27  Q3 2026  109,417  +16.4%`.
    ```powershell
    python trader-analyst\facts.py AAPL --offline "$F\trader-analyst"
    ```

11. **Pelosi tracker.** The first command lists 4 filings in 2026; the second reads one (Bloom Energy shares and call options).
    ```powershell
    python trader-pelosi\ptr.py index --member pelosi --offline "$F\trader-pelosi\2026FD.zip" --year 2026
    python trader-pelosi\ptr.py parse "$F\trader-pelosi\ptr-20035143.txt" --filed 2026-08-21
    ```

If all of these match, the tools work on your PC.

### 2.3 See the pipeline trade 39 recorded sessions

The `paper-trading-scenarios` folder holds 13 practice runs of the whole pipeline over 39 recorded TSLA sessions (24 Aug to 16 Oct 2026). Each one changes one choice from the example's rules: stop, target, entry order, opening range, volume check, setup, risk per trade, entry window, or the Time Machine as a filter. Each READY ticket got an automatic yes (`ticket.py approve --auto-yes`, which only works on a practice run with `"paper": true`), a simulated paper broker filled it against the recorded bars, and the tools recorded every placement and fill in the scenario's practice log, so the risk check counted them the way it would on a real day. Nothing was sent to a broker. Read `paper-trading-comparison.pdf` there, including its section on the largest losses at any one moment. To run them again (about 7 minutes, same numbers each time):

```powershell
python -B paper-trading-scenarios\run_scenarios.py
python -B paper-trading-scenarios\make_report.py
```

## 3. A real trading day

### 3.1 New York time and India time

The tools always work in New York time and ignore your PC's time zone. You type New York times too, for example `--now "2026-11-02 10:15"` or `"flat_by": "15:55"`. The setup guide PDF in this folder is written for India, so here are the times in IST:

| what | New York | IST until Fri 12 Mar 2027 | IST from Mon 15 Mar 2027 |
| --- | --- | --- | --- |
| market opens | 9:30 AM | 8:00 PM | 7:00 PM |
| new entries allowed (`no_entries_before`) | 9:45 AM | 8:15 PM | 7:15 PM |
| first opening-range breakout can show | 9:50 AM | 8:20 PM | 7:20 PM |
| the classic opening-range scan | 10:15 AM | 8:45 PM | 7:45 PM |
| last new entry (`no_entries_after`) | 3:30 PM | 2:00 AM | 1:00 AM |
| be flat (`flat_by`) | 3:55 PM | 2:25 AM | 1:25 AM |
| market closes | 4:00 PM | 2:30 AM | 1:30 AM |

The AM times in IST fall after midnight, on the next calendar day.

There are two columns because US clocks went back on Sunday 1 November 2026 (Eastern Standard Time, UTC-5) and go forward on Sunday 14 March 2027 (Eastern Daylight Time, UTC-4). India doesn't change its clocks.

- **Early closes** at 1:00 PM New York: Fri 27 Nov 2026, Thu 24 Dec 2026, Fri 26 Nov 2027, Mon 3 Jul 2028 and Fri 24 Nov 2028. On those days the tools move your last entry to 12:30 PM and flat-by to 12:55 PM New York (in winter, 11:00 PM and 11:25 PM IST).
- **Closed all day:**
  - 2026: Thu 26 Nov and Fri 25 Dec.
  - 2027: Fri 1 Jan, Mon 18 Jan, Mon 15 Feb, Fri 26 Mar, Mon 31 May, Fri 18 Jun, Mon 5 Jul, Mon 6 Sep, Thu 25 Nov and Fri 24 Dec.
  - 2028: Mon 17 Jan, Mon 21 Feb, Fri 14 Apr, Mon 29 May, Mon 19 Jun, Tue 4 Jul, Mon 4 Sep, Thu 23 Nov and Mon 25 Dec. No New Year's Day holiday: 1 Jan 2028 is a Saturday.
- **After 2028:** the tools only know 2026 to 2028, and say so (`Holiday list covers 2026 to 2028 only`). After that, check nyse.com.

To check at any time, run this in PowerShell. It says whether the NYSE is open and when it next opens:

```powershell
python "C:\Users\gurka\Desktop\day-trader-skill-main\trader-alerts\alerts.py" clock
```

### 3.2 The day, step by step

Open Claude Code in your workspace folder. You talk; Claude runs the tools.

| when (New York) | you type | what happens | file written |
| --- | --- | --- | --- |
| once a week | `/trader-scanner universe` | downloads the list of about 5,900 US stocks | `trader\universe.txt` |
| before 9:30 AM | `/trader-scanner daily` | finds daily-chart setups: 20-day breakout, bull flag, back above the 20-day average, oversold, oversold bounce (2 to 3 minutes for the whole list) | `trader\watchlist.json` |
| before 9:30 AM | `/trader-news` | headlines and SEC 8-K filings for the watchlist, and how far each stock has moved | `trader\state\news.json` |
| after 9:50 AM (10:15 is the classic time) | `/trader-scanner orb` | opening-range breakouts so far (about 4 minutes for the whole list; faster on your own list, for example "scan TSLA, NVDA and AMD") | `trader\watchlist.json` |
| your pick | `/trader-news TSLA` | what is moving it, and whether an earnings report falls inside your trade | |
| your pick | `/trader-entry TSLA` | says WAITING, TRIGGERED or EXPIRED. "Watch TSLA until it triggers" keeps watching in the background | `trader\entry.json` |
| right after the trigger | `/trader-exit` | sets the stop, the target and the flat-by time | `trader\plan.json` |
| then | `/trader-timemachine` | 10,000 replays plus the real history of the same rule, and one honest verdict | `trader\timemachine.json` |
| then | `/trader-budget` | how many shares, which limit decided it, and one bracket order | `trader\orders.json` |
| then | `/trader-risk` | PASS or BLOCK, with the reason | `trader\state\risk.json` |
| then | `/trader-broker` | checks the Alpaca account (first ticket of the day), shows the ticket; you reply `yes`; then Claude's Allow once card | `trader\state\broker.json` |
| while the trade is open | `/trader-alerts` | alerts on your stop, your target and the flat-by time. Keep them checked with `/loop 5m /trader-alerts check` | `trader\alerts.json`, `trader\prices.json` |
| by 3:55 PM | "flatten TSLA" | builds the sell ticket, which goes through the risk check and your yes | `trader\orders.json` |

Tips:
- **You pick the stock.** The scanner lists charts that match a rule; it never says what to buy.
- **Run the Time Machine before every trade**, and believe it when it says "coin flip".
- **Refresh a stale price.** If the price has moved since the plan, refresh it first ("get a TSLA quote and save it"), then run budget and risk again.
- **Keep the news coming.** `/loop 15m /trader-news` checks it every 15 minutes during the session.
- **Keep the laptop awake.** A background watch stops while your laptop sleeps, so keep it awake (Windows Settings > System > Power) while a trade is being watched or is open.
- **No orders from background tasks.** Nothing is placed from one: there is nobody there to say yes.

### 3.3 Reading the output

- **WAITING / TRIGGERED / EXPIRED** (entry timer): a trigger is the first 5-minute bar that *closes* above the level. A price that only pokes above it during the bar doesn't count.
- **R**: your risk per share. A 1R target sits as far above the entry as the stop sits below it.
- **The Time Machine verdict** ("Close to a coin flip", "Leans toward the stop", and so on): a simulation built from the last 60 days, not a prediction.
- **`<- binds`** (budget): the limit that decided the share count.
- **PASS / BLOCK** (risk): a PASS only means the order may be shown to you. It is not advice, and it is not a yes.
- **READY / NOT READY / DONE** (broker): only a READY ticket can take your yes. A pass counts for 15 minutes: after that the ticket says NOT READY until the risk check runs again.
- **PRACTICE / PRACTICE RUN** (risk, broker): the clock was pinned with `--now` or `--today`, for a replay or a test. Nothing from a practice run can be placed.
- **auto-yes** (broker log): an automatic yes from `ticket.py approve --auto-yes`, used by unattended replays such as the paper-trading scenarios. It works only on a practice run with `"paper": true`, so it can never lead to an order; on the real clock or a live config the tool refuses it, and every real order still needs your own typed yes.

## 4. Connect a paper broker (optional)

You can stop before this section and still use everything else. To send practice orders to Alpaca's paper account:

1. **Make paper keys.** Create an Alpaca account and make **paper** API keys in the Alpaca dashboard. Paper keys are separate from live keys.
2. **Install `uv`.** It provides `uvx`, which runs Alpaca's server. Run `winget install --id=astral-sh.uv -e`, or see docs.astral.sh/uv. Then close and reopen PowerShell and check that `uvx --version` works.
3. **Add Alpaca's MCP server**, in whichever app you use:
   - **Claude Desktop:**
     1. Go to Settings > Developer > Edit Config. This opens `%APPDATA%\Claude\claude_desktop_config.json`.
     2. Paste the block from `trader-broker\claude_desktop_config.example.json`, and type your paper keys into that file.
     3. Restart Claude Desktop.
   - **Claude Code:**
     1. Add the same server with `claude mcp add` (run `claude mcp add --help` to see the flags). The command is `uvx alpaca-mcp-server`, with `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE=true` and `ALPACA_TOOLSETS=account,trading,stock-data`.
     2. Make orders always ask first. In your workspace, put this in `.claude\settings.json`: `{"permissions": {"ask": ["mcp__alpaca__place_stock_order"]}}`. Never list an order tool under `allow`.
4. **Lock the tools.** In Claude Desktop, go to Customize > Connectors > alpaca:
   - `place_stock_order`, and every tool that changes, cancels or closes orders: **Needs approval**. Never Always allow.
   - Option and crypto orders, `close_all_positions`, `cancel_all_orders` and the exercise tools: **Blocked**.
   - Read-only tools (account info, positions, orders, quotes): Always allow is fine.
5. **Pin your paper account.** The "PAPER account per your config" line on each ticket comes from `"paper"` in your `config.json`, not from Alpaca, and Alpaca's account info doesn't say paper or live either. The account number does:
   1. Ask Claude for your Alpaca account info (read only). Note the `account_number` it shows.
   2. In the Alpaca dashboard, switch to your paper account and find its account number (upper left). Check that the two match.
   3. Add that number to `trader\config.json` yourself: `"alpaca_account": "PA1234ABCD"` (your own number, in quotes).

   From then on, before the first ticket of each day, Claude checks that Alpaca is still connected to that account (`ticket.py account`). If it isn't, the check says **STOP** and nothing can be placed. That catches live keys, or a typo in `ALPACA_PAPER_TRADE`. Write that setting exactly as `"true"`. Repeat these steps whenever you change the keys.

Keys never go in the chat. If you paste one by mistake, delete the message and make new keys in the Alpaca dashboard.

To use Interactive Brokers instead:
1. Add its connector from Customize > Connectors > Browse connectors.
2. Sign in there, not in the chat.
3. Set `"broker": "ibkr"` in your config.

IBKR also asks you to approve each order inside IBKR.

## 5. Saying yes to an order

1. Claude shows one ticket at a time and asks: "Place this order? Reply yes or no."
2. Reply `yes` (or `y`) or `no`. You may add the order's own details, like `yes buy 8 TSLA`, but nothing else. A doubt ("yes never mind"), another stock ("yes NVDA too"), another number or any other word means it doesn't count, and Claude asks again. If you have any doubt, reply `no`.
3. Answer within 15 minutes of the risk check. If you answer later, Claude runs the risk check again first, because the price, the entry window or today's limits may have moved.
4. Claude's own card appears. Read the order on it (symbol, buy or sell, quantity, stop-loss, take-profit), then click **Allow once** or **Deny**.
5. Claude records what happened (`placed`, `denied` or `failed`), and later records the fills. If a buy fills above the price it was checked at, the log keeps the bigger loss at the stop, and today's loss limit counts that.

One yes covers one order. If an order **failed** (or you clicked Deny, or said no), it can be tried again the same day: fix what stopped it, run `/trader-risk` again, and give a new yes. The ticket says so: `a new risk check (check.py) makes it READY for a new yes`. The usual case is the end-of-day sell that fails because the bracket's stop-loss and take-profit still hold the shares: cancel those two, run the risk check, say yes again. A **placed** order is never offered twice.

Want a hard cap on the price you pay? Ask for `--limit cap` when sizing (`/trader-budget`): a limit order at the highest price where your max loss and reward:risk still hold. It can't fill worse, but it may not fill at all. Market orders stay the default.

Replays that run unattended (like `paper-trading-scenarios`) give their yes with `ticket.py approve --auto-yes`. The tool accepts that only on a practice run (`--now` or `--today`) with `"paper": true` in the config, and records it as `auto_yes`, never as your reply. A practice run never places anything, so an automatic yes can't reach Alpaca. On the real clock, or with `"paper": false`, it refuses, and every real order still needs your typed yes and your Allow once. A replay records its stop-loss and take-profit fills with `ticket.py fill --practice`, into `trader\practice\`.

## 6. The other tools

- **News**, `/trader-news` or `/trader-news TSLA NVDA`:
  - Shows headlines and 8-K filings since the last check, newest first, tagged (earnings, analyst, legal, deal, halt, and so on). `!` marks an important filing.
  - Avoid very small windows like `--hours 1`. Items outside the window get marked as seen, so they won't show later.
- **Earnings analyst**, `/trader-analyst TSLA`:
  - Shows revenue, margins, net income and EPS quarter by quarter, growth against a year earlier, the last four quarters, and links to the latest 10-Q and earnings release.
  - Needs `sec_user_agent`.
- **Pelosi tracker**, `/trader-pelosi pelosi`: Congress members' public trade disclosures (PTRs).
  - "Alert me when Pelosi files" runs `ptr.py new --member pelosi --read`. Keep using the same `--member` every time, because switching it can flood you with old filings.
  - Reading the PDFs needs `pdftotext`. Git Bash on this PC has it; otherwise Claude reads the PDF itself.
  - A PTR arrives days or weeks after the trade. It is public information, not a tip.
- **Price alerts**, `/trader-alerts`: for example "is the market open", "get me a TSLA quote", "alert me if KO moves 3%" or "alert me at 15:55". Each alert rings once.

## 7. Running the tools yourself

You can run any tool without Claude. From your workspace folder, in PowerShell:

```powershell
$T = "C:\Users\gurka\Desktop\day-trader-skill-main"
cd $env:USERPROFILE\trading-desk
python "$T\trader-scanner\scan.py" universe
python "$T\trader-scanner\scan.py" daily
python "$T\trader-scanner\scan.py" orb --at 10:15
python "$T\trader-news\news.py" --tickers TSLA --hours 12
python "$T\trader-entry\entry.py" check TSLA --write
python "$T\trader-entry\entry.py" watch TSLA --until 15:30
python "$T\trader-exit\exit.py" plan --stop or-mid --target 1R --write
python "$T\trader-timemachine\timemachine.py" run --write --until 15:55
python "$T\trader-budget\size.py" --write
python "$T\trader-risk\check.py"
python "$T\trader-broker\ticket.py" account --number PA1234ABCD   # your account_number, from get_account_info
python "$T\trader-broker\ticket.py" show
python "$T\trader-alerts\alerts.py" add --from-plan
python "$T\trader-alerts\alerts.py" check
python "$T\trader-analyst\facts.py" TSLA
python "$T\trader-pelosi\ptr.py" index --member pelosi
```

Every tool prints its help with `--help`, for example `python "$T\trader-exit\exit.py" plan --help`.

Flags most tools share:

| flag | meaning |
| --- | --- |
| `--now "2026-11-02 10:15"` | Pretend it is this New York time (for replays and tests); add `Z` for UTC. For the risk check and the order ticket, `--now` and `--today` make a practice run, which can never lead to an order and keeps its own files in `trader\practice\`. On `ticket.py fill` it only dates the fill, which stays real; `fill --practice` records a practice one. |
| `--offline PATH` | Read saved files instead of the internet. |
| `--json` | Machine-readable output. |
| `--write` | Save the result into `trader\` (entry, exit, timemachine, budget, and alerts `quote`). |
| `--no-write` / `--dry-run` | Look without saving: `--no-write` for scanner, risk and alerts `check`; `--dry-run` for news and pelosi `new`. |
| `--config PATH` | Use another config file (the default is `trader\config.json`). |
| `TRADER_HOME` (environment) | Where `trader\` is, for every tool and from any folder (see 1.3). Unset: the `trader\` folder where you run. |

A few one-tool options worth knowing: `size.py --limit cap` (a capped limit entry, see section 5), `timemachine.py run --block 6` (replays that draw runs of 6 bars from one past day, so calm and wild stretches clump the way real days do; the default `--block 1` is the reel's way), and `timemachine.py run --min-volume-x 1.5` (the history only counts breakouts on 1.5 times the opening range's volume, like `entry.py --min-volume-x 1.5`).

Exit codes, which are useful in scripts:
- `0`: found, triggered, passed or done.
- `1`: nothing, blocked, refused or an error.
- `2`: no input, or a hard error (news, analyst, pelosi).

## 8. When something goes wrong

| what you see | what to do |
| --- | --- |
| `Python was not found` | See step 1.1. |
| `/trader-scanner` is unknown | Start a new Claude Code session. Installed as a plugin? Then the name is `/day-trader-skill:trader-scanner`. Check the folder with `Get-ChildItem $env:USERPROFILE\.claude\skills`. |
| `SEC filings skipped` or `SEC requires a User-Agent` | Put your real name and email in `sec_user_agent` in `trader\config.json`. |
| `HTTP 429`, or "Yahoo did not answer" | The free data source is rate-limiting you. Wait a minute and try again. |
| `No session on ... the market is closed` | A weekend or an NYSE holiday (by the New York date). |
| `config: ... must be ...` | A value in `trader\config.json` that the tools can't read. Fix that key the way the message says (see 1.4). Don't set it to `null` just to get past the error: that switches the limit off. |
| `WARNING: these checks are off in your config` | A limit is `null`. Put a number back unless you really meant it. |
| `WARNING: nothing logged yet at ...` | Normal on your first day. After that, Claude Code is open in a different folder from your workspace, so today's trades aren't counted: see 1.3 (or set `TRADER_HOME`). |
| `NOT READY: trader-risk has not checked these orders` | Run `/trader-risk`. |
| `NOT READY: the risk check is from ..., not today` | Run `/trader-risk` again. |
| `NOT READY: the risk check is 16 minutes old, and a PASS counts for 15` | Run `/trader-risk` again. |
| `NOT READY: the risk check was a practice run` | It ran with `--now` or `--today`. Run `/trader-risk` again without them. |
| `NOT READY: the last risk check stopped with an error` | Fix what the error says, then run `/trader-risk` again. |
| `NOT READY: this exact order was not in the last risk check` | The order changed after the check. Run `/trader-risk` again. |
| `NOT READY: no Alpaca account pinned yet` | Do section 4, step 5. |
| `NOT READY: the Alpaca account is not checked today` | Ask Claude to check the Alpaca account (account info, then `ticket.py account`). |
| `STOP: the Alpaca server is connected to account ...` | Alpaca's server is using other keys or another mode than the account you pinned, maybe your live account. Place nothing. Fix the server config, restart Claude, and check again. |
| `PRACTICE RUN` at the top of a ticket | The clock was pinned with `--now` or `--today`. For a real order, run without them. |
| `DONE: failed at ...; a new risk check (check.py) makes it READY for a new yes` | Fix what stopped it (for a flatten sell: cancel the bracket's open legs), run `/trader-risk` again, then give a new yes. |
| `Holiday list covers 2026 to 2028 only` | Check the date on nyse.com. |
| `error: skipped a rule: ...` (alerts) | One alert rule in `trader\alerts.json` is broken. The others, your stop included, were still checked. Fix that rule (a price without quotes, a ticker in quotes). |
| `... is not valid JSON`, or `should be one JSON object` | A file in `trader\` is damaged. Fix it, or move it aside (moving a `state\` file aside only resets that tool's memory; alerts, news and pelosi start a fresh one by themselves and say so). |
| A command starting with `/trader-` fails in PowerShell | Those are typed in Claude Code, not in PowerShell. |

## 9. Before you ever use real money

- [ ] Weeks on a paper account, and you can explain every step's output.
- [ ] `/trader-risk` runs without a config error, and its report lists no checks as off.
- [ ] `place_stock_order` is set to Needs approval.
- [ ] You always open Claude Code in the same workspace folder, or `TRADER_HOME` points at your trader folder.
- [ ] `alpaca_account` holds the number you checked in the Alpaca dashboard, and the daily account check passes. If you ever go live, you pin the live number deliberately, yourself.
- [ ] You know how to close a position in the broker's own app, even at 2:25 AM IST.
- [ ] You have read the "Possible vulnerabilities" and "Known bugs" sections of `CLAUDE.md` in this folder (what was fixed, and what is left).

Nothing here is tax or legal advice.
