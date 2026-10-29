"""Поток ликвидаций: разбор сообщений Bybit (формат проверен живым подключением), окна, каскады, карта цен, runtime/liq.json, событие в hot_events.jsonl, цикл на фейковом сокете."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.marketdata import hotscan, liqwatch
from tb.platform import scansvc

NOW = 1793270000.0


def msg(sym, side, v, p, t=NOW):
    return {"topic": "allLiquidation.%sUSDT" % sym, "type": "snapshot", "ts": int(t * 1000), "data": [{"T": int(t * 1000), "s": sym + "USDT", "S": side, "v": str(v), "p": str(p)}]}


class Book(unittest.TestCase):
    def test_side_mapping_buy_is_long_liquidated(self):
        b = liqwatch.LiqBook()
        self.assertEqual(b.handle(msg("HYPE", "Buy", 10, 90.0), NOW), 1)
        self.assertEqual(b.handle(msg("HYPE", "Sell", 5, 90.0), NOW), 1)
        w = b.window("HYPE", NOW, 300)
        self.assertAlmostEqual(w["long"], 900.0)
        self.assertAlmostEqual(w["short"], 450.0)
        self.assertEqual(w["n"], 2)

    def test_garbage_is_ignored(self):
        b = liqwatch.LiqBook()
        for m in ({"topic": "tickers.BTCUSDT", "data": {}}, {"topic": "allLiquidation.X", "data": "no"}, msg("A", "Hold", 1, 1), msg("A", "Buy", 0, 5), msg("A", "Buy", "x", 5)):
            self.assertEqual(b.handle(m, NOW), 0)

    def test_window_excludes_old_and_prune_drops_day_old(self):
        b = liqwatch.LiqBook({"keep_s": 3600})
        b.handle(msg("SOL", "Buy", 100, 100.0, t=NOW - 7200), NOW)
        b.handle(msg("SOL", "Buy", 100, 100.0, t=NOW - 100), NOW)
        self.assertAlmostEqual(b.window("SOL", NOW, 300)["long"], 10000.0)
        b.prune(NOW)
        self.assertEqual(len(b.ev["SOL"]), 1)

    def test_cascade_threshold_cooldown_and_loud_cap(self):
        b = liqwatch.LiqBook({"min_usd": 100000, "max_loud_per_hour": 1, "cooldown_s": 900})
        b.handle(msg("SOL", "Buy", 1500, 100.0), NOW)                   # 150k лонгов: каскад, но не вдвое выше порога -> тихий
        b.handle(msg("ETH", "Sell", 100, 2500.0), NOW)                  # 250k шортов: громкий
        b.handle(msg("XRP", "Buy", 100, 1.0), NOW)                      # мелочь
        ev = b.cascades(NOW)
        self.assertEqual([(e["sym"], e["liq_side"], e["direction"], e["wake"]) for e in ev], [("ETH", "short", "up", True), ("SOL", "long", "down", False)])
        self.assertEqual(b.cascades(NOW + 60), [])                      # cooldown
        self.assertEqual(ev[0]["type"], "liq_cascade")
        self.assertEqual(ev[0]["liq_usd"], 250000)

    def test_map_buckets_prices(self):
        b = liqwatch.LiqBook({"map_step_pct": 0.5})
        for px, v in ((100.0, 10), (100.1, 10), (101.0, 5), (99.0, 1)):
            b.handle(msg("SOL", "Buy", v, px), NOW)
        rows = b.levels("SOL", NOW)
        self.assertEqual(sum(r["long"] for r in rows), round(100 * 10 + 100.1 * 10 + 101 * 5 + 99 * 1))
        self.assertGreaterEqual(rows[0]["long"], rows[-1]["long"])      # по убыванию объёма корзины

    def test_snapshot_shape(self):
        b = liqwatch.LiqBook()
        b.handle(msg("SOL", "Buy", 100, 100.0), NOW)
        snap = b.snapshot(NOW)
        r = snap["syms"][0]
        self.assertEqual((r["sym"], r["long_5m"], r["short_5m"], r["n_1h"]), ("SOL", 10000, 0, 1))
        self.assertIn("map", r)
        self.assertEqual(snap["totals_1h"], {"long": 10000, "short": 0})


class RunnerFiles(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbliq_")
        os.makedirs(os.path.join(self.root, "runtime"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_read_symbols_from_heat(self):
        with open(paths.runtime("heat.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"rows": [{"sym": "BTC"}, {"sym": "ETH"}, {"sym": "SOL"}, {"bad": 1}]}, f)
        self.assertEqual(liqwatch.read_symbols(self.root, 2), ["BTC", "ETH"])
        self.assertEqual(liqwatch.read_symbols(os.path.join(self.root, "none"), 5), [])

    def test_publish_writes_liq_json_and_event_that_reaches_bus_converter(self):
        clock = {"t": NOW}
        r = liqwatch.Runner(self.root, {"min_usd": 100000}, clock=lambda: clock["t"])
        r.book.handle(msg("ETH", "Sell", 100, 2500.0), NOW)
        r.publish(NOW)
        with open(paths.runtime("liq.json", root=self.root), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["syms"][0]["sym"], "ETH")
        with open(paths.runtime("hot_events.jsonl", root=self.root), encoding="utf-8") as f:
            ev = json.loads(f.readline())
        self.assertEqual((ev["type"], ev["sym"], ev["liq_side"], ev["env"]), ("liq_cascade", "ETH", "short", "mainnet"))
        pub = scansvc.Publisher(self.root, {}, os.path.join(self.root, "runtime", "hot_events.jsonl"), emit=lambda *a: None)
        d, sym = pub.convert(ev)
        self.assertEqual((d["kind"], d["liq_usd"], d["liq_side"], d["dir"], sym), ("liq_cascade", 250000, "short", "up", "ETH"))

    def test_tail_feeds_outcome_tracker_with_liq_events_only(self):
        path = os.path.join(self.root, "runtime", "hot_events.jsonl")
        tail = hotscan.LiqTail(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "range_5m", "sym": "A", "price": 1.0}) + "\n")
            f.write(json.dumps({"type": "liq_cascade", "sym": "ETH", "price": 2500.0, "direction": "up"}) + "\n")
            f.write('{"type": "liq_casc')                                      # недописанная строка ждёт
        got = tail.poll()
        self.assertEqual([(e["type"], e["sym"]) for e in got], [("liq_cascade", "ETH")])
        self.assertEqual(tail.poll(), [])


class FakeClient(object):
    """Подмена WSClient: отдаёт заранее заданные сообщения и запоминает отправленное."""

    def __init__(self, url, msgs):
        self.url, self.msgs, self.sent, self.connected, self.subscriptions, self.closed = url, msgs, [], True, [], False

    async def messages(self):
        for m in self.msgs:
            yield json.dumps(m)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class LoopOnFake(RunnerFiles):
    def test_main_subscribes_from_heat_and_counts_messages(self):
        with open(paths.runtime("heat.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"rows": [{"sym": "SOL"}, {"sym": "ETH"}]}, f)
        holder = {}

        def factory(url):
            holder["c"] = FakeClient(url, [msg("SOL", "Buy", 100, 100.0), msg("SOL", "Sell", 50, 100.0)])
            return holder["c"]
        stop = {"v": False}
        r = liqwatch.Runner(self.root, {"publish_s": 0.1}, client_factory=factory, stop=lambda: stop["v"])

        async def go():
            t = asyncio.ensure_future(r.main())
            await asyncio.sleep(1.4)
            stop["v"] = True
            await asyncio.wait_for(t, 5)
        asyncio.run(go())
        c = holder["c"]
        subs = [a for m in c.sent if m.get("op") == "subscribe" for a in m["args"]]
        self.assertEqual(sorted(subs), ["allLiquidation.ETHUSDT", "allLiquidation.SOLUSDT"])
        self.assertEqual(r.book.n, 2)
        self.assertTrue(os.path.exists(paths.runtime("liq.json", root=self.root)))
        self.assertTrue(c.closed)


if __name__ == "__main__":
    unittest.main()
