import asyncio
import base64
import hashlib
import os
import sys
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import wsclient as w  # noqa: E402


class Conn(object):
    def __init__(self, reader, writer):
        self.r, self.wr = reader, writer
        self.all_masked = True
        self.index = 0

    async def send(self, text):
        self.wr.write(w.encode_frame(w.OP_TEXT, text.encode(), mask=False))
        await self.wr.drain()

    async def frame(self, op, payload=b"", fin=True):
        self.wr.write(w.encode_frame(op, payload, mask=False, fin=fin))
        await self.wr.drain()

    async def recv(self):
        """Читает клиентский кадр; требует маскировку."""
        try:
            return await w.read_frame(self.r, 1 << 20, expect_masked=True)
        except w.WSProtocolError:
            self.all_masked = False
            raise

    async def recv_text(self):
        while True:
            fin, op, p = await self.recv()
            if op == w.OP_TEXT:
                return p.decode()

    def drop(self):
        self.wr.close()


class MockServer(object):
    def __init__(self, handler, bad_accept=False):
        self.handler, self.bad_accept = handler, bad_accept
        self.conns = []
        self.server = None

    async def start(self):
        import _bootstrap
        _bootstrap.require_localhost()
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = "ws://127.0.0.1:%d/ws" % self.port
        return self

    async def _on(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            key = [l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + w.GUID).encode()).digest()).decode()
            if self.bad_accept:
                acc = "AAAA"
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            await writer.drain()
            c = Conn(reader, writer)
            c.index = len(self.conns)
            self.conns.append(c)
            await self.handler(c)
        except (asyncio.IncompleteReadError, ConnectionError, w.WSError):
            pass
        finally:
            writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def take(client, n, timeout=5.0):
    out = []

    async def go():
        async for m in client.messages():
            out.append(m)
            if len(out) >= n:
                return
    await asyncio.wait_for(go(), timeout)
    return out


class WSTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_echo_masked(self):
        got = []

        async def h(c):
            got.append(await c.recv_text())
            await c.send("world")
            await asyncio.sleep(0.2)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, subscriptions=["hello"])
        msgs = await take(cl, 1)
        await cl.close()
        await srv.stop()
        self.assertEqual(msgs, ["world"])
        self.assertEqual(got, ["hello"])
        self.assertTrue(srv.conns[0].all_masked)

    def test_mask_vector_and_plain_refused(self):
        # RFC 6455 5.7: "Hello" masked with 37 fa 21 3d -> 7f 9f 4d 51 58
        fr = w.encode_frame(w.OP_TEXT, b"Hello", mask=True, key=bytes([0x37, 0xfa, 0x21, 0x3d]))
        self.assertEqual(fr, bytes([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58]))
        with self.assertRaises(ValueError):
            w.WSClient("ws://127.0.0.1:1/")

    async def test_bad_accept_rejected(self):
        async def h(c):
            await asyncio.sleep(0.2)
        srv = await MockServer(h, bad_accept=True).start()
        cl = w.WSClient(srv.url, allow_plain=True)
        with self.assertRaises(w.WSProtocolError):
            await cl.connect()
        await srv.stop()

    async def test_fragmentation_and_ping(self):
        pongs = []

        async def h(c):
            await c.frame(w.OP_TEXT, b"ab", fin=False)
            await c.frame(w.OP_PING, b"pp")
            await c.frame(w.OP_CONT, b"cd", fin=True)
            fin, op, p = await c.recv()
            pongs.append((op, p))
            await asyncio.sleep(0.2)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True)
        msgs = await take(cl, 1)
        await asyncio.sleep(0.1)
        await cl.close()
        await srv.stop()
        self.assertEqual(msgs, ["abcd"])
        self.assertEqual(pongs, [(w.OP_PONG, b"pp")])

    async def test_large_frame_lengths(self):
        big = "x" * 70000

        async def h(c):
            await c.send("y" * 300)
            await c.send(big)
            await asyncio.sleep(0.2)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True)
        msgs = await take(cl, 2)
        await cl.close()
        await srv.stop()
        self.assertEqual([len(m) for m in msgs], [300, 70000])

    async def test_oversize_rejected(self):
        async def h(c):
            await c.send("z" * 2000)
            await asyncio.sleep(0.2)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, max_frame=1000)
        await cl.connect()
        with self.assertRaises(w.WSProtocolError):
            await cl.read_message()
        await cl._drop()
        await srv.stop()

    async def test_reconnect_resubscribe(self):
        subs = []

        async def h(c):
            subs.append((c.index, await c.recv_text()))
            await c.send("m%d" % c.index)
            if c.index == 0:
                await asyncio.sleep(0.05)
                c.drop()
            else:
                await asyncio.sleep(1)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, subscriptions=["SUB"], backoff_base=0.05, backoff_max=0.1)
        msgs = await take(cl, 2)
        await cl.close()
        await srv.stop()
        self.assertEqual(msgs, ["m0", "m1"])
        self.assertEqual(subs, [(0, "SUB"), (1, "SUB")])
        self.assertEqual(cl.reconnects, 1)

    async def test_silence_timeout_reconnects(self):
        async def h(c):
            if c.index == 1:
                await c.send("alive")
            await asyncio.sleep(2)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, silence_timeout=0.2, backoff_base=0.05, backoff_max=0.1)
        msgs = await take(cl, 1)
        await cl.close()
        await srv.stop()
        self.assertEqual(msgs, ["alive"])
        self.assertGreaterEqual(len(srv.conns), 2)

    async def test_server_close_frame_reconnects(self):
        async def h(c):
            if c.index == 0:
                await c.frame(w.OP_CLOSE, b"\x03\xe8")
            else:
                await c.send("again")
                await asyncio.sleep(1)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, backoff_base=0.05, backoff_max=0.1)
        msgs = await take(cl, 1)
        await cl.close()
        await srv.stop()
        self.assertEqual(msgs, ["again"])

    async def test_keepalive_text_sent(self):
        seen = []

        async def h(c):
            seen.append(await c.recv_text())
            await c.send("x")
            await asyncio.sleep(0.3)
        srv = await MockServer(h).start()
        cl = w.WSClient(srv.url, allow_plain=True, keepalive_text="ping", keepalive_interval=0.05)
        await take(cl, 1)
        await cl.close()
        await srv.stop()
        self.assertEqual(seen, ["ping"])

    def test_backoff_bounds(self):
        cl = w.WSClient("ws://127.0.0.1:1/", allow_plain=True, backoff_base=1, backoff_max=10, jitter=0.3)
        for a in range(10):
            d = cl.backoff_delay(a)
            base = min(10, 2 ** a)
            self.assertTrue(0.7 * base - 1e-9 <= d <= 1.3 * base + 1e-9)


if __name__ == "__main__":
    unittest.main()
