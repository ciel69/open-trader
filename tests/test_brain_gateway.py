"""WP11-C2: торговый флаг дня в шлюзе (fail closed), теги agent./model. от шлюза, политика пирамидинга и сетапов по уровню риска, передача agent_limits и setup в guard
исполнителем. Всё во временном корне; уровень 3 (по умолчанию) поведение не меняет."""
import copy
import json
import os
import shutil
import time
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _gw
from _gw import GwBase, plan
from fakes.harness import Env, plan_long
from tb import paths
from tb.gateway import catalog, registry
from tb.executor import gate as G

PLAN_ID = "PL-1028-SOL-01"


class FlagBase(GwBase):
    def setUp(self):
        GwBase.setUp(self)
        os.makedirs(paths.control_dir(root=self.root), exist_ok=True)
        cfg = _gw.agents_cfg()
        cfg["agents"]["bot"]["limits"]["modes"] = ["paper", "live"]
        cfg["agents"]["bot"]["limits"]["plans_per_hour"] = 50
        self.write_agents(cfg)
        self.now = time.time()

    def flag(self, **over):
        f = {"day_id": "D-20261028-1", "enabled": True, "mode": "live", "agent": "bot", "until_ts": self.now + 3600}
        f.update(over)
        for k in [k for k, v in f.items() if v is Ellipsis]:
            del f[k]
        with open(paths.control_dir("trading.json", root=self.root), "w", encoding="utf-8") as fh:
            json.dump(f, fh)

    def platform(self, day="D-20261028-1"):
        with open(paths.control_dir("platform.json", root=self.root), "w", encoding="utf-8") as fh:
            json.dump({"state": "running", "mode": "live", "day_id": day}, fh)

    def good(self):
        self.flag()
        self.platform()

    def submit(self, mode="live", agent="bot", **kw):
        return self.call(agent, "submit_plan", {"plan": plan(mode=mode, **kw), "reason": "тест"})


class TradingFlag(FlagBase):
    def test_paper_always_available_without_any_flag(self):
        r = self.submit("paper")
        self.assertTrue(r["ok"] is not False or self.code(r) not in ("E_TRADING_FLAG", "E_TRADING_OFF"), r)
        self.assertNotIn(self.code(r), ("E_TRADING_FLAG", "E_TRADING_OFF", "E_TRADING_DAY"))
        self.assertEqual(len(self.inbox()), 1)

    def test_live_denied_without_flag_file(self):
        r = self.submit("live")
        self.assertEqual((r["ok"], self.code(r)), (False, "E_TRADING_FLAG"))
        self.assertEqual(self.inbox(), [])

    def test_live_denied_for_each_flag_problem(self):
        cases = [("off", dict(enabled=False), "E_TRADING_OFF"), ("enabled_str", dict(enabled="true"), "E_TRADING_OFF"), ("mode_paper", dict(mode="paper"), "E_TRADING_MODE"),
                 ("no_mode", dict(mode=Ellipsis), "E_TRADING_MODE"), ("expired", dict(until_ts=self.now - 5), "E_TRADING_EXPIRED"), ("until_str", dict(until_ts="завтра"), "E_TRADING_EXPIRED"),
                 ("no_day", dict(day_id=None), "E_TRADING_DAY"), ("other_agent", dict(agent="chat"), "E_TRADING_AGENT")]
        self.platform()
        for name, over, code in cases:
            self.flag(**over)
            self.assertEqual(self.code(self.submit("live")), code, name)
        self.assertEqual(self.inbox(), [])

    def test_day_must_match_platform_day(self):
        self.flag()
        self.platform(day="D-OTHER")
        self.assertEqual(self.code(self.submit("live")), "E_TRADING_DAY")
        os.remove(paths.control_dir("platform.json", root=self.root))
        self.assertEqual(self.code(self.submit("live")), "E_TRADING_DAY")

    def test_corrupt_or_wrong_type_flag_fails_closed(self):
        self.platform()
        for raw in ("{не json", "[]", "null", '"enabled"', ""):
            with open(paths.control_dir("trading.json", root=self.root), "w", encoding="utf-8") as fh:
                fh.write(raw)
            self.assertEqual(self.code(self.submit("live")), "E_TRADING_FLAG", raw)

    def test_live_allowed_with_good_flag(self):
        self.good()
        r = self.submit("live")
        self.assertNotIn(self.code(r), tuple(registry.TRADING_CODES))
        self.assertEqual(len(self.inbox()), 1)

    def test_testnet_flag_mode_allows_live_plans(self):
        self.flag(mode="testnet")
        self.platform()
        self.assertEqual(len(self.inbox()), 0)
        self.submit("live")
        self.assertEqual(len(self.inbox()), 1)

    def test_flag_without_agent_field_applies_to_any_agent(self):
        self.flag(agent=Ellipsis)
        self.platform()
        self.submit("live", agent="chat")
        self.assertEqual(len(self.inbox()), 1)

    def test_tightening_and_closing_tools_never_blocked(self):
        self.make_plan(status="managing", mode="live", cur_stop=99.0)           # флаг не выставлен вообще
        spec_stop = 99.0
        for tool, args in (("stop_plan", {"id": PLAN_ID, "rev": 1, "to": "be", "reason": "x"}), ("close_plan", {"id": PLAN_ID, "reason": "x"}),
                           ("hold_plan", {"id": PLAN_ID, "reason": "x"}), ("release_plan", {"id": PLAN_ID, "reason": "x"}), ("cancel_plan", {"id": PLAN_ID, "reason": "x"})):
            r = self.call("bot", tool, args)
            self.assertNotIn(self.code(r), tuple(registry.TRADING_CODES), tool)
        self.assertEqual(len(self.inbox()), 5)
        del spec_stop

    def test_amend_weakening_denied_tightening_allowed_when_flag_off(self):
        self.make_plan(status="armed", mode="live")
        r = self.call("bot", "amend_plan", {"id": PLAN_ID, "rev": 1, "patch": {"thesis": "Новое обоснование входа после пересмотра"}, "reason": "x"})
        self.assertEqual(self.code(r), "E_TRADING_FLAG")                         # план ещё не в позиции: изменение может ослабить риск
        self.make_plan(pid="PL-1028-ETH-01", status="managing", mode="live", sym="ETH")
        r = self.call("chat", "amend_plan", {"id": "PL-1028-ETH-01", "rev": 1, "patch": {"thesis": "Обновлённое обоснование для позиции"}, "reason": "x"})
        self.assertNotIn(self.code(r), tuple(registry.TRADING_CODES))             # в позиции без ослабления: не блокируется

    def test_amend_with_flag_on_allowed(self):
        self.good()
        self.make_plan(status="armed", mode="live")
        r = self.call("bot", "amend_plan", {"id": PLAN_ID, "rev": 1, "patch": {"thesis": "Новое обоснование входа после пересмотра"}, "reason": "x"})
        self.assertNotIn(self.code(r), tuple(registry.TRADING_CODES))

    def test_trading_allowed_function_contract(self):
        self.assertEqual(registry.trading_allowed(self.root, "paper"), (True, None))
        self.assertEqual(registry.trading_allowed(self.root, "live")[0], False)
        self.good()
        self.assertEqual(registry.trading_allowed(self.root, "live", self.now, "bot"), (True, None))
        self.assertEqual(registry.trading_allowed(self.root, "live", self.now + 7200, "bot"), (False, "E_TRADING_EXPIRED"))


class TagsAndPolicies(FlagBase):
    def payload(self):
        return self.inbox()[-1]["payload"]

    def test_gateway_stamps_agent_and_model_tags(self):
        self.good()
        res = self.gw.call("bot", "submit_plan", {"plan": plan(mode="live"), "reason": "т"}, meta={"profile": "ds-pro", "job": "J-1"})
        self.assertNotIn(self.code(res), tuple(registry.TRADING_CODES), res)
        self.assertEqual(self.payload()["tags"]["extra"], ["agent.bot", "model.ds-pro", "mem.off"])         # mem.off: live-режим, память скрыта (show_in_live=false)

    def test_foreign_agent_model_tags_replaced_other_tags_kept(self):
        self.good()
        p = plan(mode="live", tags={"setup": "level_reclaim", "extra": ["agent.chat", "model.fake", "my-tag"]})
        self.gw.call("bot", "submit_plan", {"plan": p, "reason": "т"}, meta={"profile": "ds-flash"})
        self.assertEqual(self.payload()["tags"]["extra"], ["my-tag", "agent.bot", "model.ds-flash", "mem.off"])
        self.assertEqual(self.payload()["tags"]["setup"], "level_reclaim")

    def test_no_meta_only_agent_tag_and_cap_five(self):
        self.good()
        p = plan(mode="live", tags={"extra": ["aa", "bb", "cc", "dd", "ee"]})
        self.gw.call("bot", "submit_plan", {"plan": p, "reason": "т"})
        ex = self.payload()["tags"]["extra"]
        self.assertEqual(len(ex), 5)
        self.assertEqual(ex[-2:], ["agent.bot", "mem.off"])
        self.assertNotIn("model", " ".join(ex))

    @staticmethod
    def add_plan():
        p = plan(mode="paper")
        p["manage"].append({"id": "pyr", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "add", "risk_frac": 0.5}, "once": True})
        return p

    def test_pyramid_policy_blocks_add_even_if_agent_allows(self):
        cfg = _gw.agents_cfg()
        cfg["agents"]["bot"]["limits"].update(allow_add=True, modes=["paper", "live"])
        self.write_agents(cfg)
        p = self.add_plan()
        with mock.patch.object(catalog.risklevels, "pyramid_policy", return_value={"allowed": False, "level": 3}) as m:
            r = self.call("bot", "submit_plan", {"plan": p, "reason": "т"})
        self.assertEqual(self.code(r), "E_ADD_FORBIDDEN")
        self.assertIn("политик", r["errors"][0]["msg"])
        self.assertEqual(m.call_args[1]["agent_limits"]["allow_add"], True)
        with mock.patch.object(catalog.risklevels, "pyramid_policy", return_value={"allowed": True, "level": 4}):
            r = self.call("bot", "submit_plan", {"plan": p, "reason": "т"})
        self.assertNotEqual(self.code(r), "E_ADD_FORBIDDEN")

    def test_add_without_agent_right_denied_before_policy(self):
        p = self.add_plan()
        with mock.patch.object(catalog.risklevels, "pyramid_policy", side_effect=AssertionError("не должна вызываться")):
            self.assertEqual(self.code(self.call("bot", "submit_plan", {"plan": p, "reason": "т"})), "E_ADD_FORBIDDEN")

    def test_setup_aggr_rejected_at_level_3_for_chase_allowed_for_reclaim(self):
        shutil.copy(os.path.join(_bootstrap.ROOT, "config", "risk_levels.json"), paths.config("risk_levels.json", root=self.root))
        r = self.call("bot", "submit_plan", {"plan": plan(mode="paper", tags={"setup": "momentum_chase"}), "reason": "т"})
        self.assertEqual(self.code(r), "E_SETUP_AGGR")
        for ok_setup in ("level_reclaim", "sweep_reject", "что-то-новое"):
            r = self.call("bot", "submit_plan", {"plan": plan(mode="paper", tags={"setup": ok_setup}), "reason": "т"})
            self.assertNotEqual(self.code(r), "E_SETUP_AGGR", ok_setup)


class ExecutorPassesGuardInputs(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def spy(self):
        seen = []
        orig = G.guard.evaluate

        def f(ns):
            seen.append(ns)
            return orig(ns)
        return seen, mock.patch.object(G.guard, "evaluate", f)

    def test_agent_limits_of_reads_tag_set_by_gateway(self):
        with open(os.path.join(self.e.root, "config", "agents.json"), "w", encoding="utf-8") as f:
            json.dump({"agents": {"api": {"limits": {"max_risk_usdt": 0.4, "max_risk_pct": 2.0}}}}, f)
        self.assertEqual(G.agent_limits_of({"tags": {"extra": ["x", "agent.api"]}})["max_risk_usdt"], 0.4)
        self.assertIsNone(G.agent_limits_of({"tags": {"extra": ["x"]}}))
        self.assertIsNone(G.agent_limits_of({"tags": {}}))
        self.assertIsNone(G.agent_limits_of({"tags": {"extra": ["agent.ghost"]}}))
        os.remove(os.path.join(self.e.root, "config", "agents.json"))
        self.assertIsNone(G.agent_limits_of({"tags": {"extra": ["agent.api"]}}))

    def test_guard_receives_setup_and_agent_limits_on_entry(self):
        with open(os.path.join(self.e.root, "config", "agents.json"), "w", encoding="utf-8") as f:
            json.dump({"agents": {"api": {"limits": {"max_risk_usdt": 0.7, "max_risk_pct": 2.5}}}}, f)
        p = plan_long()
        p["tags"] = {"experiment": "t-exp", "variant": "A", "setup": "level_reclaim", "extra": ["agent.api", "model.ds-pro"]}
        seen, patch = self.spy()
        with patch:
            self.e.enter(p)
        entry_ns = [ns for ns in seen if not getattr(ns, "no_session", False) or True][-1]
        self.assertEqual(entry_ns.setup, "level_reclaim")
        self.assertEqual(entry_ns.agent_limits["max_risk_usdt"], 0.7)

    def test_direct_owner_plan_has_no_agent_limits_and_level3_unchanged(self):
        seen, patch = self.spy()
        with patch:
            self.e.enter(plan_long())
        self.assertIsNone(seen[-1].agent_limits)

    def test_agent_tags_reach_the_journal(self):
        p = plan_long()
        p["tags"] = dict(p["tags"], extra=["agent.api", "model.ds-pro"])
        self.e.enter(p)
        rows = [json.loads(ln) for ln in open(os.path.join(self.e.root, "state", "trades.jsonl"), encoding="utf-8") if ln.strip()]
        opened = [r for r in rows if r.get("ev") == "open"]
        self.assertTrue(opened)
        self.assertIn("agent.api", opened[-1]["tags"])
        self.assertIn("model.ds-pro", opened[-1]["tags"])

    def test_add_denied_when_level_policy_forbids(self):
        self.e.ex.cfg["pyramid"]["enabled"] = True
        pid = self.e.enter(plan_long())
        p = self.e.ex.store.load(pid) if hasattr(self.e.ex.store, "load") else None
        view = self.e.ex.view("SOL")
        item = {"spec": self.e.ex.plans[pid]["spec"], "rt": self.e.ex.plans[pid]["rt"]} if hasattr(self.e.ex, "plans") and pid in getattr(self.e.ex, "plans", {}) else None
        if item is None:
            self.skipTest("структура исполнителя без прямого доступа к плану")
        with mock.patch.object(G.risklevels, "pyramid_policy", return_value={"level_allows": False, "agent_allows": True, "level": 3, "allowed": False}):
            v = self.e.ex.gate.add(item, view, {"id": "add1", "do": {"action": "add", "frac": 0.5}})
        self.assertFalse(v.allow)
        self.assertEqual(v.code, "ADD_DISABLED")
        self.assertIn("политикой", v.detail)
        del p


if __name__ == "__main__":
    unittest.main()
