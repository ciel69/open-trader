"""События Bybit по WebSocket для агента: демон под инструментом Monitor, каждая строка stdout = одно событие = пробуждение агента.

  ./tb bybit <read|trade|testnet> watch --symbols SOLUSDT [--above 150 --below SOLUSDT=140 --move-pct 1.5] [--private] [--max-seconds 600]

Приватный сокет (wss://.../v5/private, --private): op=auth (подпись HMAC-SHA256(secret, "GET/realtime"+expires)), затем подписка order/execution/position.
  fill / sl_hit / tp_hit / liquidation / adl  исполнение (execution), в т. ч. частичное; sl_hit и tp_hit при срабатывании стопа или тейка
  order_cancelled / order_rejected            отмена или отказ ордера (заполнения идут событиями fill, чтобы не дублировать)
  position_open / position_changed / position_closed   изменение размера, стопа или тейка позиции (обновления одной лишь нереализованной прибыли не печатаются)
Публичный сокет (wss://.../v5/public/linear): тикеры --symbols (без --symbols берутся символы открытых позиций):
  level  цена пересекла уровень --above / --below (каждый уровень один раз; уровень SYM=цена или цена для единственного символа)
  move   цена ушла на --move-pct и больше за --move-window секунд (после срабатывания пауза на окно)
Ждущие лимитные входы (--pending, venues/bybit_limit.py): символы и уровни берутся из state/pending_orders.json, раз в --pending-every секунд заявки обрабатываются
(заполнение -> журнал, ttl/инвалидация -> отмена) и печатаются события:
  pending_filled     {"pending_id","symbol","side","qty","planned_qty","partial","avg","journal_id"[,"warning","remainder_cancelled"]}  лимит заполнен (полностью или частично)
  pending_cancelled  {"pending_id","symbol","side","reason"}   ttl | invalidated_above/below | session_ended | manual | close | статус биржи
  level с полями pending_id и role (limit: цена дошла до лимита; invalidation: цена дошла до уровня отмены)
fill/order_cancelled по ордеру заявки получают pending_id (из orderLinkId); у fill есть leaves и partial (остаток не исполнен).
Служебные: watch_start, watch_end, ws_down / ws_up (нет связи дольше --down-after секунд), auth_failed, error.
Печатаются только короткие JSON-строки; ключ, подпись и тела авторизации не печатаются и не логируются.
Формат сообщений Bybit v5 по документации из памяти; ПРОВЕРЕНО ВЖИВУЮ только то, что перечислено в docs/HARNESS.md.
"""
import asyncio
import collections
import hashlib
import hmac
import json
import sys
import time

from tb.core import pending as pending_core
from tb.marketdata.wsclient import WSClient

WS_URLS = {
    "testnet": {"private": "wss://stream-testnet.bybit.com/v5/private", "public": "wss://stream-testnet.bybit.com/v5/public/linear"},
    "mainnet": {"private": "wss://stream.bybit.com/v5/private", "public": "wss://stream.bybit.com/v5/public/linear"},
}
PING = '{"op":"ping"}'          # у Bybit прикладной ping (кадры ping протокола не используются)
PING_EVERY = 20.0
AUTH_TTL_MS = 10000             # expires = сейчас (по часам сервера) + 10 с
SL_TYPES = {"StopLoss", "PartialStopLoss"}
TP_TYPES = {"TakeProfit", "PartialTakeProfit"}
CANCEL_STATUSES = {"Cancelled": "order_cancelled", "PartiallyFilledCanceled": "order_cancelled",
                   "Deactivated": "order_cancelled", "Rejected": "order_rejected"}


def ws_signature(secret, expires):
    """Подпись op=auth: HMAC-SHA256(secret, "GET/realtime" + expires), hex."""
    return hmac.new(secret.encode(), ("GET/realtime%s" % expires).encode(), hashlib.sha256).hexdigest()


def auth_message(key, secret, expires):
    return json.dumps({"op": "auth", "args": [key, int(expires), ws_signature(secret, int(expires))]}, separators=(",", ":"))


def norm_symbol(s):
    s = s.strip().upper()
    return s if s.endswith(("USDT", "USDC")) else s + "USDT"


def parse_levels(items, symbols):
    """['150', 'SOLUSDT=140'] -> {'SOLUSDT': [150.0, 140.0]}. Голое число допустимо только при одном символе."""
    out = {}
    for it in items or []:
        if "=" in it:
            sym, val = it.split("=", 1)
            sym = norm_symbol(sym)
        else:
            if len(symbols) != 1:
                raise ValueError("уровень %r без символа неоднозначен: напиши SYM=цена" % it)
            sym, val = symbols[0], it
        out.setdefault(sym, []).append(float(val))
    return out


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(x):
    x = round(x, 8)
    return int(x) if x == int(x) and abs(x) < 1e15 else x


# ---------------------------------------------------------------- приватные события
class PrivateParser(object):
    """Сообщения order/execution/position -> события. Хранит размеры позиций и виденные execId (дубли при повторной доставке)."""

    def __init__(self):
        self.sizes = {}      # symbol -> (size, stopLoss, takeProfit)
        self.seen = collections.deque(maxlen=2000)
        self.seen_set = set()

    def handle(self, msg):
        topic = msg.get("topic") or ""
        data = msg.get("data")
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            if topic.startswith("execution"):
                out += self._execution(d)
            elif topic == "order":
                out += self._order(d)
            elif topic == "position":
                out += self._position(d)
        return out

    def _execution(self, d):
        if d.get("category") not in (None, "", "linear") or d.get("execType") in ("Funding",):
            return []
        eid = d.get("execId")
        if eid:
            if eid in self.seen_set:
                return []
            if len(self.seen) == self.seen.maxlen:
                self.seen_set.discard(self.seen[0])
            self.seen.append(eid)
            self.seen_set.add(eid)
        stop, et = d.get("stopOrderType") or "", d.get("execType") or ""
        name = ("sl_hit" if stop in SL_TYPES else "tp_hit" if stop in TP_TYPES else "liquidation" if et == "BustTrade"
                else "adl" if et == "AdlTrade" else "fill")
        ev = {"event": name, "symbol": d.get("symbol"), "side": d.get("side"), "price": _f(d.get("execPrice")),
              "qty": _f(d.get("execQty")), "fee": _f(d.get("execFee")), "order_id": d.get("orderId"), "maker": bool(d.get("isMaker"))}
        pid = pending_core.link_id(d.get("orderLinkId"))
        if pid:   # исполнение лимитного входа из pending_orders.json (в т. ч. частичное)
            ev["pending_id"] = pid
            if d.get("leavesQty") not in (None, ""):
                ev["leaves"] = _f(d.get("leavesQty"))
                ev["partial"] = _f(d.get("leavesQty")) > 0
        if _f(d.get("closedSize")) > 0:
            ev["closed_qty"] = _f(d.get("closedSize"))
        if _f(d.get("execPnl")) != 0:
            ev["pnl"] = _f(d.get("execPnl"))
        return [ev]

    def _order(self, d):
        name = CANCEL_STATUSES.get(d.get("orderStatus"))
        stop = d.get("stopOrderType") or ""
        if not name or (d.get("orderStatus") == "Deactivated" and stop in SL_TYPES | TP_TYPES):
            return []   # привязанные к позиции стоп/тейк гасятся при закрытии: не событие
        ev = {"event": name, "symbol": d.get("symbol"), "side": d.get("side"), "order_id": d.get("orderId"), "status": d.get("orderStatus"),
              "type": d.get("orderType"), "price": _f(d.get("price")), "qty": _f(d.get("qty")), "filled": _f(d.get("cumExecQty"))}
        if pending_core.link_id(d.get("orderLinkId")):
            ev["pending_id"] = d["orderLinkId"]
        why = d.get("rejectReason") if name == "order_rejected" else d.get("cancelType")
        if why and why != "EC_NoError":
            ev["reason"] = why
        return [ev]

    def _position(self, d):
        sym = d.get("symbol")
        size, sl, tp = _f(d.get("size")), _f(d.get("stopLoss")), _f(d.get("takeProfit"))
        prev = self.sizes.get(sym)
        self.sizes[sym] = (size, sl, tp)
        base = {"symbol": sym, "side": d.get("side") or None, "size": size, "entry": _f(d.get("entryPrice") or d.get("avgPrice")),
                "sl": sl, "tp": tp, "liq": _f(d.get("liqPrice"))}
        if size > 0 and (prev is None or prev[0] == 0):
            return [dict(base, event="position_open")]
        if size == 0 and prev is not None and prev[0] > 0:   # пустое обновление без известной позиции (напр. после смены плеча) не событие
            return [{"event": "position_closed", "symbol": sym}]
        if size > 0 and prev is not None and prev != (size, sl, tp):
            return [dict(base, event="position_changed", prev_size=prev[0])]
        return []

    def sync(self, rows):
        """Сверка с REST (старт и после переподключения): позиции, изменившиеся пока сокета не было, дают те же события."""
        out, now_open = [], {}
        for r in rows or []:
            if _f(r.get("size")) > 0:
                now_open[r.get("symbol")] = r
        for sym, r in now_open.items():
            out += [dict(e, source="rest") for e in self._position(r)]
        for sym, st in list(self.sizes.items()):
            if sym not in now_open and st[0] > 0:
                out += [dict(e, source="rest") for e in self._position({"symbol": sym, "size": "0"})]
        return out


# ---------------------------------------------------------------- публичные события
class PriceWatch(object):
    """Уровни (каждый один раз) и скорость движения цены по тикерам."""

    def __init__(self, above=None, below=None, move_pct=0.0, window=60.0):
        self.above, self.below = above or {}, below or {}
        self.move_pct, self.window = float(move_pct or 0), float(window)
        self.fired = set()
        self.tags = {}            # (kind, sym, level) -> дополнительные поля события (pending_id, role)
        self.last = {}
        self.hist = collections.defaultdict(collections.deque)
        self.cool = {}

    def handle(self, msg, now):
        topic, data = msg.get("topic") or "", msg.get("data")
        if not topic.startswith("tickers.") or not isinstance(data, dict):
            return []
        sym = data.get("symbol") or topic.split(".", 1)[1]
        px = _f(data.get("lastPrice"))
        return self.on_price(sym, px, now) if px > 0 else []

    def on_price(self, sym, px, now):
        out, first = [], sym not in self.last
        self.last[sym] = px
        for kind, levels, hit in (("above", self.above, lambda lv: px >= lv), ("below", self.below, lambda lv: px <= lv)):
            for lv in levels.get(sym, []):
                key = (kind, sym, lv)
                if key not in self.fired and hit(lv):
                    self.fired.add(key)
                    ev = {"event": "level", "symbol": sym, "dir": kind, "level": lv, "price": px}
                    ev.update(self.tags.get(key, {}))
                    if first:
                        ev["already"] = True   # первая же цена уже за уровнем
                    out.append(ev)
        if self.move_pct > 0:
            h = self.hist[sym]
            h.append((now, px))
            while h and now - h[0][0] > self.window:
                h.popleft()
            if now >= self.cool.get(sym, 0) and len(h) >= 2:
                lo, hi = min(p for _, p in h), max(p for _, p in h)
                up, dn = (px - lo) / lo * 100, (px - hi) / hi * 100   # dn <= 0
                pct, ref = (up, lo) if up >= -dn else (dn, hi)
                if abs(pct) >= self.move_pct:
                    out.append({"event": "move", "symbol": sym, "pct": round(pct, 3), "from": ref, "price": px, "window_s": self.window})
                    h.clear()
                    h.append((now, px))
                    self.cool[sym] = now + self.window
        return out

    def symbols_with_levels(self):
        return set(self.above) | set(self.below)


class DownMonitor(object):
    """ws_down, когда сокет без связи дольше after секунд (один раз); ws_up после возвращения."""

    def __init__(self, name, after):
        self.name, self.after = name, float(after)
        self.since = None
        self.announced = False

    def update(self, connected, now):
        if connected:
            self.since = None
            if self.announced:
                self.announced = False
                return {"event": "ws_up", "channel": self.name}
            return None
        if self.since is None:
            self.since = now
        elif not self.announced and now - self.since >= self.after:
            self.announced = True
            return {"event": "ws_down", "channel": self.name, "down_s": int(now - self.since)}
        return None


# ---------------------------------------------------------------- исполнение
class Emitter(object):
    def __init__(self, env, out=None):
        self.env, self.out = env, out or sys.stdout

    def __call__(self, ev):
        ev = dict(ev, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), env=self.env)
        self.out.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.out.flush()


def _make_client(url, kind, opts, on_connect=None, subs=None):
    return WSClient(url, allow_plain=opts.get("allow_plain", False), silence_timeout=opts.get("silence_timeout", 45.0),
                    backoff_base=opts.get("backoff_base", 1.0), backoff_max=opts.get("backoff_max", 30.0),
                    keepalive_text=PING, keepalive_interval=opts.get("ping_every", PING_EVERY),
                    subscriptions=subs, on_connect=on_connect, name=kind)


class AuthFailed(Exception):
    pass


async def private_task(c, url, emit, clients, opts, sync_rows=None):
    """Авторизация при каждом подключении (свежий expires), подписка после успешного ответа auth."""
    parser, loop = PrivateParser(), asyncio.get_event_loop()

    async def on_connect(cl):
        exp = int(time.time() * 1000) + (c.offset_ms or 0) + AUTH_TTL_MS
        await cl.send(auth_message(c.key, c.secret, exp))

    async def resync():
        if sync_rows is None:
            return
        try:
            rows = await loop.run_in_executor(None, sync_rows)
        except Exception as e:
            emit({"event": "error", "where": "position_sync", "msg": str(e)[:120]})
            return
        for ev in parser.sync(rows):
            emit(ev)

    cl = _make_client(url, "private", opts, on_connect)
    clients["private"] = cl
    async for raw in cl.messages():
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        op = msg.get("op")
        if op == "auth":
            if msg.get("success"):
                await cl.send(json.dumps({"op": "subscribe", "args": ["order", "execution", "position"]}, separators=(",", ":")))
                await resync()
            else:
                emit({"event": "auth_failed", "msg": str(msg.get("ret_msg"))[:120]})
                await cl.close()
                raise AuthFailed()
        elif op == "subscribe":
            if not msg.get("success"):
                emit({"event": "error", "where": "subscribe", "msg": str(msg.get("ret_msg"))[:120]})
        elif op in ("pong", "ping"):
            continue
        else:
            for ev in parser.handle(msg):
                emit(ev)


async def public_task(url, symbols, watch, emit, clients, opts, hook=None):
    subs = [json.dumps({"op": "subscribe", "args": ["tickers.%s" % s for s in symbols]}, separators=(",", ":"))]
    cl = _make_client(url, "public", opts, subs=subs)
    clients["public"] = cl
    async for raw in cl.messages():
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("op") == "subscribe" and not msg.get("success"):
            emit({"event": "error", "where": "subscribe_public", "msg": str(msg.get("ret_msg"))[:120]})
        elif isinstance(msg, dict):
            for ev in watch.handle(msg, time.time()):
                emit(ev)
                if hook:
                    hook(ev)


async def down_task(clients, mons, emit, step=1.0, clock=time.time):
    while True:
        await asyncio.sleep(step)
        for name, cl in list(clients.items()):
            ev = mons[name].update(cl.connected, clock())
            if ev:
                emit(ev)


def pending_events(res):
    """Итог bybit_limit.sync_pending -> события агенту."""
    out = []
    if not isinstance(res, dict):
        return out
    if res.get("skipped"):
        return out
    for r in res.get("filled", []):
        ev = {"event": "pending_filled", "pending_id": r["id"], "symbol": r["sym"] + "USDT", "side": r["side"], "qty": r.get("qty"), "planned_qty": r.get("planned_qty"),
              "partial": r.get("partial"), "avg": r.get("avg"), "journal_id": r.get("journal_id")}
        for k in ("warning", "remainder_cancelled"):
            if r.get(k):
                ev[k] = r[k]
        out.append(ev)
    for r in res.get("cancelled", []):
        out.append({"event": "pending_cancelled", "pending_id": r["id"], "symbol": r["sym"] + "USDT", "side": r["side"], "reason": r.get("reason")})
    for r in res.get("errors", []):
        out.append({"event": "error", "where": "pending", "pending_id": r.get("id"), "msg": str(r.get("error"))[:160]})
    return out


async def pending_task(c, emit, tick, every, touched=None):
    """Раз в every секунд обрабатывает ждущие лимиты (в потоке: REST синхронный) и печатает события.
    touched: очередь (id заявки, причина) от публичного сокета: цена коснулась уровня инвалидации -> отмена сразу, не дожидаясь REST-цены (прокол мог быть коротким)."""
    loop = asyncio.get_event_loop()
    while True:
        force, ident = None, None
        try:
            if touched is not None:
                try:
                    ident, force = await asyncio.wait_for(touched.get(), timeout=every)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(every)
            res = await loop.run_in_executor(None, (lambda: tick(force=force, ident=ident)) if force else tick)
            for ev in pending_events(res):
                emit(ev)
        except Exception as e:
            emit({"event": "error", "where": "pending", "msg": "%s: %s" % (type(e).__name__, str(e)[:120])})
            await asyncio.sleep(1.0)


def pending_levels(env):
    """-> (above, below, tags, символы): лимиты и уровни инвалидации ждущих заявок площадки."""
    above, below, tags, syms = {}, {}, {}, set()
    for r in pending_core.orders(env=env):
        sym = r["sym"] + "USDT"
        syms.add(sym)
        lim = (below if r["side"] == "long" else above)       # лонг ждёт откат вниз до цены, шорт ждёт подъём
        if r["price"] is not None:
            lim.setdefault(sym, []).append(r["price"])
            tags[("below" if r["side"] == "long" else "above", sym, r["price"])] = {"pending_id": r["id"], "role": "limit"}
        if r.get("cancel_above") is not None:
            above.setdefault(sym, []).append(r["cancel_above"])
            tags[("above", sym, r["cancel_above"])] = {"pending_id": r["id"], "role": "invalidation"}
        if r.get("cancel_below") is not None:
            below.setdefault(sym, []).append(r["cancel_below"])
            tags[("below", sym, r["cancel_below"])] = {"pending_id": r["id"], "role": "invalidation"}
    return above, below, tags, syms


async def run_watch(c, a, emit, urls=None, opts=None, sync_rows=None, pending_tick=None):
    """Основной цикл. Возвращает код выхода: 0 штатно (--max-seconds), 3 при auth_failed."""
    opts, urls = opts or {}, urls or WS_URLS[c.env]
    symbols = [norm_symbol(s) for s in (a.symbols or "").split(",") if s.strip()]
    clients, tasks, mons = {}, [], {}
    watch = PriceWatch(parse_levels(a.above, symbols), parse_levels(a.below, symbols), a.move_pct, a.move_window)
    use_pending, explicit = bool(getattr(a, "pending", False)), bool(symbols)
    touched = asyncio.Queue() if use_pending else None
    if use_pending:
        p_above, p_below, p_tags, p_syms = pending_levels(c.env)
        for src, dst in ((p_above, watch.above), (p_below, watch.below)):
            for sym, lvs in src.items():
                dst.setdefault(sym, []).extend(lv for lv in lvs if lv not in dst.get(sym, []))
        watch.tags.update(p_tags)
        symbols = sorted(set(symbols) | p_syms)
    if a.private:
        if not c.key or not c.secret:
            emit({"event": "error", "where": "start", "msg": "нет BYBIT_API_KEY / BYBIT_API_SECRET в окружении"})
            return 2
        try:
            c.sync_time()
        except Exception as e:
            emit({"event": "error", "where": "time_sync", "msg": str(e)[:120]})
        if not explicit and sync_rows is not None:
            try:
                symbols = sorted(set(symbols) | {r.get("symbol") for r in sync_rows() if _f(r.get("size")) > 0 and r.get("symbol")})
            except Exception as e:
                emit({"event": "error", "where": "positions", "msg": str(e)[:120]})
    public = bool(symbols) and (a.move_pct > 0 or watch.symbols_with_levels())
    if not a.private and not public and not use_pending:
        emit({"event": "error", "where": "start", "msg": "нечего слушать: нужен --private, --pending или --symbols с --above/--below/--move-pct"})
        return 2
    emit({"event": "watch_start", "private": bool(a.private), "symbols": symbols if public else [], "above": watch.above, "below": watch.below,
          "move_pct": a.move_pct, "max_seconds": a.max_seconds})
    if a.private:
        mons["private"] = DownMonitor("private", a.down_after)
        tasks.append(asyncio.ensure_future(private_task(c, urls["private"], emit, clients, opts, sync_rows)))
    if public:
        mons["public"] = DownMonitor("public", a.down_after)
        hook = None
        if use_pending and pending_tick is not None:
            def hook(ev):     # касание уровня инвалидации -> немедленная отмена заявки
                if ev.get("event") == "level" and ev.get("role") == "invalidation":
                    touched.put_nowait((ev["pending_id"], "invalidated_%s %s (касание по WebSocket, цена %s)" % (ev["dir"], ev["level"], ev["price"])))
        tasks.append(asyncio.ensure_future(public_task(urls["public"], symbols, watch, emit, clients, opts, hook)))
    if use_pending and pending_tick is not None:
        tasks.append(asyncio.ensure_future(pending_task(c, emit, pending_tick, float(getattr(a, "pending_every", 30.0)), touched)))
    # монитор связи стартует, когда клиенты уже созданы; до этого clients пуст
    tasks.append(asyncio.ensure_future(down_task(clients, mons, emit, opts.get("down_step", 1.0))))
    code = 0
    try:
        done, _ = await asyncio.wait(tasks, timeout=a.max_seconds or None, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if isinstance(exc, AuthFailed):
                code = 3
            elif exc is not None:
                emit({"event": "error", "where": "task", "msg": "%s: %s" % (type(exc).__name__, exc)})
                code = 1
        if not done:
            emit({"event": "watch_end", "reason": "max_seconds"})
    finally:
        for t in tasks:
            t.cancel()
        for cl in clients.values():
            try:
                await cl.close()
            except Exception:
                pass
        await asyncio.gather(*tasks, return_exceptions=True)
    return code


def main_watch(c, a):
    """Вызывается из venues/bybit.py main (команда watch)."""
    from tb.venues import bybit
    emit = Emitter(c.env)

    def rows():
        return c.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"}).get("list", [])
    try:
        tick = None
        if getattr(a, "pending", False):
            from tb.venues import bybit_limit
            tick = lambda force=None, ident=None: bybit_limit.sync_pending(c, force=force, ident=ident)
        return asyncio.run(run_watch(c, a, emit, sync_rows=rows if a.private else None, pending_tick=tick))
    except KeyboardInterrupt:
        return 0
    except (ValueError, bybit.BybitError) as e:
        emit({"event": "error", "where": "start", "msg": str(e)[:160]})
        return 2
    except BrokenPipeError:
        return 0
