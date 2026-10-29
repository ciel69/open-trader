"""Общие помощники тестов шлюза (WP11-A): временный TB_ROOT с реестром агентов, подмена оценки риска и капитала. В реальный state/ ничего не пишется."""
import copy
import json
import os
import shutil
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.brain import plan_cli
from tb.core import plan_schema
from tb.gateway import catalog
from tb.gateway.rpc import Gateway

ALL_TOOLS = ["@read", "@events.ack", "@plan.submit", "@plan.manage", "@idea.write", "@journal.post"]


def agents_cfg():
    return {"agents": {
        "chat": {"role": "brain-chat", "auth": "local", "tools": ALL_TOOLS,
                 "limits": {"max_risk_usdt": 2.0, "max_risk_pct": 10.0, "symbols": ["*"], "modes": ["paper", "live"], "calls_per_min": 500, "plans_per_hour": 50}},
        "bot": {"role": "t", "auth": "token", "tools": ALL_TOOLS,
                "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 5.0, "symbols": ["SOL", "ETH"], "modes": ["paper"], "calls_per_min": 500, "plans_per_hour": 2}},
        "ro": {"role": "ro", "auth": "local", "tools": ["@read"], "limits": {"symbols": ["*"], "modes": ["paper"], "calls_per_min": 5}},
    }}


def plan(mode="paper", sym="SOL", **kw):
    p = copy.deepcopy(plan_cli.TEMPLATES["scalp"])
    p["symbol"] = sym
    p["mode"] = mode
    p.pop("source", None)
    p["source"] = {"kind": "brain"}
    p.update(kw)
    return p


class GwBase(unittest.TestCase):
    risk = 0.5
    equity = 30.0

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbgw_")
        self._env = dict(os.environ)
        os.environ["TB_ROOT"] = self.root
        os.environ["TB_KEYS_DIR"] = os.path.join(self.root, "keys")
        os.makedirs(paths.config(root=self.root))
        self.write_agents(agents_cfg())
        self._orig = (catalog._preview, catalog._equity)
        catalog._preview = lambda plan, root: {"ok": True, "risk_usdt": self.risk, "leverage": 5, "margin": 3.0}
        catalog._equity = lambda: self.equity
        self.gw = Gateway(self.root)

    def tearDown(self):
        catalog._preview, catalog._equity = self._orig
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.root, ignore_errors=True)

    def write_agents(self, cfg):
        with open(paths.config("agents.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def write_flag(self, agent=None, day="D-1"):
        """Торговый флаг дня (WP11-C2): live-планы через шлюз возможны только при нём."""
        d = paths.state("control", root=self.root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "trading.json"), "w", encoding="utf-8") as f:
            json.dump({"day_id": day, "enabled": True, "mode": "live", "agent": agent, "until_ts": time.time() + 3600}, f)
        with open(os.path.join(d, "platform.json"), "w", encoding="utf-8") as f:
            json.dump({"state": "running", "mode": "live", "day_id": day}, f)

    def call(self, agent, tool, args=None):
        return self.gw.call(agent, tool, args)

    def code(self, res):
        return (res.get("errors") or [{}])[0].get("code")

    def inbox(self):
        d = paths.plans_inbox(root=self.root)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return []
        out = []
        for n in names:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                out.append(json.load(f))
        return out

    def make_plan(self, pid="PL-1028-SOL-01", status="managing", sym="SOL", mode="paper", cur_stop=None):
        """Кладёт spec/runtime плана в state/plans (как это делает исполнитель)."""
        spec, _ = plan_schema.normalize(plan(mode=mode, sym=sym))
        spec.update({"id": pid, "rev": 1, "created_ts": time.time(), "created_by": "brain:api"})
        d = paths.plans(pid, root=self.root)
        os.makedirs(d)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)
        rt = {"status": status, "pos": {"cur_stop": cur_stop or spec["stop"]["price"], "avg": spec["entry"]["ref_price"]}}
        with open(os.path.join(d, "runtime.json"), "w", encoding="utf-8") as f:
            json.dump(rt, f)
        return spec
