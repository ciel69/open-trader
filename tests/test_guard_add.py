import argparse

import _bootstrap  # noqa: F401
from _wp9 import TmpState
from tb.core import guard, journal, portfolio

F = 0.0012     # fee + slip bybit


def add_args(**kw):
    d = dict(sym="BTC", side="long", px=105.0, stop=101.0, qty_open=1.0, avg_open=100.0, plan_risk0=0.5, risk_add_cap=None, score=0.7,
             contract_max_lev=50, exec="taker", venue="bybit", no_session=True, pyramid_enabled=True, realized_net=0.0)
    d.update(kw)
    return argparse.Namespace(**d)


class AddTest(TmpState):
    def setUp(self):
        TmpState.setUp(self)
        portfolio.set_venue("bybit", 100.0, 80.0)        # margin_per_trade_max = 20

    def test_ok_sizing_within_plan_risk(self):
        r = guard.evaluate_add(add_args())
        self.assertTrue(r["ok"], r["reasons"])
        q = r["qty_max"]
        self.assertGreater(q, 0)
        # худший исход плана после добора при стопе не хуже -R0
        avg = (100.0 + 105.0 * q) / (1.0 + q)
        w = 1.0 * (101.0 - 100.0) - F * 101.0 * 1.0 + q * (101.0 - 105.0) - F * (105.0 + 101.0) * q
        self.assertGreaterEqual(w, -0.5 - 1e-9)
        self.assertLessEqual(r["margin_total"], 20.0 + 1e-9)
        self.assertGreaterEqual(r["leverage"], 1)
        self.assertGreater(avg, 100.0)

    def test_disabled_by_default_flag(self):
        r = guard.evaluate_add(add_args(pyramid_enabled=False))
        self.assertFalse(r["ok"])
        self.assertIn("pyramid_disabled", r["reasons"])
        a = add_args()
        del a.pyramid_enabled           # флаг не задан: exec_limits.json -> false
        self.assertFalse(guard.evaluate_add(a)["ok"])

    def test_not_in_profit_refused(self):
        r = guard.evaluate_add(add_args(px=100.3, stop=100.2))   # +0.3 при R0=0.5 -> 0.6R
        self.assertFalse(r["ok"])
        self.assertTrue(any(w.startswith("ADD_NOT_IN_PROFIT") for w in r["reasons"]))
        r = guard.evaluate_add(add_args(px=98.0, stop=97.0))      # в убытке (усреднение)
        self.assertFalse(r["ok"])

    def test_stop_not_breakeven_refused(self):
        r = guard.evaluate_add(add_args(stop=99.0))
        self.assertTrue(any(w.startswith("ADD_STOP_NOT_BE") for w in r["reasons"]))
        r = guard.evaluate_add(add_args(stop=100.1))              # меньше 2*f*avg = 0.24
        self.assertFalse(r["ok"])

    def test_short(self):
        r = guard.evaluate_add(add_args(side="short", px=95.0, stop=99.0))
        self.assertTrue(r["ok"], r["reasons"])
        r = guard.evaluate_add(add_args(side="short", px=95.0, stop=101.0))
        self.assertFalse(r["ok"])

    def test_margin_limit(self):
        r = guard.evaluate_add(add_args(qty_open=8.0, avg_open=100.0, plan_risk0=5.0, px=105.0, stop=101.0))
        # маржа 20 x плечо <=30 = 600 номинала, уже занято 800: места нет
        self.assertFalse(r["ok"])
        self.assertTrue(any(w.startswith("ADD_TOO_SMALL") for w in r["reasons"]))

    def test_margin_caps_size(self):
        r = guard.evaluate_add(add_args(qty_open=4.0, plan_risk0=10.0, risk_add_cap=10.0, px=110.0, stop=104.0))
        self.assertTrue(r["ok"], r["reasons"])
        self.assertLessEqual(r["margin_total"], 20.0 + 1e-9)
        self.assertLessEqual(r["leverage"], 30)

    def test_lot_step_and_min(self):
        r = guard.evaluate_add(add_args(qty_step=0.1))
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["qty_max"] / 0.1, round(r["qty_max"] / 0.1), places=6)
        r = guard.evaluate_add(add_args(min_qty=100.0))
        self.assertFalse(r["ok"])

    def test_daily_loss_and_streak(self):
        for i in range(3):
            journal._append({"ev": "open", "id": "S%d" % i, "ts_open": "2026-10-28T01:00:00Z", "sym": "X%d" % i, "side": "long", "entry": 1.0, "lev": 1,
                             "margin": 1.0, "notional": 1.0, "sl": 0.9, "tp": 1.2, "risk_usdt": 0.1, "mode": "live"})
            journal._append({"ev": "close", "id": "S%d" % i, "ts_close": "%sT0%d:00:00Z" % (guard.time.strftime("%Y-%m-%d", guard.time.gmtime()), i + 1),
                             "exit": 0.9, "reason": "sl", "pnl_usdt": -0.5, "fees_usdt": 0.0, "r": -1.0})
        r = guard.evaluate_add(add_args())
        self.assertFalse(r["ok"])
        self.assertTrue(any("stops in a row" in w for w in r["reasons"]))

    def test_drawdown_halt(self):
        portfolio.set_venue("bybit", 100.0, 80.0, {"venues": {}, "hwm": 200.0})
        r = guard.evaluate_add(add_args())
        self.assertFalse(r["ok"])
        self.assertTrue(any(w.startswith("drawdown_halt") for w in r["reasons"]))

    def test_session_required_unless_flag(self):
        r = guard.evaluate_add(add_args(no_session=False))
        self.assertIn("no_active_session", r["reasons"])

    def test_realized_loss_shrinks_room(self):
        a = guard.evaluate_add(add_args(realized_net=0.0))
        b = guard.evaluate_add(add_args(realized_net=-0.3, px=106.0))
        self.assertTrue(a["ok"])
        if b["ok"]:
            self.assertLess(b["qty_max"], a["qty_max"])

    def test_day_state_counts_losing_parts(self):
        today = guard.time.strftime("%Y-%m-%d", guard.time.gmtime())
        journal._append({"ev": "open", "id": "P", "ts_open": today + "T01:00:00Z", "sym": "BTC", "side": "long", "entry": 100.0, "lev": 5,
                         "margin": 10.0, "notional": 50.0, "sl": 98.0, "tp": 106.0, "risk_usdt": 1.0, "mode": "live"})
        journal._append({"ev": "part", "id": "P", "ts": today + "T02:00:00Z", "px": 99.0, "qty": 0.25, "fee": 0.01, "pnl": -0.25})
        st = guard.day_state(guard.load_profile(), today)
        self.assertAlmostEqual(st["realized_today"], -0.26, places=4)
        self.assertEqual(st["open_risk"], 1.0)      # резерв R0 плана не уменьшается
