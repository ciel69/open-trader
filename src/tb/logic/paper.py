"""Чистая модель заполнения бумажных ордеров (04 п. 4.1). Владелец: WP7.

Без часов, файлов и сети: время (секунды, float) и цены приходят аргументами; никакой случайности, одинаковый поток тиков даёт
одинаковый результат. PaperPort (executor/paper_port.py) оборачивает PaperState в VenuePort и кормит его тиками.

Допущения модели (консервативные, без подглядывания в будущее):
- Рыночный ордер исполняется по первому тику с ts >= t_заявки + latency: покупка по ask, продажа по bid плюс проскальзывание slip_bps
  (полуспред уже в bid/ask), комиссия taker.
- Лимит заполняется только когда last ПРОШЁЛ сквозь уровень не менее чем на through_ticks тиков (касание без прохода не заполняет,
  консервативная очередь); цена исполнения = цена лимита, комиссия maker, частичных заполнений нет. Лимит проверяется только на тиках
  строго после заявки (по устаревшему last в момент постановки не заполняется). Пересекающий стакан лимит: post-only отменяется
  (CancelByPostOnly, как на бирже), иначе исполняется как taker, но не хуже своей цены.
- Стоп срабатывает по last (или mark, если так задан): исполнение рынком по цене стопа с проскальзыванием stop_slip_bps против позиции;
  гэп = худшая из (стоп, bid/ask). На одном тике стоп проверяется РАНЬШЕ лимитов (при неопределённости порядка стоп первым), а после
  входа по лимиту стоп проверяется ещё раз на том же тике.
- Закрытие позиции снимает её reduce-only ордера; reduce-only больше остатка обрезается (как на бирже).
- Funding: на метках 00/08/16 UTC (каждые 28800 с от эпохи) последняя известная ставка на номинал по mark, знак по стороне.
- Ликвидация упрощённая: liq = avg * (1 -/+ 1/lev +/- maintenance).
"""
import copy
import math

FUNDING_PERIOD_S = 28800     # 00/08/16 UTC: эпоха начинается в 00:00 UTC, 8 ч = 28800 с

DEFAULTS = {
    "taker_fee": 0.001,      # costs.bybit.fee
    "maker_fee": 0.00036,    # costs.bybit.maker_fee
    "slip_bps": 2.0,         # paper.slip_bps
    "stop_slip_bps": 5.0,    # paper.stop_slip_bps
    "through_ticks": 1,      # paper.fill_through_ticks
    "latency_ms": 300,       # paper.latency_ms
    "tick": 0.01,            # шаг цены по умолчанию (на символ задаётся через PaperState.ticks)
    "maintenance": 0.005,
    "default_lev": 10.0,
}


def _buy(side):
    return str(side).lower() in ("buy", "long")


def floor_step(x, step):
    """Округление вниз до кратного step (без накопления ошибки float)."""
    if not step or step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    return round(n * step, 10)


def fill_market(side, qty, bid, ask, slip_bps, fee_rate):
    """-> {"px","fee"}: рыночное исполнение. side buy/long: по ask вверх на slip_bps; sell/short: по bid вниз. Комиссия = px * qty * fee_rate.

    >>> fill_market("buy", 1.0, 99.0, 100.0, 10, 0.001)["px"]
    100.1
    """
    s = slip_bps / 10000.0
    px = ask * (1.0 + s) if _buy(side) else bid * (1.0 - s)
    return {"px": px, "fee": abs(px * qty * fee_rate)}


def limit_touch(side, price, last, tick, through_ticks):
    """-> bool: лимит заполнен, только если цена ПРОШЛА сквозь уровень не менее чем на through_ticks.
    buy: last <= price - through*tick; sell: last >= price + through*tick. Касание ровно по уровню при through >= 1 не заполняет.

    >>> limit_touch("buy", 100.0, 100.0, 0.01, 1)
    False
    >>> limit_touch("buy", 100.0, 99.99, 0.01, 1)
    True
    """
    need = (through_ticks or 0) * (tick or 0.0)
    eps = 1e-9 * max(1.0, abs(price))
    if _buy(side):
        return last <= price - need + eps if need > 0 else last < price
    return last >= price + need - eps if need > 0 else last > price


def stop_trigger(side, stop, last, bid, ask, slip_bps):
    """-> None | {"px"}: side = сторона ПОЗИЦИИ. long: last <= stop; short: last >= stop. Цена исполнения: long = min(stop, bid) вниз на slip;
    short = max(stop, ask) вверх на slip (гэп через стоп исполняется по худшей из стопа и bid/ask).

    >>> stop_trigger("long", 95.0, 94.0, 93.9, 94.1, 5)["px"] < 93.9
    True
    >>> stop_trigger("long", 95.0, 96.0, 95.9, 96.1, 5) is None
    True
    """
    if not stop or stop <= 0 or not last or last <= 0:
        return None
    s = slip_bps / 10000.0
    if str(side).lower() == "long":
        if last > stop:
            return None
        base = min(stop, bid) if bid and bid > 0 else stop
        return {"px": base * (1.0 - s)}
    if last < stop:
        return None
    base = max(stop, ask) if ask and ask > 0 else stop
    return {"px": base * (1.0 + s)}


def apply_funding(side, notional, rate):
    """-> USDT (знак по стороне): при rate > 0 long платит, short получает. Начисляется при пересечении меток 00/08/16 UTC.

    >>> apply_funding("long", 1000.0, 0.0001)
    -0.1
    """
    v = notional * rate
    return -v if str(side).lower() == "long" else v


def funding_marks_crossed(ts_prev, ts_now):
    """-> число меток 00/08/16 UTC в (ts_prev, ts_now]. Секунды эпохи."""
    if ts_prev is None or ts_now is None or ts_now <= ts_prev:
        return 0
    return int(ts_now // FUNDING_PERIOD_S) - int(ts_prev // FUNDING_PERIOD_S)


def liq_price(side, avg, lev, maintenance):
    """Упрощённая цена ликвидации (изолированная маржа)."""
    if not lev or lev <= 0:
        return 0.0
    if str(side).lower() == "long":
        return max(0.0, avg * (1.0 - 1.0 / lev + maintenance))
    return avg * (1.0 + 1.0 / lev - maintenance)


class PaperState(object):
    """Состояние бумажного счёта: котировки, позиции, ордера, исполнения, закрытые сделки.

    Все методы принимают время аргументом; методы, меняющие мир, возвращают список событий
    [{"kind":"execution"|"order"|"position", ...}] в тех же полях, что у VenuePort (для on_private).
    """

    def __init__(self, equity=0.0, cfg=None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(dict((k, v) for k, v in cfg.items() if v is not None))
        self.equity0 = float(equity)
        self.equity = float(equity)           # совместимость с заготовкой: начальный капитал
        self.cash = float(equity)             # начальный капитал + реализованный результат - комиссии +/- funding
        self.q = {}                           # sym -> {"bid","ask","last","mark","funding","ts"}
        self.pos = {}                         # sym -> позиция
        self.orders = {}                      # link_id -> открытый ордер
        self.history = {}                     # link_id -> завершённый ордер
        self.execs = []
        self.closed = []
        self.lev = {}                         # sym -> плечо
        self.ticks = {}                       # sym -> шаг цены
        self.funding_log = []                 # [{"sym","ts","rate","amount"}]
        self._seq = 0
        self._oseq = 0

    # ---- служебное ----
    def _id(self, pref):
        self._seq += 1
        return "%s-%06d" % (pref, self._seq)

    def tick_size(self, sym):
        return self.ticks.get(sym) or self.cfg["tick"]

    def upnl(self, p):
        q = self.q.get(p["sym"]) or {}
        mark = q.get("mark") or q.get("last") or p["avg"]
        sign = 1.0 if p["side"] == "long" else -1.0
        return sign * (mark - p["avg"]) * p["size"]

    def margin(self, p):
        return p["size"] * p["avg"] / p["lev"] if p["lev"] else 0.0

    def account(self):
        """-> (equity, free): equity = cash + нереализованный результат; free = equity - маржа открытых позиций."""
        eq = self.cash + sum(self.upnl(p) for p in self.pos.values())
        used = sum(self.margin(p) for p in self.pos.values())
        return eq, max(0.0, eq - used)

    def position_view(self, sym, ts=0.0):
        p = self.pos.get(sym)
        if not p:
            return None
        q = self.q.get(sym) or {}
        return {"sym": sym, "side": p["side"], "size": p["size"], "avg": p["avg"], "mark": q.get("mark") or q.get("last") or p["avg"],
                "sl": p["sl"], "tp": 0.0, "liq": liq_price(p["side"], p["avg"], p["lev"], self.cfg["maintenance"]), "upnl": self.upnl(p),
                "lev": p["lev"], "margin": self.margin(p), "ts": ts or q.get("ts", 0.0)}

    @staticmethod
    def order_view(o):
        return {"sym": o["sym"], "link_id": o["link_id"], "order_id": o["order_id"], "side": o["side"], "type": o["type"], "price": o["price"],
                "qty": o["qty"], "filled": o["filled"], "reduce_only": o["reduce_only"], "status": o["status"], "ts": o["ts"],
                "post_only": o.get("post_only", False), "cancel_type": o.get("cancel_type", "")}

    def _ev_order(self, o):
        d = self.order_view(o)
        d["kind"] = "order"
        return d

    def _ev_pos(self, sym, ts):
        v = self.position_view(sym, ts)
        if v is None:
            v = {"sym": sym, "side": "", "size": 0.0, "avg": 0.0, "mark": 0.0, "sl": 0.0, "tp": 0.0, "liq": 0.0, "upnl": 0.0, "lev": 0.0,
                 "margin": 0.0, "ts": ts}
        v["kind"] = "position"
        return v

    # ---- ордера ----
    def submit(self, o, ts):
        """Принимает ордер (dict: sym, link_id, side Buy|Sell, type market|limit, qty, price, reduce_only, post_only, sl, sl_trigger).
        Валидацию (стоп, размер, дубль link_id) делает порт. -> события."""
        o = dict(o)
        self._oseq += 1
        o.update({"order_id": "PO-%06d" % self._oseq, "seq": self._oseq, "ts": ts, "ready_ts": ts + self.cfg["latency_ms"] / 1000.0,
                  "filled": 0.0, "status": "New", "marketable": False})
        o.setdefault("price", 0.0)
        o.setdefault("post_only", False)
        o.setdefault("reduce_only", False)
        self.orders[o["link_id"]] = o
        ev = [self._ev_order(o)]
        if o["type"] == "limit":
            q = self.q.get(o["sym"])
            if q and ((_buy(o["side"]) and o["price"] >= q["ask"]) or (not _buy(o["side"]) and o["price"] <= q["bid"])):
                if o["post_only"]:
                    self._finish(o, "Cancelled", ev, "CancelByPostOnly")
                    return ev
                o["marketable"] = True
        if o["ready_ts"] <= ts and (o["type"] == "market" or o["marketable"]):
            self._execute_taker(o, ts, ev)
        return ev

    def _finish(self, o, status, ev, cancel_type=""):
        o["status"] = status
        o["cancel_type"] = cancel_type
        self.orders.pop(o["link_id"], None)
        self.history[o["link_id"]] = o
        ev.append(self._ev_order(o))

    def cancel(self, link_id, ts=0.0, cancel_type="CancelByUser"):
        """-> (найден, события)."""
        o = self.orders.get(link_id)
        if not o:
            return False, []
        ev = []
        self._finish(o, "Cancelled", ev, cancel_type)
        return True, ev

    def amend(self, link_id, price, qty, ts):
        o = self.orders.get(link_id)
        if not o:
            return None
        if price is not None:
            o["price"] = price
        if qty is not None:
            o["qty"] = qty
        o["ready_ts"] = ts + self.cfg["latency_ms"] / 1000.0     # правка теряет очередь
        o["ts"] = ts
        return [self._ev_order(o)]

    def cancel_all(self, sym, reduce_only_only, ts):
        ev = []
        for o in sorted([o for o in self.orders.values() if o["sym"] == sym and (o["reduce_only"] or not reduce_only_only)], key=lambda x: x["seq"]):
            self._finish(o, "Cancelled", ev, "CancelByUser")
        return ev

    def set_stop(self, sym, price, trigger):
        p = self.pos.get(sym)
        if p:
            p["sl"], p["sl_trigger"] = price, trigger

    # ---- исполнение ----
    def _execute_taker(self, o, ts, ev):
        q = self.q.get(o["sym"])
        if not q:
            return
        f = fill_market(o["side"], o["qty"] - o["filled"], q["bid"], q["ask"], self.cfg["slip_bps"], self.cfg["taker_fee"])
        px = f["px"]
        if o["type"] == "limit" and o["price"] > 0:
            px = min(px, o["price"]) if _buy(o["side"]) else max(px, o["price"])
        fee = abs(px * (o["qty"] - o["filled"]) * self.cfg["taker_fee"])
        self._fill_order(o, px, fee, False, ts, ev)

    def _fill_order(self, o, px, fee, is_maker, ts, ev, stop_type=""):
        sym = o["sym"]
        qty = o["qty"] - o["filled"]
        if o["reduce_only"]:
            p = self.pos.get(sym)
            if not p or (_buy(o["side"]) == (p["side"] == "long")):
                self._finish(o, "Cancelled", ev, "CancelByReduceOnly")
                return
            qty = min(qty, p["size"])
            fee = abs(px * qty * (self.cfg["maker_fee"] if is_maker else self.cfg["taker_fee"]))
        o["filled"] += qty
        self._finish(o, "Filled", ev)          # сначала закрываем ордер, затем исполнение (иначе закрытие позиции отменило бы его второй раз)
        if o["reduce_only"]:
            self._reduce(sym, qty, px, fee, is_maker, ts, ev, o["link_id"], o["order_id"], stop_type, "Trade")
        else:
            self._open(sym, "long" if _buy(o["side"]) else "short", qty, px, fee, is_maker, ts, ev, o)
            if o.get("sl"):
                self._check_stop(sym, ts, ev)

    def _exec(self, sym, side, px, qty, fee, is_maker, link, oid, stop_type, exec_type, closed_size, pnl, ts, ev):
        e = {"exec_id": self._id("PX"), "sym": sym, "side": "Buy" if _buy(side) else "Sell", "price": px, "qty": qty, "fee": fee,
             "is_maker": is_maker, "link_id": link, "order_id": oid, "stop_type": stop_type, "exec_type": exec_type,
             "closed_size": closed_size, "pnl": pnl, "ts_ms": int(round(ts * 1000))}
        self.execs.append(e)
        d = dict(e)
        d["kind"] = "execution"
        ev.append(d)

    def _open(self, sym, side, qty, px, fee, is_maker, ts, ev, o):
        p = self.pos.get(sym)
        self.cash -= fee
        if p:
            tot = p["size"] + qty
            p["avg"] = (p["avg"] * p["size"] + px * qty) / tot
            p["size"] = tot
            p["fees_open"] += fee
            if o.get("sl") and (p["sl"] <= 0 or (o["sl"] > p["sl"] if side == "long" else o["sl"] < p["sl"])):
                p["sl"], p["sl_trigger"] = o["sl"], o.get("sl_trigger", "last")
        else:
            self.pos[sym] = {"sym": sym, "side": side, "size": qty, "avg": px, "sl": o.get("sl") or 0.0, "sl_trigger": o.get("sl_trigger", "last"),
                             "lev": self.lev.get(sym, self.cfg["default_lev"]), "fees_open": fee, "funding": 0.0, "ts_open": ts,
                             "best_px": px, "worst_px": px, "t_best": ts, "t_worst": ts}
        self._exec(sym, side, px, qty, fee, is_maker, o["link_id"], o["order_id"], "", "Trade", 0.0, 0.0, ts, ev)
        ev.append(self._ev_pos(sym, ts))

    def _reduce(self, sym, qty, px, fee, is_maker, ts, ev, link, oid, stop_type, exec_type):
        p = self.pos[sym]
        qty = min(qty, p["size"])
        sign = 1.0 if p["side"] == "long" else -1.0
        gross = sign * (px - p["avg"]) * qty
        frac = qty / p["size"]
        open_fee, fund = p["fees_open"] * frac, p["funding"] * frac
        net = gross - open_fee - fee + fund
        self.cash += gross - fee
        close_side = "sell" if p["side"] == "long" else "buy"
        self._exec(sym, close_side, px, qty, fee, is_maker, link, oid, stop_type, exec_type, qty, net, ts, ev)
        self.closed.append({"sym": sym, "side": p["side"], "qty": qty, "entry": p["avg"], "exit": px, "pnl": net, "open_fee": open_fee,
                            "close_fee": fee, "funding": fund, "ts_ms": int(round(ts * 1000)), "stop_type": stop_type, "exec_type": exec_type,
                            "best_px": p["best_px"], "worst_px": p["worst_px"], "t_best": p["t_best"], "t_worst": p["t_worst"],
                            "ts_open": p["ts_open"]})
        p["size"] -= qty
        p["fees_open"] -= open_fee
        p["funding"] -= fund
        if p["size"] <= 1e-12:
            del self.pos[sym]
            for o in sorted([o for o in self.orders.values() if o["sym"] == sym and o["reduce_only"]], key=lambda x: x["seq"]):
                self._finish(o, "Cancelled", ev, "CancelByReduceOnly")
        ev.append(self._ev_pos(sym, ts))

    def _check_stop(self, sym, ts, ev):
        p, q = self.pos.get(sym), self.q.get(sym)
        if not p or not q or p["sl"] <= 0:
            return False
        ref = q["mark"] if p.get("sl_trigger") == "mark" and q.get("mark") else q["last"]
        hit = stop_trigger(p["side"], p["sl"], ref, q["bid"], q["ask"], self.cfg["stop_slip_bps"])
        if hit:
            px = hit["px"]
            self._reduce(sym, p["size"], px, abs(px * p["size"] * self.cfg["taker_fee"]), False, ts, ev, "", "", "StopLoss", "Trade")
            return True
        return False

    def _check_liq(self, sym, ts, ev):
        p, q = self.pos.get(sym), self.q.get(sym)
        if not p or not q:
            return
        liq = liq_price(p["side"], p["avg"], p["lev"], self.cfg["maintenance"])
        if liq > 0 and ((p["side"] == "long" and q["last"] <= liq) or (p["side"] == "short" and q["last"] >= liq)):
            self._reduce(sym, p["size"], liq, abs(liq * p["size"] * self.cfg["taker_fee"]), False, ts, ev, "", "", "Liquidation", "BustTrade")

    # ---- тик ----
    def on_tick(self, sym, last, bid, ask, mark, funding, ts):
        """Обрабатывает тик (порядок: funding, экстремумы, стоп, ликвидация, рыночные по задержке, лимиты). -> события."""
        ev = []
        prev = self.q.get(sym)
        if not last or last <= 0:
            return ev
        bid = bid if bid and bid > 0 else last
        ask = ask if ask and ask > 0 else last
        rate = funding if isinstance(funding, (int, float)) else (prev or {}).get("funding")
        if prev and ts < prev["ts"]:
            return ev                     # тик из прошлого игнорируется (нет backdating)
        self.q[sym] = {"bid": bid, "ask": ask, "last": last, "mark": mark or last, "funding": rate, "ts": ts}
        p = self.pos.get(sym)
        if p:
            n = funding_marks_crossed(prev["ts"] if prev else None, ts)
            if n and rate:
                amt = apply_funding(p["side"], p["size"] * (mark or last), rate) * n
                p["funding"] += amt
                self.cash += amt
                self.funding_log.append({"sym": sym, "ts": ts, "rate": rate, "amount": amt})
            if last > p["best_px"] if p["side"] == "long" else last < p["best_px"]:
                p["best_px"], p["t_best"] = last, ts
            if last < p["worst_px"] if p["side"] == "long" else last > p["worst_px"]:
                p["worst_px"], p["t_worst"] = last, ts
            if not self._check_stop(sym, ts, ev):
                self._check_liq(sym, ts, ev)
        for o in sorted([o for o in self.orders.values() if o["sym"] == sym], key=lambda x: x["seq"]):
            if o["link_id"] not in self.orders or o["ready_ts"] > ts:
                continue
            if o["type"] == "market" or o["marketable"]:
                self._execute_taker(o, ts, ev)
            elif ts > o["ts"] and limit_touch(o["side"], o["price"], last, self.tick_size(sym), self.cfg["through_ticks"]):
                fee = abs(o["price"] * (o["qty"] - o["filled"]) * self.cfg["maker_fee"])
                self._fill_order(o, o["price"], fee, True, ts, ev)
        return ev

    # ---- сохранение ----
    def snapshot(self):
        """JSON-совместимый слепок (для перезапуска исполнителя)."""
        return copy.deepcopy({"cfg": self.cfg, "equity0": self.equity0, "cash": self.cash, "q": self.q, "pos": self.pos, "orders": self.orders,
                              "history": dict(list(self.history.items())[-500:]), "execs": self.execs[-2000:], "closed": self.closed[-1000:],
                              "lev": self.lev, "ticks": self.ticks, "seq": self._seq, "oseq": self._oseq})

    @classmethod
    def restore(cls, d):
        s = cls(d.get("equity0", 0.0), d.get("cfg"))
        s.cash = d.get("cash", s.cash)
        for k in ("q", "pos", "orders", "history", "lev", "ticks"):
            setattr(s, k, copy.deepcopy(d.get(k) or {}))
        s.execs, s.closed = list(d.get("execs") or []), list(d.get("closed") or [])
        s._seq, s._oseq = d.get("seq", 0), d.get("oseq", 0)
        return s
