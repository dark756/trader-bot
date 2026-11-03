"""Build feature-search.pdf: RESULTS.md as a report, with three charts.

    python -B paper-trading-scenarios/feature-search/make_report.py [--build DIR]

It runs write_results.py first, so the PDF's text is RESULTS.md's, word for word, and the two never disagree.
The charts come from results/. The look, the chart style and the printing are those of ../make_report.py:
matplotlib for the charts, then Microsoft Edge (headless), or the Chrome-family program that REPORT_BROWSER
names, prints the HTML to PDF. --build keeps the HTML in DIR.
"""

import csv
import html
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

import write_results

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
PDF = os.path.join(HERE, "feature-search.pdf")

_spec = importlib.util.spec_from_file_location("scenario_report", os.path.join(os.path.dirname(HERE), "make_report.py"))
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)
plt, matplotlib, np = report.plt, report.matplotlib, report.np

FIRST, SECOND = "#2a78d6", "#eb6834"      # categorical slots 1 and 2 (blue, orange), validated as a pair
FOOTER = "Feature search: 832 combinations of the bot's choices on 30 stocks"
NEW_PAGE = ("The picks, one per category (chosen in-sample, then tested out-of-sample)", "Each choice on its own",
            "Stress tests of the picks", "The Time Machine filter, tested wider", "What this means for the bot")
NUMBER = re.compile(r"^([+\-]?\$?[\d,]+(\.\d+)?( %)?|n/a|\d+ / \d+|\d+ of \d+"
                    r"|[+\-]\$[\d,]+\.\d+ vs [+\-]\$[\d,]+\.\d+)$")


# ---------------------------------------------------------------- the charts

def read_combos():
    rows = []
    for row in csv.DictReader(open(os.path.join(RESULTS, "combos.csv"), encoding="utf-8")):
        if row["setup"] == "always":
            continue
        for key in list(row):
            if key.startswith(("is_", "oos_", "all_")):
                row[key] = float(row[key]) if row[key] not in ("", None) else None
        rows.append(row)
    return rows


def legend(ax):
    """Above the plot, so it never sits on a mark."""
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=7, handletextpad=0.4,
              borderaxespad=0.3, columnspacing=1.6)


def chart_scatter(combos, summary):
    """Every combination's Sharpe ratio in-sample against out-of-sample."""
    winner = summary["picks"][0]["id"]
    fig, ax = plt.subplots(figsize=(7.0, 3.5))
    rest = [row for row in combos if row["id"] != winner]
    ax.scatter([row["is_sharpe"] for row in rest], [row["oos_sharpe"] for row in rest], s=9, color=FIRST,
               alpha=0.45, linewidth=0, label="one combination (%d in all)" % len(combos), zorder=2)
    top = next(row for row in combos if row["id"] == winner)
    ax.scatter([top["is_sharpe"]], [top["oos_sharpe"]], s=46, color=SECOND, edgecolor="white", linewidth=1.4,
               zorder=4, label="the in-sample winner")
    words = "in-sample winner: %s in, %s out" % (report.money(top["is_net"]), report.money(top["oos_net"]))
    ax.annotate(words.replace("$", r"\$"), (top["is_sharpe"], top["oos_sharpe"]), xytext=(-10, -20),
                textcoords="offset points", ha="right", fontsize=7, color=report.INK,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="none", alpha=0.92),
                arrowprops=dict(arrowstyle="-", color=report.INK_2, lw=0.6))
    ax.axhline(0, color=report.INK_2, lw=0.7, zorder=1)
    ax.axvline(0, color=report.INK_2, lw=0.7, zorder=1)
    ax.grid(color=report.GRID, lw=0.5)
    ax.set_axisbelow(True)
    ax.set_xlabel("Sharpe ratio in-sample (%s to %s)" % tuple(report.short_date(day) for day in summary["in_sample"]))
    ax.set_ylabel("Sharpe ratio out-of-sample\n(%s to %s)" % tuple(report.short_date(day)
                                                                  for day in summary["out_of_sample"]))
    ax.text(0.01, 0.98, "rank correlation %.2f" % summary["rank_correlation_sharpe"], transform=ax.transAxes,
            va="top", fontsize=7, color=report.INK_2)
    legend(ax)
    return report.svg(fig)


def dumbbell(rows, first_label, second_label, width=7.0):
    """rows: (label, first value, second value), top to bottom. Two markers per row joined by a line."""
    fig, ax = plt.subplots(figsize=(width, 0.19 * len(rows) + 0.7))
    y = np.arange(len(rows))[::-1]
    for row_y, (_, first, second) in zip(y, rows):
        ax.plot([first, second], [row_y, row_y], color=report.AXIS, lw=1.4, zorder=1, solid_capstyle="round")
    ax.scatter([row[1] for row in rows], y, s=30, color=FIRST, marker="o", edgecolor="white", linewidth=1.2,
               zorder=3, label=first_label)
    ax.scatter([row[2] for row in rows], y, s=30, color=SECOND, marker="s", edgecolor="white", linewidth=1.2,
               zorder=3, label=second_label)
    ax.axvline(0, color=report.INK_2, lw=0.7, zorder=0)
    ax.set_yticks(y)
    ax.set_yticklabels([row[0] for row in rows])
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.grid(axis="x", color=report.GRID, lw=0.5)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda value, _: "%+.2fR" % value if value else "0"))
    return fig, ax


def chart_features(combos):
    """Each choice's median R per trade in both halves."""
    groups = write_results.feature_groups(write_results.read_combos())
    order = ["setup", "stop", "target", "entry", "volume check", "entry window"]
    rows = []
    for feature in order:
        for (name, value) in sorted(key for key in groups if key[0] == feature):
            members = groups[(name, value)]
            rows.append(("%s: %s" % (feature, value), write_results.median(members, "is_avg_r"),
                         write_results.median(members, "oos_avg_r")))
    fig, ax = dumbbell(rows, "in-sample (first 39 sessions)", "out-of-sample (last 20)")
    ax.set_xlabel("Median R per trade, after costs, over every combination with that choice")
    legend(ax)
    return report.svg(fig)


def chart_filter(wide):
    """R per trade without and with the Time Machine filter, combo by combo."""
    rows = [(" ".join(item["id"].split()[:2]), item["without"]["avg_r"], item["with"]["avg_r"])
            for item in wide["combos"]]
    fig, ax = dumbbell(rows, "without the filter", "with the filter")
    ax.set_xlabel("R per trade after costs, %s to %s (each row: a setup and stop; 1R target, market, until 15:30)"
                  % (report.short_date(wide["from"]), report.short_date(wide["to"])))
    legend(ax)
    return report.svg(fig)


# ---------------------------------------------------------------- RESULTS.md -> HTML

def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def table_html(lines):
    cells = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    head, body = cells[0], cells[2:]
    numeric = [all(NUMBER.match(row[index]) for row in body if index < len(row)) for index in range(len(head))]
    lead = sum(len(row[0]) for row in body) / float(max(len(body), 1)) > 40      # long combination names
    out = ["<table%s><tr>" % (" class='lead'" if lead else "")]
    out += ["<th%s>%s</th>" % (" class='n'" if numeric[index] else "", inline(cell)) for index, cell in enumerate(head)]
    out.append("</tr>")
    for row in body:
        out.append("<tr>" + "".join("<td%s>%s</td>" % (" class='n'" if numeric[index] else "", inline(cell))
                                    for index, cell in enumerate(row)) + "</tr>")
    out.append("</table>")
    return "".join(out)


def to_html(markdown, charts, meta):
    """The small Markdown RESULTS.md uses: headings, a quote, lists two deep, tables, code blocks, ** and `."""
    out, depth, code, pending = [], 0, False, None
    lines = markdown.split("\n")
    index = 0

    def close_lists(target=0):
        nonlocal depth
        while depth > target:
            out.append("</li></ul>")
            depth -= 1

    out.append("<section>")
    while index < len(lines):
        line = lines[index]
        if code:
            if line.startswith("```"):
                out.append("</code></pre>")
                code = False
            else:
                out.append(html.escape(line) + "\n")
            index += 1
            continue
        if line.startswith("```"):
            close_lists()
            out.append("<pre><code>")
            code = True
        elif line.startswith("|"):
            close_lists()
            block = []
            while index < len(lines) and lines[index].startswith("|"):
                block.append(lines[index])
                index += 1
            out.append(table_html(block))
            if pending:
                out.append("<div class='fig'>%s</div><p class='cap'>%s</p>" % pending)
                pending = None
            continue
        elif line.startswith("# "):
            out.append("<h1>%s</h1><div class='meta'>%s</div>" % (inline(line[2:]), meta))
        elif line.startswith("## "):
            close_lists()
            title = line[3:]
            if title in NEW_PAGE:
                out.append("</section><section>")
            out.append("<h2>%s</h2>" % inline(title))
            pending = charts.get(title)
        elif line.startswith("> "):
            close_lists()
            out.append("<div class='box warn'><p>%s</p></div>" % inline(line[2:]))
        elif re.match(r"^( *)- ", line):
            level = len(re.match(r"^( *)", line).group(1)) // 2 + 1
            if level > depth:
                while depth < level:
                    out.append("<ul><li>")
                    depth += 1
            else:
                close_lists(level)
                out.append("</li><li>")
            out.append(inline(line.strip()[2:]))
        elif not line.strip():
            close_lists()
        else:
            close_lists()
            out.append("<p>%s</p>" % inline(line))
        index += 1
    close_lists()
    out.append("</section>")
    return "".join(out)


def main(argv):
    build = argv[argv.index("--build") + 1] if "--build" in argv else tempfile.mkdtemp(prefix="feature-report-")
    os.makedirs(build, exist_ok=True)
    write_results.main()
    summary = json.load(open(os.path.join(RESULTS, "picks.json"), encoding="utf-8"))
    combos = read_combos()
    report.style()
    charts = {
        "The picks, one per category (chosen in-sample, then tested out-of-sample)": (
            chart_scatter(combos, summary),
            "Each dot is one of the %d combinations: its Sharpe ratio in the first 39 sessions (across) against the "
            "last 20 (up). If doing well early meant doing well later, the dots would climb from left to right; "
            "they don't. The in-sample winner is near the right edge and below zero." % len(combos)),
        "Each choice on its own": (
            chart_features(combos),
            "Blue: the first 39 sessions; orange: the last 20. A choice that really helps sits to the right in both. "
            "Most rows swap order between the halves, and nearly every marker is left of zero."),
    }
    wide_path = os.path.join(RESULTS, "tm_filter.json")
    if os.path.exists(wide_path):
        wide = json.load(open(wide_path, encoding="utf-8"))
        charts["The Time Machine filter, tested wider"] = (
            chart_filter(wide),
            "Blue: every trade the rule found; orange: only those the Time Machine rated target-first. The orange "
            "marker is to the right in %d of %d rows, and still left of zero in most." % (wide["better_r"],
                                                                                          wide["count"]))
    meta = ("%d US stocks and ETFs, %d sessions (%s to %s), %d combinations. Every number comes from the files "
            "in <code>paper-trading-scenarios/feature-search/</code>."
            % (len(summary["symbols"]), len(summary["sessions"]), report.short_date(summary["sessions"][0]),
               report.short_date(summary["sessions"][-1]) + " 2026", summary["combos"]))
    with open(os.path.join(HERE, "RESULTS.md"), encoding="utf-8") as file:
        body = to_html(file.read(), charts, meta)
    css = report.CSS.replace("Paper trading the Day Trader skill: 13 scenarios", FOOTER) + """
pre { background: var(--wash); border-radius: 3pt; padding: 6pt 8pt; font-size: 7.2pt; white-space: pre-wrap; }
pre code { font-size: 7.2pt; }
table.lead th:first-child, table.lead td:first-child { width: 30%; }
th.n { white-space: normal; }
body { font-family: 'Segoe UI', Inter, system-ui, sans-serif; }
"""
    page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Feature search</title>"
            "<style>%s</style></head><body>%s</body></html>" % (css, body))
    html_path = os.path.join(build, "feature-search.html")
    with open(html_path, "w", encoding="utf-8") as file:
        file.write(page)
    report.PDF = PDF
    report.print_pdf(os.path.abspath(html_path))
    if "--build" not in argv:
        shutil.rmtree(build, ignore_errors=True)
    print("Wrote %s (%d KB)" % (PDF, os.path.getsize(PDF) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
