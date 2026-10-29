"""WP3: правила входа, жизненного цикла и сопровождения (long/short, трейлы, стопы только в сторону уменьшения риска)."""
import copy
import json
import math
import random
import unittest

import _bootstrap  # noqa: F401
from _logic_helpers import T0, MS, close_candle, view, long_plan, short_plan, managing_rt
from tb.logic import rules as R
from tb.logic.candles import CandleBook

LIM = {"entry": {"max_price_age_s": 3, "max_chase_bps": 15}, "stop_moves": {"min_interval_s": 5, "min_step_ticks": 2, "min_gap_bps_from_price": 8},
       "connectivity": {"public_price_stale_s": 10}, "pyramid": {"enabled": False, "max_adds": 1}, "events": {"wake_r": [1.0, -0.5], "near_target_frac": 0.8}}


def merge(dst, patch):
    """Глубокое слияние rt_patch (как делает исполнитель)."""
    out = copy.deepcopy(dst)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def armed_rt(**kw):
    rt = {"status": "armed", "hold": None, "ts": {"armed": T0 - 1000}, "entry": {"state": "waiting"}}
    rt.update(kw)
    return rt


def stops(res):
    return [a for a in res["actions"] if a["type"] == "move_stop"]


class EntryTest(unittest.TestCase):
    def test_no_when_market_fires(self):
        r = R.evaluate_entry(long_plan(), armed_rt(), view(100.0), T0, LIM)
        self.assertTrue(r["fire"])
        self.assertIsNone(r["blocked"])
        self.assertEqual(r["rt_patch"]["entry"]["fired_px"], 100.0)
        self.assertTrue(r["cond"]["met"])

    def test_condition_not_met(self):
        p = long_plan()
        p["entry"]["when"] = {"kind": "price_beyond", "dir": "above", "level": 101.0}
        r = R.evaluate_entry(p, armed_rt(), view(100.0), T0, LIM)
        self.assertFalse(r["fire"])
        self.assertIsNone(r["blocked"])
        self.assertAlmostEqual(r["cond"]["dist_pct"], 1.0, 3)
        self.assertIn("already", r["rt_patch"]["entry"]["cond_state"])

    def test_cond_state_round_trip_requires_crossing(self):
        p = long_plan()
        p["entry"]["when"] = {"kind": "price_beyond", "dir": "above", "level": 100.0}
        rt = armed_rt()
        r = R.evaluate_entry(p, rt, view(100.5), T0, LIM)          # на взводе уже выше
        self.assertFalse(r["fire"])
        rt = merge(rt, r["rt_patch"])
        r = R.evaluate_entry(p, rt, view(100.6), T0 + 1, LIM)
        self.assertFalse(r["fire"])
        rt = merge(rt, r["rt_patch"])
        r = R.evaluate_entry(p, rt, view(99.8, now=T0 + 2), T0 + 2, LIM)
        rt = merge(rt, r["rt_patch"])
        r = R.evaluate_entry(p, rt, view(100.1, now=T0 + 3), T0 + 3, LIM)
        self.assertTrue(r["fire"])

    def test_seq_state_persists_in_patch(self):
        p = long_plan()
        p["entry"]["when"] = {"kind": "seq", "within_min": 90, "steps": [
            {"kind": "price_beyond", "dir": "below", "level": 99.0, "allow_already": True},
            {"kind": "price_in_zone", "low": 99.5, "high": 101}]}
        rt = armed_rt()
        r = R.evaluate_entry(p, rt, view(98.9), T0, LIM)
        self.assertFalse(r["fire"])
        rt = merge(rt, r["rt_patch"])
        self.assertEqual(rt["entry"]["cond_state"]["seq"]["c"]["step"], 1)
        r = R.evaluate_entry(p, rt, view(100.0, now=T0 + 5), T0 + 5, LIM)
        self.assertTrue(r["fire"])

    def test_stale_price_blocks_when_met(self):
        p = long_plan()
        p["entry"]["when"] = {"kind": "price_in_zone", "low": 90, "high": 110}
        r = R.evaluate_entry(p, armed_rt(), view(100.0, ts=T0 - 5), T0, LIM)       # возраст 5 с > 3 с, но < 10 с
        self.assertFalse(r["fire"])
        self.assertEqual(r["blocked"], "STALE")
        self.assertEqual(r["rt_patch"]["entry"]["blocked_why"], "STALE")

    def test_chase_long(self):
        for px, blocked in ((100.15, None), (100.1501, "CHASE"), (100.0, None), (99.0, None)):      # дешевле опорной не погоня
            r = R.evaluate_entry(long_plan(), armed_rt(), view(px, bid=px - 0.001, ask=px + 0.001), T0, LIM)
            self.assertEqual(r["blocked"], blocked, px)
            self.assertEqual(r["fire"], blocked is None)

    def test_chase_short(self):
        for px, blocked in ((99.85, None), (99.8499, "CHASE"), (101.0, None)):
            r = R.evaluate_entry(short_plan(), armed_rt(), view(px, bid=px - 0.001, ask=px + 0.001), T0, LIM)
            self.assertEqual(r["blocked"], blocked, px)

    def test_chase_cap_by_limits_and_plan_min(self):
        p = long_plan()
        p["entry"]["valid"]["max_chase_bps"] = 50                  # план просит больше потолка: действует потолок 15
        self.assertEqual(R.evaluate_entry(p, armed_rt(), view(100.2, bid=100.199, ask=100.201), T0, LIM)["blocked"], "CHASE")
        p["entry"]["valid"]["max_chase_bps"] = 5
        self.assertEqual(R.evaluate_entry(p, armed_rt(), view(100.1, bid=100.099, ask=100.101), T0, LIM)["blocked"], "CHASE")

    def test_chase_reference_falls_back_to_level(self):
        p = long_plan()
        del p["entry"]["ref_price"]
        p["entry"]["when"] = {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 100.0}
        b = CandleBook()
        close_candle(b, 5, T0 - 900, 99, 101, 98, 100.5)
        r = R.evaluate_entry(p, armed_rt(), view(100.3, b, bid=100.299, ask=100.301), T0, LIM)
        self.assertEqual(r["blocked"], "CHASE")

    def test_limit_entry_skips_chase_and_spread(self):
        p = long_plan()
        p["entry"]["how"] = {"type": "limit", "price": 99.0}
        r = R.evaluate_entry(p, armed_rt(), view(105.0, bid=106.0, ask=105.0), T0, LIM)
        self.assertTrue(r["fire"])

    def test_spread_crossed_or_missing_blocks_market(self):
        for bid, ask in ((100.1, 100.0), (0.0, 100.0)):
            r = R.evaluate_entry(long_plan(), armed_rt(), view(100.0, bid=bid, ask=ask), T0, LIM)
            self.assertEqual(r["blocked"], "SPREAD")

    def test_hold_blocks(self):
        r = R.evaluate_entry(long_plan(), armed_rt(hold="reconciling"), view(100.0), T0, LIM)
        self.assertEqual(r["blocked"], "HOLD")
        self.assertFalse(r["fire"])

    def test_not_before(self):
        p = long_plan(lifecycle={"not_before_ts": T0 + 60})
        self.assertFalse(R.evaluate_entry(p, armed_rt(), view(100.0), T0, LIM)["fire"])
        self.assertTrue(R.evaluate_entry(p, armed_rt(), view(100.0, now=T0 + 60), T0 + 60, LIM)["fire"])

    def test_entry_in_progress_or_wrong_status_no_fire(self):
        for ent in ("placing", "resting", "filled"):
            self.assertFalse(R.evaluate_entry(long_plan(), armed_rt(entry={"state": ent}), view(100.0), T0, LIM)["fire"])
        self.assertFalse(R.evaluate_entry(long_plan(), armed_rt(status="managing"), view(100.0), T0, LIM)["fire"])
        self.assertTrue(R.evaluate_entry(long_plan(), armed_rt(entry={"state": "blocked"}), view(100.0), T0, LIM)["fire"])

    def test_no_price(self):
        r = R.evaluate_entry(long_plan(), armed_rt(), view(None), T0, LIM)
        self.assertFalse(r["fire"])
        self.assertEqual(r["blocked"], "STALE")

    def test_confirm_candle_flow_scalp(self):
        p = short_plan()
        p["entry"]["when"] = {"all": [{"kind": "candle_reject", "tf": "5m", "level": 100.0, "dir": "short", "wick_min_pct": 0.05},
                                      {"kind": "spread_bps", "lte": 6}]}
        b = CandleBook()
        close_candle(b, 5, T0 - 900, 99.5, 100.4, 99.0, 99.6)
        r = R.evaluate_entry(p, armed_rt(), view(99.9, b), T0, LIM)
        self.assertTrue(r["fire"])

    def test_pure(self):
        p, rt, v = long_plan(), armed_rt(), view(100.0)
        before = copy.deepcopy((p, rt))
        a, b = R.evaluate_entry(p, rt, v, T0, LIM), R.evaluate_entry(p, rt, v, T0, LIM)
        self.assertEqual(a, b)
        self.assertEqual((p, rt), before)


class LifecycleTest(unittest.TestCase):
    def test_ttl_boundary(self):
        p = long_plan()
        p["entry"]["valid"]["ttl_min"] = 10
        rt = armed_rt(ts={"armed": T0 - 600})
        a = R.evaluate_lifecycle(p, rt, view(100.0), T0, LIM)
        self.assertEqual([x["type"] for x in a], ["transition"])
        self.assertEqual((a[0]["to"], a[0]["why"]), ("expired", "LIFECYCLE:TTL"))
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(ts={"armed": T0 - 599}), view(100.0), T0, LIM), [])

    def test_ttl_falls_back_to_lifecycle(self):
        p = long_plan(lifecycle={"ttl_min": 5})
        del p["entry"]["valid"]["ttl_min"]
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(ts={"armed": T0 - 301}), view(100.0), T0, LIM)[0]["to"], "expired")

    def test_cancel_levels(self):
        p = long_plan()
        p["entry"]["valid"].update(cancel_above=103.0, cancel_below=97.0)
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(), view(102.99), T0, LIM), [])
        a = R.evaluate_lifecycle(p, armed_rt(), view(103.0), T0, LIM)
        self.assertEqual((a[0]["to"], a[0]["reason"]), ("invalidated", "CANCEL_ABOVE"))
        a = R.evaluate_lifecycle(p, armed_rt(), view(97.0), T0, LIM)
        self.assertEqual(a[0]["reason"], "CANCEL_BELOW")
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(), view(104.0, ts=T0 - 60), T0, LIM), [])       # цена устарела: не решаем

    def test_cancel_entry_added_when_order_resting(self):
        p = long_plan()
        p["entry"]["valid"]["cancel_above"] = 103.0
        a = R.evaluate_lifecycle(p, armed_rt(entry={"state": "resting"}), view(104.0), T0, LIM)
        self.assertEqual([x["type"] for x in a], ["cancel_entry", "transition"])

    def test_invalidation_before_entry(self):
        p = long_plan(invalidation=[{"kind": "price_beyond", "dir": "below", "level": 98.0}])
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(), view(98.5), T0, LIM), [])
        a = R.evaluate_lifecycle(p, armed_rt(), view(98.0), T0, LIM)
        self.assertEqual((a[0]["to"], a[0]["reason"]), ("invalidated", "INVALIDATION:0"))

    def test_invalidation_candle_after_arming_only(self):
        p = long_plan(invalidation=[{"kind": "candle_close", "tf": "5m", "dir": "below", "level": 98.0, "n": 1}])
        b = CandleBook()
        close_candle(b, 5, T0 - 1500, 99, 99, 96, 97)                   # до взвода
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(), view(100.0, b), T0, LIM), [])
        close_candle(b, 5, T0 - 900, 99, 99, 96, 97)
        self.assertEqual(R.evaluate_lifecycle(p, armed_rt(), view(100.0, b), T0, LIM)[0]["to"], "invalidated")

    def test_invalidation_in_position_closes(self):
        p = long_plan(invalidation=[{"kind": "price_beyond", "dir": "below", "level": 99.5}])
        a = R.evaluate_lifecycle(p, managing_rt(), view(99.5), T0, LIM)
        self.assertEqual((a[0]["type"], a[0]["reason"], a[0]["frac"]), ("close", "invalidation", 1.0))
        self.assertEqual(R.evaluate_lifecycle(p, managing_rt(), view(99.6), T0, LIM), [])

    def test_max_hold(self):
        p = long_plan(lifecycle={"max_hold_min": 10})
        rt = managing_rt(entered=T0 - 600)
        a = R.evaluate_lifecycle(p, rt, view(100.0), T0, LIM)
        self.assertEqual((a[0]["type"], a[0]["reason"], a[0]["why"]), ("close", "timeout", "LIFECYCLE:MAX_HOLD"))
        self.assertEqual(R.evaluate_lifecycle(p, managing_rt(entered=T0 - 599), view(100.0), T0, LIM), [])

    def test_session_end_flatten(self):
        lim = dict(LIM, session_end_ts=T0 + 20)
        p = long_plan(lifecycle={"flatten_at_session_end": True})
        a = R.evaluate_lifecycle(p, managing_rt(), view(100.0), T0, lim)             # до конца 20 с < 30 с запаса
        self.assertEqual(a[0]["reason"], "session_end")
        lim = dict(LIM, session_end_ts=T0 + 31)
        self.assertEqual(R.evaluate_lifecycle(p, managing_rt(), view(100.0), T0, lim), [])
        p2 = long_plan(lifecycle={"flatten_at_session_end": False})
        self.assertEqual(R.evaluate_lifecycle(p2, managing_rt(), view(100.0), T0 + 40, dict(LIM, session_end_ts=T0)), [])

    def test_session_end_cancels_pending_entry(self):
        p = long_plan(lifecycle={"flatten_at_session_end": True})
        a = R.evaluate_lifecycle(p, armed_rt(), view(100.0), T0, dict(LIM, session_end_ts=T0))
        self.assertEqual((a[0]["to"], a[0]["reason"]), ("cancelled", "SESSION_END"))

    def test_terminal_and_other_statuses_empty(self):
        p = long_plan(lifecycle={"max_hold_min": 1, "ttl_min": 1})
        for st in ("closed", "expired", "cancelled", "draft", None):
            self.assertEqual(R.evaluate_lifecycle(p, {"status": st, "ts": {"armed": 0, "entered": 0}}, view(100.0), T0, LIM), [])

    def test_priority_levels_before_ttl(self):
        p = long_plan()
        p["entry"]["valid"].update(ttl_min=1, cancel_above=101.0)
        a = R.evaluate_lifecycle(p, armed_rt(ts={"armed": T0 - 6000}), view(102.0), T0, LIM)
        self.assertEqual(len([x for x in a if x["type"] == "transition"]), 1)
        self.assertEqual(a[0]["reason"], "CANCEL_ABOVE")


class RNowTest(unittest.TestCase):
    def test_long_short_and_none(self):
        self.assertAlmostEqual(R.r_now(managing_rt("long", 100.0, r=2.0), 102.0), 1.0)
        self.assertAlmostEqual(R.r_now(managing_rt("long", 100.0, r=2.0), 99.0), -0.5)
        self.assertAlmostEqual(R.r_now(managing_rt("short", 100.0, r=2.0), 104.0), -2.0)
        self.assertAlmostEqual(R.r_now(managing_rt("short", 100.0, r=2.0), 98.0), 1.0)
        self.assertIsNone(R.r_now({}, 100.0))
        self.assertIsNone(R.r_now(managing_rt(qty=0.0), 100.0))
        self.assertIsNone(R.r_now(managing_rt(), None))
        self.assertIsNone(R.r_now(managing_rt(), float("nan")))


class MoveStopTest(unittest.TestCase):
    def be_plan(self, side="long"):
        mk = long_plan if side == "long" else short_plan
        return mk(manage=[{"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True}])

    def test_breakeven_long(self):
        p, rt = self.be_plan(), managing_rt("long")
        self.assertEqual(R.evaluate_manage(p, rt, view(100.99), T0, LIM)["actions"], [])
        res = R.evaluate_manage(p, rt, view(101.0), T0, LIM)
        a = res["actions"][0]
        self.assertEqual((a["type"], a["mode"], a["rule"], a["why"]), ("move_stop", "breakeven", "be_1r", "RULE:be_1r"))
        self.assertAlmostEqual(a["to"], 100.0 * (1 + 20 / 1e4))          # 2 x 9 бп фактических комиссий + 2 бп
        self.assertEqual(res["rt_patch"]["rules"]["be_1r"], {"fired": 1, "last_ts": T0, "state": "done"})

    def test_breakeven_short(self):
        res = R.evaluate_manage(self.be_plan("short"), managing_rt("short"), view(99.0), T0, LIM)
        self.assertAlmostEqual(res["actions"][0]["to"], 100.0 * (1 - 20 / 1e4))

    def test_breakeven_offset_override(self):
        p = self.be_plan()
        p["manage"][0]["do"]["offset_bps"] = 0
        self.assertAlmostEqual(R.evaluate_manage(p, managing_rt(), view(101.5), T0, LIM)["actions"][0]["to"], 100.0)

    def test_once_not_repeated_after_patch(self):
        p, rt = self.be_plan(), managing_rt()
        res = R.evaluate_manage(p, rt, view(101.5), T0, LIM)
        rt2 = merge(rt, res["rt_patch"])
        self.assertEqual(R.evaluate_manage(p, rt2, view(101.6, now=T0 + 1), T0 + 1, LIM)["actions"], [])

    def test_not_tighter_is_noop_and_marks_skipped(self):
        p, rt = self.be_plan(), managing_rt(stop=100.5)
        res = R.evaluate_manage(p, rt, view(102.0), T0, LIM)
        self.assertEqual(res["actions"], [])
        self.assertEqual(res["skipped"], [{"rule": "be_1r", "code": "NOT_TIGHTER"}])
        self.assertEqual(res["rt_patch"]["rules"]["be_1r"]["state"], "skipped")
        rt2 = merge(rt, res["rt_patch"])
        self.assertEqual(R.evaluate_manage(p, rt2, view(102.0, now=T0 + 1), T0 + 1, LIM)["skipped"], [])
        # равный стоп тоже не строже
        p["manage"][0]["do"] = {"action": "move_stop", "to": "price", "price": 100.5}
        self.assertEqual(R.evaluate_manage(p, rt, view(102.0), T0, LIM)["actions"], [])

    def test_short_not_tighter(self):
        p = short_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "price", "price": 100.9}}])
        rt = managing_rt("short", stop=100.5)
        self.assertEqual(R.evaluate_manage(p, rt, view(99.0), T0, LIM)["actions"], [])
        p["manage"][0]["do"]["price"] = 100.4
        self.assertEqual(R.evaluate_manage(p, rt, view(99.0), T0, LIM)["actions"][0]["to"], 100.4)

    def test_wrong_side_and_too_close_not_consumed(self):
        p = long_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "price", "price": 100.5}, "once": True}])
        rt = managing_rt()
        res = R.evaluate_manage(p, rt, view(100.4), T0, LIM)             # стоп выше цены
        self.assertEqual((res["actions"], res["skipped"][0]["code"]), ([], "WRONG_SIDE"))
        res = R.evaluate_manage(p, rt, view(100.505), T0, LIM)           # зазор 5 бп < 8 бп
        self.assertEqual(res["skipped"][0]["code"], "TOO_CLOSE")
        self.assertNotIn("rules", res["rt_patch"])                       # правило не израсходовано
        res = R.evaluate_manage(p, rt, view(100.6), T0, LIM)             # зазор 10 бп
        self.assertEqual(res["actions"][0]["to"], 100.5)

    def test_lock_r_entry_pct_price_modes(self):
        rt = managing_rt("long", r=2.0)
        for do, want, mode in (({"to": "lock_r", "r": 0.5}, 101.0, "lock_r"), ({"to": "entry_pct", "pct": 0.5}, 100.5, "price"),
                               ({"to": "price", "price": 100.2}, 100.2, "price")):
            p = long_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": dict(do, action="move_stop")}])
            a = R.evaluate_manage(p, rt, view(104.0), T0, LIM)["actions"][0]
            self.assertAlmostEqual(a["to"], want)
            self.assertEqual(a["mode"], mode)
        rt = managing_rt("short", r=2.0)
        p = short_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "lock_r", "r": 0.5}}])
        self.assertAlmostEqual(R.evaluate_manage(p, rt, view(96.0), T0, LIM)["actions"][0]["to"], 99.0)

    def test_tick_rounding_is_conservative(self):
        lim = dict(LIM, tick=0.05)
        rt = managing_rt("long", r=2.0)
        p = long_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "price", "price": 101.03}}])
        self.assertAlmostEqual(R.evaluate_manage(p, rt, view(104.0), T0, lim)["actions"][0]["to"], 101.0)     # long вниз
        rt = managing_rt("short", r=2.0)
        p = short_plan(manage=[{"id": "mv", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "price", "price": 98.97}}])
        self.assertAlmostEqual(R.evaluate_manage(p, rt, view(96.0), T0, lim)["actions"][0]["to"], 99.0)       # short вверх

    def test_multiple_stops_collapse_to_tightest_and_mark_all(self):
        p = long_plan(manage=[
            {"id": "a_be", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True},
            {"id": "b_lock", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "lock_r", "r": 0.5}, "once": True}])
        res = R.evaluate_manage(p, managing_rt(), view(102.0), T0, LIM)
        self.assertEqual(len(stops(res)), 1)
        self.assertAlmostEqual(stops(res)[0]["to"], 100.5)
        self.assertEqual(set(res["rt_patch"]["rules"]), {"a_be", "b_lock"})

    def test_hold_and_stale_price_freeze_stops_not_closes(self):
        p = long_plan(manage=[
            {"id": "be", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}},
            {"id": "cl", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "close", "frac": 0.5, "reason": "rule"}}])
        res = R.evaluate_manage(p, managing_rt(hold="reconciling"), view(102.0), T0, LIM)
        self.assertEqual([a["type"] for a in res["actions"]], ["close"])
        res = R.evaluate_manage(p, managing_rt(), view(102.0, ts=T0 - 60), T0, LIM)             # устаревшая цена: pnl_r не вычислим
        self.assertEqual(res["actions"], [])


class TrailTest(unittest.TestCase):
    def plan(self, do, side="long", when=None):
        mk = long_plan if side == "long" else short_plan
        return mk(manage=[{"id": "trail", "when": when or {"kind": "pnl_r", "gte": 0.0}, "do": dict(do, action="trail")}])

    def book_up(self, lows, tf=5, start=T0 - 1200):
        b = CandleBook()
        for i, lo in enumerate(lows):
            close_candle(b, tf, start + i * tf * 60, lo + 0.3, lo + 1.0, lo, lo + 0.5)
        return b

    def test_candle_extreme_long_buffer_and_monotonic(self):
        p = self.plan({"mode": "candle_extreme", "tf": "5m", "n": 3, "buffer_bps": 4})
        b = self.book_up([100.5, 101.0, 101.2])
        rt = managing_rt(entered=T0 - 1200)
        res = R.evaluate_manage(p, rt, view(103.0, b), T0, LIM)
        a = stops(res)[0]
        self.assertAlmostEqual(a["to"], 100.5 * (1 - 4 / 1e4))
        self.assertEqual(a["mode"], "trail")
        rt = merge(rt, res["rt_patch"])
        rt["pos"]["cur_stop"] = a["to"]
        # та же свеча: пересчёта нет
        self.assertEqual(R.evaluate_manage(p, rt, view(103.0, b, now=T0 + 1), T0 + 1, LIM)["actions"], [])
        # новая свеча, но экстремум последних 3 ниже (откат): кандидат слабее - действия нет
        close_candle(b, 5, T0 - 300, 100.0, 101.0, 99.5, 100.7)
        self.assertEqual(stops(R.evaluate_manage(p, rt, view(103.0, b, now=T0 + 2), T0 + 2, LIM)), [])
        # новая свеча с ростом: стоп подтягивается
        close_candle(b, 5, T0, 102.0, 104.0, 101.8, 103.0)
        rt2 = merge(rt, R.evaluate_manage(p, rt, view(103.0, b, now=T0 + 3), T0 + 3, LIM)["rt_patch"])
        close_candle(b, 5, T0 + 300, 102.0, 104.0, 101.9, 103.0)
        close_candle(b, 5, T0 + 600, 102.0, 104.0, 102.0, 103.0)
        res = R.evaluate_manage(p, rt2, view(104.5, b, now=T0 + 900), T0 + 900, LIM)
        self.assertAlmostEqual(stops(res)[0]["to"], 101.8 * (1 - 4 / 1e4))

    def test_candle_extreme_short(self):
        p = self.plan({"mode": "candle_extreme", "tf": "5m", "n": 2, "buffer_bps": 0}, "short")
        b = CandleBook()
        close_candle(b, 5, T0 - 600, 99, 99.6, 98, 98.5)
        close_candle(b, 5, T0 - 300, 98.5, 99.2, 97, 97.5)
        rt = managing_rt("short", entered=T0 - 600)
        a = stops(R.evaluate_manage(p, rt, view(97.0, b), T0, LIM))[0]
        self.assertAlmostEqual(a["to"], 99.6)

    def test_candle_extreme_needs_n_candles_after_entry(self):
        p = self.plan({"mode": "candle_extreme", "tf": "5m", "n": 3})
        b = self.book_up([100.5, 101.0])
        res = R.evaluate_manage(p, managing_rt(entered=T0 - 1200), view(103.0, b), T0, LIM)
        self.assertEqual(res["actions"], [])
        b = self.book_up([100.5, 101.0, 101.2], start=T0 - 1500)       # одна свеча до входа не считается
        self.assertEqual(R.evaluate_manage(p, managing_rt(entered=T0 - 1200), view(103.0, b), T0, LIM)["actions"], [])

    def test_atr_trail(self):
        b = CandleBook()
        for i in range(20):
            close_candle(b, 5, T0 - 6000 + i * 300, 100, 101, 99, 100)
        p = self.plan({"mode": "atr", "tf": "5m", "n": 14, "mult": 2.0, "buffer_bps": 5})
        rt = managing_rt(entered=T0 - 6000)
        rt["pos"]["best_px"] = 106.0
        a = stops(R.evaluate_manage(p, rt, view(105.0, b), T0, LIM))[0]
        self.assertAlmostEqual(a["to"], (106.0 - 2 * 2.0) * (1 - 5 / 1e4))
        ps = self.plan({"mode": "atr", "tf": "5m", "n": 14, "mult": 2.0}, "short")
        rts = managing_rt("short", entered=T0 - 6000)
        rts["pos"]["best_px"] = 94.0
        a = stops(R.evaluate_manage(ps, rts, view(95.0, b), T0, LIM))[0]
        self.assertAlmostEqual(a["to"], 94.0 + 4.0)

    def test_atr_without_history(self):
        p = self.plan({"mode": "atr", "tf": "5m", "n": 14, "mult": 2.0})
        self.assertEqual(R.evaluate_manage(p, managing_rt(), view(105.0, CandleBook()), T0, LIM)["actions"], [])

    def test_percent_uses_best_price(self):
        p = self.plan({"mode": "percent", "pct": 2.0})
        rt = managing_rt()
        rt["pos"]["best_px"] = 106.0
        a = stops(R.evaluate_manage(p, rt, view(105.0), T0, LIM))[0]
        self.assertAlmostEqual(a["to"], 106.0 * 0.98)
        # best_px отстаёт от цены: берётся максимум
        rt["pos"]["best_px"] = 101.0
        self.assertAlmostEqual(stops(R.evaluate_manage(p, rt, view(105.0), T0, LIM))[0]["to"], 105.0 * 0.98)

    def test_percent_interval_between_moves(self):
        p = self.plan({"mode": "percent", "pct": 1.0})
        rt = managing_rt()
        res = R.evaluate_manage(p, rt, view(105.0), T0, LIM)
        rt2 = merge(rt, res["rt_patch"])
        rt2["pos"]["cur_stop"] = stops(res)[0]["to"]
        self.assertEqual(R.evaluate_manage(p, rt2, view(106.0, now=T0 + 4), T0 + 4, LIM)["actions"], [])
        self.assertEqual(len(stops(R.evaluate_manage(p, rt2, view(106.0, now=T0 + 5), T0 + 5, LIM))), 1)

    def test_step_r_ladder(self):
        p = self.plan({"mode": "step_r", "step_r": 1.0})
        rt = managing_rt(r=2.0)
        for best, want in ((101.9, None), (102.0, 100.0), (105.9, 102.0), (106.0, 104.0)):
            rt["pos"]["best_px"] = best
            st = stops(R.evaluate_manage(p, rt, view(best), T0, LIM))
            if want is None:
                self.assertEqual(st, [], best)
            else:
                self.assertAlmostEqual(st[0]["to"], want, msg=str(best)) if want > 98 else None
        rt = managing_rt(r=2.0, stop=99.0)
        rt["pos"]["initial_stop"] = 98.0
        rt["pos"]["best_px"] = 102.0
        # k=1 -> стоп на входе (100.0): строже 98
        self.assertAlmostEqual(stops(R.evaluate_manage(p, rt, view(102.0), T0, LIM))[0]["to"], 100.0)

    def test_trail_candidate_direct(self):
        rt = managing_rt(r=2.0)
        rt["pos"]["best_px"] = 110.0
        c = R.trail_candidate({"do": {"mode": "percent", "pct": 2.0, "buffer_bps": 10}}, long_plan(), rt, view(109.0))
        self.assertAlmostEqual(c, 110.0 * 0.98 * (1 - 0.001))
        self.assertIsNone(R.trail_candidate({"do": {"mode": "weird"}}, long_plan(), rt, view(109.0)))
        self.assertIsNone(R.trail_candidate({"do": {"mode": "percent", "pct": -1}}, long_plan(), rt, view(109.0)))
        self.assertIsNone(R.trail_candidate({"do": {"mode": "percent", "pct": 2}}, long_plan(), {}, view(109.0)))
        self.assertIsNone(R.trail_candidate({"do": {"mode": "percent", "pct": 2}}, {"side": "x"}, rt, view(109.0)))
        self.assertAlmostEqual(R.trail_candidate({"mode": "percent", "pct": 2.0}, long_plan(), rt, view(109.0)), 110.0 * 0.98)   # rule = do

    def test_gap_clamps_candidate_to_price_distance(self):
        p = self.plan({"mode": "percent", "pct": 0.01})                 # кандидат почти у цены
        rt = managing_rt()
        rt["pos"]["best_px"] = 105.0
        a = stops(R.evaluate_manage(p, rt, view(105.0), T0, LIM))[0]
        self.assertAlmostEqual(a["to"], 105.0 * (1 - 8 / 1e4))
        ps = self.plan({"mode": "percent", "pct": 0.01}, "short")
        rts = managing_rt("short")
        rts["pos"]["best_px"] = 95.0
        self.assertAlmostEqual(stops(R.evaluate_manage(ps, rts, view(95.0), T0, LIM))[0]["to"], 95.0 * (1 + 8 / 1e4))

    def test_min_step_ticks(self):
        lim = dict(LIM, tick=0.1)
        p = self.plan({"mode": "percent", "pct": 1.0})
        rt = managing_rt(stop=103.8)
        rt["pos"]["best_px"] = 105.0                                  # кандидат 103.95 -> 103.9; шаг 0.1 < 2 тика
        self.assertEqual(stops(R.evaluate_manage(p, rt, view(105.0), T0, lim)), [])
        rt = managing_rt(stop=103.6)
        rt["pos"]["best_px"] = 105.0                                  # шаг 0.3 >= 0.2
        self.assertAlmostEqual(stops(R.evaluate_manage(p, rt, view(105.0), T0, lim))[0]["to"], 103.9)

    def test_trail_gated_by_when(self):
        p = self.plan({"mode": "percent", "pct": 1.0}, when={"kind": "pnl_r", "gte": 1.3})
        rt = managing_rt()
        self.assertEqual(R.evaluate_manage(p, rt, view(101.2), T0, LIM)["actions"], [])
        self.assertEqual(len(stops(R.evaluate_manage(p, rt, view(101.3), T0, LIM))), 1)


class OtherActionsTest(unittest.TestCase):
    def test_partial_and_close_and_notify(self):
        p = long_plan(manage=[
            {"id": "p1", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "partial_close", "frac_of_remaining": 0.5}, "once": True},
            {"id": "p2", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "partial_close", "frac_of_initial": 0.25}, "once": True},
            {"id": "c1", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "close", "frac": 0.3, "reason": "rule"}, "once": True},
            {"id": "c2", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "close"}, "once": True},
            {"id": "n1", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "notify", "code": "R_PLUS"}, "once": True}])
        res = R.evaluate_manage(p, managing_rt(), view(101.0), T0, LIM)
        a = {x["rule"]: x for x in res["actions"]}
        self.assertEqual(a["p1"]["frac_remaining"], 0.5)
        self.assertEqual(a["p2"]["frac_initial"], 0.25)
        self.assertEqual((a["c1"]["frac"], a["c1"]["reason"]), (0.3, "rule"))
        self.assertEqual((a["c2"]["frac"], a["c2"]["reason"]), (1.0, "rule"))
        self.assertEqual(a["n1"]["code"], "R_PLUS")
        self.assertEqual([x["rule"] for x in res["actions"]], ["p1", "p2", "c1", "c2", "n1"])           # порядок массива
        self.assertEqual(res["rt_patch"]["rules"]["p1"]["state"], "done")

    def test_invalid_fractions_skipped(self):
        for do in ({"action": "partial_close", "frac_of_remaining": 0}, {"action": "partial_close", "frac_of_remaining": 1.5},
                   {"action": "partial_close"}, {"action": "close", "frac": 0}, {"action": "close", "frac": 2}, {"action": "notify"}, {"action": "zzz"}):
            p = long_plan(manage=[{"id": "x1", "when": {"kind": "pnl_r", "gte": 0.0}, "do": do}])
            self.assertEqual(R.evaluate_manage(p, managing_rt(), view(101.0), T0, LIM)["actions"], [], do)

    def test_move_target(self):
        mk = lambda price: long_plan(manage=[{"id": "mt", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_target", "target": "t1", "price": price}}])
        rt = managing_rt()                                              # t1 = 103, стоп 99 (ниже входа)
        a = R.evaluate_manage(mk(102.0), rt, view(101.0), T0, LIM)["actions"]                       # придвинуть можно всегда
        self.assertEqual((a[0]["type"], a[0]["target"], a[0]["price"]), ("move_target", "t1", 102.0))
        res = R.evaluate_manage(mk(104.0), rt, view(101.0), T0, LIM)                                  # отодвинуть при риске: нельзя
        self.assertEqual((res["actions"], res["skipped"][0]["code"]), ([], "TARGET_FARTHER_NOT_RISKFREE"))
        rt = managing_rt(stop=100.0)                                    # стоп в безубытке: можно
        self.assertEqual(R.evaluate_manage(mk(104.0), rt, view(101.0), T0, LIM)["actions"][0]["price"], 104.0)
        self.assertEqual(R.evaluate_manage(mk(103.0), rt, view(101.0), T0, LIM)["actions"], [])      # без изменения
        rt["targets"]["t1"]["state"] = "filled"
        self.assertEqual(R.evaluate_manage(mk(102.0), rt, view(101.0), T0, LIM)["actions"], [])

    def test_move_target_short(self):
        mk = lambda price: short_plan(manage=[{"id": "mt", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_target", "target": "t1", "price": price}}])
        rt = managing_rt("short")                                       # t1 = 97
        self.assertEqual(R.evaluate_manage(mk(98.0), rt, view(99.0), T0, LIM)["actions"][0]["price"], 98.0)
        self.assertEqual(R.evaluate_manage(mk(96.0), rt, view(99.0), T0, LIM)["actions"], [])
        rt = managing_rt("short", stop=100.0)
        self.assertEqual(R.evaluate_manage(mk(96.0), rt, view(99.0), T0, LIM)["actions"][0]["price"], 96.0)

    def test_add_gating(self):
        p = long_plan(manage=[{"id": "pyr", "when": {"kind": "pnl_r", "gte": 1.5}, "do": {"action": "add", "risk_frac": 0.5}, "once": True}])
        rt = managing_rt()
        res = R.evaluate_manage(p, rt, view(101.5), T0, LIM)             # pyramid.enabled=false
        self.assertEqual((res["actions"], res["skipped"][0]["code"]), ([], "ADD_DISABLED"))
        lim = dict(LIM, pyramid={"enabled": True, "max_adds": 1})
        res = R.evaluate_manage(p, rt, view(101.5), T0, lim)
        self.assertEqual((res["actions"][0]["type"], res["actions"][0]["risk_frac"]), ("add", 0.5))
        self.assertEqual(R.evaluate_manage(p, managing_rt(adds={"n": 1}), view(101.5), T0, lim)["skipped"][0]["code"], "MAX_ADDS")
        self.assertEqual(R.evaluate_manage(p, managing_rt(hold="x"), view(101.5), T0, lim)["actions"], [])
        self.assertEqual(R.evaluate_manage(p, rt, view(101.4), T0, lim)["actions"], [])

    def test_max_times_cooldown_once(self):
        p = long_plan(manage=[{"id": "nt", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "notify", "code": "TIME"}, "max_times": 2, "cooldown_s": 60}])
        rt = managing_rt()
        res = R.evaluate_manage(p, rt, view(101.0), T0, LIM)
        self.assertEqual(len(res["actions"]), 1)
        self.assertEqual(res["rt_patch"]["rules"]["nt"]["state"], "waiting")
        rt = merge(rt, res["rt_patch"])
        self.assertEqual(R.evaluate_manage(p, rt, view(101.0, now=T0 + 59), T0 + 59, LIM)["actions"], [])
        res = R.evaluate_manage(p, rt, view(101.0, now=T0 + 60), T0 + 60, LIM)
        self.assertEqual(len(res["actions"]), 1)
        self.assertEqual(res["rt_patch"]["rules"]["nt"]["state"], "done")
        rt = merge(rt, res["rt_patch"])
        self.assertEqual(R.evaluate_manage(p, rt, view(101.0, now=T0 + 999), T0 + 999, LIM)["actions"], [])

    def test_only_in_managing_with_position(self):
        p = long_plan(manage=[{"id": "nt", "when": {"kind": "elapsed", "min": 0}, "do": {"action": "notify", "code": "TIME"}}])
        for rt in (managing_rt(status="armed"), managing_rt(status="entered"), managing_rt(qty=0.0), {}):
            self.assertEqual(R.evaluate_manage(p, rt, view(101.0), T0, LIM)["actions"], [])

    def test_events_once(self):
        p = long_plan()
        rt = managing_rt()
        res = R.evaluate_manage(p, rt, view(101.0), T0, LIM)
        self.assertEqual([e["name"] for e in res["events"]], ["r_plus_1"])
        self.assertEqual(res["events"][0]["r"], 1.0)
        rt = merge(rt, res["rt_patch"])
        self.assertEqual(R.evaluate_manage(p, rt, view(102.0, now=T0 + 1), T0 + 1, LIM)["events"], [])
        res = R.evaluate_manage(p, rt, view(99.5, now=T0 + 2), T0 + 2, LIM)
        self.assertEqual([e["name"] for e in res["events"]], ["r_minus_0_5"])
        self.assertEqual(R.evaluate_manage(p, merge(rt, res["rt_patch"]), view(99.4, now=T0 + 3), T0 + 3, LIM)["events"], [])

    def test_events_short_and_near_target(self):
        p = short_plan()
        rt = managing_rt("short")                                       # t1 = 97
        res = R.evaluate_manage(p, rt, view(97.6), T0, LIM)             # 2.4/3 = 0.8
        names = [e["name"] for e in res["events"]]
        self.assertIn("near_target", names)
        self.assertNotIn("r_plus_1", [e["name"] for e in R.evaluate_manage(p, rt, view(99.5), T0, LIM)["events"]])
        self.assertIn("r_plus_1", [e["name"] for e in R.evaluate_manage(p, rt, view(99.0), T0, LIM)["events"]])
        rt2 = merge(rt, res["rt_patch"])
        self.assertNotIn("near_target", [e["name"] for e in R.evaluate_manage(p, rt2, view(97.4, now=T0 + 1), T0 + 1, LIM)["events"]])


class SafetyTest(unittest.TestCase):
    """Стоп только в сторону уменьшения риска: тест-ловушка со сломанным движком правил и свойства на случайных ходах."""

    def trail_plan(self, side):
        mk = long_plan if side == "long" else short_plan
        return mk(manage=[{"id": "trail", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "trail", "mode": "percent", "pct": 1.0}},
                          {"id": "be", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}}])

    def test_trap_broken_trail_candidate(self):
        orig = R.trail_candidate
        try:
            for side in ("long", "short"):
                s = 1 if side == "long" else -1
                rt = managing_rt(side, stop=100.0 - s * 0.5)
                rt["pos"]["best_px"] = 100.0 + s * 3
                px = 100.0 + s * 3
                for bad in (lambda *a: 90.0 if s == 1 else 110.0,                 # шире текущего стопа
                            lambda *a: rt["pos"]["cur_stop"],                      # равный
                            lambda *a: float("nan"), lambda *a: float("inf") * s, lambda *a: -5.0, lambda *a: 0.0, lambda *a: None,
                            lambda *a: "abc"):
                    R.trail_candidate = bad
                    res = R.evaluate_manage(self.trail_plan(side), rt, view(px), T0, LIM)
                    cur = rt["pos"]["cur_stop"]
                    for a in stops(res):
                        self.assertGreater(s * (a["to"] - cur), 0, (side, a))
                    self.assertEqual([a for a in stops(res) if a["rule"] == "trail"], [], (side,))
        finally:
            R.trail_candidate = orig

    def test_trap_broken_stop_target(self):
        orig = R._stop_target
        try:
            R._stop_target = lambda *a, **k: 50.0                                  # long: далеко под стопом
            p = long_plan(manage=[{"id": "be", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "breakeven"}}])
            self.assertEqual(stops(R.evaluate_manage(p, managing_rt(), view(102.0), T0, LIM)), [])
            R._stop_target = lambda *a, **k: 150.0                                 # short: выше стопа
            p = short_plan(manage=[{"id": "be", "when": {"kind": "pnl_r", "gte": 0.0}, "do": {"action": "move_stop", "to": "breakeven"}}])
            self.assertEqual(stops(R.evaluate_manage(p, managing_rt("short"), view(98.0), T0, LIM)), [])
        finally:
            R._stop_target = orig

    def test_enforce_tighter_last_line_of_defence(self):
        acts = [{"type": "move_stop", "to": 99.0}, {"type": "move_stop", "to": 100.0}, {"type": "move_stop", "to": 101.0},
                {"type": "move_stop", "to": float("nan")}, {"type": "move_stop", "to": None}, {"type": "close", "frac": 1.0}]
        self.assertEqual([a.get("to") for a in R.enforce_tighter(acts, 1, 100.0) if a["type"] == "move_stop"], [101.0])
        self.assertEqual([a.get("to") for a in R.enforce_tighter(acts, -1, 100.0) if a["type"] == "move_stop"], [99.0])
        self.assertEqual(len(R.enforce_tighter(acts, 1, 100.0)), 2)

    def _walk(self, side, seed):
        rnd = random.Random(seed)
        s = 1 if side == "long" else -1
        p = self.trail_plan(side)
        p["manage"].append({"id": "lk", "when": {"kind": "pnl_r", "gte": 2.0}, "do": {"action": "move_stop", "to": "lock_r", "r": 1.0}})
        p["manage"].append({"id": "stp", "when": {"kind": "pnl_r", "gte": 0.5}, "do": {"action": "trail", "mode": "step_r", "step_r": 0.5}})
        rt = managing_rt(side)
        px = 100.0
        cur = rt["pos"]["cur_stop"]
        moves = 0
        lim = dict(LIM, tick=0.01)
        for i in range(300):
            px = max(50.0, px * (1 + rnd.gauss(0, 0.004)))
            now = T0 + i * 7
            rt["pos"]["best_px"] = max(rt["pos"]["best_px"], px) if s == 1 else min(rt["pos"]["best_px"], px)
            res = R.evaluate_manage(p, rt, view(px, now=now), now, lim)
            for a in stops(res):
                self.assertGreater(s * (a["to"] - cur), 0, (side, seed, i, a, cur))
                self.assertGreater(s * (px - a["to"]), 0, "стоп по ту сторону цены")
                cur = a["to"]
                rt["pos"]["cur_stop"] = cur
                moves += 1
            rt = merge(rt, res["rt_patch"])
            self.assertGreaterEqual(s * (rt["pos"]["cur_stop"] - rt["pos"]["initial_stop"]), 0)
        return moves

    def test_property_random_walks_never_loosen(self):
        total = 0
        for side in ("long", "short"):
            for seed in range(12):
                total += self._walk(side, seed)
        self.assertGreater(total, 20)           # движок реально двигал стопы, а не молчал

    def test_permutation_invariance_of_action_set(self):
        rules = [
            {"id": "a_be", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True},
            {"id": "b_lk", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "lock_r", "r": 0.5}, "once": True},
            {"id": "c_tr", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "trail", "mode": "percent", "pct": 1.0}},
            {"id": "d_nt", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "notify", "code": "R_PLUS"}, "once": True},
            {"id": "e_cl", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "partial_close", "frac_of_remaining": 0.3}, "once": True}]
        rt = managing_rt()
        rt["pos"]["best_px"] = 102.5
        sets = set()
        rnd = random.Random(7)
        for _ in range(12):
            rr = rules[:]
            rnd.shuffle(rr)
            res = R.evaluate_manage(long_plan(manage=rr), rt, view(102.5), T0, LIM)
            sets.add(json.dumps(sorted(json.dumps(a, sort_keys=True) for a in res["actions"])))
            self.assertEqual(sorted(res["rt_patch"]["rules"]), sorted(r["id"] for r in rules))
        self.assertEqual(len(sets), 1)

    def test_pure_no_mutation_deterministic(self):
        p = self.trail_plan("long")
        rt = managing_rt()
        rt["pos"]["best_px"] = 103.0
        v = view(103.0)
        before = copy.deepcopy((p, rt, LIM))
        a, b = R.evaluate_manage(p, rt, v, T0, LIM), R.evaluate_manage(p, rt, v, T0, LIM)
        self.assertEqual(a, b)
        self.assertEqual((p, rt, LIM), before)
        pl, rt2 = long_plan(lifecycle={"max_hold_min": 1}), managing_rt()
        before = copy.deepcopy((pl, rt2))
        self.assertEqual(R.evaluate_lifecycle(pl, rt2, v, T0, LIM), R.evaluate_lifecycle(pl, rt2, v, T0, LIM))
        self.assertEqual((pl, rt2), before)

    def test_garbage_plans_never_raise(self):
        for p in ({}, {"side": "long"}, {"side": "long", "manage": [None]}, {"side": "long", "manage": [{"id": "x"}]},
                  {"side": "long", "manage": [{"id": "x", "when": "bad", "do": []}]}):
            try:
                R.evaluate_manage(p, managing_rt(), view(101.0), T0, LIM)
            except AttributeError:
                self.fail("мусор в manage не должен ронять движок: %r" % (p,))


class AutoProtectTest(unittest.TestCase):
    """exec_limits.auto_protect: стоп подтягивается и у плана без правил manage; только в сторону прибыли."""
    AP = dict(LIM, auto_protect={"enabled": True, "be_r": 0.7, "trail_activate_r": 1.0, "trail_lock_frac": 0.5})

    def test_disabled_without_section(self):
        rt = managing_rt(best_px=None)
        rt["pos"]["best_px"] = 103.0
        self.assertEqual(stops(R.evaluate_manage(long_plan(), rt, view(103.0), T0, LIM)), [])

    def test_breakeven_at_be_r(self):
        rt = managing_rt()
        rt["pos"]["best_px"] = 100.8
        s = stops(R.evaluate_manage(long_plan(), rt, view(100.8), T0, self.AP))
        self.assertEqual(len(s), 1)
        self.assertGreater(s[0]["to"], 100.0)
        self.assertLess(s[0]["to"], 100.8)

    def test_mfe_lock_keeps_half_of_peak_long_and_short(self):
        rt = managing_rt()
        rt["pos"]["best_px"] = 102.0                                 # пик +2R, цена откатила до 101.6
        s = stops(R.evaluate_manage(long_plan(), rt, view(101.6), T0, self.AP))
        self.assertAlmostEqual(s[0]["to"], 101.0, 2)                 # 50% от +2R = +1R
        rt = managing_rt(side="short")
        rt["pos"]["best_px"] = 98.0
        s = stops(R.evaluate_manage(short_plan(), rt, view(98.4), T0, self.AP))
        self.assertAlmostEqual(s[0]["to"], 99.0, 2)

    def test_never_loosens_existing_stop(self):
        rt = managing_rt(stop=101.5)
        rt["pos"]["best_px"] = 102.0
        self.assertEqual(stops(R.evaluate_manage(long_plan(), rt, view(101.8), T0, self.AP)), [])


if __name__ == "__main__":
    unittest.main()
