import argparse
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import guard
from tb.core import journal

PROF = guard.load_profile()
TODAY = "2026-10-27"


def args(**kw):
    d = dict(sym="BTC", side="long", entry=100.0, sl=98.0, tp=104.0, equity=30.0, free=20.0, score=0.7,
             regime="range", min_quote=1.0, contract_max_lev=50)
    d.update(kw)
    return argparse.Namespace(**d)


def rec(ev, **kw):
    r = {"ev": ev}
    r.update(kw)
    journal._append(r)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.old = journal.PATH
        fd, journal.PATH = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(journal.PATH)

    def tearDown(self):
        if os.path.exists(journal.PATH):
            os.remove(journal.PATH)
        journal.PATH = self.old

    def test_ok_and_within_profile(self):
        r = guard.check(args(), PROF, TODAY)
        self.assertTrue(r["ok"], r)
        self.assertLessEqual(r["leverage"], PROF["max_leverage"])
        self.assertLessEqual(r["margin"], PROF["margin_per_trade_max"])
        self.assertLessEqual(r["risk_usdt"], 30.0 * PROF["risk_per_trade_frac"] + 1e-9)
        self.assertGreater(r["liq_dist_pct_est"], r["stop_pct"] * 2)

    def test_requires_stop_and_tp(self):
        self.assertFalse(guard.check(args(sl=None), PROF, TODAY)["ok"])
        self.assertFalse(guard.check(args(tp=None), PROF, TODAY)["ok"])

    def test_wrong_side_and_wide_stop_and_rr(self):
        self.assertFalse(guard.check(args(sl=102.0), PROF, TODAY)["ok"])
        self.assertFalse(guard.check(args(sl=95.0, tp=110.0), PROF, TODAY)["ok"])  # стоп 5% > 3%
        self.assertFalse(guard.check(args(tp=101.0), PROF, TODAY)["ok"])  # RR 0.5

    def test_short_levels(self):
        self.assertTrue(guard.check(args(side="short", sl=102.0, tp=96.0), PROF, TODAY)["ok"])
        self.assertFalse(guard.check(args(side="short", sl=98.0, tp=96.0), PROF, TODAY)["ok"])

    def test_daily_loss_limit_halts(self):
        rec("open", id="a", sym="ETH", side="long", mode="live", risk_usdt=1.0, notional=50, entry=1, ts_open="x")
        rec("close", id="a", ts_close=TODAY + "T10:00:00Z", pnl_usdt=-3.1, reason="manual")
        self.assertFalse(guard.check(args(), PROF, TODAY)["ok"])

    def test_three_stops_halt(self):
        for i in range(3):
            rec("open", id=str(i), sym="S%d" % i, side="long", mode="live", risk_usdt=0.5, notional=50, entry=1, ts_open="x")
            rec("close", id=str(i), ts_close=TODAY + "T1%d:00:00Z" % i, pnl_usdt=-0.3, reason="sl")
        r = guard.check(args(), PROF, TODAY)
        self.assertFalse(r["ok"])
        self.assertTrue(any("stops in a row" in x for x in r["reasons"]))

    def test_open_risk_reduces_room_and_blocks_same_symbol(self):
        rec("open", id="o", sym="BTC", side="long", mode="live", risk_usdt=0.5, notional=50, entry=1, ts_open="x")
        self.assertFalse(guard.check(args(sym="BTC"), PROF, TODAY)["ok"])
        self.assertTrue(guard.check(args(sym="SOL"), PROF, TODAY)["ok"])

    def test_max_open_positions(self):
        for s in ("A", "B"):
            rec("open", id=s, sym=s, side="long", mode="live", risk_usdt=0.1, notional=10, entry=1, ts_open="x")
        self.assertFalse(guard.check(args(sym="C"), PROF, TODAY)["ok"])

    def test_paper_trades_do_not_count(self):
        rec("open", id="p", sym="BTC", side="long", mode="paper", risk_usdt=0.5, notional=50, entry=1, ts_open="x")
        self.assertTrue(guard.check(args(sym="BTC"), PROF, TODAY)["ok"])

    def test_reserve_and_min_quote(self):
        self.assertFalse(guard.check(args(free=8.0), PROF, TODAY)["ok"])
        self.assertFalse(guard.check(args(free=8.5, min_quote=2.0), PROF, TODAY)["ok"])

    def test_storm_blocked(self):
        self.assertFalse(guard.check(args(regime="storm"), PROF, TODAY)["ok"])


if __name__ == "__main__":
    unittest.main()
