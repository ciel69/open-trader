import argparse
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import guard, journal

PROF = guard.load_profile()
TODAY = "2026-10-27"
COST_PLATFORM = guard.round_trip_cost_pct(PROF, "platform")
COST_BYBIT = guard.round_trip_cost_pct(PROF, "bybit")


def args(**kw):
    d = dict(sym="SOL", side="long", entry=100.0, sl=98.0, tp=104.0, equity=40.0, free=30.0, score=1.0,
             regime="range", min_quote=1.0, contract_max_lev=50)
    d.update(kw)
    return argparse.Namespace(**d)


class Base(unittest.TestCase):
    def setUp(self):
        self.old = journal.PATH
        fd, journal.PATH = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(journal.PATH)

    def tearDown(self):
        if os.path.exists(journal.PATH):
            os.remove(journal.PATH)
        journal.PATH = self.old

    def open_pos(self, sym, side, risk, mode="live", tid=None):
        journal._append({"ev": "open", "id": tid or "%s-%s" % (sym, side), "sym": sym, "side": side, "mode": mode,
                         "risk_usdt": risk, "notional": 50, "entry": 1, "ts_open": "x"})


class CostTest(Base):
    def test_costs_values(self):
        self.assertAlmostEqual(COST_PLATFORM, 0.28, places=6)
        self.assertAlmostEqual(COST_BYBIT, 0.24, places=6)

    def test_net_rr_boundary(self):
        # стоп 2%, min_rr 1.5: нужен тейк >= 1.5*2 + 2.5*0.28 = 3.7% (платформа)
        self.assertFalse(guard.check(args(tp=103.6), PROF, TODAY)["ok"])
        r = guard.check(args(tp=103.8), PROF, TODAY)
        self.assertTrue(r["ok"], r)
        self.assertGreaterEqual(r["net_rr"], PROF["min_rr"])

    def test_gross_rr_ok_but_net_rr_rejected(self):
        # брутто RR 1.5 раньше проходил, теперь нет
        r = guard.check(args(tp=103.0), PROF, TODAY)
        self.assertFalse(r["ok"])
        self.assertTrue(any("net_rr" in x for x in r["reasons"]))

    def test_venue_costs_differ(self):
        # тейк 3.65%: на Bybit (0.24%) проходит, на платформе (0.28%) нет
        self.assertTrue(guard.check(args(tp=103.65), PROF, TODAY, ctx={"venue": "bybit"})["ok"])
        self.assertFalse(guard.check(args(tp=103.65), PROF, TODAY, ctx={"venue": "platform"})["ok"])

    def test_loss_at_stop_including_costs_within_budget(self):
        r = guard.check(args(tp=108.0), PROF, TODAY)
        self.assertTrue(r["ok"], r)
        budget = 40.0 * PROF["risk_per_trade_frac"]
        self.assertLessEqual(r["risk_usdt"], budget + 1e-6)
        # размер с издержками не больше размера без них
        no_cost = dict(PROF, costs={"platform": {"fee": 0.0, "slip": 0.0}})
        r0 = guard.check(args(tp=108.0), no_cost, TODAY)
        self.assertLessEqual(r["notional"], r0["notional"])


class CorrelationTest(Base):
    def test_same_side_blocked_when_open_risk_exceeds_cap(self):
        self.open_pos("ETH", "short", 1.8)                       # лимит 0.03*40 = 1.2
        r = guard.check(args(sym="SOL", side="short", sl=102.0, tp=92.0), PROF, TODAY)
        self.assertFalse(r["ok"])
        self.assertTrue(any("correlated_same_side" in x for x in r["reasons"]))

    def test_opposite_side_allowed(self):
        self.open_pos("ETH", "short", 1.8)
        self.assertTrue(guard.check(args(sym="SOL", side="long", tp=108.0), PROF, TODAY)["ok"])

    def test_outside_group_not_limited(self):
        self.open_pos("ETH", "short", 1.8)
        self.assertTrue(guard.check(args(sym="HYPE", side="short", sl=102.0, tp=92.0), PROF, TODAY)["ok"])

    def test_remaining_room_reduces_size(self):
        base = guard.check(args(tp=108.0), PROF, TODAY)
        self.open_pos("ETH", "long", 0.6)                        # осталось 0.6 из 1.2
        part = guard.check(args(sym="SOL", tp=108.0), PROF, TODAY)
        self.assertTrue(part["ok"], part)
        self.assertLessEqual(part["risk_usdt"], 0.6 + 1e-6)
        self.assertLess(part["notional"], base["notional"])

    def test_paper_and_testnet_do_not_count(self):
        self.open_pos("ETH", "short", 5.0, mode="paper", tid="p")
        self.open_pos("BTC", "short", 5.0, mode="testnet", tid="t")
        self.assertTrue(guard.check(args(sym="SOL", side="short", sl=102.0, tp=92.0), PROF, TODAY)["ok"])

    def test_limits_do_not_grow(self):
        # новые ключи только ограничивают: прежние лимиты профиля на месте
        # max_leverage профиля поднят до 30 по прямому указанию пользователя ради scalp; у остальных типов сессий потолки прежние
        for k, v in (("max_leverage", 30), ("risk_per_trade_frac", 0.03), ("max_open_positions", 2),
                     ("daily_loss_limit_usdt", 3.0), ("max_stop_pct", 3.0), ("min_rr", 1.5), ("max_consecutive_stops", 3)):
            self.assertEqual(PROF[k], v)
        st = PROF["session_types"]
        self.assertEqual((st["intraday"]["max_leverage"], st["swing"]["max_leverage"], st["position"]["max_leverage"]), (10, 3, 2))


class ScalpLeverageTest(Base):
    def test_default_leverage_used_and_capped(self):
        a = args(sl=99.5, tp=101.5, contract_max_lev=50)               # стоп 0.5%, тейк 1.5%: проходит после издержек
        r = guard.check(a, PROF, TODAY, ctx={"lev_default": 30})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["leverage"], 30)
        low = guard.check(a, PROF, TODAY, ctx={})
        self.assertLess(low["leverage"], 30)                           # без предпочтения плечо минимально необходимое
        self.assertLess(r["margin"], low["margin"])                    # при том же риске маржа меньше
        self.assertAlmostEqual(r["risk_usdt"], low["risk_usdt"], delta=0.05)
        cap = guard.check(args(sl=99.5, tp=101.5, contract_max_lev=25), PROF, TODAY, ctx={"lev_default": 30})
        self.assertEqual(cap["leverage"], 25)                          # потолок контракта выше предпочтения
        wide = guard.check(args(sl=97.0, tp=106.5, contract_max_lev=50), PROF, TODAY, ctx={"lev_default": 30})
        self.assertLessEqual(wide["leverage"], int(100.0 / (PROF["liq_factor"] * 3.0 + PROF["maintenance_margin_pct"])))  # ликвидация дальше стопа


if __name__ == "__main__":
    unittest.main()
