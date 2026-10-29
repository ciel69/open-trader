"""Фейковый LLM для тестов рантайма мозга (WP11-C2): детерминированные сценарные ответы без сети и ключей.

    f = FakeLLM(["{...json...}", Err("timeout"), Tool("show_plan", {"id": "PL-..."}), "{...}"])   # ответы по очереди; когда сценарий кончился, повторяется последний
    client = f(req)                  # то же, что LLMClient: Resp с usage/usd; исключения LLMError; f.calls — журнал запросов
Шаг сценария: str (текст ответа), dict (JSON-ответ), Err(code[, msg]) -> LLMError, Tool(name, args) (один вызов инструмента), Boom() -> RuntimeError, callable(req) -> шаг.
Factory(scripts) -> client_factory для Runner: профиль -> FakeLLM; Factory.clients[профиль].
Учёт: FakeLLM(record_root=корень) пишет в state/llm_calls.jsonl те же строки, что настоящий клиент (для проверки суточных потолков и агрегатов).
"""
import json

from tb.llm import usage as usage_mod
from tb.llm.types import LLMError, Resp, ToolCall, Usage


class Err(object):
    def __init__(self, code, msg=""):
        self.code, self.msg = code, msg


class Tool(object):
    def __init__(self, name, args=None):
        self.name, self.args = name, args or {}


class Boom(object):
    pass


class FakeLLM(object):
    def __init__(self, script=None, usd=0.001, tokens=(100, 20), record_root=None, provider="fake", model="m", finish="stop"):
        self.script = list(script or [])
        self.usd, self.tokens, self.record_root, self.provider, self.model, self.finish = usd, tokens, record_root, provider, model, finish
        self.calls, self.format_errors, self.n = [], [], 0
        self.on_call = None

    def push(self, *steps):
        self.script.extend(steps)

    def _step(self, req):
        if not self.script:
            return "{}"
        i = min(self.n, len(self.script) - 1)
        st = self.script[i]
        self.n += 1
        return st(req) if callable(st) and not isinstance(st, (Err, Tool, Boom)) else st

    def __call__(self, req):
        if req.meter is not None:
            req.meter.check()
        self.calls.append({"req": req, "meta": dict(req.meta)})
        if self.on_call:
            self.on_call(self, req)
        st = self._step(req)
        if isinstance(st, Err):
            raise LLMError(st.code, st.msg)
        if isinstance(st, Boom):
            raise RuntimeError("boom")
        tcs, text = [], ""
        if isinstance(st, Tool):
            tcs = [ToolCall("c%d" % self.n, st.name, st.args, json.dumps(st.args))]
        elif isinstance(st, dict):
            text = json.dumps(st, ensure_ascii=False)
        else:
            text = str(st)
        u = Usage(miss=self.tokens[0], out=self.tokens[1])
        resp = Resp(text=text, tool_calls=tcs, usage=u, finish="tool_calls" if tcs else self.finish, latency_ms=5)
        resp.usd = self.usd
        if req.meter is not None:
            req.meter.add(self.usd, u.inp + u.out)
        if self.record_root:
            usage_mod.record(req.meta, self.provider, self.model, u, self.usd, 5, True, None, 0, resp.finish, root=self.record_root)
        return resp

    def note_format_error(self, resp, codes):
        self.format_errors.append(list(codes))


class Factory(object):
    def __init__(self, scripts=None, **kw):
        self.scripts, self.kw, self.clients = dict(scripts or {}), kw, {}

    def __call__(self, pid, profile):
        c = self.clients.get(pid)
        if c is None:
            c = self.clients[pid] = FakeLLM(self.scripts.get(pid, []), provider=profile.provider, model=profile.model, **self.kw)
        return c

    def calls(self, pid=None):
        return sum(len(c.calls) for k, c in self.clients.items() if pid in (None, k))
