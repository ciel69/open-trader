"""Низкоуровневые операции Bybit для исполнителя v2 (venues/bybit_exec.py, WP1A): параметры и подпись каждого запроса, округление стопа
(таблица кейсов), монотонность, 34040, пагинация executions, order_by_link, классификация ошибок. Без сети: состоящая из памяти биржа FakeExchange.

FakeExchange используется и в test_live_port.py (контрактный тест порта)."""
import hashlib
import hmac
import json
import unittest
import urllib.parse

import _bootstrap  # noqa: F401
from tb.executor.ports import PortError
from tb.venues import bybit as bb
from tb.venues import bybit_exec as X

OK = {"retCode": 0, "retMsg": "OK"}
T0 = 1700000000.0


def err(code, msg="err"):
    return {"retCode": code, "retMsg": msg, "result": {}}


class FakeExchange(object):
    """Транспорт Client: состояние в памяти. Инструмент SOLUSDT: tick 0.01, qtyStep 0.1, minQty 0.1. Цена 100 (bid 99.95 / ask 100.05)."""

    def __init__(self, last=100.0, tick="0.01"):
        self.calls = []                 # (method, path, params|body, headers)
        self.last, self.bid, self.ask, self.mark = last, last - 0.05, last + 0.05, last
        self.tick = tick
        self.positions = {}             # symbol -> dict Bybit
        self.orders = {}                # orderId -> dict Bybit
        self.history = {}
        self.execs = []                 # dict Bybit (execution/list), старые первыми
        self.closed = []
        self.n = 0
        self.script = {}                # path -> [ответ или исключение], одноразово
        self.page = 100

    # ---- помощники теста
    def paths(self):
        return [(m, p) for m, p, _, _ in self.calls]

    def writes(self):
        return [(p, b) for m, p, b, _ in self.calls if m == "POST"]

    def push(self, path, resp):
        self.script.setdefault(path, []).append(resp)

    def new_exec(self, symbol, side, px, qty, link, oid, stop="", closed=0.0, pnl=0.0, maker=False, ts=None):
        self.n += 1
        e = {"execId": "E%d" % self.n, "symbol": symbol, "side": side, "execPrice": str(px), "execQty": str(qty), "execFee": "0.01",
             "isMaker": maker, "orderLinkId": link, "orderId": oid, "stopOrderType": stop or "UNKNOWN", "execType": "Trade",
             "closedSize": str(closed), "execPnl": str(pnl), "execTime": str(int((ts or T0) * 1000) + self.n)}
        self.execs.append(e)
        return e

    def open_position(self, symbol, side, size, avg, sl=0.0, tp=0.0):
        self.positions[symbol] = {"symbol": symbol, "side": side, "size": str(size), "avgPrice": str(avg), "markPrice": str(self.mark),
                                  "stopLoss": str(sl) if sl else "0", "takeProfit": str(tp) if tp else "0", "liqPrice": "50", "unrealisedPnl": "0.5",
                                  "leverage": "5", "positionIM": "20", "updatedTime": str(int(T0 * 1000)), "slTriggerBy": "LastPrice", "positionIdx": 0}

    # ---- транспорт
    def __call__(self, method, url, headers, body):
        path = url.split("bybit.com")[1].split("?")[0]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        b = json.loads(body) if body else None
        self.calls.append((method, path, b if b is not None else q, headers))
        if self.script.get(path):
            r = self.script[path].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        h = getattr(self, "h_" + path.replace("/v5/", "").replace("/", "_").replace("-", "_"), None)
        if h is None:
            return err(10001, "unknown " + path)
        return h(q if method == "GET" else b)

    def h_market_time(self, p):
        return dict(OK, result={"timeNano": "1700000000000000000"})

    def h_market_instruments_info(self, p):
        return dict(OK, result={"list": [{"symbol": p["symbol"], "lotSizeFilter": {"minOrderQty": "0.1", "qtyStep": "0.1", "minNotionalValue": "5"},
                                           "priceFilter": {"tickSize": self.tick}, "leverageFilter": {"maxLeverage": "50"}}]})

    def h_market_tickers(self, p):
        return dict(OK, result={"list": [{"symbol": p["symbol"], "bid1Price": str(self.bid), "ask1Price": str(self.ask), "lastPrice": str(self.last),
                                           "markPrice": str(self.mark), "fundingRate": "0.0001"}]})

    def h_market_kline(self, p):
        base = 1700000000000
        step = int(p["interval"]) * 60000
        rows = [[str(base + i * step), "100", "101", "99", "100.5", "10", "1000"] for i in range(int(p["limit"]))]
        return dict(OK, result={"list": list(reversed(rows))})            # Bybit отдаёт новые первыми

    def h_account_wallet_balance(self, p):
        return dict(OK, result={"list": [{"totalEquity": "40.5", "totalAvailableBalance": "30.25", "coin": []}]})

    def h_position_list(self, p):
        sym = p.get("symbol")
        rows = [dict(v) for k, v in self.positions.items() if sym in (None, k)]
        return dict(OK, result={"list": rows if rows else ([{"symbol": sym, "side": "", "size": "0"}] if sym else [])})

    def h_position_set_leverage(self, p):
        return err(110043, "leverage not modified") if getattr(self, "lev_same", False) else dict(OK, result={})

    def h_position_trading_stop(self, p):
        pos = self.positions.get(p["symbol"])
        if not pos:
            return err(10001, "can not set tp/sl/ts for zero position")
        if "stopLoss" in p:
            if float(p["stopLoss"]) == float(pos["stopLoss"]):
                return err(34040, "not modified")
            pos["stopLoss"] = p["stopLoss"]
        if "takeProfit" in p:
            pos["takeProfit"] = p["takeProfit"]
        return dict(OK, result={})

    def _order_view(self, o):
        return dict(o)

    def h_order_create(self, p):
        self.n += 1
        oid = "O%d" % self.n
        link = p.get("orderLinkId", "")
        if link and any(o.get("orderLinkId") == link for o in list(self.orders.values()) + list(self.history.values())):
            return err(110072, "OrderLinkedID is duplicate")
        o = {"orderId": oid, "orderLinkId": link, "symbol": p["symbol"], "side": p["side"], "orderType": p["orderType"], "price": p.get("price", "0"),
             "qty": p["qty"], "cumExecQty": "0", "orderStatus": "New", "reduceOnly": bool(p.get("reduceOnly")), "stopOrderType": "", "avgPrice": "0",
             "cumExecFee": "0", "createdTime": str(int(T0 * 1000)), "updatedTime": str(int(T0 * 1000)), "timeInForce": p.get("timeInForce", "IOC")}
        pos = self.positions.get(p["symbol"])
        if o["reduceOnly"] and not pos:
            return err(110017, "Reduce-only rule not satisfied")
        if p["orderType"] == "Market":
            self._fill(o, float(p["qty"]), self.last, p)
        else:
            self.orders[oid] = o
        return dict(OK, result={"orderId": oid, "orderLinkId": link})

    def _fill(self, o, qty, px, p=None):
        o["cumExecQty"], o["avgPrice"], o["orderStatus"] = str(qty), str(px), "Filled"
        sym = o["symbol"]
        if o["reduceOnly"]:
            pos = self.positions[sym]
            left = float(pos["size"]) - qty
            self.new_exec(sym, o["side"], px, qty, o["orderLinkId"], o["orderId"], closed=qty)
            if left <= 1e-12:
                del self.positions[sym]
            else:
                pos["size"] = str(round(left, 8))
        else:
            self.new_exec(sym, o["side"], px, qty, o["orderLinkId"], o["orderId"])
            self.open_position(sym, o["side"], qty, px, sl=float((p or {}).get("stopLoss", 0) or 0))
        self.history[o["orderId"]] = o
        self.orders.pop(o["orderId"], None)

    def fill_limit(self, oid, qty=None):
        o = self.orders[oid]
        self._fill(o, qty or float(o["qty"]), float(o["price"]))

    def _find(self, p):
        for o in list(self.orders.values()):
            if (p.get("orderId") and o["orderId"] == p["orderId"]) or (p.get("orderLinkId") and o["orderLinkId"] == p["orderLinkId"]):
                return o
        return None

    def h_order_cancel(self, p):
        o = self._find(p)
        if not o:
            return err(110001, "order not exists or too late to cancel")
        o["orderStatus"] = "Cancelled"
        self.history[o["orderId"]] = self.orders.pop(o["orderId"])
        return dict(OK, result={"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})

    def h_order_cancel_all(self, p):
        """Как testnet (WP1A): без orderFilter или с StopOrder снимает и стоп уровня позиции; Order снимает только обычные ордера."""
        flt = p.get("orderFilter")
        done = [self.h_order_cancel({"orderId": k})["result"] for k, o in list(self.orders.items()) if not (flt == "Order" and o.get("stopOrderType"))]
        if flt != "Order" and p["symbol"] in self.positions:
            self.positions[p["symbol"]]["stopLoss"] = "0"
        return dict(OK, result={"list": done})

    def h_order_amend(self, p):
        o = self._find(p)
        if not o:
            return err(110001, "order not exists")
        if p.get("price", o["price"]) == o["price"] and p.get("qty", o["qty"]) == o["qty"]:
            return err(10001, "order not modified")
        if "price" in p:
            o["price"] = p["price"]
        if "qty" in p:
            o["qty"] = p["qty"]
        return dict(OK, result={"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})

    def h_order_realtime(self, p):
        rows = [o for o in self.orders.values() if (p.get("symbol") in (None, o["symbol"])) and (not p.get("orderLinkId") or o["orderLinkId"] == p["orderLinkId"])]
        return dict(OK, result={"list": [self._order_view(o) for o in rows]})

    def h_order_history(self, p):
        rows = [o for o in self.history.values() if not p.get("orderLinkId") or o["orderLinkId"] == p["orderLinkId"]]
        return dict(OK, result={"list": rows})

    def h_execution_list(self, p):
        a, b = int(p.get("startTime", 0)), int(p.get("endTime", 1 << 60))
        if b - a > 7 * 24 * 3600 * 1000:
            return err(10001, "startTime and endTime interval must not exceed 7 days")
        rows = [e for e in reversed(self.execs) if a <= int(e["execTime"]) <= b and p.get("symbol") in (None, e["symbol"])]
        i = int(p.get("cursor") or 0)
        lim = min(int(p.get("limit", 50)), self.page)
        res = {"list": rows[i:i + lim]}
        res["nextPageCursor"] = str(i + lim) if i + lim < len(rows) else ""
        return dict(OK, result=res)

    def h_position_closed_pnl(self, p):
        return dict(OK, result={"list": list(self.closed), "nextPageCursor": ""})


def mk(ex=None, **kw):
    ex = ex or FakeExchange(**kw)
    c = bb.Client("testnet", "KEY", "SEC", transport=ex, clock=lambda: T0)
    return c, ex


INS = {"tick": "0.01", "qty_step": "0.1", "min_qty": "0.1", "min_notional": "5", "max_leverage": 50.0}


class RequestShapeTests(unittest.TestCase):
    def test_new_paths_allowed(self):
        for mp in (("POST", "/v5/position/trading-stop"), ("POST", "/v5/order/amend"), ("GET", "/v5/execution/list")):
            self.assertIn(mp, bb.ALLOWED)

    def test_no_withdraw_or_transfer_paths(self):
        for _, p in bb.ALLOWED:
            self.assertNotIn("withdraw", p)
            self.assertNotIn("transfer", p)
            self.assertNotIn("api-key", p)

    def test_signed_write_has_headers_and_hmac(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        X.set_trading_stop(c, "SOL", sl=96.5)
        m, path, body, h = ex.calls[-1]
        self.assertEqual((m, path), ("POST", "/v5/position/trading-stop"))
        raw = json.dumps(body, separators=(",", ":"))
        ts = h["X-BAPI-TIMESTAMP"]
        exp = hmac.new(b"SEC", ("%sKEY%s%s" % (ts, bb.RECV_WINDOW, raw)).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(h["X-BAPI-SIGN"], exp)
        self.assertEqual(h["X-BAPI-API-KEY"], "KEY")

    def test_trading_stop_params(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        X.set_trading_stop(c, "SOL", sl=96.5, tp=110, trigger_by="MarkPrice")
        body = ex.calls[-1][2]
        self.assertEqual(body, {"category": "linear", "symbol": "SOLUSDT", "tpslMode": "Full", "positionIdx": 0, "stopLoss": "96.5",
                                "slTriggerBy": "MarkPrice", "takeProfit": "110", "tpTriggerBy": "MarkPrice"})

    def test_trading_stop_not_modified_is_unchanged(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        self.assertEqual(X.set_trading_stop(c, "SOL", sl=95), {"ok": True, "unchanged": True})

    def test_trading_stop_remove_forbidden(self):
        c, ex = mk()
        for kw in ({"sl": 0}, {"sl": -1}, {"tp": 0}):
            with self.assertRaises(PortError) as cm:
                X.set_trading_stop(c, "SOL", **kw)
            self.assertEqual(cm.exception.code, "STOP_REMOVE_FORBIDDEN")
        with self.assertRaises(PortError):
            X.set_trading_stop(c, "SOL")
        self.assertEqual(ex.writes(), [])

    def test_set_leverage_and_not_modified(self):
        c, ex = mk()
        self.assertFalse(X.set_leverage(c, "SOL", 10)["unchanged"])
        self.assertEqual(ex.calls[-1][2], {"category": "linear", "symbol": "SOLUSDT", "buyLeverage": "10", "sellLeverage": "10"})
        ex.lev_same = True
        self.assertTrue(X.set_leverage(c, "SOL", 10)["unchanged"])
        with self.assertRaises(PortError):
            X.set_leverage(c, "SOL", 0)

    def test_place_entry_market_params_with_embedded_stop(self):
        c, ex = mk()
        r = X.place_entry(c, "SOL", "long", 1.234, "tbx-1-1", "market", sl=95.004, ins=INS)
        body = ex.writes()[-1][1]
        self.assertEqual(body, {"category": "linear", "symbol": "SOLUSDT", "side": "Buy", "orderType": "Market", "qty": "1.2", "positionIdx": 0,
                                "tpslMode": "Full", "stopLoss": "95", "slTriggerBy": "LastPrice", "orderLinkId": "tbx-1-1"})
        self.assertEqual((r["qty"], r["stop"]), (1.2, 95.0))
        self.assertEqual(float(ex.positions["SOLUSDT"]["stopLoss"]), 95.0)

    def test_place_entry_stop_rounded_away_from_entry(self):
        for side, sl, exp in (("long", 94.996, "94.99"), ("short", 105.004, "105.01")):
            c, ex = mk()
            X.place_entry(c, "SOL", side, 1, "tbx-1-1", "market", sl=sl, ins=INS)
            self.assertEqual(ex.writes()[-1][1]["stopLoss"], exp, side)

    def test_place_entry_limit_post_only_and_gtc(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99.999, sl=95, ins=INS)
        b = ex.writes()[-1][1]
        self.assertEqual((b["orderType"], b["price"], b["timeInForce"]), ("Limit", "99.99", "PostOnly"))   # покупка: цена вниз
        X.place_entry(c, "SOL", "short", 1, "tbx-1-3", "limit", price=100.001, post_only=False, sl=105, ins=INS)
        b = ex.writes()[-1][1]
        self.assertEqual((b["side"], b["price"], b["timeInForce"]), ("Sell", "100.01", "GTC"))              # продажа: цена вверх

    def test_place_entry_without_stop_never_calls_exchange(self):
        c, ex = mk()
        for sl in (None, 0, 0.0, -1, "0"):
            with self.assertRaises(PortError) as cm:
                X.place_entry(c, "SOL", "long", 1, "tbx-1-1", "market", sl=sl, ins=INS)
            self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", "NO_STOP"))
        self.assertEqual(ex.calls, [])

    def test_place_entry_bad_args(self):
        c, ex = mk()
        bad = [dict(side="up"), dict(order_type="stop"), dict(link_id=""), dict(link_id="x" * 37), dict(qty=0), dict(qty=0.05), dict(order_type="limit")]
        for ch in bad:
            kw = dict(side="long", qty=1, link_id="tbx-1-1", order_type="market")
            kw.update(ch)
            with self.assertRaises(PortError, msg=str(ch)):
                X.place_entry(c, "SOL", kw["side"], kw["qty"], kw["link_id"], kw["order_type"], sl=95, ins=INS)
        self.assertEqual(ex.writes(), [])

    def test_place_reduce_only_params(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Buy", 2, 100, sl=95)
        X.place_reduce_only(c, "SOL", "sell", 1.0, "tbx-1-4", price=110.001, post_only=True, ins=INS)
        b = ex.writes()[-1][1]
        self.assertEqual(b, {"category": "linear", "symbol": "SOLUSDT", "side": "Sell", "orderType": "Limit", "qty": "1", "reduceOnly": True,
                             "positionIdx": 0, "orderLinkId": "tbx-1-4", "price": "110.01", "timeInForce": "PostOnly"})
        X.place_reduce_only(c, "SOL", "sell", 0.5, "tbx-1-5", ins=INS)
        b = ex.writes()[-1][1]
        self.assertEqual((b["orderType"], b["reduceOnly"], "price" in b), ("Market", True, False))

    def test_reduce_only_buy_price_floor(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Sell", 2, 100, sl=105)
        X.place_reduce_only(c, "SOL", "buy", 1, "tbx-1-6", price=90.009, ins=INS)
        self.assertEqual(ex.writes()[-1][1]["price"], "90")

    def test_amend_params_and_rounding(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        X.amend_order(c, "SOL", "tbx-1-2", price=98.456, qty=1.55, ins=INS)
        b = ex.writes()[-1]
        self.assertEqual(b[0], "/v5/order/amend")
        self.assertEqual(b[1], {"category": "linear", "symbol": "SOLUSDT", "orderLinkId": "tbx-1-2", "price": "98.46", "qty": "1.5"})
        with self.assertRaises(PortError):
            X.amend_order(c, "SOL", "tbx-1-2", ins=INS)

    def test_amend_same_values_is_unchanged(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        self.assertTrue(X.amend_order(c, "SOL", "tbx-1-2", price=99, ins=INS)["unchanged"])
        self.assertFalse(X.amend_order(c, "SOL", "tbx-1-2", price=98, ins=INS)["unchanged"])

    def test_amend_missing_order_rejected(self):
        c, ex = mk()
        with self.assertRaises(bb.BybitError) as cm:
            X.amend_order(c, "SOL", "tbx-9-9", price=90, ins=INS)
        self.assertEqual(cm.exception.code, 110001)

    def test_cancel_by_link_and_gone(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        self.assertFalse(X.cancel_by_link(c, "SOL", "tbx-1-2")["gone"])
        self.assertEqual(ex.writes()[-1], ("/v5/order/cancel", {"category": "linear", "symbol": "SOLUSDT", "orderLinkId": "tbx-1-2"}))
        self.assertTrue(X.cancel_by_link(c, "SOL", "tbx-1-2")["gone"])        # повторно: ордера нет = успех

    def test_cancel_all_reduce_only_only(self):
        c, ex = mk()
        ex.open_position("SOLUSDT", "Buy", 2, 100, sl=95)
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=90, sl=85, ins=INS)             # вход: остаётся
        X.place_reduce_only(c, "SOL", "sell", 1, "tbx-1-3", price=110, ins=INS)
        r = X.cancel_all(c, "SOL", reduce_only_only=True)
        self.assertEqual(len(r["cancelled"]), 1)
        self.assertEqual([o["link_id"] for o in X.open_orders(c, "SOL")], ["tbx-1-2"])
        self.assertEqual(ex.positions["SOLUSDT"]["stopLoss"], "95")
        X.cancel_all(c, "SOL")
        self.assertEqual(X.open_orders(c, "SOL"), [])
        self.assertEqual(ex.writes()[-1], ("/v5/order/cancel-all", {"category": "linear", "symbol": "SOLUSDT", "orderFilter": "Order"}))
        self.assertEqual(ex.positions["SOLUSDT"]["stopLoss"], "95")            # стоп позиции жив (orderFilter=Order обязателен)

    def test_klines_ascending_floats_ms(self):
        c, ex = mk()
        rows = X.klines(c, "SOL", 5, 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))
        self.assertEqual(rows[0][0], 1700000000000)
        self.assertEqual(rows[0][1:], (100.0, 101.0, 99.0, 100.5, 10.0, 1000.0))
        self.assertEqual(ex.calls[-1][2]["interval"], "5")
        with self.assertRaises(PortError):
            X.klines(c, "SOL", 7)

    def test_account_and_quote(self):
        c, ex = mk()
        a = X.account(c, clock=lambda: 5.0)
        self.assertEqual((a["equity"], a["free"], a["ts"]), (40.5, 30.25, 5.0))
        self.assertEqual(X.quote(c, "SOL"), (99.95, 100.05, 100.0))

    def test_positions_norm_shape(self):
        c, ex = mk()
        self.assertEqual(X.positions_norm(c), [])
        ex.open_position("SOLUSDT", "Sell", 2, 100, sl=105, tp=90)
        p = X.positions_norm(c)[0]
        self.assertEqual((p["sym"], p["side"], p["size"], p["avg"], p["sl"], p["tp"], p["lev"], p["margin"]), ("SOL", "short", 2.0, 100.0, 105.0, 90.0, 5.0, 20.0))
        self.assertEqual(X.get_stop(c, "SOL")["sl"], 105.0)
        self.assertIsNone(X.get_stop(c, "ETH"))


# ------------------------------------------------------------------ монотонность и округление стопа
class TightenStopTests(unittest.TestCase):
    def setUp(self):
        self.c, self.ex = mk(tick="0.5")

    def tighten(self, side, stop, **kw):
        return X.tighten_stop(self.c, "SOL", side, stop, ins=dict(INS, tick="0.5"), **kw)

    def writes(self):
        return [w for w in self.ex.writes() if w[0] == "/v5/position/trading-stop"]

    def test_long_tighten_ok(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        r = self.tighten("long", 96)
        self.assertEqual(r, {"ok": True, "stop": 96.0, "unchanged": False})
        self.assertEqual(self.writes()[-1][1]["stopLoss"], "96")

    def test_long_weaker_rejected_without_exchange_write(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=96)
        with self.assertRaises(PortError) as cm:
            self.tighten("long", 95)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", "STOP_NOT_TIGHTER"))
        self.assertEqual(self.writes(), [])

    def test_equal_is_unchanged_without_write(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=96)
        self.assertTrue(self.tighten("long", 96)["unchanged"])
        self.assertEqual(self.writes(), [])

    def test_short_mirror(self):
        self.ex.open_position("SOLUSDT", "Sell", 1, 100, sl=105)
        self.assertEqual(self.tighten("short", 104)["stop"], 104.0)
        with self.assertRaises(PortError) as cm:
            self.tighten("short", 106)
        self.assertEqual(cm.exception.code, "STOP_NOT_TIGHTER")
        self.assertTrue(self.tighten("short", 104)["unchanged"])

    def test_remove_forbidden(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        for v in (0, 0.0, None, -3):
            with self.assertRaises(PortError) as cm:
                self.tighten("long", v)
            self.assertEqual(cm.exception.code, "STOP_REMOVE_FORBIDDEN")
        self.assertEqual(self.writes(), [])

    def test_wrong_side_of_price(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        for v in (100.0, 101.0):                    # long: стоп >= last
            with self.assertRaises(PortError) as cm:
                self.tighten("long", v)
            self.assertEqual(cm.exception.code, "STOP_ON_WRONG_SIDE")
        self.ex.positions.clear()
        self.ex.open_position("SOLUSDT", "Sell", 1, 100, sl=105)
        with self.assertRaises(PortError) as cm:
            self.tighten("short", 99)
        self.assertEqual(cm.exception.code, "STOP_ON_WRONG_SIDE")
        self.assertEqual(self.writes(), [])

    def test_wrong_side_uses_trigger_price(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        self.ex.mark = 97.0                         # mark ниже last
        self.assertEqual(self.tighten("long", 98.5, trigger_by="last")["stop"], 98.5)
        self.ex.positions["SOLUSDT"]["stopLoss"] = "95"
        with self.assertRaises(PortError) as cm:
            self.tighten("long", 98.5, trigger_by="mark")
        self.assertEqual(cm.exception.code, "STOP_ON_WRONG_SIDE")
        self.assertEqual(self.writes()[-1][1]["slTriggerBy"], "LastPrice")

    def test_no_position_is_position_gone(self):
        with self.assertRaises(PortError) as cm:
            self.tighten("long", 96)
        self.assertEqual(cm.exception.kind, "position_gone")

    def test_side_mismatch(self):
        self.ex.open_position("SOLUSDT", "Sell", 1, 100, sl=105)
        with self.assertRaises(PortError) as cm:
            self.tighten("long", 96)
        self.assertEqual(cm.exception.code, "SIDE_MISMATCH")

    def test_stop_set_when_none_exists(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=0)
        self.assertEqual(self.tighten("long", 90)["stop"], 90.0)

    def test_exchange_34040_race_is_unchanged(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        self.ex.push("/v5/position/trading-stop", err(34040, "not modified"))
        self.assertTrue(self.tighten("long", 96)["unchanged"])

    def test_tp_untouched_by_stop_move(self):
        self.ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95, tp=110)
        self.tighten("long", 96)
        self.assertNotIn("takeProfit", self.writes()[-1][1])
        self.assertEqual(self.ex.positions["SOLUSDT"]["takeProfit"], "110")

    def test_rounding_table(self):
        """tick 0.5. (сторона, текущий стоп, запрошенный) -> (результат). Ужесточение: long вверх, short вниз; сравнение с текущим после округления."""
        table = [
            # long: запрос 96.2 -> 96.5 (вверх, строже запрошенного)
            ("long", 95.0, 96.2, ("ok", 96.5)),
            ("long", 95.0, 96.5, ("ok", 96.5)),              # ровно на границе тика
            ("long", 95.0, 96.0, ("ok", 96.0)),
            ("long", 96.0, 95.9, ("unchanged", 96.0)),       # 95.9 -> 96.0 == текущему: unchanged (а не «слабее»)
            ("long", 96.0, 95.4, ("tighter_err", None)),     # 95.4 -> 95.5 < 96.0
            ("long", 96.0, 96.0, ("unchanged", 96.0)),
            ("long", 96.0, 96.01, ("ok", 96.5)),             # на волосок выше текущего -> следующий тик (строже)
            # short: запрос 103.8 -> 103.5 (вниз)
            ("short", 105.0, 103.8, ("ok", 103.5)),
            ("short", 105.0, 103.5, ("ok", 103.5)),
            ("short", 104.0, 104.1, ("unchanged", 104.0)),   # 104.1 -> 104.0 == текущему
            ("short", 104.0, 104.6, ("tighter_err", None)),  # 104.6 -> 104.5 > 104.0
            ("short", 104.0, 103.99, ("ok", 103.5)),
        ]
        for side, cur, req, (kind, val) in table:
            self.ex.positions.clear()
            self.ex.open_position("SOLUSDT", "Buy" if side == "long" else "Sell", 1, 100, sl=cur)
            before = len(self.writes())
            label = "%s cur=%s req=%s" % (side, cur, req)
            if kind == "tighter_err":
                with self.assertRaises(PortError, msg=label) as cm:
                    self.tighten(side, req)
                self.assertEqual(cm.exception.code, "STOP_NOT_TIGHTER", label)
                self.assertEqual(len(self.writes()), before, label)
            else:
                r = self.tighten(side, req)
                self.assertEqual(r["stop"], val, label)
                self.assertEqual(r["unchanged"], kind == "unchanged", label)
                self.assertEqual(len(self.writes()), before + (0 if kind == "unchanged" else 1), label)


# ------------------------------------------------------------------ чтение: ордера и исполнения
class ReadTests(unittest.TestCase):
    def test_order_by_link_realtime_then_history(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        o = X.order_by_link(c, "SOL", "tbx-1-2")
        self.assertEqual((o["sym"], o["link_id"], o["side"], o["type"], o["price"], o["qty"], o["status"], o["reduce_only"]),
                         ("SOL", "tbx-1-2", "Buy", "limit", 99.0, 1.0, "New", False))
        self.assertEqual(ex.paths()[-1], ("GET", "/v5/order/realtime"))
        X.cancel_by_link(c, "SOL", "tbx-1-2")
        o = X.order_by_link(c, "SOL", "tbx-1-2")
        self.assertEqual(o["status"], "Cancelled")
        self.assertEqual(ex.paths()[-2:], [("GET", "/v5/order/realtime"), ("GET", "/v5/order/history")])
        self.assertIsNone(X.order_by_link(c, "SOL", "tbx-9-9"))

    def test_open_orders_shape_and_symbol_filter(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        os_ = X.open_orders(c, "SOL")
        self.assertEqual(set(["sym", "link_id", "order_id", "side", "type", "price", "qty", "filled", "reduce_only", "status", "ts"]) - set(os_[0]), set())
        self.assertEqual(ex.calls[-1][2]["symbol"], "SOLUSDT")
        X.open_orders(c)
        self.assertEqual(ex.calls[-1][2]["settleCoin"], "USDT")

    def test_open_orders_hides_position_level_stops(self):
        c, ex = mk()
        X.place_entry(c, "SOL", "long", 1, "tbx-1-2", "limit", price=99, sl=95, ins=INS)
        ex.orders["S1"] = {"orderId": "S1", "orderLinkId": "", "symbol": "SOLUSDT", "side": "Sell", "orderType": "Market", "price": "0", "qty": "1",
                           "cumExecQty": "0", "orderStatus": "Untriggered", "reduceOnly": True, "stopOrderType": "StopLoss", "createdTime": "1", "updatedTime": "1"}
        self.assertEqual([o["link_id"] for o in X.open_orders(c, "SOL")], ["tbx-1-2"])
        self.assertEqual(len(X.open_orders(c, "SOL", include_stops=True)), 2)
        ex.positions["SOLUSDT"] = {}
        ex.open_position("SOLUSDT", "Buy", 1, 100, sl=95)
        self.assertEqual(X.cancel_all(c, "SOL", reduce_only_only=True)["cancelled"], [])      # стоп не отменяется как reduce-only ордер
        self.assertIn("S1", ex.orders)

    def _seed_execs(self, ex, n):
        for i in range(n):
            ex.new_exec("SOLUSDT", "Buy" if i % 2 == 0 else "Sell", 100 + i, 0.1, "tbx-1-%d" % i, "O%d" % i, ts=T0 - 1000 + i)

    def test_executions_pagination_cursor_and_order(self):
        c, ex = mk()
        ex.page = 3
        self._seed_execs(ex, 8)
        rows = X.executions(c, int((T0 - 2000) * 1000), clock=lambda: T0)
        self.assertEqual(len(rows), 8)
        self.assertEqual([r["ts_ms"] for r in rows], sorted(r["ts_ms"] for r in rows))
        pages = [b for m, p, b, _ in ex.calls if p == "/v5/execution/list"]
        self.assertEqual(len(pages), 3)
        self.assertNotIn("cursor", pages[0])
        self.assertEqual(pages[1]["cursor"], "3")
        self.assertEqual(pages[0]["limit"], "100")

    def test_executions_fields(self):
        c, ex = mk()
        ex.new_exec("SOLUSDT", "Sell", 94.9, 1, "tbx-1-9", "O9", stop="StopLoss", closed=1, pnl=-5.1, maker=True, ts=T0)
        e = X.executions(c, int(T0 * 1000) - 10, "SOL", clock=lambda: T0 + 1)[0]
        self.assertEqual((e["sym"], e["side"], e["price"], e["qty"], e["fee"], e["is_maker"], e["link_id"], e["order_id"], e["stop_type"],
                          e["exec_type"], e["closed_size"], e["pnl"]), ("SOL", "Sell", 94.9, 1.0, 0.01, True, "tbx-1-9", "O9", "StopLoss", "Trade", 1.0, -5.1))
        X.executions(c, 0, clock=lambda: T0 + 1)
        self.assertTrue(any(b["startTime"] == "0" for m, p, b, _ in ex.calls if p == "/v5/execution/list"))

    def test_executions_long_range_split_in_windows(self):
        c, ex = mk()
        X.executions(c, int((T0 - 20 * 86400) * 1000), clock=lambda: T0)
        wins = [(int(b["startTime"]), int(b["endTime"])) for m, p, b, _ in ex.calls if p == "/v5/execution/list"]
        self.assertGreaterEqual(len(wins), 3)
        for a, b in wins:
            self.assertLessEqual(b - a, 7 * 24 * 3600 * 1000)
        for (a1, b1), (a2, b2) in zip(wins, wins[1:]):
            self.assertEqual(a2, b1 + 1)

    def test_executions_funding_skipped_and_dups_removed(self):
        c, ex = mk()
        e = ex.new_exec("SOLUSDT", "Buy", 100, 1, "l", "O1", ts=T0)
        ex.execs.append(dict(e))                                       # дубль execId
        ex.new_exec("SOLUSDT", "Buy", 100, 1, "", "", ts=T0)
        ex.execs[-1]["execType"] = "Funding"
        self.assertEqual(len(X.executions(c, 0, clock=lambda: T0 + 1)), 1)

    def test_closed_pnl_normalised_with_position_side(self):
        c, ex = mk()
        ex.closed = [{"symbol": "SOLUSDT", "side": "Sell", "closedSize": "1", "avgEntryPrice": "100", "avgExitPrice": "95", "closedPnl": "-5.2", "openFee": "0.05",
                      "closeFee": "0.06", "updatedTime": str(int(T0 * 1000)), "orderId": "O1"}]
        r = X.closed_pnl(c, "SOL", 0, clock=lambda: T0 + 1)
        self.assertEqual((r[0]["sym"], r[0]["side"], r[0]["qty"], r[0]["entry"], r[0]["exit"], r[0]["pnl"], r[0]["open_fee"], r[0]["close_fee"], r[0]["ts_ms"]),
                         ("SOL", "long", 1.0, 100.0, 95.0, -5.2, 0.05, 0.06, int(T0 * 1000)))


# ------------------------------------------------------------------ классификация ошибок
class ClassifyTests(unittest.TestCase):
    def kc(self, exc, write=True):
        e = X.to_port_error(exc, write=write)
        return e.kind, e.code

    def test_table(self):
        mk_ = lambda code: bb.BybitError("Bybit retCode=%s: x" % code, code=code)
        self.assertEqual(self.kc(mk_(10001)), ("rejected", 10001))
        zero = bb.BybitError("Bybit retCode=10001: can not set tp/sl/ts for zero position", code=10001)
        self.assertEqual(self.kc(zero), ("position_gone", 10001))
        self.assertEqual(self.kc(mk_(110007)), ("rejected", 110007))
        self.assertEqual(self.kc(mk_(10006)), ("rate", 10006))
        self.assertEqual(self.kc(mk_(10018)), ("rate", 10018))
        self.assertEqual(self.kc(mk_(10016)), ("unknown", 10016))
        self.assertEqual(self.kc(mk_(10016), write=False), ("network", 10016))
        self.assertEqual(self.kc(bb.BybitError("HTTP 429", http=429)), ("rate", "HTTP_429"))
        self.assertEqual(self.kc(bb.BybitError("HTTP 403", http=403)), ("network", "HTTP_403"))
        self.assertEqual(self.kc(bb.BybitError("HTTP 502", http=502)), ("unknown", "HTTP_502"))
        self.assertEqual(self.kc(bb.BybitError("HTTP 502", http=502), write=False), ("network", "HTTP_502"))
        self.assertEqual(self.kc(bb.BybitError("метод не разрешён")), ("rejected", None))

    def test_transport_errors(self):
        self.assertEqual(self.kc(TimeoutError("t"))[0], "unknown")
        self.assertEqual(self.kc(ConnectionResetError("r"))[0], "unknown")
        self.assertEqual(self.kc(ValueError("мусор"))[0], "unknown")
        self.assertEqual(self.kc(TimeoutError("t"), write=False)[0], "network")
        import socket
        import urllib.error
        self.assertEqual(self.kc(urllib.error.URLError(socket.gaierror(8, "nodename")))[0], "network")
        self.assertEqual(self.kc(urllib.error.URLError(ConnectionRefusedError()))[0], "network")
        self.assertEqual(self.kc(urllib.error.URLError(TimeoutError("timed out")))[0], "unknown")

    def test_http_error_carries_code_from_client(self):
        import io
        import urllib.error
        import urllib.request
        orig = urllib.request.urlopen

        def boom(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(bb.BybitError) as cm:
                bb._http("GET", "https://api-testnet.bybit.com/v5/market/time", {}, None)
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(cm.exception.http, 403)

    def test_client_error_has_code_attribute(self):
        c, ex = mk()
        ex.push("/v5/position/set-leverage", err(110026, "x"))
        with self.assertRaises(bb.BybitError) as cm:
            X.set_leverage(c, "SOL", 5)
        self.assertEqual(cm.exception.code, 110026)
        self.assertIn("110026", str(cm.exception))


class SecretHygieneTests(unittest.TestCase):
    def test_no_secret_in_errors(self):
        c, ex = mk()
        ex.push("/v5/order/create", err(10003, "API key is invalid"))
        with self.assertRaises(bb.BybitError) as cm:
            X.place_entry(c, "SOL", "long", 1, "tbx-1-1", "market", sl=95, ins=INS)
        self.assertNotIn("SEC", str(cm.exception).replace("SECTION", ""))
        self.assertNotIn("KEY", str(cm.exception))

    def test_module_has_no_secret_names(self):
        import tb.venues.bybit_exec as m
        with open(m.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("BYBIT_API_SECRET", src)
        self.assertNotIn("BYBIT_API_KEY", src)


if __name__ == "__main__":
    unittest.main()
