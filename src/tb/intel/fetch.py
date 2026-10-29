"""Загрузка источников: HTTP с лимитами, кэш, ETag, backoff, вежливость к доменам (07 п. 3.1). Владелец: WP13.

Единственная сетевая функция проекта для intel: `http_get_text` (тесты и разработка подменяют транспорт, живых запросов не делают).
Ограничения: тело <= 1 МБ, `User-Agent: traider-bot/2`, без cookies, `Accept-Encoding: identity`, редиректы только на тот же домен (www. равнозначен),
таймаут на запрос, без системных прокси (как сканер). Ошибка источника НИКОГДА не исключение наружу: `Fetcher.fetch` возвращает Outcome(ok=False, error=КОД).
Кэш `runtime/intel/cache/<id>.json`: {"id","fetched","ok","fails","next_try","etag","last_modified","last_error","data","hist"}; текст третьих лиц в кэш не пишется.
Лимиты вежливости: `min_gap_s` на домен (иначе запрос откладывается до следующего цикла: Outcome.deferred), общий бюджет `max_per_hour` запросов в час.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from tb import paths
from tb.core import filelock
from tb.intel import sources as src_mod

MAX_BYTES = 1024 * 1024
UA = src_mod.UA
RE_SID = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class FetchError(Exception):
    """kind: timeout | net | http | redirect | toobig | budget. Сообщение без текста ответа."""

    def __init__(self, kind, msg="", status=None):
        Exception.__init__(self, "%s%s" % (kind, (":" + msg) if msg else ""))
        self.kind, self.status = kind, status


class Resp(object):
    def __init__(self, status, headers, body, url=""):
        self.status, self.headers, self.body, self.url = status, headers, body, url


def _host(url):
    h = urllib.parse.urlparse(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


class _SameDomainRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def http_error_308(self, req, fp, code, msg, headers):
        # Python 3.9 не знает постоянный редирект 308 (появился в 3.11): для GET это то же, что 302
        return self.http_error_302(req, fp, 302, msg, headers)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme not in ("http", "https") or (old.scheme == "https" and new.scheme != "https") or _host(newurl) != _host(req.full_url):
            raise FetchError("redirect", "cross_domain")
        return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


def http_get_text(url, params=None, headers=None, timeout=10.0, max_bytes=MAX_BYTES, proxy=False):
    """GET -> Resp(status, headers{lower}, body bytes, url). 304 возвращается как Resp. Иные ошибки -> FetchError."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    if not re.match(r"^https?://", url):
        raise FetchError("net", "scheme")
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    h.update(headers or {})
    handlers = [_SameDomainRedirect()]
    if not proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)        # без HTTPCookieProcessor: cookies не хранятся
    req = urllib.request.Request(url, headers=h)
    try:
        with opener.open(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise FetchError("toobig", cl)
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError("toobig", ">%d" % max_bytes)
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.geturl())
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Resp(304, {k.lower(): v for k, v in (e.headers or {}).items()}, b"", url)
        ra = None
        try:
            ra = float(e.headers.get("Retry-After")) if e.headers and e.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            ra = None
        err = FetchError("http", str(e.code), e.code)
        err.retry_after = ra
        raise err
    except (socket.timeout, TimeoutError):
        raise FetchError("timeout")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise FetchError("timeout")
        raise FetchError("net", type(getattr(e, "reason", e)).__name__)
    except (OSError, ValueError) as e:
        raise FetchError("net", type(e).__name__)


class Outcome(object):
    def __init__(self, sid, ok=False, deferred=False, error=None, data=None, items=None, status=None, ms=None, nbytes=0, fresh=True):
        self.sid, self.ok, self.deferred, self.error = sid, ok, deferred, error
        self.data, self.items, self.status, self.ms, self.nbytes, self.fresh = data or {}, items or [], status, ms, nbytes, fresh


def cache_dir(root=None):
    return paths.runtime("intel", "cache", root=root)


def cache_path(sid, root=None):
    if not RE_SID.match(sid):
        raise ValueError("bad source id")
    return os.path.join(cache_dir(root), sid + ".json")


def read_cache(sid, root=None):
    try:
        with open(cache_path(sid, root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cache(sid, doc, root=None):
    filelock.atomic_write_json(cache_path(sid, root), doc, indent=None)


class Fetcher(object):
    def __init__(self, root, cfg, transport=None, clock=time.time):
        self.root, self.cfg, self.clock = root, cfg, clock
        self.proxy = bool(cfg.get("proxy", False))
        self.transport = transport or (lambda url, params, headers, timeout, max_bytes: http_get_text(url, params, headers, timeout, max_bytes, proxy=self.proxy))
        self._lock = threading.Lock()
        self._last = {}                        # домен -> время последнего запроса
        self._hour = []                        # времена запросов за час
        self.min_gap = float(cfg.get("min_gap_s", 2.0))
        self.max_per_hour = int(cfg.get("max_per_hour", 280))
        self.backoff_base = float(cfg.get("backoff_base_s", 60.0))
        self.backoff_max = float(cfg.get("max_backoff_s", 3600.0))

    # ---- расписание ----
    def due(self, src, now, cache=None):
        c = cache if cache is not None else read_cache(src["id"], self.root)
        if now < float(c.get("next_try") or 0):
            return False
        last = c.get("tried") or c.get("fetched") or 0
        return now - float(last) >= float(src["interval_s"])

    def _admit(self, src, now):
        """Вежливость: пауза на домен и общий бюджет. -> None или код отказа (запрос откладывается)."""
        with self._lock:
            dom = src.get("domain") or ""
            if now - self._last.get(dom, -1e18) < self.min_gap:
                return "gap"
            self._hour = [t for t in self._hour if now - t < 3600]
            if len(self._hour) >= self.max_per_hour:
                return "budget"
            self._last[dom] = now
            self._hour.append(now)
        return None

    def requests_last_hour(self, now):
        with self._lock:
            return len([t for t in self._hour if now - t < 3600])

    def backoff_s(self, fails, retry_after=None):
        b = min(self.backoff_max, self.backoff_base * (2 ** max(0, fails - 1)))
        return max(b, min(retry_after, self.backoff_max)) if retry_after else b

    # ---- один источник ----
    def fetch(self, src, ctx=None, now=None, force=False, persist=True):
        """Один запрос + разбор + кэш. Не бросает. persist=False (verify): кэш не читается и не пишется."""
        now = self.clock() if now is None else now
        sid = src["id"]
        cache = read_cache(sid, self.root) if persist else {}
        if persist and not force and not self.due(src, now, cache):
            return Outcome(sid, deferred=True, error="not_due")
        why = self._admit(src, now)
        if why:
            return Outcome(sid, deferred=True, error=why)
        url, params = src_mod.request_of(src, now)
        headers = {"Accept": src_mod.ACCEPT.get(src.get("format"), "*/*")}
        if persist and cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if persist and cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        t0 = time.time()
        try:
            try:
                resp = self.transport(url, params, headers, float(src.get("timeout_s", 10)), MAX_BYTES)
            except FetchError:
                raise
            ms = int((time.time() - t0) * 1000)
            if not isinstance(resp, Resp):
                raise FetchError("net", "bad_transport")
            if resp.status == 304:
                return self._ok(src, cache, now, cache.get("data") or {}, [], resp, ms, persist, fresh=False)
            if resp.status != 200:
                raise FetchError("http", str(resp.status), resp.status)
            body = resp.body or b""
            if len(body) > MAX_BYTES:
                raise FetchError("toobig")
            pctx = dict(ctx or {})
            pctx["now"] = now
            data = src_mod.parse(src, body, pctx)
            items = data.pop("items", None) or []
            return self._ok(src, cache, now, data, items, resp, ms, persist)
        except FetchError as e:
            return self._fail(src, cache, now, e.kind if e.kind != "http" else "http_%s" % (e.status or 0), persist, getattr(e, "retry_after", None), t0)
        except src_mod.ParseError as e:
            return self._fail(src, cache, now, ("parse:%s" % e)[:60], persist, None, t0)
        except Exception as e:  # noqa: BLE001 — ни один источник не роняет сервис
            return self._fail(src, cache, now, "internal:%s" % type(e).__name__, persist, None, t0)

    def _ok(self, src, cache, now, data, items, resp, ms, persist, fresh=True):
        doc = {"id": src["id"], "ok": True, "fetched": now, "tried": now, "fails": 0, "next_try": 0, "last_error": None, "data": data,
               "etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified"), "hist": cache.get("hist") or []}
        hk = src.get("hist_key")
        if hk and isinstance(data.get(hk), (int, float)):
            doc["hist"] = [h for h in doc["hist"] if now - h[0] < 30 * 3600] + [[int(now), data[hk]]]
            doc["hist"] = doc["hist"][-80:]
        if persist:
            write_cache(src["id"], doc, self.root)
        return Outcome(src["id"], ok=True, data=data, items=items, status=resp.status, ms=ms, nbytes=len(resp.body or b""), fresh=fresh)

    def _fail(self, src, cache, now, code, persist, retry_after, t0):
        fails = int(cache.get("fails") or 0) + 1
        doc = dict(cache)
        doc.update({"id": src["id"], "ok": False, "tried": now, "fails": fails, "last_error": code,
                    "next_try": now + self.backoff_s(fails, retry_after)})
        doc.setdefault("fetched", cache.get("fetched"))
        if persist:
            try:
                write_cache(src["id"], doc, self.root)
            except OSError:
                pass
        return Outcome(src["id"], ok=False, error=code, ms=int((time.time() - t0) * 1000))
