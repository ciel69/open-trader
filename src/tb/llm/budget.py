"""Потолки расхода: сутки и час (календарные, UTC) по токенам и долларам, на провайдера и общие; потолок задания (JobMeter).

Расход считается по `state/llm_calls.jsonl` (его пишут все процессы: brain и intel), поэтому переживает перезапуск. Исчерпание любого потолка:
LLMError('budget'), событие шины `limit_hit{which:"brain_budget", until_ts}` (один раз на окно и область). Порог warn_frac даёт state="warn".
limits = {"day_usd","hour_usd","day_tokens","hour_tokens","job_usd","warn_frac","providers":{<id>:{"day_usd","hour_usd","day_tokens","hour_tokens"}}}.
Без ключа/нуль или None = нет потолка (0 для local означает «бесплатно», а не «запрещено»: потолки с нулём игнорируются).
"""
import time

from tb.llm import usage as usage_mod
from tb.llm.types import LLMError

SCOPES = (("day_usd", "usd", 86400), ("hour_usd", "usd", 3600), ("day_tokens", "tokens", 86400), ("hour_tokens", "tokens", 3600))


def window_start(now, size):
    return now - (now % size)


class JobMeter(object):
    """Потолок одного задания: накопление $ и токенов по вызовам, check() кидает LLMError('budget')."""

    def __init__(self, max_usd=None, max_tokens=None):
        self.max_usd, self.max_tokens, self.usd, self.tokens = max_usd, max_tokens, 0.0, 0

    def add(self, usd, tokens):
        self.usd += usd or 0.0
        self.tokens += tokens or 0

    def check(self):
        if self.max_usd and self.usd >= self.max_usd:
            raise LLMError("budget", "потолок задания по долларам исчерпан")
        if self.max_tokens and self.tokens >= self.max_tokens:
            raise LLMError("budget", "потолок задания по токенам исчерпан")


class Budget(object):
    def __init__(self, limits=None, root=None, clock=time.time, emit=True):
        self.limits = dict(limits or {})
        self.root, self.clock, self.emit = root, clock, emit
        self._emitted = set()

    def _caps(self, provider):
        """-> [(ключ, метрика, размер окна, потолок, область)]."""
        out = []
        for key, metric, size in SCOPES:
            v = self.limits.get(key)
            if v:
                out.append((key, metric, size, float(v), "all"))
            pv = ((self.limits.get("providers") or {}).get(provider) or {}).get(key) if provider else None
            if pv:
                out.append((key, metric, size, float(pv), provider))
        return out

    def status(self, provider=None):
        """-> {"state":"ok|warn|exhausted","scope","key","used","cap","until_ts","items":[...]}; самый строгий из потолков."""
        now = self.clock()
        warn = float(self.limits.get("warn_frac") or 0.8)
        worst = {"state": "ok", "items": []}
        day0 = window_start(now, 86400)
        rows = usage_mod.read_rows(self.root, since=day0)
        for key, metric, size, cap, scope in self._caps(provider):
            w0 = window_start(now, size)
            tot = usage_mod.window_totals([r for r in rows if r["t"] >= w0], None if scope == "all" else scope)
            used = tot[metric]
            frac = used / cap
            st = "exhausted" if used >= cap else ("warn" if frac >= warn else "ok")
            item = {"state": st, "scope": scope, "key": key, "used": round(used, 6), "cap": cap, "until_ts": w0 + size}
            worst["items"].append(item)
            if _rank(st) > _rank(worst["state"]):
                worst.update({k: item[k] for k in ("scope", "key", "used", "cap", "until_ts")})
                worst["state"] = st
        return worst

    def check(self, provider=None):
        """Нет места под вызов -> событие + LLMError('budget'). Иначе -> status (state ok|warn)."""
        st = self.status(provider)
        if st["state"] == "exhausted":
            self._notify(st)
            raise LLMError("budget", "исчерпан потолок %s (%s): %s из %s" % (st["key"], st["scope"], st["used"], st["cap"]))
        return st

    def _notify(self, st):
        mark = (st["scope"], st["key"], int(st["until_ts"]))
        if not self.emit or mark in self._emitted:
            return
        self._emitted.add(mark)
        try:
            from tb.core import bus
            bus.emit("limit_hit", {"which": "brain_budget", "until_ts": st["until_ts"]}, root=self.root)
        except Exception:
            pass

    def after_call(self, provider=None):
        """Вызывается после записи учёта: если вызов исчерпал потолок, сразу публикует событие (следующий check откажет)."""
        st = self.status(provider)
        if st["state"] == "exhausted":
            self._notify(st)
        return st


def _rank(s):
    return {"ok": 0, "warn": 1, "exhausted": 2}[s]


def from_registry(reg, root=None, clock=time.time):
    """Потолки из providers.json: корневой budget + limits каждого провайдера."""
    lim = {k: v for k, v in (reg.budget or {}).items() if k in ("day_usd", "hour_usd", "day_tokens", "hour_tokens", "job_usd", "warn_frac")}
    lim["providers"] = {pid: {k: v for k, v in p.limits.items() if k in ("day_usd", "hour_usd", "day_tokens", "hour_tokens")}
                        for pid, p in reg.providers.items()}
    return Budget(lim, root=root, clock=clock)
