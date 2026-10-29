"""BybitFeed (venues/bybit_wsx.py, WP1A): слияние delta тикеров, kline confirm/дубли/дыры, приватные события со ссылками, дубликаты execId,
монитор состояния, динамические подписки; интеграция на локальном WS-сервере (auth, subscribe, обрыв и возврат). Без сети."""
import asyncio
import json
import unittest

import _bootstrap  # noqa: F401
from test_wsclient import MockServer
from tb.executor.ports import MarketFeed
from tb.venues import bybit_stream as bs
from tb.venues import bybit_wsx as W


class FakeClient(object):
    env, key, secret, offset_ms = "testnet", "KEYX", "SECRETX", 0

    def sync_time(self):
        pass


def feed(**kw):
    f = W.BybitFeed(FakeClient(), **kw)
    f.log = []
    f.on_ticker = lambda *a: f.log.append(("t",) + a)
    f.on_kline = lambda *a: f.log.append(("k",) + a)
    f.on_private = lambda ev: f.log.append(("p", ev))
    f.on_state = lambda *a: f.log.append(("s",) + a)
    return f


def tick(typ, **d):
    d.setdefault("symbol", "SOLUSDT")
    return {"topic": "tickers.SOLUSDT", "type": typ, "ts": 1700000000000, "data": d}


def kl(start, confirm, tf="5", close="100.5", sym="SOLUSDT"):
    return {"topic": "kline.%s.%s" % (tf, sym), "type": "snapshot", "data": [{"start": start, "end": start + int(tf) * 60000 - 1, "interval": tf, "open": "100",
            "close": close, "high": "101", "low": "99", "volume": "12", "turnover": "1200", "confirm": confirm, "timestamp": start + 5}]}


class TickerTests(unittest.TestCase):
    def test_is_market_feed(self):
        self.assertIsInstance(feed(), MarketFeed)

    def test_snapshot_then_deltas_merged(self):
        f = feed()
        f.handle_public(tick("snapshot", lastPrice="100", bid1Price="99.9", ask1Price="100.1", markPrice="100.02", fundingRate="0.0001"), now=5.0)
        f.handle_public(tick("delta", bid1Price="99.95"), now=6.0)                 # в delta нет lastPrice/ask/mark
        f.handle_public(tick("delta", lastPrice="100.3", markPrice="100.2"), now=7.0)
        self.assertEqual(f.log[0], ("t", "SOL", 100.0, 99.9, 100.1, 100.02, 0.0001, 5.0))
        self.assertEqual(f.log[1], ("t", "SOL", 100.0, 99.95, 100.1, 100.02, 0.0001, 6.0))
        self.assertEqual(f.log[2], ("t", "SOL", 100.3, 99.95, 100.1, 100.2, 0.0001, 7.0))

    def test_empty_and_garbage_fields_do_not_overwrite(self):
        f = feed()
        f.handle_public(tick("snapshot", lastPrice="100", bid1Price="99.9", ask1Price="100.1", markPrice="100", fundingRate="0.0002"))
        f.handle_public(tick("delta", lastPrice="", bid1Price="abc", ask1Price="0", markPrice=None, fundingRate=""))
        last = f.log[-1]
        self.assertEqual(last[2:7], (100.0, 99.9, 100.1, 100.0, 0.0002))

    def test_repeat_snapshot_replaces_state(self):
        f = feed()
        f.handle_public(tick("snapshot", lastPrice="100", bid1Price="99.9", ask1Price="100.1", markPrice="100"))
        f.handle_public(tick("snapshot", lastPrice="101", markPrice="101"))           # свежий snapshot без bid/ask: старые не тянем
        self.assertEqual(f.log[-1][2:6], (101.0, 0.0, 0.0, 101.0))

    def test_no_event_until_last_known(self):
        f = feed()
        f.handle_public(tick("delta", bid1Price="99.9"))
        self.assertEqual(f.log, [])
        f.handle_public(tick("delta", lastPrice="100"))
        self.assertEqual(len(f.log), 1)

    def test_symbol_without_quote_suffix_and_unknown_topic(self):
        f = feed()
        f.handle_public({"topic": "tickers.1000PEPEUSDT", "type": "snapshot", "data": {"symbol": "1000PEPEUSDT", "lastPrice": "0.01"}})
        self.assertEqual(f.log[0][1], "1000PEPE")
        self.assertEqual(f.handle_public({"topic": "orderbook.1.SOLUSDT", "data": {}}), 0)
        self.assertEqual(f.handle_public("junk"), 0)

    def test_handler_exception_does_not_kill_feed(self):
        f = feed()

        def bad(*a):
            raise RuntimeError("boom")
        f.on_ticker = bad
        f.handle_public(tick("snapshot", lastPrice="100"))
        self.assertEqual(f.handler_errors, 1)
        self.assertIn("boom", f.last_handler_error)


class KlineTests(unittest.TestCase):
    def test_fields_confirm_and_forming(self):
        f = feed()
        f.handle_public(kl(1700000000000, False))
        f.handle_public(kl(1700000000000, False, close="100.8"))
        f.handle_public(kl(1700000000000, True, close="100.9"))
        ks = [x for x in f.log if x[0] == "k"]
        self.assertEqual(ks[0], ("k", "SOL", 5, 1700000000000, 100.0, 101.0, 99.0, 100.5, 12.0, 1200.0, False))
        self.assertEqual([k[-1] for k in ks], [False, False, True])
        self.assertEqual(ks[2][7], 100.9)

    def test_duplicate_confirmed_dropped(self):
        f = feed()
        f.handle_public(kl(1700000000000, True))
        f.handle_public(kl(1700000000000, True))
        f.handle_public(kl(1699999700000, True))                 # старая подтверждённая
        self.assertEqual(len([x for x in f.log if x[0] == "k"]), 1)

    def test_gap_reported_once_with_range(self):
        f = feed()
        gaps = []
        f.on_gap = lambda *a: gaps.append(a)
        f.handle_public(kl(1700000000000, True))
        f.handle_public(kl(1700000300000, True))                 # подряд: дыры нет
        self.assertEqual(gaps, [])
        f.handle_public(kl(1700000300000 + 3 * 300000, True))    # пропущены 2 свечи
        self.assertEqual(gaps, [("SOL", 5, 1700000600000, 1700000900000)])

    def test_tf_60_and_independent_per_tf(self):
        f = feed()
        f.handle_public(kl(1700000000000, True, tf="60"))
        f.handle_public(kl(1700000000000, True, tf="5"))
        self.assertEqual([(x[2]) for x in f.log if x[0] == "k"], [60, 5])

    def test_garbage_item_skipped(self):
        f = feed()
        f.handle_public({"topic": "kline.5.SOLUSDT", "data": [{"start": "x"}, {"start": 1, "open": "1", "close": "1", "high": "1", "low": "1", "confirm": True}]})
        self.assertEqual(len([x for x in f.log if x[0] == "k"]), 1)


class SubscribeTests(unittest.TestCase):
    def test_topics_and_unsubscribe(self):
        f = feed()
        f.subscribe(["SOL", "ethusdt"], [1, 5])
        self.assertEqual(f.topics(), ["tickers.ETHUSDT", "kline.1.ETHUSDT", "kline.5.ETHUSDT", "tickers.SOLUSDT", "kline.1.SOLUSDT", "kline.5.SOLUSDT"])
        f.subscribe(["SOL"], [15])
        self.assertIn("kline.15.SOLUSDT", f.topics())
        f.unsubscribe(["SOL"])
        self.assertNotIn("tickers.SOLUSDT", f.topics())

    def test_bad_timeframe_rejected(self):
        with self.assertRaises(ValueError):
            feed().subscribe(["SOL"], [3])


EXEC = {"category": "linear", "symbol": "SOLUSDT", "side": "Sell", "execPrice": "94.9", "execQty": "1", "execFee": "0.05", "execId": "e1", "orderId": "o1",
        "orderLinkId": "tbx-12-3", "isMaker": False, "stopOrderType": "StopLoss", "closedSize": "1", "execPnl": "-5.1", "execType": "Trade", "leavesQty": "0",
        "execTime": "1700000001234"}


class PrivateTests(unittest.TestCase):
    def test_execution_normalised_with_link_stop_maker(self):
        f = feed()
        f.handle_private({"topic": "execution", "data": [EXEC]})
        ev = f.log[0][1]
        self.assertEqual((ev["kind"], ev["exec_id"], ev["sym"], ev["side"], ev["price"], ev["qty"], ev["fee"], ev["is_maker"], ev["link_id"], ev["order_id"],
                          ev["stop_type"], ev["exec_type"], ev["closed_size"], ev["pnl"], ev["leaves"], ev["ts_ms"]),
                         ("execution", "e1", "SOL", "Sell", 94.9, 1.0, 0.05, False, "tbx-12-3", "o1", "StopLoss", "Trade", 1.0, -5.1, 0.0, 1700000001234))
        f.handle_private({"topic": "execution", "data": [dict(EXEC, execId="e2", isMaker=True, stopOrderType="UNKNOWN", orderLinkId="")]})
        ev = f.log[1][1]
        self.assertEqual((ev["is_maker"], ev["stop_type"], ev["link_id"]), (True, "", ""))

    def test_duplicate_exec_id_dropped_and_preload(self):
        f = feed()
        f.handle_private({"topic": "execution", "data": [EXEC, EXEC]})
        f.handle_private({"topic": "execution", "data": [EXEC]})
        self.assertEqual(len(f.log), 1)
        f2 = feed()
        f2.preload_exec_ids(["e1"], 1700000001234)
        f2.handle_private({"topic": "execution", "data": [EXEC]})
        self.assertEqual(f2.log, [])
        self.assertEqual(f2.last_exec_ts_ms, 1700000001234)

    def test_seen_window_bounded(self):
        f = feed()
        f.preload_exec_ids(["x%d" % i for i in range(W.SEEN_MAX + 10)])
        self.assertEqual(len(f.seen_set), W.SEEN_MAX)
        self.assertNotIn("x0", f.seen_set)

    def test_funding_and_spot_skipped(self):
        f = feed()
        f.handle_private({"topic": "execution", "data": [dict(EXEC, execId="f1", execType="Funding"), dict(EXEC, execId="s1", category="spot")]})
        self.assertEqual(f.log, [])

    def test_order_event(self):
        f = feed()
        f.handle_private({"topic": "order", "data": [{"category": "linear", "symbol": "SOLUSDT", "orderId": "o9", "orderLinkId": "tbx-12-4", "side": "Sell",
                                                      "orderType": "Limit", "price": "110", "qty": "1", "cumExecQty": "0.4", "leavesQty": "0.6",
                                                      "orderStatus": "PartiallyFilled", "reduceOnly": True, "stopOrderType": "", "createType": "CreateByUser",
                                                      "cancelType": "UNKNOWN", "rejectReason": "EC_NoError", "avgPrice": "110", "updatedTime": "1700000002000"}]})
        ev = f.log[0][1]
        self.assertEqual((ev["kind"], ev["sym"], ev["link_id"], ev["type"], ev["price"], ev["filled"], ev["leaves"], ev["reduce_only"], ev["status"], ev["side"]),
                         ("order", "SOL", "tbx-12-4", "limit", 110.0, 0.4, 0.6, True, "PartiallyFilled", "Sell"))

    def test_position_and_wallet(self):
        f = feed()
        f.handle_private({"topic": "position", "data": [{"category": "linear", "symbol": "SOLUSDT", "side": "Buy", "size": "2", "entryPrice": "100", "markPrice": "101",
                                                         "stopLoss": "95", "takeProfit": "0", "liqPrice": "50", "unrealisedPnl": "2", "leverage": "5", "positionIM": "40"}]}, now=9.0)
        f.handle_private({"topic": "position", "data": [{"category": "linear", "symbol": "SOLUSDT", "side": "", "size": "0", "stopLoss": "0"}]})
        f.handle_private({"topic": "wallet", "data": [{"accountType": "UNIFIED", "totalEquity": "41.2", "totalAvailableBalance": "30"}]}, now=10.0)
        p, p0, w = (x[1] for x in f.log)
        self.assertEqual((p["kind"], p["sym"], p["side"], p["size"], p["avg"], p["sl"], p["tp"], p["lev"], p["margin"], p["link_id"]),
                         ("position", "SOL", "long", 2.0, 100.0, 95.0, 0.0, 5.0, 40.0, ""))
        self.assertEqual((p0["size"], p0["side"]), (0.0, ""))
        self.assertEqual((w["kind"], w["equity"], w["free"]), ("wallet", 41.2, 30.0))


class StateTests(unittest.TestCase):
    class Cl(object):
        def __init__(self):
            self.connected, self.connects, self.reconnects = False, 0, 0

    def test_state_transitions(self):
        f = feed()
        pub, prv = self.Cl(), self.Cl()
        f._clients = {"public": pub, "private": prv}
        f._tick_state(0.0)
        self.assertEqual([x for x in f.log if x[0] == "s"], [("s", "public", False, 0.0), ("s", "private", False, 0.0)])
        pub.connected, pub.connects = True, 1
        prv.connected, prv.connects = True, 1
        f.log.clear()
        f._tick_state(3.0)                                   # private подключён, но auth/subscribe ещё нет
        self.assertEqual(f.log, [("s", "public", True, 3.0), ("s", "private", False, 3.0)])
        f.priv_ready = True
        f.log.clear()
        f._tick_state(4.0)
        self.assertEqual(f.log, [("s", "private", True, 4.0)])
        pub.connected = False
        f.log.clear()
        f._tick_state(10.0)
        f._tick_state(11.0)
        self.assertEqual(f.log, [("s", "public", False, 0.0), ("s", "public", False, 1.0)])
        pub.connected, pub.connects = True, 2
        f.log.clear()
        f._tick_state(15.0)
        self.assertEqual(f.log, [("s", "public", True, 5.0)])
        st = f.state(16.0)
        self.assertTrue(st["public"]["up"] and st["private"]["up"])

    def test_fast_reconnect_between_ticks_reported(self):
        f = feed()
        pub = self.Cl()
        pub.connected, pub.connects = True, 1
        f._clients = {"public": pub}
        f.want_private = False
        f._tick_state(0.0)
        f._tick_state(1.0)
        f.log.clear()
        pub.connects = 2                                    # переподключился целиком между тиками
        f._tick_state(2.0)
        self.assertEqual(f.log, [("s", "public", False, 0.0), ("s", "public", True, 0.0)])

    def test_state_without_private(self):
        f = feed(private=False)
        self.assertIsNone(f.state()["private"])


class SecretHygieneTests(unittest.TestCase):
    def test_module_does_not_print(self):
        import tb.venues.bybit_wsx as m
        with open(m.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("print(", src)
        self.assertNotIn("logging", src)


# ------------------------------------------------------------------ интеграция на локальном WS-сервере
async def sink_handler(c):
    """Публичный сокет-заглушка: читает подписки, отвечает success."""
    while True:
        await c.recv_text()
        await c.send(json.dumps({"success": True, "op": "subscribe"}))


OPTS = {"allow_plain": True, "backoff_base": 0.05, "backoff_max": 0.1, "ping_every": 50, "state_step": 0.05}


class WsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_subscribe_ticker_kline_and_dynamic_subscribe(self):
        got_subs = []

        async def h(c):
            while True:
                m = json.loads(await c.recv_text())
                got_subs.append(m)
                await c.send(json.dumps({"success": True, "op": "subscribe", "ret_msg": "subscribe"}))
                if len(got_subs) == 1:
                    await c.send(json.dumps(tick("snapshot", lastPrice="100", bid1Price="99.9", ask1Price="100.1", markPrice="100", fundingRate="0.0001")))
                    await c.send(json.dumps(tick("delta", bid1Price="99.95")))
                    await c.send(json.dumps(kl(1700000000000, True)))
        srv = await MockServer(h).start()
        f = feed(private=False, urls={"public": srv.url, "private": srv.url}, opts=OPTS)
        f.subscribe(["SOL"], [1])
        task = asyncio.ensure_future(f.run())
        for _ in range(100):
            if len(f.log) >= 3:
                break
            await asyncio.sleep(0.02)
        f.subscribe(["ETH"], [5])                           # на лету
        for _ in range(100):
            if len(got_subs) >= 2 and any(x[0] == "s" and x[2] is True for x in f.log):
                break
            await asyncio.sleep(0.02)
        await f.stop()
        await asyncio.wait_for(task, 2)
        await srv.stop()
        self.assertEqual(got_subs[0], {"op": "subscribe", "args": ["tickers.SOLUSDT", "kline.1.SOLUSDT"]})
        self.assertEqual(got_subs[1], {"op": "subscribe", "args": ["tickers.ETHUSDT", "kline.5.ETHUSDT"]})
        kinds = [x[0] for x in f.log if x[0] in ("t", "k")]
        self.assertEqual(kinds, ["t", "t", "k"])
        self.assertTrue(any(x[0] == "s" and x[1] == "public" and x[2] is True for x in f.log))

    async def test_private_auth_signature_subscribe_events_reconnect(self):
        auths, subs = [], []

        async def h(c):
            auth = json.loads(await c.recv_text())
            auths.append(auth)
            await c.send(json.dumps({"success": True, "ret_msg": "", "op": "auth"}))
            subs.append(json.loads(await c.recv_text()))
            await c.send(json.dumps({"success": True, "op": "subscribe"}))
            if c.index == 0:
                await c.send(json.dumps({"topic": "execution", "data": [EXEC]}))
                await asyncio.sleep(0.15)
                c.drop()
            else:
                await c.send(json.dumps({"topic": "execution", "data": [EXEC, dict(EXEC, execId="e2")]}))   # e1 уже был: дубль
                await asyncio.sleep(1)
        srv = await MockServer(h).start()
        sink = await MockServer(sink_handler).start()
        f = feed(urls={"public": sink.url, "private": srv.url}, opts=OPTS)
        task = asyncio.ensure_future(f.run())
        for _ in range(150):
            if len([x for x in f.log if x[0] == "p"]) >= 2:
                break
            await asyncio.sleep(0.02)
        await f.stop()
        await asyncio.wait_for(task, 2)
        await srv.stop()
        await sink.stop()
        priv = [a for a in auths if a["args"][0] == "KEYX"]
        self.assertGreaterEqual(len(priv), 2)                                       # auth при каждом подключении
        self.assertEqual(priv[0]["args"][2], bs.ws_signature("SECRETX", priv[0]["args"][1]))
        self.assertEqual(subs[0], {"op": "subscribe", "args": ["order", "execution", "position", "wallet"]})
        ids = [x[1]["exec_id"] for x in f.log if x[0] == "p"]
        self.assertEqual(ids, ["e1", "e2"])                                         # дубль после реконнекта отброшен
        ups = [x[2] for x in f.log if x[0] == "s" and x[1] == "private"]
        self.assertIn(False, ups)
        self.assertIn(True, ups)

    async def test_auth_failure_marks_feed_and_stops_private(self):
        async def h(c):
            await c.recv_text()
            await c.send(json.dumps({"success": False, "ret_msg": "Params error", "op": "auth"}))
            await asyncio.sleep(0.5)
        srv = await MockServer(h).start()
        sink = await MockServer(sink_handler).start()
        f = feed(urls={"public": sink.url, "private": srv.url}, opts=OPTS)
        task = asyncio.ensure_future(f.run())
        for _ in range(100):
            if f.auth_failed:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(f.auth_failed)
        self.assertFalse(f.state()["private"]["up"])
        await f.stop()
        await asyncio.wait_for(task, 2)
        await srv.stop()
        await sink.stop()
        self.assertTrue(any("auth" in e for e in f.sub_errors))
        self.assertNotIn("SECRETX", " ".join(f.sub_errors))


if __name__ == "__main__":
    unittest.main()
