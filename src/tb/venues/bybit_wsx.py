"""Асинхронный фид Bybit для исполнителя v2: публичный (tickers, kline) и приватный (order, execution, position, wallet) WS (05 WP1). Владелец: WP1A.

Реализует executor.ports.MarketFeed: BybitFeed(client). Основа: marketdata.wsclient.WSClient (переподключение с отступом, прикладной ping Bybit
{"op":"ping"}, таймаут тишины); подпись и сообщение auth, URL и константы берутся из bybit_stream (импорт, не копия).
Ключ только из Client (c.key / c.secret, они пришли из окружения); в логи, события и ошибки ключ, подпись и тело auth не попадают.

Обработчики (присваиваются заранее, как в MarketFeed):
  on_ticker(sym, last, bid, ask, mark, funding, ts)      sym без квоты (SOL); ts = локальное время получения (epoch, с)
  on_kline(sym, tf_min, start_ms, o, h, l, c, vol, turnover, confirm)
  on_private(ev)    ev["kind"] = execution | order | position | wallet; поля порта (см. ниже) + "link_id" (из orderLinkId, "" если нет)
  on_state(channel "public|private", up, down_s)          при смене состояния и каждую секунду, пока канал лежит (down_s растёт)
  on_gap(sym, tf_min, from_ms, to_ms)                     дополнительно: пропуск подтверждённых свечей (надо дозапросить REST), по умолчанию None
Ошибка в обработчике не убивает фид: счётчик handler_errors и last_handler_error (текст без секретов).

Тикеры: snapshot заменяет состояние символа, delta обновляет ТОЛЬКО присутствующие непустые поля (в delta может не быть lastPrice/bid/ask).
Свечи: confirm=true закрытая; повтор уже подтверждённой свечи отбрасывается; пропуск подтверждённой свечи (start дальше ожидаемого) -> on_gap.
Приватные: дубликаты execution по execId отбрасываются (окно 5000; preload_exec_ids для восстановления после рестарта), funding-записи пропущены.
Внимание: биржа присылает в `order` и стоп/тейк уровня позиции как ордера (stop_type StopLoss|TakeProfit, link_id пуст, status Untriggered,
create_type CreateByStopLoss): это не ордера порта, исполнитель различает их по stop_type. При каждом изменении ордера/позиции приходят ещё
`position` и `wallet`. Проверено на testnet: docs/spec_v2/WP1A_testnet.md (темы, snapshot/delta, периодичность, переподключение).
Формат событий:
  execution: exec_id, sym, side(Buy|Sell), price, qty, fee, is_maker, link_id, order_id, stop_type, exec_type, closed_size, pnl, leaves, ts_ms
  order:     sym, link_id, order_id, side, type, price, qty, filled, leaves, reduce_only, status, stop_type, create_type, cancel_type, reject_reason, avg, fee, ts, updated_ms
  position:  sym, side(long|short|""), size, avg, mark, sl, tp, liq, upnl, lev, margin, ts
  wallet:    equity, free, ts
"""
import asyncio
import collections
import json
import time

from tb.executor.ports import KLINE_TFS, MarketFeed
from tb.marketdata.wsclient import WSClient
from tb.venues import bybit_exec as X
from tb.venues import bybit_stream as bs

SUB_CHUNK = 10                  # тем в одном сообщении subscribe
PRIVATE_TOPICS = ["order", "execution", "position", "wallet"]
SEEN_MAX = 5000


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _pos_num(x):
    """Положительное число либо None (пустая строка, мусор, <= 0)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _msg(op, args):
    return json.dumps({"op": op, "args": list(args)}, separators=(",", ":"))


class BybitFeed(MarketFeed):
    """Фид Bybit. Использование: feed = BybitFeed(client); задать on_*; feed.subscribe(["SOL"], [1, 5]); await feed.run() (останавливает stop())."""

    def __init__(self, client, urls=None, opts=None, clock=time.time, private=True):
        self.client = client
        self.urls = urls or bs.WS_URLS[client.env]
        self.opts = dict(opts or {})
        self.clock = clock
        self.want_private = bool(private)
        self.on_ticker = None
        self.on_kline = None
        self.on_private = None
        self.on_state = None
        self.on_gap = None
        self.tfs = {}                 # sym (без квоты) -> set(tf)
        self.tickers = {}             # sym -> слитое состояние тикера (поля Bybit)
        self.confirmed = {}           # (sym, tf) -> start последней подтверждённой свечи
        self.seen = collections.deque()
        self.seen_set = set()
        self.last_exec_ts_ms = 0
        self.handler_errors = 0
        self.last_handler_error = None
        self.sub_errors = collections.deque(maxlen=20)
        self.auth_failed = False
        self.last_msg = {"public": 0.0, "private": 0.0}
        self.priv_ready = False
        self._clients = {}
        self._loop = None
        self._pubq = None
        self._tasks = []
        self._st = {}                 # channel -> {"down_since","up","connects"}
        self._stop_ev = None

    # ------------------------------------------------------------ подписки
    @staticmethod
    def _sym(s):
        return X.base_of(bs.norm_symbol(s))

    def topics(self):
        """Текущий список публичных тем: tickers.<SYM> и kline.<tf>.<SYM>."""
        out = []
        for sym in sorted(self.tfs):
            out.append("tickers.%s" % X.sym_of(sym))
            out += ["kline.%d.%s" % (tf, X.sym_of(sym)) for tf in sorted(self.tfs[sym])]
        return out

    def subscribe(self, symbols, tfs):
        """Динамически (можно вызывать на лету). tfs из {1,5,15,60}; тикер символа подписывается всегда."""
        tfs = [int(t) for t in (tfs or [])]
        bad = [t for t in tfs if t not in KLINE_TFS]
        if bad:
            raise ValueError("таймфреймы %s не поддерживаются (разрешены %s)" % (bad, list(KLINE_TFS)))
        new = []
        for s in symbols or []:
            sym = self._sym(s)
            if sym not in self.tfs:
                self.tfs[sym] = set()
                new.append("tickers.%s" % X.sym_of(sym))
            for tf in tfs:
                if tf not in self.tfs[sym]:
                    self.tfs[sym].add(tf)
                    new.append("kline.%d.%s" % (tf, X.sym_of(sym)))
        self._queue_public("subscribe", new)

    def unsubscribe(self, symbols):
        gone = []
        for s in symbols or []:
            sym = self._sym(s)
            if sym in self.tfs:
                gone.append("tickers.%s" % X.sym_of(sym))
                gone += ["kline.%d.%s" % (tf, X.sym_of(sym)) for tf in sorted(self.tfs.pop(sym))]
            self.tickers.pop(sym, None)
            for k in [k for k in self.confirmed if k[0] == sym]:
                del self.confirmed[k]
        self._queue_public("unsubscribe", gone)

    def _queue_public(self, op, topics):
        if not topics or self._loop is None or self._pubq is None:
            return          # фид ещё не запущен: темы уйдут при подключении (on_connect)
        for ch in _chunks(topics, SUB_CHUNK):
            self._loop.call_soon_threadsafe(self._pubq.put_nowait, _msg(op, ch))

    # ------------------------------------------------------------ публичные сообщения
    def handle_public(self, msg, now=None):
        """Одно разобранное сообщение публичного сокета. Возвращает число выданных событий (для тестов)."""
        now = self.clock() if now is None else now
        if not isinstance(msg, dict):
            return 0
        self.last_msg["public"] = now
        if "op" in msg:
            if msg.get("success") is False and msg.get("op") == "subscribe":
                self.sub_errors.append(str(msg.get("ret_msg"))[:120])
            return 0
        topic, data = msg.get("topic") or "", msg.get("data")
        if topic.startswith("tickers."):
            return self._ticker(topic.split(".", 1)[1], data, msg.get("type"), now)
        if topic.startswith("kline."):
            parts = topic.split(".")
            if len(parts) == 3:
                return self._kline(parts[2], parts[1], data)
        return 0

    def _call(self, fn, *args):
        if fn is None:
            return False
        try:
            fn(*args)
        except Exception as e:      # ошибка обработчика не должна рвать поток
            self.handler_errors += 1
            self.last_handler_error = "%s: %s" % (type(e).__name__, str(e)[:120])
        return True

    def _ticker(self, symbol, data, typ, now):
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return 0
        sym = self._sym(data.get("symbol") or symbol)
        if typ == "snapshot" or sym not in self.tickers:
            st = {}
        else:
            st = self.tickers[sym]
        for k in ("lastPrice", "bid1Price", "ask1Price", "markPrice"):
            v = _pos_num(data.get(k))
            if v is not None:
                st[k] = v
        if data.get("fundingRate") not in (None, ""):
            try:
                st["fundingRate"] = float(data["fundingRate"])
            except (TypeError, ValueError):
                pass
        self.tickers[sym] = st
        last = st.get("lastPrice", 0.0)
        if last <= 0:
            return 0
        return 1 if self._call(self.on_ticker, sym, last, st.get("bid1Price", 0.0), st.get("ask1Price", 0.0), st.get("markPrice", 0.0),
                               st.get("fundingRate", 0.0), now) else 0

    def _kline(self, symbol, tf_s, data):
        if not isinstance(data, list):
            return 0
        sym = self._sym(symbol)
        n = 0
        for it in data:
            if not isinstance(it, dict):
                continue
            try:
                tf = int(it.get("interval") or tf_s)
                start = int(it["start"])
                o, h, l, c = (float(it[k]) for k in ("open", "high", "low", "close"))
            except (KeyError, TypeError, ValueError):
                continue
            vol, turn, conf = _f(it.get("volume")), _f(it.get("turnover")), bool(it.get("confirm"))
            key = (sym, tf)
            if conf:
                last = self.confirmed.get(key)
                if last is not None and start <= last:
                    continue        # повтор уже подтверждённой свечи
                if last is not None and start > last + tf * 60000:
                    self._call(self.on_gap, sym, tf, last + tf * 60000, start - tf * 60000)
                self.confirmed[key] = start
            if self._call(self.on_kline, sym, tf, start, o, h, l, c, vol, turn, conf):
                n += 1
        return n

    # ------------------------------------------------------------ приватные сообщения
    def preload_exec_ids(self, ids, last_ts_ms=0):
        """Восстановить окно виденных execId после рестарта (исполнитель хранит их сам)."""
        for i in ids or []:
            self._remember(i)
        self.last_exec_ts_ms = max(self.last_exec_ts_ms, int(last_ts_ms or 0))

    def _remember(self, eid):
        if eid in self.seen_set:
            return False
        if len(self.seen) >= SEEN_MAX:
            self.seen_set.discard(self.seen.popleft())
        self.seen.append(eid)
        self.seen_set.add(eid)
        return True

    def handle_private(self, msg, now=None):
        """Одно разобранное сообщение приватного сокета (после auth/subscribe). Возвращает число выданных событий."""
        now = self.clock() if now is None else now
        if not isinstance(msg, dict):
            return 0
        self.last_msg["private"] = now
        topic, data = msg.get("topic") or "", msg.get("data")
        if not isinstance(data, list):
            return 0
        n = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            if d.get("category") not in (None, "", "linear") and topic != "wallet":
                continue
            ev = None
            if topic.startswith("execution"):
                ev = self._execution(d)
            elif topic == "order":
                ev = self._order(d)
            elif topic == "position":
                ev = self._position(d, now)
            elif topic == "wallet":
                ev = self._wallet(d, now)
            if ev is not None and self._call(self.on_private, ev):
                n += 1
        return n

    def _execution(self, d):
        ex = X.norm_execution(d)
        if ex["exec_type"] == "Funding":
            return None
        if ex["exec_id"] and not self._remember(ex["exec_id"]):
            return None
        ex["kind"] = "execution"
        ex["leaves"] = _f(d.get("leavesQty"))
        self.last_exec_ts_ms = max(self.last_exec_ts_ms, ex["ts_ms"])
        return ex

    def _order(self, d):
        o = X.norm_order(d)
        o.update({"kind": "order", "leaves": _f(d.get("leavesQty")), "create_type": d.get("createType") or "", "cancel_type": d.get("cancelType") or "",
                  "reject_reason": d.get("rejectReason") or ""})
        return o

    def _position(self, d, now):
        p = X._norm_position(d, clock=lambda: now)
        p["kind"] = "position"
        p["link_id"] = ""
        return p

    def _wallet(self, d, now):
        free = _f(d.get("totalAvailableBalance"))
        return {"kind": "wallet", "equity": _f(d.get("totalEquity")), "free": free, "ts": now, "link_id": ""}

    # ------------------------------------------------------------ состояние каналов
    def state(self, now=None):
        """-> {"public": {"up","age_s","down_s","reconnects"}, "private": {...}|None, "auth_failed": bool}."""
        now = self.clock() if now is None else now
        out = {"auth_failed": self.auth_failed}
        for ch in ("public", "private"):
            cl = self._clients.get(ch)
            if cl is None and ch == "private" and not self.want_private:
                out[ch] = None
                continue
            st = self._st.get(ch) or {}
            up = self._is_up(ch)
            since = st.get("down_since")
            last = self.last_msg[ch]
            out[ch] = {"up": up, "age_s": (now - last) if last else None, "down_s": 0.0 if up or since is None else now - since,
                       "reconnects": cl.reconnects if cl is not None else 0}
        return out

    def _is_up(self, ch):
        cl = self._clients.get(ch)
        if cl is None or not cl.connected:
            return False
        return self.priv_ready if ch == "private" else True

    def _tick_state(self, now):
        """Один шаг монитора связи: уведомления on_state при падении, каждую секунду пока лежит, и при возврате."""
        for ch in ("public", "private"):
            if ch == "private" and not self.want_private:
                continue
            st = self._st.setdefault(ch, {"down_since": now, "up": None, "connects": 0})
            up, cl = self._is_up(ch), self._clients.get(ch)
            connects = cl.connects if cl is not None else 0
            if up:
                if st["up"] is False:
                    down_s = now - (st["down_since"] if st["down_since"] is not None else now)
                    st["down_since"] = None
                    self._call(self.on_state, ch, True, max(0.0, down_s))
                elif st["up"] is True and connects != st["connects"]:
                    # переподключение целиком уложилось между тиками: пропущенные события возможны, сообщаем как падение и возврат
                    self._call(self.on_state, ch, False, 0.0)
                    self._call(self.on_state, ch, True, 0.0)
                st["up"], st["connects"] = True, connects
            else:
                if st["down_since"] is None:
                    st["down_since"] = now
                st["up"] = False
                st["connects"] = connects
                self._call(self.on_state, ch, False, max(0.0, now - st["down_since"]))

    # ------------------------------------------------------------ сетевая часть
    def _client(self, url, kind, on_connect):
        o = self.opts
        return WSClient(url, allow_plain=o.get("allow_plain", False), silence_timeout=o.get("silence_timeout", 45.0), backoff_base=o.get("backoff_base", 1.0),
                        backoff_max=o.get("backoff_max", 30.0), keepalive_text=bs.PING, keepalive_interval=o.get("ping_every", bs.PING_EVERY),
                        on_connect=on_connect, name=kind)

    async def _public_task(self):
        async def on_connect(cl):
            for ch in _chunks(self.topics(), SUB_CHUNK):
                await cl.send(_msg("subscribe", ch))
        cl = self._client(self.urls["public"], "public", on_connect)
        self._clients["public"] = cl
        async for raw in cl.messages():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.handle_public(msg)

    async def _pump_task(self):
        while True:
            m = await self._pubq.get()
            cl = self._clients.get("public")
            if cl is not None and cl.connected:
                try:
                    await cl.send(m)
                except Exception:
                    pass        # соединение упало: темы уйдут заново при переподключении

    async def _private_task(self):
        c, loop = self.client, asyncio.get_event_loop()
        if getattr(c, "offset_ms", None) is None:
            try:
                await loop.run_in_executor(None, c.sync_time)
            except Exception as e:
                self.sub_errors.append("time_sync: %s" % type(e).__name__)

        async def on_connect(cl):
            self.priv_ready = False
            exp = int(self.clock() * 1000) + (c.offset_ms or 0) + bs.AUTH_TTL_MS
            await cl.send(bs.auth_message(c.key, c.secret, exp))
        cl = self._client(self.urls["private"], "private", on_connect)
        self._clients["private"] = cl
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
                    await cl.send(_msg("subscribe", PRIVATE_TOPICS))
                else:
                    self.auth_failed = True
                    self.sub_errors.append("auth: %s" % str(msg.get("ret_msg"))[:80])
                    self.priv_ready = False
                    await cl.close()
                    return
            elif op == "subscribe":
                if msg.get("success"):
                    self.priv_ready = True
                else:
                    self.sub_errors.append("private subscribe: %s" % str(msg.get("ret_msg"))[:80])
            elif op in ("pong", "ping"):
                self.last_msg["private"] = self.clock()
            else:
                self.handle_private(msg)

    async def _monitor_task(self):
        step = float(self.opts.get("state_step", 1.0))
        while True:
            self._tick_state(self.clock())
            await asyncio.sleep(step)

    async def run(self):
        """Работает до stop() или отмены. Публичный сокет всегда; приватный, если want_private и в клиенте есть ключ.
        Исключение публичной задачи пробрасывается наружу (фатальная ошибка); обрывы связи переживает WSClient сам."""
        self._loop = asyncio.get_event_loop()
        self._pubq = asyncio.Queue()
        self._stop_ev = asyncio.Event()
        pub = asyncio.ensure_future(self._public_task())
        self._tasks = [pub, asyncio.ensure_future(self._pump_task()), asyncio.ensure_future(self._monitor_task())]
        if self.want_private:
            if self.client.key and self.client.secret:
                self._tasks.append(asyncio.ensure_future(self._private_task()))
            else:
                self.want_private = False
                self.sub_errors.append("private: нет ключа в клиенте")
        waiter = asyncio.ensure_future(self._stop_ev.wait())
        try:
            await asyncio.wait([waiter, pub], return_when=asyncio.FIRST_COMPLETED)
            if pub.done() and not pub.cancelled() and pub.exception() is not None:
                raise pub.exception()
        finally:
            waiter.cancel()
            await self._shutdown()

    async def stop(self):
        ev = getattr(self, "_stop_ev", None)
        if ev is not None:
            ev.set()
        await self._shutdown()

    async def _shutdown(self):
        for cl in list(self._clients.values()):
            try:
                await cl.close()
            except Exception:
                pass
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
