import io
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import volwatch
from tb.venues import bybit as bb

CFG = dict(volwatch.DEFAULTS, symbols=["SOL"], min_samples=5)


def run(prices, cfg=CFG, start=1000.0, dt=5.0):
    """prices: список цен с шагом dt секунд; возвращает события."""
    w = volwatch.SymWatch("SOL", cfg)
    evs = []
    for i, p in enumerate(prices):
        e = w.update(start + i * dt, p)
        if e:
            evs.append((i, e))
    return evs


def calm(n, base=100.0, amp=0.05):
    return [base + (amp if i % 2 else -amp) for i in range(n)]


class SpikeTest(unittest.TestCase):
    def test_calm_market_no_events(self):
        self.assertEqual(run(calm(1500)), [])

    def test_spike_after_baseline(self):
        px = calm(900) + [100.0 + 1.2 * k / 60.0 for k in range(61)]       # +1.2% за 5 минут
        evs = run(px)
        self.assertEqual(len(evs), 1)
        e = evs[0][1]
        self.assertEqual((e["type"], e["direction"]), ("vol_spike", "up"))
        self.assertTrue(e["scalp_tp_feasible"] or e["range_5m_pct"] < 1.0)
        self.assertGreaterEqual(e["range_5m_pct"], 0.6)
        self.assertGreater(e["ratio"], 2.5)

    def test_floor_blocks_small_moves(self):
        px = calm(900) + [100.0 + 0.4 * k / 60.0 for k in range(61)]       # +0.4% меньше порога 0.6%
        self.assertEqual(run(px), [])

    def test_ratio_blocks_when_market_always_wild(self):
        wild = [100.0 + (0.5 if (i // 6) % 2 else -0.5) for i in range(900)]  # диапазон ~1% всегда
        evs = run(wild)
        # пока база не накоплена (первые минуты), срабатывает только порог; когда база есть, постоянная «дикость» событий не даёт
        self.assertTrue(all(i < 400 for i, _ in evs), evs)
        self.assertLessEqual(len(evs), 1)

    def test_cooldown(self):
        px = calm(900) + [100.0 + 1.5 * k / 60.0 for k in range(61)] + [101.5 - 1.5 * k / 60.0 for k in range(61)]
        self.assertEqual(len(run(px)), 1)

    def test_down_direction(self):
        px = calm(900) + [100.0 - 1.0 * k / 60.0 for k in range(61)]
        self.assertEqual(run(px)[0][1]["direction"], "down")


class WatcherTest(unittest.TestCase):
    def test_stale_and_recovered(self):
        out = io.StringIO()
        d = tempfile.mkdtemp()
        w = volwatch.VolWatcher(dict(volwatch.DEFAULTS, symbols=["SOL"]), out, os.path.join(d, "ev.jsonl"))
        w.step({"feed_ok": True, "ts_epoch": 1000, "symbols": {"SOL": {"price": 100}}}, 5000)
        w.step(None, 5001)
        w.step({"feed_ok": True, "ts_epoch": 5010, "symbols": {"SOL": {"price": 100}}}, 5011)
        types = [json.loads(x)["type"] for x in out.getvalue().splitlines()]
        self.assertEqual(types, ["feed_stale", "feed_recovered"])
        self.assertTrue(os.path.exists(os.path.join(d, "ev.jsonl")))


class FeesTest(unittest.TestCase):
    def test_fee_rate_parsed_and_allowed(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url))
            if url.endswith("/v5/market/time"):
                return {"retCode": 0, "result": {"timeSecond": "1700000000"}}
            return {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.0002"}]}}
        c = bb.Client("mainnet", "K", "S", transport=transport, clock=lambda: 1700000000.0)
        r = bb.fee_rate(c, "SOL")
        self.assertEqual((r["taker_pct"], r["maker_pct"]), (0.055, 0.02))
        self.assertTrue(any("/v5/account/fee-rate" in u and m == "GET" for m, u in calls))
        with self.assertRaises(bb.BybitError):
            c.call("POST", "/v5/account/fee-rate", {})            # только чтение


if __name__ == "__main__":
    unittest.main()
