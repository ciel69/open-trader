"""FakePort: минимальная память-реализация VenuePort для контрактного теста и ранних тестов других пакетов (06 п. 6.2, 6.12).

Заготовка WP0. Соблюдает инварианты живого порта: вход без стопа запрещён (NO_STOP), стоп только строже (STOP_NOT_TIGHTER), снять
стоп нельзя, стоп на неверной стороне от last отклоняется (STOP_ON_WRONG_SIDE). Журнал вызовов в `calls`; ответы можно заранее
задать через `script(op, exception_or_result)`. WP5 расширяет (fill, trigger_stop, set_price и т. д.).
"""
import time

from tb.executor.ports import (CODE_NO_STOP, CODE_STOP_NOT_TIGHTER, CODE_STOP_ON_WRONG_SIDE, CODE_STOP_REMOVE, ENV_TESTNET,
                               KIND_REJECTED, PortError, VenuePort)


class FakePort(VenuePort):
    PASS = object()

    def __init__(self, env=ENV_TESTNET, equity=40.0, clock=None):
        self.env = env
        self.equity = equity
        self.px = {}            # sym -> (bid, ask, last)
        self.pos = {}           # sym -> dict позиции (как positions())
        self.orders = {}        # link_id -> dict ордера
        self.execs = []
        self.calls = []         # [(op, args)]
        self._script = {}       # op -> результат или исключение (одноразово)
        self.lev = {}
        self.clock = clock or time.time
        self.sink = None            # fn(ev): приватные события (как WS) для Executor.on_private
        self.auto_fill = True       # рыночные ордера исполняются сразу
        self._xid = 0
        self.kl = {}                # (sym, tf) -> [(start,o,h,l,c,v,t)]

    # ---- помощники теста ----
    def script(self, op, result_or_exc):
        """Заранее заданный ответ операции: одноразовый; список = очередь ответов; Exception поднимается, FakePort.PASS = обычное поведение."""
        if isinstance(result_or_exc, list):
            self._script[op] = list(result_or_exc)
        else:
            self._script[op] = [result_or_exc]

    def set_price(self, sym, last, spread=0.0):
        self.px[sym] = (last - spread / 2.0, last + spread / 2.0, last)

    def _call(self, op, *args):
        self.calls.append((op, args))
        q = self._script.get(op)
        if q:
            r = q.pop(0)
            if not q:
                del self._script[op]
            if isinstance(r, Exception):
                raise r
            if r is self.PASS:
                return None
            return r
        return None

    # ---- чтение ----
    def account(self):
        r = self._call("account")
        return r if r is not None else {"equity": self.equity, "free": self.equity, "ts": 0.0}

    def positions(self):
        r = self._call("positions")
        return r if r is not None else [dict(p) for p in self.pos.values()]

    def open_orders(self, sym=None):
        r = self._call("open_orders", sym)
        return r if r is not None else [dict(o) for o in self.orders.values() if sym in (None, o["sym"]) and o["status"] in ("New", "PartiallyFilled")]

    def order_by_link(self, sym, link_id):
        self._call("order_by_link", sym, link_id)
        o = self.orders.get(link_id)
        return dict(o) if o else None

    def executions(self, since_ms, sym=None):
        r = self._call("executions", since_ms, sym)
        return r if r is not None else [e for e in self.execs if e["ts_ms"] >= since_ms and sym in (None, e["sym"])]

    def closed_pnl(self, sym, since_ms):
        r = self._call("closed_pnl", sym, since_ms)
        return r if r is not None else []

    def instrument(self, sym):
        r = self._call("instrument", sym)
        return r if r is not None else {"tick": "0.01", "qty_step": "0.1", "min_qty": "0.1", "min_notional": "5", "max_leverage": 50.0}

    def quote(self, sym):
        self._call("quote", sym)
        return self.px.get(sym, (0.0, 0.0, 0.0))

    def klines(self, sym, tf_min, limit=200):
        r = self._call("klines", sym, tf_min, limit)
        return r if r is not None else list(self.kl.get((sym, tf_min), []))[-limit:]

    # ---- запись ----
    def set_leverage(self, sym, lev):
        self._call("set_leverage", sym, lev)
        self.lev[sym] = lev
        return {"ok": True}

    def _emit(self, ev):
        if self.sink:
            self.sink(ev)

    def place_entry(self, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last"):
        self._call("place_entry", sym, side, qty, link_id, order_type, price, post_only, sl, sl_trigger)
        if not sl or sl <= 0:
            raise PortError(KIND_REJECTED, "вход без стопа запрещён", code=CODE_NO_STOP)
        if link_id in self.orders:
            raise PortError(KIND_REJECTED, "дубль orderLinkId", code=110072)
        _, _, last = self.px.get(sym, (0.0, 0.0, price or 0.0))
        self.orders[link_id] = {"sym": sym, "link_id": link_id, "order_id": "O-" + link_id, "side": "Buy" if side == "long" else "Sell",
                                "type": order_type, "price": price or last, "qty": qty, "filled": 0.0, "reduce_only": False,
                                "status": "New", "ts": self.clock(), "sl": sl}
        if order_type == "market" and self.auto_fill:
            self.fill(link_id, qty, last, sl=sl)
        return {"ok": True, "order_id": "O-" + link_id, "link_id": link_id}

    def fill(self, link_id, qty, px, sl=None, stop_type="", cancel_rest=False):
        """Исполняет ордер (частично или целиком): позиция, исполнение (self.execs), события sink."""
        o = self.orders[link_id]
        qty = min(qty, o["qty"] - o["filled"])
        o["filled"] += qty
        o["status"] = "Filled" if o["filled"] >= o["qty"] - 1e-12 else "PartiallyFilled"
        if cancel_rest and o["status"] != "Filled":
            o["status"] = "Cancelled"
        sym = o["sym"]
        closed = 0.0
        pnl = 0.0
        if o["reduce_only"]:
            p = self.pos.get(sym)
            if p:
                closed = min(qty, p["size"])
                sg = 1 if p["side"] == "long" else -1
                pnl = sg * (px - p["avg"]) * closed
                p["size"] = round(p["size"] - closed, 12)
                if p["size"] <= 1e-12:
                    del self.pos[sym]
                    self._cancel_reduce_only(sym)
        else:
            side = "long" if o["side"] == "Buy" else "short"
            p = self.pos.setdefault(sym, {"sym": sym, "side": side, "size": 0.0, "avg": px, "mark": px, "sl": sl or o.get("sl") or 0.0,
                                          "tp": 0.0, "liq": 0.0, "upnl": 0.0, "lev": float(self.lev.get(sym, 1.0)), "margin": 0.0, "ts": self.clock()})
            total = p["size"] + qty
            p["avg"] = (p["avg"] * p["size"] + px * qty) / total
            p["size"] = round(total, 12)
            p["margin"] = p["size"] * p["avg"] / max(p["lev"], 1.0)
        self._xid += 1
        ex = {"exec_id": "X%d" % self._xid, "sym": sym, "side": o["side"], "price": px, "qty": qty, "fee": round(qty * px * (0.0002 if o["type"] == "limit" else 0.00055), 8),
              "is_maker": o["type"] == "limit", "link_id": link_id if not stop_type else "", "order_id": o["order_id"], "stop_type": stop_type,
              "exec_type": "Trade", "closed_size": closed, "pnl": pnl, "ts_ms": int(self.clock() * 1000)}
        self.execs.append(ex)
        self._emit(dict(ex, kind="execution", leaves=o["qty"] - o["filled"]))
        if sym in self.pos:
            self._emit(dict(self.pos[sym], kind="position", link_id=""))
        else:
            self._emit({"kind": "position", "sym": sym, "side": "", "size": 0.0, "link_id": ""})
        return ex

    def _cancel_reduce_only(self, sym):
        for o in self.orders.values():
            if o["sym"] == sym and o["reduce_only"] and o["status"] in ("New", "PartiallyFilled"):
                o["status"] = "Cancelled"

    def trigger_stop(self, sym, px=None):
        """Срабатывание стопа позиции: закрывающее исполнение с пустым link_id и stop_type=StopLoss; reduce-only лимиты снимаются биржей."""
        p = self.pos[sym]
        px = px if px is not None else p["sl"]
        link = "STOP-%d" % (self._xid + 1)
        self.orders[link] = {"sym": sym, "link_id": "", "order_id": "O-" + link, "side": "Sell" if p["side"] == "long" else "Buy", "type": "market",
                             "price": px, "qty": p["size"], "filled": 0.0, "reduce_only": True, "status": "New", "ts": self.clock()}
        return self.fill(link, p["size"], px, stop_type="StopLoss")

    def match(self, sym):
        """Пересечение цены: резервы reduce-only лимитов и стоп позиции по последней цене."""
        _, _, last = self.px[sym]
        done = []
        p = self.pos.get(sym)
        if p and p["sl"] > 0 and ((p["side"] == "long" and last <= p["sl"]) or (p["side"] == "short" and last >= p["sl"])):
            done.append(self.trigger_stop(sym, p["sl"]))
            return done
        for lk, o in list(self.orders.items()):
            if o["sym"] != sym or o["status"] not in ("New", "PartiallyFilled") or o["type"] != "limit":
                continue
            if (o["side"] == "Buy" and last <= o["price"]) or (o["side"] == "Sell" and last >= o["price"]):
                done.append(self.fill(lk, o["qty"] - o["filled"], o["price"]))
        return done

    def place_reduce_only(self, sym, side, qty, link_id, price=None, post_only=False):
        self._call("place_reduce_only", sym, side, qty, link_id, price, post_only)
        p = self.pos.get(sym)
        if not p:
            raise PortError("position_gone", "позиции нет", code=110017)
        if link_id in self.orders:
            raise PortError(KIND_REJECTED, "дубль orderLinkId", code=110072)
        held = sum(o["qty"] - o["filled"] for o in self.orders.values() if o["sym"] == sym and o["reduce_only"] and o["status"] in ("New", "PartiallyFilled"))
        qty = max(0.0, min(qty, p["size"] - held))      # биржа молча обрезает reduce-only
        _, _, last = self.px.get(sym, (0.0, 0.0, price or 0.0))
        self.orders[link_id] = {"sym": sym, "link_id": link_id, "order_id": "O-" + link_id, "side": side.capitalize(),
                                "type": "limit" if price else "market", "price": price or last, "qty": qty, "filled": 0.0,
                                "reduce_only": True, "status": "New", "ts": self.clock()}
        if price is None and self.auto_fill:
            self.fill(link_id, qty, last)
        return {"ok": True, "order_id": "O-" + link_id, "link_id": link_id, "qty": qty}

    def amend_order(self, sym, link_id, price=None, qty=None):
        self._call("amend_order", sym, link_id, price, qty)
        o = self.orders.get(link_id)
        if not o or o["status"] not in ("New", "PartiallyFilled"):
            raise PortError(KIND_REJECTED, "ордера нет", code=110001)
        if price is not None:
            o["price"] = price
        if qty is not None:
            o["qty"] = qty
        return {"ok": True}

    def cancel_order(self, sym, link_id):
        self._call("cancel_order", sym, link_id)
        o = self.orders.get(link_id)
        if not o or o["status"] not in ("New", "PartiallyFilled"):
            return {"ok": True, "gone": True}
        o["status"] = "Cancelled"
        return {"ok": True}

    def cancel_all(self, sym, reduce_only_only=False):
        """Снимает ТОЛЬКО обычные ордера (стоп позиции не затрагивается: как orderFilter=Order на бирже)."""
        self._call("cancel_all", sym, reduce_only_only)
        for o in self.orders.values():
            if o["sym"] == sym and o["status"] in ("New", "PartiallyFilled") and (o["reduce_only"] or not reduce_only_only):
                o["status"] = "Cancelled"
        return {"ok": True}

    def set_stop(self, sym, side, stop_price, trigger_by="last"):
        self._call("set_stop", sym, side, stop_price, trigger_by)
        if stop_price is None or stop_price <= 0:
            raise PortError(KIND_REJECTED, "снять стоп нельзя", code=CODE_STOP_REMOVE)
        p = self.pos.get(sym)
        if not p:
            raise PortError("position_gone", "позиции нет")
        cur = p["sl"]
        long_ = side == "long"
        if cur > 0 and ((long_ and stop_price < cur) or (not long_ and stop_price > cur)):
            raise PortError(KIND_REJECTED, "стоп не строже текущего", code=CODE_STOP_NOT_TIGHTER)
        _, _, last = self.px.get(sym, (0.0, 0.0, p["avg"]))
        if (long_ and stop_price >= last) or (not long_ and stop_price <= last):
            raise PortError(KIND_REJECTED, "стоп не с той стороны от цены", code=CODE_STOP_ON_WRONG_SIDE)
        unchanged = cur > 0 and stop_price == cur
        p["sl"] = stop_price
        return {"ok": True, "stop": stop_price, "unchanged": unchanged}

    def raw_set_stop(self, sym, stop_price):
        """Как сырой trading-stop биржи: РАЗРЕШАЕТ ослабить стоп (для тестов ловушки монотонности и ручных правок)."""
        self.pos[sym]["sl"] = stop_price

    def close_market(self, sym, side, qty, link_id):
        self._call("close_market", sym, side, qty, link_id)
        p = self.pos.get(sym)
        if not p:
            raise PortError("position_gone", "позиции нет", code=110017)
        q = p["size"] if qty is None else qty
        return self.place_reduce_only(sym, side, q, link_id, price=None)

    def writes(self):
        """Вызовы, меняющие состояние биржи (для проверки observe)."""
        w = ("set_leverage", "place_entry", "place_reduce_only", "amend_order", "cancel_order", "cancel_all", "set_stop", "close_market")
        return [c for c in self.calls if c[0] in w]
