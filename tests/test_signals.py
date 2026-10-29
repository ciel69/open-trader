import copy
import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401
from tb.legacy_engine import signals

CFG = json.load(open(os.path.join(ROOT, "config", "config.json")))


def mk_bars(closes, wick=0.0004):
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append({"ts": i * 300.0, "o": o, "h": max(o, c) * (1 + wick), "l": min(o, c) * (1 - wick), "c": c})
        prev = c
    return bars


def trend(n=80, step=0.0012, start=100.0):
    return mk_bars([start * (1 + step) ** i for i in range(n)], wick=0.0012)


class TestSignals(unittest.TestCase):
    def test_warmup(self):
        r = signals.compute_signal(trend(10), 0.0001, CFG)
        self.assertTrue(r["warmup"])
        self.assertEqual(r["action"], "flat")
        self.assertEqual(signals.compute_signal([], 0.0001, CFG)["action"], "flat")

    def test_uptrend_long(self):
        r = signals.compute_signal(trend(), 0.0001, CFG)
        self.assertEqual(r["action"], "long", r["reasons"])
        self.assertFalse(r["warmup"])
        self.assertGreater(r["score"], 0)
        self.assertLessEqual(r["score"], 1)
        s = CFG["strategy"]
        self.assertTrue(s["stop_pct_min"] <= r["stop_pct"] <= s["stop_pct_max"])
        self.assertAlmostEqual(r["tp_pct"], r["stop_pct"] * s["tp_rr"])

    def test_downtrend_short(self):
        r = signals.compute_signal(trend(step=-0.0012, start=200.0), 0.0001, CFG)
        self.assertEqual(r["action"], "short", r["reasons"])

    def test_sideways_flat(self):
        for n in range(60, 73):
            closes = [100 * (1 + 0.003 * math.sin(2 * math.pi * i / 12.0)) for i in range(n)]
            r = signals.compute_signal(mk_bars(closes), 0.0001, CFG)
            self.assertEqual(r["action"], "flat", (n, r["reasons"]))

    def test_low_atr_flat(self):
        r = signals.compute_signal(trend(step=0.00005, start=100.0), 0.0001, CFG)
        self.assertEqual(r["action"], "flat")
        self.assertTrue(any("ATR" in x for x in r["reasons"]))

    def test_high_atr_flat(self):
        r = signals.compute_signal(mk_bars([100 * 1.02 ** i for i in range(80)], wick=0.01), 0.0001, CFG)
        self.assertEqual(r["action"], "flat")

    def test_edge_filter_blocks_with_high_costs(self):
        self.assertEqual(signals.compute_signal(trend(), 0.0001, CFG)["action"], "long")
        cfg = copy.deepcopy(CFG)
        cfg["costs"]["taker_fee_pct"] = 0.01  # 1% за сторону
        r = signals.compute_signal(trend(), 0.0001, cfg)
        self.assertEqual(r["action"], "flat")
        self.assertTrue(any(x.startswith("edge") for x in r["reasons"]), r["reasons"])

    def test_edge_filter_blocks_with_high_funding(self):
        r = signals.compute_signal(trend(), 0.01, CFG)  # 1%/час, лонг платит
        self.assertEqual(r["action"], "flat")

    def test_cost_units(self):
        # 2*0.09% + 0.05% + 0.01%*4h = 0.27 (в процентах цены)
        self.assertAlmostEqual(signals.round_trip_cost_pct(CFG, 0.0001, "long"), 0.27)

    def test_deterministic(self):
        b = trend()
        self.assertEqual(signals.compute_signal(b, 0.0001, CFG), signals.compute_signal(b, 0.0001, CFG))


if __name__ == "__main__":
    unittest.main()
