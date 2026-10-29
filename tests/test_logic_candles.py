"""WP3: CandleBook и MarketView (агрегация свечей, confirm, дыры, подкачка seed)."""
import unittest

import _bootstrap  # noqa: F401
from _logic_helpers import T0, MS, row, close_candle, view
from tb.logic.candles import CandleBook, MarketView, Candle, tf_min

F = 300 * MS   # шаг 5м в мс


class TfTest(unittest.TestCase):
    def test_tf_min(self):
        self.assertEqual([tf_min(x) for x in (1, "5", "5m", "15m", "1h", "60", 60)], [1, 5, 5, 15, 60, 60, 60])
        for bad in (None, "x", "", 0, -5, True, "m"):
            self.assertIsNone(tf_min(bad))


class OnKlineTest(unittest.TestCase):
    def test_forming_then_confirm(self):
        b = CandleBook()
        self.assertIsNone(b.on_kline(5, row(T0 * MS, 1, 2, 1, 1.5), False))
        self.assertEqual(b.forming(5).c, 1.5)
        self.assertFalse(b.forming(5).confirm)
        self.assertEqual(b.last_closed(5, 3), [])
        c = b.on_kline(5, row(T0 * MS, 1, 2, 0.9, 1.7), True)
        self.assertIsInstance(c, Candle)
        self.assertTrue(c.confirm)
        self.assertIsNone(b.forming(5))
        self.assertEqual(b.last_closed(5)[-1].c, 1.7)

    def test_duplicate_confirm_returns_none(self):
        b = CandleBook()
        self.assertIsNotNone(b.on_kline(5, row(T0 * MS, 1, 2, 1, 1.5), True))
        self.assertIsNone(b.on_kline(5, row(T0 * MS, 1, 2, 1, 1.5), True))
        self.assertEqual(len(b.last_closed(5, 10)), 1)

    def test_late_forming_push_after_close_ignored(self):
        b = CandleBook()
        b.on_kline(5, row(T0 * MS, 1, 2, 1, 1.5), True)
        self.assertIsNone(b.on_kline(5, row(T0 * MS, 1, 3, 1, 2.5), False))
        self.assertIsNone(b.forming(5))
        self.assertEqual(b.last_closed(5)[-1].c, 1.5)

    def test_gap_detected_and_closed_by_late_candle(self):
        b = CandleBook()
        close_candle(b, 5, T0, 1, 2, 1, 1.5)
        close_candle(b, 5, T0 + 900, 1, 2, 1, 1.5)       # пропущены T0+300 и T0+600
        self.assertEqual(b.gaps[5], ((T0 + 300) * MS, (T0 + 600) * MS))
        close_candle(b, 5, T0 + 300, 1, 2, 1, 1.5)
        self.assertEqual(b.gaps[5], ((T0 + 600) * MS, (T0 + 600) * MS))
        close_candle(b, 5, T0 + 600, 1, 2, 1, 1.5)
        self.assertNotIn(5, b.gaps)

    def test_unconfirmed_predecessor_lost_registers_gap(self):
        b = CandleBook()
        close_candle(b, 5, T0, 1, 2, 1, 1.5)
        b.on_kline(5, row((T0 + 300) * MS, 1, 2, 1, 1.5), False)
        b.on_kline(5, row((T0 + 600) * MS, 1, 2, 1, 1.5), False)   # подтверждения T0+300 не было
        self.assertIn(5, b.gaps)

    def test_garbage_rows_ignored(self):
        b = CandleBook()
        for r in (None, (), (1,), ("a", 1, 2, 1, 1, 1, 1), (T0, float("nan"), 2, 1, 1, 1, 1), (T0, 1, 1, 2, 1, 1, 1), (T0, -1, 2, 1, 1, 1, 1)):
            self.assertIsNone(b.on_kline(5, r, True))
        self.assertEqual(b.last_closed(5, 5), [])
        self.assertIsNone(b.on_kline("zz", row(T0, 1, 2, 1, 1), True))

    def test_turnover_optional(self):
        b = CandleBook()
        self.assertEqual(b.on_kline(5, (T0 * MS, 1, 2, 1, 1.5, 7), True).turn, 0.0)

    def test_independent_timeframes(self):
        b = CandleBook()
        close_candle(b, 1, T0, 1, 2, 1, 1.5)
        close_candle(b, "5m", T0, 1, 2, 1, 1.6)
        self.assertEqual(b.tfs(), [1, 5])
        self.assertEqual(b.last_closed(1)[-1].c, 1.5)
        self.assertEqual(b.last_closed(5)[-1].c, 1.6)


class SeedTest(unittest.TestCase):
    def rows(self, n, start=T0):
        return [row((start + i * 300) * MS, 100, 101, 99, 100 + i, 5) for i in range(n)]

    def test_last_row_forming_when_not_finished(self):
        b = CandleBook()
        rows = self.rows(5)
        now = T0 + 4 * 300 + 100                      # последняя ещё идёт
        b.seed(5, rows, now=now)
        self.assertEqual(len(b.last_closed(5, 10)), 4)
        self.assertEqual(b.forming(5).start, rows[-1][0])

    def test_last_row_closed_when_finished(self):
        b = CandleBook()
        b.seed(5, self.rows(5), now=T0 + 5 * 300)
        self.assertEqual(len(b.last_closed(5, 10)), 5)
        self.assertIsNone(b.forming(5))

    def test_boundary_start_plus_tf_equals_now_is_closed(self):
        b = CandleBook()
        b.seed(5, self.rows(1), now=T0 + 300)
        self.assertEqual(len(b.last_closed(5, 3)), 1)

    def test_without_now_last_is_forming(self):
        b = CandleBook()
        b.seed(5, self.rows(3))
        self.assertEqual(len(b.last_closed(5, 10)), 2)
        self.assertIsNotNone(b.forming(5))

    def test_unsorted_and_duplicate_rows(self):
        b = CandleBook()
        rows = self.rows(4)
        b.seed(5, list(reversed(rows)) + rows[:2], now=T0 + 10 * 300)
        self.assertEqual([c.start for c in b.last_closed(5, 10)], [r[0] for r in rows])

    def test_seed_idempotent(self):
        b = CandleBook()
        b.seed(5, self.rows(6), now=T0 + 6 * 300)
        snap = b.last_closed(5, 100)
        b.seed(5, self.rows(6), now=T0 + 6 * 300)
        self.assertEqual(b.last_closed(5, 100), snap)

    def test_seed_fills_gap(self):
        b = CandleBook()
        close_candle(b, 5, T0, 1, 2, 1, 1.5)
        close_candle(b, 5, T0 + 900, 1, 2, 1, 1.5)
        self.assertIn(5, b.gaps)
        b.seed(5, self.rows(4), now=T0 + 4 * 300)
        self.assertNotIn(5, b.gaps)
        self.assertEqual(len(b.last_closed(5, 10)), 4)

    def test_seed_then_push_continues(self):
        b = CandleBook()
        b.seed(5, self.rows(3), now=T0 + 3 * 300 - 50)     # последняя формируется
        c = b.on_kline(5, row((T0 + 600) * MS, 100, 105, 99, 104), True)
        self.assertEqual(c.c, 104.0)
        self.assertIsNone(b.forming(5))
        self.assertEqual(len(b.last_closed(5, 10)), 3)

    def test_closed_since(self):
        b = CandleBook()
        b.seed(5, self.rows(5), now=T0 + 5 * 300)
        self.assertEqual(len(b.closed_since(5, (T0 + 600) * MS)), 3)
        self.assertEqual(len(b.closed_since(5, (T0 + 601) * MS)), 2)
        self.assertEqual(b.closed_since(5, (T0 + 99999) * MS), [])

    def test_trim_keeps_recent(self):
        from tb.logic import candles
        b = CandleBook()
        old = candles.MAX_KEEP
        candles.MAX_KEEP = 5
        try:
            b.seed(5, self.rows(12), now=T0 + 12 * 300)
        finally:
            candles.MAX_KEEP = old
        self.assertEqual(len(b.last_closed(5, 100)), 5)
        self.assertEqual(b.last_closed(5, 1)[0].start, (T0 + 11 * 300) * MS)


class IndicatorsTest(unittest.TestCase):
    def test_atr_wilder(self):
        b = CandleBook()
        # постоянный диапазон 2, закрытия без гэпов: TR = 2
        for i in range(20):
            close_candle(b, 5, T0 + i * 300, 100, 101, 99, 100)
        self.assertAlmostEqual(b.atr(5, 14), 2.0)
        # одна широкая свеча сглаживается по Уайлдеру
        close_candle(b, 5, T0 + 20 * 300, 100, 110, 90, 100)
        self.assertAlmostEqual(b.atr(5, 14), (2.0 * 13 + 20.0) / 14)

    def test_atr_uses_previous_close_gap(self):
        b = CandleBook()
        close_candle(b, 5, T0, 100, 101, 99, 100)
        close_candle(b, 5, T0 + 300, 110, 111, 109, 110)    # гэп вверх: TR = |111-100| = 11
        self.assertAlmostEqual(b.atr(5, 1), 11.0)

    def test_atr_not_enough_history(self):
        b = CandleBook()
        for i in range(14):
            close_candle(b, 5, T0 + i * 300, 100, 101, 99, 100)
        self.assertIsNone(b.atr(5, 14))
        close_candle(b, 5, T0 + 14 * 300, 100, 101, 99, 100)
        self.assertIsNotNone(b.atr(5, 14))
        self.assertIsNone(b.atr(1, 14))
        self.assertIsNone(b.atr(5, 0))

    def test_vol_median(self):
        b = CandleBook()
        for i, v in enumerate([1, 9, 5, 3, 7]):
            close_candle(b, 5, T0 + i * 300, 100, 101, 99, 100, vol=v)
        self.assertEqual(b.vol_median(5, 5), 5)
        self.assertEqual(b.vol_median(5, 4), 6.0)         # [9,5,3,7] -> (5+7)/2
        self.assertIsNone(b.vol_median(5, 6))
        self.assertEqual(b.vol_median(5, 3, before_ms=(T0 + 3 * 300) * MS), 5)   # [1,9,5] -> 5

    def test_swing_levels(self):
        b = CandleBook()
        highs = [10, 11, 12, 15, 12, 11, 10, 9, 8, 9, 10, 11]
        lows = [h - 2 for h in highs]
        for i, (h, l) in enumerate(zip(highs, lows)):
            close_candle(b, 5, T0 + i * 300, h - 1, h, l, h - 1)
        sw = b.swing_levels(5, 3, 3, 4)
        self.assertIn(15.0, sw)         # максимум на индексе 3
        self.assertIn(6.0, sw)          # минимум low=8-2 на индексе 8
        self.assertEqual(sw, sorted(sw, key=lambda x: sw.index(x)))   # порядок по времени сохранён
        self.assertEqual(b.swing_levels(5, 3, 3, 1), [6.0])
        self.assertEqual(b.swing_levels(5, 3, 3, 0), [])

    def test_swing_needs_right_neighbours(self):
        b = CandleBook()
        for i, h in enumerate([1, 2, 3, 9]):
            close_candle(b, 5, T0 + i * 300, h, h + 0.5, h - 0.5, h)
        self.assertEqual(b.swing_levels(5, 2, 2, 4), [])


class ViewTest(unittest.TestCase):
    def test_price_age(self):
        self.assertEqual(MarketView("S", 1, 1, 1, 1, 100.0, None, 102.5).price_age(), 2.5)
        self.assertEqual(MarketView("S", 1, 1, 1, 1, 100.0, None, 99.0).price_age(), 0.0)
        self.assertEqual(MarketView("S", 1, 1, 1, 1, None, None, 99.0).price_age(), float("inf"))


class DoctestTest(unittest.TestCase):
    def test_docstring_examples(self):
        import doctest
        from tb.logic import candles, conds, levels, pyramid, rules
        for m in (candles, conds, levels, pyramid, rules):
            res = doctest.testmod(m)
            self.assertEqual(res.failed, 0, m.__name__)


if __name__ == "__main__":
    unittest.main()
