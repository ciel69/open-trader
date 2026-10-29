import os
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import data


class TestData(unittest.TestCase):
    def test_append_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "ticks_ETH.csv")
            data.append_tick(p, "2026-10-27T06:30:00Z", 2313.67)
            data.append_tick(p, "2026-10-27T06:31:00Z", 2314.5)
            t = data.load_ticks(p)
            self.assertEqual(len(t), 2)
            self.assertEqual(t[1][0] - t[0][0], 60.0)
            self.assertAlmostEqual(t[0][1], 2313.67)
            self.assertEqual(len(data.load_ticks(p, max_rows=1)), 1)
            self.assertAlmostEqual(data.load_ticks(p, max_rows=1)[0][1], 2314.5)

    def test_load_missing_and_bad_rows(self):
        self.assertEqual(data.load_ticks("/nonexistent/x.csv"), [])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.csv")
            with open(p, "w") as f:
                f.write("ts_iso,price\n2026-10-27T06:30:00Z,100\nbad,line\n2026-10-27T06:31:00Z,-5\n")
            self.assertEqual(len(data.load_ticks(p)), 1)

    def test_to_bars(self):
        ticks = [(0, 10), (60, 12), (120, 9), (300, 11), (330, 13)]
        bars = data.to_bars(ticks, 300)
        self.assertEqual(len(bars), 2)
        self.assertEqual((bars[0]["o"], bars[0]["h"], bars[0]["l"], bars[0]["c"]), (10, 12, 9, 9))
        self.assertEqual((bars[1]["o"], bars[1]["c"], bars[1]["ts"]), (11, 13, 300.0))
        self.assertEqual(len(data.to_bars(ticks, 300, drop_last=True)), 1)

    def test_synthetic_deterministic_and_regimes(self):
        for reg in ("mixed", "trend", "range"):
            a = data.synthetic_ticks(500, 7, reg)
            b = data.synthetic_ticks(500, 7, reg)
            c = data.synthetic_ticks(500, 8, reg)
            self.assertEqual(a, b)
            self.assertNotEqual(a, c)
            self.assertEqual(a[1][0] - a[0][0], 60.0)
            self.assertTrue(all(p > 0 for _, p in a))


if __name__ == "__main__":
    unittest.main()
