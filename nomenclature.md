# Nomenclature: how things are named in this code

This is a map of the naming scheme in the 11 tools, so you can read the code quickly and rename things safely.
- Sections 2 to 9 describe the conventions.
- Section 10 covers the JSON files the tools share.
- Section 12 is an index of every module.
- Section 13 is a checklist for renaming.

The code is all standard-library Python and follows one style throughout: names that say what they hold, one script per skill folder, and plain-English messages. The names were spelled out on 2026-11-02 (before that, most variables were one or two letters); nothing a user sees changed: the flags, the JSON and config keys and the messages are the same.

## 1. Before you rename anything: what is safe

| kind of name | safe to rename? | where else it appears |
| --- | --- | --- |
| Local variables, and `_private` helpers used in one file | Yes | Only that file (but some `_helpers` are used by tests; see the next table). |
| Public module functions and constants | Carefully | The tests load each script and call its functions by name (see the next table). |
| CLI subcommands and flags (`orb`, `--min-price`) | Carefully | Every `SKILL.md`, `README.md`, `howtouse.md`, `CLAUDE.md`, and the tests that call `main([...])`. |
| Keys in the `trader\*.json` files | Carefully | Every other script that reads that file (section 10). Old files already in `trader\` keep the old key. |
| Config keys (`max_loss_per_trade_usd`) | Carefully | Users' own `trader\config.json`, `config.example.json`, `tests/test_repo.py` (it checks some example keys) and the SKILL.md files. |
| Alpaca order fields (`ALPACA_FIELDS`) | **Never** | These are Alpaca's API names. They are also the input to the order fingerprint, which must match between trader-risk and trader-broker. |
| Duplicated helpers (time, holidays, fingerprint, `rules`, `finite_number`, `clean`, `body_of`, `home`) | Only all at once | Every copy, in several scripts (`CLAUDE.md`, "Duplicated code"). `tests/test_repo.py` (`SharedCode`) fails when the copies disagree. |

### Names the tests call directly

Renaming any of these means editing the matching `tests/test_*.py` file.

| script | names the tests use |
| --- | --- |
| `scan.py` | `ScanError`, `BACKOFF`, `NASDAQ_FILES`, `SLEEP`, `_read_maybe_gz`, `build_universe`, `chart_path`, `classify`, `clock_label`, `daily_closes`, `eastern_offset`, `eastern_to_utc`, `fetch_spark`, `http_get`, `load_offline`, `main`, `orb_candidate`, `orb_hit`, `orb_window`, `parse_chart`, `parse_hhmm`, `parse_now`, `parse_spark`, `parse_symdir`, `rsi`, `scan_daily`, `scan_orb`, `session_bars`, `spark_path`, `symbols_from_file`, `to_yahoo`, `ts_to_eastern`, `usable`, `yahoo_get`, `calendar_note`, `find_setups`, `session` |
| `news.py` | `NewsError`, `CONTROL`, `Fetcher`, `body_of`, `cik_map`, `clean`, `dedupe`, `main`, `parse_chart`, `parse_now`, `parse_rss`, `parse_sec_atom`, `parse_time`, `remember`, `render_moves`, `sec_user_agent`, `tag`, `to_eastern`, `watch_from_files`, `yahoo_symbol` |
| `entry.py` | `EntryError`, `BACKOFF`, `SimClock`, `_open_url`, `_sleep`, `chart_path`, `chart_range`, `describe`, `evaluate`, `get_chart`, `main`, `parse_chart`, `parse_hhmm`, `parse_now`, `to_eastern`, `ts_of`, `utc_of`, `watch_loop` |
| `exit.py` | `PlanError`, `atr_of`, `compute`, `load_chart`, `parse_stop`, `parse_target`, `session_bars` |
| `timemachine.py` | `TMError`, `BACKOFF`, `EPOCH`, `PAUSE`, `USER_AGENT`, `fetch_text`, `_sleep`, `cents_away`, `chart_result`, `chart_url`, `eastern_offset`, `eastern_to_utc`, `fetch_chart`, `full_sessions`, `lean`, `main`, `normalize_plan`, `parse_bars`, `parse_now`, `report`, `split_sessions`, `time_machine`, `to_eastern`, `utc_of`, `verdict`, `verdict_notes`, `entry_window`, `parse_clock` |
| `size.py` | `SizeError`, `bracket_order`, `limit_cap`, `main`, `orders_from`, `parse_weights`, `report`, `size`, `size_trade`, `weights_from_arg` |
| `check.py` | `RiskError`, `ALPACA_FIELDS`, `check`, `day_trades`, `fingerprint`, `load_prices`, `main`, `parse_now`, `real_only`, `rules`, `to_eastern`, `utc_now`, `window_check` |
| `ticket.py` | `ALPACA_FIELDS`, `fingerprint`, `main`, `max_loss`, `parse_yes`, `plain`, `status_of`, `tool_input`, `utc_now` |
| `alerts.py` | `AlertError`, `BACKOFF`, `NYSE_EARLY_CLOSE`, `NYSE_HOLIDAYS`, `PAUSE`, `USER_AGENT`, `YAHOO_HOSTS`, `_sleep`, `_urlopen`, `eastern_offset`, `evaluate`, `get_quotes`, `main`, `market_status`, `parse_now`, `parse_stooq`, `parse_yahoo`, `rules_from_plan`, `stooq_url`, `validate`, `yahoo_url` |
| `facts.py` | `Fetcher`, `analyse`, `changes`, `exhibit_name`, `latest_filings`, `main`, `ny_now`, `quarterly`, `user_agent` |
| `ptr.py` | `PtrError`, `CONTROL`, `MAX_BYTES`, `fetch_and_read`, `iso_date`, `load_index`, `main`, `new_since`, `parse_index`, `parse_ptr_text`, `read_index_bytes`, `render_report`, `select`, `watchlist` |

`tests/test_repo.py` (`SharedCode`) also reads, in every script that has them: `home`, `eastern_offset`, `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS`, `fingerprint` and `ALPACA_FIELDS`, `clean` and `CONTROL`, and the `rules`, `BOOL_KEYS` and `NUMBER_KEYS` of check, size and ticket.

## 2. Case styles

| what | style | examples |
| --- | --- | --- |
| Skill folders | `trader-<job>`, lowercase with hyphens | `trader-scanner`, `trader-timemachine` |
| Scripts | one lowercase word | `scan.py`, `news.py`, `entry.py`, `exit.py`, `timemachine.py`, `size.py`, `check.py`, `ticket.py`, `alerts.py`, `facts.py`, `ptr.py` |
| Constants and compiled regexes | `UPPER_SNAKE_CASE`, spelled out, at the top of the file | `USER_AGENT`, `NYSE_HOLIDAYS`, `OCC_OPTION_SYMBOL`, `REPLY_TOKEN`, `HALF_CENT`, `BAND_QUANTILES` |
| Functions | `snake_case`, a verb phrase or the noun it returns | `parse_now`, `fetch_spark`, `starting_account`, `verdict` |
| Private helpers | a leading underscore, kept only for one time helper and the test seams | `_nth_sunday`, `_open_url`, `_sleep`, `_urlopen`, `_read_maybe_gz` |
| Classes | `CapWords` | one error per script (`ScanError` ... `PtrError`), `Fetcher`, `SimClock` |
| CLI subcommand handlers | `cmd_<subcommand>(args)` | `cmd_orb`, `cmd_plan`, `cmd_approve` |
| Entry point | `main(argv=None)`, returns the exit code | `if __name__ == "__main__": sys.exit(main())` |
| Test seams (tests swap these out) | a module-level alias | `SLEEP` (scan); `_sleep` (entry, exit, timemachine, alerts); `_urlopen` (alerts); `_open_url` (entry, exit); `fetch_text` (timemachine); `utc_now` (check, ticket: the real clock); `MAX_BYTES` (ptr) |
| Nested helpers | a short phrase for what they return | `value_at`, `column`, `field`, `fresh`, `slot`, `draw`, `units`, `confirm`, `on_trigger`, `common`, `net`, `clock` |

The folder names contain hyphens, so Python can't `import` them normally. The tests load a script with `tests/_load.py`, for example `load("trader-risk", "check")`, which registers it as the module `trader_check`.

## 3. Function vocabulary

| pattern | means | examples |
| --- | --- | --- |
| `parse_*` | text or raw JSON in, a Python value out; raises the script's error on bad input | `parse_now`, `parse_hhmm` (alerts' returns `None` instead), `parse_clock`, `parse_utc`, `parse_window_time`, `parse_date` (facts), `parse_date_flag` (ptr), `parse_time`, `parse_chart`, `parse_spark`, `parse_rss`, `parse_sec_atom`, `parse_stop`, `parse_target`, `parse_weights`, `parse_yes`, `parse_symdir`, `parse_bars`, `parse_stooq`, `parse_yahoo`, `parse_index`, `parse_ptr_text` |
| `read_*`, `load_*` | from disk, or from a saved fixture | `read_json`, `read_config`, `read_text`, `read_offline`, `read_symbols`, `read_index_bytes`, `read_rules_doc`, `read_alert_state`, `read_metrics`, `load_config`, `load_offline`, `load_plan`, `load_prices`, `load_state`, `load_index`, `load_chart`, `load_json`, `load_orders` |
| `write_*`, `save_*` | to disk, always atomically (a `.tmp` file, then a rename) | `write_json`, `write_orders`, `write_prices`, `write_verdicts`, `write_state`, `save_state` |
| `fetch_*`, `*_get`, `get_*`, `download` | the network (or a fixture with `--offline`) | `fetch_spark`, `fetch_chart`, `fetch_yahoo`, `fetch`, `fetch_text`, `fetch_and_read`, `yahoo_get`, `http_get`, `_open_url`, `get_chart`, `get_quotes`, `download` |
| `*_url`, `*_path` | build a URL or a URL path, with no I/O | `chart_url`, `stooq_url`, `yahoo_url`, `doc_url`, `spark_path`, `chart_path` |
| `*_text` | a value formatted for people, with no I/O | `price_text` (`"358.32"`), `number_text` (`"1.5"`), `percent_text`, `signed_percent_text`, `millions_text`, `ratio_text`, `hhmm_text` (`"09:45"`), `clock_text` (`"9:45"`), `ampm_text` (`"9:45 AM"`), `until_text`. Not formatters: `read_text`, `offline_text`, `fetch_text`, `child_text`, `pdf_to_text` and `parse_ptr_text` get text from somewhere. |
| `dollars`, `alpaca_*`, `utc_iso` | other formatters | `dollars` (`"$46.88"`), `alpaca_price` and `alpaca_number` (the strings Alpaca wants), `utc_iso` (`"...Z"`), `iso_date` |
| `round_*`, `to_*` | a rounding or a conversion | `round_to_cent`, `round_down_to_cent`, `round_price`, `round_to_unit`, `to_decimal`, `to_eastern`, `to_yahoo` |
| `render*`, `report`, `describe`, `plain*`, `*_line`, `*_report` | results to lines of text for people | `render`, `render_moves`, `render_chart`, `render_report`, `render_index`, `report`, `plan_report`, `describe`, `plain`, `plain_parts`, `bar_line` |
| `cmd_*` | one CLI subcommand | `cmd_universe`, `cmd_check`, `cmd_run`, `cmd_flatten` |
| `*_to_*` | a conversion | `eastern_to_utc`, `ts_to_eastern`, `pdf_to_text` |
| `*_of` | "the X of this thing" | `utc_of`, `ts_of`, `atr_of`, `status_of`, `body_of` |
| `*_for` | "X for this input" | `shares_for`, `facts_for` |
| `*_from*` | build something from a source | `rules_from_plan`, `orders_from`, `weights_from_arg`, `watch_from_files`, `symbols_from_file`, `series_from_result` |
| `is_*` | a yes/no question | `is_bracket`, `is_entry_record`, `is_number` |
| `scan_*`, `orb_*` | scanner rules | `scan_daily`, `scan_orb`, `orb_candidate`, `orb_hit`, `orb_window` |
| a bare noun | a computation named for what it returns | `rsi`, `session`, `usable`, `verdict`, `lean`, `margin`, `starting_account`, `order_kind`, `fingerprint`, `item_fingerprint`, `percentile`, `percent_change`, `year_ago_value`, `amount_range` |
| `rules(config)` | the config merged over `DEFAULTS` and checked; raises the script's error on a value it cannot read | check, size, ticket |
| `switched_off(settings)` | the warnings for limits that are `null` and risks that are switched on | check |
| `*_gap`, `risk_verdict` | why something is not ready, or `None` when it is | `account_gap`, `risk_verdict` (ticket) |
| `clean`, `no_entities`, `body_of` | fetched data made safe: text without control codes; XML without entity declarations; an answer with a size cap | news, facts, ptr |
| `finite_number(value)` | a float, or `None` if it isn't a finite number; never raises | size, check, ticket |
| `positive_price(value, what)` | a positive price, or the script's error | timemachine |
| `to_decimal(value)` | a `Decimal` | exit only |

## 4. Variable names

Every variable says what it holds, and a word means the same thing in every script. One-letter names are gone, except `_` for a value that is thrown away. The few short names left are standard terms: `rsi`, `ttm`, `eps`, `nth`, and `at` (the `--at` time in `orb_window`). The keys inside a bar dict (`o`, `h`, `l`, `c`, `v`, `t`) are data, not variables: they stay short because the JSON files use them.

| name | holds | where / notes |
| --- | --- | --- |
| `args` | the parsed command-line arguments (argparse) | every `cmd_*` and `main` |
| `parser`, `commands`, `command`, `group` | the ArgumentParser, its subparsers, one subcommand's parser, an argument group | inside `main()` (ptr: `build_parser`) |
| `config` | the config dict as read from `trader\config.json` | everywhere. In news's `sec_user_agent` and facts' `user_agent`, `config` is the path and `config_doc` the dict. |
| `settings` | the config merged over `DEFAULTS` and checked by `rules()` | check, size, ticket |
| `max_loss_cap`, `min_reward_risk`, `daily_loss_cap`, `max_trades`, `max_orders` | the limits a check applies; `None` means that limit is off | check |
| `pinned` | `True` when `--now` or `--today` pinned the clock: a practice run | ticket (`clock` is `"real"` or `"pinned"` in check `main`) |
| `account_record` | today's Alpaca account check record | ticket |
| `want`, `got` | the pinned account number, and the one `get_account_info` showed | ticket `cmd_account` |
| `fills` | the records with status `filled` logged today | check |
| `tokens`, `words`, `extra`, `unknown_numbers`, `order_numbers` | the reply split up; its words; words a yes may not carry; numbers this order does not have; the numbers it has | ticket `parse_yes` |
| `error`, `file`, `tmp_path`, `folder` | a caught exception (or one message in a list of them); an open file; the `.tmp` file an atomic write goes through; the folder it lives in | everywhere |
| `doc` | a parsed JSON document (a file or a response) | everywhere |
| `record` | one record in `trader\state\broker.json` | check, ticket (facts' `recent` is SEC's table of recent filings) |
| `row` | one row of a table: a check result, an index row, a portfolio row, a filing | everywhere |
| `result`, `results` | one result dict; Yahoo's `chart.result` list | everywhere |
| `order`, `other` | one order dict; another order in the same batch | check, ticket, size |
| `verdict` | trader-risk's verdict row for one order | ticket |
| `order_fingerprint`, `item_fp` | the order fingerprint (check, ticket); a news item's fingerprint | |
| `symbol`, `yahoo_sym`, `ticker` | a ticker in dot form (`BRK.B`); its Yahoo form (`BRK-B`); `ticker` where the source calls it that (portfolio, weights, news) | everywhere |
| `position`, `portfolio`, `lot` | one portfolio position; the portfolio doc; one lot of shares | check, ticket, alerts, size, ptr |
| `hit` | one scanner hit | scan, entry |
| `bar`, `bars` | one bar dict; a list of them, oldest first | scan, entry, exit, timemachine |
| `slot`, `index`, `entry_bar`, `last_bar` | 5-minute bar numbers, slot 0 being the 9:30 bar; the plan's entry bar; the `--until` bar | entry, scan, timemachine |
| `quote` | Yahoo's quote indicators, or one quote dict | chart parsers; alerts |
| `match`, `pattern` | a regex match; a compiled regex | everywhere |
| `timestamp` | unix seconds | everywhere |
| `utc_time`, `now_utc` | a naive UTC datetime | everywhere (news uses an *aware* UTC datetime) |
| `eastern`, `now_et` | a naive New York datetime | everywhere |
| `today`, `day` | the New York date: a `"YYYY-MM-DD"` string in size, check, ticket and alerts, a `date` in scan, entry, exit and ptr; `day` is a session date | |
| `clock_time`, `hours` | a `datetime.time`; a session's `(open, close)` times, or `None` when it is closed | |
| `range_high`, `range_low`, `opening_range`, `range_bar_count` | the opening range: its high and low, its dict, how many bars it has | scan, entry, exit, timemachine |
| `trigger`, `trigger_level`, `volume_x` | the bar that fired; the price its close had to beat; its volume over the range's average | entry |
| `entry_ref`, `entry_ref_from` | the entry price the risk is measured from, and why that price | check, size |
| `entry_price` | a plan's entry | exit `compute` (a Decimal), timemachine history |
| `risk_cents`, `reward_cents`, `stop_cents`, `target_cents` | amounts in **integer cents** | check, size |
| `risk_per_share`, `reward_risk` | the risk a share in dollars; (target - entry) / (entry - stop) | everywhere |
| `sizing` | the sizing dict from `size_trade` | size |
| `slices` | the dollars for each ticker in weights mode | size |
| `monte_carlo` | the Monte Carlo result (its JSON key stays `mc`) | timemachine |
| `random_source` | the seeded `random.Random` | timemachine `simulate` |
| `open_ratio`, `high_ratio`, `low_ratio`, `close_ratio` | a replayed bar, as ratios to the bar before | timemachine `simulate` |
| `p_target`, `p_stop`, `p_neither`, `lean_result` | probabilities; the lean tuple (gap, share, break_even) | timemachine |
| `status` | the market status dict; a record's status | alerts; ticket |
| `rule`, `rule_id`, `rule_type`, `ring_at` | one alert rule, its id, its type, when a time rule rings | alerts |
| `accession`, `cik` | an SEC accession number; the 10-digit SEC company id | news, facts |
| `market_time` | Yahoo's `regularMarketTime` | alerts, timemachine |

## 5. Suffixes, units and time

| suffix | means | examples |
| --- | --- | --- |
| `_utc` | a naive UTC datetime | `now_utc`, `utc_time` |
| `_et`, `eastern` | a naive New York datetime | `now_et`, `eastern` |
| `_dt` | a datetime (not just a time) | `open_dt`, `close_dt` |
| `_at` | when something happens, a datetime | `checked_at`, `decided_at`, `cutoff_at`, `flat_at`, `price_at`, `ring_at`, `rang_at` |
| `_min` | minutes after midnight | `OPEN_MIN`, `CLOSE_MIN`, `until_min` (timemachine) |
| `_cents` | integer cents | `risk_cents`, `reward_cents`, `stop_cents`, `target_cents`, `amount_cents`, `price_cents` |
| `_pct` | a percent, 0 to 100 | `cash_buffer_pct`, `max_position_pct`, `buffer_pct`, `position_cap_pct`, `pct_over`, `run_pct`, `risk_pct` |
| `_usd` | dollars | `max_loss_per_trade_usd`, `value_usd`, `amount_usd` |
| `_x` | a multiple of something | `min_volume_x`, `volume_x` |
| `_r`, `R` | a multiple of the risk | `target_r`, `r_target`, `r_multiple`, `avg_R` |
| `_per_share` | per share | `risk_per_share` |
| `_count` | a count | `day_trade_count`, `range_bar_count`, `share_count`, `unit_count` |
| `_text` | text for people | `price_text`, `qty_text`, `leg_text`, `rule_text` |
| `_yoy` | vs the same quarter a year earlier | `revenue_yoy` |
| `_ttm` | the trailing twelve months | `revenue_ttm` |
| `p_*` | a probability, 0 to 1 | `p_target`, `p_stop`, `p_neither`, `p_green` |

Time rules:
- **Inside the code**, times are naive UTC datetimes (no tzinfo). news.py is the exception: it uses aware UTC.
- **For people and for `--now`**, times are New York wall-clock time. The offset (-4 or -5) is computed by `eastern_offset`; the machine's time zone is never used.
- **`--now` without an offset** means New York time in every tool (news.py too); add `Z` or `+00:00` for UTC.
- **Stored timestamps** are UTC with a `Z` (`"2026-10-19T14:15:00Z"`). The broker log uses New York time with its offset (`"2026-10-19T10:15:00-04:00"`, from `ny_iso`).
- **Dates** are `"YYYY-MM-DD"`, the New York date.
- **Verdict times.** In `risk.json`, `now_utc` is the clock the check ran on (the pinned time in a practice run) and `checked_at` is always the real time it ran. A verdict counts for `VERDICT_MINUTES` (15) after `now_utc`, and only for a ticket on the same kind of clock.
- **Clock strings:**
  - For data, `"15:55"` in 24-hour form: the config and the contracts (`hhmm_text`, `clock24`).
  - For people, `"10:15 AM"`: `ampm_text`, `clock12`, `clock_label`. `entry.json` `time` and `plan.json` `entry_time` use this form.
- **Bars:**
  - `slot`, `bar_index` and `entry_bar` count 5-minute bars from 0, and slot 0 is the 9:30 bar. Slot `k` starts at 9:30 + 5k minutes.
  - A trigger's time is its bar's **close** time: the 10:10 bar is slot 8 and closes at 10:15.
- **Prices** are floats rounded to cents for display. Alpaca prices are strings with two decimals (`"358.32"`).

## 6. Domain abbreviations

| short | long |
| --- | --- |
| OR, ORB | opening range (the first 15 minutes), opening-range breakout |
| R | the risk per share (entry minus stop); targets are multiples of R |
| reward:risk, `reward_risk` | (target minus entry) / (entry minus stop) |
| ATR | average true range (exit's `atr:K` stop) |
| RSI | relative strength index (Wilder, 14 days) |
| MA20 | the 20-day moving average of closes |
| spark, v8 | Yahoo's two chart endpoints: spark (closes for 20 symbols a request) and v8 (one symbol, full bars) |
| TTM, YoY | trailing twelve months; year over year |
| EPS | earnings per share (diluted) |
| XBRL, CIK, accn | SEC's tagged financial data; the company id; a filing's accession number |
| 8-K, 10-Q, 10-K, EX-99.1 | SEC forms: an event, a quarter, a year; the press-release exhibit |
| PTR, FD, DocID | Periodic Transaction Report; Financial Disclosure (the House index); the House document id |
| SP, JT, DC | the owner in a PTR: spouse, joint, dependent child |
| PDT | pattern day trader (the old $25,000 rule) |
| OCC | the option-symbol format (`TSLA261120C00400000`, matched by `OCC_OPTION_SYMBOL`) |
| tif, `time_in_force` | `day`, `gtc`, ... |
| bracket | an entry plus a stop-loss plus a take-profit that cancel each other |
| notional | an order sized in dollars instead of shares |
| `mc`, pool, bands, hist | the Monte Carlo result (`monte_carlo` in the code); per-slot bar ratios; fan-chart percentiles; the histogram of closes |
| green, neither | target first or above the entry at the end; still open at the end |
| `USER_AGENT` | the User-Agent header (the SEC needs your name and email) |

## 7. Same name, different meaning

These names exist in more than one script but do different things. Read the file you're in.

| name | meaning by script |
| --- | --- |
| `dollars(amount)` | scan `"$1,234.56"`; exit (Decimal) `"$1,234.56"`; size, check and ticket drop `.00` (`"$5,000"`); facts (nested in `changes`) `"$1.23B"` |
| `number_text` | scan and alerts `"50"` or `"50.25"`; check `"1.5"` (up to 4 decimals); exit a Decimal without trailing zeros |
| `price_text` | check and ticket `"358.32"`; scan `"1,234.56"` (with a thousands comma) |
| `percent_text` | check `"25"` or `"2.50"` (the `%` is in the sentence); facts `"12.3%"`; timemachine `"29.4 %"` (it takes a fraction) |
| `round_to_cent` | size and check: a float, half up; exit: a Decimal, half up |
| `read_json` | exit: one object or a PlanError; check and ticket: `kind=dict` refuses anything but one object, `required=False` allows a missing file; size: a SizeError; alerts: `read_json(path, default)` gives the default when the file is missing |
| `load_chart` | entry: the chart a command works on (offline file or Yahoo), and the time to use; exit: the bars for an ATR stop |
| `http_get` | scan: the body; alerts: `(HTTP status, body)` |
| `dedupe` | news: one item per story; alerts: a list without repeats, in order |
| `compute` | exit: the stop and target math; timemachine: load the plan and the data, then run |
| `describe` | entry: the status lines; exit: the plan lines; ptr: one PTR row |
| `report` | timemachine, size (weights mode), check: their printed report |
| `render` | news: the headlines; facts: the quarter table; ticket: one ticket |
| `select` | news: the items inside the time window; ptr: index rows by member, type or date |
| `session` | scan, entry, exit, alerts: the `(open, close)` times for a date |
| `parse_chart` | scan: a series dict; news: a price-move dict; entry: a bars dict |
| `parse_hhmm` | scan, entry, exit: `"15:55"` to a `time`, or the script's error; alerts: `None` for a bad time |
| `read_config` | scan: the dict (one JSON object, or a ScanError); entry and exit: merged with `DEFAULTS` (entry also validates the times) |
| `load_prices` | size: a file path to `{SYM: price}`; check: a parsed doc to `(prices, times)` |
| `parse_clock` | exit: `"3:05 PM"` to a `time` (a bare `"3:05"` is 03:05); timemachine: to minutes after midnight, where a bare 1 to 8 o'clock means **PM** |
| `utc_iso`, `iso_date` | check, size and entry: `utc_iso` turns a UTC datetime into `"...Z"`; ptr's `iso_date` turns `"8/21/2026"` into `"2026-08-21"` |
| `fingerprint`, `item_fingerprint` | check and ticket: `fingerprint`, sha256 of the Alpaca fields (the two must match); news: `item_fingerprint`, sha1 of an item id |
| `evaluate` | entry: a setup's status; alerts: which rules ring |
| `usable` | scan: every 5-minute slot of a chart is there; alerts: `(the rules that check out, a message for each skipped)` |
| `parse_now` | every tool: `--now` to naive UTC (no offset = New York); news: the same, but aware UTC |
| `load_state` | ticket: the broker log, or a TicketError; ptr: the pelosi state, a broken one starts fresh |
| `ny_now` | facts and ptr: New York wall-clock time now (they have no `parse_now`) |
| `slot`, `slot_time` | scan's `slot_time(day, index)`: the datetime of a slot; timemachine's nested `slot`: a random slot near a bar |
| `write_json` | scan, entry, exit, facts, ticket, alerts and timemachine: an atomic write (tmp file, then rename); size's is `write_orders`, check's `write_verdicts`, news's `write_state`, ptr's `save_state` |
| `Fetcher` | news and facts: two separate classes |
| `watchlist` | ptr: the set of tickers to flag; elsewhere, the `watchlist.json` path |
| `rules` | check: every limit, raises `RiskError`; size: the sizing keys, `SizeError`; ticket: `paper`, `broker`, `alpaca_account` and `budget`, `TicketError`. Not alerts' `rules` (the alert rules list). |
| `MAX_BYTES` | news 16 MB; ptr 64 MB (a download, or the index XML unzipped); facts 100 MB |
| `CONTROL` | news and facts: every control code; ptr: the same but a tab stays a tab (PTR columns) |
| `clean` | news, facts, ptr: fetched text without control codes. facts' `clean_title` is different: it drops the `/DE/`-style state suffix. |
| `parse_utc` | check, size, ticket: `"...Z"` to naive UTC, or `None` |

## 8. Errors, messages and exit codes

- **One exception per script:** `ScanError`, `NewsError`, `EntryError`, `PlanError`, `TMError`, `SizeError`, `RiskError`, `TicketError`, `AlertError`, `FactsError`, `PtrError`.
  - It is raised with a full sentence that says what went wrong and how to fix it.
  - `main()` catches it and prints one line to stderr, prefixed `scan.py:` ... `ticket.py:`. news, facts and ptr print `news:`, `facts:` and `ptr:`.
- **Message style:**
  - Plain words and short sentences.
  - Never an em dash (a test fails if one appears outside `tests/fixtures`).
  - The fixed phrases "Nothing was placed." and "Education and tooling, not financial advice."
- **Exit codes:**

| script | 0 | 1 | 2 |
| --- | --- | --- | --- |
| scan | found hits (`universe`: kept some) | nothing, or an error | |
| news | something new shown | nothing new | no tickers |
| entry | triggered (`watchlist`: any triggered) | anything else, or an error | |
| exit | a plan (`show`: a plan exists) | refused, missing, or an error | |
| timemachine | ran | error | |
| size | an order (weights mode: sized) | refused, or an error | |
| check | every order passed | any block, or an error | |
| ticket | `show`: some ticket READY; `approve`: yes recorded; `account`: the account matches the pin | not ready, not recorded, a STOP, or an error | |
| alerts | `clock`: open; `check`: something rang; `quote`: got a quote | closed, nothing rang, or an error | |
| facts | every ticker worked | some failed | none worked, or no User-Agent |
| ptr | found / new | nothing | an error |

## 9. Command-line conventions

- **Flag names.** Flags are kebab-case, and argparse turns them into snake_case attributes: `--min-volume-x` becomes `args.min_volume_x`. Where a flag is a Python keyword, `dest=` renames it: exit's `--from` is `from_path`, and timemachine's is `from_file`.
- **Flags shared across tools:**
  - `--now`, `--offline`, `--json`;
  - `--config` (default `trader/config.json`);
  - `--today` (check, ticket, ptr `new`). On check and ticket, `--now` or `--today` makes a practice run (`clock: pinned`).
- **ticket `account --number`** takes the `account_number` that Alpaca's `get_account_info` shows.
- **ticket `approve`** takes exactly one of `--reply` (the user's words) and `--auto-yes` (an automatic yes, `args.auto_yes`, for unattended replays; refused unless the clock is pinned and `"paper"` is true).
- **ticket `fill --practice`** (`args.practice`) records a practice fill in `trader/practice/`. Without it a fill is real, even with `--now`, which only dates it.
- **Saving:**
  - `--write [PATH]` is optional, and a bare `--write` uses the default path: entry `check`, exit `plan`, timemachine `run`, size, alerts `quote`.
  - Tools that save by default take `--no-write` (scan, check, alerts `check`) or `--dry-run` (news, ptr `new`). These two words mean the same thing.
  - `--out` names the output file in scan, check and entry `watch`.
- **Default paths** always point inside `trader/`, built by `home("state", "broker.json")`: `$TRADER_HOME` when it is set, else `trader/` where the tool runs. On check and ticket, a practice run's state defaults (`risk.json`, `broker.json`, `portfolio.json`) point inside `trader/practice/` instead; a path you pass is used as given.

## 10. Files and JSON keys

| file | `created_by` | main keys |
| --- | --- | --- |
| `trader/watchlist.json` | `trader-scanner` | `mode` (`daily` or `orb`), `as_of`, `universe`, `scanned`, `failed`, `filters`, `hits[]`. A daily hit has `symbol`, `setups`, `price`, `close_date`, `rsi`, `volume`; an orb hit has `symbol`, `setups` `["orb"]`, `price`, `at`, `bar_start`, `bar_index`, `or_high`, `or_low`, `pct_over`. |
| `trader/entry.json` | `trader-entry` | `symbol`, `setup`, `side`, `date`, `status` (`triggered`, `waiting`, `expired`, `no-data`), `time` (`"10:15 AM"`), `bar_start` (`"10:10"`), `bar_index`, `price`, `level`, `interval`, `opening_range {high, low, minutes}`, `checked_at`, `reason` |
| `trader/plan.json` | `trader-exit` | `symbol`, `side`, `date`, `setup`, `entry`, `entry_time`, `entry_bar`, `interval`, `opening_range`, `stop`, `stop_rule` (`or_mid`, `or_low`, `price`, `pct`, `atr`), `stop_label`, `stop_detail`, `target`, `target_r`, `target_label`, `risk_per_share`, `reward_risk`, `flat_by` (`"15:55"`), and with an atr stop also `atr`, `atr_bars` |
| `trader/timemachine.json` | `trader-timemachine` | `plan`, `mc` (`p_target`, `p_stop`, `p_neither`, `p_green`, `median_close`, `p5_close`, `p95_close`, `avg_R`, `bands`, `sample_paths`, `hist`, `window`, `block`), `history` (`triggered`, `target`, `stop`, `close`, `trades[]`, `entry_window` `"09:45 to 15:30"`, `min_volume_x`), `verdict`, `verdict_notes`, `warnings` |
| `trader/orders.json` | `trader-budget` or `trader-broker` | `status` (`"proposed"`), `paper`, `source`, `orders[]`: the Alpaca fields plus our `why` and `plan {entry, stop, target, risk_per_share, max_loss, setup, entry_time, flat_by}` |
| `trader/prices.json` | (alerts) | `as_of`, `source`, `prices {SYM: price}`, `detail {SYM: {prev_close, time, source, fetched}}` |
| `trader/portfolio.json` | (broker) | `cash`, `positions[] {ticker, shares, lots[] {id, date, shares, cost}}` |
| `trader/alerts.json` | (alerts) | `rules[] {id, type, symbol, price, pct, direction, at, label, before_close, date, rearm, note}` |
| `trader/state/risk.json` | (risk) | `date`, `now_new_york`, `now_utc`, `clock` (`real` or `pinned`), `paper`, `results[] {index, symbol, side, kind, fingerprint, verdict, reasons, warnings, info, max_loss, ...}`, `pass`, `block`, `checked_at`, `orders_file`. After a check that stopped with an error: `{error, clock, checked_at, results: []}` |
| `trader/state/broker.json` | (broker) | `orders[]`: records with `at`, `date`, `index`, `fingerprint`, `symbol`, `side`, `qty`, `kind`, `max_loss`, `stop`, `plain`, `status` (`approved`, `declined`, `placed`, `denied`, `failed`, `filled`), `reply`, `auto_yes` (true on a yes from `approve --auto-yes`, which has no `reply`, and on its `placed` record), `broker_order_id`, `practice` (true on every record a practice run writes, fills and noes included, and on `fill --practice`; a real run ignores those), `account` (the account checked that day), `filled_price` (a placed entry recorded with its fill). `accounts[]`: the daily Alpaca checks, `{at, date, account_number, paper}` |
| `trader/state/news.json`, `alerts.json`, `pelosi.json` | (their tool) | news: `seen {fingerprint: when}` (only what was shown), `ciks`, `last_check`. alerts: `rung {id: when}`, `last_check`. pelosi: `seen [DocID]` (only what was shown), `last_check` (`"2026-10-13 09:00 New York"`) |
| `trader/practice/...` | (risk, broker) | a practice run's own `portfolio.json`, `state/risk.json` and `state/broker.json`, in the same shapes |

- **`created_by`**, where a file has one (the five hand-off files at the top of the table), is the writing skill's folder name. The files marked (alerts), (broker) and so on don't carry it.
- **`side`** is `"buy"` or `"sell"`.
- **Ticker keys** are `symbol` in orders, plans and watchlists, but `ticker` in the portfolio. Every reader of the portfolio accepts either.
- **Config keys** are snake_case with a unit suffix:
  - `_usd`, `_pct` and `_per_day` for amounts;
  - `allow_*`, `require_*`, `*_guard`, `paper` and `fractional` for switches (true/false);
  - `no_entries_before`, `no_entries_after` and `flat_by` for times;
  - `blocked` for a list of tickers;
  - `broker`, `sec_user_agent` and `alpaca_account` (the pinned Alpaca account number) for text.
- **Config values are typed.** Switches are JSON `true`/`false`, limits are plain numbers, `null` switches a limit off and `0` means zero. A value of the wrong type is an error, never a default.
- **Alpaca order fields.** Never rename these; numbers go in them as strings: `symbol`, `side`, `qty`, `notional`, `type`, `time_in_force`, `limit_price`, `stop_price`, `trail_price`, `trail_percent`, `extended_hours`, `client_order_id`, `order_class`, `take_profit_limit_price`, `stop_loss_stop_price`, `stop_loss_limit_price`, `advanced_instructions`.

## 11. Layout, comments and docstrings

- **The module docstring** is also the tool's help text, and it states the rules in plain words. When a SKILL.md and the code disagree, read the docstring and the code.
- **Section banners** split each script into parts, and they are what section 12 is grouped by:
  ```python
  # ---------------------------------------------------------------- time
  ```
- **Function docstrings** are one line, often in the form "input -> output": `"""ISO time -> naive UTC. A time without an offset is taken as New York time."""`
- **Comments** are short and sit at the end of the line they explain: `SLEEP = time.sleep  # tests swap this out`.
- **Python 3.8 only.** No `match`, no `list[...]` style type hints, no `zoneinfo`, no `str.removeprefix`; a test checks this.
- **Lines** are at most 120 characters. A continuation line sits one column after the bracket it is inside (`foo(a,` then `b)` under `a`); when the bracket ends its line, the next line is indented four more.

## 12. Index of every module

Each script's names are listed in file order, grouped under its section banners.

The time helpers are the same in every script that has them: `_nth_sunday`, `eastern_offset`, `to_eastern`, `eastern_to_utc` and `parse_now`. They are listed once per script and not explained again.

Every script also has `home(*parts)`, right after its error class: a path in the trader folder, `$TRADER_HOME` when it is set, else `trader/` (written with `/`). It is the same function in all eleven.

### `trader-scanner/scan.py`

- **Error:** `ScanError`. **Test seam:** `SLEEP`.
- **Constants:**
  - network: `USER_AGENT`, `YAHOO_HOSTS`, `NASDAQ_FILES`, `NASDAQ_URL`, `SPARK_MAX` (20 symbols a spark request), `PAUSE`, `BACKOFF`;
  - scan limits: `BAR_MINUTES` (5), `MAX_CONFIRM` (200), `MIN_DAILY_BARS` (30), `NOTE`;
  - calendar: `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS` (2026 to 2028), `OPEN`, `CLOSE`, `EARLY`;
  - setup labels: `SETUPS` (key, label) and `SETUP_LABEL` (the same as a dict);
  - universe regexes: `TICKER`, `ALWAYS_OUT`, `COMMON`, `SOFT_OUT`.
- **time:** the time helpers, `ts_to_eastern`, `parse_hhmm`, `trading_day`, `session`, `clock_label` (`"10:15 AM"`), `day_label` (`"Mon 2026-10-19"`), `calendar_note` (the warning past `CALENDAR_YEARS`, or `None`).
- **network:** `http_get`, `yahoo_get`, `to_yahoo` (`BRK.B` to `BRK-B`), `spark_path`, `chart_path`.
- **parsing:**
  - `series_from_result`: a Yahoo result to `{t, c, o, h, l, v, meta}`;
  - `parse_spark`, `parse_chart`, `_read_maybe_gz`, `file_stem`;
  - `absorb_doc`: folds one offline JSON doc into data and errors;
  - `load_offline`.
- **universe:** `classify` (one row to `"keep"` or the reason it is dropped), `parse_symdir`, `build_universe`.
- **daily setups:** `rsi`, `daily_closes`, `find_setups` (no setups from a zero close), `scan_daily`.
- **opening-range breakout:**
  - `slot_time`: the datetime of a slot;
  - `session_bars`;
  - `usable`: every slot is present;
  - `orb_candidate`: stage 1, closes only;
  - `orb_hit`: stage 2, highs and lows;
  - `orb_window`: the day and the cutoff, or a plain refusal;
  - `scan_orb`.
- **fetching:** `progress`, `fetch_spark`, `read_symbols`, `symbols_from_file`, `read_config` (one JSON object), `write_json` (an atomic write), `dollars`.
- **commands:** `cmd_universe`, `cmd_daily`, `number_text`, `cmd_orb`, `price_text`, `main`.

### `trader-news/news.py`

- **Error:** `NewsError`. **Class:** `Fetcher` (`get(url, name, sec=False)`; offline, it reads `name` from the folder).
- **Constants:** the URL templates `YAHOO_RSS_URL`, `CHART_URL`, `SEC_FEED_URL`, `TICKERS_URL`; `PLACEHOLDER`, `BROWSER_USER_AGENT`, `KEEP_DAYS` (30), `MOVE_PCT` (3.0), `MAX_BYTES` (16 MB), `SYMBOL` and `FORM` (what a ticker and an SEC form name may look like), `CONTROL` (the characters `clean` takes out), `ITEMS_8K`, `IMPORTANT_ITEMS`, `KEYWORDS` (tag, regex).
- **time:** `_nth_sunday`, `eastern_offset`, `to_eastern` (this version also accepts aware datetimes), `eastern_to_utc`, `parse_now` (`--now` to aware UTC; no offset means New York time).
- **inputs:** `load_json(path, errors)` (one JSON object, or `None` with a note), `object_rows`, `clean_symbol`, `watch_from_files` (`{ticker: name}`), `sec_user_agent`.
- **fetching:**
  - `Fetcher`;
  - `body_of`: the answer's bytes, gunzipped, capped at `MAX_BYTES` either way;
  - `clean`: fetched text without control codes or direction overrides;
  - `no_entities`: refuses XML that declares entities;
  - `yahoo_symbol` (for a URL: encoded), `cik_map`.
- **parsing:**
  - `local_tag`: an XML tag without its namespace;
  - `child_text`, `parse_time` (feed times), `parse_rss`, `parse_sec_atom`, `parse_chart(data, ticker, now=None)` (with `now`: the last price at or before it), `collect_moves`, `mention_patterns`, `tag`, `dedupe`;
  - `item_fingerprint`: sha1 of an item id.
- **the check:** `collect`, `select`, `remember` (marks what was shown as seen), `render_moves`, `render`, `json_ready`, `main`, `run_check` (the body of `main`), `write_state` (an atomic write).

### `trader-entry/entry.py`

- **Error:** `EntryError`. **Class:** `SimClock` (`now()`, `sleep(seconds)`). **Test seams:** `_open_url`, `_sleep`.
- **Constants:**
  - network: `USER_AGENT`, `PAUSE`, `BACKOFF`, `HOSTS`;
  - bar timing: `GRACE` (2 s after a bar closes), `SETTLE` (60 s for Yahoo to catch up), `INTERVALS` (`{"1m": 60, "5m": 300}`);
  - config and time: `DEFAULTS` (the entry window), `EPOCH`;
  - calendar: `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS`, `OPEN`, `CLOSE`, `EARLY`;
  - output labels: `STATUS_LABEL`, `PHASE_LABEL`, `PHASE_SHORT`.
- **time:**
  - the time helpers;
  - `utc_now`, `utc_of` (unix to naive UTC), `ts_of` (naive UTC to unix), `utc_iso` (to `"...Z"`);
  - `parse_hhmm`, then the clock formats `hhmm_text` (`"09:45"`), `clock_text` (`"9:45"`) and `ampm_text` (`"9:45 AM"`);
  - `trading_day`, `session`, `next_open`.
- **Yahoo:**
  - `yahoo_symbol`, `chart_path`;
  - `chart_range`: the smallest Yahoo range that still holds the day;
  - `_open_url`, `_sleep`, `yahoo_get`, `parse_chart`, `read_text`, `offline_file`, `get_chart`.
- **the rule:**
  - `read_config`, `round_price` (rounds to cents), `early_cutoff`;
  - `evaluate`: the status at one moment, a pure function;
  - `public`: drops the `_` keys;
  - `entry_doc`: the `entry.json` shape;
  - `write_json`.
- **words:** `setup_name`, `describe`, `bar_line`.
- **watch:** `SimClock`, `watch_loop`.
- **commands:** `evaluate_options`, `load_chart`, `cmd_check`, `cmd_watch`, `cmd_watchlist`, `main`.

### `trader-exit/exit.py`

- **Error:** `PlanError`. **Test seams:** `_open_url`, `_sleep`.
- **Constants:**
  - network: `USER_AGENT`, `BACKOFF`, `HOSTS`, `INTERVALS`;
  - rules: `DEFAULTS` (`flat_by`, `min_reward_risk`), `ATR_BARS` (14), `MIN_RISK_PCT` (0.05 %), `CENT` (`Decimal("0.01")`), `EPOCH`, `NOTE`;
  - calendar: `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS`, `OPEN`, `CLOSE`, `EARLY`;
  - words: `NUMBER_WORDS` (1 to `"one"`).
- **time:** the time helpers, `utc_now`, `utc_of`, `parse_hhmm`, `parse_clock` (`"10:15 AM"` to a `time`), `hhmm_text`, `ampm_text`, `session`.
- **money math (Decimal):**
  - `to_decimal`: anything to a Decimal;
  - `round_to_cent` (rounds half up), `round_down_to_cent` (rounds down), `dollars`, `number_text`, `ratio_text`;
  - `parse_stop`, `parse_target`, `r_multiple_label`, `compute`.
- **bars for ATR:** `_open_url`, `_sleep`, `yahoo_get`, `read_text`, `load_chart`, `session_bars`, `atr_of`.
- **plan:**
  - `read_json` (one JSON object), `write_json` (an atomic write), `read_config`, `resolve_flat_by`;
  - `gather`: the entry facts, from `entry.json` or the flags;
  - `build_plan`, `describe`, `cmd_plan`, `cmd_show`, `main`.

### `trader-timemachine/timemachine.py`

- **Error:** `TMError`. **Test seams:** `fetch_text`, `_sleep`.
- **Constants:**
  - network: `USER_AGENT`, `PAUSE`, `BACKOFF`, `HOSTS`;
  - the session: `INTERVAL` (300 s), `SESSION_BARS` (78), `OPEN_MIN`, `CLOSE_MIN` and `EARLY_MIN` (minutes after midnight), `MIN_SESSIONS` (20);
  - the replays: `SAMPLE_PATHS` (160), `BAND_PATHS` (2,000), `BAND_QUANTILES`, `HIST_BINS` (24);
  - the history's entry window: `ENTRY_WINDOW` (`("09:45", "15:30")`, trader-entry's defaults);
  - verdict thresholds: `COIN_FLIP` (0.05), `STRONG` (0.15), `MANY_NEITHER` (0.40);
  - other: `EPOCH`, `WEEKDAYS`, `NYSE_EARLY_CLOSE`.
- **time:**
  - the time helpers, `utc_of`;
  - `parse_clock`: to minutes after midnight, where a bare 1 to 8 o'clock means PM;
  - `clock12` (`"10:15 AM"`), `clock24` (`"10:15"`).
- **data:** `chart_url`, `_sleep`, `fetch_text`, `fetch_chart`, `read_offline`, `chart_result`, `parse_bars`, `split_sessions` (`{date: {slot: bar}}`), `full_sessions`.
- **the simulation:** `build_pool`, `percentile` (a percentile), `simulate` (with a nested `draw`: `block` 1 draws each bar on its own, the reel's way; `block` N draws runs of N bars from one past session).
- **stop rules and the history replay:** `cents_away`, `opening_range`, `rule_stop`, `history_replay` (only inside `entry_window`, and with `min_volume_x` only on that volume).
- **the plan:** `positive_price` (a positive price, or a TMError), `load_plan`, `normalize_plan`, `resolve_rule`.
- **the run:** `until_text`, `lean`, `verdict`, `verdict_notes`, `time_machine` (builds the whole result document).
- **output:** `percent_text` (`"29.4 %"`), `report`, `render_chart`.
- **commands:** `write_json` (an atomic write), `entry_window` (the config's window, read as trader-entry reads it), `compute`, `cmd_run`, `cmd_chart`, `main`.

### `trader-budget/size.py`

- **Error:** `SizeError`.
- **Constants:** `DEFAULTS` (every config key with its default), `BOOL_KEYS` and `NUMBER_KEYS` (what `rules` checks), `EPSILON`, `FRESH_MINUTES` (15), `SETUP_WORDS`.
- **time:** the time helpers, `parse_utc`, `utc_iso`.
- **inputs:** `read_json`, `load_config` (a path to the checked config), `rules`, `finite_number`, `parse_weights`, `weights_from_arg`, `load_prices` (a path to `{SYM: price}`), `price_time`, `fresh_price`.
- **money helpers:**
  - `round_to_cent`;
  - `shares_for`: whole shares, worked out in integer cents;
  - `alpaca_price` (`"358.32"`), `alpaca_number` (a number as an Alpaca string), `dollars`.
- **plan mode:** `size_trade` (a pure function returning the sizing dict), `limit_cap` (the highest limit price the max loss and min reward:risk allow, for `--limit cap`), `bracket_order`, `plan_report`.
- **weights mode:** `normalise`, `allocate`, `round_to_unit`, `whole_shares`, `size`, `orders_from`, `report`.
- **orders file:** `write_orders` (an atomic write).
- **main:** `run_plan`, `run_weights`, `main`.

### `trader-risk/check.py`

- **Error:** `RiskError`.
- **Constants:**
  - config: `DEFAULTS`; for `rules`, `BOOL_KEYS`, `LIMIT_KEYS` (the ones `null` switches off), `NUMBER_KEYS` and `WHOLE_KEYS` (counts);
  - order fields: `ALPACA_FIELDS`, `PRICE_FIELDS`, `ORDER_TYPES`, `TIME_IN_FORCE_VALUES`;
  - regexes: `OCC_OPTION_SYMBOL` (option symbols), `TICKER`, `HHMM`;
  - tolerances: `HALF_CENT` (a 0.005 tolerance), `FRESH_MINUTES`;
  - words: `ENTRY_REF_WORDS`;
  - calendar: `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS`, `OPEN`, `CLOSE`, `EARLY`.
- **time:**
  - the time helpers, `utc_now` (the real clock, a test seam), `utc_iso` (`"...Z"`), `parse_utc`, `trading_day`;
  - `parse_window_time`: `"09:45"` to a `time`, where `null` means that bound is off;
  - `hours_earlier`, `window_check`, `business_days`.
- **files and helpers:**
  - `home`, `read_json` (`kind=dict` refuses anything but one object), `real_only` (the broker log without practice records), `fingerprint`, `finite_number` (finite numbers only), `dollars`, `round_to_cent`, `price_text`;
  - `starting_account`: the starting cash and holdings;
  - `record_day`, `is_entry_record`, `placed_today`;
  - `entry_loss`: an entry's max loss, raised by a worse fill logged today;
  - `day_trades`.
- **your rules:** `rules` (the config, checked), `switched_off` (warnings for what is off, and what risky switches are on).
- **the check:**
  - `check`: a pure function, with a nested `fresh(sym)`;
  - `percent_text`, `number_text`, `report`;
  - `load_prices`: a doc to `(prices, times)`;
  - `main`, `write_verdicts` (an atomic write of `risk.json`).

### `trader-broker/ticket.py`

- **Error:** `TicketError`.
- **Constants:**
  - order fields and words: `ALPACA_FIELDS`, `STRING_FIELDS`, `TIF_WORDS`;
  - the yes: `YES_WORDS`, `BATCH_WORDS`, `DOUBT_WORDS`, `TICKET_WORDS` (the only other words a yes may carry), `SIDE_WORDS`, `REPLY_TOKEN` (splits a reply into words and numbers), `ALLOWED_PUNCTUATION` (the only other characters allowed);
  - the gates: `VERDICT_MINUTES` (15), `RETRY_STATUSES` (`failed`, `denied`, `declined`: a newer risk check re-arms these), `BROKERS`, `ACCOUNT_NUMBER` (what an account number may look like);
  - `PRACTICE`: the practice-run banner (in the commands part).
- **time:** the time helpers, `utc_now` (the real clock, a test seam), `parse_utc`, `ny_iso` (`"2026-10-19T10:15:00-04:00"`).
- **files:** `home`, `read_json` (`kind=dict` refuses anything but one object), `write_json` (an atomic write), `record_day`, `rules` (the config keys this tool reads, checked).
- **the order:**
  - `fingerprint`, `number_string`, `finite_number`;
  - `tool_input`: the exact `place_stock_order` arguments;
  - `dollars`, `price_text`, `is_bracket`;
  - `max_loss(order, verdict=None)`: trader-risk's figure when there is a verdict, else the order's own;
  - `plain_parts`, `plain`, `amount_numbers` (each also takes `verdict`), `parse_yes(reply, order, batch_symbols, verdict)`.
- **state:**
  - `load_state` (refuses a log that is not one), `latest(state, order_fingerprint, today, practice)` (only decisions of the same kind, practice or real);
  - `checked_after`: the risk check ran after this decision;
  - `risk_verdict`: the verdict row, or why not (no check, an error, another day, the wrong kind of clock, too old, not this order);
  - `account_check`: today's account check record; `account_gap`: why the account is not confirmed, or `None`;
  - `status_of`: READY, NOT READY, BLOCKED, APPROVED or DONE (a failed, denied or declined one is READY again after a newer check);
  - `order_kind`.
- **rendering:** `render`, `ticket_json` (both take `now` and `pinned`).
- **fills -> portfolio:** `apply_fill` (finds the position by `ticker` or `symbol`), `log_fill` (marks a practice fill).
- **commands:** `run_clock` (now, today, pinned), `load_orders`, `pick_order`, `PRACTICE`, `cmd_show`, `auto_yes_gap` (why `--auto-yes` is refused: not a practice run, or `"paper": false`), `cmd_approve`, `cmd_account`, `cmd_decline`, `cmd_result`, `cmd_fill`, `cmd_flatten`, `cmd_log`, `main`.

### `trader-alerts/alerts.py`

- **Error:** `AlertError`. **Test seams:** `_urlopen`, `_sleep`.
- **Constants:**
  - network: `USER_AGENT`, `PAUSE`, `BACKOFF`, `YAHOO_HOSTS`, `HHMM`;
  - calendar: `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSE`, `CALENDAR_YEARS`, `OPEN`, `CLOSE`, `EARLY`;
  - rule types: `PRICE_TYPES` (every rule type), `NO_QUOTE` (the types that need no quote).
- **time:** the time helpers, `trading_day`, `session`, `market_status`.
- **quotes:**
  - `stooq_url`, `yahoo_url`, `parse_stooq`, `parse_yahoo`;
  - `http_get`: returns (status, body);
  - `fetch`, `fetch_yahoo`, `offline_text`, `get_quotes`, `write_prices`;
  - `write_json` (an atomic write), `read_json`, `home`.
- **rules:**
  - `parse_hhmm`, `is_number` (a finite number, not true/false), `validate`;
  - `usable`: the rules that check out, and a message for each one skipped;
  - `active`: the rule's date is today;
  - `rang_today`, `ring_time`;
  - `evaluate(rules, quotes, status, state, now_utc, problems=None)`: a pure function returning the rings and the new state; a bad rule is skipped, its message goes into `problems`;
  - `number_text`, `rules_from_plan`.
- **commands:** `holdings`, `dedupe`, `cmd_clock`, `cmd_quote`, `cmd_check` (a broken state starts fresh, with a note), `read_rules_doc`, `read_alert_state` (the two files, checked), `cmd_add`, `cmd_list`, `cmd_reset`, `main`.

### `trader-analyst/facts.py`

- **Error:** `FactsError`. **Class:** `Fetcher` (`json(url, name)`).
- **Constants:** `TICKERS_URL`, `FACTS_URL`, `SUBMISSIONS_URL`, `ARCHIVE_URL`, `PLACEHOLDER`, `MAX_BYTES` (100 MB), `CIK_DIGITS` (digits only, for `--cik`), `CONTROL`, `USER_AGENT_HELP`, `CONCEPTS` (each metric's XBRL concept names, tried in order), `FLOWS`, `EXHIBIT_RANK`.
- **top of file:** `home`, the time helpers (`_nth_sunday`, `eastern_offset`, `to_eastern`) and `ny_now` (New York now, for `as_of`), `user_agent`, `Fetcher`, `body_of` (capped, gunzipped), `clean`, `doc_url` (a file in a filing's folder, its name encoded).
- **facts:**
  - `parse_date`, `facts_for`, `periods`;
  - `quarterly`: three-month values, plus a derived Q4;
  - `series`, `year_ago_value`, `percent_change` (a percent change), `margin`, `analyse`, `stale_note`, `changes`.
- **filings:** `exhibit_name`, `latest_filings`.
- **output:** `millions_text` (millions), `signed_percent_text` (a signed percent), `percent_text` (a percent), `render`, `metrics_row`, `clean_title`, `ticker_ciks`, `read_metrics` (an earlier `--metrics` file, or a FactsError: never written over), `write_json` (an atomic write), `main`.

### `trader-pelosi/ptr.py`

- **Error:** `PtrError`.
- **Constants:**
  - URLs and the agent: `INDEX_URL`, `PTR_URL`, `FD_URL`, `USER_AGENT`;
  - lookups: `FILING_TYPES`, `TXN_TYPES`, `OWNERS`, `LATE_DAYS` (45);
  - limits: `MAX_BYTES` (64 MB), `DOC_ID` (digits only), `CONTROL` (keeps tabs);
  - regexes: `TRANSACTION_LINE`, `LABELS`, `PAGE_HEADER`, `END_OF_TABLE`, `AMOUNT_NUMBER`, `OPTION`, `SHARES`.
- **time:** `_nth_sunday`, `eastern_offset`, `to_eastern`, `ny_now` (New York now: today's date and `last_check`), `parse_date_flag` (`--today` / `--since`, or a PtrError).
- **the index:** `iso_date` (`"8/21/2026"` to `"2026-08-21"`), `clean`, `download` (capped), `read_index_bytes` (checks the XML's size before it unzips), `parse_index` (refuses entity declarations), `load_index`, `member_match`, `select`.
- **state:** `load_state(path, notes)` (a broken file starts fresh, with a note), `save_state` (an atomic write), `new_since` (unseen and filed in the last 45 days, on every run).
- **parsing a PTR:** `amount_range`, `finish_row` (cleans one row), `parse_ptr_text`, `pdf_to_text`.
- **output:** `watchlist` (the tickers to flag: `--tickers` plus positions by `ticker` or `symbol`), `describe`, `render_report`, `render_index`.
- **CLI:** `build_parser`, `fetch_and_read` (refuses a DocID that is not digits), `main`, `run`.

## 13. Rename checklist

1. **Find every use**, in code, tests and docs. In Git Bash, from the repo folder:
   ```bash
   grep -rnw --include='*.py' --include='*.md' --include='*.json' OLD_NAME .
   ```
   In PowerShell:
   ```powershell
   Get-ChildItem -Recurse -Include *.py,*.md,*.json | Select-String -Pattern "\bOLD_NAME\b"
   ```
2. **Rename it everywhere it appears:** the script, every duplicate copy in other scripts, the tests, the SKILL.md files, README.md, howtouse.md, CLAUDE.md and this file.
3. **A JSON key:** update every reader listed in section 10, and decide what happens to files already in `trader\`.
4. **A CLI flag:** update the SKILL.md examples, the README and howtouse.md.
5. **Run the tests:** `python -B -m unittest discover -s tests`. Every test should pass.
6. **Keep lines at 120 characters or fewer**, and re-align continuation lines under their bracket if the rename moved it.
7. **If you installed the skills by copying the folders**, copy them again (howtouse.md, step 1.2).
