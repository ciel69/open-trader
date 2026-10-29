"""MCP-сервер шлюза по stdio (WP11-A): `./tb mcp serve --agent ID`.

Протокол: JSON-RPC 2.0, сообщения построчно (UTF-8, без переводов строк внутри). Поддержаны версии MCP 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05
(при initialize отвечаем версией клиента, если она известна, иначе 2025-11-25). Методы: initialize, ping, tools/list, tools/call, resources/list,
resources/read, notifications/initialized, notifications/cancelled. tools/list и tools/call работают и без initialize (клиенты без состояния).

Безопасность: агент задан аргументом --agent (id, не секрет); токен, если запись реестра требует auth=token, читается из TB_AGENT_TOKEN или файла
agents/<id>.token, НИКОГДА не из аргументов. Видны и вызываемы только инструменты этого агента. Ошибки наружу без текстов исключений.
stdout занят протоколом: всё постороннее перенаправлено в stderr. Долгий wait_events идёт в отдельном потоке (ping и отмена остаются доступны).

Честное ограничение: MCP-сервер не может разбудить простаивающий чат (нет способа инициировать ход модели); wait_events работает, только пока
у модели идёт ход.
"""
import json
import os
import sys
import threading

from tb.brain import common
from tb.gateway import catalog, registry
from tb.gateway.rpc import Gateway

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "traider-bot-gateway"
SERVER_VERSION = "1.0"
MAX_LINE = 1024 * 1024
MAX_CONCURRENT = 4
RESOURCES = (("tb://status", "get_status", "Platform status", {}), ("tb://brief", "get_brief", "Decision brief (scope=start)", {"scope": "start"}),
             ("tb://events", "get_events", "Pending bus events", {}))
SECRET_ENV_PREFIXES = ("BYBIT_", "ANTHROPIC_", "TB_AGENT_TOKEN")

E_PARSE, E_REQUEST, E_METHOD, E_PARAMS, E_INTERNAL, E_NOT_FOUND = -32700, -32600, -32601, -32602, -32603, -32002


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


class Server(object):
    def __init__(self, gateway, agent_id, out, log=None):
        self.gw, self.agent_id, self.out = gateway, agent_id, out
        self.log = log or (lambda m: None)
        self._wlock = threading.Lock()
        self._cancel = {}
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._threads = []

    # ---- вывод
    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass

    # ---- методы
    def _init(self, params):
        want = params.get("protocolVersion") if isinstance(params, dict) else None
        ver = want if want in PROTOCOLS else PROTOCOLS[0]
        return {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Trading platform gateway. Use get_status/get_brief first; decisions only through plan tools. You never see keys or place raw orders."}

    def _tools(self):
        out = []
        for t in self.gw.tools_for(self.agent_id):
            out.append({"name": t.name, "description": t.desc, "inputSchema": t.input_schema(),
                        "annotations": {"readOnlyHint": not t.write, "openWorldHint": False}})
        return {"tools": out}

    def _result(self, res):
        return {"content": [{"type": "text", "text": common.dumps(res)}], "structuredContent": res, "isError": res.get("ok") is False}

    def _call_tool(self, params, rid):
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or (args is not None and not isinstance(args, dict)):
            return _err(rid, E_PARAMS, "name: строка, arguments: объект")
        ev = self._cancel.setdefault(rid, threading.Event()) if rid is not None else None
        try:
            res = self.gw.call(self.agent_id, name, args, via="mcp", cancel=ev)
        finally:
            self._cancel.pop(rid, None)
        if ev is not None and ev.is_set():
            return None                                   # запрос отменён клиентом: ответ не нужен
        return _ok(rid, self._result(res))

    def _resources(self):
        have = set(t.name for t in self.gw.tools_for(self.agent_id))
        return {"resources": [{"uri": u, "name": u[5:], "description": d, "mimeType": "application/json"} for u, tool, d, _ in RESOURCES if tool in have]}

    def _read_resource(self, params, rid):
        uri = params.get("uri")
        for u, tool, _, args in RESOURCES:
            if uri == u:
                res = self.gw.call(self.agent_id, tool, dict(args), via="mcp")
                return _ok(rid, {"contents": [{"uri": u, "mimeType": "application/json", "text": common.dumps(res)}]})
        return _err(rid, E_NOT_FOUND, "ресурс не найден")

    def handle(self, msg):
        """Синхронно обрабатывает один запрос/нотификацию -> ответ dict или None. tools/call здесь выполняется inline (поток — в dispatch)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None                               # ответ клиента (мы запросов не шлём)
            return _err(msg.get("id") if isinstance(msg, dict) else None, E_REQUEST, "invalid request")
        method, rid, params = msg["method"], msg.get("id"), msg.get("params")
        note = "id" not in msg
        params = params if isinstance(params, dict) else {}
        try:
            if method == "notifications/cancelled":
                ev = self._cancel.get(params.get("requestId"))
                if ev is not None:
                    ev.set()
                return None
            if note:
                return None                               # прочие нотификации (initialized и т.д.)
            if method == "initialize":
                return _ok(rid, self._init(params))
            if method == "ping":
                return _ok(rid, {})
            if method == "tools/list":
                return _ok(rid, self._tools())
            if method == "tools/call":
                return self._call_tool(params, rid)
            if method == "resources/list":
                return _ok(rid, self._resources())
            if method == "resources/templates/list":
                return _ok(rid, {"resourceTemplates": []})
            if method == "resources/read":
                return self._read_resource(params, rid)
            return _err(rid, E_METHOD, "method not found")
        except Exception as e:
            self.log("internal error: %s" % e.__class__.__name__)
            return _err(rid, E_INTERNAL, "internal error")

    def dispatch(self, msg):
        """Точка входа цикла: tools/call идёт в поток, остальное inline; батч (массив) обрабатывается inline."""
        if isinstance(msg, list):
            if not msg:
                self.send(_err(None, E_REQUEST, "empty batch"))
                return
            rs = [r for r in (self.handle(m) for m in msg) if r is not None]
            if rs:
                self.send(rs)
            return
        if isinstance(msg, dict) and msg.get("method") == "tools/call" and isinstance(msg.get("id"), (str, int)) \
                and not isinstance(msg.get("id"), bool):
            if not self._sem.acquire(False):
                self.send(_ok(msg["id"], self._result({"ok": False, "status": "error", "errors": [{"code": "E_BUSY", "path": "", "msg": "слишком много параллельных вызовов"}]})))
                return
            rid = msg["id"]
            self._cancel.setdefault(rid, threading.Event())

            def work():
                try:
                    r = self.handle(msg)
                    if r is not None:
                        self.send(r)
                finally:
                    self._sem.release()
            t = threading.Thread(target=work, daemon=True)
            t.start()
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            return
        r = self.handle(msg)
        if r is not None:
            self.send(r)

    def serve(self, stdin):
        while True:
            line = stdin.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                while True:                               # дочитать до конца слишком длинной строки
                    rest = stdin.readline(MAX_LINE)
                    if not rest or rest.endswith(b"\n"):
                        break
                self.send(_err(None, E_REQUEST, "message too large"))
                continue
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send(_err(None, E_PARSE, "parse error"))
                continue
            self.dispatch(msg)
        for t in list(self._threads):
            t.join(timeout=2.0)


def _usage():
    sys.stderr.write("использование: tb mcp serve --agent ID\n")
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] != "serve" or argv[1] != "--agent":
        return _usage()
    agent_id = argv[2]
    gw = Gateway()
    agent = gw.agent(agent_id)
    token = registry.read_token(agent_id) if agent is not None and agent.auth == "token" else None
    ok, code = registry.authenticate(agent, token)
    # токены и ключи в окружении дальше процессу не нужны
    for k in list(os.environ):
        if k.startswith(SECRET_ENV_PREFIXES):
            del os.environ[k]
    if not ok:
        sys.stderr.write("tb mcp: агент не допущен (%s)\n" % code)
        return 3
    out = sys.stdout.buffer
    sys.stdout = sys.stderr                                # посторонний print не должен портить протокол
    stdin = sys.stdin.buffer
    srv = Server(gw, agent_id, out, log=lambda m: sys.stderr.write("tb mcp: %s\n" % m))
    try:
        srv.serve(stdin)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
