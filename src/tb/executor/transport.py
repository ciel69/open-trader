"""HTTP-транспорт исполнителя для venues.bybit.Client (замена _http по параметру transport). Владелец: WP5, доработка WP1B.

Зачем: на testnet примерно 1 запрос из 10-40 «зависает» на ~16 с (WP1A_testnet.md T10, подтверждено в WP5), а новый TLS на каждый запрос стоит
сотни миллисекунд. Поэтому:
  * keep-alive: http.client.HTTPSConnection, по одному соединению на (поток, «полоса», хост); после ошибки соединение выбрасывается и создаётся заново;
    простаивавшее дольше idle_get / idle_post секунд соединение не переиспользуется (сервер мог его закрыть: для записи это означало бы неизвестный исход);
  * три полосы соединений: read (GET), write (прочие POST) и protect (ЗАЩИТНЫЕ записи: trading-stop и reduce-only ордера). Зависшее чтение или обычная запись
    не делят сокет с защитой; вызов из другого потока (страж демона, пул подкачки) получает свои соединения автоматически;
  * чтение (GET): таймаут read_timeout (4 с) и ОДИН повтор на новом соединении; повтор безопасен (чтение, подпись ещё в пределах recv_window 10 с);
  * запись (POST): один запрос с таймаутом write_timeout (9 с) и без повтора: исход неизвестен -> PortError(unknown) -> проверка по orderLinkId.
Транспорт ничего не знает о ключах (заголовки приходят готовыми из Client). Параметр opener (как у urllib.request.urlopen) оставлен для тестов:
с ним работает прежний путь «новое соединение на запрос».
"""
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from tb.venues.bybit import BybitError

READ_TIMEOUT_S = 4.0
WRITE_TIMEOUT_S = 9.0
IDLE_GET_S = 30.0
IDLE_POST_S = 10.0
RETRY_ERRORS = (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError, OSError, http.client.HTTPException)
PROTECT_PATHS = ("/v5/position/trading-stop",)


def is_protective(method, url, body):
    """Защитная запись: подтяжка стопа/тейка позиции и reduce-only ордер (закрытие, kill/flatten, лесенка)."""
    if method != "POST":
        return False
    path = urllib.parse.urlsplit(url).path
    if path in PROTECT_PATHS:
        return True
    return path == "/v5/order/create" and bool(body) and '"reduceOnly":true' in body.replace(" ", "")


def lane_of(method, url, body):
    if method == "GET":
        return "read"
    return "protect" if is_protective(method, url, body) else "write"


def _default_factory(scheme, host, timeout):
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, timeout=timeout)


class KeepAlive(object):
    """Транспорт с постоянными соединениями. Объект вызывается как transport(method, url, headers, body) -> dict ответа биржи."""

    def __init__(self, read_timeout=READ_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S, factory=None, clock=time.monotonic,
                 idle_get=IDLE_GET_S, idle_post=IDLE_POST_S):
        self.read_timeout, self.write_timeout = read_timeout, write_timeout
        self.factory = factory or _default_factory
        self.clock = clock
        self.idle_get, self.idle_post = idle_get, idle_post
        self._tl = threading.local()
        self.stats = {"connects": 0, "reused": 0}      # для диагностики и тестов (по процессу, без блокировки: счётчики приблизительные)

    def _conns(self):
        d = getattr(self._tl, "conns", None)
        if d is None:
            d = self._tl.conns = {}
        return d

    def _drop(self, key):
        ent = self._conns().pop(key, None)
        if ent is not None:
            try:
                ent["conn"].close()
            except Exception:
                pass

    def _once(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        target = u.path + ("?" + u.query if u.query else "")
        key = (u.scheme, u.netloc, lane_of(method, url, body))
        conns = self._conns()
        ent = conns.get(key)
        now = self.clock()
        if ent is not None and now - ent["last"] > (self.idle_get if method == "GET" else self.idle_post):
            self._drop(key)
            ent = None
        if ent is None:
            ent = conns[key] = {"conn": self.factory(u.scheme, u.netloc, timeout), "last": now}
            self.stats["connects"] += 1
        else:
            self.stats["reused"] += 1
        conn = ent["conn"]
        conn.timeout = timeout
        if getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(timeout)
        try:
            conn.request(method, target, body=body.encode() if body else None, headers=headers)
            r = conn.getresponse()
            data = r.read()
        except BaseException:
            self._drop(key)
            raise
        ent["last"] = self.clock()
        if getattr(r, "will_close", False):
            self._drop(key)
        if r.status != 200:
            raise BybitError("HTTP %s" % r.status, http=r.status)
        return json.loads(data.decode())

    def __call__(self, method, url, headers, body):
        attempts = 2 if method == "GET" else 1
        timeout = self.read_timeout if method == "GET" else self.write_timeout
        last = None
        for _ in range(attempts):
            try:
                return self._once(method, url, headers, body, timeout)
            except RETRY_ERRORS as e:
                last = e
        raise last

    def close(self):
        """Закрыть соединения текущего потока."""
        for key in list(self._conns()):
            self._drop(key)


def make_transport(read_timeout=READ_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S, opener=None, factory=None, clock=time.monotonic):
    if opener is None:
        return KeepAlive(read_timeout, write_timeout, factory=factory, clock=clock)
    open_ = opener

    def transport(method, url, headers, body):
        attempts = 2 if method == "GET" else 1
        timeout = read_timeout if method == "GET" else write_timeout
        last = None
        for _ in range(attempts):
            req = urllib.request.Request(url, data=body.encode() if body else None, headers=headers, method=method)
            try:
                with open_(req, timeout=timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                raise BybitError("HTTP %s" % e.code, http=e.code)
            except RETRY_ERRORS as e:
                last = e
        raise last

    return transport
