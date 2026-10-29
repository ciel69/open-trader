"""PaperPort: бумажный VenuePort поверх logic/paper.py (04 п. 4.1). Владелец: WP7.

Код исполнителя не ветвится по режиму: отличается только порт и журнал (mode=paper). Сети нет: порт кормится тиками из Executor
(`port.on_ticker(sym, last, bid, ask, mark, funding, ts)` с той же сигнатурой, что MarketFeed.on_ticker; `on_kline` аналогично).
События исполнения/ордеров/позиций отдаются в `port.on_private(ev)` (формат MarketFeed.on_private, ev["kind"] execution|order|position),
чтобы исполнитель обрабатывал стоп/тейк бумажной позиции так же, как приватный поток биржи (стоп приходит с пустым link_id и stop_type=StopLoss).

Время: заявки ставятся по `clock()` (по умолчанию time.time; в тестах FakeClock), тики несут своё ts. Задержка `paper.latency_ms`:
рыночный ордер (в т.ч. закрытие) исполняется по первому тику с ts >= t_заявки + latency, поэтому при latency_ms > 0 позиция появляется
ПОСЛЕ тика, а не внутри place_entry (исполнитель ждёт по fill_wait_s и сверяет по order_by_link/positions, как с живым портом).
latency_ms=0 даёт исполнение в самом вызове (удобно для тестов).

Упрощения: частичных заполнений нет; приоритет очереди не моделируется (только правило прохода сквозь уровень); funding по последней известной
ставке; instrument берётся из instrument_fn(sym) (например LivePort.instrument на mainnet только для чтения) или из таблицы по умолчанию.
"""
import json
import os
import time

from tb import paths
from tb.executor.ports import (CODE_NO_STOP, CODE_STOP_NOT_TIGHTER, CODE_STOP_ON_WRONG_SIDE, CODE_STOP_REMOVE, ENV_PAPER, KIND_NETWORK,
                               KIND_POSITION_GONE, KIND_REJECTED, ORDER_TYPES, SIDES, PortError, VenuePort)
from tb.logic import paper as P

DEFAULT_INSTRUMENT = {"tick": "0.01", "qty_step": "0.1", "min_qty": "0.1", "min_notional": "5", "max_leverage": 50.0}
CODE_DUP_LINK = "110072"
CODE_NO_ORDER = "110001"
CODE_MIN_QTY = "MIN_QTY"
CODE_BAD_ARGS = "BAD_ARGS"
CODE_SIDE_MISMATCH = "SIDE_MISMATCH"
CODE_NO_QUOTE = "NO_QUOTE"
CODE_MARGIN = "110007"
KLINE_KEEP = 1000


def _rej(code, msg):
    return PortError(KIND_REJECTED, msg, code=code)


def load_costs(root=None, venue=None):
    """-> dict комиссий и проскальзываний paper из config/risk_profile.json (costs.bybit) и exec_limits (paper.*). Нет файла = умолчания logic.paper."""
    cfg = {}
    try:
        with open(paths.config("risk_profile.json", root=root), encoding="utf-8") as f:
            c = (json.load(f).get("costs") or {}).get(venue or "bybit") or {}
        if isinstance(c.get("fee"), (int, float)):
            cfg["taker_fee"] = float(c["fee"])
        if isinstance(c.get("maker_fee"), (int, float)):
            cfg["maker_fee"] = float(c["maker_fee"])
        if isinstance(c.get("slip"), (int, float)):
            cfg["slip_bps"] = float(c["slip"]) * 10000.0
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from tb.core import exec_cfg
        pc = exec_cfg.load(root).get("paper") or {}
        for src, dst in (("slip_bps", "slip_bps"), ("stop_slip_bps", "stop_slip_bps"), ("fill_through_ticks", "through_ticks"),
                         ("latency_ms", "latency_ms")):
            if isinstance(pc.get(src), (int, float)):
                cfg[dst] = pc[src]
    except Exception:
        pass
    return cfg


class PaperPort(VenuePort):
    env = ENV_PAPER

    def __init__(self, equity=40.0, cfg=None, clock=time.time, instrument_fn=None):
        self.clock = clock
        self.cfg = dict(cfg or {})
        self.state = P.PaperState(equity, self.cfg)
        self.instrument_fn = instrument_fn
        self.instruments = {}
        self.bars = {}                 # (sym, tf) -> [(start_ms,o,h,l,c,vol,turnover)]
        self.on_private = None         # fn(ev)
        self.calls = 0

    @classmethod
    def from_config(cls, equity=40.0, root=None, clock=time.time, instrument_fn=None, venue=None):
        """Комиссии/проскальзывание из config/risk_profile.json (costs.<биржа>) и exec_limits.json (paper.*)."""
        return cls(equity, load_costs(root, venue), clock, instrument_fn)

    # ---- питание тиками (вызывает Executor / тест) ----
    def _emit(self, events):
        if self.on_private:
            for ev in events:
                self.on_private(ev)
        return events

    def on_ticker(self, sym, last, bid, ask, mark=None, funding=None, ts=None):
        """Тот же вид, что MarketFeed.on_ticker. -> события (они же уходят в on_private)."""
        return self._emit(self.state.on_tick(sym, last, bid, ask, mark, funding, self.clock() if ts is None else ts))

    def on_kline(self, sym, tf_min, start_ms, o, h, l, c, vol, turnover, confirm):
        key = (sym, tf_min)
        rows = self.bars.setdefault(key, [])
        row = (start_ms, o, h, l, c, vol, turnover)
        if rows and rows[-1][0] == start_ms:
            rows[-1] = row
        elif not rows or start_ms > rows[-1][0]:
            rows.append(row)
        if len(rows) > KLINE_KEEP:
            del rows[:len(rows) - KLINE_KEEP]

    def seed_klines(self, sym, tf_min, rows):
        self.bars[(sym, tf_min)] = [tuple(r) for r in sorted(rows, key=lambda r: r[0])]

    def set_price(self, sym, last, spread=0.0, ts=None):
        """Помощник тестов/фикстур: тик с bid/ask вокруг last."""
        return self.on_ticker(sym, last, last - spread / 2.0, last + spread / 2.0, last, None, ts)

    def set_instrument(self, sym, ins):
        self.instruments[sym] = dict(DEFAULT_INSTRUMENT, **ins)

    # ---- чтение ----
    def account(self):
        eq, free = self.state.account()
        return {"equity": eq, "free": free, "ts": self.clock()}

    def positions(self):
        return [self.state.position_view(s) for s in sorted(self.state.pos)]

    def open_orders(self, sym=None):
        return [self.state.order_view(o) for o in sorted(self.state.orders.values(), key=lambda o: o["seq"]) if sym in (None, o["sym"])]

    def order_by_link(self, sym, link_id):
        o = self.state.orders.get(link_id) or self.state.history.get(link_id)
        return self.state.order_view(o) if o and o["sym"] == sym else None

    def executions(self, since_ms, sym=None):
        return [dict(e) for e in self.state.execs if e["ts_ms"] >= since_ms and sym in (None, e["sym"])]

    def closed_pnl(self, sym, since_ms):
        return [dict(c) for c in self.state.closed if c["sym"] == sym and c["ts_ms"] >= since_ms]

    def instrument(self, sym):
        if sym in self.instruments:
            return dict(self.instruments[sym])
        if self.instrument_fn:
            try:
                ins = self.instrument_fn(sym)
                if ins:
                    self.instruments[sym] = dict(DEFAULT_INSTRUMENT, **ins)
                    return dict(self.instruments[sym])
            except Exception:
                pass
        return dict(DEFAULT_INSTRUMENT)

    def quote(self, sym):
        q = self.state.q.get(sym)
        if not q:
            raise PortError(KIND_NETWORK, "нет котировки %s (тики не поступали)" % sym, code=CODE_NO_QUOTE)
        return (q["bid"], q["ask"], q["last"])

    def klines(self, sym, tf_min, limit=200):
        return list(self.bars.get((sym, tf_min), []))[-limit:]

    # ---- запись ----
    def set_leverage(self, sym, lev):
        ins = self.instrument(sym)
        if not isinstance(lev, (int, float)) or lev <= 0 or lev > float(ins["max_leverage"]):
            raise _rej(CODE_BAD_ARGS, "плечо %r вне 0..%s" % (lev, ins["max_leverage"]))
        self.state.lev[sym] = float(lev)
        p = self.state.pos.get(sym)
        if p:
            p["lev"] = float(lev)
        return {"ok": True}

    def _prep_qty(self, sym, qty, reduce_only=False):
        ins = self.instrument(sym)
        self.state.ticks[sym] = float(ins["tick"])
        if not isinstance(qty, (int, float)) or qty <= 0:
            raise _rej(CODE_BAD_ARGS, "qty %r" % (qty,))
        q = P.floor_step(float(qty), float(ins["qty_step"]))
        if q < float(ins["min_qty"]) - 1e-12:
            raise _rej(CODE_MIN_QTY, "qty %s меньше минимума %s" % (qty, ins["min_qty"]))
        return q

    def _need_quote(self, sym):
        q = self.state.q.get(sym)
        if not q:
            raise PortError(KIND_NETWORK, "нет котировки %s (тики не поступали)" % sym, code=CODE_NO_QUOTE)
        return q

    def _dup(self, link_id):
        if not link_id or link_id in self.state.orders or link_id in self.state.history:
            raise _rej(CODE_DUP_LINK, "дубль orderLinkId %r" % (link_id,))

    def place_entry(self, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last"):
        if not isinstance(sl, (int, float)) or isinstance(sl, bool) or sl <= 0:
            raise _rej(CODE_NO_STOP, "вход без стопа запрещён")
        if side not in SIDES or order_type not in ORDER_TYPES or sl_trigger not in ("last", "mark"):
            raise _rej(CODE_BAD_ARGS, "side/type/trigger")
        q = self._need_quote(sym)
        qty = self._prep_qty(sym, qty)
        if order_type == "limit" and (not isinstance(price, (int, float)) or price <= 0):
            raise _rej(CODE_BAD_ARGS, "для limit нужна price")
        self._dup(link_id)
        long_ = side == "long"
        p = self.state.pos.get(sym)
        if p and p["side"] != side:
            raise _rej(CODE_SIDE_MISMATCH, "по %s уже открыта позиция %s" % (sym, p["side"]))
        ref = price if order_type == "limit" else (q["ask"] if long_ else q["bid"])
        if (long_ and sl >= ref) or (not long_ and sl <= ref):
            raise _rej(CODE_STOP_ON_WRONG_SIDE, "стоп %s не с той стороны от цены входа %s" % (sl, ref))
        lev = self.state.lev.get(sym, self.state.cfg["default_lev"])
        need = qty * ref / lev + qty * ref * self.state.cfg["taker_fee"]
        if need > self.state.account()[1] + 1e-9:
            raise _rej(CODE_MARGIN, "не хватает маржи: нужно %.4f, свободно %.4f" % (need, self.state.account()[1]))
        o = {"sym": sym, "link_id": link_id, "side": "Buy" if long_ else "Sell", "type": order_type, "qty": qty, "price": price or 0.0,
             "reduce_only": False, "post_only": bool(post_only) and order_type == "limit", "sl": float(sl), "sl_trigger": sl_trigger}
        ev = self._emit(self.state.submit(o, self.clock()))
        return self._order_reply(link_id)

    def place_reduce_only(self, sym, side, qty, link_id, price=None, post_only=False):
        s = str(side).lower()
        if s not in ("buy", "sell"):
            raise _rej(CODE_BAD_ARGS, "side закрытия buy|sell")
        p = self.state.pos.get(sym)
        if not p:
            raise PortError(KIND_POSITION_GONE, "позиции по %s нет" % sym, code="110017")
        if (s == "sell") != (p["side"] == "long"):
            raise _rej(CODE_SIDE_MISMATCH, "сторона закрытия не совпадает с позицией")
        self._need_quote(sym)
        qty = self._prep_qty(sym, qty)
        if price is not None and (not isinstance(price, (int, float)) or price <= 0):
            raise _rej(CODE_BAD_ARGS, "price")
        self._dup(link_id)
        o = {"sym": sym, "link_id": link_id, "side": s.capitalize(), "type": "limit" if price else "market", "qty": min(qty, p["size"]) if not price else qty,
             "price": price or 0.0, "reduce_only": True, "post_only": bool(post_only) and bool(price)}
        self._emit(self.state.submit(o, self.clock()))
        return self._order_reply(link_id)

    def _order_reply(self, link_id):
        o = self.state.orders.get(link_id) or self.state.history.get(link_id) or {}
        return {"ok": True, "order_id": o.get("order_id", ""), "link_id": link_id, "status": o.get("status", ""),
                "cancel_type": o.get("cancel_type", "")}

    def amend_order(self, sym, link_id, price=None, qty=None):
        o = self.state.orders.get(link_id)
        if not o or o["sym"] != sym:
            raise _rej(CODE_NO_ORDER, "ордера нет")
        if price is None and qty is None:
            return {"ok": True, "unchanged": True}
        if (price is not None and price == o["price"]) and (qty is None or qty == o["qty"]):
            return {"ok": True, "unchanged": True}
        if price is not None:
            if price <= 0:
                raise _rej(CODE_BAD_ARGS, "price")
            q = self.state.q.get(sym)
            if q and o["post_only"] and ((o["side"] == "Buy" and price >= q["ask"]) or (o["side"] == "Sell" and price <= q["bid"])):
                raise _rej("POST_ONLY_CROSS", "правка пересекла бы стакан")
        if qty is not None:
            qty = self._prep_qty(sym, qty)
        self._emit(self.state.amend(link_id, price, qty, self.clock()))
        return {"ok": True, "unchanged": False, "link_id": link_id}

    def cancel_order(self, sym, link_id):
        found, ev = self.state.cancel(link_id)
        self._emit(ev)
        return {"ok": True, "gone": not found, "link_id": link_id}

    def cancel_all(self, sym, reduce_only_only=False):
        """Снимает только обычные ордера (стоп позиции остаётся: урок WP1A, cancel-all без orderFilter снимал стоп)."""
        ev = self.state.cancel_all(sym, reduce_only_only, self.clock())
        self._emit(ev)
        return {"ok": True, "cancelled": len([e for e in ev if e.get("kind") == "order"])}

    def set_stop(self, sym, side, stop_price, trigger_by="last"):
        if not isinstance(stop_price, (int, float)) or isinstance(stop_price, bool) or stop_price <= 0:
            raise _rej(CODE_STOP_REMOVE, "снять стоп нельзя")
        p = self.state.pos.get(sym)
        if not p:
            raise PortError(KIND_POSITION_GONE, "позиции по %s нет" % sym, code="110017")
        long_ = side == "long"
        if (p["side"] == "long") != long_:
            raise _rej(CODE_SIDE_MISMATCH, "сторона не совпадает с позицией")
        cur = p["sl"]
        if cur > 0 and ((long_ and stop_price < cur) or (not long_ and stop_price > cur)):
            raise _rej(CODE_STOP_NOT_TIGHTER, "стоп %s не строже текущего %s" % (stop_price, cur))
        q = self.state.q.get(sym) or {}
        ref = q.get("mark") if trigger_by == "mark" and q.get("mark") else q.get("last", p["avg"])
        if (long_ and stop_price >= ref) or (not long_ and stop_price <= ref):
            raise _rej(CODE_STOP_ON_WRONG_SIDE, "стоп %s не с той стороны от цены %s" % (stop_price, ref))
        unchanged = cur > 0 and stop_price == cur
        self.state.set_stop(sym, float(stop_price), trigger_by)
        return {"ok": True, "stop": float(stop_price), "unchanged": unchanged}

    def close_market(self, sym, side, qty, link_id):
        p = self.state.pos.get(sym)
        if not p:
            raise PortError(KIND_POSITION_GONE, "позиции по %s нет" % sym, code="110017")
        return self.place_reduce_only(sym, side, p["size"] if qty is None else qty, link_id, price=None)

    # ---- сохранение между запусками ----
    def save(self, path):
        from tb.core.filelock import atomic_write_json
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        atomic_write_json(path, self.state.snapshot())

    def load(self, path):
        """Восстанавливает состояние счёта из файла (нет файла = без изменений)."""
        try:
            with open(path, encoding="utf-8") as f:
                self.state = P.PaperState.restore(json.load(f))
            return True
        except (OSError, ValueError):
            return False
