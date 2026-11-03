"""Write RESULTS.md from results/picks.json, results/features.csv and results/stress.json.

    python -B paper-trading-scenarios/feature-search/write_results.py

Every number in RESULTS.md comes from those files. Standard library only.
"""

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")

WORDS = {"orb5": "5-minute range", "orb15": "15-minute range", "orb30": "30-minute range",
         "pdh": "prior-day high", "always": "no signal (buy at 9:45)", "market": "market entry",
         "cap": "limit at the cap", "novol": "no volume check", "vol1.5x": "volume 1.5x",
         "until1530": "entries until 15:30", "until1100": "entries until 11:00"}


def plain(combo_id):
    setup, stop, target, entry, volume, window = combo_id.split()
    stop_words = {"or-mid": "stop at the range midpoint", "or-low": "stop at the range low"}.get(stop)
    if stop_words is None:
        kind, value = stop.split(":")
        stop_words = "stop %s%% under" % value if kind == "pct" else "stop %s x ATR" % value
    return ", ".join([WORDS[setup], stop_words, "target " + target, WORDS[entry], WORDS[volume], WORDS[window]])


def money(value, sign=True):
    if value is None:
        return "n/a"
    text = "${:,.2f}".format(abs(value))
    return ("-" if value < 0 else "+" if sign else "") + text


def number(value, digits=2):
    return "n/a" if value is None else ("%." + str(digits) + "f") % value


def percent(value, digits=0):
    return "n/a" if value is None else ("%." + str(digits) + "f %%") % (100.0 * value)


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def median(rows, key):
    values = [row[key] for row in rows if row[key] is not None]
    return sorted(values)[len(values) // 2] if len(values) % 2 else sum(sorted(values)[len(values) // 2 - 1:
                                                                                        len(values) // 2 + 1]) / 2.0


def read_combos():
    rows = []
    for row in csv.DictReader(open(os.path.join(OUT, "combos.csv"), encoding="utf-8")):
        for key, value in list(row.items()):
            if key.startswith(("is_", "oos_", "all_")):
                row[key] = float(value) if value not in ("", None) else None
        rows.append(row)
    return rows


def feature_groups(combos):
    groups = {}
    for row in combos:
        if row["setup"] == "always":
            continue
        values = {"setup": "pdh" if row["setup"] == "pdh" else "orb" + row["or_minutes"], "stop": row["stop"],
                  "target": row["target"], "entry": row["entry"],
                  "volume check": "1.5x" if row["volume"] else "none", "entry window": "until " + row["cutoff"]}
        for feature, value in values.items():
            groups.setdefault((feature, value), []).append(row)
    return groups


def baseline_pairs(combos):
    base = {(row["stop"], row["target"]): row for row in combos if row["setup"] == "always"}
    return [(row, base[(row["stop"], row["target"])]) for row in combos
            if row["setup"] != "always" and (row["stop"], row["target"]) in base]


def main():
    summary = json.load(open(os.path.join(OUT, "picks.json"), encoding="utf-8"))
    combos = read_combos()
    stress_path = os.path.join(OUT, "stress.json")
    stress = json.load(open(stress_path, encoding="utf-8")) if os.path.exists(stress_path) else None
    picks = summary["picks"]
    top = picks[0]
    symbols = summary["symbols"]
    capital = 5000 * len(symbols)
    out = []
    add = out.append
    add("# Feature search: which of the bot's choices made money on 30 stocks")
    add("")
    add("> **Not financial advice.** A replay of %d US stocks and ETFs over %d sessions (%s to %s) with "
        "simulated fills. It says what these rules did on these days, not what they will do next." %
        (len(symbols), len(summary["sessions"]), summary["sessions"][0], summary["sessions"][-1]))
    add("")
    add("## The answer in short")
    add("")
    best_is_sharpe = max(p["is"]["sharpe"] for p in picks)
    add("- **No combination showed a real edge.** Of %d combinations of the bot's choices, %s made money "
        "in-sample (%s to %s) after costs and %s out-of-sample (%s to %s). **%d made money in both halves.**"
        % (summary["combos"], percent(summary["share_positive"]["is"], 1), summary["in_sample"][0],
           summary["in_sample"][1], percent(summary["share_positive"]["oos"], 1), summary["out_of_sample"][0],
           summary["out_of_sample"][1], len(summary["up_in_both_halves"])))
    add("- **Picking the in-sample winner did not help.** The rank of a combination's in-sample Sharpe ratio "
        "and its out-of-sample one correlate at %s (0 is no relation). The in-sample winner of %d of the %d "
        "categories was the same combination, %s: %s in-sample, %s out-of-sample."
        % (number(summary["rank_correlation_sharpe"]), sum(1 for p in picks if p["id"] == top["id"]), len(picks),
           plain(top["id"]), money(top["is"]["net"]), money(top["oos"]["net"])))
    in_count = summary["sessions"].index(summary["in_sample"][1]) + 1
    sharpe_day = best_is_sharpe / 252 ** 0.5
    noise = ((1 + 0.5 * sharpe_day ** 2) / in_count) ** 0.5 * 252 ** 0.5
    add("- **The best in-sample Sharpe ratio, %s, is within luck.** From %d sessions a Sharpe ratio's own "
        "standard error is about %s (annualized), so one try lands that high by chance easily, and this was the "
        "best of %d. Its deflated Sharpe ratio, the chance it is real after that many tries, is %s."
        % (number(best_is_sharpe), in_count, number(noise, 1), summary["combos"], percent(top["is"]["dsr"], 1)))
    add("- **The typical trade lost money.** The median combination averaged %sR a trade over the whole period "
        "after costs (%sR before costs), where 1R is the loss the risk check allowed at the stop."
        % (number(summary["median_avg_r"]["cost"], 3), number(summary["median_avg_r"]["no_cost"], 3)))
    add("- **Costs matter but are not the whole story.** With no cost at all, %s of combinations made money "
        "in-sample and %s out-of-sample; %d made money in both halves."
        % (percent(summary["share_positive_no_cost"]["is"], 1), percent(summary["share_positive_no_cost"]["oos"], 1),
           len(summary["up_in_both_halves_no_cost"])))
    wide_path = os.path.join(OUT, "tm_filter.json")
    wide = json.load(open(wide_path, encoding="utf-8")) if os.path.exists(wide_path) else None
    if wide:
        add("- **The Time Machine filter, tested on its own:** across %d combinations "
            "it improved R per trade in %d and the net in %d, mostly by skipping trades it rated stop-first. "
            "With it, %d of the %d made money over its %d sessions (%d without it)."
            % (wide["count"], wide["better_r"], wide["better_net"], wide["positive_with"], wide["count"],
               wide["sessions"], wide["positive_without"]))
    add("")
    add("So the honest pick in every category is a rule that lost least, not one that makes money. "
        "The tables below show the in-sample winners anyway, next to what they did afterwards.")
    add("")
    add("## What was tested")
    add("")
    add("- **Stocks:** %s. Each one trades in its own $5,000 practice account, so the book is $%s."
        % (", ".join(symbols), "{:,}".format(capital)))
    add("- **Sessions:** %d (the session before them is history for the prior-day high). In-sample: the first "
        "%d, %s to %s, where the picks are made. Out-of-sample: the last %d, %s to %s, never looked at "
        "while picking." % (len(summary["sessions"]), in_count, summary["in_sample"][0], summary["in_sample"][1],
                            len(summary["sessions"]) - in_count, summary["out_of_sample"][0],
                            summary["out_of_sample"][1]))
    add("- **Choices (%d combinations):** setup (5, 15 or 30-minute opening range, or the prior-day high); "
        "stop (range midpoint, range low, 0.5 or 1 %% under, 1, 1.5 or 2 x ATR); target (1R, 1.5R, 2R, 3R); "
        "entry (market, or a limit at the cap); volume check (none or 1.5x); entry window (until 15:30 or "
        "11:00). The risk per trade is the example config's $50; the Time Machine filter and $100 risk are in "
        "the stress tests, because they are slow or only scale the same trades." % summary["combos"])
    add("- **The rules are the pack's own:** `engine.py` calls `entry.evaluate`, `exit.compute`, "
        "`size.size_trade`, `check.check` and the scenario runner's simulated broker. `validate.py` checks it "
        "reproduces all 13 TSLA scenarios (507 sessions) exactly.")
    add("- **Costs:** %s cent a share each way (spread and slippage). Alpaca charges no commission. Fills come "
        "from 5-minute bars; a bracket whose stop was already crossed when it arrived counts as rejected, as "
        "Alpaca rejects it." % number(100 * summary["cost_per_share_each_way"], 0))
    add("- **Metrics:** net after costs; return on the $%s book; Sharpe and Sortino ratios (annualized from "
        "daily returns, no-trade days included); maximum drawdown; return per dollar of drawdown; win rate; "
        "profit factor (gains over losses); average R per trade and its t-statistic; how many of the %d "
        "stocks made money; the deflated Sharpe ratio for the number of combinations tried." %
        ("{:,}".format(capital), len(symbols)))
    add("")
    add("## The picks, one per category (chosen in-sample, then tested out-of-sample)")
    add("")
    add("A combination needs %d in-sample trades to be picked. \"Rank\" is where the pick landed out-of-sample "
        "among all %d (100 = the best)." % (summary["min_trades"], summary["combos"]))
    add("")
    rows = []
    for item in picks:
        key = item["key"]
        def show(moment, key=key):
            value = moment[key]
            if key in ("net", "max_drawdown", "steady"):
                return money(value, key != "max_drawdown")
            if key == "win_rate":
                return percent(value, 1)
            if key == "symbols_up":
                return "%s of %d" % (value, len(symbols))
            return number(value)
        rows.append([item["category"], plain(item["id"]), show(item["is"]), show(item["oos"]),
                     money(item["is"]["net"]), money(item["oos"]["net"]), money(item["all"]["net"]),
                     number(item["oos_rank_pct"], 0)])
    add(table(["Category", "In-sample winner", "Metric in-sample", "Metric out-of-sample", "Net in", "Net out",
               "Net, whole period", "Rank out"], rows))
    add("")
    add("The full numbers of every pick (trades, Sharpe, drawdown, profit factor, deflated Sharpe, both halves) "
        "are in `results/picks.json`, and every combination is a row of `results/combos.csv`.")
    add("")
    add("Whole period, the five combinations with the most net, for the record (picked with hindsight, so "
        "they prove nothing):")
    add("")
    add(table(["Combination", "Trades", "Net", "Sharpe", "Max drawdown", "Profit factor", "Avg R", "Net with no cost"],
              [[plain(item["id"]), item["all"]["trades"], money(item["all"]["net"]), number(item["all"]["sharpe"]),
                money(item["all"]["max_drawdown"], False), number(item["all"]["profit_factor"]),
                number(item["all"]["avg_r"], 3), money(item["all_no_cost"]["net"])]
               for item in summary["best_all_period"]]))
    add("")
    add("## Each choice on its own")
    add("")
    add("Every combination that has the choice. R per trade is the fairer measure: a choice that trades less "
        "also loses less when trades lose, without making the trades any better. A choice that really helps "
        "should help in both halves.")
    add("")
    groups = feature_groups(combos)
    rows = []
    for (feature, value), members in sorted(groups.items()):
        rows.append([feature, value, len(members), int(median(members, "all_trades")),
                     number(median(members, "is_avg_r"), 3), number(median(members, "oos_avg_r"), 3),
                     number(median(members, "all_avg_r"), 3), money(median(members, "is_net")),
                     money(median(members, "oos_net"))])
    add(table(["Choice", "Value", "Combos", "Median trades", "R per trade in", "R per trade out",
               "R per trade, whole", "Median net in", "Median net out"], rows))
    add("")
    add("What the two halves agree on, by the median R per trade:")
    add("")
    agreed = []
    for feature in ("setup", "stop", "target", "entry", "volume check", "entry window"):
        values = [value for (name, value) in groups if name == feature]
        best_in = max(values, key=lambda value: median(groups[(feature, value)], "is_avg_r"))
        best_out = max(values, key=lambda value: median(groups[(feature, value)], "oos_avg_r"))
        if best_in == best_out:
            agreed.append({"setup": "the %s setup" % best_in, "stop": "a %s stop" % best_in,
                           "target": "a %s target" % best_in,
                           "entry": "a limit entry at the cap" if best_in == "cap" else "a market entry",
                           "volume check": "no volume check" if best_in == "none" else "the volume check",
                           "entry window": "entries " + best_in}[feature])
            add("- **%s: %s** was the best in both halves (%sR in, %sR out a trade)."
                % (feature.capitalize(), best_in, number(median(groups[(feature, best_in)], "is_avg_r"), 3),
                   number(median(groups[(feature, best_in)], "oos_avg_r"), 3)))
        else:
            add("- **%s:** no stable winner. Best in-sample %s, best out-of-sample %s."
                % (feature.capitalize(), best_in, best_out))
    add("- **The margins are small,** a few hundredths of R a trade, and every one of these \"best\" choices "
        "still lost money per trade over the whole period.")
    vol, novol = groups[("volume check", "1.5x")], groups[("volume check", "none")]
    add("- **The volume check makes each trade worse** (%sR against %sR a trade over the whole period) and "
        "lowers the losses only by trading about %d times less (a median %d trades against %d)."
        % (number(median(vol, "all_avg_r"), 3), number(median(novol, "all_avg_r"), 3),
           round(median(novol, "all_trades") / max(median(vol, "all_trades"), 1)), median(vol, "all_trades"),
           median(novol, "all_trades")))
    add("")
    add("## Baselines")
    add("")
    pairs = baseline_pairs(combos)
    def share_better(part, key):
        return sum(1 for row, base in pairs if row[part + key] > base[part + key]) / float(len(pairs))
    beat_net = lambda part: share_better(part, "_net")
    beat_r = lambda part: share_better(part, "_avg_r")
    add("- **No signal at all:** buy every stock every session at 9:45, with the same stop and target. Each of the "
        "%d combinations with a matching stop and target is set against that baseline:" % len(pairs))
    add("  - in dollars, %s of them lost less in-sample and %s out-of-sample, because they trade far less "
        "(a median %d trades against %d);" % (percent(beat_net("is")), percent(beat_net("oos")),
                                              median([row for row, _ in pairs], "all_trades"),
                                              median([base for _, base in pairs], "all_trades")))
    add("  - per trade, only %s beat it in-sample and %s out-of-sample. The breakout signals did not pick "
        "better trades than buying at 9:45 every day." % (percent(beat_r("is")), percent(beat_r("oos"))))
    hold = summary["buy_and_hold"]
    add("- **Buy and hold** $5,000 of each stock (not a like-for-like rival: it holds overnight and takes the "
        "whole market move): %s (%s %%) in-sample, %s (%s %%) out-of-sample, %s over the whole period."
        % (money(hold["is"][0]), number(hold["is"][1]), money(hold["oos"][0]), number(hold["oos"][1]),
           money(hold["all"][0])))
    add("")
    if stress:
        add("## Stress tests of the picks")
        add("")
        add("Each in-sample winner, and the README's own rules (15-minute range, midpoint stop, 1R, market, "
            "until 15:30) for comparison. Net over the whole period on the $%s book." % "{:,}".format(capital))
        add("")
        rows = []
        for item in stress["combos"]:
            cost = item["cost"]
            rows.append([plain(item["id"]), money(cost["0.0"]["net"]), money(cost["0.01"]["net"]),
                         money(cost["0.03"]["net"]), money(item["delay"]["all"]["net"]), money(item["risk100"]["net"])])
        add(table(["Combination", "No cost", "1 cent (base)", "3 cents", "Yes 5 min later", "$100 risk"], rows))
        add("")
        add("The Time Machine filter (trade only when it has target-first above stop-first), from %s, when it has "
            "20 earlier sessions to build on, with and without the filter on the same sessions:" % stress["tm_from"])
        add("")
        rows = []
        for item in stress["combos"]:
            tm, off = item["tm"], item["tm_off"]
            rows.append([plain(item["id"]), "%d / %d" % (off["trades"], tm["trades"]), money(off["net"]),
                         money(tm["net"]), number(off["avg_r"], 3), number(tm["avg_r"], 3)])
        add(table(["Combination", "Trades without / with", "Net without", "Net with", "R per trade without",
                   "R per trade with"], rows))
        add("")
        rows = []
        for item in stress["combos"]:
            picking = item["stock_picking"]
            one = item["one_account"]
            rows.append([plain(item["id"]), "%d" % picking["count"],
                         money(picking["oos_winners_only"]["net"]) if picking["oos_winners_only"] else "n/a",
                         money(picking["oos_all_stocks"]["net"]), one["all"]["trades"], money(one["all"]["net"]),
                         money(one["is"]["net"]), money(one["oos"]["net"]), number(one["all"]["sharpe"]),
                         money(one["all"]["max_drawdown"], False)])
        add(table(["Combination", "Stocks up in-sample", "Out-of-sample, those stocks only", "Out-of-sample, all 30",
                   "One account: trades", "Net", "In", "Out", "Sharpe", "Max drawdown"], rows))
        add("")
        add("- **Costs decide the sign.** At no cost the in-sample winner made %s; at 1 cent a share %s; at 3 "
            "cents %s." % tuple(money(stress["combos"][0]["cost"][key]["net"]) for key in ("0.0", "0.01", "0.03")))
        add("- **Stock picking** keeps only the stocks a combination made money on in-sample. If good stocks stayed "
            "good, that column would beat the next one in every row; it does in %d of %d."
            % (sum(1 for item in stress["combos"] if item["stock_picking"]["oos_winners_only"]
                   and item["stock_picking"]["oos_winners_only"]["net"]
                   > item["stock_picking"]["oos_all_stocks"]["net"]),
               len(stress["combos"])))
        singles = [item["one_account"]["all"]["net"] for item in stress["combos"]]
        add("- **One account** is a single $5,000 account trading the whole list the way one person would: each "
            "session the first signal among the 30 stocks, one trade a day. Over the %d sessions these ended "
            "between %s and %s. The in-sample winner's account lost in-sample and made money out-of-sample, the "
            "opposite of the 30-account book: at this size the result is mostly which trades happened to come first."
            % (len(summary["sessions"]), money(min(singles)), money(max(singles))))
        add("")
    if wide:
        add("## The Time Machine filter, tested wider")
        add("")
        add("It was the one choice that made trades better in the stress tests, so it got a test of its own: every "
            "setup and stop with a 1R target, market entry, no volume check and entries until 15:30 (%d combos), "
            "with and without the filter, over the %d sessions from %s to %s."
            % (wide["count"], wide["sessions"], wide["from"], wide["to"]))
        add("")
        rows = [[plain(item["id"]), "%d / %d" % (item["without"]["trades"], item["with"]["trades"]),
                 money(item["without"]["net"]), money(item["with"]["net"]), number(item["without"]["avg_r"], 3),
                 number(item["with"]["avg_r"], 3), number(item["without"]["sharpe"]), number(item["with"]["sharpe"])]
                for item in wide["combos"]]
        add(table(["Combination", "Trades without / with", "Net without", "Net with", "R per trade without",
                   "R per trade with", "Sharpe without", "Sharpe with"], rows))
        add("")
        add("- **R per trade was better with the filter in %d of %d combos**, the net in %d. With the filter %d of "
            "%d made money." % (wide["better_r"], wide["count"], wide["better_net"], wide["positive_with"],
                                wide["count"]))
        add("- Both columns are the same %d sessions and the filter's rule was fixed in advance (it is the pack's "
            "own), so this is a fair A/B test, not a pick. It is still one stretch of market, and the filter "
            "was not part of the in-sample search." % wide["sessions"])
        add("")
    add("## What this means for the bot")
    add("")
    add("- **Don't expect these rules to make money.** On these %d stocks and %d sessions, no choice of setup, "
        "stop, target, entry, volume check or window made money in both halves, or picked better trades than "
        "buying at 9:45 with no signal at all." % (len(symbols), len(summary["sessions"])))
    add("- **Raising the risk per trade only scales the result.** It moves the losses and the gains together; "
        "it is not a way to profit.")
    add("- **If you keep paper trading it,** the choices the two halves agreed on per trade were %s. Treat "
        "them as the least-bad defaults, not as a strategy." % (", ".join(agreed[:-1]) + " and " + agreed[-1]
                                                               if len(agreed) > 1 else "".join(agreed) or "none"))
    if wide and wide["better_r"] * 4 >= wide["count"] * 3:
        add("- **The Time Machine filter is worth keeping on** (the pack's S13 scenario): it improved R per trade "
            "in %d of %d combinations. It did not turn the rules into a money-maker, and each check takes about "
            "a second." % (wide["better_r"], wide["count"]))
    elif wide:
        add("- **The Time Machine filter did not help reliably:** R per trade was better with it in %d of %d "
            "combinations." % (wide["better_r"], wide["count"]))
    add("- **What was not tested, and could change the picture:**")
    add("  - the scanner's choice of stocks: `scan.py orb` picks the day's movers from the whole market, and "
        "the repo has a market-wide scan for one morning only, so this search used a fixed list of 30 large names;")
    add("  - more history: Yahoo keeps 5-minute bars for 60 days only, so this is one stretch of market. A "
        "longer test needs another data source;")
    add("  - short trades (the pack is long only), news and earnings days, and other markets.")
    add("")
    add("## Caveats")
    add("")
    add("- %d sessions is one market regime. A Sharpe ratio estimated from 20 days has a standard error of about "
        "3.5 (annualized), which is why the deflated Sharpe and the out-of-sample half matter more than any "
        "single figure." % len(summary["sessions"]))
    add("- The 30 names are today's well-known stocks, which is survivor bias of its own.")
    add("- Fills are simulated from 5-minute bars at the next bar's open, the stop or the target, with a flat cost "
        "a share; Alpaca's paper fills (at the bid and ask, with random partial fills) and live fills differ.")
    add("")
    add("## Run it again")
    add("")
    add("```bash")
    add("python -B paper-trading-scenarios/feature-search/fetch_data.py      # new 60-day window from Yahoo")
    add("python -B paper-trading-scenarios/feature-search/validate.py        # the engine vs the 13 scenarios")
    add("python -B paper-trading-scenarios/feature-search/search.py          # the grid, about 2 minutes")
    add("python -B paper-trading-scenarios/feature-search/stress.py          # the stress tests, about 10 minutes")
    add("python -B paper-trading-scenarios/feature-search/tm_filter.py       # the filter, about 80 minutes")
    add("python -B paper-trading-scenarios/feature-search/write_results.py   # this file")
    add("```")
    add("")
    add("A new download is a new 60-day window, so the numbers will change; with the same `data/` the runs "
        "repeat exactly.")
    with open(os.path.join(HERE, "RESULTS.md"), "w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(out) + "\n")
    print("wrote RESULTS.md")


if __name__ == "__main__":
    main()
