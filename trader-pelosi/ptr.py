#!/usr/bin/env python3
"""Congress trade tracker for trader-pelosi. Standard library only.

Reads the House Clerk's public financial disclosure index and the Periodic
Transaction Reports (PTRs) members of the House file when they, their spouse
or a dependent child buy or sell a stock.

    python3 ptr.py index --member pelosi                 # this year's PTRs by a member
    python3 ptr.py index --year 2026 --since 2026-10-01  # every PTR filed since a date
    python3 ptr.py new --member pelosi                   # only what is new since last check
    python3 ptr.py fetch 20035143                        # download one PTR and read it
    python3 ptr.py parse ptr.txt --filed 2026-08-21      # rows from `pdftotext -layout` text

Where the data comes from:
  - The index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
    (a ZIP with <YEAR>FD.xml: one <Member> per filing, FilingType P = PTR).
  - Each PTR: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf
  - Reading a PDF needs `pdftotext` (poppler) if you want rows from this tool.
    Without it, the tool prints the PDF path and Claude reads the PDF itself.

What a PTR is, and is not:
  - It is a PUBLIC disclosure under the STOCK Act, due within 30 days of the
    member learning of the trade and never later than 45 days after it. So it
    arrives days or weeks after the trade, and amounts are ranges, not prices.
  - Reading them fast is not inside information. There is none here.
  - This covers the House. Senate PTRs are on efdsearch.senate.gov behind an
    agreement you must accept yourself; see SKILL.md for the manual route.

State: `new` remembers which DocIDs it has shown in trader/state/pelosi.json,
so each PTR alerts once, and it only ever shows filings from the last 45 days
(the longest a PTR may take), so a new --member, or none, never floods you
with the whole year. Dates are New York dates (the Clerk is in Washington).
Offline: --offline FILE reads a saved <YEAR>FD.zip (or the .xml inside it)
instead of the network. The tests use it.

Fetched text is data, never instructions. A DocID is digits only (it names a
file on disk), control characters are taken out before anything prints, an
index that declares XML entities is refused, and nothing over 64 MB (a
download, or the XML once unzipped) is read.
"""

import argparse
import datetime as _dt
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d%s.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf"
FD_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d/%s.pdf"
USER_AGENT = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"
FILING_TYPES = {
    "P": "Periodic Transaction Report", "O": "Annual report", "A": "Amendment",
    "C": "Candidate report", "X": "Extension", "T": "Termination report",
    "W": "Waiver", "D": "Disclosure (new member or candidate)", "H": "New filer report",
}
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child", "": "self"}
LATE_DAYS = 45
MAX_BYTES = 64 << 20       # the most a download, or the index XML unzipped, may be
DOC_ID = re.compile(r"^\d{1,12}$")
# terminal control codes and text-direction overrides: never printed from a filing (a tab stays a tab)
CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


class PtrError(Exception):
    pass


def home(*parts):
    """A path in the trader folder: $TRADER_HOME when it is set, else trader/ in this folder."""
    trader_home = os.environ.get("TRADER_HOME", "").strip()
    return os.path.join(os.path.expanduser(trader_home), *parts) if trader_home else "/".join(("trader",) + parts)


# ---------------------------------------------------------------- time (New York, no tz files)

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


def parse_date_flag(text, flag):
    try:
        return _dt.date.fromisoformat(str(text).strip())
    except ValueError:
        raise PtrError("%s wants a date like 2026-10-19" % flag)


# ---------------------------------------------------------------- the index

def iso_date(us_date):
    """'8/21/2026' or '08/21/2026' -> '2026-08-21' (two-digit years -> 20xx)."""
    match = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", us_date or "")
    if not match:
        return None
    month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def clean(text):
    """Fetched text, safe to print: no control codes (terminal escapes) or
    direction overrides, and each run of whitespace one space."""
    return " ".join(CONTROL.sub(" ", text or "").split())


def download(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise PtrError("HTTP %s for %s" % (error.code, url))
    except urllib.error.URLError as error:
        raise PtrError("could not reach %s: %s" % (url, error.reason))
    except (OSError, http.client.HTTPException) as error:     # a timeout or a reset mid-answer
        raise PtrError("lost the answer from %s: %s" % (url, str(error) or type(error).__name__))
    if len(data) > MAX_BYTES:
        raise PtrError("%s is over %d MB; not reading it" % (url, MAX_BYTES >> 20))
    return data


def read_index_bytes(data):
    """Index XML bytes from the ZIP (or the XML itself)."""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = [info for info in archive.infolist() if info.filename.lower().endswith(".xml")]
                if not infos:
                    raise PtrError("no XML inside the ZIP (files: %s)" % ", ".join(archive.namelist()))
                if infos[0].file_size > MAX_BYTES:     # the read stops at this size, so a bomb cannot lie about it
                    raise PtrError("the index XML unzips to %d MB, over %d MB; not reading it"
                                   % (infos[0].file_size >> 20, MAX_BYTES >> 20))
                return archive.read(infos[0])
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
            raise PtrError("the index ZIP is broken: %s" % error)
    return data


def parse_index(xml_bytes, year):
    """Every filing in a <YEAR>FD.xml as a dict, newest first."""
    if re.search(rb"<!ENTITY", xml_bytes if isinstance(xml_bytes, bytes) else xml_bytes.encode("utf-8"), re.I):
        raise PtrError("the index declares XML entities; refusing to read it")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise PtrError("index is not valid XML: %s" % error)
    out = []
    for member in root.iter("Member"):
        fields = {child.tag: clean(child.text) for child in member}
        doc = fields.get("DocID", "")
        kind = fields.get("FilingType", "")
        filing_year = int(fields.get("Year") or year)
        url = (PTR_URL if kind == "P" else FD_URL) % (filing_year, doc)
        name = " ".join(part for part in (fields.get("Prefix"), fields.get("First"), fields.get("Last"),
                                          fields.get("Suffix")) if part)
        out.append({"doc_id": doc, "type": kind, "type_name": FILING_TYPES.get(kind, kind),
                    "last": fields.get("Last", ""), "first": fields.get("First", ""), "name": name,
                    "district": fields.get("StateDst", ""), "year": filing_year,
                    "filed": iso_date(fields.get("FilingDate", "")), "url": url})
    out.sort(key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)
    return out


def load_index(year, offline=None):
    if offline:
        with open(offline, "rb") as file:
            data = file.read()
    else:
        data = download(INDEX_URL % (year, "FD"))
    return parse_index(read_index_bytes(data), year)


def member_match(row, member):
    """'pelosi', 'Nancy Pelosi' or 'Pelosi, Nancy' against an index row."""
    member = (member or "").strip().lower()
    if not member:
        return True
    if "," in member:
        last, first = [part.strip() for part in member.split(",", 1)]
    else:
        parts = member.split()
        last, first = parts[-1], " ".join(parts[:-1])
    if row["last"].lower() != last:
        return False
    return not first or row["first"].lower().startswith(first)


def select(rows, members=None, kind="P", since=None):
    out = []
    for row in rows:
        if kind and row["type"] != kind:
            continue
        if members and not any(member_match(row, member) for member in members):
            continue
        if since and (row["filed"] or "") < since:
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------- state

def load_state(path, notes=None):
    """{"seen": [DocIDs], "last_check": ...}. A file that is not readable JSON
    starts a fresh state, with a note (when notes is a list)."""
    state = None
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as file:
                state = json.load(file)
        except (OSError, ValueError) as error:
            if notes is not None:
                notes.append("could not read %s (%s); started a fresh one" % (path, error))
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("seen"), list):
        state["seen"] = []
    state["seen"] = [str(doc_id) for doc_id in state["seen"]]
    state.setdefault("last_check", None)
    return state


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def new_since(rows, state, today, first_days=LATE_DAYS):
    """Rows not seen before and filed in the last first_days days (45, the
    longest a PTR may take). The window holds on every run, so a filter you
    have not used before (another member, or everyone) shows its recent
    filings, never the whole year."""
    seen = set(state.get("seen", []))
    cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
    return [row for row in rows if row["doc_id"] not in seen and (row["filed"] or "") >= cutoff]


# ---------------------------------------------------------------- parsing a PTR

TRANSACTION_LINE = re.compile(
                 r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
                 r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABELS = [
    ("status", re.compile(r"^\s*F(?:ILING)?\s+S(?:TATUS)?\s*:\s*(.*)$", re.I)),
    ("description", re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)),
    ("account", re.compile(r"^\s*S(?:UBHOLDING)?\s+O(?:F)?\s*:\s*(.*)$", re.I)),
    ("location", re.compile(r"^\s*L(?:OCATION)?\s*:\s*(.*)$", re.I)),
]
# Column headers repeat at the top of every page, sometimes in the middle of a
# row that continues on the next page: skip them without ending the row.
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)",
                         re.I)
END_OF_TABLE = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)
AMOUNT_NUMBER = re.compile(r"\$([\d,]+)")
OPTION = re.compile(r"([\d,]+)\s+(call|put)\s+options?\b.*?strike price of \$([\d,.]+)"
                    r".*?expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.S)
SHARES = re.compile(r"(?:purchased|sold|bought)\s+([\d,]+)\s+shares", re.I)


def amount_range(text):
    numbers = [int(digits.replace(",", "")) for digits in AMOUNT_NUMBER.findall(text or "")]
    low = numbers[0] if numbers else None
    high = numbers[1] if len(numbers) > 1 else None
    return low, high


def finish_row(row, filed):
    asset = " ".join(row.pop("_asset").split())
    tickers = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
    codes = re.findall(r"\[([A-Z]{2})\]", asset)
    row["ticker"] = tickers[-1] if tickers else None
    row["asset_type"] = codes[-1] if codes else None
    name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
    if tickers:
        name = re.sub(r"\s*\(%s\)" % re.escape(tickers[-1]), "", name)
    row["asset"] = name.strip(" -")
    row["amount"] = " ".join(row["amount"].split())
    row["amount_low"], row["amount_high"] = amount_range(row["amount"])
    row["description"] = " ".join(row.get("description", "").split()) or None
    option_match = OPTION.search(row["description"] or "")
    if option_match:
        row["option"] = {"contracts": int(option_match.group(1).replace(",", "")),
                         "kind": option_match.group(2).lower(),
                         "strike": float(option_match.group(3).replace(",", "")),
                         "expires": iso_date(option_match.group(4)),
                         "exercised": bool(re.match(r"\s*exercised\b", row["description"], re.I))}
    shares_match = SHARES.search(row["description"] or "")
    if shares_match:
        row["shares"] = int(shares_match.group(1).replace(",", ""))
    if filed and row["date"]:
        days = (_dt.date.fromisoformat(filed) - _dt.date.fromisoformat(row["date"])).days
        row["days_to_disclose"] = days
        row["late"] = days > LATE_DAYS
    return row


def parse_ptr_text(text, filed=None):
    """Rows from `pdftotext -layout` output of a House PTR.

    Returns {"name", "status", "district", "doc_id", "filed", "rows": [...]}.
    Each row: owner, owner_name, asset, ticker, asset_type, type, type_name,
    date, notified, amount, amount_low, amount_high, status, description,
    account, option {contracts, kind, strike, expires}, shares,
    days_to_disclose, late."""
    head = {"name": None, "status": None, "district": None, "doc_id": None, "filed": filed}
    rows, current, mode = [], None, None
    amount_open = False
    for raw in text.splitlines():
        line = CONTROL.sub(" ", raw).rstrip()
        stripped = line.strip()
        match = re.match(r"^Name:\s+(.*)$", stripped)
        if match and not head["name"]:
            head["name"] = match.group(1).strip()
            continue
        match = re.match(r"^Status:\s+(.*)$", stripped)
        if match and not head["status"]:
            head["status"] = match.group(1).strip()
            continue
        match = re.match(r"^State/District:\s*(\S+)", stripped)
        if match:
            head["district"] = match.group(1)
            continue
        match = re.search(r"Filing ID #(\d+)", stripped)
        if match:
            head["doc_id"] = match.group(1)
            continue
        match = re.match(r"^Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})", stripped)
        if match:
            head["filed"] = head["filed"] or iso_date(match.group(1))
            continue
        transaction = TRANSACTION_LINE.match(line)
        if transaction and iso_date(transaction.group("date")):
            if current:
                rows.append(current)
            owner = transaction.group("owner") or ""
            current = {"owner": owner, "owner_name": OWNERS.get(owner, owner),
                       "_asset": transaction.group("asset"), "type": transaction.group("type"),
                       "type_name": TXN_TYPES.get(transaction.group("type"), transaction.group("type")),
                       "date": iso_date(transaction.group("date")), "notified": iso_date(transaction.group("notified")),
                       "amount": transaction.group("amount"), "status": None, "account": None}
            amount_open = current["amount"].endswith("-")
            mode = "asset"
            continue
        if current is None:
            continue
        if not stripped:
            if mode in ("description", "asset"):
                mode = "labels" if mode == "description" or "[" in current["_asset"] else mode
            continue
        if PAGE_HEADER.match(stripped):
            continue
        if END_OF_TABLE.match(stripped):
            mode = "labels"
            continue
        label = next(((label_key, pattern.match(line)) for label_key, pattern in LABELS if pattern.match(line)), None)
        if label:
            key, label_match = label
            value = label_match.group(1).strip()
            if key == "description":
                current["description"] = value
                mode = "description"
            elif key in ("status", "account"):
                current[key] = value
                mode = "labels"
            else:
                mode = "labels"
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", stripped)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                current["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                current["_asset"] += " " + " ".join(parts)
        elif mode == "description":
            current["description"] = current.get("description", "") + " " + stripped
    if current:
        rows.append(current)
    head["rows"] = [finish_row(row, head["filed"]) for row in rows]
    return head


def pdf_to_text(pdf_path):
    pdftotext_path = shutil.which("pdftotext")
    if not pdftotext_path:
        return None
    try:
        out = subprocess.run([pdftotext_path, "-layout", pdf_path, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


# ---------------------------------------------------------------- output

def watchlist(args):
    """Tickers to flag: --tickers plus your positions ("ticker" or "symbol",
    like the other tools). A portfolio it cannot read is a note, not a stop."""
    tickers = {ticker.upper() for ticker in (args.tickers or [])}
    path = getattr(args, "portfolio", None)
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as file:
                portfolio = json.load(file)
        except (OSError, ValueError) as error:
            print("ptr: could not read %s (%s); flagging --tickers only" % (path, error), file=sys.stderr)
            portfolio = {}
        rows = portfolio.get("positions") if isinstance(portfolio, dict) else None
        for position in rows if isinstance(rows, list) else []:
            symbol = (position.get("ticker") or position.get("symbol")) if isinstance(position, dict) else None
            if isinstance(symbol, str) and symbol.strip():
                tickers.add(symbol.strip().upper())
    return tickers


def describe(row):
    bits = ["%s %s" % (row["owner_name"], row["type_name"])]
    what = row["asset"]
    if row.get("ticker"):
        what += " (%s)" % row["ticker"]
    if row.get("option"):
        option = row["option"]
        what += ": %s%d %s options, $%g strike, expires %s" % (
            "exercised " if option.get("exercised") else "", option["contracts"], option["kind"],
            option["strike"], option["expires"])
    elif row.get("shares"):
        what += ": %s shares" % "{:,}".format(row["shares"])
    bits.append(what)
    bits.append(row["amount"])
    when = "traded %s" % row["date"]
    if row.get("days_to_disclose") is not None:
        when += ", disclosed %d days later%s" % (row["days_to_disclose"],
                                                   " (LATE: over 45)" if row.get("late") else "")
    bits.append(when)
    if row.get("account"):
        bits.append("account: %s" % row["account"])
    return ", ".join(bits)


def render_report(report, watch=()):
    out = ["%s (%s), %s. PTR %s, filed %s." % (
        report.get("name") or "?", report.get("district") or "?", report.get("status") or "?",
        report.get("doc_id") or "?", report.get("filed") or "?")]
    for row in report["rows"]:
        flag = "  <- %s is on your list" % row["ticker"] if row.get("ticker") in watch else ""
        out.append("  - %s%s" % (describe(row), flag))
        if row.get("description") and not row.get("option") and not row.get("shares"):
            out.append("      %s" % row["description"])
    if not report["rows"]:
        out.append("  no transaction rows found: read the PDF directly")
    out.append("Public disclosure under the STOCK Act, filed up to 45 days after the trade. "
               "Amounts are ranges. Not inside information.")
    return "\n".join(out) + "\n"


def render_index(rows):
    out = []
    for row in rows:
        out.append("%s  %-28s %-5s %-4s doc %-9s %s" % (
            row["filed"], row["name"][:28], row["district"], row["type"], row["doc_id"], row["url"]))
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="cmd")
    year = ny_now().year

    def common(command):
        command.add_argument("--year", type=int, default=year)
        command.add_argument("--member", action="append",
                             help="last name or 'First Last'; repeat for more members")
        command.add_argument("--type", default="P", help="FilingType (default P = PTR; '' for all)")
        command.add_argument("--since", help="only filings on or after YYYY-MM-DD")
        command.add_argument("--offline", metavar="FILE", help="a saved <YEAR>FD.zip or .xml")
        command.add_argument("--json", action="store_true")

    command = commands.add_parser("index", help="list filings from the yearly index")
    common(command)
    command.add_argument("--limit", type=int, default=50)

    command = commands.add_parser("new", help="filings not seen at the last check")
    common(command)
    command.add_argument("--state", default=home("state", "pelosi.json"))
    command.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    command.add_argument("--dry-run", action="store_true", help="do not update the state file")
    command.add_argument("--read", action="store_true",
                         help="also download and read each new PTR (needs pdftotext)")
    command.add_argument("--pdf-dir", default=home("state", "ptr-pdfs"))
    command.add_argument("--tickers", nargs="*", help="flag trades in these tickers")
    command.add_argument("--portfolio", default=home("portfolio.json"))

    command = commands.add_parser("fetch", help="download one PTR PDF and read it")
    command.add_argument("doc_id")
    command.add_argument("--year", type=int, default=year)
    command.add_argument("--filed", help="filing date YYYY-MM-DD if known")
    command.add_argument("--pdf-dir", default=home("state", "ptr-pdfs"))
    command.add_argument("--tickers", nargs="*")
    command.add_argument("--portfolio", default=home("portfolio.json"))
    command.add_argument("--json", action="store_true")

    command = commands.add_parser("parse", help="rows from pdftotext -layout text of a PTR")
    command.add_argument("text_file")
    command.add_argument("--filed", help="filing date YYYY-MM-DD (else read from the signature)")
    command.add_argument("--tickers", nargs="*")
    command.add_argument("--portfolio", default=home("portfolio.json"))
    command.add_argument("--json", action="store_true")
    return parser


def fetch_and_read(doc_id, year, pdf_dir, filed=None):
    doc_id = str(doc_id).strip()
    if not DOC_ID.match(doc_id):      # it names a file on disk and a path on the Clerk's site
        raise PtrError("%r is not a House DocID (digits only, like 20035143)" % doc_id)
    os.makedirs(pdf_dir, exist_ok=True)
    path = os.path.join(pdf_dir, "%s.pdf" % doc_id)
    if not os.path.isfile(path):
        data = download(PTR_URL % (year, doc_id))
        if not data.startswith(b"%PDF"):
            raise PtrError("doc %s did not come back as a PDF" % doc_id)
        with open(path + ".tmp", "wb") as file:      # a half-written PDF never passes for a downloaded one
            file.write(data)
        os.replace(path + ".tmp", path)
    text = pdf_to_text(path)
    if text is None:
        return path, None
    report = parse_ptr_text(text, filed=filed)
    report["pdf"] = path
    report["url"] = PTR_URL % (year, doc_id)
    return path, report


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.cmd:
        build_parser().print_help()
        return 2
    try:
        return run(args)
    except (PtrError, OSError) as error:
        print("ptr: %s" % error, file=sys.stderr)
        return 2


def run(args):
    if args.cmd == "parse":
        with open(args.text_file, encoding="utf-8", errors="replace") as file:
            report = parse_ptr_text(file.read(), filed=args.filed)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    if args.cmd == "fetch":
        path, report = fetch_and_read(args.doc_id, args.year, args.pdf_dir, args.filed)
        if report is None:
            print("Saved %s. pdftotext is not installed, so read the PDF directly "
                  "(Claude can open it), or install poppler (brew install poppler)." % path)
            return 0
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    if args.since:
        parse_date_flag(args.since, "--since")
    today = parse_date_flag(args.today, "--today") if getattr(args, "today", None) else ny_now().date()
    rows = load_index(args.year, args.offline)
    if args.cmd == "new" and not args.offline:
        if today.month == 1 and args.year == today.year:
            try:
                rows += load_index(args.year - 1)
            except PtrError:
                pass
    picked = select(rows, args.member, args.type or None, args.since)

    if args.cmd == "index":
        picked = picked[:args.limit]
        if args.json:
            print(json.dumps(picked, indent=2))
        else:
            sys.stdout.write(render_index(picked))
            if not picked:
                print("No matching filings in the %d index." % args.year)
        return 0 if picked else 1

    # new
    notes = []
    state = load_state(args.state, notes)
    for note in notes:
        print("ptr: %s" % note, file=sys.stderr)
    fresh = new_since(picked, state, today)
    watch = watchlist(args)
    reports = []
    if args.read:
        for row in fresh:
            if row["type"] != "P":
                continue
            path, report = fetch_and_read(row["doc_id"], row["year"], args.pdf_dir, row["filed"])
            reports.append({"filing": row, "pdf": path, "report": report})
    if args.json:
        print(json.dumps({"new": fresh, "reports": reports}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        for row in fresh:
            print("NEW %s: %s (%s) filed %s, doc %s" % (
                row["type_name"], row["name"], row["district"], row["filed"], row["doc_id"]))
            print("    %s" % row["url"])
        for fetched in reports:
            if fetched["report"] is None:
                print("    read %s (pdftotext not installed)" % fetched["pdf"])
            else:
                sys.stdout.write(render_report(fetched["report"], watch))
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(row["doc_id"] for row in fresh)          # what was shown is seen
        state["seen"] = sorted(seen)
        state["last_check"] = ny_now().strftime("%Y-%m-%d %H:%M New York")
        save_state(args.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
