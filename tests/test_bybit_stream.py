import argparse
import asyncio
import hashlib
import hmac
import io
import json
import unittest

import _bootstrap  # noqa: F401
from test_wsclient import MockServer
from tb.venues import bybit_stream as bs

EXEC_SL = {"topic": "execution", "data": [{"category": "linear", "symbol": "SOLUSDT", "side": "Sell", "execPrice": "140.5", "execQty": "0.1",
                                           "execFee": "0.01", "execId": "e1", "orderId": "o1", "isMaker": False, "stopOrderType": "StopLoss",
                                           "closedSize": "0.1", "execPnl": "-0.3", "execType": "Trade"}]}


class AuthTests(unittest.TestCase):
    def test_signature_matches_spec(self):
        exp = hmac.new(b"sec", b"GET/realtime1700000005000", hashlib.sha256).hexdigest()
        self.assertEqual(bs.ws_signature("sec", 1700000005000), exp)
        m = json.loads(bs.auth_message("key", "sec", 1700000005000))
        self.assertEqual(m, {"op": "auth", "args": ["key", 1700000005000, exp]})

    def test_levels_parse(self):
        self.assertEqual(bs.parse_levels(["150", "sol=140.5"], ["SOLUSDT"]), {"SOLUSDT": [150.0, 140.5]})
        with self.assertRaises(ValueError):
            bs.parse_levels(["150"], ["SOLUSDT", "ETHUSDT"])
        self.assertEqual(bs.norm_symbol("sol"), "SOLUSDT")


class PrivateParseTests(unittest.TestCase):
    def test_sl_hit_with_pnl(self):
        evs = bs.PrivateParser().handle(EXEC_SL)
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e["event"], e["symbol"], e["price"], e["qty"], e["closed_qty"], e["pnl"]), ("sl_hit", "SOLUSDT", 140.5, 0.1, 0.1, -0.3))

    def test_fill_tp_and_duplicate_dropped(self):
        p = bs.PrivateParser()
        m = {"topic": "execution.linear", "data": [{"category": "linear", "symbol": "SOLUSDT", "side": "Buy", "execPrice": "150", "execQty": "0.2",
                                                    "execId": "x1", "orderId": "o2", "isMaker": True, "execType": "Trade"}]}
        self.assertEqual(p.handle(m)[0]["event"], "fill")
        self.assertEqual(p.handle(m), [])
        tp = json.loads(json.dumps(m))
        tp["data"][0].update(execId="x2", stopOrderType="TakeProfit")
        self.assertEqual(p.handle(tp)[0]["event"], "tp_hit")

    def test_funding_and_other_category_skipped(self):
        p = bs.PrivateParser()
        self.assertEqual(p.handle({"topic": "execution", "data": [{"category": "linear", "execType": "Funding", "execId": "f"}]}), [])
        self.assertEqual(p.handle({"topic": "execution", "data": [{"category": "spot", "execId": "s"}]}), [])

    def test_order_cancel_reject_and_noise(self):
        p = bs.PrivateParser()
        base = {"symbol": "SOLUSDT", "side": "Buy", "orderId": "o", "orderType": "Limit", "price": "1", "qty": "2", "cumExecQty": "0"}
        got = lambda st, **kw: p.handle({"topic": "order", "data": [dict(base, orderStatus=st, **kw)]})
        self.assertEqual(got("Cancelled", cancelType="CancelByUser")[0]["event"], "order_cancelled")
        self.assertEqual(got("Rejected", rejectReason="EC_PostOnlyWillTakeLiquidity")[0]["event"], "order_rejected")
        self.assertEqual(got("New"), [])
        self.assertEqual(got("Filled"), [])        # заполнения идут через execution
        self.assertEqual(got("Deactivated", stopOrderType="StopLoss"), [])   # стоп позиции гасится при закрытии

    def test_position_lifecycle_ignores_pnl_only_updates(self):
        p = bs.PrivateParser()
        pos = lambda size, **kw: p.handle({"topic": "position", "data": [dict({"symbol": "SOLUSDT", "side": "Buy", "size": size, "entryPrice": "150",
                                                                               "stopLoss": "145", "takeProfit": "160", "unrealisedPnl": "0"}, **kw)]})
        self.assertEqual(pos("0", side=""), [])                  # пустая позиция до открытия: не событие
        self.assertEqual(pos("0.5")[0]["event"], "position_open")
        self.assertEqual(pos("0.5", unrealisedPnl="3.2"), [])
        self.assertEqual(pos("0.5", stopLoss="150")[0]["event"], "position_changed")
        self.assertEqual(pos("0.3")[0]["event"], "position_changed")
        self.assertEqual(pos("0", side="")[0]["event"], "position_closed")
        self.assertEqual(pos("0", side=""), [])

    def test_rest_sync_detects_changes_while_offline(self):
        p = bs.PrivateParser()
        ev = p.sync([{"symbol": "SOLUSDT", "side": "Buy", "size": "0.5", "avgPrice": "150"}])
        self.assertEqual((ev[0]["event"], ev[0]["source"]), ("position_open", "rest"))
        self.assertEqual(p.sync([{"symbol": "SOLUSDT", "side": "Buy", "size": "0.5", "avgPrice": "150"}]), [])
        ev = p.sync([])
        self.assertEqual((ev[0]["event"], ev[0]["symbol"]), ("position_closed", "SOLUSDT"))


class PriceWatchTests(unittest.TestCase):
    def test_levels_fire_once(self):
        w = bs.PriceWatch({"SOLUSDT": [150.0]}, {"SOLUSDT": [140.0]})
        self.assertEqual(w.on_price("SOLUSDT", 145, 0), [])
        self.assertEqual(w.on_price("SOLUSDT", 150.2, 1)[0]["dir"], "above")
        self.assertEqual(w.on_price("SOLUSDT", 151, 2), [])
        self.assertEqual(w.on_price("SOLUSDT", 149, 3), [])
        e = w.on_price("SOLUSDT", 139.9, 4)
        self.assertEqual((e[0]["dir"], e[0]["level"], e[0]["price"]), ("below", 140.0, 139.9))
        self.assertEqual(w.on_price("SOLUSDT", 139, 5), [])
        self.assertEqual(w.on_price("ETHUSDT", 1, 6), [])

    def test_first_price_beyond_level_is_flagged(self):
        e = bs.PriceWatch({"SOLUSDT": [150.0]}, {}).on_price("SOLUSDT", 155, 0)
        self.assertTrue(e[0]["already"])

    def test_ticker_message_without_last_price_ignored(self):
        w = bs.PriceWatch({"SOLUSDT": [150.0]}, {})
        self.assertEqual(w.handle({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "bid1Price": "151"}}, 0), [])
        e = w.handle({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "151"}}, 1)
        self.assertEqual(e[0]["event"], "level")

    def test_move_pct_window_and_cooldown(self):
        w = bs.PriceWatch(move_pct=1.0, window=60)
        self.assertEqual(w.on_price("S", 100, 0), [])
        self.assertEqual(w.on_price("S", 100.5, 10), [])
        e = w.on_price("S", 101.2, 20)
        self.assertEqual((e[0]["event"], e[0]["pct"], e[0]["from"]), ("move", 1.2, 100))
        self.assertEqual(w.on_price("S", 103, 30), [])       # пауза на окно после срабатывания
        e = w.on_price("S", 101.9, 85)                       # пауза кончилась; окно: 103@30 -> 101.9, падение больше 1%
        self.assertEqual((e[0]["event"], e[0]["pct"] < 0, e[0]["from"]), ("move", True, 103))

    def test_slow_drift_does_not_trigger(self):
        w = bs.PriceWatch(move_pct=1.0, window=60)
        out = []
        for i in range(40):
            out += w.on_price("S", 100 + i * 0.1, i * 10)   # 0.1% за 10 с, ~0.6% за окно
        self.assertEqual(out, [])


class DownMonitorTests(unittest.TestCase):
    def test_down_after_threshold_once_then_up(self):
        m = bs.DownMonitor("private", 30)
        self.assertIsNone(m.update(False, 0))
        self.assertIsNone(m.update(False, 29))
        e = m.update(False, 31)
        self.assertEqual((e["event"], e["channel"]), ("ws_down", "private"))
        self.assertIsNone(m.update(False, 60))
        self.assertEqual(m.update(True, 61)["event"], "ws_up")
        self.assertIsNone(m.update(True, 62))

    def test_short_blip_silent(self):
        m = bs.DownMonitor("public", 30)
        m.update(False, 0)
        self.assertIsNone(m.update(True, 5))
        self.assertIsNone(m.update(False, 100))


class FakeClient:
    env, key, secret, offset_ms = "testnet", "KEY123", "SECRET456", 0

    def sync_time(self):
        pass


class WatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def ns(self, **kw):
        d = dict(symbols="", above=[], below=[], move_pct=0.0, move_window=60.0, private=True, down_after=30.0, max_seconds=2.0)
        d.update(kw)
        return argparse.Namespace(**d)

    async def test_private_auth_subscribe_event_and_reconnect(self):
        auths, subs = [], []

        async def h(c):
            auth = json.loads(await c.recv_text())
            auths.append(auth)
            await c.send(json.dumps({"success": True, "ret_msg": "", "op": "auth"}))
            subs.append(json.loads(await c.recv_text()))
            await c.send(json.dumps({"success": True, "op": "subscribe"}))
            if c.index == 0:
                await c.send(json.dumps(EXEC_SL))
                await asyncio.sleep(0.05)
                c.drop()
            else:
                for sz in ("0.5", "0"):
                    await c.send(json.dumps({"topic": "position", "data": [{"symbol": "SOLUSDT", "side": "Buy", "size": sz, "stopLoss": "0"}]}))
                await asyncio.sleep(2)
        srv = await MockServer(h).start()
        buf = io.StringIO()
        em = bs.Emitter("testnet", buf)
        opts = {"allow_plain": True, "backoff_base": 0.05, "backoff_max": 0.1, "ping_every": 50}
        code = await bs.run_watch(FakeClient(), self.ns(max_seconds=1.0), em, {"private": srv.url, "public": srv.url}, opts, sync_rows=lambda: [])
        await srv.stop()
        self.assertEqual(code, 0)
        evs = [json.loads(l) for l in buf.getvalue().splitlines()]
        names = [e["event"] for e in evs]
        self.assertEqual(names[0], "watch_start")
        self.assertIn("sl_hit", names)
        self.assertIn("position_closed", names)
        self.assertEqual(names[-1], "watch_end")
        self.assertGreaterEqual(len(auths), 2)                            # повторная auth после обрыва
        for a in auths:
            self.assertEqual(a["op"], "auth")
            self.assertEqual(a["args"][0], "KEY123")
            self.assertEqual(a["args"][2], bs.ws_signature("SECRET456", a["args"][1]))
        self.assertEqual(subs[0], {"op": "subscribe", "args": ["order", "execution", "position"]})
        self.assertEqual(subs[0], subs[1])                                # и повторная подписка
        self.assertNotIn("KEY123", buf.getvalue())                        # секреты не печатаются
        self.assertNotIn("SECRET456", buf.getvalue())
        self.assertNotIn(bs.ws_signature("SECRET456", auths[0]["args"][1]), buf.getvalue())

    async def test_auth_failure_reported_and_exits(self):
        async def h(c):
            await c.recv_text()
            await c.send(json.dumps({"success": False, "ret_msg": "Params Error", "op": "auth"}))
            await asyncio.sleep(1)
        srv = await MockServer(h).start()
        buf = io.StringIO()
        opts = {"allow_plain": True, "ping_every": 50}
        code = await bs.run_watch(FakeClient(), self.ns(max_seconds=3.0), bs.Emitter("testnet", buf), {"private": srv.url, "public": srv.url}, opts, sync_rows=lambda: [])
        await srv.stop()
        self.assertEqual(code, 3)
        self.assertIn("auth_failed", buf.getvalue())

    async def test_public_level_event(self):
        got = []

        async def h(c):
            got.append(json.loads(await c.recv_text()))
            await c.send(json.dumps({"topic": "tickers.SOLUSDT", "type": "snapshot", "data": {"symbol": "SOLUSDT", "lastPrice": "149"}}))
            await c.send(json.dumps({"topic": "tickers.SOLUSDT", "type": "delta", "data": {"symbol": "SOLUSDT", "lastPrice": "150.5"}}))
            await asyncio.sleep(2)
        srv = await MockServer(h).start()
        buf = io.StringIO()
        opts = {"allow_plain": True, "ping_every": 50}
        a = self.ns(private=False, symbols="sol", above=["150"], max_seconds=0.6)
        code = await bs.run_watch(FakeClient(), a, bs.Emitter("testnet", buf), {"private": srv.url, "public": srv.url}, opts)
        await srv.stop()
        self.assertEqual(code, 0)
        self.assertEqual(got[0], {"op": "subscribe", "args": ["tickers.SOLUSDT"]})
        lv = [json.loads(l) for l in buf.getvalue().splitlines() if '"level"' in l]
        self.assertEqual(len(lv), 1)
        self.assertEqual((lv[0]["symbol"], lv[0]["price"]), ("SOLUSDT", 150.5))

    async def test_nothing_to_listen_is_error(self):
        buf = io.StringIO()
        code = await bs.run_watch(FakeClient(), self.ns(private=False, symbols="SOL"), bs.Emitter("testnet", buf))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
