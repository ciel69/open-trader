"""Диспетчер шлюза: агент -> право -> лимит частоты -> схема входа -> обработчик -> фильтр выхода -> аудит (WP11-A).

Ответ всегда dict: {"ok": true|false|null, ...поля инструмента} либо отказ шлюза {"ok": false, "status": "denied"|"error", "errors": [{"code","path","msg"}]}.
Коды отказа шлюза: E_AGENT_UNKNOWN, E_AGENT_DISABLED, E_AUTH*, E_TOOL_UNKNOWN, E_TOOL_DENIED, E_RATE_LIMIT, E_ARGS, E_INTERNAL, а также коды ограничений
(E_SYMBOL_FORBIDDEN, E_MODE_FORBIDDEN, E_SIZE_MULT, E_RISK_LIMIT, E_RISK_PCT, E_RISK_UNKNOWN, E_ADD_FORBIDDEN, E_RATE_PLANS, E_KILL).
Тексты исключений наружу не уходят (только имя класса): токены и содержимое файлов не могут просочиться.
"""
import collections
import threading
import time

from tb import paths
from tb.brain import common
from tb.gateway import audit, catalog, registry


def _denied(code, msg, path="", status="denied"):
    return {"ok": False, "status": status, "errors": [{"code": code, "path": path, "msg": common.clean_text(msg, 200)}]}


class Gateway(object):
    def __init__(self, root=None, clock=time.time):
        self.root = root or paths.root()
        self.clock = clock
        self._calls = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def agents(self):
        return registry.load(catalog.TOOLS, self.root)

    def agent(self, agent_id):
        return self.agents().get(agent_id)

    def tools_for(self, agent_id):
        a = self.agent(agent_id)
        return [catalog.TOOLS[n] for n in (a.tools if a and a.enabled else []) if n in catalog.TOOLS]

    def _rate_ok(self, agent):
        now = self.clock()
        with self._lock:
            q = self._calls[agent.id]
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= agent.limits["calls_per_min"]:
                return False
            q.append(now)
            return True

    def call(self, agent_id, tool, args=None, via="inproc", cancel=None, meta=None):
        t0 = time.time()
        res = self._call(agent_id, tool, args, via, cancel, meta)
        errs = res.get("errors") or [{}]
        code = errs[0].get("code") if res.get("ok") is not True and errs else None
        if res.get("ok") is None:
            code = "QUEUED"
        audit.record(agent_id, tool, args, res.get("ok"), code, (time.time() - t0) * 1000.0, via=via, root=self.root, now=t0)
        return res

    def _call(self, agent_id, tool, args, via, cancel, meta=None):
        agent = self.agent(agent_id) if isinstance(agent_id, str) else None
        if agent is None:
            return _denied("E_AGENT_UNKNOWN", "агент не зарегистрирован")
        if not agent.enabled:
            return _denied("E_AGENT_DISABLED", "агент отключён")
        t = catalog.TOOLS.get(tool) if isinstance(tool, str) else None
        if t is None:
            return _denied("E_TOOL_UNKNOWN", "неизвестный инструмент", "tool")
        if tool not in agent.tools:
            return _denied("E_TOOL_DENIED", "инструмент недоступен этому агенту (право %s)" % t.right, "tool")
        if not self._rate_ok(agent):
            return _denied("E_RATE_LIMIT", "превышен лимит %d вызовов в минуту" % agent.limits["calls_per_min"])
        clean, errs, dropped = catalog.validate(t.schema, args)
        if clean is None:
            return _denied("E_ARGS", "; ".join(errs), "args", "rejected")
        ctx = catalog.Ctx(self.root, agent, now=self.clock(), cancel=cancel, meta=meta)
        try:
            res = t.handler(ctx, clean)
        except catalog.ToolError as e:
            res = {"ok": False, "status": e.status, "errors": [{"code": e.code, "path": e.path, "msg": e.msg}]}
        except Exception as e:      # текст исключения наружу не отдаём
            return _denied("E_INTERNAL", "внутренняя ошибка: %s" % e.__class__.__name__, status="error")
        out = catalog.finish(t, res)
        if dropped:
            out["dropped_fields"] = dropped
        return out


_DEFAULT = {}


def _gw(root=None):
    r = root or paths.root()
    if r not in _DEFAULT:
        _DEFAULT[r] = Gateway(r)
    return _DEFAULT[r]


def call(agent_id, tool, args=None, root=None, cancel=None, meta=None):
    """In-process вызов (API-демон WP11-C): gateway.call("api", "get_brief", {"scope": "start"}). meta (профиль, задание, роль) ставит раннер, не модель:
    шлюз пишет из него теги agent./model. в план."""
    return _gw(root).call(agent_id, tool, args, cancel=cancel, meta=meta)


def tools_for(agent_id, root=None):
    return _gw(root).tools_for(agent_id)
