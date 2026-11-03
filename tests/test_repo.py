import datetime
import inspect
import json
import os
import re
import tempfile
import unittest

from _load import ROOT, load

# v2, the day-trading pack: the eleven folders the reel's repo card shows
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-pelosi"]
ABOUT = ("11 Claude skills for day trading: scan thousands of charts, track breaking news, time every "
         "entry and exit, and replay every outcome before you risk a dollar.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-pelosi", "trader-risk", "trader-broker", "trader-alerts"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]
# this copy's own notes, kept at the root next to what the reel shows
LOCAL_DOCS = ["CLAUDE.md", "howtouse.md", "nomenclature.md", "paper-trading-scenarios",
              "shashwat-day-trader-skill-setup-guide.pdf"]
SCRIPTS = {"trader-scanner": "scan", "trader-news": "news", "trader-entry": "entry", "trader-exit": "exit",
           "trader-timemachine": "timemachine", "trader-risk": "check", "trader-budget": "size",
           "trader-broker": "ticket", "trader-alerts": "alerts", "trader-analyst": "facts", "trader-pelosi": "ptr"}
# characters that make text show in a different order than it runs ("Trojan Source")
BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        # .env and .env.local hold the user's own API keys: git ignores them and they never ship
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git") and d != ".env"
                      and not d.startswith(".env."))
        shipped = sorted([".claude-plugin", "LICENSE", "README.md", "tests"] + SKILLS)
        self.assertEqual([d for d in root if d in shipped], shipped)
        self.assertEqual([d for d in root if d not in shipped and d not in LOCAL_DOCS], [])

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill")

    def test_readme_carries_the_reel_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill"))
        for line in (ABOUT,
                     "https://github.com/Jakeschincariol/day-trader-skill install skill",
                     "/plugin marketplace add Jakeschincariol/day-trader-skill",
                     "/plugin install day-trader-skill@day-trader-skill",
                     "## Fine print"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_hidden_direction_characters(self):
        # raw control codes (a NUL, an escape) in Python source: write them as escapes, like "\x1b"
        raw = re.compile("[" + "".join(chr(i) for i in list(range(9)) + list(range(11, 13)) + list(range(14, 32))
                                       + list(range(0x7f, 0xa0))) + "]")
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue                         # real fetched data; the tools clean() it before it prints
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            m = BIDI.search(text) or (raw.search(text) if path.endswith(".py") else None)
            self.assertIsNone(m, "%s has U+%04X: write it as an escape" % (rel, ord(m.group()) if m else 0))

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_options", False), False)


class SharedCode(unittest.TestCase):
    """Each folder stands alone, so shared code is copied into each script. The copies must agree."""

    @classmethod
    def setUpClass(cls):
        cls.mods = {f: load(f, n) for f, n in SCRIPTS.items()}

    def test_one_nyse_calendar(self):
        ref = self.mods["trader-risk"]
        for f in ("trader-scanner", "trader-entry", "trader-exit", "trader-alerts"):
            m = self.mods[f]
            self.assertEqual((m.NYSE_HOLIDAYS, m.NYSE_EARLY_CLOSE, m.CALENDAR_YEARS),
                             (ref.NYSE_HOLIDAYS, ref.NYSE_EARLY_CLOSE, ref.CALENDAR_YEARS), f)
        self.assertEqual(self.mods["trader-timemachine"].NYSE_EARLY_CLOSE, ref.NYSE_EARLY_CLOSE)
        years = {int(d[:4]) for d in ref.NYSE_HOLIDAYS}
        self.assertEqual(tuple(sorted(years)), ref.CALENDAR_YEARS)        # no year half filled in
        soon = datetime.date.today() + datetime.timedelta(days=90)
        self.assertGreaterEqual(ref.CALENDAR_YEARS[-1], soon.year,
                                "the NYSE calendar runs out within 90 days: add the next year (nyse.com) to every copy")

    def test_one_new_york_clock(self):
        days = [(3, 7), (3, 8), (3, 9), (3, 13), (3, 14), (3, 15), (10, 31), (11, 1), (11, 2), (11, 7), (11, 8)]
        instants = [datetime.datetime(y, m, d, h, 30) for y in range(2026, 2031) for m, d in days for h in range(24)]
        want = [self.mods["trader-risk"].eastern_offset(t) for t in instants]
        for f, m in self.mods.items():
            self.assertEqual([m.eastern_offset(t) for t in instants], want, f)

    def test_one_fingerprint(self):
        risk, broker = self.mods["trader-risk"], self.mods["trader-broker"]
        self.assertEqual(inspect.getsource(risk.fingerprint).replace("Same function in trader-broker/ticket.py",
                                                                      "Same function as trader-risk/check.py"),
                         inspect.getsource(broker.fingerprint).replace(": identifies the exact order",
                                                                        ": the exact order that was checked"))
        self.assertEqual(risk.ALPACA_FIELDS, broker.ALPACA_FIELDS)

    def test_one_trader_folder(self):
        self.assertEqual(len({inspect.getsource(m.home) for m in self.mods.values()}), 1)
        old = os.environ.pop("TRADER_HOME", None)
        try:
            for f, m in self.mods.items():
                self.assertEqual(m.home("state", "x.json"), "trader/state/x.json", f)
            with tempfile.TemporaryDirectory() as d:
                os.environ["TRADER_HOME"] = d
                for f, m in self.mods.items():
                    self.assertEqual(m.home("state", "x.json"), os.path.join(d, "state", "x.json"), f)
        finally:
            os.environ.pop("TRADER_HOME", None)
            if old is not None:
                os.environ["TRADER_HOME"] = old

    def test_fetched_text_is_cleaned_one_way(self):
        news, facts, ptr = self.mods["trader-news"], self.mods["trader-analyst"], self.mods["trader-pelosi"]
        for i in range(0x2100):
            ch = chr(i)
            want = bool(news.CONTROL.match(ch))
            self.assertEqual(bool(facts.CONTROL.match(ch)), want, hex(i))
            self.assertEqual(bool(ptr.CONTROL.match(ch)), want and ch != "\t", hex(i))   # ptr keeps PTR columns
        self.assertEqual(inspect.getsource(news.clean), inspect.getsource(facts.clean))

    def test_one_reading_of_the_config(self):
        risk, size, broker = self.mods["trader-risk"], self.mods["trader-budget"], self.mods["trader-broker"]
        self.assertEqual((set(risk.BOOL_KEYS) | set(size.BOOL_KEYS)) & (set(risk.NUMBER_KEYS) | set(size.NUMBER_KEYS)),
                         set())
        tools = ((risk, risk.RiskError), (size, size.SizeError), (broker, broker.TicketError))
        for k, bad in (("paper", "false"), ("paper", 1), ("budget", "5000$"), ("budget", float("nan")),
                       ("budget", True), ("budget", -1)):
            for mod, err in tools:
                with self.assertRaises(err, msg="%s %s=%r" % (mod.__name__, k, bad)):
                    mod.rules({k: bad})
        for k in set(risk.NUMBER_KEYS) & set(size.NUMBER_KEYS):
            for mod, err in tools[:2]:
                with self.assertRaises(err, msg="%s %s" % (mod.__name__, k)):
                    mod.rules({k: "1$"})
                self.assertIsNone(mod.rules({k: None})[k], (mod.__name__, k))     # null: off, the same everywhere
                self.assertEqual(mod.rules({k: 0})[k], 0, (mod.__name__, k))       # 0: zero


if __name__ == "__main__":
    unittest.main()
