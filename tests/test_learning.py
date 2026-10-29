import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import guard, journal, learning, portfolio, session

PROF = guard.load_profile()


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.old = (journal.PATH, portfolio.PATH, session.PATH, learning.PATH)
        journal.PATH, portfolio.PATH, session.PATH, learning.PATH = (os.path.join(d, x) for x in ("t.jsonl", "p.json", "s.jsonl", "l.json"))
        portfolio.set_venue("bybit", 10.0, 10.0)

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH, learning.PATH = self.old

    def trade(self, tid, pnl=None, risk=0.2, venue="bybit", mode="live", opened="2999-01-01T00:00:00Z"):
        journal._append({"ev": "open", "id": tid, "sym": "SOL", "side": "long", "mode": mode, "venue": venue, "risk_usdt": risk,
                         "notional": 20, "entry": 1, "ts_open": opened})
        if pnl is not None:
            journal._append({"ev": "close", "id": tid, "ts_close": opened, "pnl_usdt": pnl, "reason": "manual", "fees_usdt": 0.01})


class ProgressTest(Base):
    def test_inactive_is_none_and_start_twice_refused(self):
        self.assertIsNone(learning.progress())
        learning.start(30, 1.5, "bybit", now=0)
        self.assertIsNotNone(learning.progress())
        with self.assertRaises(SystemExit):
            learning.start(30, 1.5, "bybit", now=0)

    def test_counts_only_live_bybit_since_start(self):
        learning.start(5, 1.5, "bybit", now=1000)
        self.trade("old", -0.5, opened="1970-01-01T00:00:00Z")          # до старта: не в счёте
        self.trade("tn", -0.5, mode="testnet")
        self.trade("pl", -0.5, venue="platform")
        self.trade("a", -0.3)
        self.trade("b", 0.1)
        self.trade("c", None, risk=0.25)                                 # открытая
        p = learning.progress()
        self.assertEqual((p["trades"], p["closed"], p["open"]), (3, 2, 1))
        self.assertAlmostEqual(p["realized"], -0.2)
        self.assertAlmostEqual(p["room"], 1.5 - 0.2 - 0.25)

    def test_limits_reasons(self):
        learning.start(2, 1.0, "bybit", now=0)
        self.trade("a", -0.6)
        self.trade("b", -0.5)
        p = learning.progress()
        self.assertEqual(len(p["reasons"]), 2)                           # сделок 2 из 2 и убыток 1.1 >= 1.0


class GuardTest(Base):
    def run_guard(self, **kw):
        import argparse
        a = argparse.Namespace(sym="SOL", side="long", entry=100.0, sl=99.0, tp=102.2, equity=None, free=None, score=1.0,
                               regime="range", min_quote=0.0, contract_max_lev=50, profile=None, no_session=False)
        return guard.evaluate(a)

    def test_guard_blocks_at_trade_limit_and_loss_limit(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        learning.start(2, 1.5, "bybit", now=0)
        self.assertTrue(self.run_guard()["ok"])
        self.trade("a", -0.1)
        self.trade("b", -0.1)
        r = self.run_guard()
        self.assertFalse(r["ok"])
        self.assertTrue(any("learning_mode: сделок" in x for x in r["reasons"]))

    def test_remaining_loss_room_caps_trade_risk(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        learning.start(30, 1.5, "bybit", now=0)
        big = self.run_guard()["risk_usdt"]
        self.trade("a", -1.4)                                             # остаток 0.1
        r = self.run_guard()
        self.assertTrue(r["ok"], r)
        self.assertLessEqual(r["risk_usdt"], 0.1 + 1e-6)
        self.assertLess(r["risk_usdt"], big)
        self.trade("b", -0.2)
        self.assertFalse(self.run_guard()["ok"])

    def test_other_venue_and_inactive_unaffected(self):
        portfolio.set_venue("platform", 30.0, 20.0)
        session.start("intraday", "t", "platform", None, None, PROF)
        learning.start(1, 1.5, "bybit", now=0)
        self.trade("a", -0.1)
        self.assertTrue(self.run_guard()["ok"])


if __name__ == "__main__":
    unittest.main()
