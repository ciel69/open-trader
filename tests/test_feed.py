import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tests"))
import _bootstrap  # noqa: F401
from tb.marketdata import feed as fd  # noqa: E402
from test_wsclient import MockServer  # noqa: E402


def cfg():
    with open(os.path.join(HERE, "config", "feed_config.json")) as f:
        c = json.load(f)
    # тесты построены на двух символах; реальный конфиг может содержать больше (SOL, HYPE, ZEC для paper-теста)
    c["symbols"] = {k: v for k, v in c["symbols"].items() if k in ("ETH", "BTC")}
    return c


def okx_ticker(px, inst="ETH-USDT-SWAP"):
    return json.dumps({"arg": {"channel": "tickers", "instId": inst},
                       "data": [{"instId": inst, "last": str(px), "bidPx": str(px - 0.05), "askPx": str(px + 0.05)}]})


def bybit_ticker(px):
    return json.dumps({"topic": "tickers.ETHUSDT", "type": "delta", "data": {"symbol": "ETHUSDT", "lastPrice": str(px)}})


class ParserTests(unittest.TestCase):
    def test_selftest_fixtures(self):
        self.assertTrue(fd.run_selftest(out=lambda s: None))

    def test_okx_units(self):
        ev = fd.parse_okx(fd.FIXTURES["okx_book"][1])[0]
        self.assertEqual(ev["bids"][0], (2307.9, 30.0))
        self.assertEqual(ev["asks"][0], (2308.0, 10.0))

    def test_bybit_ticker_delta_partial(self):
        evs = fd.parse_bybit(bybit_ticker(100.5))
        self.assertEqual([e["kind"] for e in evs], ["ticker"])

    def test_garbage_counted_not_fatal(self):
        f = fd.Feed(cfg(), now=1000.0)
        for raw in ["", "{", "[]", "null", '{"arg":{"channel":"trades"},"data":[{"px":"x"}]}',
                    json.dumps({"arg": {"channel": "tickers"}, "data": [{"instId": "DOGE-USDT-SWAP", "last": "1"}]}),
                    "\x00\xff", okx_ticker(float("nan"))]:
            f.on_raw("okx", raw, 1001.0)
        self.assertEqual(f.src_stats["okx"]["parse_errors"], 8)
        f.on_raw("okx", okx_ticker(2300.0), 1002.0)
        self.assertEqual(f.live_price("ETH", "okx", 1002.5), 2300.0)


class FeedLogicTests(unittest.TestCase):
    def test_stale_source_dropped_and_feed_ok(self):
        f = fd.Feed(cfg(), now=1000.0)
        f.on_raw("okx", okx_ticker(2300.0), 1001.0)
        f.on_raw("bybit", bybit_ticker(2301.0), 1001.0)
        f.on_raw("okx", okx_ticker(60000.0, "BTC-USDT-SWAP"), 1001.0)
        m = f.market(1002.0)
        self.assertEqual(m["symbols"]["ETH"]["prices_by_src"], {"okx": 2300.0, "bybit": 2301.0})
        self.assertEqual(m["symbols"]["ETH"]["src"], "okx")
        self.assertTrue(m["feed_ok"])
        # okx молчит дольше stale_seconds (10), bybit обновляется
        f.on_raw("bybit", bybit_ticker(2302.0), 1020.0)
        m = f.market(1021.0)
        self.assertEqual(m["symbols"]["ETH"]["prices_by_src"], {"bybit": 2302.0})
        self.assertEqual(m["symbols"]["ETH"]["src"], "bybit")
        self.assertFalse(m["feed_ok"])  # у BTC нет живых источников
        m = f.market(1100.0)
        self.assertFalse(m["feed_ok"])
        self.assertEqual(m["symbols"]["ETH"]["regime"], "unknown")

    def test_all_silent_feed_ok_false_and_stale_recovered(self):
        c = cfg()
        c["event_cooldown_s"] = 0
        f = fd.Feed(c, now=1000.0)
        f.on_raw("okx", okx_ticker(2300.0), 1001.0)
        f.detect(1002.0, f.market(1002.0))
        self.assertEqual(f.pending, [])
        m = f.market(1100.0)
        self.assertFalse(m["feed_ok"])
        f.detect(1100.0, m)
        types = sorted((e["type"], e["data"]["src"]) for e in f.pending)
        self.assertEqual(types, [("feed_stale", "bybit"), ("feed_stale", "okx")])
        f.pending = []
        f.on_raw("okx", okx_ticker(2300.0), 1101.0)
        f.detect(1101.5, f.market(1101.5))
        self.assertEqual([(e["type"], e["data"]["src"]) for e in f.pending], [("feed_recovered", "okx")])

    def test_event_cooldown(self):
        f = fd.Feed(cfg(), now=0.0)
        self.assertIsNotNone(f._emit("imbalance_spike", "ETH", {}, 100.0))
        self.assertIsNone(f._emit("imbalance_spike", "ETH", {}, 200.0))
        self.assertIsNotNone(f._emit("imbalance_spike", "BTC", {}, 200.0))   # другой символ
        self.assertIsNotNone(f._emit("vol_spike", "ETH", {}, 200.0))          # другой тип
        self.assertIsNotNone(f._emit("imbalance_spike", "ETH", {}, 401.0))    # после cooldown

    def test_imbalance_event_and_divergence(self):
        f = fd.Feed(cfg(), now=1000.0)
        f.on_raw("okx", fd.FIXTURES["okx_book"][1].replace("30", "300"), 1001.0)
        f.on_raw("okx", okx_ticker(2300.0), 1001.0)
        f.on_raw("bybit", bybit_ticker(2310.0), 1001.0)
        m = f.market(1002.0)
        self.assertGreater(m["symbols"]["ETH"]["book"]["imbalance"], 0.6)
        f.detect(1002.0, m)
        types = {e["type"] for e in f.pending}
        self.assertIn("imbalance_spike", types)
        self.assertIn("src_divergence", types)

    def test_cvd_and_contract_size(self):
        f = fd.Feed(cfg(), now=1000.0)
        f.on_raw("okx", fd.FIXTURES["okx_trade"][1], 1001.0)  # buy sz=5 контрактов * 0.1
        m = f.market(1002.0)
        self.assertAlmostEqual(m["symbols"]["ETH"]["cvd_1m"], 0.5)
        f.on_raw("okx", fd.FIXTURES["okx_funding"][1], 1001.0)
        self.assertAlmostEqual(f.market(1002.0)["symbols"]["ETH"]["funding_hourly"], 0.0001 / 8)

    def test_regime_change_event(self):
        c = cfg()
        c["event_cooldown_s"] = 0
        f = fd.Feed(c, now=0.0)
        t = 0.0
        px = 100.0
        # 40 минут вверх -> trend_up (ret_15m>0, ema_fast>ema_slow)
        for i in range(2400):
            t += 1.0
            px *= 1.00005
            f.on_raw("okx", okx_ticker(round(px, 4)), t)
            f.on_raw("okx", okx_ticker(60000.0, "BTC-USDT-SWAP"), t)
            f.sample(t)
        m = f.market(t)
        self.assertEqual(m["symbols"]["ETH"]["regime"], "trend_up")
        f.detect(t, m)
        # шторм: сильные колебания
        for i in range(300):
            t += 1.0
            px *= 1.01 if i % 2 else 0.99
            f.on_raw("okx", okx_ticker(round(px, 4)), t)
            f.on_raw("okx", okx_ticker(60000.0, "BTC-USDT-SWAP"), t)
            f.sample(t)
        m = f.market(t)
        self.assertEqual(m["symbols"]["ETH"]["regime"], "storm")
        f.pending = []
        f.detect(t, m)
        self.assertIn(("regime_change", "ETH"), [(e["type"], e["symbol"]) for e in f.pending])
        self.assertTrue(all("\n" not in json.dumps(e) for e in f.pending))


class IOTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_atomic_write_no_tmp_left(self):
        p = os.path.join(self.d, "runtime", "market.json")
        fd.atomic_write_json(p, {"a": 1})
        fd.atomic_write_json(p, {"a": 2})
        with open(p) as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(os.path.dirname(p)), ["market.json"])

    def test_write_cycle_files(self):
        f = fd.Feed(cfg(), now=1000.0)
        f.on_raw("okx", okx_ticker(2300.0), 1001.0)
        fd.write_cycle(f, os.path.join(self.d, "rt"), 1002.0)
        for n in ("market.json", "health.json"):
            with open(os.path.join(self.d, "rt", n)) as fh:
                json.load(fh)
        h = json.load(open(os.path.join(self.d, "rt", "health.json")))
        self.assertEqual(h["sources"]["okx"]["messages"], 1)

    def test_events_jsonl_lines(self):
        p = os.path.join(self.d, "e.jsonl")
        fd.append_events(p, [{"type": "a", "data": {"x": 1}}, {"type": "b"}])
        fd.append_events(p, [{"type": "c"}])
        lines = open(p).read().splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines], ["a", "b", "c"])


class E2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_against_mock_server(self):
        d = tempfile.mkdtemp()
        try:
            async def h(c):
                sub = await c.recv_text()
                assert "ETH-USDT-SWAP" in sub
                await c.send(okx_ticker(2300.0))
                await c.send(okx_ticker(60000.0, "BTC-USDT-SWAP"))
                await c.send("garbage{")
                await asyncio.sleep(3)
            srv = await MockServer(h).start()
            c = cfg()
            c["sources"]["okx"].update({"url": srv.url, "allow_plain": True})
            c["sources"]["bybit"]["enabled"] = False
            c["once_seconds"] = 1.0
            c["write_interval_s"] = 0.2
            feed = await asyncio.wait_for(fd.run(c, d, once=True), 10)
            await srv.stop()
            m = json.load(open(os.path.join(d, "runtime", "market.json")))
            self.assertTrue(m["feed_ok"])
            self.assertEqual(m["symbols"]["ETH"]["price"], 2300.0)
            h_ = json.load(open(os.path.join(d, "runtime", "health.json")))
            self.assertFalse(h_["running"])
            self.assertEqual(h_["sources"]["okx"]["parse_errors"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
