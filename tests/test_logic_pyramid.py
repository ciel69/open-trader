"""WP3: пирамидинг. worst_case_pnl и check_add (все ветки отказа P1-P9, граница W = -R0, округление лота)."""
import copy
import unittest

import _bootstrap  # noqa: F401
from _logic_helpers import T0, view, long_plan, short_plan, managing_rt
from tb.logic import pyramid as P

F = 0.0011                      # fee 0.09% + slip 0.02%
LIM = {"pyramid": {"enabled": True, "min_r": 1.0, "max_adds": 1, "max_add_risk_frac": 1.0, "min_gap_s": 60},
       "connectivity": {"public_price_stale_s": 10}}
RULE = {"id": "pyr", "when": {"all": [{"kind": "pnl_r", "gte": 1.5}, {"kind": "price_in_zone", "low": 1, "high": 1e9}]},
        "do": {"action": "add", "risk_frac": 0.5, "how": "market"}}
GUARD = {"ok": True, "qty_max": 100.0}


def rt_long(stop=100.5, **kw):
    rt = managing_rt("long", 100.0, stop=stop, r=1.0)
    rt["pos"]["risk0"] = 1.2
    rt["realized"] = {"pnl": 0.0, "fees": 0.09, "parts": []}
    rt.update(kw)
    return rt


_D = object()


def check(plan=_D, rt=_D, v=_D, lim=_D, rule=RULE, price=102.0, guard=GUARD):
    return P.check_add(long_plan() if plan is _D else plan, rt_long() if rt is _D else rt, view(102.0) if v is _D else v,
                       LIM if lim is _D else lim, rule, price, guard)


class WorstCaseTest(unittest.TestCase):
    def test_formula_long(self):
        rt = {"pos": {"qty": 2.0, "avg": 100.0}, "realized": {"pnl": 0.0, "fees": 0.2}}
        self.assertAlmostEqual(P.worst_case_pnl(rt, 99.0, 0.09, 0.0, "long"), -0.2 - 2.0 - 0.0009 * 99.0 * 2.0)

    def test_formula_short(self):
        rt = {"pos": {"qty": 2.0, "avg": 100.0}, "realized": {"pnl": 0.0, "fees": 0.0}}
        self.assertAlmostEqual(P.worst_case_pnl(rt, 101.0, 0.09, 0.02, -1), -2.0 - F * 101.0 * 2.0)

    def test_profit_locked_stop_gives_positive(self):
        rt = {"pos": {"qty": 1.0, "avg": 100.0}, "realized": {"pnl": 0.5, "fees": 0.1}}
        self.assertAlmostEqual(P.worst_case_pnl(rt, 101.0, 0.09, 0.02, "long"), 0.4 + 1.0 - F * 101.0)

    def test_no_position_returns_realized(self):
        self.assertAlmostEqual(P.worst_case_pnl({"realized": {"pnl": 1.0, "fees": 0.25}}, 99.0, 0.09, 0.0, "long"), 0.75)
        self.assertEqual(P.worst_case_pnl({}, 99.0, 0.09, 0.0, "long"), 0.0)

    def test_zero_costs(self):
        rt = {"pos": {"qty": 3.0, "avg": 50.0}, "realized": {}}
        self.assertAlmostEqual(P.worst_case_pnl(rt, 49.0, 0, 0, "long"), -3.0)


class CheckAddOkTest(unittest.TestCase):
    def test_sizing_formula(self):
        r = check()
        self.assertTrue(r["ok"], r)
        lu = 1.5 + F * (102.0 + 100.5)
        W = -0.09 + 0.5 - F * 100.5
        A, B = 0.5 * 1.2 / lu, (W + 1.2) / lu
        self.assertLess(A, B)
        self.assertAlmostEqual(r["qty_max"], A)
        self.assertAlmostEqual(r["risk_add"], A * lu)
        self.assertEqual(r["codes"], [])

    def test_worst_case_after_add_not_below_minus_r0(self):
        # реализованный минус делает B главным ограничением: W(S_после) == -R0
        rt = rt_long()
        rt["realized"] = {"pnl": -0.5, "fees": 0.09, "parts": []}
        r = check(rt=rt, rule={"do": {"risk_frac": 1.0}})
        self.assertTrue(r["ok"], r)
        lu = 1.5 + F * 202.5
        W = P.worst_case_pnl(rt, 100.5, 0.09, 0.02, "long")
        self.assertLess(W, 0)
        self.assertAlmostEqual(r["qty_max"], (W + 1.2) / lu)
        self.assertAlmostEqual(W - r["qty_max"] * lu, -1.2)            # ровно на границе -R0

    def test_floor_to_step_never_exceeds_limit(self):
        lim = dict(LIM, qty_step=0.1, min_qty=0.1)
        r = check(lim=lim)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["qty_max"], 0.3)
        lu = 1.5 + F * 202.5
        self.assertLess(r["qty_max"], 0.6 / lu + 1e-9 + 0.1)
        self.assertAlmostEqual(r["risk_add"], 0.3 * lu)

    def test_guard_qty_max_caps(self):
        r = check(guard={"ok": True, "qty_max": 0.2})
        self.assertAlmostEqual(r["qty_max"], 0.2)

    def test_risk_frac_capped_by_limits(self):
        lim = copy.deepcopy(LIM)
        lim["pyramid"]["max_add_risk_frac"] = 0.25
        r = check(lim=lim)
        lu = 1.5 + F * 202.5
        self.assertAlmostEqual(r["qty_max"], 0.25 * 1.2 / lu)
        r2 = check(rule={"do": {"risk_frac": 7.0}})                      # >1 режется до 1
        self.assertAlmostEqual(r2["qty_max"], min(1.2 / lu, (-0.09 + 0.5 - F * 100.5 + 1.2) / lu))

    def test_short(self):
        rt = managing_rt("short", 100.0, stop=99.5, r=1.0)
        rt["pos"]["risk0"] = 1.2
        rt["realized"] = {"pnl": 0.0, "fees": 0.09, "parts": []}
        r = P.check_add(short_plan(), rt, view(98.0), LIM, RULE, 98.0, GUARD)
        self.assertTrue(r["ok"], r)
        lu = 1.5 + F * (98.0 + 99.5)
        self.assertAlmostEqual(r["qty_max"], 0.5 * 1.2 / lu)

    def test_r0_fallback_from_first_entry(self):
        rt = rt_long()
        del rt["pos"]["risk0"]
        rt["pos"]["entries"] = [{"px": 100.0, "qty": 1.0, "fee": 0.09, "kind": "entry"}]
        r = check(rt=rt)
        self.assertTrue(r["ok"], r)
        r0 = 1.0 * (1.0 + F * 199.0)
        lu = 1.5 + F * 202.5
        self.assertAlmostEqual(r["qty_max"], 0.5 * r0 / lu)

    def test_guard_plan_risk0_has_priority(self):
        r = check(guard={"ok": True, "qty_max": 100.0, "plan_risk0": 2.4})
        lu = 1.5 + F * 202.5
        self.assertAlmostEqual(r["qty_max"], 0.5 * 2.4 / lu)


class CheckAddRefusalsTest(unittest.TestCase):
    def codes(self, **kw):
        r = check(**kw)
        self.assertFalse(r["ok"], r)
        self.assertEqual((r["qty_max"], r["risk_add"]), (0.0, 0.0))
        return r["codes"]

    def test_disabled_default(self):
        lim = copy.deepcopy(LIM)
        lim["pyramid"]["enabled"] = False
        self.assertEqual(self.codes(lim=lim), ["ADD_DISABLED"])
        self.assertEqual(self.codes(lim={}), ["ADD_DISABLED"])
        self.assertEqual(self.codes(lim=None), ["ADD_DISABLED"])

    def test_not_managing(self):
        self.assertEqual(self.codes(rt=rt_long(status="entered")), ["NOT_MANAGING"])
        self.assertEqual(self.codes(rt=rt_long(status="closed")), ["NOT_MANAGING"])
        self.assertEqual(self.codes(rt={"status": "managing"}), ["NOT_MANAGING"])

    def test_hold_kill(self):
        self.assertIn("HOLD", self.codes(rt=rt_long(hold="reconciling")))
        lim = dict(LIM, kill=True)
        self.assertIn("KILL", self.codes(lim=lim))
        self.assertIn("KILL", self.codes(guard={"ok": True, "qty_max": 5, "kill": True}))

    def test_max_adds(self):
        self.assertIn("MAX_ADDS", self.codes(rt=rt_long(adds={"n": 1, "risk_used": 0.5})))
        lim = copy.deepcopy(LIM)
        lim["pyramid"]["max_adds"] = 2
        self.assertTrue(check(rt=rt_long(adds={"n": 1}), lim=lim)["ok"])

    def test_not_profit_boundary(self):
        self.assertIn("NOT_PROFIT", self.codes(v=view(100.99), price=100.99))
        # ровно +1R проходит (min_r = 1.0), но правило требует 1.5: порог берётся наибольший
        self.assertIn("NOT_PROFIT", self.codes(v=view(101.0), price=101.0))
        self.assertTrue(check(v=view(101.5), price=101.5)["ok"])
        lone = {"when": {"kind": "pnl_r", "gte": 0.2}, "do": {"risk_frac": 0.5}}
        self.assertIn("NOT_PROFIT", self.codes(v=view(100.99), price=100.99, rule=lone))        # min_r из лимитов сильнее правила
        self.assertTrue(check(v=view(101.0), price=101.0, rule=lone)["ok"])
        self.assertIn("NOT_PROFIT", self.codes(v=view(99.0), price=99.0))                       # в убытке никогда

    def test_stop_not_breakeven(self):
        self.assertIn("STOP_NOT_BE", self.codes(rt=rt_long(stop=99.0)))                          # стоп начальный
        self.assertIn("STOP_NOT_BE", self.codes(rt=rt_long(stop=100.0)))                         # ровно на входе без комиссий
        self.assertIn("STOP_NOT_BE", self.codes(rt=rt_long(stop=100.2199)))
        self.assertTrue(check(rt=rt_long(stop=100.2201))["ok"])                                  # >= 2*f*avg = 0.22
        rt = rt_long()
        rt["pos"]["cur_stop"] = None
        self.assertIn("STOP_NOT_BE", self.codes(rt=rt))

    def test_stop_not_breakeven_short(self):
        rt = managing_rt("short", 100.0, stop=100.0, r=1.0)
        rt["pos"]["risk0"] = 1.2
        r = P.check_add(short_plan(), rt, view(98.0), LIM, RULE, 98.0, GUARD)
        self.assertIn("STOP_NOT_BE", r["codes"])
        rt["pos"]["cur_stop"] = 99.7
        self.assertTrue(P.check_add(short_plan(), rt, view(98.0), LIM, RULE, 98.0, GUARD)["ok"])

    def test_stop_side(self):
        self.assertIn("STOP_SIDE", self.codes(price=100.5))                                       # цена добора на стопе

    def test_min_gap(self):
        self.assertIn("MIN_GAP", self.codes(rt=rt_long(adds={"n": 0, "last_ts": T0 - 59}), v=view(102.0, now=T0)))
        self.assertTrue(check(rt=rt_long(adds={"n": 0, "last_ts": T0 - 60}), v=view(102.0, now=T0))["ok"])

    def test_pending_action(self):
        self.assertIn("PENDING", self.codes(rt=rt_long(actions={"seq": 3, "pending": ["a-12-003"]})))

    def test_stale_price(self):
        self.assertIn("STALE", self.codes(v=view(102.0, ts=T0 - 11, now=T0)))
        self.assertIn("STALE", self.codes(v=view(None)))

    def test_guard(self):
        self.assertIn("GUARD", self.codes(guard=None))
        self.assertIn("GUARD", self.codes(guard={"ok": False, "reasons": ["margin_total"]}))

    def test_risk_cap_when_budget_exhausted(self):
        rt = rt_long()
        rt["realized"] = {"pnl": -2.0, "fees": 0.09, "parts": []}                                 # W + R0 <= 0
        self.assertIn("RISK_CAP", self.codes(rt=rt))

    def test_min_size(self):
        lim = dict(LIM, qty_step=1.0, min_qty=1.0)
        self.assertIn("MIN_SIZE", self.codes(lim=lim))                                             # 0.35 -> 0 при шаге 1
        self.assertIn("MIN_SIZE", self.codes(lim=dict(LIM, min_qty=0.5)))
        self.assertIn("MIN_SIZE", self.codes(guard={"ok": True, "qty_max": 0.0}))
        self.assertIn("MIN_SIZE", self.codes(guard={"ok": True, "qty_max": 0.05, "qty_step": 0.1, "min_qty": 0.1}))

    def test_codes_collected_and_unique_and_known(self):
        r = check(rt=rt_long(stop=99.0, hold="x", adds={"n": 1}), v=view(100.1, ts=T0 - 99, now=T0), price=100.1, guard=None)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["codes"]), len(set(r["codes"])))
        for c in r["codes"]:
            self.assertIn(c, P.ADD_CODES)
        for c in ("HOLD", "MAX_ADDS", "NOT_PROFIT", "STOP_NOT_BE", "STALE", "GUARD"):
            self.assertIn(c, r["codes"])

    def test_no_data_without_r0(self):
        rt = rt_long()
        del rt["pos"]["risk0"]
        rt["pos"]["entries"] = []
        self.assertIn("NO_DATA", self.codes(rt=rt))


class PurityTest(unittest.TestCase):
    def test_deterministic_no_mutation(self):
        v = view(102.0)
        args = (long_plan(), rt_long(), v, copy.deepcopy(LIM), copy.deepcopy(RULE), 102.0, dict(GUARD))
        before = copy.deepcopy(args[:2] + args[3:])
        a, b = P.check_add(*args), P.check_add(*args)
        self.assertEqual(a, b)
        self.assertEqual(args[:2] + args[3:], before)

    def test_every_refusal_has_known_code(self):
        for kw in ({"rt": {}}, {"lim": {}}, {"guard": {}}, {"price": None}, {"price": float("nan")}, {"price": -1.0}):
            try:
                r = check(**kw)
            except Exception as e:                                        # noqa: BLE001
                self.fail("check_add упал на %r: %r" % (kw, e))
            self.assertFalse(r["ok"])
            self.assertTrue(set(r["codes"]) <= set(P.ADD_CODES))


if __name__ == "__main__":
    unittest.main()
