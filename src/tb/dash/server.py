"""Локальный дашборд: `./tb dash [--port 8765] [--open] [--max-seconds N]`.

http.server из stdlib, слушает ТОЛЬКО 127.0.0.1. Отдаёт статическую страницу (static/) и JSON API, только GET:
  /api/state?mode=live|paper|testnet|all&venue=all|platform|bybit   всё для страницы одним ответом
  /api/market  /api/hot  /api/ideas  /api/health  /api/trades?mode=&venue=      срезы того же состояния
  /api/ping
  /api/v2/state  /api/v2/plans?status=&mode=  /api/v2/plan?id=  /api/v2/decisions?after=&before=&plan=&actor=&kind=&only=fail&mode=&limit=
  /api/v2/brief?id=  /api/v2/events?after=&limit=  /api/v2/exp?by=&mode=      слой «система плана» (model2.py), тоже только чтение
  /api/ctl/session  /api/ctl/status  /api/ctl/result?id=  /api/ctl/audit?n=      плоскость управления (WP12): чтение
POST разрешены ТОЛЬКО под /api/ctl/ (login, logout, challenge, command) и только после проверок ctlweb (токен, cookie, CSRF, Origin, Content-Type);
на остальных путях POST и все PUT/DELETE/PATCH/OPTIONS = 405. На биржу не ходит, ключей Bybit/LLM не читает, процессов не запускает.
Расширения (WP13/14/15/11-C): EXT_API (модули tb.<пакет>.dashapi с функцией api(path, query, root, now), только чтение) и EXT_SCRIPTS (скрипты вкладок в static/).
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tb.dash import ctlweb, model, model2

HOST = "127.0.0.1"
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# явный список (никаких путей из запроса в файловую систему): страница, стили, скрипты по частям, иконка
SCRIPTS = ("theme.js", "core.js", "charts.js", "v-strip.js", "v-overview.js", "v-plans.js", "v-decisions.js", "v-brief.js", "v-trades.js", "v-exp.js",
           "v-ideas.js", "v-market.js", "v-system.js", "v-control.js", "app.js")
FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/favicon.svg": "favicon.svg"}
FILES.update({"/" + n: n for n in SCRIPTS})

# ---- реестр расширений (владелец server.py = WP12; остальные пакеты добавляют файлы, не правя чужой код) ----
# EXT_API: модули `tb.<пакет>.dashapi` с функцией api(path, query, root, now) -> None (не мой путь) | (код, объект); подключаются, если модуль существует.
EXT_API = ("tb.intel.dashapi", "tb.lifecycle.dashapi", "tb.memory.dashapi", "tb.brain.dashapi", "tb.llm.dashapi")
# EXT_SCRIPTS: скрипты вкладок в dash/static/ ВНЕ базового списка: известные имена + автообнаружение `x-*.js`; подключаются перед app.js, если файл есть.
# Скрипт регистрирует вкладку вызовом registerTab(id, title, iconPath, render) из core.js.
EXT_SCRIPTS = ("v-intel.js", "v-day.js", "v-memory.js", "v-brain.js", "v-notify.js")
EXT_MARK = "<!--EXT_SCRIPTS-->"
RE_EXT_NAME = re.compile(r"^x-[a-z0-9-]{1,30}\.js$")


def ext_scripts():
    """-> список имён скриптов-расширений, реально лежащих в static/ (порядок: известные, затем x-*.js по алфавиту)."""
    try:
        have = set(os.listdir(STATIC))
    except OSError:
        return []
    return [n for n in EXT_SCRIPTS if n in have] + sorted(n for n in have if RE_EXT_NAME.match(n))


def ext_api(path, query, root, now):
    """Первый расширяющий модуль, взявшийся за путь. -> (код, объект) | None."""
    import importlib
    import importlib.util
    for mod in EXT_API:
        try:
            if importlib.util.find_spec(mod) is None:
                continue
            fn = getattr(importlib.import_module(mod), "api", None)
        except (ImportError, ValueError, AttributeError):
            continue
        if callable(fn):
            r = fn(path, query, root, now)
            if r is not None:
                return r
    return None
# типы заданы явно: mimetypes на Windows берёт их из реестра и может отдать .js как text/plain
CTYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".svg": "image/svg+xml"}
MODES = ("live", "paper", "testnet", "all")
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def _clean(value, allowed, default):
    return value if value in allowed else default


def api_payload(path, query, root=None, now=None):
    """-> (статус, объект) для /api/*; чистая функция, удобна для тестов."""
    if path.startswith("/api/v2/"):
        code, obj = model2.api_v2(path, query, root, now)
        return code, model.scrub(obj)
    mode = _clean((query.get("mode") or ["live"])[0], MODES, "live")
    venue = (query.get("venue") or ["all"])[0]
    venue = venue if venue in ("all", "platform", "bybit") else "all"
    if path.startswith("/api/ctl/"):      # плоскость управления: чтение (post идёт через ctlweb); сессию знает только Handler
        return 404, {"error": "unknown endpoint"}
    if path == "/api/ping":
        return 200, {"ok": True, "now": time.time() if now is None else now}
    if path == "/api/platform":          # статус платформы (WP11-B): тот же словарь, что `./tb status --json`
        from tb.platform import status as platform_status
        return 200, model.scrub(platform_status.build_status(root, now))
    if path == "/api/ideas":
        return 200, model.scrub(model.ideas_info(root, now))
    ext = ext_api(path, query, root, now)
    if ext is not None:
        return ext[0], model.scrub(ext[1])
    st = model.build_state(root, mode, venue, now)
    if path == "/api/state":
        return 200, st
    if path == "/api/market":
        return 200, st["market"]
    if path == "/api/hot":
        return 200, st["hot"]
    if path == "/api/health":
        return 200, {"health": st["health"], "warnings": st["warnings"], "events": st["events"]}
    if path == "/api/trades":
        return 200, {"filter": st["filter"], "open": st["open_trades"], "closed": st["history"],
                     "closed_total": st["history_total"], "metrics": st["metrics"]}
    return 404, {"error": "unknown endpoint"}


class Handler(BaseHTTPRequestHandler):
    server_version = "tb-dash"
    root = None
    quiet = True
    ctl = None            # ctlweb.CtlWeb (создаётся в make_server)

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")  # защита от DNS rebinding

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path.startswith("/api/ctl/") and self.ctl is not None:
            try:
                code, obj, extra = self.ctl.get(u.path, parse_qs(u.query), self.headers)
            except Exception:
                return self._json(500, {"error": "internal"})
            return self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", extra)
        if u.path.startswith("/api/"):
            try:
                code, obj = api_payload(u.path, parse_qs(u.query), self.root)
            except Exception as e:  # дашборд не должен падать из-за одного странного файла
                return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            return self._json(code, obj)
        name = FILES.get(u.path)
        if not name and u.path.startswith("/") and u.path[1:] in ext_scripts():
            name = u.path[1:]
        if not name:
            return self._json(404, {"error": "not found"})
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "static missing"})
        if name == "index.html":
            tags = "".join('<script src="/%s"></script>\n' % n for n in ext_scripts())
            body = body.replace(EXT_MARK.encode("ascii"), tags.encode("utf-8"))
        self._send(200, body, CTYPES[os.path.splitext(name)[1]])

    do_HEAD = do_GET

    def _deny(self):
        self._json(405, {"error": "method not allowed"})

    def do_POST(self):
        """POST только под /api/ctl/*; всё прочее 405. Разбор и проверки: ctlweb.CtlWeb.post."""
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path = urlparse(self.path).path
        if not path.startswith("/api/ctl/") or self.ctl is None:
            return self._deny()
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if n < 0 or n > ctlweb.MAX_BODY:
            return self._json(400, {"ok": False, "code": "E_BODY", "msg": ctlweb.ERR_TEXT["E_BODY"]})
        body = self.rfile.read(n) if n else b""
        try:
            code, obj, extra = self.ctl.post(path, self.headers, body, self.server.server_address[1])
        except Exception:
            return self._json(500, {"ok": False, "code": "E_INTERNAL", "msg": ctlweb.ERR_TEXT["E_INTERNAL"]})
        self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", extra)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny


class QuietServer(ThreadingHTTPServer):
    """Обрыв соединения клиентом (закрыл вкладку, отправил слишком большое тело и получил отказ) не печатает трассировку в консоль."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def make_server(port=8765, root=None, quiet=True):
    """Сервер на 127.0.0.1:port (port=0 -> свободный порт). Адрес менять нельзя: наружу дашборд не слушает."""
    handler = type("BoundHandler", (Handler,), {"root": root, "quiet": quiet, "ctl": ctlweb.CtlWeb(root)})
    return QuietServer((HOST, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb dash", description="локальный дашборд (127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="открыть страницу в браузере по умолчанию")
    ap.add_argument("--max-seconds", type=float, default=None, help="остановиться через N секунд (для проверок)")
    ap.add_argument("--root", default=None, help="корень проекта (по умолчанию TB_ROOT или папка проекта)")
    ap.add_argument("--verbose", action="store_true", help="печатать запросы")
    a = ap.parse_args(argv)
    try:
        srv = make_server(a.port, a.root, quiet=not a.verbose)
    except OSError as e:
        print("не удалось занять 127.0.0.1:%d (%s). Другой порт: ./tb dash --port 8766" % (a.port, e), file=sys.stderr)
        return 1
    url = "http://%s:%d/" % (HOST, srv.server_address[1])
    print("дашборд: %s  (127.0.0.1; управление: paper без токена, опасное по токену один раз; остановка Ctrl+C)" % url, flush=True)
    if a.max_seconds:
        threading.Timer(a.max_seconds, srv.shutdown).start()
    if a.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
