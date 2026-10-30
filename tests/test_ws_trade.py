"""WebSocket Trade API (executor/ws_trade.py) на локальном фейковом сервере: авторизация, ордерные запросы по WS, REST для остального, откат на REST до отправки,
неизвестный исход после отправки (без повтора на REST), отказ биржи, переподключение. Сети и ключей нет."""
import asyncio
import json
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from test_wsclient import MockServer
from tb.executor import ws_trade as W
from tb.venues import bybit


class Server(object):
    """MockServer в отдельном потоке со своим циклом событий; поведение задаётся полями."""

    def __init__(self, auth_ok=True, answer=True, drop_after=None, ret_code=0):
        self.auth_ok, self.answer, self.drop_after, self.ret_code = auth_ok, answer, drop_after, ret_code
        self.requests, self.auths, self.conns = [], [], 0
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def handler(c):
            self.conns += 1
            n = 0
            while True:
                m = json.loads(await c.recv_text())
                if m.get("op") == "auth":
                    self.auths.append(m["args"])
                    await c.send(json.dumps({"success": self.auth_ok, "ret_msg": "" if self.auth_ok else "bad", "op": "auth", "conn_id": "c"}))
                elif m.get("op") == "ping":
                    await c.send(json.dumps({"success": True, "ret_msg": "pong", "op": "ping"}))
                elif m.get("reqId"):
                    self.requests.append(m)
                    n += 1
                    if self.drop_after is not None and n >= self.drop_after:
                        c.drop()
                        return
                    if self.answer:
                        data = {"orderId": "OID-%d" % n, "orderLinkId": (m["args"][0] or {}).get("orderLinkId")} if self.ret_code == 0 else {}
                        await c.send(json.dumps({"reqId": m["reqId"], "retCode": self.ret_code, "retMsg": "OK" if self.ret_code == 0 else "Order does not exist",
                                                 "op": m["op"], "data": data, "header": m.get("header"), "connId": "c"}))

        async def main():
            self.stopev = asyncio.Event()
            self.srv = await MockServer(handler).start()
            self.url = self.srv.url
            self.ready.set()
            await self.stopev.wait()
            self.srv.server.close()
            for c in list(self.srv.conns):
                c.drop()
            await asyncio.sleep(0.1)
        try:
            self.loop.run_until_complete(main())
        except Exception:
            pass
        finally:
            try:
                self.loop.run_until_complete(asyncio.sleep(0.05))
            except Exception:
                pass

    def stop(self):
        try:
            self.loop.call_soon_threadsafe(self.stopev.set)
        except RuntimeError:
            return
        self.th.join(3)


class RestSpy(object):
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        return {"retCode": 0, "retMsg": "OK", "result": {"via": "rest"}}


def make(server, **kw):
    ws = W.WsTrade("KEY", "SECRET", url=server.url, allow_plain=True, request_timeout=kw.pop("timeout", 2.0), **kw).start()
    self_close = ws.close
    return ws, self_close


class WsTradeTest(unittest.TestCase):
    def setUp(self):
        _bootstrap.require_localhost()

    def start(self, **kw):
        srv = Server(**kw)
        self.addCleanup(srv.stop)
        ws = W.WsTrade("KEY", "SECRET", url=srv.url, allow_plain=True, request_timeout=1.5).start()
        self.addCleanup(ws.close)
        return srv, ws

    def test_auth_signature_and_order_over_ws(self):
        srv, ws = self.start()
        self.assertTrue(ws.wait_ready(5))
        key, exp, sig = srv.auths[0]
        self.assertEqual((key, sig), ("KEY", W.bs.ws_signature("SECRET", exp)))
        rest = RestSpy()
        tr = W.WsTradeTransport(rest, ws)
        body = json.dumps({"category": "linear", "symbol": "SOLUSDT", "side": "Buy", "orderType": "Market", "qty": "1", "orderLinkId": "L1"}, separators=(",", ":"))
        resp = tr("POST", "https://api.bybit.com/v5/order/create", {"X-BAPI-TIMESTAMP": "1700000000000", "X-BAPI-RECV-WINDOW": "10000", "X-BAPI-SIGN": "zzz"}, body)
        self.assertEqual(rest.calls, [])
        self.assertEqual((resp["retCode"], resp["result"]["orderLinkId"]), (0, "L1"))
        sent = srv.requests[0]
        self.assertEqual((sent["op"], sent["args"][0]["orderLinkId"]), ("order.create", "L1"))
        self.assertEqual(sent["header"], {"X-BAPI-TIMESTAMP": "1700000000000", "X-BAPI-RECV-WINDOW": "10000"})      # подпись REST по WS не уходит

    def test_everything_else_goes_to_rest(self):
        srv, ws = self.start()
        ws.wait_ready(5)
        rest = RestSpy()
        tr = W.WsTradeTransport(rest, ws)
        tr("GET", "https://api.bybit.com/v5/position/list?category=linear", {}, None)
        tr("POST", "https://api.bybit.com/v5/position/trading-stop", {}, "{}")
        tr("POST", "https://api.bybit.com/v5/order/cancel-all", {}, "{}")
        self.assertEqual(len(rest.calls), 3)
        self.assertEqual(srv.requests, [])

    def test_not_ready_falls_back_to_rest_before_sending(self):
        ws = W.WsTrade("KEY", "SECRET", url="ws://127.0.0.1:1/x", allow_plain=True, request_timeout=0.5)       # не запущен: соединения нет
        rest = RestSpy()
        tr = W.WsTradeTransport(rest, ws)
        resp = tr("POST", "https://api.bybit.com/v5/order/create", {}, '{"symbol":"SOLUSDT"}')
        self.assertEqual(resp["result"], {"via": "rest"})
        self.assertEqual(tr.fallbacks, 1)

    def test_auth_failure_keeps_rest(self):
        srv, ws = self.start(auth_ok=False)
        time.sleep(0.5)
        self.assertFalse(ws.is_ready())
        self.assertTrue(ws.auth_failed)
        tr = W.WsTradeTransport(RestSpy(), ws)
        self.assertEqual(tr("POST", "https://api.bybit.com/v5/order/create", {}, '{"a":1}')["result"], {"via": "rest"})

    def test_no_answer_after_send_is_unknown_not_rest_retry(self):
        srv, ws = self.start(answer=False)
        ws.wait_ready(5)
        rest = RestSpy()
        tr = W.WsTradeTransport(rest, ws)
        with self.assertRaises(TimeoutError):
            tr("POST", "https://api.bybit.com/v5/order/create", {}, '{"orderLinkId":"L2"}')
        self.assertEqual(rest.calls, [])                  # ордер мог создаться: повтор на REST запрещён, исход проверяет исполнитель по link_id
        self.assertEqual(ws.stats["timeouts"], 1)

    def test_bybit_client_maps_exchange_reject_like_rest(self):
        srv, ws = self.start(ret_code=110001)
        ws.wait_ready(5)
        c = bybit.Client("testnet", key="K", secret="S", transport=W.WsTradeTransport(RestSpy(), ws))
        c.offset_ms = 0
        with self.assertRaises(bybit.BybitError) as e:
            c.call("POST", "/v5/order/cancel", {"category": "linear", "symbol": "SOLUSDT", "orderLinkId": "NOPE"})
        self.assertEqual(e.exception.code, 110001)

    def test_reconnects_after_drop_and_resumes(self):
        srv, ws = self.start(drop_after=1)
        ws.wait_ready(5)
        tr = W.WsTradeTransport(RestSpy(), ws)
        with self.assertRaises((ConnectionError, TimeoutError)):
            tr("POST", "https://api.bybit.com/v5/order/create", {}, '{"orderLinkId":"L3"}')       # соединение оборвано после отправки: исход неизвестен
        srv.drop_after = None
        end = time.time() + 15
        while time.time() < end and not (srv.conns >= 2 and ws.is_ready()):
            time.sleep(0.1)
        self.assertTrue(ws.is_ready())
        self.assertGreaterEqual(ws.stats["reconnects"], 1)
        r = tr("POST", "https://api.bybit.com/v5/order/create", {}, '{"orderLinkId":"L4"}')
        self.assertEqual(r["result"]["orderLinkId"], "L4")


if __name__ == "__main__":
    unittest.main()
