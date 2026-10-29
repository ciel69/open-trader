import json
import os
import unittest

import _bootstrap  # noqa: F401
from tb.legacy_engine import sizing

CFG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.json")))
CONTRACT = {"max_leverage": 50, "min_size_quote": "1.0", "taker_fee_pct": "0.0009"}


def size(score=0.8, stop=1.2, equity=30.0, avail=20.0, side="long", ctx=None, contract=CONTRACT):
    return sizing.dynamic_size({"score": score, "stop_pct": stop}, side, equity, avail, contract, CFG, ctx)


class SizingTest(unittest.TestCase):
    def test_within_hard_caps(self):
        r = size()
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["leverage"], CFG["risk"]["leverage"])
        self.assertLessEqual(r["margin"], CFG["risk"]["margin_per_trade"])

    def test_higher_score_not_smaller(self):
        self.assertGreaterEqual(size(score=1.0)["margin"] * size(score=1.0)["leverage"],
                                size(score=0.1)["margin"] * size(score=0.1)["leverage"])

    def test_wider_stop_means_lower_or_equal_leverage(self):
        self.assertGreaterEqual(size(stop=0.7)["leverage"], size(stop=2.0)["leverage"])

    def test_storm_and_risk_off_block(self):
        self.assertFalse(size(ctx={"regime": "storm"})["ok"])
        self.assertFalse(size(ctx={"risk_off": True})["ok"])

    def test_counter_trend_smaller_than_aligned(self):
        a = size(ctx={"regime": "trend_up"}, side="long")
        c = size(ctx={"regime": "trend_up"}, side="short")
        self.assertGreaterEqual(a["risk_usdt"], c["risk_usdt"])

    def test_limited_by_available_funds(self):
        r = size(avail=2.0)
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["margin"], 2.0)

    def test_below_min_size_rejected(self):
        self.assertFalse(size(avail=0.5)["ok"])

    def test_contract_max_leverage_respected(self):
        r = size(stop=0.7, contract={"max_leverage": 2, "min_size_quote": "1.0"})
        self.assertLessEqual(r["leverage"], 2)

    def test_invalid_inputs(self):
        self.assertFalse(sizing.dynamic_size({}, "long", 30, 20, CONTRACT, CFG)["ok"])
        self.assertFalse(size(equity=0)["ok"])

    def test_liquidation_buffer(self):
        r = size(stop=60.0)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
