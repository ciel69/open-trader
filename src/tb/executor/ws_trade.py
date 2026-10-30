"""WebSocket Trade API Bybit для записи ордеров (wss://stream.bybit.com/v5/trade, testnet: stream-testnet): постоянное авторизованное соединение вместо REST.

Что переносится на WS (операции Trade API биржи): POST /v5/order/create, /v5/order/amend, /v5/order/cancel (то есть вход, закрытие по рынку, reduce-only ордера лесенки,
правка и отмена лимитов). ВСЁ ОСТАЛЬНОЕ остаётся на REST keep-alive (executor/transport.py): подтяжка стопа позиции (/v5/position/trading-stop), cancel-all,
set-leverage, чтение. Если WS не готов (подключается, не прошла авторизация, оборвался до отправки), запрос уходит на REST как раньше.

Исход неизвестен, как и у REST: если запрос УШЁЛ в сокет, а ответа нет (таймаут, обрыв), поднимается TimeoutError/ConnectionError, а не повтор на REST:
LivePort превращает это в PortError(unknown), и исполнитель сверяет результат по orderLinkId. Слепых повторов записи нет.

Модуль ничего не пишет в файлы и не печатает; ключ используется только для op=auth (подпись HMAC-SHA256(secret, "GET/realtime"+expires)), как у приватного сокета.
Включение: config/exec_limits.json -> ws_trade.enabled (по умолчанию выключено; сначала проверить на testnet: `./tb exec wscheck`).
"""
import asyncio
import concurrent.futures
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

from tb.marketdata.wsclient import WSClient, WSError
from tb.venues import bybit_stream as bs

WS_URLS = {"testnet": "wss://stream-testnet.bybit.com/v5/trade", "mainnet": "wss://stream.bybit.com/v5/trade"}
OPS = {"/v5/order/create": "order.create", "/v5/order/amend": "order.amend", "/v5/order/cancel": "order.cancel"}
HEADER_KEYS = ("X-BAPI-TIMESTAMP", "X-BAPI-RECV-WINDOW")


class WsNotSent(Exception):
    """Запрос в сокет не уходил (нет соединения, нет авторизации): безопасно повторить на REST."""


class WsTrade(object):
    def __init__(self, key, secret, env="mainnet", url=None, allow_plain=False, offset_ms=None, request_timeout=5.0, ping_s=20.0, clock=time.time, name="ws-trade"):
        self.key, self.secret = key, secret
        self.url = url or WS_URLS[env]
        self.allow_plain = allow_plain
        self.offset_ms = offset_ms or (lambda: 0)
        self.request_timeout = request_timeout
        self.ping_s = ping_s
        self.clock = clock
        self.name = name
        self.stats = {"requests": 0, "ok": 0, "not_sent": 0, "timeouts": 0, "reconnects": 0, "auth_failed": 0, "last_ms": None}
        self._pending = {}
        self._plock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._cl = None
        self._thread = None
        self._stop = False
        self.auth_failed = False

    # ---- жизненный цикл
    def start(self):
        if self._thread is not None:
            return self
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(started,), name=self.name, daemon=True)
        self._thread.start()
        started.wait(5.0)
        return self

    def _run(self, started):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        started.set()
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            pass
        finally:
            self._ready.clear()

    async def _on_connect(self, cl):
        """Каждое (пере)подключение: прежние запросы без ответа считаются потерянными, отправляется авторизация."""
        self._ready.clear()
        self._fail_pending(ConnectionError("ws-trade: соединение переустановлено"))
        if cl.connects > 1:
            self.stats["reconnects"] += 1
        exp = int(self.clock() * 1000) + int(self.offset_ms() or 0) + bs.AUTH_TTL_MS
        await cl.send(bs.auth_message(self.key, self.secret, exp))

    async def _main(self):
        self._cl = WSClient(self.url, allow_plain=self.allow_plain, silence_timeout=45.0, keepalive_text=bs.PING, keepalive_interval=self.ping_s,
                            on_connect=self._on_connect, name=self.name)
        async for raw in self._cl.messages():
            self._on_message(raw)

    def _on_message(self, raw):
        try:
            m = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        op = m.get("op")
        if op == "auth":
            ok = m.get("success") is True or m.get("retCode") == 0
            if ok:
                self.auth_failed = False
                self._ready.set()
            else:
                self.auth_failed = True
                self.stats["auth_failed"] += 1
            return
        rid = m.get("reqId")
        if rid:
            with self._plock:
                fut = self._pending.pop(rid, None)
            if fut is not None and not fut.done():
                fut.set_result(m)

    def _fail_pending(self, exc):
        with self._plock:
            items, self._pending = list(self._pending.values()), {}
        for f in items:
            if not f.done():
                f.set_exception(exc)

    def is_ready(self):
        cl = self._cl
        return self._ready.is_set() and cl is not None and cl.connected

    def wait_ready(self, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.is_ready():
                return True
            time.sleep(0.02)
        return self.is_ready()

    def close(self):
        self._stop = True
        loop, cl = self._loop, self._cl
        if loop is not None and cl is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(cl.close(), loop).result(2.0)
            except Exception:
                pass
        self._ready.clear()
        self._fail_pending(ConnectionError("ws-trade: закрыт"))

    # ---- запрос
    def request(self, op, args, headers=None, timeout=None):
        """-> разобранный ответ биржи (dict с retCode/retMsg/data). WsNotSent: не отправлено. TimeoutError/ConnectionError: отправлено, исход неизвестен."""
        if not self.is_ready():
            self.stats["not_sent"] += 1
            raise WsNotSent("ws-trade не готов")
        rid = uuid.uuid4().hex[:20]
        fut = concurrent.futures.Future()
        with self._plock:
            self._pending[rid] = fut
        hdr = dict((k, str(v)) for k, v in (headers or {}).items() if k in HEADER_KEYS)
        hdr.setdefault("X-BAPI-TIMESTAMP", str(int(self.clock() * 1000) + int(self.offset_ms() or 0)))
        msg = json.dumps({"reqId": rid, "header": hdr, "op": op, "args": list(args)}, separators=(",", ":"))
        t0 = time.monotonic()
        self.stats["requests"] += 1
        try:
            asyncio.run_coroutine_threadsafe(self._cl.send(msg), self._loop).result(timeout or self.request_timeout)
        except WSError as e:
            with self._plock:
                self._pending.pop(rid, None)
            if str(e) == "not connected":
                self.stats["not_sent"] += 1
                raise WsNotSent("ws-trade: нет соединения")
            raise ConnectionError("ws-trade: %s" % e)
        except Exception as e:
            with self._plock:
                self._pending.pop(rid, None)
            raise ConnectionError("ws-trade: отправка не подтверждена (%s)" % type(e).__name__)
        try:
            m = fut.result(timeout or self.request_timeout)
        except concurrent.futures.TimeoutError:
            with self._plock:
                self._pending.pop(rid, None)
            self.stats["timeouts"] += 1
            raise TimeoutError("ws-trade: нет ответа %s за %.1f с" % (op, timeout or self.request_timeout))
        self.stats["ok"] += 1
        self.stats["last_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        return m


class WsTradeTransport(object):
    """Транспорт для venues.bybit.Client: ордерные POST через WS, всё остальное через переданный REST-транспорт. Вызывается как transport(method, url, headers, body)."""

    def __init__(self, rest, ws):
        self.rest, self.ws = rest, ws
        self.fallbacks = 0

    def __call__(self, method, url, headers, body):
        op = OPS.get(urlsplit(url).path) if method == "POST" else None
        if op is None or not body or not self.ws.is_ready():
            if op is not None:
                self.fallbacks += 1
            return self.rest(method, url, headers, body)
        try:
            args = [json.loads(body)]
        except ValueError:
            return self.rest(method, url, headers, body)
        try:
            m = self.ws.request(op, args, {k: headers.get(k) for k in HEADER_KEYS if headers.get(k)})
        except WsNotSent:
            self.fallbacks += 1
            return self.rest(method, url, headers, body)
        return {"retCode": m.get("retCode"), "retMsg": m.get("retMsg"), "result": m.get("data") if isinstance(m.get("data"), dict) else {},
                "retExtInfo": m.get("retExtInfo") or {}, "time": m.get("time")}

    def close(self):
        self.ws.close()
        closer = getattr(self.rest, "close", None)
        if closer:
            closer()


def wrap_client(client, cfg, env):
    """Подключить WS Trade к клиенту Bybit по настройкам exec_limits.ws_trade. -> WsTradeTransport или None (выключено / нет ключа)."""
    if not cfg or not cfg.get("enabled") or not getattr(client, "key", None) or not getattr(client, "secret", None):
        return None
    ws = WsTrade(client.key, client.secret, env=env, offset_ms=lambda: getattr(client, "offset_ms", None) or 0,
                 request_timeout=float(cfg.get("request_timeout_s", 5.0)), ping_s=float(cfg.get("ping_s", 20.0))).start()
    tr = WsTradeTransport(client.transport, ws)
    client.transport = tr
    return tr
