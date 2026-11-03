"""Build paper-trading-comparison.pdf from the scenario results.

    python -B paper-trading-scenarios/make_report.py [--build DIR]

Reads scenarios.json, each S*/summary.json, S*/trades.csv and S*/days/*/paper-broker.json,
draws the charts with matplotlib (SVG), lays the pages out in HTML and prints them to PDF
with Microsoft Edge (headless), or the Chrome or Chromium program that REPORT_BROWSER names.
--build keeps the HTML and SVG files in DIR.
"""

import csv
import datetime
import glob
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import matplotlib
matplotlib.use("svg")
import matplotlib.pyplot as plt      # noqa: E402
import numpy as np                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PDF = os.path.join(HERE, "paper-trading-comparison.pdf")
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
START_CASH = 5000.0

INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, RED = "#2a78d6", "#e34948"                 # gain and loss (the diverging poles)
TARGET, STOP, FLAT = "#008300", "#d03b3b", "#898781"

SHORT = {"S01": "Baseline", "S02": "Stop: range low", "S03": "Stop: 1.5 x ATR", "S04": "Stop: 1 %",
         "S05": "Target: 1.5R", "S06": "Target: 2R", "S07": "Entry: limit at cap", "S08": "Range: 30 minutes",
         "S09": "Volume: 1.5 x range", "S10": "Setup: prior-day high", "S11": "Risk: $100 a trade",
         "S12": "Window: until 11:00", "S13": "Time Machine filter"}
BASELINE_HAS = {"S02": "stop at the range midpoint", "S03": "stop at the range midpoint",
                "S04": "stop at the range midpoint", "S05": "target at 1R", "S06": "target at 1R",
                "S07": "market entry", "S08": "15-minute range", "S09": "no volume check",
                "S10": "opening-range breakout, midpoint stop", "S11": "$50 a trade ($150 a day)",
                "S12": "entries 09:45 to 15:30", "S13": "every READY ticket taken"}
NOTES = {
    "S01": "{bound_cap} of {trades} orders were sized by the 75 % position cap, not the $50 max loss, so a typical "
           "trade risked about {avg_gate}, not $50. Every other scenario is measured against this one.",
    "S02": "The wider stop puts the 1R target twice as far away, so most trades ran out of day: {flat_by} of "
           "{trades} were sold at the 15:55 flat-by and only {targets} reached the target. Fewer shares fit the "
           "$50 limit (about {avg_shares} a trade).",
    "S03": "An ATR stop sat near the range midpoint on most days, so most sessions ended the same way as the "
           "baseline ({same} of {base_trades}). It also had the deepest open loss of any $50 scenario.",
    "S04": "A 1 % stop was tighter than the midpoint on some days and wider on others. Two baseline stops "
           "became targets (25 Aug and 24 Sep). One of two scenarios that ended above $5,000.",
    "S05": "Two trades reached 1.5R and paid more than at 1R. Four of the baseline's six targets never got "
           "there and were sold at 15:55 instead: two for a little more than 1R would have paid, two for less.",
    "S06": "No trade reached 2R in these 39 sessions. The {flat_by} that did not stop out were sold at 15:55, "
           "and 16 Sep, a baseline target, became a stop. The worst result of the 13.",
    "S07": "Every limit order filled within five minutes of the signal, at or under the plan's price, so these "
           "are the baseline's trades at slightly better fills. No loss went past the risk check's figure; on "
           "other days a limit that never fills would mean a missed trade.",
    "S08": "Waiting 30 minutes for the range made it wider and the entries later; two sessions never "
           "triggered. 16 Sep turned from a target into a stop.",
    "S09": "Breakout bars this heavy were rare: {signals} of {sessions} sessions. The check skipped all six of "
           "the baseline's targets along with most of its stops, and both trades it took lost.",
    "S10": "A different setup trades different days: {overlap} of its {trades} trading sessions are baseline "
           "trading sessions. S04 uses the same 1 % stop on the opening-range setup, so compare it with S04 as "
           "well as with the baseline. The best result of the 13, and the smallest drawdown of the scenarios "
           "with more than two trades.",
    "S11": "The 75 % position cap set the size of every trade (9 or 10 shares), so doubling the limit only "
           "added shares where $50 had been the tighter limit. Two orders were blocked: size.py measures the "
           "cap against the $5,000 budget, check.py against the account after losses ($4,930.52). One of them "
           "(5 Oct) was a target in the baseline.",
    "S12": "Two signals came after 11:00 (22 Sep and 30 Sep); both were small flat-by gains in the baseline, so "
           "cutting them cost a little.",
    "S13": "The filter skipped two trades (25 Aug and 24 Sep) where the Time Machine had stop-first ahead; both "
           "were stops in the baseline. Two skips are far too few to say the filter works.",
}


def money(value, sign=True):
    if value is None:
        return ""
    text = "${:,.2f}".format(abs(value))
    if value < 0:
        return "-" + text
    return ("+" if sign and value > 0 else "") + text


def pct(value):
    return "" if value is None else "{:.0f} %".format(100 * value)


def esc(text):
    return html.escape(str(text))


def short_date(day):
    return datetime.date.fromisoformat(day).strftime("%d %b").lstrip("0")


def load():
    with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as file:
        spec = json.load(file)
    runs = []
    for scenario in spec["scenarios"]:
        folder = os.path.join(HERE, scenario["id"])
        with open(os.path.join(folder, "summary.json"), encoding="utf-8") as file:
            summary = json.load(file)
        with open(os.path.join(folder, "trades.csv"), encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        for row in rows:
            for key in ("pnl", "gate_max_loss", "max_open_loss", "loss_at_stop_after_fill", "fill_price", "stop", "exit_price",
                        "target", "plan_entry", "fill_minus_plan", "cash_after", "tm_p_target", "tm_p_stop"):
                row[key] = float(row[key]) if row.get(key) not in (None, "") else None
        runs.append({"id": scenario["id"], "key": scenario["id"][:3], "scenario": scenario, "summary": summary,
                     "rows": rows, "trades": [row for row in rows if row["action"] == "traded"]})
    return spec, runs


def outcome(row):
    if row["action"] == "traded":
        return {"target": "T", "stop": "S", "flat by": "F"}[row["exit_reason"]]
    return {"blocked by risk": "B", "skipped by the filter": "X", "not filled": "N"}.get(row["action"], "")


EXTRA = {}


def facts(runs):
    """Everything the text quotes, worked out from the files."""
    base = runs[0]
    trades = [(run, row) for run in runs for row in run["trades"]]
    extra_run, extra_row = max(trades, key=lambda pair: -pair[1]["loss_at_stop_after_fill"] - pair[1]["gate_max_loss"])
    over = [-row["pnl"] - row["gate_max_loss"] for _, row in trades if row["pnl"] < -row["gate_max_loss"] - 0.005]
    pools = []
    for path in glob.glob(os.path.join(HERE, base["id"], "days", "*", "timemachine.json")):
        with open(path, encoding="utf-8") as file:
            pools.append(json.load(file)["mc"]["pool_sessions"])
    one_r = [run for run in runs if run["key"] not in ("S05", "S06") and run["summary"]["targets"] + run["summary"]["stops"] >= 5]
    forecast_share = [run["summary"]["tm_avg_p_target"] / (run["summary"]["tm_avg_p_target"] + run["summary"]["tm_avg_p_stop"])
                      for run in one_r]
    run50, row50 = max(((run, row) for run, row in trades if run["key"] != "S11"),
                       key=lambda pair: -pair[1]["loss_at_stop_after_fill"])
    if -row50["loss_at_stop_after_fill"] > 50:
        over_text = ("One position did stand to lose more: on %s in %s (%s), the market order filled %.0f cents above "
                     "the plan's price, so its %s shares, sized to %s at the plan, risked %s at the stop. It never got "
                     "there (%s, %s)."
                     % (short_date(row50["date"]), run50["key"], SHORT[run50["key"]], 100 * row50["fill_minus_plan"],
                        row50["shares"], money(row50["gate_max_loss"], sign=False),
                        money(-row50["loss_at_stop_after_fill"], sign=False),
                        {"flat by": "sold at 15:55", "target": "it hit the target", "stop": "stopped"}[row50["exit_reason"]],
                        money(row50["pnl"])))
        over_short = ("Once, a market fill above the plan's price took a position's risk past the limit (%s at the "
                      "stop, %s %s); it never hit the stop." % (money(-row50["loss_at_stop_after_fill"], sign=False),
                                                                 run50["key"], short_date(row50["date"])))
        EXTRA["over_finding"] = ("once it took a trade's risk past the $50 limit (%s at the stop, %s on %s)"
                                 % (money(-row50["loss_at_stop_after_fill"], sign=False), run50["key"],
                                    short_date(row50["date"])))
    else:
        EXTRA["over_finding"] = "it never took a trade past the $50 limit here"
        over_text = "Even after the fill, no position stood to lose more than %s at its stop." % money(
            -row50["loss_at_stop_after_fill"], sign=False)
        over_short = ""
    EXTRA.update(
        over_text=over_text, over_short=over_short,
        both=sum(run["summary"]["stop_and_target_same_bar"] for run in runs),
        max=money(-extra_row["loss_at_stop_after_fill"] - extra_row["gate_max_loss"], sign=False),
        max_key=extra_run["key"], max_date=short_date(extra_row["date"]),
        max_slip="%.0f cents" % (100 * extra_row["fill_minus_plan"]),
        over_low="%.0f" % (100 * min(over)) if over else "0", over_high="%.0f" % (100 * max(over)) if over else "0",
        gaps=sum(1 for _, row in trades if row["exit_reason"] == "stop" and row["exit_price"] < row["stop"] - 0.005),
        risk50=money(max(-row["loss_at_stop_after_fill"] for run, row in trades if run["key"] != "S11"), sign=False),
        pool_low=min(pools), pool_high=max(pools),
        share_low=pct(min(forecast_share)), share_high=pct(max(forecast_share)),
        actual_low=pct(min(run["summary"]["actual_target_share"] for run in one_r)),
        actual_high=pct(max(run["summary"]["actual_target_share"] for run in one_r)),
        one_r_skipped=", ".join(run["key"] for run in runs if run["key"] not in ("S05", "S06") and run not in one_r))
    base_by_day = {row["date"]: row for row in base["rows"]}
    rng = np.random.default_rng(19)
    for run in runs:
        summary, trades = run["summary"], run["trades"]
        pnls = np.array([row["pnl"] for row in trades])
        sums = rng.choice(pnls, size=(10000, len(pnls)), replace=True).sum(axis=1) if len(pnls) else np.zeros(1)
        run["range90"] = tuple(float(x) for x in np.percentile(sums, [5, 95]))
        changed = [(row["date"], outcome(base_by_day[row["date"]]), outcome(row),
                    (row["pnl"] or 0) - (base_by_day[row["date"]]["pnl"] or 0)) for row in run["rows"]
                   if outcome(row) != outcome(base_by_day[row["date"]])
                   or abs((row["pnl"] or 0) - (base_by_day[row["date"]]["pnl"] or 0)) > 0.005]
        run["changed"] = changed
        run["same"] = sum(1 for row in trades if outcome(base_by_day[row["date"]]) == outcome(row))
        run["overlap"] = sum(1 for row in trades if base_by_day[row["date"]]["action"] == "traded")
        run["bound_cap"] = sum(1 for row in trades if row["limit_binding"] == "position cap")
        run["extra_risk"] = max(trades, key=lambda row: -row["loss_at_stop_after_fill"] - row["gate_max_loss"])
        run["limit"] = 100 if run["key"] == "S11" else 50
        run["fields"] = dict(trades=summary["trades"], targets=summary["targets"], stops=summary["stops"],
                             flat_by=summary["flat_by"], signals=summary["signals"], sessions=summary["sessions"],
                             avg_gate=money(summary["avg_gate_max_loss"], sign=False),
                             avg_shares="{:.0f}".format(summary["avg_shares"] or 0), bound_cap=run["bound_cap"],
                             same=run["same"], base_trades=base["summary"]["trades"], overlap=run["overlap"])


# ---------------------------------------------------------------- charts

def style():
    plt.rcParams.update({
        "font.family": "Segoe UI", "font.size": 7.5, "svg.fonttype": "none", "axes.unicode_minus": False,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.linewidth": 0.6, "xtick.color": MUTED,
        "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2, "xtick.major.width": 0.6,
        "ytick.major.width": 0.6, "xtick.major.size": 2.5, "ytick.major.size": 0, "axes.grid": False,
        "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": "none", "axes.facecolor": "none",
        "text.color": INK})


def svg(fig):
    buffer = io.StringIO()
    fig.savefig(buffer, format="svg", bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    text = buffer.getvalue()
    return text[text.index("<svg"):]


def labels(runs):
    return ["%s  %s" % (run["key"], SHORT[run["key"]]) for run in runs]


def chart_net(runs):
    fig, ax = plt.subplots(figsize=(7.0, 3.3))
    y = np.arange(len(runs))[::-1]
    for row_y, run in zip(y, runs):
        low, high = run["range90"]
        net = run["summary"]["net_pnl"]
        ax.plot([low, high], [row_y, row_y], color=AXIS, lw=2, solid_capstyle="round", zorder=1)
        color = BLUE if net >= 0 else RED
        ax.scatter([net], [row_y], s=34, color=color, edgecolor="white", linewidth=1.2, zorder=3)
        ax.text(1.01, row_y, money(net), va="center", ha="left", fontsize=7, color=INK,
                transform=ax.get_yaxis_transform())
    ax.text(1.01, len(runs) - 0.3, "net", va="bottom", ha="left", fontsize=6.5, color=MUTED,
            transform=ax.get_yaxis_transform())
    ax.axvline(0, color=INK_2, lw=0.8, zorder=0)
    ax.set_yticks(y)
    ax.set_yticklabels(labels(runs))
    ax.set_ylim(-0.6, len(runs) - 0.4)
    ax.set_xlim(min(run["range90"][0] for run in runs) - 20, max(run["range90"][1] for run in runs) + 20)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: money(v, sign=False) if v >= 0
                                                                 else "-" + money(-v, sign=False)))
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Net result over the 39 sessions (dot; blue a gain, red a loss) and its 90 % bootstrap range (bar)")
    return svg(fig)


def chart_grid(runs, sessions):
    fig, ax = plt.subplots(figsize=(7.0, 3.4))
    colors = {"T": TARGET, "S": STOP, "F": FLAT}
    for row_index, run in enumerate(runs):
        by_day = {row["date"]: row for row in run["rows"]}
        for col, day in enumerate(sessions):
            code = outcome(by_day[day])
            y = len(runs) - 1 - row_index
            if code in colors:
                ax.add_patch(plt.Rectangle((col + 0.06, y + 0.06), 0.88, 0.88, color=colors[code], lw=0))
                ax.text(col + 0.5, y + 0.47, code, ha="center", va="center", fontsize=5.6, color="white",
                        fontweight="bold")
            elif code:
                ax.add_patch(plt.Rectangle((col + 0.1, y + 0.1), 0.8, 0.8, facecolor="white", edgecolor=INK_2, lw=0.6))
                ax.text(col + 0.5, y + 0.47, code, ha="center", va="center", fontsize=5.6, color=INK, fontweight="bold")
            else:
                ax.add_patch(plt.Rectangle((col + 0.06, y + 0.06), 0.88, 0.88, color="#f0efec", lw=0))
    ax.set_xlim(0, len(sessions))
    ax.set_ylim(0, len(runs))
    ax.set_yticks(np.arange(len(runs)) + 0.5)
    ax.set_yticklabels(labels(runs)[::-1])
    ax.set_xticks(np.arange(len(sessions)) + 0.5)
    ax.set_xticklabels([datetime.date.fromisoformat(d).strftime("%m-%d") for d in sessions], rotation=90,
                       fontsize=5.6)
    ax.tick_params(axis="x", length=0, pad=2)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_aspect("equal")
    return svg(fig)


def chart_equity(runs, sessions):
    fig, axes = plt.subplots(4, 4, figsize=(7.0, 7.0))        # not shared: the legend panel sets its own limits
    base = [START_CASH] + [row["cash_after"] for row in runs[0]["rows"]]
    x = np.arange(len(sessions) + 1)
    values = [row["cash_after"] for run in runs for row in run["rows"]]
    low, high = min(values + [START_CASH]), max(values + [START_CASH])
    for index, ax in enumerate(axes.flat):
        if index >= len(runs):
            ax.axis("off")
            continue
        run = runs[index]
        curve = [START_CASH] + [row["cash_after"] for row in run["rows"]]
        ax.axhline(START_CASH, color=GRID, lw=0.6, zorder=0)
        if index:
            ax.plot(x, base, color=AXIS, lw=1.2, zorder=1)
        ax.plot(x, curve, color=BLUE, lw=1.6, zorder=2)
        end = curve[-1]
        ax.set_title("%s  %s" % (run["key"], SHORT[run["key"]]), fontsize=7, loc="left", color=INK, pad=3)
        ax.scatter([x[-1]], [end], s=8, color=BLUE, zorder=3)
        ax.annotate(money(end - START_CASH), (x[-1], end), xytext=(4, 0), textcoords="offset points", fontsize=6.5,
                    va="center", ha="left", color=INK)
        ax.set_ylim(low - 25, high + 25)
        ax.set_xlim(0, len(sessions) + 11)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: "${:,.0f}".format(v)))
        ax.tick_params(labelsize=6)
        ax.set_xticks([1, 20, 39])
        if index + 4 >= len(runs):               # the lowest panel in its column
            ax.set_xticklabels([short_date(sessions[0]), short_date(sessions[19]), short_date(sessions[38])])
        else:
            ax.set_xticklabels([])
        if index % 4:
            ax.set_yticklabels([])
    legend_ax = axes.flat[len(runs)]
    legend_ax.plot([0.05, 0.2], [0.82, 0.82], color=BLUE, lw=1.6)
    legend_ax.text(0.25, 0.82, "the scenario's account", va="center", fontsize=7, color=INK)
    legend_ax.plot([0.05, 0.2], [0.64, 0.64], color=AXIS, lw=1.2)
    legend_ax.text(0.25, 0.64, "the baseline (S01)", va="center", fontsize=7, color=INK)
    legend_ax.text(0.05, 0.3, "Cash after each session,\nfrom $5,000. The same\nscale in every panel.",
                   fontsize=6.5, color=INK_2, va="center", linespacing=1.4)
    legend_ax.set_xlim(0, 1)
    legend_ax.set_ylim(0, 1)
    legend_ax.axis("on")
    for spine in legend_ax.spines.values():
        spine.set_visible(False)
    legend_ax.set_xticks([])
    legend_ax.set_yticks([])
    fig.subplots_adjust(hspace=0.5, wspace=0.12)
    return svg(fig)


def chart_open_losses(runs):
    fig, ax = plt.subplots(figsize=(7.0, 3.6))
    y = np.arange(len(runs))[::-1]
    jitter = np.random.default_rng(7)
    for row_y, run in zip(y, runs):
        for row in run["trades"]:
            color = BLUE if row["pnl"] > 0 else RED
            ax.scatter([row["max_open_loss"]], [row_y + jitter.uniform(-0.22, 0.22)], s=14, color=color,
                       edgecolor="white", linewidth=0.6, zorder=3)
        deepest = run["summary"]["deepest_open_loss"]["loss"]
        ax.text(1.01, row_y, money(deepest), ha="left", va="center", fontsize=6.8, color=INK,
                transform=ax.get_yaxis_transform())
        ax.plot([-run["limit"]] * 2, [row_y - 0.4, row_y + 0.4], color=INK, lw=1.4, zorder=2)
    ax.text(1.01, len(runs) - 0.3, "deepest", va="bottom", ha="left", fontsize=6.5, color=MUTED,
            transform=ax.get_yaxis_transform())
    ax.set_yticks(y)
    ax.set_yticklabels(labels(runs))
    ax.set_ylim(-0.6, len(runs) - 0.4)
    ax.set_xlim(-108, 3)
    ax.axvline(0, color=AXIS, lw=0.6)
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: money(v, sign=False) if v >= 0
                                                                 else "-" + money(-v, sign=False)))
    ax.scatter([], [], s=14, color=BLUE, label="trade ended with a gain")
    ax.scatter([], [], s=14, color=RED, label="trade ended with a loss")
    ax.plot([], [], color=INK, lw=1.4, label="the max loss a trade allowed in that scenario")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, frameon=False, fontsize=7, handletextpad=0.4,
              columnspacing=1.4)
    ax.set_xlabel("Deepest open loss of each trade: the most it was down at any one moment (each 5-minute bar's low)")
    return svg(fig)


def chart_instances(runs, instances):
    fig, axes = plt.subplots(1, len(instances), figsize=(7.0, 2.5))
    for ax, (title, run, row) in zip(axes, instances):
        with open(os.path.join(HERE, run["id"], "days", row["date"], "paper-broker.json"), encoding="utf-8") as file:
            fill = json.load(file)
        marks = fill["marks"]
        x = np.arange(len(marks))
        ax.fill_between(x, [m["low"] for m in marks], [m["high"] for m in marks], color="#cde2fb", lw=0, step="mid")
        ax.plot(x, [m["close"] for m in marks], color=BLUE, lw=1.2, drawstyle="steps-mid")
        for level, name, color in ((fill["target"], "target", TARGET), (fill["fill_price"], "fill", INK_2),
                                   (fill["stop"], "stop", STOP)):
            ax.axhline(level, color=color, lw=0.9, ls=(0, (3, 2)) if name != "fill" else "-")
        worst = min(range(len(marks)), key=lambda i: marks[i]["open_pnl_worst"])
        ax.scatter([worst], [marks[worst]["worst"]], s=26, color=RED, edgecolor="white", linewidth=0.8, zorder=4)
        right_half = worst > (len(marks) - 1) / 2
        ax.annotate(money(marks[worst]["open_pnl_worst"]), (worst, marks[worst]["worst"]),
                    xytext=(-5, 4) if right_half else (5, 4), ha="right" if right_half else "left",
                    textcoords="offset points", fontsize=6.8, color=INK, fontweight="bold")
        ax.set_title("%s\n%s %s, %d shares\nfill %.2f, stop %.2f\ntarget %.2f" % (
            title, run["key"], short_date(row["date"]), fill["qty"], fill["fill_price"], fill["stop"], fill["target"]),
            fontsize=6.6, loc="left", color=INK)
        ticks = sorted({0, len(marks) - 1})
        ax.set_xticks(ticks)
        ax.set_xticklabels([marks[i]["time"] for i in ticks], fontsize=5.8)
        ax.tick_params(axis="y", labelsize=5.8)
        ax.set_xlim(-0.6, len(marks) - 0.4)
        span = [fill["stop"], fill["target"]] + [m["low"] for m in marks] + [m["high"] for m in marks]
        pad = (max(span) - min(span)) * 0.08
        ax.set_ylim(min(span) - pad, max(span) + pad)
    fig.subplots_adjust(wspace=0.38)
    return svg(fig)


# ---------------------------------------------------------------- pages

CSS = """
@page { size: A4; margin: 15mm 16mm 16mm 16mm;
        @bottom-left { content: "Paper trading the Day Trader skill: 13 scenarios"; font: 7pt 'Segoe UI', sans-serif;
                       color: #898781; }
        @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 7pt 'Segoe UI', sans-serif;
                        color: #898781; } }
:root { --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
        --wash: #f4f3f0; --blue: #2a78d6; --red: #d03b3b; --green: #006300; }
* { box-sizing: border-box; }
html { background: #ffffff; }
body { margin: 0; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 9pt; line-height: 1.42; color: var(--ink);
       background: #ffffff; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
section { break-before: page; }
section:first-of-type { break-before: auto; }
h1 { font-size: 22pt; line-height: 1.15; margin: 0 0 4pt; font-weight: 600; letter-spacing: -0.2pt; }
h2 { font-size: 14pt; margin: 0 0 6pt; font-weight: 600; }
h3 { font-size: 10pt; margin: 12pt 0 3pt; font-weight: 600; }
p { margin: 0 0 6pt; }
.sub { font-size: 11pt; color: var(--ink-2); margin-bottom: 2pt; }
.meta { font-size: 8pt; color: var(--muted); margin-bottom: 14pt; }
.lede { font-size: 10pt; }
.box { border: 0.6pt solid var(--axis); border-radius: 4pt; padding: 8pt 10pt; margin: 8pt 0; }
.box.warn { background: var(--wash); border-color: var(--wash); }
.box h3 { margin-top: 0; }
ul, ol { margin: 0 0 6pt; padding-left: 14pt; }
li { margin-bottom: 3pt; }
table { border-collapse: collapse; width: 100%; font-size: 7.8pt; margin: 4pt 0 8pt; }
th { text-align: left; font-weight: 600; color: var(--ink-2); border-bottom: 0.8pt solid var(--axis);
     padding: 3pt 4pt; vertical-align: bottom; }
td { padding: 2.6pt 4pt; border-bottom: 0.5pt solid var(--grid); vertical-align: top; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.base td { background: var(--wash); }
.gain { color: var(--green); } .loss { color: var(--red); }
.fig { margin: 6pt 0 2pt; } .fig svg { width: 100%; height: auto; display: block; }
.cap { font-size: 7.6pt; color: var(--ink-2); margin: 2pt 0 8pt; }
.key { display: inline-block; width: 9pt; height: 9pt; border-radius: 1.5pt; vertical-align: -1pt; margin: 0 3pt 0 8pt;
       color: #fff; font-size: 6pt; font-weight: 700; text-align: center; line-height: 9pt; }
.case { break-inside: avoid; border-top: 0.5pt solid var(--grid); padding: 5pt 0 4pt; }
.case h3 { margin: 0 0 2pt; }
.case .choice { font-size: 8.4pt; color: var(--ink-2); font-style: italic; margin-bottom: 1pt; }
.case .line { font-size: 7.8pt; color: var(--ink-2); margin-bottom: 2pt; font-variant-numeric: tabular-nums; }
table.tight { font-size: 7.4pt; } table.tight td { white-space: nowrap; } table.tight th { line-height: 1.2; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16pt; }
.flow { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4pt; margin: 6pt 0 10pt; }
.flow div { border: 0.6pt solid var(--axis); border-radius: 3pt; padding: 4pt 5pt; font-size: 7.4pt; }
.flow b { display: block; font-size: 7.8pt; margin-bottom: 1pt; }
.flow div.sim { border-style: dashed; }
.small { font-size: 7.8pt; color: var(--ink-2); }
code { font-family: Consolas, monospace; font-size: 7.8pt; }
"""


def signed(value):
    css = "gain" if value > 0 else "loss" if value < 0 else ""
    return '<span class="%s">%s</span>' % (css, money(value))


def page_summary(spec, runs):
    base = runs[0]["summary"]
    s = {run["key"]: run["summary"] for run in runs}
    above = sorted([run for run in runs if run["summary"]["net_pnl"] > 0], key=lambda r: -r["summary"]["net_pnl"])
    worst = min(runs, key=lambda r: r["summary"]["net_pnl"])
    trades = sum(run["summary"]["trades"] for run in runs)
    deepest50 = min((run for run in runs if run["limit"] == 50), key=lambda r: r["summary"]["deepest_open_loss"]["loss"])
    dd = [run["summary"]["max_drawdown"]["amount"] for run in runs]
    all_cross = all(run["range90"][0] < 0 < run["range90"][1] for run in runs if run["summary"]["trades"] >= 10)
    d50 = deepest50["summary"]["deepest_open_loss"]
    mismatches = sum(len(run["summary"].get("books_mismatches", [])) for run in runs)
    return """
<section>
<h1>Paper trading the Day Trader skill</h1>
<div class="sub">13 scenarios on the same 39 recorded TSLA sessions, %(first)s to %(last)s</div>
<div class="meta">Run on 3 November 2026 with the repo's own tools and data. $5,000 practice account. Every number
here comes from the files in <code>paper-trading-scenarios/</code>.</div>

<p class="lede">Each scenario is the README example's rules with <b>one choice changed</b>: the stop, the target,
the entry order, the opening range, a volume check, the setup, the risk per trade, the entry window, or the Time
Machine as a filter. Nine choices give twelve alternative settings, plus the baseline, so any difference traces back to
that one choice. For every session, the pack's own tools found the signal, planned the exit, replayed it in the Time Machine,
sized it, ran the risk check and showed the ticket. Where the ticket was READY, it got an automatic practice yes, a
simulated paper broker filled the exact order the ticket showed against that session's 5-minute bars, and the tools
recorded every placement and fill, as on a real day.</p>

<div class="box"><h3>What happened</h3><ul>
<li><b>The pipeline ran on all %(runs)d scenario sessions</b> (13 x 39) without a tool error: %(trades)d trades,
the rest no signal, plus %(blocked)d risk blocks and %(skipped)d filter skips. Every READY ticket got an automatic
practice yes (<code>ticket.py approve --auto-yes</code>), and nothing was sent to a broker. %(books)s</li>
<li><b>%(n_above)d of 13 ended above $5,000:</b> %(above)s. The baseline ended at %(base_net)s over %(base_trades)d
trades (%(base_t)d targets, %(base_s)d stops, %(base_f)d sold at 15:55). The worst was %(worst_name)s at
%(worst_net)s.</li>
<li><b>None of it is beyond chance.</b> %(cross)s With 17 to 19 trades, one trade ending at the target instead of
the stop moves a total by about %(flip)s.</li>
<li><b>Maximum loss at once.</b> In the twelve $50 scenarios, no open position was ever down more than
%(d50)s (%(d50_key)s on %(d50_date)s at %(d50_time)s). With the $100 limit the worst moment was %(d100)s. %(gaps)s
%(over_short)s Over several sessions the losses added up: drawdowns of %(dd_low)s to %(dd_high)s, which the daily limit ($150, one
day at a time) never sees.</li>
<li><b>The runs turned up three things in the tools</b> (see the last pages): size.py and check.py measure the position cap
against different bases, so two S11 orders were sized and then blocked. Market fills above the plan's price carried up
to %(extra)s more risk than the risk check approved. An entry watch that ends without a trigger leaves the previous
day's <code>entry.json</code> in place (size.py then refuses the old plan, so no order comes of it).</li>
</ul></div>

<div class="box warn"><h3>Read this first</h3>
<p><b>Not financial advice.</b> This is one stock over eight weeks, replayed with simulated fills. It shows what these
rules did on these days, not what they will do on the next ones. Fills come from 5-minute bars without fees, spread or
partial fills. No broker was connected.</p></div>
</section>""" % dict(
        first=short_date(spec["sessions"][0]) + " 2026", last=short_date(spec["sessions"][-1]) + " 2026",
        runs=13 * len(spec["sessions"]), trades=trades,
        blocked=sum(run["summary"]["blocked_by_risk"] for run in runs),
        skipped=sum(run["summary"]["skipped_by_filter"] for run in runs),
        books=("The tools' own books matched the simulated broker's cash every evening." if not mismatches else
               "In %d sessions the tools' own books did not match the simulated broker's cash (see summary.json)."
               % mismatches),
        n_above=len(above), above=" and ".join("%s %s (%s)" % (r["key"], esc(SHORT[r["key"]]),
                                                               money(r["summary"]["net_pnl"])) for r in above),
        base_net=money(base["net_pnl"]), base_trades=base["trades"], base_t=base["targets"], base_s=base["stops"],
        base_f=base["flat_by"], worst_name="%s %s" % (worst["key"], esc(SHORT[worst["key"]])),
        worst_net=money(worst["summary"]["net_pnl"]),
        cross=("Every scenario with ten or more trades has a 90 % bootstrap range that runs from a loss to a gain, so "
               "none can be told apart from break-even, or from the others." if all_cross else
               "Most scenarios have a 90 % range that runs from a loss to a gain."),
        d50=money(d50["loss"]), d50_key=deepest50["key"], d50_date=short_date(d50["date"]), d50_time=d50["time"],
        d100=money(s["S11"]["deepest_open_loss"]["loss"]), dd_low=money(min(dd), sign=False),
        dd_high=money(max(dd), sign=False), extra=EXTRA["max"], flip=money(2 * base["avg_gate_max_loss"], sign=False),
        over_short=EXTRA["over_short"],
        gaps="No stop was gapped through." if EXTRA["gaps"] == 0 else "%d stops filled below their price." % EXTRA["gaps"])


def page_setup(spec):
    return """
<section>
<h2>The setup, and how each session ran</h2>
<h3>Setup done on this PC (howtouse.md, section 1)</h3>
<table><tr><th>Step</th><th>What was done</th></tr>
<tr><td>python3</td><td>Copied <code>python.exe</code> to <code>python3.exe</code> in the Python 3.13 folder, so the
skills' <code>python3</code> commands run the real Python (checked in Git Bash: Python 3.13.5).</td></tr>
<tr><td>Skills</td><td>Copied the 11 <code>trader-*</code> folders into <code>%USERPROFILE%\\.claude\\skills</code>, so
<code>/trader-scanner</code> and the rest appear in new Claude Code sessions.</td></tr>
<tr><td>Workspace</td><td>Made <code>%USERPROFILE%\\trading-desk\\trader</code> with <code>config.json</code>, a copy
of the example rules ($5,000, $50 a trade, $150 a day, paper).</td></tr>
<tr><td>TRADER_HOME</td><td>Set to that folder (user environment variable), so every tool uses it from any folder.
First fixed the tests so they ignore it: before the fix, setting it made 12 tests fail and wrote test files into that
folder.</td></tr>
<tr><td>Checked</td><td>All 384 tests pass, with and without TRADER_HOME. The offline replay of the README example
gives the README's numbers.</td></tr>
<tr><td>Left for you</td><td>Your name and email in <code>sec_user_agent</code> (the SEC tools send it). The Alpaca
paper connection (howtouse.md section 4) needs your own keys and dashboard. Add <code>alpaca_account</code> only
after you have read the number in the Alpaca dashboard yourself.</td></tr></table>

<h3>One session, in one scenario</h3>
<div class="flow">
<div><b>1 Entry</b>entry.py watch replays the session bar by bar from 9:30 and stops at the trigger.</div>
<div><b>2 Exit plan</b>exit.py plan sets the stop and target before any order.</div>
<div><b>3 Time Machine</b>10,000 replays of the rest of the day, plus every past session of the rule.</div>
<div><b>4 Size</b>size.py fits whole shares inside the four limits and writes one bracket order.</div>
<div><b>5 Risk</b>check.py gives PASS or BLOCK and records the verdict.</div>
<div><b>6 Ticket and yes</b>ticket.py show turns a PASS into a READY ticket; approve --auto-yes gives the yes.</div>
<div class="sim"><b>7 Paper broker</b>Simulated: fills that order on the session's bars; the tools record it.</div>
</div>
<p>Steps 1 to 6 are the real tools, run from the repo on a <b>pinned clock</b> (practice runs), in the scenario's own
trader folder. Each morning the folder is cleared of the previous day's plan files, and the account starts with the
cash the tools recorded the evening before. <b>The yes in step 6 is automatic</b>: <code>ticket.py approve
--auto-yes</code>, which the tool accepts only on a practice run with <code>"paper": true</code>, so nothing can be
placed from it; on the real clock it refuses, and a real order still needs your typed yes. Step 7 stands in for the
Allow once card and Alpaca paper. The tools then record the placement and every fill (<code>ticket.py result</code>,
<code>fill --practice</code>), and a position still open at 15:55 goes out through <code>flatten</code>, the risk
check and its own automatic yes, so the risk check counts each trade the way it would on a real day.</p>

<h3>The simulated paper broker</h3>
<ul>
<li><b>Market entry:</b> fills at the open of the bar after the signal, the first price after the trigger bar
closed.</li>
<li><b>Limit entry:</b> fills at that open if it is at or under the limit, else at the limit once a bar trades down to
it. If it has not filled by the end of the entry window, it is canceled.</li>
<li><b>Stop-loss:</b> sells at the stop, or at the bar's open if a bar opens below the stop (a gap).
<b>Take-profit:</b> sells at the target, or at the open if a bar opens above it.</li>
<li><b>Both in one bar:</b> the stop is assumed to come first, because 5-minute bars do not say which came first.
<b>Flat by:</b> anything still open at 15:55 sells at that bar's open, once the bracket's two legs are canceled; the
sell is the flatten ticket the tools wrote, checked and approved.</li>
<li><b>Open loss</b> is marked at every bar's low (or the stop fill), so the deepest open loss is the most the position
was down at any one moment.</li>
</ul>
<p class="small">The data is the repo's 60 days of TSLA 5-minute bars
(<code>tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz</code>, 59 full sessions). The first 20 sessions serve as
history only, because the Time Machine needs 20 sessions before the day it replays. No network was used. A separate
check recomputed all 220 trades from the raw bars and got the same fills, exits and results.</p>
</section>"""


def page_scenarios(runs):
    rows = "".join("<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        ' class="base"' if run["key"] == "S01" else "", run["key"], esc(run["scenario"]["feature"]),
        esc(run["scenario"]["choice"]), esc(BASELINE_HAS.get(run["key"], ""))) for run in runs)
    return """
<section>
<h2>The 13 scenarios</h2>
<p>The baseline is the README example's trade rule, run every session. Each other scenario changes one thing and keeps
the rest: the 15-minute opening range on 5-minute bars, a close above the range high, the stop at the range midpoint,
the target at 1R, a market entry, and the example config: $5,000 budget, $50 max loss a trade, $150 a day, 3 trades a
day, a 75 %% position cap, a 10 %% cash buffer, minimum reward:risk 1.0, entries 09:45 to 15:30, flat by 15:55.</p>
<table><tr><th>#</th><th>Feature</th><th>This scenario</th><th>The baseline has</th></tr>%s</table>
<p class="small">Choices left out: 1-minute bars (the repo has no 1-minute history), the Time Machine's block bootstrap
(it changes the forecast, not the trade), and the other config limits, which one trade a day never reaches.</p>
</section>""" % rows


def page_results(runs, chart):
    body = ""
    for run in runs:
        s = run["summary"]
        body += ("<tr%s><td>%s %s</td><td class='n'>%d</td><td class='n'>%d / %d / %d</td><td class='n'>%s</td>"
                 "<td class='n'>%s</td><td class='n'>%+.2f</td><td class='n'>%s</td><td class='n'>%s</td>"
                 "<td class='n'>%s</td><td class='n'>%s</td></tr>") % (
            ' class="base"' if run["key"] == "S01" else "", run["key"], esc(SHORT[run["key"]]), s["trades"],
            s["targets"], s["stops"], s["flat_by"], pct(s["win_rate"]), signed(s["net_pnl"]), s["avg_r"] or 0,
            money(s["worst_trade"]["pnl"]), money(s["deepest_open_loss"]["loss"]),
            money(-s["max_drawdown"]["amount"]), "${:,.2f}".format(s["end_cash"]))
    return """
<section>
<h2>Results at a glance</h2>
<table><tr><th>Scenario</th><th class="n">Trades</th><th class="n">Target / stop / 15:55</th><th class="n">Won</th>
<th class="n">Net</th><th class="n">Avg R</th><th class="n">Worst trade</th><th class="n">Deepest open loss</th>
<th class="n">Max drawdown</th><th class="n">End cash</th></tr>%s</table>
<p class="cap">Avg R: the average result per trade in units of the risk the risk check approved for it (+1 is a full
target, -1 a full stop). Drawdown: the largest fall of the account from a high to a later low.</p>
<div class="fig">%s</div>
<p class="cap">The bar is the range where 90 %% of 10,000 bootstrap totals fell: the scenario's own trades drawn again
at random, as if the same kind of days had come in another mix. Every bar with more than a handful of trades runs
from a loss to a gain, so these totals cannot rank the choices. S09 has only two trades, so its bar says little.</p>
</section>""" % (body, chart)


def page_grid(runs, chart):
    return """
<section>
<h2>Session by session</h2>
<p>What each scenario did on each of the 39 sessions.
<span class="key" style="background:%(target)s">T</span>target
<span class="key" style="background:%(stop)s">S</span>stop
<span class="key" style="background:%(flat)s">F</span>sold at the 15:55 flat-by
<span class="key" style="background:#fff;color:#0b0b0b;border:0.6pt solid #52514e">B</span>blocked by the risk check
<span class="key" style="background:#fff;color:#0b0b0b;border:0.6pt solid #52514e">X</span>skipped by the filter
<span class="key" style="background:#f0efec"></span>no signal</p>
<div class="fig">%(chart)s</div>
<ul class="small">
<li>The opening-range rules signal on the same 19 sessions (17 with the 30-minute range or the 11:00 cut-off), so
their rows line up: a change of stop or target changes how a trade ends, not whether there is one.</li>
<li>S09 and S10 trade different days. The volume check passed on only %(s09)d sessions; the prior-day high
triggered on %(s10)d, %(s10_new)d of them sessions with no opening-range signal.</li>
<li>No signal means the price never closed above the range high (or the level) inside the entry window: 20 of the 39
sessions for the baseline.</li>
</ul>
</section>""" % dict(target=TARGET, stop=STOP, flat=FLAT, chart=chart, s09=runs[8]["summary"]["signals"],
                     s10=runs[9]["summary"]["trades"], s10_new=runs[9]["summary"]["trades"] - runs[9]["overlap"])


def page_equity(runs, chart):
    above = [run["key"] for run in runs if run["summary"]["end_cash"] > START_CASH]
    lowest = min(runs, key=lambda run: run["summary"]["end_cash"])
    return """
<section>
<h2>The account over the 39 sessions</h2>
<div class="fig">%s</div>
<p class="cap">Each step is one trade of a few tens of dollars, so a line's shape comes from a handful of sessions.
%s finish above $5,000; %s %s finishes lowest, at $%s.</p>
</section>""" % (chart, " and ".join(above), lowest["key"], esc(SHORT[lowest["key"]]),
                 "{:,.2f}".format(lowest["summary"]["end_cash"]))


def case_html(run, base):
    s = run["summary"]
    line = "%d trades: %d target, %d stop, %d at 15:55. Net %s, avg R %+.2f, worst trade %s, deepest open loss %s." % (
        s["trades"], s["targets"], s["stops"], s["flat_by"], money(s["net_pnl"]), s["avg_r"] or 0,
        money(s["worst_trade"]["pnl"]), money(s["deepest_open_loss"]["loss"]))
    versus = ""
    if run["key"] != "S01":
        changed = run["changed"]
        swing = max(changed, key=lambda c: abs(c[3])) if changed else None
        names = {"T": "a target", "S": "a stop", "F": "a 15:55 exit", "B": "a block", "X": "a skip", "N": "no fill",
                 "": "no trade"}
        versus = " Against the baseline: %d session%s came out differently, %s in all." % (
            len(changed), "" if len(changed) == 1 else "s", money(s["net_pnl"] - base["net_pnl"]))
        if swing:
            what = ("%s became %s" % (names[swing[1]], names[swing[2]]) if swing[1] != swing[2]
                    else "%s, for a different amount" % names[swing[1]])
            versus += " Biggest swing: %s, %s (%s)." % (short_date(swing[0]), what, money(swing[3]))
    return ('<div class="case"><h3>%s %s</h3><div class="choice">%s</div><div class="line">%s</div><p>%s%s</p></div>'
            % (run["key"], esc(SHORT[run["key"]]), esc(run["scenario"]["choice"]), esc(line),
               esc(NOTES[run["key"]].format(**run["fields"])), esc(versus)))


def pages_cases(runs):
    base = runs[0]["summary"]
    cases = [case_html(run, base) for run in runs]
    return """
<section>
<h2>What happened in each case</h2>
%s
</section>
<section>
%s
</section>""" % ("".join(cases[:7]), "".join(cases[7:]))


def pages_losses(runs, chart_dots, chart_paths, instances):
    body = ""
    for run in runs:
        s = run["summary"]
        deep = s["deepest_open_loss"]
        extra = run["extra_risk"]
        extra_amount = -extra["loss_at_stop_after_fill"] - extra["gate_max_loss"]
        body += ("<tr%s><td>%s %s</td><td class='n'>$%d</td><td class='n'>%s</td><td class='n'>%s %s</td>"
                 "<td class='n'>%.0f %%</td><td class='n'>%s</td><td class='n'>%s</td><td class='n'>%d</td>"
                 "<td class='n'>%s</td><td class='n'>%s</td><td class='n'>%d</td></tr>") % (
            ' class="base"' if run["key"] == "S01" else "", run["key"], esc(SHORT[run["key"]]), run["limit"],
            money(deep["loss"]), short_date(deep["date"]), deep["time"], 100 * -deep["loss"] / run["limit"],
            money(s["worst_trade"]["pnl"]),
            "%s (%s)" % (money(extra_amount, sign=False), short_date(extra["date"])) if extra_amount > 0.005 else "none",
            s["losses_over_gate_max_loss"], money(-s["max_drawdown"]["amount"]),
            "%s to %s" % (short_date(s["max_drawdown"]["from"]) if s["max_drawdown"]["from"] != "the start"
                          else "start", short_date(s["max_drawdown"]["to"])), s["longest_losing_streak"])
    titles = "".join("<li><b>%s</b> (%s %s, %s): %s</li>" % (esc(title), run["key"], esc(SHORT[run["key"]]),
                                                              short_date(row["date"]), esc(note))
                     for title, run, row, note in instances)
    return """
<section>
<h2>Maximum losses incurred at once</h2>
<p>A trade's loss is not one number. Four ways to measure it, from the single worst moment to a bad stretch:</p>
<ol>
<li><b>Deepest open loss</b>: the most an open position was down at any one moment, marked at each 5-minute bar's
low. This is the loss at once. It counts even when the trade later recovered.</li>
<li><b>Worst trade</b>: the largest loss once a trade was closed.</li>
<li><b>Risk beyond the check's figure</b>: check.py approves a max loss at the plan's entry price. A market order then
fills at the next price, sometimes higher, and a stop at the planned price would lose more than the check approved.
Shown as the largest such excess, and the count of closed losses that went past the check's figure.</li>
<li><b>Drawdown and losing streak</b>: the largest fall of the account from a high to a later low, and the most losing
trades in a row.</li>
</ol>
<table class="tight"><tr><th>Scenario</th><th class="n">Limit</th><th class="n">Deepest<br>open loss</th>
<th class="n">When</th><th class="n">Of the<br>limit</th><th class="n">Worst<br>trade</th>
<th class="n">Most risk beyond<br>the check (day)</th><th class="n">Losses<br>past it</th><th class="n">Max<br>drawdown</th>
<th class="n">Drawdown<br>from, to</th><th class="n">Losing<br>streak</th></tr>%(body)s</table>
<div class="fig">%(dots)s</div>
</section>
<section>
<h3>What those numbers say</h3>
<ul>
<li><b>No loss went past $50.</b> In the twelve $50 scenarios the deepest any position went was %(d50)s,
%(d50_pct)s of the limit: the sizing works from the stop, and %(gap_text_lower)s %(over_text)s</li>
<li><b>The deepest moment was usually the stop-out itself.</b> Because the stop is the floor of the trade, the worst
open loss and the worst trade are the same trade in %(same_worst)d of 13 scenarios. Winners dipped too: the deepest
dip of a trade that still ended in profit was %(win_dip)s.</li>
<li><b>Market fills carried a little more risk than the check approved.</b> The market order fills at the next price,
which in the baseline averaged %(slip)s a share above the plan's entry. That raised the risk at the stop by up to
%(extra_max)s on one trade (%(max_key)s, %(max_date)s: a fill %(max_slip)s above the plan). Closed losses went past the
check's figure by %(over_low)s to %(over_high)s cents. The limit-at-cap entry (S07) had none: it never fills above the
plan's price.</li>
<li><b>Raising the limit to $100 raised the worst moment.</b> S11's deepest open loss was %(d100)s, more than any $50
scenario, although the position cap kept the size at 9 or 10 shares.</li>
<li><b>Losses come in runs.</b> The drawdowns (%(dd_low)s to %(dd_high)s) are two to four times one trade's limit,
spread over days. The $150 daily loss limit counts one day at a time, so it never stops a run like this: with one
trade a day it was never reached.</li>
</ul>
<h3>Four of those moments, bar by bar</h3>
<div class="fig">%(paths)s</div>
<p class="cap">The blue line is each bar's close and the pale band its low-to-high range, from the fill to the exit.
The red dot is the deepest moment. Dashed lines: the target (green) and the stop (red); the solid gray line is the
fill.</p>
<ul class="small">%(titles)s</ul>
</section>""" % instances_facts(runs, chart_paths, titles, body, chart_dots)


def instances_facts(runs, chart_paths, titles, body, chart_dots):
    fifty = [run for run in runs if run["limit"] == 50]
    deepest = min(fifty, key=lambda r: r["summary"]["deepest_open_loss"]["loss"])["summary"]["deepest_open_loss"]
    winners = [row for run in runs for row in run["trades"] if row["pnl"] > 0]
    dd = [run["summary"]["max_drawdown"]["amount"] for run in runs]
    trades = [row for run in runs for row in run["trades"]]
    return dict(body=body, dots=chart_dots, over_text=EXTRA["over_text"],
                gap_text_lower="no bar in these sessions opened below a stop, so no stop filled worse than its price."
                if EXTRA["gaps"] == 0 else "%d stops filled below their price on a gap." % EXTRA["gaps"],
                risk50=EXTRA["risk50"], max_key=EXTRA["max_key"],
                max_date=EXTRA["max_date"], max_slip=EXTRA["max_slip"], over_low=EXTRA["over_low"],
                over_high=EXTRA["over_high"],
                gap_text="No bar in these sessions opened below a stop, so no stop filled worse than its price."
                if EXTRA["gaps"] == 0 else "%d stops filled below their price on a gap." % EXTRA["gaps"],
                d50=money(deepest["loss"]), d50_pct="%.0f %%" % (100 * -deepest["loss"] / 50),
                same_worst=sum(1 for run in runs if run["summary"]["worst_trade"]["date"]
                               == run["summary"]["deepest_open_loss"]["date"]),
                win_dip=money(min(row["max_open_loss"] for row in winners)),
                slip="%.1f cents" % (100 * np.mean([row["fill_minus_plan"] for row in runs[0]["trades"]])),
                extra_max=money(max(-row["loss_at_stop_after_fill"] - row["gate_max_loss"] for row in trades),
                                sign=False),
                d100=money(runs[10]["summary"]["deepest_open_loss"]["loss"]), dd_low=money(min(dd), sign=False),
                dd_high=money(max(dd), sign=False), paths=chart_paths, titles=titles)


def page_time_machine(runs):
    body = ""
    for run in runs:
        s = run["summary"]
        hit = s["targets"] + s["stops"]
        body += ("<tr%s><td>%s %s</td><td class='n'>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
                 "<td class='n'>%d of %d</td><td class='n'>%s</td></tr>") % (
            ' class="base"' if run["key"] == "S01" else "", run["key"], esc(SHORT[run["key"]]),
            pct(s["tm_avg_p_target"]), pct(s["tm_avg_p_stop"]),
            pct(s["tm_avg_p_target"] / (s["tm_avg_p_target"] + s["tm_avg_p_stop"])) if s["tm_avg_p_target"] else "",
            s["targets"], hit, pct(s["actual_target_share"]))
    return """
<section>
<h2>The Time Machine's forecasts against what happened</h2>
<p>For every trade, the Time Machine replayed the rest of the session 10,000 times from TSLA's own earlier sessions
and gave the odds of the target first, the stop first, or neither by the close. Averaged over each scenario's trades,
next to what the trades did:</p>
<table class="tight"><tr><th>Scenario</th><th class="n">Forecast: target first</th><th class="n">Stop first</th>
<th class="n">Target share of the two</th><th class="n">Trades that hit target, of those that hit one</th>
<th class="n">Actual target share</th></tr>%(body)s</table>
<ul>
<li>For the 1R scenarios with five or more trades that hit a side (all but %(skipped)s), the forecast's target share
sat between %(share_low)s and %(share_high)s; what happened was %(actual_low)s to %(actual_high)s. Twenty trades or
fewer per scenario cannot confirm or refute a forecast that close to a coin flip.</li>
<li>The forecast moved with the rule: a farther target lowered the forecast odds of reaching it, from %(f01)s in the
baseline to %(f02)s (S02, range-low stop), %(f05)s (1.5R) and %(f06)s (2R), and the targets actually reached fell
with it, from %(t01)d to %(t02)d, %(t05)d and %(t06)d.</li>
<li>As a filter (S13), the Time Machine had stop-first ahead on only two of the 19 signals, and both of those trades
were stops in the baseline. It is one way to use the forecast, not proof that it picks trades.</li>
</ul>
<p class="small">The Time Machine draws its replays only from sessions before the trade (its pool grew from
%(pool_low)d to %(pool_high)d sessions over the run), so it never saw the day it was forecasting.</p>
</section>""" % dict(body=body, skipped=EXTRA["one_r_skipped"], share_low=EXTRA["share_low"],
                     share_high=EXTRA["share_high"], actual_low=EXTRA["actual_low"], actual_high=EXTRA["actual_high"],
                     pool_low=EXTRA["pool_low"], pool_high=EXTRA["pool_high"],
                     **{"f" + k[1:]: pct(s["tm_avg_p_target"]) for k, s in
                        ((run["key"], run["summary"]) for run in runs) if k in ("S01", "S02", "S05", "S06")},
                     **{"t" + k[1:]: s["targets"] for k, s in
                        ((run["key"], run["summary"]) for run in runs) if k in ("S01", "S02", "S05", "S06")})


def page_findings():
    return """
<section>
<h2>Things the runs turned up in the tools</h2>
<ol>
<li><b>size.py and check.py measure the 75 % position cap differently.</b> size.py takes 75 % of the $5,000 budget
($3,750); check.py takes 75 % of the account, which after losses was $4,930.52 ($3,697.89). In S11, size.py proposed
10 shares twice (about $3,713 and $3,741), and check.py blocked both (2 Oct and 5 Oct). In real use you would see a BLOCK on an
order the budget tool had just written. Not changed: tell me if you want the two to agree.</li>
<li><b>A market fill above the plan's price is risk the check did not approve.</b> check.py approves the max loss at
the plan's entry (or a fresh price); the fill can be higher. Here it added up to %(extra)s of risk on one trade, and
%(over)s. A fast market makes the gap bigger. <code>size.py --limit cap</code> (S07) bounds it, and on these days every such limit filled
within five minutes.</li>
<li><b>An EXPIRED entry watch leaves yesterday's entry.json in place.</b> The watch writes the file only on a trigger,
so exit.py plan goes on to plan the old entry with only a note ("This plan is for 2026-08-03, not today"). size.py
then refuses the old plan ("make a new plan with trader-exit"), so no order can come of it; it costs a confusing step.
The runs here clear the folder each morning. Not changed.</li>
<li><b>size.py's report names "trader/portfolio.json"</b> as the source of the cash even when <code>--portfolio</code>
or TRADER_HOME points somewhere else. Cosmetic. Not changed.</li>
<li><b>Fixed: the tests and TRADER_HOME.</b> With TRADER_HOME set, 12 tests failed and the tests wrote
<code>entry.json</code>, <code>plan.json</code> and <code>timemachine.json</code> into that folder, which would have
overwritten a real trader folder. <code>tests/_load.py</code> now clears it for the test run; 384 tests pass either
way.</li>
</ol>

<h2 style="margin-top:14pt">Method and limits</h2>
<ul>
<li><b>One stock, 39 sessions.</b> TSLA from 24 Aug to 16 Oct 2026; the scanner was not part of the runs, because
the repo has a market-wide scan for only one morning (19 Oct). The days and the stock are the README example's.</li>
<li><b>One trade a day at most.</b> The opening-range rule signals once a session, so the daily limits (3 trades, $150,
10 orders) were never reached and concurrent positions never happened.</li>
<li><b>Simulated fills.</b> From 5-minute bars: no fees, no spread, no partial fills, and the stop is assumed first
when a bar reaches both legs (%(both)s). Real fills, especially market orders and stops in a fast market, can be
worse.</li>
<li><b>Practice clock.</b> Every check.py and ticket.py run was a practice run (pinned clock); their verdicts would
never count for a real order, by design.</li>
<li><b>Reproducible.</b> <code>python -B paper-trading-scenarios/run_scenarios.py</code> reruns all 13 in about 3
minutes with the same numbers; <code>make_report.py</code> rebuilds this PDF. Every command and its full output is
in each scenario's <code>run.log</code>, every session in <code>trades.csv</code>, and every tool file of every
trading day under <code>days/</code>.</li>
</ul>
<p class="small" style="margin-top:10pt">Education and tooling, not financial advice.</p>
</section>""".replace("%(extra)s", EXTRA["max"]).replace("%(over)s", EXTRA["over_finding"]).replace(
        "%(both)s", "it never happened here" if not EXTRA["both"] else "%d times here" % EXTRA["both"])


def pick_instances(runs):
    """Four moments for the bar-by-bar chart: (title, run, row, note)."""
    fifty = [run for run in runs if run["limit"] == 50]
    deep_run = min(fifty, key=lambda r: r["summary"]["deepest_open_loss"]["loss"])
    deep_row = next(row for row in deep_run["trades"] if row["date"] == deep_run["summary"]["deepest_open_loss"]["date"])
    s11 = runs[10]
    s11_row = next(row for row in s11["trades"] if row["date"] == s11["summary"]["deepest_open_loss"]["date"])
    winner_run, winner_row = min(((run, row) for run in runs for row in run["trades"] if row["pnl"] > 0),
                                 key=lambda pair: pair[1]["max_open_loss"])
    extra_run, extra_row = max(((run, row) for run in runs for row in run["trades"]),
                               key=lambda pair: -pair[1]["loss_at_stop_after_fill"] - pair[1]["gate_max_loss"])
    return [
        ("Deepest, $50 limit", deep_run, deep_row,
         "the deepest open loss of the twelve $50 scenarios, %s, at the stop-out" % money(deep_row["max_open_loss"])),
        ("Deepest, $100 limit", s11, s11_row, "%s at once with the doubled limit" % money(s11_row["max_open_loss"])),
        ("Deep dip, still a winner", winner_run, winner_row,
         "down %s at one point before it ended %s" % (money(-winner_row["max_open_loss"], sign=False),
                                                      money(winner_row["pnl"]))),
        ("Fill above the plan", extra_run, extra_row,
         "filled %.2f over the plan's entry, so the stop risked %s against the check's %s; it hit the target" % (
             extra_row["fill_minus_plan"], money(-extra_row["loss_at_stop_after_fill"], sign=False),
             money(extra_row["gate_max_loss"], sign=False))),
    ]


def print_pdf(html_path):
    # Edge on Windows; REPORT_BROWSER names another Chromium-family browser (Chrome, Chromium) anywhere else
    browser = os.environ.get("REPORT_BROWSER") or next((path for path in EDGE if os.path.exists(path)), None)
    if not browser or not os.path.exists(browser):
        raise SystemExit("Microsoft Edge not found: open %s in a browser and print it to PDF, or set REPORT_BROWSER "
                         "to a Chrome or Chromium program" % html_path)
    profile = tempfile.mkdtemp(prefix="edge-print-")
    sandbox = [] if os.name == "nt" else ["--no-sandbox"]     # Chromium as root on Linux needs it
    # Edge prints with --headless=new; Chrome for Testing in a Linux container hangs on it, so another browser
    # gets the plain flag
    headless = "--headless" if os.environ.get("REPORT_BROWSER") else "--headless=new"
    try:
        subprocess.run([browser, headless, "--disable-gpu", "--no-first-run", "--no-pdf-header-footer"]
                       + sandbox + ["--user-data-dir=" + profile, "--print-to-pdf=" + PDF,
                                    "file:///" + html_path.replace("\\", "/").lstrip("/")],
                       check=True, capture_output=True, timeout=180)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def main(argv):
    build = argv[argv.index("--build") + 1] if "--build" in argv else tempfile.mkdtemp(prefix="paper-report-")
    os.makedirs(build, exist_ok=True)
    spec, runs = load()
    facts(runs)
    style()
    sessions = spec["sessions"]
    instances = pick_instances(runs)
    page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Paper trading comparison</title>"
            "<style>%s</style></head><body>" % CSS)
    page += page_summary(spec, runs)
    page += page_setup(spec)
    page += page_scenarios(runs)
    page += page_results(runs, chart_net(runs))
    page += page_grid(runs, chart_grid(runs, sessions))
    page += page_equity(runs, chart_equity(runs, sessions))
    page += pages_cases(runs)
    page += pages_losses(runs, chart_open_losses(runs), chart_instances(runs, [i[:3] for i in instances]), instances)
    page += page_time_machine(runs)
    page += page_findings()
    page += "</body></html>"
    html_path = os.path.join(build, "paper-trading-comparison.html")
    with open(html_path, "w", encoding="utf-8") as file:
        file.write(page)
    print_pdf(os.path.abspath(html_path))
    if "--build" not in argv:
        shutil.rmtree(build, ignore_errors=True)
    print("Wrote %s (%d KB)" % (PDF, os.path.getsize(PDF) // 1024))


if __name__ == "__main__":
    main(sys.argv[1:])
