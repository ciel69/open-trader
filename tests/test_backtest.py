import copy
import json
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401
from tb.research import backtest
from tb.marketdata import data

CFG = json.load(open(os.path.join(ROOT, "config", "config.json")))


def strong_trend_ticks(n=3000, drift=0.00015, noise=0.0007, seed=3):
    rng = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        p *= 1 + drift + noise * rng.gauss(0, 1)
        out.append((1767225600.0 + 60 * i, p))
    return out


def zero_cost(cfg):
    c = copy.deepcopy(cfg)
    c["costs"].update({"taker_fee_pct": 0.0, "slippage_buffer_pct": 0.0, "funding_hourly_assumed": 0.0})
    return c


class TestBacktest(unittest.TestCase):
    def test_zero_costs_strong_trend_profit(self):
        m = backtest.run_backtest(strong_trend_ticks(), zero_cost(CFG))
        self.assertGreater(m["trades"], 0)
        self.assertGreater(m["pnl_usdt"], 0)
        self.assertAlmostEqual(m["costs_usdt"], 0.0)

    def test_high_fee_loses(self):
        cfg = zero_cost(CFG)
        cfg["costs"]["taker_fee_pct"] = 0.02  # 2% за сторону
        cfg["strategy"]["min_edge_mult"] = 0.0  # отключаем edge-фильтр, чтобы сделки были
        m = backtest.run_backtest(strong_trend_ticks(), cfg)
        self.assertGreater(m["trades"], 0)
        self.assertLess(m["pnl_usdt"], 0)

    def test_high_fee_blocked_by_edge_filter(self):
        cfg = zero_cost(CFG)
        cfg["costs"]["taker_fee_pct"] = 0.02
        m = backtest.run_backtest(strong_trend_ticks(), cfg)
        self.assertEqual(m["trades"], 0)

    def test_deterministic_by_seed(self):
        a = backtest.run_backtest(data.synthetic_ticks(5000, 11, "mixed"), CFG)
        b = backtest.run_backtest(data.synthetic_ticks(5000, 11, "mixed"), CFG)
        self.assertEqual(a, b)
        c = backtest.run_backtest(data.synthetic_ticks(5000, 12, "mixed"), CFG)
        self.assertNotEqual(a["trade_list"], c["trade_list"])

    def test_one_position_and_cooldown(self):
        m = backtest.run_backtest(data.synthetic_ticks(8000, 5, "mixed"), CFG)
        tl = m["trade_list"]
        for x, y in zip(tl, tl[1:]):
            self.assertGreaterEqual(y["entry_ts"], x["exit_ts"])
            if x["reason"] == "stop":
                self.assertGreaterEqual(y["entry_ts"] - x["exit_ts"], CFG["risk"]["cooldown_after_stop_minutes"] * 60)

    def test_walkforward_windows(self):
        w = backtest.walkforward(data.synthetic_ticks(6000, 2, "mixed"), CFG, 3)
        self.assertEqual(len(w), 3)

    def test_liquidation_and_margin_loss_bounded(self):
        # обвал на 40% за минуту: убыток ограничен маржой
        ticks = strong_trend_ticks(400)
        t0, p0 = ticks[-1]
        ticks += [(t0 + 60 * (i + 1), p0 * (0.55 if i > 2 else 1.0)) for i in range(50)]
        m = backtest.run_backtest(ticks, zero_cost(CFG))
        for t in m["trade_list"]:
            self.assertGreaterEqual(t["pnl"], -CFG["risk"]["margin_per_trade"] - 1e-9)


if __name__ == "__main__":
    unittest.main()
