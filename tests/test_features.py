import math
import os
import sys
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import features as f  # noqa: E402

REG = {"storm_rv_5m": 0.004, "trend_atr_mult": 0.5}


class FeatureTests(unittest.TestCase):
    def test_ema(self):
        self.assertIsNone(f.ema([], 5))
        self.assertAlmostEqual(f.ema([1, 2], 3), 1.5)  # alpha=0.5
        self.assertAlmostEqual(f.ema([7.0] * 20, 9), 7.0)

    def test_window_return(self):
        s = [(t, 100.0 + t) for t in range(0, 601)]
        self.assertAlmostEqual(f.window_return(s, 600, 300), 700.0 / 400.0 - 1)
        self.assertIsNone(f.window_return(s, 600, 1000))
        self.assertIsNone(f.window_return([], 1, 1))

    def test_realized_vol(self):
        flat = [(t, 100.0) for t in range(0, 301)]
        self.assertEqual(f.realized_vol(flat, 300, 300, 5), 0.0)
        # знакопеременные шаги +-0.1% каждые 5 с -> sqrt(60)*0.001
        s = [(t, 100.0 * (1.001 if (t // 5) % 2 else 1.0)) for t in range(0, 301)]
        rv = f.realized_vol(s, 300, 300, 5)
        self.assertAlmostEqual(rv, math.sqrt(60) * math.log(1.001), delta=0.002)
        self.assertIsNone(f.realized_vol(s[:3], 300, 300, 5))

    def test_book(self):
        self.assertEqual(f.book_imbalance([(10, 3)], [(11, 1)]), 0.5)
        self.assertEqual(f.book_imbalance([(10, 1)], [(11, 3)]), -0.5)
        self.assertIsNone(f.book_imbalance([], []))
        self.assertEqual(f.book_imbalance([(10, 1), (9, 100)], [(11, 1)], n=1), 0.0)
        self.assertAlmostEqual(f.spread_bps(99.99, 100.01), 2.0)
        self.assertIsNone(f.spread_bps(101, 100))
        self.assertIsNone(f.spread_bps(None, 100))

    def test_cvd_volume(self):
        tr = [(1, 2.0, "buy"), (50, 1.0, "sell"), (58, 4.0, "buy")]
        self.assertEqual(f.cvd(tr, 60, 60), 5.0)
        self.assertEqual(f.cvd(tr, 60, 5), 4.0)
        self.assertEqual(f.volume(tr, 60, 15), 5.0)

    def test_oi_change(self):
        s = [(0, 100.0), (150, 101.0), (300, 102.0)]
        self.assertAlmostEqual(f.oi_change(s, 300, 300), 0.02)

    def test_bars_atr(self):
        s = [(0, 10), (30, 12), (59, 11), (60, 11), (90, 9), (119, 10)]
        b = f.bars_from_series(s, 60)
        self.assertEqual(len(b), 2)
        self.assertEqual((b[0]["h"], b[0]["l"], b[0]["c"]), (12, 10, 11))
        self.assertAlmostEqual(f.atr(b, 14), 2.0)  # max(H-L=2, |H-pc|=0, |L-pc|=2)
        self.assertIsNone(f.atr(b[:1]))

    def test_regime(self):
        c = f.classify_regime
        self.assertEqual(c(0.005, 0, 0, 1, 0, REG), "storm")
        self.assertEqual(c(0.001, 103, 100, 2, 0.01, REG), "trend_up")
        self.assertEqual(c(0.001, 97, 100, 2, -0.01, REG), "trend_down")
        self.assertEqual(c(0.001, 103, 100, 2, -0.01, REG), "range")  # несогласованный ret_15m
        self.assertEqual(c(0.001, 100.2, 100, 2, 0.01, REG), "range")
        self.assertEqual(c(0.001, None, 100, 2, 0.01, REG), "unknown")
        self.assertEqual(c(0.001, 101, 100, 0, 0.01, REG), "unknown")
        self.assertEqual(c(None, 103, 100, 2, 0.01, REG), "trend_up")


if __name__ == "__main__":
    unittest.main()
