"""Защита от зацикливания и «спама» решений мозга (WP11-C2).

Три независимых ограничителя (лимит планов в час уже стоит в шлюзе: agents.json plans_per_hour):
  RoleRate    не более N запусков роли в час (role_jobs_per_hour), сверх лимита задание роли пропускается (ROLE_RATE);
  JobRate     не более N запусков заданий в час в целом (runtime.guards.jobs_total_per_hour) и N обычных/критичных на подписку (budget.jobs_per_hour/critical_per_hour);
  LoopGuard   не более N заданий по одному ключу (plan:/sym:) за окно: дальше ключ блокируется на loop_block_s (событие error BRAIN_LOOP, задания закрываются skipped);
  RepeatGuard одно и то же решение (роль, план, решение, аргументы) не исполняется чаще same_decision_max раз за окно; сверх лимита решение не исполняется (REPEAT_DECISION).
Состояние в памяти процесса (перезапуск демона начинает окна заново; жёсткие потолки токенов/долларов хранятся в state/llm_calls.jsonl и переживают рестарт).
Чистый модуль: время подаётся аргументом.
"""
import hashlib
import json
import threading


class _Window(object):
    def __init__(self):
        self._t = {}
        self._lock = threading.Lock()

    def add(self, key, now, window):
        with self._lock:
            lst = [t for t in self._t.get(key, []) if now - t < window]
            lst.append(now)
            self._t[key] = lst
            return len(lst)

    def count(self, key, now, window):
        with self._lock:
            return len([t for t in self._t.get(key, []) if now - t < window])


class JobRate(object):
    def __init__(self, total_per_hour=60, per_sub=12, critical=30, scan=None):
        self.total, self.per_sub, self.critical, self.scan = total_per_hour, per_sub, critical, scan
        self.w = _Window()

    def allow(self, sub, prio, now):
        """-> (ok, код). Учитывает запуск только при ok."""
        if self.total and self.w.count("*", now, 3600.0) >= self.total:
            return False, "JOBS_TOTAL"
        scan = self.scan is not None and prio >= 3          # сигналы сканера (класс 3+) считаются отдельно и не съедают лимит заданий по позициям
        kind = "crit" if prio == 0 else ("scan" if scan else "norm")
        cap = self.critical if prio == 0 else (self.scan if scan else self.per_sub)
        if cap and self.w.count((sub, kind), now, 3600.0) >= cap:
            return False, "JOBS_SUB"
        self.w.add("*", now, 3600.0)
        self.w.add((sub, kind), now, 3600.0)
        return True, None

    def seed(self, sub, prio, ts, now):
        """Восстановление окон по файлам заданий после рестарта."""
        if now - ts < 3600.0:
            self.w.add("*", ts, 1e18)
            self.w.add((sub, "crit" if prio == 0 else ("scan" if self.scan is not None and prio >= 3 else "norm")), ts, 1e18)


class RoleRate(object):
    """Потолок заданий роли в час (runtime.guards.role_jobs_per_hour: {роль: N}); роли без записи не ограничены. Учитывается только запуск, который прошёл."""

    def __init__(self, caps=None):
        self.caps = dict(caps or {})
        self.w = _Window()

    def allow(self, role, now):
        cap = self.caps.get(role)
        if cap is None:
            return True
        if self.w.count(role, now, 3600.0) >= cap:
            return False
        self.w.add(role, now, 3600.0)
        return True


class LoopGuard(object):
    def __init__(self, max_jobs=8, window_s=600.0, block_s=600.0):
        self.max_jobs, self.window_s, self.block_s = max_jobs, window_s, block_s
        self.w = _Window()
        self.blocked = {}

    def check(self, key, now):
        """-> (ok, новая_блокировка). Вызывается при взятии задания по ключу."""
        until = self.blocked.get(key)
        if until and until > now:
            return False, False
        n = self.w.add(key, now, self.window_s)
        if self.max_jobs and n > self.max_jobs:
            self.blocked[key] = now + self.block_s
            return False, True
        return True, False


def decision_key(role, plan, decision, args):
    raw = json.dumps([role, plan, decision, args], sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RepeatGuard(object):
    def __init__(self, max_same=3, window_s=3600.0):
        self.max_same, self.window_s = max_same, window_s
        self.w = _Window()

    def allow(self, key, now):
        """Одинаковое решение: исполняется не более max_same раз за окно (hold не считается, его вызывающий не передаёт)."""
        if not self.max_same:
            return True
        if self.w.count(key, now, self.window_s) >= self.max_same:
            return False
        self.w.add(key, now, self.window_s)
        return True
