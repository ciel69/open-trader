"""Нейтральные к провайдеру типы: сообщения, инструменты, запрос, ответ, ошибка.

Сообщения (dict):
  {"role":"user","content":str}
  {"role":"assistant","content":str,"tool_calls":[{"id","name","args"(dict),"raw_args"(str)}],"reasoning":str|None,"opaque":dict|None}
  {"role":"tool","tool_call_id":str,"name":str,"content":str}
Инструмент: {"name","description","schema"(JSON Schema объекта)}.
`opaque` и `reasoning` адаптер возвращает провайдеру как есть (DeepSeek reasoning_content, блоки thinking Anthropic); для других провайдеров игнорируются.
"""
import copy

# код ошибки -> можно ли переключаться на запасной профиль (brain.json:fallback.on: timeout, http_5xx, rate, budget, format)
FALLBACK_KIND = {"timeout": "timeout", "network": "timeout", "server": "http_5xx", "rate": "rate", "budget": "budget", "format": "format",
                 "empty": "format", "truncated": "format"}


class LLMError(Exception):
    """Ошибка слоя LLM. Текст безопасен (без ключей и заголовков). code: auth, billing, bad_request, invalid_params, rate, server, timeout,
    network, bad_body, no_key, disabled, config, budget, format, empty, truncated."""

    def __init__(self, code, msg="", status=None, retries=0):
        Exception.__init__(self, "%s: %s" % (code, msg) if msg else code)
        self.code, self.msg, self.status, self.retries = code, msg, status, retries

    @property
    def fallback_kind(self):
        return FALLBACK_KIND.get(self.code)


def user(text):
    return {"role": "user", "content": text}


def assistant(text="", tool_calls=None, reasoning=None, opaque=None):
    m = {"role": "assistant", "content": text or ""}
    if tool_calls:
        m["tool_calls"] = [{"id": c.id, "name": c.name, "args": c.args, "raw_args": c.raw_args} if isinstance(c, ToolCall) else dict(c)
                           for c in tool_calls]
    if reasoning:
        m["reasoning"] = reasoning
    if opaque:
        m["opaque"] = opaque
    return m


def tool_result(call_id, name, content):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def tool(name, description, schema):
    return {"name": name, "description": description, "schema": schema}


class ToolCall(object):
    __slots__ = ("id", "name", "args", "raw_args", "error")

    def __init__(self, id, name, args=None, raw_args="", error=None):
        self.id, self.name, self.args, self.raw_args, self.error = id, name, args if args is not None else {}, raw_args, error


class Usage(object):
    """Токены вызова. inp = все токены промпта = miss + hit + write. out включает reasoning (рассуждения тарифицируются как выход)."""
    __slots__ = ("miss", "hit", "write", "out", "reasoning")

    def __init__(self, miss=0, hit=0, write=0, out=0, reasoning=0):
        self.miss, self.hit, self.write, self.out, self.reasoning = int(miss), int(hit), int(write), int(out), int(reasoning)

    @property
    def inp(self):
        return self.miss + self.hit + self.write

    def add(self, o):
        return Usage(self.miss + o.miss, self.hit + o.hit, self.write + o.write, self.out + o.out, self.reasoning + o.reasoning)

    def as_dict(self):
        return {"in": self.inp, "in_hit": self.hit, "in_write": self.write, "out": self.out, "reasoning": self.reasoning}


class Req(object):
    """Запрос к модели.

    response_format: None | "json" (json_object) | {"schema": {...}, "name": "respond"} (структурный вывод; Anthropic: инструмент respond).
    tool_choice: None | "auto" | "none" | "required" | {"name": str}.   thinking: None | "off" | "low" | "medium" | "high".
    meta: поля для учёта (job, role, team, agent, profile, rev, event_seq, prompt_hash, fallback_from, round); meter: JobMeter (budget.py).
    """

    def __init__(self, system="", messages=None, tools=None, tool_choice=None, response_format=None, max_tokens=1024, temperature=None,
                 thinking=None, meta=None, meter=None, timeout_s=None):
        self.system, self.messages, self.tools = system, list(messages or []), list(tools or [])
        self.tool_choice, self.response_format, self.max_tokens = tool_choice, response_format, max_tokens
        self.temperature, self.thinking, self.meta, self.meter, self.timeout_s = temperature, thinking, dict(meta or {}), meter, timeout_s

    def copy(self, **kw):
        r = Req(self.system, self.messages, self.tools, self.tool_choice, self.response_format, self.max_tokens, self.temperature,
                self.thinking, self.meta, self.meter, self.timeout_s)
        for k, v in kw.items():
            setattr(r, k, list(v) if k in ("messages", "tools") else v)
        return r


class Resp(object):
    """Ответ. finish: stop | tool_calls | length | filtered | other. message = нейтральное assistant-сообщение для истории."""

    def __init__(self, text="", tool_calls=None, usage=None, finish="stop", latency_ms=0, raw_id=None, reasoning=None, opaque=None, retries=0):
        self.text, self.tool_calls, self.usage = text or "", list(tool_calls or []), usage or Usage()
        self.finish, self.latency_ms, self.raw_id, self.reasoning, self.opaque, self.retries = finish, latency_ms, raw_id, reasoning, opaque, retries
        self.usd = 0.0
        self.err = None

    @property
    def message(self):
        return assistant(self.text, self.tool_calls, self.reasoning, self.opaque)

    @property
    def empty(self):
        return not self.text.strip() and not self.tool_calls


def deep(o):
    return copy.deepcopy(o)
