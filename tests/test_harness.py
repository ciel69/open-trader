import contextlib
import io
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import guard
from tb.core import journal
from tb.core import portfolio
from tb.core import session

PROF = guard.load_profile()


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.old = (journal.PATH, portfolio.PATH, session.PATH)
        journal.PATH = os.path.join(d, "trades.jsonl")
        portfolio.PATH = os.path.join(d, "portfolio.json")
        session.PATH = os.path.join(d, "sessions.jsonl")

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH = self.old

    def trade(self, tid, stype, pnl, r, sess=None, mode="live"):
        journal._append({"ev": "open", "id": tid, "sym": "ETH", "side": "long", "mode": mode, "stype": stype,
                         "session": sess, "risk_usdt": 1.0, "notional": 50, "entry": 1, "ts_open": "x"})
        journal._append({"ev": "close", "id": tid, "ts_close": "2026-10-27T10:00:00Z", "pnl_usdt": pnl, "r": r,
                         "reason": "manual", "fees_usdt": 0.1})


class PortfolioTest(Base):
    def test_hwm_drawdown_ladder_and_deposit(self):
        portfolio.set_venue("platform", 30.0, 20.0)
        portfolio.deposit("bybit", 10.0)
        p = portfolio.load()
        self.assertAlmostEqual(portfolio.total(p), 40.0)
        self.assertAlmostEqual(p["hwm"], 40.0)          # пополнение не прибыль и не просадка
        self.assertEqual(portfolio.dd_mult(p, PROF), 1.0)
        portfolio.set_venue("platform", 25.0, 15.0)     # 35/40: просадка 12.5%
        p = portfolio.load()
        self.assertEqual(portfolio.dd_mult(p, PROF), 0.5)
        portfolio.set_venue("platform", 20.0, 10.0)     # 30/40: 25% >= halt
        self.assertEqual(portfolio.dd_mult(portfolio.load(), PROF), 0.0)

    def test_growth_raises_hwm_and_limits_scale(self):
        portfolio.set_venue("platform", 30.0, 20.0)
        small = portfolio.scaled_profile(PROF, portfolio.load())
        portfolio.set_venue("platform", 60.0, 40.0)
        big = portfolio.scaled_profile(PROF, portfolio.load())
        self.assertAlmostEqual(big["margin_per_trade_max"], 2 * small["margin_per_trade_max"])
        self.assertAlmostEqual(big["daily_loss_limit_usdt"], 2 * small["daily_loss_limit_usdt"])
        self.assertEqual(portfolio.load()["hwm"], 60.0)

    def test_perf_mult_probation_good_bad(self):
        self.assertEqual(portfolio.perf_mult("scalp", PROF)[0], PROF["perf_mult"]["probation"])
        for i in range(30):
            self.trade("g%d" % i, "intraday", 1.0 if i % 3 else -0.5, 1.5 if i % 3 else -1.0)
        self.assertEqual(portfolio.perf_mult("intraday", PROF)[0], PROF["perf_mult"]["good"])
        for i in range(30):
            self.trade("b%d" % i, "swing", -1.0 if i % 3 else 0.5, -1.0 if i % 3 else 0.5)
        self.assertEqual(portfolio.perf_mult("swing", PROF)[0], PROF["perf_mult"]["bad"])
        for i in range(40):
            self.trade("p%d" % i, "scalp", 5.0, 3.0, mode="paper")     # paper не считается
        self.assertEqual(portfolio.perf_mult("scalp", PROF)[0], PROF["perf_mult"]["probation"])


class SessionTest(Base):
    def test_start_limits_and_single_active(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        r = session.start("scalp", "t", "platform", None, None, PROF, now=1000.0)
        self.assertAlmostEqual(r["budget_usdt"], 0.015 * 40.0)
        self.assertNotIn("expires", r)                                    # срока у сессии нет
        with self.assertRaises(SystemExit):
            session.start("intraday", "t", "platform", None, None, PROF, now=1001.0)

    def test_no_expiry_budget_and_max_trades(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        s = session.start("scalp", "t", "platform", None, None, PROF, now=1000.0)
        self.assertTrue(session.verdict(s, PROF, now=1100.0)[0])
        self.assertTrue(session.verdict(s, PROF, now=1000 + 100 * 3600)[0])       # сколько бы ни прошло, сессия идёт до stop
        self.trade("a", "scalp", -0.7, -1.0, sess=s["id"])               # бюджет 0.6
        ok, why = session.verdict(s, PROF, now=1100.0)
        self.assertFalse(ok)
        self.assertTrue(any("session_budget" in x for x in why))

    def test_unknown_type(self):
        with self.assertRaises(SystemExit):
            session.start("nope", "t", "platform", None, None, PROF, now=1000.0)


class GuardCliTest(Base):
    def run_guard(self, **kw):
        args = ["--sym", "BTC", "--side", "long", "--entry", "100", "--sl", kw.get("sl", "99.5"),
                "--tp", kw.get("tp", "101.5"), "--score", "0.8", "--contract-max-lev", "50"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            guard.main(args + kw.get("extra", []))
        return json.loads(buf.getvalue())

    def test_no_session_blocks(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        out = self.run_guard()
        self.assertFalse(out["ok"])
        self.assertIn("no_active_session", out["reasons"])

    def test_scalp_session_allows_tight_stop_blocks_wide(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        session.start("scalp", "t", "platform", None, None, PROF)
        ok = self.run_guard()
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["leverage"], 30)                           # плечо scalp по умолчанию
        self.assertEqual(ok["ctx"]["perf_mult"], PROF["perf_mult"]["probation"])
        wide = self.run_guard(sl="98.0", tp="104.0")                    # стоп 2% > 0.8% для scalp
        self.assertFalse(wide["ok"])

    def test_swing_leverage_cap_and_drawdown_halt(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        session.start("swing", "t", "platform", None, None, PROF)
        out = self.run_guard(sl="92.0", tp="118.0")                     # стоп 8%, тейк 18%: RR после издержек >= 2.0
        self.assertTrue(out["ok"], out)
        self.assertLessEqual(out["leverage"], 3)
        portfolio.set_venue("platform", 25.0, 15.0)                     # просадка от 40 > 20%
        halted = self.run_guard(sl="92.0", tp="118.0")
        self.assertFalse(halted["ok"])
        self.assertTrue(any("drawdown_halt" in x for x in halted["reasons"]))

    def test_scalp_net_rr_relaxed_but_intraday_not(self):
        portfolio.set_venue("platform", 40.0, 30.0)
        session.start("scalp", "t", "platform", None, None, PROF)
        ok = self.run_guard(sl="99.5", tp="101.2")                      # стоп 0.5%, тейк 1.2%: net RR 1.18 >= 1.0 (при 1.5 был бы отказ)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["leverage"], 30)
        low = self.run_guard(sl="99.5", tp="100.9")                     # net RR 0.8 < 1.0
        self.assertFalse(low["ok"])
        self.assertTrue(any("net_rr" in x for x in low["reasons"]))
        session.stop = None
        session._append({"ev": "stop", "id": session.active()["id"], "ts": "x", "note": ""})
        session.start("intraday", "t", "platform", None, None, PROF)
        self.assertFalse(self.run_guard(sl="98.0", tp="103.2")["ok"])   # стоп 2%, тейк 3.2%: net RR 1.28 < 1.5, intraday не смягчался


if __name__ == "__main__":
    unittest.main()
