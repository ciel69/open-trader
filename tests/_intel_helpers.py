"""Помощники тестов WP13a: фикстуры ответов, фейковый транспорт (без сети), временный корень, защита от сети и от записи в реальный state/."""
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb.intel import fetch as ifetch

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_intel")
NOW = 1793181300.0        # 2026-10-28T09:55:00Z (за 8 ч 5 мин до решения FOMC 18:00Z)
REAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fx(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


ROUTES = [
    ("announcements/index", "bybit_ann.json"), ("market/time", "bybit_time.json"), ("market/tickers", "bybit_tickers.json"),
    ("open-interest", "bybit_oi.json"), ("account-ratio", "bybit_ls.json"), ("alternative.me", "fng.json"), ("deribit", "dvol.json"),
    ("coingecko", "btc_dom.json"), ("coindesk", "rss_coindesk.xml"), ("theblock", "rss_theblock.xml"), ("decrypt.co", "atom_decrypt.xml"),
    ("cointelegraph", "rss_theblock.xml"), ("bls.gov", "bls.ics"), ("federalreserve", "fomc.html"),
]


class FakeNet(object):
    """Транспорт fetch.Fetcher: (url, params, headers, timeout, max_bytes) -> Resp. Значение маршрута: имя фикстуры | bytes | Exception | int (HTTP-код) | callable."""

    def __init__(self, **over):
        self.routes = dict(ROUTES)
        self.over = {}
        self.calls = []
        for k, v in over.items():
            self.over[k.replace("__", ".")] = v

    def set(self, key, val):
        self.over[key] = val

    def __call__(self, url, params, headers, timeout, max_bytes):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        val = None
        for key, v in list(self.over.items()):
            if key in url:
                val = v
                break
        else:
            for key, name in self.routes.items() if isinstance(self.routes, dict) else self.routes:
                if key in url:
                    val = name
                    break
        if callable(val) and not isinstance(val, (str, bytes)):
            val = val(url, params, headers)
        if isinstance(val, Exception):
            raise val
        if isinstance(val, int):
            e = ifetch.FetchError("http", str(val), val)
            raise e
        if isinstance(val, tuple):
            return ifetch.Resp(val[0], val[2] if len(val) > 2 else {}, val[1], url)
        if val is None:
            raise ifetch.FetchError("http", "404", 404)
        body = fx(val) if isinstance(val, str) else val
        return ifetch.Resp(200, {}, body, url)

    def count(self, key):
        return sum(1 for c in self.calls if key in c["url"])


def _no_net(*a, **k):
    raise AssertionError("сетевой вызов в тесте запрещён")


class IntelCase(unittest.TestCase):
    """Временный TB_ROOT, запрет живой сети, проверка, что реальный runtime/intel и state/ не тронуты."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbintel_")
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, {"TB_ROOT": self.root})
        p.start()
        self.addCleanup(p.stop)
        for target in ("socket.create_connection", "urllib.request.OpenerDirector.open", "urllib.request.urlopen"):
            q = mock.patch(target, _no_net)
            q.start()
            self.addCleanup(q.stop)
        self._real = self._snap()
        self.addCleanup(self._check_real)

    def _snap(self):
        out = {}
        for sub in ("runtime/intel", "state/bus"):
            d = os.path.join(REAL_ROOT, *sub.split("/"))
            out[sub] = sorted(os.listdir(d)) if os.path.isdir(d) else None
        return out

    def _check_real(self):
        self.assertEqual(self._snap(), self._real, "тест изменил реальный каталог проекта")

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, rel, text):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p
