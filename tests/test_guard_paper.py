import argparse

import _bootstrap  # noqa: F401
from _wp9 import TmpState
from tb.core import guard, journal, portfolio


def args(**kw):
    d = dict(sym="BTC", side="long", entry=100.0, sl=98.0, tp=104.0, equity=100.0, free=None, score=1.0, regime="range",
             min_quote=1.0, contract_max_lev=50, paper=True, no_session=False)
    d.update(kw)
    return argparse.Namespace(**d)


class PaperTest(TmpState):
    def test_paper_ok_without_session(self):
        r = guard.evaluate(args())
        self.assertTrue(r["ok"], r["reasons"])
        self.assertTrue(r["ctx"]["paper"])
        self.assertEqual(r["ctx"]["risk_mult"], 1.0)
        prof = guard.load_profile()
        self.assertLessEqual(r["margin"], prof["margin_per_trade_frac"] * 100.0 + 1e-9)
        self.assertLessEqual(r["risk_usdt"], 100.0 * prof["risk_per_trade_frac"] + 1e-6)

    def test_same_args_live_needs_session(self):
        r = guard.evaluate(args(paper=False))
        self.assertFalse(r["ok"])
        self.assertIn("no_active_session", r["reasons"])

    def test_paper_geometry_kept(self):
        self.assertFalse(guard.evaluate(args(sl=None))["ok"])
        self.assertFalse(guard.evaluate(args(sl=105.0))["ok"])
        self.assertFalse(guard.evaluate(args(tp=100.5))["ok"])          # net RR
        self.assertFalse(guard.evaluate(args(sl=90.0, tp=140.0))["ok"])  # стоп 10% > max_stop_pct

    def test_paper_ignores_slots_limits_and_live_journal(self):
        for i in range(3):
            journal._append({"ev": "open", "id": "L%d" % i, "ts_open": "2026-10-28T01:00:00Z", "sym": "ETH%d" % i, "side": "long", "entry": 10.0,
                             "lev": 5, "margin": 5.0, "notional": 25.0, "sl": 9.0, "tp": 12.0, "risk_usdt": 9.0, "mode": "live"})
        self.assertTrue(guard.evaluate(args(sym="ETH0"))["ok"])        # символ занят live-позицией и лимиты исчерпаны: paper не мешает
        r = guard.check(args(sym="ETH0", paper=False), guard.load_profile())
        self.assertFalse(r["ok"])

    def test_paper_stype_limits(self):
        r = guard.evaluate(args(stype="scalp", sl=98.0, tp=104.0))      # стоп 2% > scalp 0.8%
        self.assertFalse(r["ok"])

    def test_leverage_ceiling(self):
        r = guard.evaluate(args(contract_max_lev=3))
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["leverage"], 3)
