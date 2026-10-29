"""LLMClient: бюджет -> адаптер -> учёт. Интерфейс слоя для runtime мозга (WP11-C2).

    reg = providers.load(root)
    client = client.make(reg, "deepseek", "flash", root=root)        # или LLMClient(...)
    resp = client(req)            # то же, что client.complete(req); исключения только LLMError
`client` можно передавать как `call` в schema.ask_json и toolloop.run (есть note_format_error). Учёт пишется в state/llm_calls.jsonl на КАЖДЫЙ вызов
(успешный и неуспешный) с полями из req.meta; ключ и тексты в учёт не попадают.
"""
import time

from tb.llm import adapters, budget as budget_mod, providers, usage as usage_mod
from tb.llm.transport import Transport
from tb.llm.types import LLMError


class LLMClient(object):
    def __init__(self, registry, provider_id, model_key, root=None, budget=None, transport=None, environ=None, clock=time.time, record=True):
        self.registry = registry
        self.provider = registry.provider(provider_id)
        self.model = self.provider.model(model_key)
        self.root, self.clock, self.record = root, clock, record
        self.budget = budget if budget is not None else budget_mod.from_registry(registry, root=root, clock=clock)
        self.adapter = adapters.make(self.provider, self.model, transport=transport, environ=environ)

    @property
    def flags(self):
        m = self.model
        return {"tools": m.tools, "strict": m.strict, "json_mode": m.json_mode, "thinking": m.thinking, "cache": m.cache}

    def __call__(self, req):
        return self.complete(req)

    def complete(self, req):
        p, m = self.provider, self.model
        if not p.enabled:
            raise LLMError("disabled", "провайдер %s отключён%s" % (p.id, " (" + p.error + ")" if p.error else ""))
        if req.meter is not None:
            req.meter.check()
        self.budget.check(p.id)
        t0 = time.time()
        try:
            resp = self.adapter.complete(req)
        except LLMError as e:
            self._log(req, None, ok=False, err=e.code, lat=(time.time() - t0) * 1000, retries=e.retries)
            raise
        resp.usd = usage_mod.price_usd(m.price, resp.usage, self.clock())
        self._log(req, resp, ok=True, err=None, lat=resp.latency_ms, retries=resp.retries)
        if req.meter is not None:
            req.meter.add(resp.usd, resp.usage.inp + resp.usage.out)
        self.budget.after_call(p.id)
        return resp

    def _log(self, req, resp, ok, err, lat, retries):
        if not self.record:
            return
        meta = dict(req.meta)
        if "prompt_hash" not in meta:
            meta["prompt_hash"] = usage_mod.prompt_hash(req.system)
        usage_mod.record(meta, self.provider.id, self.model.key, resp.usage if resp else None, resp.usd if resp else 0.0, lat, ok, err, retries,
                         resp.finish if resp else None, root=self.root, now=self.clock())
        self._last_meta = meta

    def note_format_error(self, resp, codes):
        """Отмечает в учёте ошибку формата ответа (проверка схемы идёт после вызова)."""
        if self.record:
            usage_mod.record_format_error(getattr(self, "_last_meta", {}), self.provider.id, self.model.key, codes, root=self.root, now=self.clock())


def make(registry, provider_id, model_key, root=None, **kw):
    return LLMClient(registry, provider_id, model_key, root=root, **kw)
