"""Фейковый HTTP-сервер LLM для тестов слоя tb.llm (WP11-C1): локальный http.server, сценарии по пути, журнал запросов.

    srv = FakeLLMServer(); srv.start(); srv.push("/chat/completions", R(200, ds_ok("привет")), R(503))
    srv.requests -> [{"path","method","headers"(нижний регистр),"json"}]; srv.connections -> число TCP-соединений.
Реальных запросов в сеть нет: слушает 127.0.0.1 на свободном порту.
"""
import http.server
import json
import threading
import time


class R(object):
    """Один ответ сценария: status, json (dict) или raw (bytes), delay (сек до ответа), headers, close (закрыть соединение после ответа), drop (оборвать без ответа)."""

    def __init__(self, status=200, json=None, raw=None, delay=0.0, headers=None, close=False, drop=False):
        self.status, self.json, self.raw, self.delay, self.headers, self.close, self.drop = status, json, raw, delay, headers or {}, close, drop


def ds_ok(text="", tool_calls=None, finish=None, hit=None, miss=None, completion=5, reasoning=None, reasoning_tokens=0, rid="chatcmpl-1"):
    msg = {"role": "assistant", "content": text}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [{"id": c[0], "type": "function", "function": {"name": c[1], "arguments": c[2] if isinstance(c[2], str) else json.dumps(c[2])}}
                             for c in tool_calls]
    u = {"prompt_tokens": (hit or 0) + (miss if miss is not None else 10), "completion_tokens": completion, "total_tokens": 0}
    if hit is not None:
        u["prompt_cache_hit_tokens"] = hit
        u["prompt_cache_miss_tokens"] = miss if miss is not None else 10
    if reasoning_tokens:
        u["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {"id": rid, "choices": [{"index": 0, "message": msg, "finish_reason": finish or ("tool_calls" if tool_calls else "stop")}], "usage": u}


def an_ok(text="", tool_uses=None, stop=None, inp=10, out=5, cache_read=0, cache_write=0, thinking=None, rid="msg_1"):
    blocks = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": thinking, "signature": "SIG"})
    if text:
        blocks.append({"type": "text", "text": text})
    for t in tool_uses or []:
        blocks.append({"type": "tool_use", "id": t[0], "name": t[1], "input": t[2]})
    return {"id": rid, "type": "message", "role": "assistant", "content": blocks, "stop_reason": stop or ("tool_use" if tool_uses else "end_turn"),
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}}


class FakeLLMServer(object):
    def __init__(self):
        import _bootstrap
        _bootstrap.require_localhost()          # нет localhost-сокета -> SkipTest с понятной причиной
        self.queue, self.default, self.requests, self.connections = {}, {}, [], 0
        self._lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                http.server.BaseHTTPRequestHandler.setup(self)
                with outer._lock:
                    outer.connections += 1

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    js = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    js = None
                with outer._lock:
                    outer.requests.append({"path": self.path, "method": self.command, "headers": {k.lower(): v for k, v in self.headers.items()}, "json": js})
                    q = outer.queue.get(self.path)
                    r = q.pop(0) if q else outer.default.get(self.path)
                if r is None:
                    r = R(500, json={"error": {"message": "нет сценария"}})
                if r.delay:
                    time.sleep(r.delay)
                if r.drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                body = r.raw if r.raw is not None else json.dumps(r.json if r.json is not None else {}).encode("utf-8")
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in r.headers.items():
                    self.send_header(k, v)
                if r.close:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET = _handle

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def push(self, path, *rs):
        with self._lock:
            self.queue.setdefault(path, []).extend(rs)

    def count(self, path=None):
        return len([r for r in self.requests if path is None or r["path"] == path])
