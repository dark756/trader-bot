import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-tsla.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
BATCH = ["AAPL", "AXP", "GOOGL", "KO", "BAC"]
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
TSLA_PLAIN = ("BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about "
              "$46.88 if the stop fills at 358.32 (it can fill worse in a fast market)")


def tsla(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=AAPL, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes AAPL", "yes $1,250 of AAPL",
                  "yes 1250"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes AXP", "yes 1000", "yes but make it 1000", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 340"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("yes sell 3 KO", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_bracket_yes(self):
        o = tsla()
        for r in ("yes", "yes 8 TSLA", "yes 358.32", "yes max loss 46.88", "yes target 370.04"):
            self.assertTrue(self.yes(r, o, ["TSLA"]), r)
        for r in ("yes 9", "yes 10 TSLA", "yes but stop at 357", "yes, stop 358.32", "yes AAPL", "sure"):
            self.assertFalse(self.yes(r, o, ["TSLA", "AAPL"]), r)

    def test_a_yes_with_anything_else_in_it_does_not(self):
        o = tsla()
        for r in ("yes never mind", "yes lol jk", "yes buy NVDA", "yes 8 shares and 500 NVDA", "yes sell 8 TSLA",
                  "yes 8 TSLA instead", "yes, actually no", "yes नहीं", "yes 8 TSLA + 2 AMD",
                  "8 yes", "yes it's fine"):
            self.assertFalse(self.yes(r, o, ["TSLA"]), r)
        for r in ("Yes, buy 8 shares of TSLA at market", "yes $TSLA", "yes, place the order.",
                  "yes stop-loss 358.32", "YES!"):
            self.assertTrue(self.yes(r, o, ["TSLA"]), r)
        self.assertIn("this order is a buy", ticket.parse_yes("yes sell it", o, ["TSLA"])[1])
        self.assertIn("\"nvda\"", ticket.parse_yes("yes buy NVDA", o, ["TSLA"])[1])

    def test_the_ticket_carries_the_risk_checks_max_loss(self):
        o = tsla()
        rv = {"max_loss": 49.44, "stop": 358.32}      # a fresh 364.50 at the check: 8 x 6.18
        self.assertIn("max loss about $49.44 if the stop fills at 358.32", ticket.plain(o, rv))
        self.assertTrue(ticket.parse_yes("yes max loss 49.44", o, ["TSLA"], rv)[0])
        self.assertFalse(ticket.parse_yes("yes max loss 46.88", o, ["TSLA"], rv)[0])
        self.assertEqual(ticket.max_loss(o, rv), (49.44, 358.32))


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_alpaca_call(self):
        o = dict(AAPL, why="from a list")
        self.assertEqual(ticket.tool_input(o), AAPL)
        self.assertEqual(ticket.tool_input({"symbol": "KO", "side": "buy", "qty": 3, "type": "limit",
                                            "limit_price": 89.5, "time_in_force": "day"}),
                         {"symbol": "KO", "side": "buy", "qty": "3", "type": "limit", "limit_price": "89.5",
                          "time_in_force": "day"})

    def test_the_bracket_call_leaves_out_why_and_plan(self):
        self.assertEqual(ticket.tool_input(tsla()), TSLA_CALL)
        self.assertEqual(ticket.fingerprint(tsla()), ticket.fingerprint(TSLA_CALL))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(AAPL), "BUY $1,250 of AAPL, market order, good for today")
        self.assertEqual(ticket.plain({"symbol": "KO", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "95", "time_in_force": "gtc"}),
                         "SELL 1 share of KO, limit order at $95, good until canceled")
        self.assertEqual(ticket.plain(tsla()), TSLA_PLAIN)
        lim = tsla(type="limit", limit_price="364.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 8 TSLA, limit 364.00, good for today")
        self.assertEqual(ticket.max_loss(lim), (45.44, 358.32))
        no_plan = dict(TSLA_CALL)
        self.assertEqual(ticket.max_loss(no_plan), (None, 358.32))


class DesktopConfig(unittest.TestCase):
    def test_example_is_paper_with_placeholder_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["alpaca"]
        self.assertEqual((server["command"], server["args"]), ("uvx", ["alpaca-mcp-server"]))
        self.assertEqual(server["env"]["ALPACA_PAPER_TRADE"], "true")
        for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            self.assertIn("NEVER_IN_CHAT", server["env"][k])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_full_two_yes_flow(self):
        # --now pins the clock: a practice run end to end (RealClock below runs it for real)
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("PRACTICE RUN"))
        self.assertIn("ORDER 1 of 1, PAPER account per your config, Alpaca", text)
        for part in TSLA_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn("Practice run: in a real one, Claude would call place_stock_order", text)
        self.assertIn('"order_class": "bracket"', text)
        self.assertIn('"stop_loss_stop_price": "358.32"', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertNotIn("Allow once", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("PRACTICE RUN", text)
        self.assertNotIn("Now call place_stock_order", text)
        self.assertNotIn("{", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc123",
                                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf,
                                      now="2026-10-19 10:16"))
        self.assertEqual(code, 0)
        self.assertIn("max loss at the stop 358.32 is $47.04 now", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 46.88, TODAY))
        self.assertEqual(appr["at"], "2026-10-19T10:15:00-04:00")
        self.assertIs(appr["practice"], True)
        # filled 2 cents above the plan's entry: 8 x (364.20 - 358.32) is what is really at risk
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"], plc["filled_price"]),
                         ("abc123", 47.04, "entry", 364.2))
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 2086.4)
        self.assertEqual(pf["positions"][0]["shares"], 8)

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss $47.04", text)
        self.assertIn("(practice)", text)

        # trader-risk now counts it: a second TSLA buy today is a duplicate, and $47.04 is used
        res = check.check([tsla()], {"budget": 5000}, state=st, now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 47.04))

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf))
        late = "2026-10-19 15:55"
        code, text, _ = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 8 shares of TSLA, market order, good for today (flat by 15:55 New York)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancel_order_by_id", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(ticket.tool_input(doc["orders"][0]),
                         {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"})
        self.assertEqual(self.run_risk(now=late), 0)       # after the 15:30 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 8 TSLA", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                   "--filled-price", "366.10", "--portfolio", self.pf, now="2026-10-19 15:56"))
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual((pf["cash"], pf["positions"]), (5015.2, []))
        self.assertEqual(check.day_trades(self.state_doc(), TODAY), (1, ["2026-10-19 TSLA"]))
        code, _, err = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "tsla", "--qty", "8", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_open_position", text)
        self.assertEqual(run(["flatten", "--symbol", "TSLA", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "9"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 10:15")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:40"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:40"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:45", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 09:40"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_ibkr_tickets_send_the_final_yes_to_ibkr(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["broker"] = "ibkr"
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("Interactive Brokers", text)
        self.assertIn("after you approve it there", text)
        self.assertNotIn("place_stock_order", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "KO", "shares": 3, "lots": [
            {"id": "KO-1", "date": "2025-01-02", "shares": 2, "cost": 120.0},
            {"id": "KO-2", "date": "2026-01-02", "shares": 1, "cost": 70.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "2.5", "--price", "90",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 225.0)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["KO-2"])
        self.assertEqual(lots[0]["shares"], 0.5)
        self.assertEqual(lots[0]["cost"], 35.0)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "5", "--price", "90",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_a_failed_order_can_be_tried_again_after_a_new_check(self):
        self.run_risk()
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes"))[0], 0)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "failed", "--note", "rejected",
                                      now="2026-10-19 10:16"))
        self.assertIn("run check.py again, then ask the user for a new yes", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:16"))
        self.assertEqual(code, 1)
        self.assertIn("DONE: failed at 2026-10-19T10:16:00-04:00; a new risk check (check.py) makes it READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:16"))[0], 1)
        self.run_risk(now="2026-10-19 10:17")          # newer than the failure: READY for a new yes
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:17"))
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS, waiting for your yes (a new try: it was failed at", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:17"))[0], 0)
        self.assertEqual(run(self.args("result", "--index", "1", "--status", "placed",
                                       now="2026-10-19 10:18"))[0], 0)
        self.run_risk(now="2026-10-19 10:19")          # placed is placed: never a second time
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:19"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by trader-risk: duplicate: a buy TSLA was already placed today", text)
        self.assertEqual(ticket.status_of(tsla(), {"date": TODAY, "clock": "pinned", "now_utc": "2026-10-19T14:20:00Z",
                                                   "results": [{"fingerprint": ticket.fingerprint(tsla()),
                                                                "verdict": "PASS"}]},
                                          self.state_doc(), TODAY, check.parse_now("2026-10-19 10:20"), True)[1],
                         "DONE: placed at 2026-10-19T10:18:00-04:00")
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "failed", "approved", "placed"])

    def test_a_denied_or_declined_order_needs_a_new_check_too(self):
        for status in ("denied", "declined"):
            self.run_risk()
            if status == "denied":
                run(self.args("approve", "--index", "1", "--reply", "yes"))
                run(self.args("result", "--index", "1", "--status", "denied", now="2026-10-19 10:16"))
            else:
                run(self.args("decline", "--index", "1", "--reply", "no", now="2026-10-19 10:16"))
            self.assertIn("DONE: %s" % status, run(self.args("show", now="2026-10-19 10:16"))[1])
            self.run_risk(now="2026-10-19 10:17")
            self.assertIn("READY: trader-risk PASS", run(self.args("show", now="2026-10-19 10:17"))[1])
            os.remove(self.state)

    def test_practice_runs_keep_their_own_files(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            os.makedirs("trader")
            shutil.copy(ORDERS_PATH, os.path.join("trader", "orders.json"))
            shutil.copy(CFG_PATH, os.path.join("trader", "config.json"))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(["--now", NOW]), 0)
            self.assertTrue(os.path.exists(os.path.join("trader", "practice", "state", "risk.json")))
            self.assertEqual(run(["approve", "--index", "1", "--reply", "yes", "--now", NOW])[0], 0)
            self.assertEqual(run(["result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                  "--filled-price", "364.20", "--now", "2026-10-19 10:16"])[0], 0)
            self.assertIn("1 placed today", run(["log", "--now", NOW])[1])
            self.assertTrue(os.path.exists(os.path.join("trader", "practice", "portfolio.json")))
            self.assertFalse(os.path.exists(os.path.join("trader", "state")))      # your real files: untouched
            self.assertFalse(os.path.exists(os.path.join("trader", "portfolio.json")))
            self.assertEqual(run(["log"])[0], 1)                                  # the real log is empty
        finally:
            os.chdir(cwd)

    def test_an_auto_yes_is_for_practice_runs_on_paper(self):
        # an unattended replay (paper-trading-scenarios) records its yes with --auto-yes
        code, _, err = run(self.args("approve", "--index", "1", "--auto-yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)                      # no risk check yet: the same gates as a typed yes
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("approve", "--index", "1", "--auto-yes"))
        self.assertEqual(code, 0)
        self.assertIn("Recorded an automatic yes for order 1", text)
        self.assertIn("PRACTICE RUN", text)
        self.assertNotIn("Now call place_stock_order", text)
        self.assertNotIn("{", text)
        appr = self.state_doc()["orders"][-1]
        self.assertEqual((appr["status"], appr["auto_yes"], appr["practice"], appr["kind"], appr["max_loss"]),
                         ("approved", True, True, "entry", 46.88))
        self.assertNotIn("reply", appr)                      # nobody replied, and the log never says so
        self.assertIn("APPROVED automatically (--auto-yes, a practice run)", run(self.args("show", "--index", "1"))[1])
        code, _, err = run(self.args("approve", "--index", "1", "--auto-yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)                       # still one yes per order
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "SIM-1",
                                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        placed = self.state_doc()["orders"][1]
        self.assertEqual((placed["status"], placed["auto_yes"], placed["practice"], placed["max_loss"]),
                         ("placed", True, True, 47.04))
        self.assertIn(" auto-yes (practice)\n", run(["log", "--state", self.state, "--now", NOW])[1])
        # trader-risk counts it like a typed one: a second TSLA buy today is a duplicate
        self.assertEqual(self.run_risk("2026-10-19 10:30"), 1)

    def test_a_live_config_never_gets_an_auto_yes(self):
        with open(self.cfg) as fh:
            cfg = json.load(fh)
        cfg["paper"] = False
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)
        self.assertEqual(self.run_risk(), 0)
        self.assertIn("READY: trader-risk PASS", run(self.args("show"))[1])
        code, _, err = run(self.args("approve", "--index", "1", "--auto-yes"))
        self.assertEqual(code, 1)
        self.assertIn("--auto-yes is for paper trading only, and your config says \"paper\": false", err)
        self.assertFalse(os.path.exists(self.state))

    def test_auto_yes_and_a_reply_are_one_or_the_other(self):
        self.run_risk()
        for extra in ([], ["--reply", "yes", "--auto-yes"]):
            with self.assertRaises(SystemExit) as caught:
                run(self.args("approve", "--index", "1", *extra))
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(os.path.exists(self.state))

    def test_a_practice_fill_stays_in_the_practice_files(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            os.makedirs("trader")
            shutil.copy(ORDERS_PATH, os.path.join("trader", "orders.json"))
            shutil.copy(CFG_PATH, os.path.join("trader", "config.json"))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(["--now", NOW]), 0)
            self.assertEqual(run(["approve", "--index", "1", "--auto-yes", "--now", NOW])[0], 0)
            self.assertEqual(run(["result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                  "--filled-price", "364.20", "--now", "2026-10-19 10:16"])[0], 0)
            # the bracket's take-profit fills: the sell goes to the practice files too
            code, text, _ = run(["fill", "--practice", "--symbol", "TSLA", "--side", "sell", "--qty", "8",
                                 "--price", "370.04", "--now", "2026-10-19 10:53"])
            self.assertEqual(code, 0)
            self.assertIn("Recorded a practice fill: sell 8 TSLA at $370.04 on 2026-10-19", text)
            with open(os.path.join("trader", "practice", "portfolio.json")) as fh:
                self.assertEqual(json.load(fh), {"cash": 5046.72, "positions": []})
            with open(os.path.join("trader", "practice", "state", "broker.json")) as fh:
                log = json.load(fh)
            self.assertEqual([(r["status"], r["side"], r.get("practice")) for r in log["orders"]],
                             [("approved", "buy", True), ("placed", "buy", True), ("filled", "buy", True),
                              ("filled", "sell", True)])
            self.assertFalse(os.path.exists(os.path.join("trader", "state")))      # your real files: untouched
            self.assertFalse(os.path.exists(os.path.join("trader", "portfolio.json")))
            # a practice check now sees the round trip: what "pdt_guard" counts
            self.assertEqual(check.day_trades(log, "2026-10-20"), (1, ["2026-10-19 TSLA"]))
            # --now only dates a fill: without --practice it is a real one
            self.assertEqual(run(["fill", "--symbol", "KO", "--side", "buy", "--qty", "1", "--price", "60",
                                  "--now", NOW])[0], 0)
            with open(os.path.join("trader", "state", "broker.json")) as fh:
                self.assertNotIn("practice", json.load(fh)["orders"][-1])
        finally:
            os.chdir(cwd)

    def test_a_broken_file_is_a_plain_error(self):
        self.run_risk()
        for path, doc, says in ((self.state, [], "should be one JSON object"),
                                (self.state, {"orders": {"a": 1}}, "is not trader-broker's log"),
                                (self.orders, {"orders": ["TSLA"]}, "every order should be one JSON object")):
            saved = None
            if os.path.exists(path):
                with open(path) as fh:
                    saved = fh.read()
            with open(path, "w") as fh:
                json.dump(doc, fh)
            code, _, err = run(self.args("show"))
            self.assertEqual(code, 1, doc)
            self.assertIn(says, err)
            self.assertNotIn("Traceback", err)
            if saved is None:
                os.remove(path)
            else:
                with open(path, "w") as fh:
                    fh.write(saved)
        with open(self.risk, "w") as fh:
            json.dump(["PASS"], fh)
        self.assertIn("the risk check file is not one check.py wrote", run(self.args("show"))[1])
        code, _, err = run(["fill", "--symbol", "KO", "--side", "buy", "--qty", "nan", "--price", "90",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("a fill needs a positive qty and price", err)

    def test_flatten_with_a_broken_portfolio_is_one_line(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": 5}, fh)
        code, _, err = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                            "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)
        self.assertNotIn("Traceback", err)

    def test_a_fill_finds_the_position_by_ticker_or_symbol(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "KO", "shares": 3}]}, fh)
        code, _, err = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "3", "--price", "90",
                            "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0, err)
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh), {"cash": 270.0, "positions": []})

    def test_a_config_it_cannot_read_is_an_error(self):
        for k, v in (("paper", "false"), ("broker", "schwab"), ("alpaca_account", 123), ("budget", "5000$")):
            with open(self.cfg) as fh:
                c = json.load(fh)
            c[k] = v
            with open(self.cfg, "w") as fh:
                json.dump(c, fh)
            code, _, err = run(self.args("show"))
            self.assertEqual(code, 1, k)
            self.assertIn(k + " must be", err)
            self.assertNotIn("Traceback", err)
            shutil.copy(CFG_PATH, self.cfg)


class RealClock(unittest.TestCase):
    """No --now: the real clock, swapped for 10:15 New York on the reel's Monday."""
    ACCOUNT = "PA1234ABCD"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        self.config(alpaca_account=self.ACCOUNT)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.none = os.path.join(self.d, "none")
        self.saved = (check.utc_now, ticket.utc_now)
        self.clock("2026-10-19 10:15")

    def tearDown(self):
        check.utc_now, ticket.utc_now = self.saved
        shutil.rmtree(self.d)

    def config(self, **over):
        with open(CFG_PATH) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def clock(self, ny):
        t = check.parse_now(ny)
        check.utc_now = ticket.utc_now = lambda: t

    def risk_check(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.none,
                               "--prices", self.none, "--state", self.state, "--out", self.risk] + list(extra))

    def args(self, *extra):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state]

    def account(self, number):
        return run(["account", "--number", number, "--config", self.cfg, "--state", self.state])

    def test_a_real_order_needs_a_real_check_and_todays_account(self):
        self.assertEqual(self.risk_check(), 0)
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["clock"], "real")
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: the Alpaca account is not checked today", text)
        code, _, err = self.account("PA9999LIVE")
        self.assertEqual(code, 1)
        self.assertIn("STOP: the Alpaca server is connected to account PA9999LIVE", err)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.account("pa1234abcd")[0], 0)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0)
        self.assertNotIn("PRACTICE", text)
        self.assertIn("ORDER 1 of 1, PAPER account per your config, Alpaca", text)
        self.assertIn("account: pa1234abcd, checked against your config today at 10:15 New York", text)
        self.assertIn("Claude will call place_stock_order with exactly:", text)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Now call place_stock_order with exactly:", text)
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1]), TSLA_CALL)
        with open(self.state) as fh:
            appr = json.load(fh)["orders"][-1]
        self.assertEqual((appr["status"], appr["account"]), ("approved", "pa1234abcd"))
        self.assertNotIn("practice", appr)

    def test_a_pass_counts_for_15_minutes(self):
        self.risk_check()
        self.account(self.ACCOUNT)
        self.clock("2026-10-19 10:29")
        self.assertIn("READY: trader-risk PASS", run(self.args("show"))[1])
        self.clock("2026-10-19 10:31")
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("the risk check is 16 minutes old, and a PASS counts for 15", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        self.assertEqual(self.risk_check(), 0)          # a new check at 10:31 makes it READY again
        self.assertIn("READY: trader-risk PASS", run(self.args("show"))[1])

    def test_practice_and_real_never_mix(self):
        self.account(self.ACCOUNT)
        self.assertEqual(self.risk_check("--now", "2026-10-19 10:15"), 0)    # pinned, though it is 10:15 now
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("the risk check was a practice run (--now or --today), so it cannot pass a real order", text)
        self.assertEqual(self.risk_check("--today", TODAY), 0)
        self.assertIn("practice run", run(self.args("show"))[1])
        self.risk_check()
        code, text, _ = run(self.args("show", "--now", NOW))
        self.assertIn("this is a practice run (--now or --today) and the risk check ran on the real clock", text)
        self.assertIn("READY", run(self.args("show"))[1])

    def test_no_pinned_account_means_not_ready(self):
        self.config()
        self.risk_check()
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: no Alpaca account pinned yet", text)
        code, _, err = self.account(self.ACCOUNT)
        self.assertEqual(code, 1)
        self.assertIn("they add \"alpaca_account\": \"PA1234ABCD\" to the config themselves", err)
        self.config(broker="ibkr")                     # IBKR asks for its own approval in IBKR
        self.risk_check()
        self.assertIn("READY: trader-risk PASS", run(self.args("show"))[1])

    def test_real_runs_ignore_practice_decisions(self):
        # one shared log (the paths are passed): a practice yes and a practice fill stay practice
        self.account(self.ACCOUNT)
        self.assertEqual(self.risk_check("--now", NOW), 0)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", "--now", NOW))[0], 0)
        self.assertEqual(run(self.args("result", "--index", "1", "--status", "placed", "--now", NOW))[0], 0)
        self.assertEqual(self.risk_check(), 0)          # not "already placed today": that one was practice
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS, waiting for your yes\n", text)
        self.assertIn("placed    BUY 8 TSLA", run(["log", "--state", self.state])[1])
        self.assertIn("(practice)", run(["log", "--state", self.state])[1])

    def test_an_auto_yes_never_stands_in_for_a_real_yes(self):
        self.account(self.ACCOUNT)
        self.assertEqual(self.risk_check(), 0)
        self.assertIn("READY: trader-risk PASS", run(self.args("show"))[1])
        code, text, err = run(self.args("approve", "--index", "1", "--auto-yes"))      # the real clock: refused
        self.assertEqual(code, 1)
        self.assertIn("--auto-yes is for practice runs only", err)
        self.assertNotIn("place_stock_order", text)
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["orders"], [])
        # a practice auto-yes in the same log stays practice: the real ticket still waits for the user
        self.assertEqual(self.risk_check("--now", NOW), 0)
        self.assertEqual(run(self.args("approve", "--index", "1", "--auto-yes", "--now", NOW))[0], 0)
        self.assertEqual(self.risk_check(), 0)
        self.assertIn("READY: trader-risk PASS, waiting for your yes\n", run(self.args("show"))[1])

    def test_a_failed_check_clears_the_last_verdicts(self):
        self.account(self.ACCOUNT)
        self.assertEqual(self.risk_check(), 0)
        self.config(alpaca_account=self.ACCOUNT, max_loss_per_trade_usd="50$")
        self.assertEqual(self.risk_check(), 1)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: the last risk check stopped with an error (config: max_loss_per_trade_usd must be "
                      "a number", text)


if __name__ == "__main__":
    unittest.main()
