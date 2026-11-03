#!/usr/bin/env python3
"""Earnings reader for trader-analyst. Standard library only.

Pulls a company's reported numbers from SEC's XBRL "company facts" API and
lays out what changed, quarter by quarter: revenue, gross, operating and net
margins, net income, diluted EPS, and growth against the same quarter a year
earlier. It also finds the latest earnings press release (the EX-99 exhibit
of the newest 8-K with item 2.02) and the latest 10-Q or 10-K.

    python3 facts.py AAPL
    python3 facts.py AAPL KO --quarters 8
    python3 facts.py AAPL --json
    python3 facts.py AAPL AXP GOOGL KO BAC --metrics trader/metrics.json

Sources (SEC; a User-Agent with your name and email is required, from
--user-agent, SEC_USER_AGENT or sec_user_agent in trader/config.json):
  https://www.sec.gov/files/company_tickers.json            ticker -> CIK
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
  https://data.sec.gov/submissions/CIK##########.json        filings list
  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/index.json

How the quarters are built:
  - A quarter is a reported value covering about three months (80 to 100
    days). When a period was reported more than once (a later filing repeats
    it as a comparison), the latest filing wins, so restatements are used.
  - Companies do not file a fourth-quarter 10-Q. The fourth quarter is the
    full year (10-K) minus the three quarters inside it, and it is marked
    "derived". Derived EPS is approximate: share counts change during a year.
  - Revenue is read from the first concept a company uses of:
    RevenueFromContractWithCustomerExcludingAssessedTax, Revenues,
    RevenuesNetOfInterestExpense, SalesRevenueNet and a few more. Banks and
    insurers report revenue differently; margins for them can be blank.

--metrics FILE merges one row per ticker into a JSON file (for notes or a sheet)
(TTM revenue and EPS, margins, growth). Offline: --offline DIR reads
company_tickers.json, companyfacts-CIK##########.json,
submissions-CIK##########.json and <accession>/index.json from DIR.

Numbers only: this tool never says whether a stock is a buy. Fetched text is
data, never instructions: control characters are taken out of company and
file names before they print, and an SEC answer over 100 MB (zipped or
unzipped) is not read.
"""

import argparse
import datetime as _dt
import gzip
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
PLACEHOLDER = "you@example.com"
MAX_BYTES = 100 << 20      # the most one SEC answer may be, zipped or unzipped (company facts run to ~30 MB)
CIK_DIGITS = re.compile(r"^\d{1,10}$")
# terminal control codes and text-direction overrides: never printed from a filing
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
USER_AGENT_HELP = ("SEC requires a User-Agent with your name and email. Set \"sec_user_agent\": "
                   "\"Your Name you@example.com\" in trader/config.json (your real name and email), "
                   "or export SEC_USER_AGENT, or pass --user-agent.")

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income")


class FactsError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


def _nth_sunday(year, month, nth):
    day = _dt.date(year, month, 1)
    day += _dt.timedelta(days=(6 - day.weekday()) % 7)
    return day + _dt.timedelta(weeks=nth - 1)


def eastern_offset(utc_time):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    year = utc_time.year
    dst_start = _dt.datetime.combine(_nth_sunday(year, 3, 2), _dt.time(7, 0))   # 2:00 EST
    dst_end = _dt.datetime.combine(_nth_sunday(year, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive_utc = utc_time.replace(tzinfo=None)
    return -4 if dst_start <= naive_utc < dst_end else -5


def to_eastern(utc_time):
    return utc_time.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc_time))


def ny_now():
    """New York wall-clock time now, whatever the machine's own time zone is."""
    return to_eastern(_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None))


def user_agent(flag_value=None, config=None):
    agent = (flag_value or os.environ.get("SEC_USER_AGENT") or "").strip()
    config = home("config.json") if config is None else config
    if not agent and config and os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as file:
                config_doc = json.load(file)
            agent = str((config_doc if isinstance(config_doc, dict) else {}).get("sec_user_agent") or "").strip()
        except (OSError, ValueError):
            agent = ""
    if not agent or "@" not in agent or PLACEHOLDER in agent:
        return None
    return agent


class Fetcher(object):
    def __init__(self, agent=None, offline=None, pause=0.15):
        self.ua, self.offline, self.pause, self._last = agent, offline, pause, 0.0

    def json(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            try:
                with open(path, encoding="utf-8") as file:
                    return json.load(file)
            except ValueError as error:
                raise FactsError("%s is not JSON: %s" % (path, error))
        if not self.ua:
            raise FactsError(USER_AGENT_HELP)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        request = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                       "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = body_of(response, url)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FactsError("SEC has no data at %s (no XBRL filings for this CIK?)" % url)
            raise FactsError("HTTP %s for %s" % (error.code, url))
        except urllib.error.URLError as error:
            raise FactsError("could not reach %s: %s" % (url, error.reason))
        except (OSError, http.client.HTTPException) as error:     # a timeout or a reset mid-answer
            raise FactsError("lost the answer from %s: %s" % (url, str(error) or type(error).__name__))
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError as error:
            raise FactsError("SEC's answer from %s is not JSON: %s" % (url, error))


def body_of(response, url, limit=MAX_BYTES):
    """The answer's bytes, unzipped if it came gzipped; never more than limit either way."""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise FactsError("%s sent more than %d MB; not reading it" % (url, limit >> 20))
    if response.headers.get("Content-Encoding") == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as unzipped:
                data = unzipped.read(limit + 1)
        except (OSError, EOFError) as error:
            raise FactsError("%s sent a broken gzip answer: %s" % (url, error))
        if len(data) > limit:
            raise FactsError("%s unzips to more than %d MB; not reading it" % (url, limit >> 20))
    return data


def clean(text):
    """Fetched text, safe to print: no control codes (terminal escapes) or
    direction overrides, and each run of whitespace one space."""
    return " ".join(CONTROL.sub(" ", text or "").split())


def doc_url(folder, name):
    """A file in a filing's folder; the name comes from SEC's JSON, so it is encoded."""
    return folder + urllib.parse.quote(clean(name), safe="/")


# ---------------------------------------------------------------- facts

def parse_date(text):
    return _dt.date.fromisoformat(text)


def facts_for(companyfacts, concept, unit_hint=None):
    """Raw facts for one us-gaap concept (USD, or USD/shares for EPS)."""
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    unit = unit_hint if unit_hint in units else next(iter(units), None)
    return units.get(unit, []) if unit else []


def periods(facts):
    """{(start, end): fact} for duration facts from 10-Q/10-K.

    The value comes from the latest filing (restatements win). The fiscal
    label (fy, fp) comes from the first filing that reported the period: a
    later 10-Q that repeats it as a comparison carries its own fy and fp."""
    latest, first = {}, {}
    for fact in facts:
        if "start" not in fact or fact.get("form", "").split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        key = (fact["start"], fact["end"])
        stamp = (fact.get("filed", ""), fact.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = fact
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = fact
    out = {}
    for key, fact in latest.items():
        row = dict(fact)
        row["fy"], row["fp"] = first[key].get("fy"), first[key].get("fp")
        row["first_filed"] = first[key].get("filed")
        out[key] = row
    return out


def quarterly(facts, derive_q4=True):
    """{end: {"value", "start", "end", "derived", "fy", "fp", "form"}} three-month values."""
    per = periods(facts)
    quarters = {}
    years = []
    for (start, end), fact in per.items():
        days = (parse_date(end) - parse_date(start)).days
        if 80 <= days <= 100:
            quarters[end] = {"value": fact["val"], "start": start, "end": end, "derived": False,
                             "fy": fact.get("fy"), "fp": fact.get("fp"), "form": fact.get("form"),
                             "filed": fact.get("filed")}
        elif 350 <= days <= 380:
            years.append((start, end, fact))
    if derive_q4:
        for start, end, fact in years:
            if end in quarters:
                continue
            inside = [quarter for quarter in quarters.values() if quarter["start"] >= start and quarter["end"] < end]
            inside.sort(key=lambda quarter: quarter["end"])
            if len(inside) != 3:
                continue
            last_q_start = parse_date(inside[-1]["end"]) + _dt.timedelta(days=1)
            quarters[end] = {"value": fact["val"] - sum(quarter["value"] for quarter in inside),
                             "start": last_q_start.isoformat(), "end": end, "derived": True,
                             "fy": fact.get("fy"), "fp": "Q4", "form": fact.get("form"),
                             "filed": fact.get("filed")}
    return quarters


def series(companyfacts, key, derive_q4=True):
    """Quarterly series for a metric, trying its concepts in order and filling gaps."""
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    merged, used = {}, []
    for concept in CONCEPTS[key]:
        quarters = quarterly(facts_for(companyfacts, concept, unit), derive_q4)
        if not quarters:
            continue
        added = False
        for end, quarter in quarters.items():
            if end not in merged:
                merged[end] = dict(quarter, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def year_ago_value(by_end, end, days=365, slack=10):
    target = parse_date(end) - _dt.timedelta(days=days)
    best = None
    for end_date in by_end:
        gap = abs((parse_date(end_date) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, end_date)
    return by_end[best[1]] if best else None


def percent_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return round(100.0 * (current - previous) / abs(previous), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(companyfacts, quarters=6, name=None):
    """Every number in the report, as a dict."""
    by_metric = {}
    concepts = {}
    for key in CONCEPTS:
        by_metric[key], concepts[key] = series(companyfacts, key, derive_q4=True)
    revenue = by_metric["revenue"]
    if not revenue and not by_metric["net_income"]:
        raise FactsError("no quarterly revenue or net income in the XBRL facts")
    ends = sorted(set(revenue) | set(by_metric["net_income"]), reverse=True)[:quarters]
    rows = []
    for end in ends:
        revenue_now = revenue.get(end, {}).get("value")
        gross_profit = by_metric["gross_profit"].get(end, {}).get("value")
        if gross_profit is None and revenue_now is not None and end in by_metric["cost_of_revenue"]:
            gross_profit = revenue_now - by_metric["cost_of_revenue"][end]["value"]
        operating_income = by_metric["operating_income"].get(end, {}).get("value")
        net_income = by_metric["net_income"].get(end, {}).get("value")
        eps = by_metric["eps_diluted"].get(end, {}).get("value")
        prior = {key: year_ago_value(by_metric[key], end) for key in ("revenue", "net_income", "eps_diluted")}
        base = revenue.get(end) or by_metric["net_income"].get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "derived": any(by_metric[key].get(end, {}).get("derived") for key in ("revenue", "net_income")),
            "revenue": revenue_now, "gross_profit": gross_profit, "operating_income": operating_income,
                     "net_income": net_income,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gross_profit, revenue_now),
                     "operating_margin": margin(operating_income, revenue_now),
            "net_margin": margin(net_income, revenue_now),
            "revenue_yoy": percent_change(revenue_now, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": percent_change(net_income, (prior["net_income"] or {}).get("value")),
            "eps_yoy": percent_change(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": base.get("filed"), "form": base.get("form"),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(by_metric[key])[-4:]
        if len(last4) == 4 and (parse_date(last4[-1]) - parse_date(last4[0])).days < 300:
            ttm[key] = sum(by_metric[key][end_date]["value"] for end_date in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": clean(name or companyfacts.get("entityName")) or None,
            "cik": str(companyfacts.get("cik", "")).zfill(10),
            "rows": rows, "ttm": ttm, "concepts": concepts}


def stale_note(result, filings):
    """A note when SEC's facts lag a report that is already filed."""
    report = (filings or {}).get("report")
    if not report or not result["rows"] or not report.get("period"):
        return None
    latest = result["rows"][0]["end"]
    if report["period"] > latest and (parse_date(report["period"]) - parse_date(latest)).days > 20:
        return ("SEC's XBRL facts stop at %s, but a %s for the period ending %s was filed %s. "
                "Read it directly: %s" % (latest, report["form"], report["period"], report["filed"],
                                         report["url"]))
    return None


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    latest = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []

    def dollars(value):
        return "n/a" if value is None else "$%.2fB" % (value / 1e9) if abs(value) >= 1e9 else "$%.1fM" % (value / 1e6)

    label = "Quarter ending %s%s" % (latest["end"], " (derived from the 10-K)" if latest["derived"] else "")
    line = "%s: revenue %s" % (label, dollars(latest["revenue"]))
    if latest["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % latest["revenue_yoy"]
    line += ", net income %s" % dollars(latest["net_income"])
    if latest["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % latest["net_income_yoy"]
    if latest["eps_diluted"] is not None:
        line += ", diluted EPS $%.2f" % latest["eps_diluted"]
        if latest["eps_yoy"] is not None:
            line += " (%+.1f%%)" % latest["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if latest[key] is None:
            continue
        text = "%s %.1f%%" % (name, latest[key])
        if prev and prev[key] is not None:
            text += " (%+.1f points vs the quarter before)" % (latest[key] - prev[key])
        out.append(text + ".")
    if latest["net_income"] is not None and latest["operating_income"] is not None \
            and latest["net_income"] > latest["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the "
                   "business (investments, tax items). Check the 10-Q before reading it as growth.")
    if prev and latest["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % percent_change(latest["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- filings

EXHIBIT_RANK = [r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", r"ex(?:hibit)?[-_]?99",
                r"earnings|release|press", r"ex(?:hibit)?[-_]?\d"]


def exhibit_name(names, primary=None):
    """The press release among a filing's files (EX-99.1 by its usual names)."""
    docs = [name for name in names if name.lower().endswith((".htm", ".html", ".txt", ".pdf"))
            and name != primary and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6})", name)]
    for pattern in EXHIBIT_RANK:
        hits = sorted(name for name in docs if re.search(pattern, name, re.I))
        if hits:
            return hits[0]
    return None


def latest_filings(submissions, cik, fetcher=None):
    """Newest 10-Q/10-K and newest earnings 8-K (item 2.02) with its EX-99 exhibit."""
    recent = submissions.get("filings", {}).get("recent", {})
    out = {"report": None, "earnings_8k": None}
    base_cik = int(cik)
    for index, form in enumerate(recent.get("form", [])):
        accession = recent["accessionNumber"][index]
        folder = ARCHIVE_URL % (base_cik, accession.replace("-", ""))
        if out["report"] is None and form in ("10-Q", "10-K", "20-F", "40-F"):
            out["report"] = {"form": form, "filed": recent["filingDate"][index],
                             "period": recent["reportDate"][index], "accession": accession,
                             "url": doc_url(folder, recent["primaryDocument"][index])}
        if (out["earnings_8k"] is None and form == "8-K"
                and "2.02" in (recent.get("items", [""] * (index + 1))[index] or "")):
            entry = {"form": form, "filed": recent["filingDate"][index], "accession": accession,
                     "url": doc_url(folder, recent["primaryDocument"][index]), "exhibit": None,
                     "index": doc_url(folder, "%s-index.htm" % accession)}
            if fetcher is not None:
                try:
                    index_doc = fetcher.json(folder + "index.json",
                                             os.path.join(accession.replace("-", ""), "index.json"))
                    names = [item["name"] for item in index_doc.get("directory", {}).get("item", [])]
                    pick = exhibit_name(names, recent["primaryDocument"][index])
                    if pick:
                        entry["exhibit"] = doc_url(folder, pick)
                except FactsError:
                    pass
            out["earnings_8k"] = entry
        if out["report"] and out["earnings_8k"]:
            break
    return out


# ---------------------------------------------------------------- output

def millions_text(value):
    if value is None:
        return "-"
    return "{:,.0f}".format(value / 1e6)


def signed_percent_text(value):
    return "-" if value is None else "%+.1f%%" % value if value < 0 or value > 0 else "0.0%"


def percent_text(value):
    return "-" if value is None else "%.1f%%" % value


def render(ticker, result, filings):
    out = ["%s (%s), CIK %s. US$ millions except EPS. Source: SEC XBRL company facts." % (
        result["entity"], ticker, result["cik"]), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net inc", "EPS dil", "EPS yoy"))
    for row in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s%s" % (
            row["end"], row["fiscal"].strip()[:9], millions_text(row["revenue"]),
                                                                       signed_percent_text(row["revenue_yoy"]),
            percent_text(row["gross_margin"]), percent_text(row["operating_margin"]), percent_text(row["net_margin"]),
            millions_text(row["net_income"]), "-" if row["eps_diluted"] is None else "%.2f" % row["eps_diluted"],
            signed_percent_text(row["eps_yoy"]), "  derived" if row["derived"] else ""))
    ttm = result["ttm"]
    if ttm:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            "$" + millions_text(ttm.get("revenue")) + "M" if ttm.get("revenue") is not None else "-",
            "$" + millions_text(ttm.get("net_income")) + "M" if ttm.get("net_income") is not None else "-",
            "$%.2f" % ttm["eps_diluted"] if ttm.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filings:
        out.append("")
        report, earnings_8k = filings.get("report"), filings.get("earnings_8k")
        if report:
            out.append("Latest %s: period %s, filed %s  %s" % (report["form"], report["period"],
                                                              report["filed"], report["url"]))
        if earnings_8k:
            out.append("Latest earnings 8-K (item 2.02): filed %s  %s" % (
                earnings_8k["filed"], earnings_8k["exhibit"] or earnings_8k["index"]))
    note = stale_note(result, filings)
    if note:
        out.append("")
        out.append("NOTE: " + note)
    if any(row["derived"] for row in result["rows"]):
        out.append("")
        out.append("Derived quarters are the 10-K year minus the three 10-Qs; derived EPS is "
                   "approximate.")
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    rows = result["rows"]
    latest = rows[0] if rows else {}
    ttm = result["ttm"]
    return {"ticker": ticker, "name": result["entity"], "cik": result["cik"],
            "period_end": latest.get("end"), "revenue_ttm": ttm.get("revenue"),
            "net_income_ttm": ttm.get("net_income"), "eps_ttm": ttm.get("eps_diluted"),
            "revenue_growth_yoy": latest.get("revenue_yoy"), "eps_growth_yoy": latest.get("eps_yoy"),
            "net_income_growth_yoy": latest.get("net_income_yoy"),
            "gross_margin": latest.get("gross_margin"), "operating_margin": latest.get("operating_margin"),
            "net_margin": latest.get("net_margin"), "source": "SEC XBRL company facts"}


def clean_title(title):
    return re.sub(r"\s*/[A-Z]{2,5}/?\s*$", "", title or "").strip() or None


def ticker_ciks(fetcher):
    """ticker -> (10-digit CIK, company name) from company_tickers.json."""
    data = fetcher.json(TICKERS_URL, "company_tickers.json")
    out = {}
    try:
        for row in data.values():
            out.setdefault(row["ticker"].upper().replace("-", "."),
                           (str(int(row["cik_str"])).zfill(10), clean_title(row.get("title"))))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise FactsError("SEC's company_tickers.json is not the list it should be (%s)" % error)
    return out


def read_metrics(path):
    """{ticker: row} from an earlier --metrics file. One that is not this tool's
    {"companies": [...]} is an error, so it is never written over."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            doc = json.load(file)
        return {row["ticker"]: row for row in doc.get("companies", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise FactsError("%s is not a metrics file from this tool (%s); left as it is" % (path, error))


def write_json(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(doc, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tickers", nargs="*", help="tickers, e.g. AAPL KO")
    parser.add_argument("--cik", help="use this CIK instead of a ticker lookup (one company)")
    parser.add_argument("--quarters", type=int, default=6)
    parser.add_argument("--no-filings", action="store_true", help="skip the 8-K and 10-Q links")
    parser.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    parser.add_argument("--user-agent")
    parser.add_argument("--config", default=home("config.json"))
    parser.add_argument("--offline", metavar="DIR")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.tickers and not args.cik:
        parser.print_help()
        return 2
    if args.cik and not CIK_DIGITS.match(args.cik.strip()):
        print("facts: --cik wants SEC's number for the company, digits only, like 320193", file=sys.stderr)
        return 2
    agent = user_agent(args.user_agent, args.config)
    if not agent and not args.offline:
        print("facts: " + USER_AGENT_HELP, file=sys.stderr)
        return 2
    fetcher = Fetcher(agent, offline=args.offline)
    results, rows, failed = [], [], 0
    try:
        ciks = {} if args.cik else ticker_ciks(fetcher)
    except FactsError as error:
        print("facts: %s" % error, file=sys.stderr)
        return 2
    wanted = args.tickers or [args.cik]
    for ticker in wanted:
        tick = ticker.upper()
        if args.cik:
            cik, name = str(args.cik).zfill(10), None
        else:
            cik, name = ciks.get(tick.replace("-", "."), (None, None))
        if not cik:
            print("facts: no SEC CIK for %s (is it a US filer? ETFs and foreign filers "
                  "may not have XBRL facts)" % tick, file=sys.stderr)
            failed += 1
            continue
        try:
            company_facts = fetcher.json(FACTS_URL % cik, "companyfacts-CIK%s.json" % cik)
            result = analyse(company_facts, quarters=args.quarters, name=name)
            filings = None
            if not args.no_filings:
                submissions = fetcher.json(SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
                filings = latest_filings(submissions, cik, fetcher)
        except FactsError as error:
            print("facts: %s: %s" % (tick, error), file=sys.stderr)
            failed += 1
            continue
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
            print("facts: %s: SEC's data is not in the shape this tool reads (%s: %s)"
                  % (tick, type(error).__name__, error), file=sys.stderr)
            failed += 1
            continue
        results.append({"ticker": tick, "result": result, "filings": filings,
                        "changes": changes(result), "note": stale_note(result, filings)})
        rows.append(metrics_row(tick, result))
        if not args.json:
            sys.stdout.write(render(tick, result, filings))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        try:
            existing = read_metrics(args.metrics)
        except FactsError as error:
            print("facts: %s" % error, file=sys.stderr)
            return 1
        for row in rows:
            existing[row["ticker"]] = row
        write_json(args.metrics, {"as_of": ny_now().date().isoformat(),
                                  "companies": sorted(existing.values(), key=lambda row: row["ticker"])})
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
