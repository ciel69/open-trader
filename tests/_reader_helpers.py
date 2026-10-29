"""Помощники тестов WP13b: фейковый LLM (детерминированные ответы, без сети), заполнение items.jsonl, конфиг читателя."""
import json
import os
import re

import _bootstrap  # noqa: F401
from _intel_helpers import FIX, NOW
from tb.intel import items as iitems
from tb.intel import store
from tb.llm.types import Resp, Usage

RE_REFS = re.compile(r'"ref":"(N-[0-9a-f]{8})"')


def hostile():
    with open(os.path.join(FIX, "hostile_headlines.json"), encoding="utf-8") as f:
        return json.load(f)


def refs_of(req):
    return RE_REFS.findall(req.messages[-1]["content"])


def reply(refs, **over):
    """Валидный ответ читателя на все refs; over переопределяет поля каждой записи."""
    rows = []
    for r in refs:
        x = {"ref": r, "syms": [], "type": "macro", "imp": 2, "dir": "neutral", "ttl_min": 360, "conf": 0.6}
        x.update(over)
        rows.append(x)
    return json.dumps({"items": rows}, ensure_ascii=False)


class FakeLLM(object):
    """call(req) -> Resp. script: ответы по очереди (str | Exception | callable(refs)); затем fn(refs) или валидный ответ по умолчанию."""

    def __init__(self, script=None, fn=None, **over):
        self.script, self.fn, self.over = list(script or []), fn, over
        self.reqs, self.notes = [], []

    def __call__(self, req):
        self.reqs.append(req)
        if self.script:
            r = self.script.pop(0)
        elif self.fn:
            r = self.fn
        else:
            r = reply
        if callable(r):
            r = r(refs_of(req))
        if isinstance(r, Exception):
            raise r
        return Resp(text=r, usage=Usage(miss=300, out=120))

    def note_format_error(self, resp, codes):
        self.notes.append(list(codes))

    @property
    def texts(self):
        return [r.messages[-1]["content"] for r in self.reqs]


def rcfg(root, **over):
    c = store.load_cfg(root)
    r = {"enabled": True, "min_gap_s": 0.0, "max_wait_s": 0.0, "retries": 1}
    r.update(over)
    c["reader"] = r
    return c


def add_item(root, title, snip="", src="coindesk", ts=None, url="https://www.coindesk.com/a", now=NOW):
    it = iitems.make_item(src, src, title, snip, url, NOW - 60 if ts is None else ts, now)
    iitems.ItemStore(root).add_many([it], now)
    return it
