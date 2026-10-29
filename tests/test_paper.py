"""logic/paper.py (WP7): таблицы заполнений без часов и файлов (вход/выход, комиссии, проскальзывание, гэп через стоп, касание лимита, funding)."""
import unittest

import _bootstrap  # noqa: F401
from tb.logic import paper as P


class TestFillMarket(unittest.TestCase):
    def test_table(self):
        # side, bid, ask, slip_bps, fee -> px
        rows = [("buy", 99.0, 100.0, 0, 100.0), ("buy", 99.0, 100.0, 10, 100.1), ("long", 99.0, 100.0, 10, 100.1),
                ("sell", 99.0, 100.0, 0, 99.0), ("sell", 99.0, 100.0, 10, 98.901), ("short", 99.0, 100.0, 10, 98.901)]
        for side, bid, ask, slip, px in rows:
            r = P.fill_market(side, 2.0, bid, ask, slip, 0.001)
            self.assertAlmostEqual(r["px"], px, 6, msg=str((side, slip)))
            self.assertAlmostEqual(r["fee"], px * 2.0 * 0.001, 8)


class TestLimitTouch(unittest.TestCase):
    def test_buy_table(self):
        for last, want in ((100.5, False), (100.0, False), (99.995, False), (99.99, True), (99.0, True)):
            self.assertEqual(P.limit_touch("buy", 100.0, last, 0.01, 1), want, msg=str(last))

    def test_sell_table(self):
        for last, want in ((99.5, False), (100.0, False), (100.01, True), (101.0, True)):
            self.assertEqual(P.limit_touch("sell", 100.0, last, 0.01, 1), want, msg=str(last))

    def test_through_ticks_two_and_zero(self):
        self.assertFalse(P.limit_touch("buy", 100.0, 99.99, 0.01, 2))
        self.assertTrue(P.limit_touch("buy", 100.0, 99.98, 0.01, 2))
        self.assertTrue(P.limit_touch("buy", 100.0, 99.999, 0.01, 0))
        self.assertFalse(P.limit_touch("buy", 100.0, 100.0, 0.01, 0))


class TestStopTrigger(unittest.TestCase):
    def test_long_table(self):
        self.assertIsNone(P.stop_trigger("long", 95.0, 95.01, 95.0, 95.02, 5))
        self.assertAlmostEqual(P.stop_trigger("long", 95.0, 95.0, 94.99, 95.01, 5)["px"], 94.99 * 0.9995, 6)
        self.assertAlmostEqual(P.stop_trigger("long", 95.0, 95.0, 95.0, 95.1, 5)["px"], 95.0 * 0.9995, 6)

    def test_gap_uses_worse_of_stop_and_book(self):
        self.assertAlmostEqual(P.stop_trigger("long", 95.0, 90.0, 89.9, 90.1, 5)["px"], 89.9 * 0.9995, 6)
        self.assertAlmostEqual(P.stop_trigger("short", 105.0, 110.0, 109.9, 110.1, 5)["px"], 110.1 * 1.0005, 6)

    def test_short(self):
        self.assertIsNone(P.stop_trigger("short", 105.0, 104.99, 104.9, 105.0, 5))
        self.assertAlmostEqual(P.stop_trigger("short", 105.0, 105.0, 104.9, 105.1, 5)["px"], 105.1 * 1.0005, 6)

    def test_bad_input(self):
        self.assertIsNone(P.stop_trigger("long", 0, 90.0, 89, 91, 5))
        self.assertIsNone(P.stop_trigger("long", 95.0, 0, 0, 0, 5))


class TestFunding(unittest.TestCase):
    def test_sign(self):
        self.assertAlmostEqual(P.apply_funding("long", 1000.0, 0.0001), -0.1)
        self.assertAlmostEqual(P.apply_funding("short", 1000.0, 0.0001), 0.1)
        self.assertAlmostEqual(P.apply_funding("long", 1000.0, -0.0002), 0.2)

    def test_marks_crossed(self):
        h = 3600.0
        self.assertEqual(P.funding_marks_crossed(7 * h, 7.9 * h), 0)
        self.assertEqual(P.funding_marks_crossed(7.9 * h, 8 * h), 1)       # ровно метка: пересечена
        self.assertEqual(P.funding_marks_crossed(8 * h, 8.5 * h), 0)
        self.assertEqual(P.funding_marks_crossed(0.5 * h, 17 * h), 2)
        self.assertEqual(P.funding_marks_crossed(10 * h, 5 * h), 0)
        self.assertEqual(P.funding_marks_crossed(None, 5 * h), 0)


class TestHelpers(unittest.TestCase):
    def test_floor_step(self):
        self.assertEqual(P.floor_step(1.37, 0.1), 1.3)
        self.assertEqual(P.floor_step(0.3, 0.1), 0.3)        # 0.3/0.1 = 2.9999999999999996 не должно терять шаг
        self.assertEqual(P.floor_step(5, 0), 5)

    def test_liq_price(self):
        self.assertAlmostEqual(P.liq_price("long", 100.0, 10, 0.005), 90.5)
        self.assertAlmostEqual(P.liq_price("short", 100.0, 10, 0.005), 109.5)
        self.assertEqual(P.liq_price("long", 100.0, 0, 0.005), 0.0)


class TestStateDirect(unittest.TestCase):
    def mk(self, **cfg):
        s = P.PaperState(40.0, dict({"latency_ms": 0}, **cfg))
        s.on_tick("SOL", 100.0, 99.95, 100.05, 100.0, None, 1000.0)
        return s

    def order(self, link, side, typ, qty, price=0.0, ro=False, sl=None):
        return {"sym": "SOL", "link_id": link, "side": side, "type": typ, "qty": qty, "price": price, "reduce_only": ro, "post_only": False, "sl": sl}

    def test_no_events_without_quote(self):
        s = P.PaperState(40.0)
        self.assertEqual(s.on_tick("SOL", 0, 0, 0, 0, None, 1.0), [])

    def test_snapshot_restore_equal(self):
        s = self.mk()
        s.submit(self.order("l1", "Buy", "market", 1.0, sl=95.0), 1001.0)
        r = P.PaperState.restore(s.snapshot())
        self.assertEqual(r.pos, s.pos)
        self.assertEqual(r.account(), s.account())

    def test_dirty_flag_set_by_executions_and_orders(self):
        """Изменения, которые нельзя терять при жёстком перезапуске (исполнение, ордер, стоп), помечают состояние для немедленной записи."""
        s = self.mk()
        self.assertFalse(s.dirty)
        s.submit(self.order("l1", "Buy", "market", 1.0, sl=95.0), 1001.0)
        self.assertTrue(s.dirty)
        self.assertTrue(s.execs)
        s.dirty = False
        s.set_stop("SOL", 97.0, "last")
        self.assertTrue(s.dirty)
        s.dirty = False
        s.on_tick("SOL", 100.5, 100.45, 100.55, 100.5, None, 1002.0)       # котировка без исполнений состояние не трогает
        self.assertFalse(s.dirty)

    def test_two_identical_runs_equal(self):
        def run():
            s = self.mk()
            s.submit(self.order("l1", "Buy", "market", 1.0, sl=95.0), 1001.0)
            for i, px in enumerate((101, 99, 96, 94)):
                s.on_tick("SOL", px, px - .05, px + .05, px, None, 1002.0 + i)
            return s.snapshot()
        self.assertEqual(run(), run())

    def test_add_to_position_averages_and_keeps_tighter_stop(self):
        s = self.mk()
        s.submit(self.order("l1", "Buy", "market", 1.0, sl=95.0), 1001.0)
        s.on_tick("SOL", 102.0, 101.95, 102.05, 102.0, None, 1002.0)
        s.submit(self.order("l2", "Buy", "market", 1.0, sl=99.0), 1003.0)
        p = s.pos["SOL"]
        self.assertEqual(p["size"], 2.0)
        self.assertEqual(p["sl"], 99.0)
        self.assertAlmostEqual(p["avg"], (100.05 * 1.0002 + 102.05 * 1.0002) / 2.0, 6)


if __name__ == "__main__":
    unittest.main()
