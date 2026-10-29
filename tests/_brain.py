"""Общие помощники тестов рантайма мозга (WP11-C2): временный корень с копиями конфигов, ПОДДЕЛЬНЫЙ ключ подписи, фейковый шлюз и фабрика фейковых LLM.
Реальные state/, config/, ~/.config/traider-bot, сеть и биржа не затрагиваются."""
import copy
import json
import os
import shutil
import tempfile
import time
import types
import unittest

import _bootstrap  # noqa: F401
from fakes.fake_llm import Factory
from tb import paths
from tb.brain import conf as conf_mod, jobs
from tb.brain.runner import Runner
from tb.core import bus

ROOT = _bootstrap.ROOT
FAKE_KEY_HEX = "cd" * 32
PLAN = "PL-1028-SOL-01"
GOOD_BRIEF = {"ok": True, "brief_id": "B-20261028-1015-01", "scope": "event", "plans": [{"id": PLAN, "rev": 3, "status": "managing", "sym": "SOL", "side": "long"}],
              "positions": [{"plan": PLAN, "sym": "SOL", "side": "long", "avg": 145.0, "px": 146.9, "sl": 143.2, "r": 1.0}]}      # WP11-C3: позиция есть, смысловая проверка pm/1 видит стоп и цену


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jdump(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def pm(decision="hold", args=None, plan=PLAN, rev=3, seq=1, why="причина | факт | риск", **kw):
    d = {"schema": "tb.pm/1", "event_seq": seq, "plan": plan, "rev": rev, "decision": decision, "args": args if args is not None else {}, "why": why, "conf": 0.7, "next": "event"}
    d.update(kw)
    return d


class FakeGateway(object):
    """Подмена tb.gateway.rpc.Gateway: журнал вызовов, ответы по инструментам, сбои по требованию."""

    def __init__(self, tool_names=None):
        from tb.gateway import catalog
        self.tool_names = list(tool_names or catalog.TOOLS)
        self.calls, self.responses, self.boom, self.events = [], {}, {}, []
        self.brief = copy.deepcopy(GOOD_BRIEF)

    def tools_for(self, agent_id):
        return [types.SimpleNamespace(name=n) for n in self.tool_names]

    def call(self, agent_id, tool, args=None, meta=None, cancel=None):
        self.calls.append({"agent": agent_id, "tool": tool, "args": dict(args or {}), "meta": dict(meta or {})})
        if tool in self.boom:
            raise self.boom[tool]
        r = self.responses.get(tool)
        if callable(r):
            r = r(args)
        if r is not None:
            return r
        if tool == "get_brief":
            return copy.deepcopy(self.brief)
        if tool == "get_events":
            return {"ok": True, "events": list(self.events), "last_seq": 99}
        return {"ok": True, "cmd_id": "C-20261028T101502-a1b2c3", "status": "done"}

    def tool_calls(self, tool):
        return [c for c in self.calls if c["tool"] == tool]


class BrainBase(unittest.TestCase):
    live_good = False

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbbrain_")
        self.keys = tempfile.mkdtemp(prefix="tbkeys_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.keys, True)
        if not hasattr(self, "_env"):      # setUp может вызываться повторно внутри одного теста: исходное окружение запоминаем ОДИН раз (иначе утечёт TB_ROOT)
            self._env = {k: os.environ.get(k) for k in ("TB_ROOT", "TB_KEYS_DIR")}
        os.environ["TB_ROOT"], os.environ["TB_KEYS_DIR"] = self.root, self.keys
        self.addCleanup(self._restore)
        jdump({"hmac_key": FAKE_KEY_HEX}, os.path.join(self.keys, "control.json"))
        os.makedirs(os.path.join(self.root, "config"))
        for n in ("risk_profile.json", "exec_limits.json", "risk_levels.json", "platform.json"):
            src = os.path.join(ROOT, "config", n)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(self.root, "config", n))
        shutil.copytree(os.path.join(ROOT, "config", "roles"), os.path.join(self.root, "config", "roles"))
        prov = jload(os.path.join(ROOT, "config", "providers.json"))
        for p in prov["providers"].values():
            p["enabled"] = True
        jdump(prov, os.path.join(self.root, "config", "providers.json"))
        self.brain_doc = self.make_brain_doc()
        self.write_conf()
        self.agents = self.make_agents()
        jdump({"agents": self.agents}, os.path.join(self.root, "config", "agents.json"))
        self.t = time.time()
        self.gw = FakeGateway()

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- конфигурация
    def make_brain_doc(self):
        d = jload(os.path.join(ROOT, "config", "brain.json"))
        d["teams"]["B"] = {"agent_id": "apib", "roles": {"morning_analyst": {"primary": "an-sonnet", "fallback": []}, "position_manager": {"primary": "an-sonnet", "fallback": []}}}
        d["profiles"]["ds-pro"]["live_ok"] = True
        d["profiles"]["ds-flash"]["live_ok"] = True
        d["runtime"]["workers"] = 2
        d["swap_rules"]["cooldown_min"] = 0
        return d

    def make_agents(self):
        base = {"role": "brain-api", "auth": "token", "enabled": True, "consumer": "api", "tools": ["@read", "@events.ack", "@plan.submit", "@plan.manage", "@idea.write", "@journal.post"]}
        return {"api": dict(base, limits={"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "modes": ["paper"], "max_size_mult": 1.0, "allow_add": False}),
                "apib": dict(base, limits={"max_risk_usdt": 0.5, "max_risk_pct": 2.0, "symbols": ["*"], "modes": ["paper", "live"], "max_size_mult": 1.0, "allow_add": False})}

    def write_conf(self):
        jdump(self.brain_doc, os.path.join(self.root, "config", "brain.json"))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def w(self, rel, obj):
        jdump(obj, self.p(*rel.split("/")))

    # ---- раннер
    def runner(self, scripts=None, gw=None, **kw):
        self.factory = Factory(scripts or {}, record_root=self.root)
        r = Runner(self.root, clock=lambda: self.t, client_factory=self.factory, gateway=gw or self.gw, **kw)
        return r

    # ---- события и задания
    def emit(self, typ="entry_blocked", d=None, plan=PLAN, sym="SOL", **kw):
        self.t += 0.001
        self._n = getattr(self, "_n", 0) + 1
        return bus.emit(typ, d or {"why": "CHASE", "attempt": self._n}, plan=plan, sym=sym, root=self.root, now=self.t, **kw)

    def job(self, jid, seqs=None, types=("entry_blocked",), prio=1, key=None, plan=PLAN, sub="api", not_before=0, **extra):
        seqs = seqs if seqs is not None else [self.emit()]
        j = {"id": jid, "sub": sub, "status": "new", "prio": prio, "key": key or ("plan:%s" % plan if plan else "type:x"), "plan": plan, "sym": "SOL",
             "ts_first": self.t, "ts_last": self.t, "not_before": not_before, "count": len(seqs), "seqs": seqs, "types": list(types), "kinds": [], "dropped_before": 0}
        j.update(extra)
        os.makedirs(jobs.jobs_dir(self.root), exist_ok=True)
        jdump(j, os.path.join(jobs.jobs_dir(self.root), jid + ".json"))
        return j

    def get_job(self, jid):
        return jload(os.path.join(jobs.jobs_dir(self.root), jid + ".json"))

    def decisions(self):
        from tb.core import decisions
        return decisions.read(0, limit=0, root=self.root)

    def bus_events(self, typ=None):
        return [e for e in bus.read(0, limit=500, root=self.root) if typ in (None, e["type"])]

    def set_platform(self, mode="live", state="running", day="D-20261028-1", waivers=None):
        self.w("state/control/platform.json", {"state": state, "mode": mode, "day_id": day, "live_waivers": waivers or []})

    def all_text(self):
        """Весь текст файлов корня (для проверки утечек секретов)."""
        out = []
        for d, _, fs in os.walk(self.root):
            for f in fs:
                try:
                    with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                        out.append(fh.read())
                except OSError:
                    pass
        return "\n".join(out)
