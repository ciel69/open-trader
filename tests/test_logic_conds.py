"""WP3: вычисление условий (таблицы кейсов по видам, seq, all/any/not, дыры, чистота)."""
import copy
import unittest

import _bootstrap  # noqa: F401
from _logic_helpers import T0, MS, close_candle, view
from tb.logic.candles import CandleBook
from tb.logic import conds as C

ARMED = (T0 - 1000) * MS      # взвод: за 1000 с до T0


def ev(cond, v, st=None, ctx=None):
    return C.eval_cond(cond, v, st or {}, ctx if ctx is not None else {"armed_ms": ARMED})


def book5(*cs, start=T0 - 900):
    """Закрытые 5м свечи подряд: каждая (o,h,l,c[,vol])."""
    b = CandleBook()
    for i, c in enumerate(cs):
        close_candle(b, 5, start + i * 300, *c)
    return b


class PriceBeyondTest(unittest.TestCase):
    def test_table(self):
        cases = [("above", 100.0, 100.0, True), ("above", 100.0, 99.99, False), ("above", 100.0, 100.01, True),
                 ("below", 100.0, 100.0, True), ("below", 100.0, 100.01, False), ("below", 100.0, 99.0, True)]
        for d, lvl, px, want in cases:
            cond = {"kind": "price_beyond", "dir": d, "level": lvl, "allow_already": True}
            self.assertEqual(ev(cond, view(px))[0], want, (d, lvl, px))

    def test_already_beyond_needs_return(self):
        cond = {"kind": "price_beyond", "dir": "above", "level": 100.0}
        ok, st = ev(cond, view(101.0))
        self.assertFalse(ok)                                   # на взводе уже за уровнем
        self.assertTrue(st["already"]["c"])
        ok, st = ev(cond, view(101.5), st)
        self.assertFalse(ok)                                   # всё ещё за уровнем
        ok, st = ev(cond, view(99.0), st)
        self.assertFalse(ok)                                   # вернулись
        self.assertFalse(st["already"]["c"])
        ok, st = ev(cond, view(100.2), st)
        self.assertTrue(ok)                                    # пересечение

    def test_not_already_crossing_from_start(self):
        cond = {"kind": "price_beyond", "dir": "below", "level": 100.0}
        ok, st = ev(cond, view(100.5))
        self.assertFalse(ok)
        ok, _ = ev(cond, view(99.9), st)
        self.assertTrue(ok)

    def test_allow_already_true_fires_immediately(self):
        self.assertTrue(ev({"kind": "price_beyond", "dir": "above", "level": 100.0, "allow_already": True}, view(105.0))[0])

    def test_stateless_flag(self):
        cond = {"kind": "price_beyond", "dir": "above", "level": 100.0}
        self.assertTrue(ev(cond, view(105.0), ctx={"armed_ms": ARMED, "stateless": True})[0])

    def test_stale_or_bad_price_not_met(self):
        cond = {"kind": "price_beyond", "dir": "above", "level": 100.0, "allow_already": True}
        self.assertFalse(ev(cond, view(105.0, ts=T0 - 11))[0])        # старше 10 с
        self.assertTrue(ev(cond, view(105.0, ts=T0 - 9))[0])
        self.assertFalse(ev(cond, view(None))[0])
        self.assertFalse(ev(cond, view(0.0))[0])
        self.assertFalse(ev(cond, view(float("nan")))[0])
        self.assertFalse(ev(cond, view(105.0), ctx={"armed_ms": ARMED, "stale_s": 0.0}, st={})[0] and False)

    def test_does_not_mutate_inputs(self):
        cond = {"kind": "price_beyond", "dir": "above", "level": 100.0}
        st = {"already": {"c": False}}
        c0, s0 = copy.deepcopy(cond), copy.deepcopy(st)
        ev(cond, view(101.0), st)
        self.assertEqual((cond, st), (c0, s0))


class CandleCloseTest(unittest.TestCase):
    cond = {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 100.0, "n": 1}

    def test_strict_close(self):
        self.assertTrue(ev(self.cond, view(100, book5((99, 101, 98, 100.01))))[0])
        self.assertFalse(ev(self.cond, view(100, book5((99, 101, 98, 100.0))))[0])       # равенство уровню не считается
        below = dict(self.cond, dir="below")
        self.assertTrue(ev(below, view(100, book5((101, 101, 98, 99.99))))[0])
        self.assertFalse(ev(below, view(100, book5((101, 101, 98, 100.0))))[0])

    def test_history_before_arming_ignored(self):
        b = CandleBook()
        close_candle(b, 5, T0 - 1500, 99, 102, 98, 101)          # закрылась выше, но ДО взвода
        self.assertFalse(ev(self.cond, view(100, b))[0])
        close_candle(b, 5, T0 - 900, 99, 102, 98, 99)            # после взвода: ниже
        self.assertFalse(ev(self.cond, view(100, b))[0])

    def test_first_candle_after_arming_counts(self):
        b = CandleBook()
        close_candle(b, 5, T0 - 1000, 99, 102, 98, 101)          # start == armed: считается
        self.assertTrue(ev(self.cond, view(100, b))[0])

    def test_n_candles(self):
        c3 = dict(self.cond, n=3)
        b = book5((99, 102, 98, 101), (101, 103, 100, 102), (102, 104, 101, 103))
        self.assertTrue(ev(c3, view(100, b))[0])
        b = book5((99, 102, 98, 101), (101, 103, 98, 99), (102, 104, 101, 103))
        self.assertFalse(ev(c3, view(100, b))[0])
        b = book5((99, 102, 98, 101), (101, 103, 100, 102))
        self.assertFalse(ev(c3, view(100, b))[0])                # не хватает свечей

    def test_gap_between_candles_not_met(self):
        b = CandleBook()
        close_candle(b, 5, T0 - 900, 99, 102, 98, 101)
        close_candle(b, 5, T0 - 300, 101, 103, 100, 102)         # пропущена одна
        self.assertFalse(ev(dict(self.cond, n=2), view(100, b))[0])

    def test_no_book_or_no_base(self):
        self.assertFalse(ev(self.cond, view(100, None))[0])
        self.assertFalse(ev(self.cond, view(100, book5((99, 102, 98, 101))), ctx={})[0])
        self.assertFalse(ev(dict(self.cond, tf="7x"), view(100, book5((99, 102, 98, 101))))[0])

    def test_entered_ms_overrides_armed_when_in_position(self):
        b = book5((99, 102, 98, 101), (101, 103, 100, 99), start=T0 - 600)
        ctx = {"armed_ms": ARMED, "entered_ms": (T0 - 300) * MS, "pos": {"avg": 100.0}}
        self.assertFalse(ev(self.cond, view(100, b), ctx=ctx)[0])      # после входа только вторая свеча (99)

    def test_forming_candle_not_counted(self):
        b = CandleBook()
        b.on_kline(5, (int((T0 - 300) * MS), 99, 102, 98, 101, 5, 5), False)
        self.assertFalse(ev(self.cond, view(100, b))[0])


class CandleRejectTest(unittest.TestCase):
    def test_short_reject(self):
        cond = {"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.05}
        ok = book5((142.0, 142.9, 141.5, 142.2))                 # прокол 142.9 >= 142.5*1.0005=142.571, закрытие ниже
        self.assertTrue(ev(cond, view(142, ok))[0])
        no_wick = book5((142.0, 142.55, 141.5, 142.2))           # прокол меньше 0.05%
        self.assertFalse(ev(cond, view(142, no_wick))[0])
        closed_above = book5((142.0, 142.9, 141.5, 142.6))
        self.assertFalse(ev(cond, view(142, closed_above))[0])
        close_eq = book5((142.0, 142.9, 141.5, 142.5))           # закрытие == уровню: не ниже
        self.assertFalse(ev(cond, view(142, close_eq))[0])

    def test_long_reject_and_zero_wick(self):
        cond = {"kind": "candle_reject", "tf": "5m", "level": 100.0, "dir": "long"}
        self.assertTrue(ev(cond, view(100, book5((100.5, 101, 99.0, 100.4))))[0])
        self.assertTrue(ev(cond, view(100, book5((100.5, 101, 100.0, 100.4))))[0])   # low == level при wick 0
        self.assertFalse(ev(cond, view(100, book5((100.5, 101, 100.1, 100.4))))[0])
        self.assertFalse(ev(cond, view(100, book5((100.5, 101, 99.0, 99.9))))[0])

    def test_precision_tolerance(self):
        cond = {"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.1}
        edge = 142.5 * (1 + 0.1 / 100)
        self.assertTrue(ev(cond, view(142, book5((142.0, edge, 141.5, 142.2))))[0])


class CandleBodyTest(unittest.TestCase):
    def test_body_and_close_pos(self):
        up = {"kind": "candle_body", "tf": "5m", "dir": "up", "min_body_pct": 1.0, "min_close_pos": 0.8}
        self.assertTrue(ev(up, view(100, book5((100.0, 101.6, 99.9, 101.5))))[0])       # тело 1.5%, закрытие на 0.94
        self.assertFalse(ev(up, view(100, book5((100.0, 102.0, 99.9, 100.9))))[0])      # тело 0.9%
        self.assertFalse(ev(up, view(100, book5((100.0, 103.0, 99.9, 101.5))))[0])      # закрытие на 0.52
        self.assertFalse(ev(up, view(100, book5((101.5, 102.0, 99.9, 100.0))))[0])      # красная
        dn = dict(up, dir="down")
        self.assertTrue(ev(dn, view(100, book5((101.5, 101.6, 99.9, 100.0))))[0])
        self.assertFalse(ev(dn, view(100, book5((101.5, 101.6, 98.0, 100.0))))[0])

    def test_doji_and_flat_range(self):
        cond = {"kind": "candle_body", "tf": "5m", "dir": "up", "min_body_pct": 0.0, "min_close_pos": 0.5}
        self.assertFalse(ev(cond, view(100, book5((100.0, 100.0, 100.0, 100.0))))[0])


class VolumeSpikeTest(unittest.TestCase):
    def build(self, last_vol, last_o=100, last_c=101):
        b = CandleBook()
        t = T0 - 600 - 20 * 300
        for i in range(20):
            close_candle(b, 5, t + i * 300, 100, 101, 99, 100, vol=10 + (i % 3))   # медиана 11
        # взвод позже, чтобы последняя свеча была после него
        close_candle(b, 5, T0 - 600, last_o, 102, 98, last_c, vol=last_vol)
        return b

    def test_threshold(self):
        cond = {"kind": "volume_spike", "tf": "5m", "mult": 1.8, "lookback": 20, "on": "close", "dir": "any"}
        ctx = {"armed_ms": (T0 - 700) * MS}
        med = self.build(0).vol_median(5, 20, before_ms=(T0 - 600) * MS)
        self.assertEqual(med, 11)
        self.assertTrue(ev(cond, view(100, self.build(1.8 * med)), ctx=ctx)[0])         # ровно на пороге
        self.assertFalse(ev(cond, view(100, self.build(1.8 * med - 0.01)), ctx=ctx)[0])

    def test_dir_filter(self):
        ctx = {"armed_ms": (T0 - 700) * MS}
        up = {"kind": "volume_spike", "tf": "5m", "mult": 1.0, "lookback": 20, "dir": "up"}
        self.assertTrue(ev(up, view(100, self.build(50)), ctx=ctx)[0])
        self.assertFalse(ev(up, view(100, self.build(50, 101, 100)), ctx=ctx)[0])
        self.assertTrue(ev(dict(up, dir="down"), view(100, self.build(50, 101, 100)), ctx=ctx)[0])

    def test_not_enough_history(self):
        b = book5((100, 101, 99, 100, 50))
        cond = {"kind": "volume_spike", "tf": "5m", "mult": 1.0, "lookback": 20}
        self.assertFalse(ev(cond, view(100, b))[0])

    def test_live_uses_forming_candle(self):
        b = self.build(1)
        b.on_kline(5, ((T0 - 300) * MS, 100, 102, 99, 101, 500, 500), False)
        cond = {"kind": "volume_spike", "tf": "5m", "mult": 5, "lookback": 20, "on": "live"}
        self.assertTrue(ev(cond, view(100, b), ctx={"armed_ms": (T0 - 700) * MS})[0])
        self.assertFalse(ev(dict(cond, on="close"), view(100, b), ctx={"armed_ms": (T0 - 700) * MS})[0])
        self.assertFalse(ev(cond, view(100, b), ctx={"armed_ms": T0 * MS})[0])           # формирующаяся начата до взвода

    def test_zero_median_not_met(self):
        b = CandleBook()
        for i in range(21):
            close_candle(b, 5, T0 - 6000 + i * 300, 100, 101, 99, 100, vol=0)
        cond = {"kind": "volume_spike", "tf": "5m", "mult": 1, "lookback": 20}
        self.assertFalse(ev(cond, view(100, b), ctx={"armed_ms": (T0 - 6000) * MS})[0])


class SimpleKindsTest(unittest.TestCase):
    def test_price_in_zone(self):
        z = {"kind": "price_in_zone", "low": 99.0, "high": 101.0}
        for px, want in ((99.0, True), (101.0, True), (100, True), (98.99, False), (101.01, False)):
            self.assertEqual(ev(z, view(px))[0], want, px)

    def test_spread(self):
        cond = {"kind": "spread_bps", "lte": 6}
        self.assertTrue(ev(cond, view(100, bid=99.99, ask=100.01))[0])         # 2 бп
        self.assertTrue(ev(cond, view(100, bid=99.97, ask=100.03))[0])         # 6 бп
        self.assertFalse(ev(cond, view(100, bid=99.96, ask=100.04))[0])        # 8 бп
        self.assertFalse(ev(cond, view(100, bid=100.1, ask=100.0))[0])         # перевёрнутый
        self.assertFalse(ev(cond, view(100, bid=0.0, ask=100.0))[0])

    def pos(self, side="long"):
        s = 1 if side == "long" else -1
        return {"avg": 100.0, "initial_stop": 100.0 - s * 2.0, "r_unit": 2.0}

    def test_pnl_r_long_short(self):
        for side, px, r in (("long", 104.0, 2.0), ("long", 98.0, -1.0), ("short", 96.0, 2.0), ("short", 102.0, -1.0)):
            ctx = {"armed_ms": ARMED, "pos": self.pos(side), "side": side}
            for k, thr, want in (("gte", r, True), ("gte", r + 0.01, False), ("gt", r, False), ("lte", r, True), ("lt", r, False),
                                 ("lt", r + 0.01, True)):
                self.assertEqual(ev({"kind": "pnl_r", k: thr}, view(px), ctx=ctx)[0], want, (side, px, k, thr))

    def test_pnl_r_side_inferred_from_stop(self):
        ctx = {"armed_ms": ARMED, "pos": self.pos("short")}
        self.assertTrue(ev({"kind": "pnl_r", "gte": 1.0}, view(98.0), ctx=ctx)[0])

    def test_pnl_r_no_position(self):
        self.assertFalse(ev({"kind": "pnl_r", "gte": 0.0}, view(98.0))[0])
        self.assertFalse(ev({"kind": "pnl_r", "lte": 99}, view(98.0), ctx={"armed_ms": ARMED, "pos": {"avg": 100.0}})[0])

    def test_pnl_r_range(self):
        ctx = {"armed_ms": ARMED, "pos": self.pos(), "side": "long"}
        c = {"kind": "pnl_r", "gte": 0.5, "lt": 1.5}
        self.assertTrue(ev(c, view(101.0), ctx=ctx)[0])
        self.assertFalse(ev(c, view(103.0), ctx=ctx)[0])
        self.assertFalse(ev(c, view(100.5), ctx=ctx)[0])

    def test_elapsed(self):
        c = {"kind": "elapsed", "min": 10}
        self.assertFalse(ev(c, view(100, now=T0), ctx={"armed_ms": (T0 - 599) * MS})[0])
        self.assertTrue(ev(c, view(100, now=T0), ctx={"armed_ms": (T0 - 600) * MS})[0])
        # в позиции считается от входа
        ctx = {"armed_ms": (T0 - 5000) * MS, "entered_ms": (T0 - 100) * MS, "pos": {"avg": 1}}
        self.assertFalse(ev(c, view(100, now=T0), ctx=ctx)[0])

    def test_target_filled(self):
        ctx = {"armed_ms": ARMED, "targets": {"t1": {"state": "filled"}, "t2": {"state": "resting", "qty": 1.0, "filled_qty": 0.4},
                                              "t3": {"state": "resting", "qty": 1.0, "filled_qty": 1.0}}}
        self.assertTrue(ev({"kind": "target_filled", "target": "t1"}, view(100), ctx=ctx)[0])
        self.assertFalse(ev({"kind": "target_filled", "target": "t2"}, view(100), ctx=ctx)[0])
        self.assertTrue(ev({"kind": "target_filled", "target": "t3"}, view(100), ctx=ctx)[0])
        self.assertFalse(ev({"kind": "target_filled", "target": "t9"}, view(100), ctx=ctx)[0])

    def test_near_target(self):
        for side, tp, px, want in (("long", 110.0, 108.0, True), ("long", 110.0, 107.9, False), ("short", 90.0, 92.0, True),
                                   ("short", 90.0, 92.1, False)):
            ctx = {"armed_ms": ARMED, "pos": {"avg": 100.0}, "targets": {"t1": {"price": tp, "state": "resting"}}}
            self.assertEqual(ev({"kind": "near_target", "target": "t1", "frac": 0.8}, view(px), ctx=ctx)[0], want, (side, px))

    def test_unknown_kind_and_garbage(self):
        for bad in ({"kind": "nope"}, {}, None, "x", 5, {"kind": "price_beyond"}, {"kind": "price_beyond", "dir": "above", "level": "abc"}):
            self.assertFalse(ev(bad, view(100))[0], bad)


class CompositeTest(unittest.TestCase):
    T = {"kind": "price_in_zone", "low": 0, "high": 1000}
    F = {"kind": "price_in_zone", "low": 1, "high": 2}
    N = {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 1}      # нет данных -> None

    def test_all_any_not(self):
        v = view(100)
        self.assertTrue(ev({"all": [self.T, self.T]}, v)[0])
        self.assertFalse(ev({"all": [self.T, self.F]}, v)[0])
        self.assertTrue(ev({"any": [self.F, self.T]}, v)[0])
        self.assertFalse(ev({"any": [self.F, self.F]}, v)[0])
        self.assertTrue(ev({"not": self.F}, v)[0])
        self.assertFalse(ev({"not": self.T}, v)[0])
        self.assertFalse(ev({"all": []}, v)[0])
        self.assertFalse(ev({"any": []}, v)[0])

    def test_unknown_propagates_not_into_signal(self):
        v = view(100, None)
        self.assertFalse(ev({"not": self.N}, v)[0])                      # нет данных: not не превращает в True
        self.assertFalse(ev({"all": [self.T, self.N]}, v)[0])
        self.assertTrue(ev({"any": [self.T, self.N]}, v)[0])
        self.assertFalse(ev({"all": [self.F, self.N]}, v)[0])

    def test_no_short_circuit_memory_updates(self):
        cond = {"all": [self.F, {"kind": "price_beyond", "dir": "above", "level": 100.0}]}
        _, st = ev(cond, view(101.0))
        self.assertTrue(st["already"]["c.1"])                            # второй лист обновил память, хотя первый ложный

    def test_nested_memory_paths_independent(self):
        cond = {"all": [{"kind": "price_beyond", "dir": "above", "level": 100.0}, {"kind": "price_beyond", "dir": "below", "level": 90.0}]}
        _, st = ev(cond, view(95.0))
        self.assertEqual(st["already"], {"c.0": False, "c.1": False})


class SeqTest(unittest.TestCase):
    cond = {"kind": "seq", "within_min": 90, "steps": [
        {"kind": "price_beyond", "dir": "below", "level": 144.0, "allow_already": True},
        {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 145.0, "n": 1}]}

    def run_seq(self, steps):
        st = {}
        res = []
        for px, now, book in steps:
            ok, st = C.eval_cond(self.cond, view(px, book, now=now), st, {"armed_ms": ARMED})
            res.append(ok)
        return res, st

    def test_order_and_latching(self):
        b0 = CandleBook()
        b1 = book5((144.5, 146.0, 143.5, 145.5))
        res, st = self.run_seq([(146.0, T0, b0), (143.9, T0 + 10, b0), (146.0, T0 + 20, b0), (146.0, T0 + 30, b1), (150.0, T0 + 40, b0)])
        self.assertEqual(res, [False, False, False, True, True])     # затем защёлка
        self.assertEqual(st["seq"]["c"]["step"], 2)

    def test_second_step_before_first_does_not_count(self):
        b1 = book5((144.5, 146.0, 143.5, 145.5))
        res, _ = self.run_seq([(146.0, T0, b1)])
        self.assertEqual(res, [False])

    def test_one_step_per_evaluation(self):
        b1 = book5((144.5, 146.0, 143.5, 145.5))
        res, st = self.run_seq([(143.9, T0, b1)])        # оба шага «готовы», но шаг 2 - только на следующем вызове
        self.assertEqual(res, [False])
        self.assertEqual(st["seq"]["c"]["step"], 1)
        res2, _ = self.run_seq([(143.9, T0, b1), (143.9, T0 + 1, b1)])
        self.assertEqual(res2, [False, True])

    def test_within_resets(self):
        b1 = book5((144.5, 146.0, 143.5, 145.5))
        res, st = self.run_seq([(143.9, T0, CandleBook()), (146, T0 + 90 * 60 + 1, b1)])
        self.assertEqual(res, [False, False])
        self.assertEqual(st["seq"]["c"]["step"], 0)

    def test_within_boundary_inclusive(self):
        b1 = book5((144.5, 146.0, 143.5, 145.5))
        res, _ = self.run_seq([(143.9, T0, CandleBook()), (146, T0 + 90 * 60, b1)])
        self.assertEqual(res, [False, True])

    def test_reset_then_restart(self):
        res, st = self.run_seq([(143.9, T0, CandleBook()), (146, T0 + 90 * 60 + 1, CandleBook()), (143.0, T0 + 90 * 60 + 2, CandleBook())])
        self.assertEqual(st["seq"]["c"]["step"], 1)

    def test_empty_seq(self):
        self.assertFalse(ev({"kind": "seq", "steps": []}, view(100))[0])


class DescribeTest(unittest.TestCase):
    def test_describe_price(self):
        d = C.describe({"kind": "price_beyond", "dir": "above", "level": 110.0}, view(100.0), {}, {"armed_ms": ARMED})
        self.assertFalse(d["met"])
        self.assertIn("цена выше 110", d["text"])
        self.assertAlmostEqual(d["dist_pct"], 10.0, 3)

    def test_describe_met(self):
        d = C.describe({"kind": "price_in_zone", "low": 1, "high": 1000}, view(100.0), {}, {})
        self.assertTrue(d["met"])
        self.assertEqual(d["dist_pct"], 0.0)

    def test_describe_candle_eta(self):
        d = C.describe({"kind": "candle_close", "tf": "5m", "dir": "above", "level": 101.0, "n": 1}, view(100.0, now=T0 + 170), {},
                       {"armed_ms": ARMED})
        self.assertEqual(d["eta_s"], 130)
        self.assertIn("5м закрытие выше 101", d["text"])

    def test_describe_elapsed_eta(self):
        d = C.describe({"kind": "elapsed", "min": 10}, view(100.0, now=T0), {}, {"armed_ms": (T0 - 400) * MS})
        self.assertEqual(d["eta_s"], 200)

    def test_describe_composite_first_open_leaf(self):
        cond = {"all": [{"kind": "price_in_zone", "low": 1, "high": 1000}, {"kind": "price_beyond", "dir": "below", "level": 90.0}]}
        d = C.describe(cond, view(100.0), {}, {"armed_ms": ARMED})
        self.assertAlmostEqual(d["dist_pct"], 10.0, 3)
        self.assertIn(" И ", d["text"])

    def test_describe_seq_current_step(self):
        cond = {"kind": "seq", "within_min": 90, "steps": [{"kind": "price_in_zone", "low": 1, "high": 1000},
                                                           {"kind": "price_beyond", "dir": "above", "level": 120.0}]}
        ok, st = C.eval_cond(cond, view(100.0), {}, {"armed_ms": ARMED})
        d = C.describe(cond, view(100.0), st, {"armed_ms": ARMED})
        self.assertAlmostEqual(d["dist_pct"], 20.0, 3)

    def test_describe_never_raises(self):
        for bad in (None, {}, {"kind": "x"}, {"all": [None]}):
            self.assertIn("met", C.describe(bad, view(100), {}, {}))


class PurityTest(unittest.TestCase):
    def test_determinism_and_no_mutation(self):
        b = book5((99, 102, 98, 101), (101, 103, 100, 102))
        v = view(100.0, b)
        cond = {"all": [{"kind": "candle_close", "tf": "5m", "dir": "above", "level": 100.0, "n": 2},
                        {"kind": "price_beyond", "dir": "below", "level": 101.0}]}
        ctx = {"armed_ms": ARMED, "targets": {"t1": {}}}
        st = {"already": {"c.1": False}}
        before = copy.deepcopy((cond, ctx, st))
        r1 = C.eval_cond(cond, v, st, ctx)
        r2 = C.eval_cond(cond, v, st, ctx)
        self.assertEqual(r1, r2)
        self.assertEqual((cond, ctx, st), before)

    def test_r_multiple(self):
        self.assertEqual(C.r_multiple({"avg": 100.0, "initial_stop": 98.0}, 104.0), 2.0)
        self.assertEqual(C.r_multiple({"avg": 100.0, "initial_stop": 102.0}, 96.0), 2.0)
        self.assertIsNone(C.r_multiple({"avg": 100.0, "r_unit": 2.0}, 104.0))        # сторону не определить
        self.assertEqual(C.r_multiple({"avg": 100.0, "r_unit": 2.0}, 104.0, 1), 2.0)
        self.assertIsNone(C.r_multiple(None, 1))
        self.assertIsNone(C.r_multiple({"avg": 100.0, "initial_stop": 100.0}, 104.0, 1))


if __name__ == "__main__":
    unittest.main()
