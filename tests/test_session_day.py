"""Сессия-день (`day`): контейнер дня (бюджет/лимит сделок/срок как у intraday), горизонт каждой сделки выбирает агент: guard берёт геометрию
(max_stop_pct, min_rr, плечо) по горизонту плана (a.stype), сессия одного горизонта (CLI) по-прежнему задаёт один горизонт на все входы."""
import argparse
import json
import os
import unittest

import _bootstrap  # noqa: F401
from _wp9 import TmpState
from tb.core import guard, portfolio, session

PROF = guard.load_profile()


def args(**kw):
    d = dict(sym="DOOD", side="long", entry=100.0, sl=99.5, tp=101.6, equity=None, free=None, score=0.7, regime="range", min_quote=1.0, contract_max_lev=50,
             no_session=False)
    d.update(kw)
    return argparse.Namespace(**d)


class DayCfg(unittest.TestCase):
    def test_cfg_of_day_is_intraday_container(self):
        c = session.cfg_of("day", PROF)
        it = PROF["session_types"]["intraday"]
        self.assertEqual(c, {"max_minutes": it["max_minutes"], "budget_frac": it["budget_frac"], "max_trades": it["max_trades"]})
        self.assertNotIn("max_stop_pct", c)                                        # геометрии у дня нет: она у горизонта плана
        over = dict(PROF, day_session={"max_minutes": 600, "budget_frac": 0.02, "max_stop_pct": 9})
        c2 = session.cfg_of("day", over)
        self.assertEqual((c2["max_minutes"], c2["budget_frac"], c2["max_trades"]), (600, 0.02, it["max_trades"]))
        self.assertEqual(session.cfg_of("scalp", PROF), PROF["session_types"]["scalp"])
        self.assertTrue(session.known_type("day", PROF))
        self.assertFalse(session.known_type("week", PROF))

    def test_geometry_type(self):
        day = {"type": "day"}
        self.assertEqual(session.geometry_type(day, "scalp"), "scalp")
        self.assertEqual(session.geometry_type(day, None), "intraday")
        self.assertEqual(session.geometry_type(day, "bogus"), "intraday")
        self.assertEqual(session.geometry_type({"type": "scalp"}, "swing"), "scalp")      # сессия одного горизонта: тип сессии
        self.assertEqual(session.geometry_type(None, "swing"), "swing")


class DayGuard(TmpState):
    def setUp(self):
        TmpState.setUp(self)
        with open(portfolio.PATH, "w", encoding="utf-8") as f:
            json.dump({"venues": {"bybit": {"equity": 40.0, "free": 40.0, "ts": "2026-10-28T10:00:00Z"}}, "hwm": 40.0}, f)

    def test_day_session_starts_with_intraday_limits(self):
        rec = session.start("day", "торговый день", "bybit", 360, None, PROF, now=1_000_000.0)
        self.assertTrue(rec["id"].startswith("day-"))
        self.assertAlmostEqual(rec["budget_usdt"], round(PROF["session_types"]["intraday"]["budget_frac"] * 40.0, 4))
        self.assertNotIn("expires", rec)                                               # срока у сессии нет
        self.assertEqual(session.verdict(session.active(), PROF, now=1_000_000.0 + 10)[0], True)
        self.assertEqual(session.verdict(session.active(), PROF, now=1_000_000.0 + 100 * 3600)[0], True)

    def test_guard_geometry_by_plan_horizon_under_day_session(self):
        session.start("day", "торговый день", "bybit", 360, None, PROF)
        r = guard.evaluate(args(stype="scalp"))                                         # стоп 0.5% <= 0.8% scalp, RR net >= 1.0
        self.assertTrue(r["ok"], r["reasons"])
        self.assertLessEqual(r["leverage"], PROF["session_types"]["scalp"]["max_leverage"])
        r = guard.evaluate(args(stype="swing", sl=92.0, tp=130.0))                      # стоп 8% допустим для swing (15%), плечо не выше 3
        self.assertTrue(r["ok"], r["reasons"])
        self.assertLessEqual(r["leverage"], PROF["session_types"]["swing"]["max_leverage"])
        r = guard.evaluate(args(stype="scalp", sl=98.0, tp=106.0))                      # 2% шире 0.8% scalp
        self.assertFalse(r["ok"])
        self.assertTrue(any(w.startswith("stop ") for w in r["reasons"]), r["reasons"])
        r = guard.evaluate(args(sl=98.0, tp=106.0))                                     # без горизонта: intraday (3%)
        self.assertTrue(r["ok"], r["reasons"])

    def test_single_horizon_session_ignores_plan_horizon(self):
        session.start("scalp", "скальп", "bybit", 15, None, PROF)
        r = guard.evaluate(args(stype="swing", sl=98.0, tp=106.0))                      # сессия scalp: стоп 2% > 0.8%, горизонт плана не меняет геометрию
        self.assertFalse(r["ok"])
