import io
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import watch

CFG = dict(watch.DEFAULTS, symbols=["ETH"], min_history_hours=10, lookback_hours=48)
H = 3600


def feed(state, hours):
    """hours: список (час, [цены внутри часа]); возвращает события, возникшие на переходах."""
    out = []
    for h, prices in hours:
        for i, p in enumerate(prices):
            out += state.update(h * H + 60 * i, p)
    return out


class TriggerTest(unittest.TestCase):
    def base(self):
        return [100.0 + (i % 5) for i in range(40)]  # минимум закрытий = 100

    def test_breakdown_short(self):
        s = watch.SymState("ETH", self.base(), CFG)
        ev = feed(s, [(1000, [102, 101]), (1001, [99.0, 98.5, 98.8]), (1002, [98.7])])
        self.assertEqual([e["type"] for e in ev], ["short_breakdown"])
        e = ev[0]
        self.assertEqual(e["side"], "short")
        self.assertGreater(e["suggest_stop"], e["bar_close"])
        self.assertLess(e["suggest_tp"], e["bar_close"])

    def test_reclaim_long(self):
        s = watch.SymState("ETH", self.base(), CFG)
        ev = feed(s, [(1000, [102]), (1001, [101.0, 98.0, 100.6]), (1002, [101])])
        self.assertEqual([e["type"] for e in ev], ["long_reclaim"])
        self.assertLess(ev[0]["suggest_stop"], ev[0]["bar_close"])
        self.assertGreater(ev[0]["suggest_tp"], ev[0]["bar_close"])

    def test_no_trigger_inside_range(self):
        s = watch.SymState("ETH", self.base(), CFG)
        self.assertEqual(feed(s, [(1000, [103, 102]), (1001, [101.5]), (1002, [102])]), [])

    def test_needs_history(self):
        s = watch.SymState("ETH", [100.0] * 3, CFG)
        self.assertEqual(feed(s, [(1000, [101]), (1001, [90]), (1002, [89])]), [])

    def test_wide_stop_noted(self):
        s = watch.SymState("ETH", self.base(), CFG)
        ev = feed(s, [(1000, [102]), (1001, [110.0, 99.0]), (1002, [99])])
        self.assertEqual(ev[0]["type"], "short_breakdown")
        self.assertIn("stop_too_far", ev[0].get("note", ""))


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "runtime"))
        self.out = io.StringIO()

    def write_market(self, ts, price, ok=True):
        with open(os.path.join(self.root, "runtime", "market.json"), "w") as f:
            json.dump({"ts_epoch": ts, "feed_ok": ok, "symbols": {"ETH": {"price": price}}}, f)

    def watcher(self, **kw):
        cfg = dict(CFG, **kw)
        w = watch.Watcher(self.root, cfg, self.out)
        w.states["ETH"] = watch.SymState("ETH", [100.0 + (i % 5) for i in range(40)], cfg)
        return w

    def lines(self):
        return [json.loads(x) for x in self.out.getvalue().splitlines()]

    def test_stale_feed_event_once_then_recovered(self):
        w = self.watcher()
        self.write_market(1000, 101)
        w.step(now=2000)
        w.step(now=2010)
        self.assertEqual([e["type"] for e in self.lines()], ["feed_stale"])
        self.write_market(2020, 101)
        w.step(now=2021)
        self.assertEqual([e["type"] for e in self.lines()], ["feed_stale", "feed_recovered"])

    def test_trigger_emitted_with_cooldown(self):
        w = self.watcher()
        t0 = 1000 * H
        for i, p in enumerate([102, 101]):
            self.write_market(t0 + 60 * i, p)
            w.step(now=t0 + 60 * i + 1)
        for i, p in enumerate([99.0, 98.5]):
            self.write_market(t0 + H + 60 * i, p)
            w.step(now=t0 + H + 60 * i + 1)
        self.write_market(t0 + 2 * H, 98.0)
        w.step(now=t0 + 2 * H + 1)
        self.assertEqual([e["type"] for e in self.lines()], ["short_breakdown"])
        # повторный триггер внутри cooldown молчит
        self.write_market(t0 + 3 * H, 97.0)
        w.step(now=t0 + 3 * H + 1)
        self.write_market(t0 + 4 * H, 96.0)
        w.step(now=t0 + 4 * H + 1)
        self.assertEqual(len(self.lines()), 1)

    def test_blackout_suppresses_stdout_but_logs(self):
        t0 = 1000 * H
        b = [{"from": watch.iso(t0), "to": watch.iso(t0 + 10 * H), "name": "test"}]
        w = self.watcher(blackouts=b)
        for off, p in [(0, 102), (H, 99.0), (2 * H, 98.0)]:
            self.write_market(t0 + off, p)
            w.step(now=t0 + off + 1)
        self.assertEqual(self.lines(), [])
        with open(os.path.join(self.root, "runtime", "watch_events.jsonl")) as f:
            self.assertIn("blackout", f.read())

    def test_feed_not_ok_is_stale(self):
        w = self.watcher()
        self.write_market(5000, 101, ok=False)
        w.step(now=5001)
        self.assertEqual([e["type"] for e in self.lines()], ["feed_stale"])


if __name__ == "__main__":
    unittest.main()
