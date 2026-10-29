"""Минимальный WebSocket-клиент (RFC 6455) на asyncio, только stdlib.

Возможности: handshake с проверкой Sec-WebSocket-Accept, маскировка клиентских кадров,
text/binary/ping/pong/close, фрагментация, лимит размера, таймаут молчания, автопереподключение
с экспоненциальным backoff + джиттер, повторная отправка подписок после реконнекта.

Интерфейс:
    client = WSClient("wss://host/path", subscriptions=['{"op":"subscribe",...}'])
    async for msg in client.messages():   # str (text) или bytes (binary)
        ...
    await client.send("text")
    await client.close()
ws:// (без TLS) разрешён только при allow_plain=True (для тестов на 127.0.0.1).
Сеть тут не торгует: модуль ничего не знает о биржах.
"""
import asyncio
import base64
import hashlib
import os
import random
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


class WSError(Exception):
    pass


class WSProtocolError(WSError):
    pass


class WSClosed(WSError):
    pass


def apply_mask(data, key):
    n = len(data)
    if n == 0:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def encode_frame(opcode, payload=b"", mask=True, fin=True, key=None):
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        hdr = struct.pack("!BB", b0, mbit | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", b0, mbit | 126, n)
    else:
        hdr = struct.pack("!BBQ", b0, mbit | 127, n)
    if mask:
        key = key or os.urandom(4)
        return hdr + key + apply_mask(payload, key)
    return hdr + payload


async def read_frame(reader, max_size, expect_masked=False):
    """Возвращает (fin, opcode, payload). Бросает WSProtocolError / IncompleteReadError."""
    b0, b1 = await reader.readexactly(2)
    fin = bool(b0 & 0x80)
    if b0 & 0x70:
        raise WSProtocolError("RSV bits set")
    op = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    if masked != expect_masked:
        raise WSProtocolError("unexpected mask bit")
    if op >= 8 and (not fin or n > 125):
        raise WSProtocolError("bad control frame")
    if n > max_size:
        raise WSProtocolError("frame too large: %d" % n)
    key = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(n) if n else b""
    if masked:
        payload = apply_mask(payload, key)
    return fin, op, payload


class WSClient:
    def __init__(self, url, allow_plain=False, max_frame=1 << 20, silence_timeout=30.0,
                 connect_timeout=10.0, backoff_base=1.0, backoff_max=30.0, jitter=0.3,
                 keepalive_text=None, keepalive_interval=0.0, subscriptions=None,
                 extra_headers=None, rng=None, name="ws", on_connect=None):
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("url must be ws:// or wss://")
        if u.scheme == "ws" and not allow_plain:
            raise ValueError("plain ws:// not allowed (allow_plain=False)")
        self.url = url
        self.secure = u.scheme == "wss"
        self.host = u.hostname
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.max_frame = max_frame
        self.silence_timeout = silence_timeout
        self.connect_timeout = connect_timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.jitter = jitter
        self.keepalive_text = keepalive_text
        self.keepalive_interval = keepalive_interval
        self.subscriptions = list(subscriptions or [])
        self.extra_headers = dict(extra_headers or {})
        self.rng = rng or random.Random()
        self.name = name
        self.on_connect = on_connect  # async def hook(client): после каждого (пере)подключения, до подписок (напр. свежая авторизация)
        self.connected = False
        self.connects = 0          # успешных подключений
        self.errors = 0
        self.last_error = None
        self._reader = None
        self._writer = None
        self._wlock = None
        self._stop = None
        self._closed = False

    @property
    def reconnects(self):
        return max(0, self.connects - 1)

    def _lazy(self):
        if self._wlock is None:
            self._wlock = asyncio.Lock()
            self._stop = asyncio.Event()
            if self._closed:
                self._stop.set()

    def backoff_delay(self, attempt):
        d = min(self.backoff_max, self.backoff_base * (2 ** min(attempt, 20)))
        d *= 1.0 + self.jitter * (2.0 * self.rng.random() - 1.0)
        return max(0.0, d)

    # ---- соединение ----
    async def connect(self):
        self._lazy()
        await asyncio.wait_for(self._connect(), self.connect_timeout)
        self.connected = True
        self.connects += 1

    async def _connect(self):
        ctx = ssl.create_default_context() if self.secure else None
        reader, writer = await asyncio.open_connection(
            self.host, self.port, ssl=ctx, server_hostname=self.host if self.secure else None)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            host = self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port)
            lines = ["GET %s HTTP/1.1" % self.path, "Host: %s" % host, "Upgrade: websocket",
                     "Connection: Upgrade", "Sec-WebSocket-Key: %s" % key,
                     "Sec-WebSocket-Version: 13"]
            lines += ["%s: %s" % kv for kv in self.extra_headers.items()]
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            text = head.decode("latin-1").split("\r\n")
            parts = text[0].split(" ", 2)
            if len(parts) < 2 or parts[1] != "101":
                raise WSProtocolError("handshake status: %s" % text[0])
            hdrs = {}
            for ln in text[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            exp = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if hdrs.get("upgrade", "").lower() != "websocket":
                raise WSProtocolError("no Upgrade: websocket")
            if hdrs.get("sec-websocket-accept") != exp:
                raise WSProtocolError("bad Sec-WebSocket-Accept")
        except BaseException:
            writer.close()
            raise
        self._reader, self._writer = reader, writer

    async def _drop(self):
        self.connected = False
        w, self._writer, self._reader = self._writer, None, None
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    # ---- отправка ----
    async def _send_frame(self, opcode, payload=b""):
        if self._writer is None:
            raise WSError("not connected")
        self._lazy()
        async with self._wlock:
            self._writer.write(encode_frame(opcode, payload, mask=True))
            await self._writer.drain()

    async def send(self, text):
        if isinstance(text, str):
            await self._send_frame(OP_TEXT, text.encode("utf-8"))
        else:
            await self._send_frame(OP_BIN, bytes(text))

    async def subscribe(self, text):
        """Запомнить сообщение для повторной отправки после реконнекта и отправить, если онлайн."""
        self.subscriptions.append(text)
        if self.connected:
            await self.send(text)

    async def close(self):
        self._closed = True
        self._lazy()
        self._stop.set()
        if self._writer is not None:
            try:
                await asyncio.wait_for(self._send_frame(OP_CLOSE, struct.pack("!H", 1000)), 1.0)
            except Exception:
                pass
        await self._drop()

    # ---- чтение ----
    async def read_message(self):
        """Читает одно целое сообщение (после сборки фрагментов), обслуживая ping/close."""
        buf = bytearray()
        kind = None
        while True:
            fin, op, payload = await asyncio.wait_for(
                read_frame(self._reader, self.max_frame), self.silence_timeout)
            if op == OP_PING:
                await self._send_frame(OP_PONG, payload)
            elif op == OP_PONG:
                pass
            elif op == OP_CLOSE:
                try:
                    await self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                raise WSClosed("server closed")
            elif op in (OP_TEXT, OP_BIN):
                if kind is not None:
                    raise WSProtocolError("new data frame inside fragmented message")
                kind = op
                buf = bytearray(payload)
                if fin:
                    return self._finish(kind, buf)
            elif op == OP_CONT:
                if kind is None:
                    raise WSProtocolError("continuation without start")
                buf += payload
                if len(buf) > self.max_frame:
                    raise WSProtocolError("message too large")
                if fin:
                    return self._finish(kind, buf)
            else:
                raise WSProtocolError("unknown opcode %d" % op)

    @staticmethod
    def _finish(kind, buf):
        if kind == OP_TEXT:
            try:
                return bytes(buf).decode("utf-8")
            except UnicodeDecodeError:
                raise WSProtocolError("invalid utf-8")
        return bytes(buf)

    async def _keepalive(self):
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                if self.keepalive_text is not None:
                    await self.send(self.keepalive_text)
                else:
                    await self._send_frame(OP_PING, b"k")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # обрыв обнаружит читатель

    async def _sleep(self, delay):
        try:
            await asyncio.wait_for(self._stop.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def messages(self):
        """Бесконечный async-генератор сообщений с автопереподключением. Останавливается close()."""
        self._lazy()
        attempt = 0
        net_errors = (OSError, EOFError, asyncio.TimeoutError, WSError)
        while not self._closed:
            ka = None
            try:
                await self.connect()
                if self.on_connect is not None:
                    await self.on_connect(self)
                for s in self.subscriptions:
                    await self.send(s)
                if self.keepalive_interval and self.keepalive_interval > 0:
                    ka = asyncio.ensure_future(self._keepalive())
                got = False
                while not self._closed:
                    msg = await self.read_message()
                    if not got:
                        got = True
                        attempt = 0
                    yield msg
            except net_errors as e:
                if not self._closed:
                    self.errors += 1
                    self.last_error = "%s: %s" % (type(e).__name__, e)
            finally:
                if ka is not None:
                    ka.cancel()
                await self._drop()
            if self._closed:
                break
            await self._sleep(self.backoff_delay(attempt))
            attempt += 1
