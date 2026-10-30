"""WP11-C2: раннер заданий на ФЕЙКОВОМ LLM и фейковом шлюзе: задание -> досье -> роль -> валидный ответ -> действие; повтор формата; запасные профили; бюджет;
«только защита»; сбой посреди задания; конкурентные задания; защита от повторов и зацикливания; утечки секретов."""
import json
import os
import subprocess
import sys
import threading
import unittest

import _bootstrap  # noqa: F401
from _brain import PLAN, BrainBase, FakeGateway, pm
from fakes.fake_llm import Boom, Err, Factory
from tb.brain import fallback, jobs, swap
from tb.core import bus
from tb.llm import usage as usage_mod


class HappyPath(BrainBase):
    def test_move_stop_job_to_gateway_and_journal(self):
        r = self.runner({"ds-pro": [pm("move_stop", {"to": "be"}, why="ушла +1R, фиксирую безубыток | px 146.9 | риск 0")]})
        s = self.emit()
        self.job("J-1", seqs=[s])
        self.assertTrue(r.step())
        j = self.get_job("J-1")
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["outcome"], "decision")
        c = self.gw.tool_calls("stop_plan")
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["args"]["to"], "be")
        self.assertEqual(c[0]["args"]["rev"], 3)
        self.assertEqual(c[0]["args"]["id"], PLAN)
        self.assertEqual(c[0]["args"]["event_seq"], s)
        self.assertEqual(c[0]["agent"], "api")
        self.assertEqual(c[0]["meta"]["profile"], "ds-pro")            # метаданные для тегов model. идут из раннера, не от модели
        self.assertEqual(c[0]["meta"]["role"], "position_manager")
        d = [x for x in self.decisions() if x["what"] == "brain_decision"]
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["actor"], "brain:api")
        self.assertEqual(d[0]["basis"]["profile"], "ds-pro")
        self.assertEqual(d[0]["basis"]["role"], "position_manager")
        self.assertGreater(d[0]["basis"]["usd"], 0)
        self.assertEqual(d[0]["res"]["code"], "QUEUED" if False else "OK")

    def test_dossier_uses_gateway_get_brief_and_get_events(self):
        s = self.emit()
        self.gw.events = [{"seq": s, "type": "entry_blocked", "d": {"why": "CHASE"}}, {"seq": s + 100, "type": "kill", "d": {}}]
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1", seqs=[s])
        r.step()
        req = self.factory.clients["ds-pro"].calls[0]["req"]
        txt = req.messages[0]["content"]
        self.assertIn("<brief>", txt)
        self.assertIn("CHASE", txt)
        self.assertNotIn('"kill"', txt)                      # чужие события в досье не попадают
        self.assertEqual(self.gw.tool_calls("get_brief")[0]["args"]["scope"], "event")

    def test_move_stop_number_formatting(self):
        r = self.runner({"ds-pro": [pm("move_stop", {"to": 146.25})]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.gw.tool_calls("stop_plan")[0]["args"]["to"], "146.25")

    def test_hold_is_noted_and_does_not_call_tools(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.gw.tool_calls("stop_plan") + self.gw.tool_calls("close_plan"), [])
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "hold")
        self.assertEqual([x["res"]["code"] for x in self.decisions()], ["HOLD"])

    def test_close_partial_cancel_amend_hold_release_map_to_tools(self):
        cases = [(pm("close"), "close_plan", {}), (pm("partial_close", {"frac": 0.5}), "close_plan", {"frac": 0.5}), (pm("cancel"), "cancel_plan", {}),
                 (pm("amend", {"patch": {"targets": {"t1": {"price": 148.4}}}}), "amend_plan", {"rev": 3}), (pm("hold_plan"), "hold_plan", {}),
                 (pm("release_plan"), "release_plan", {})]
        for i, (obj, tool, extra) in enumerate(cases):
            self.gw.calls[:] = []
            self.factory = None
            if tool == "cancel_plan":                   # WP11-C3: cancel допустим только пока позиции нет
                self.gw.brief["positions"] = []
            r = self.runner({"ds-pro": [obj]})
            self.job("J-m%d" % i, key="plan:X%d" % i)
            r.step()
            c = self.gw.tool_calls(tool)
            self.assertEqual(len(c), 1, tool)
            for k, v in extra.items():
                self.assertEqual(c[0]["args"][k], v)

    def test_escalate_emits_error_event_without_tool(self):
        r = self.runner({"ds-pro": [pm("escalate", {"reason_code": "NEED_OWNER"})]})
        self.job("J-1")
        r.step()
        self.assertEqual([e["d"]["code"] for e in self.bus_events("error")], ["NEED_OWNER"])
        self.assertEqual(self.gw.tool_calls("stop_plan"), [])

    def test_events_acked_after_done_and_not_before_open_jobs(self):
        a, b, c = self.emit(), self.emit(), self.emit()
        self.job("J-a", seqs=[a], key="plan:PL-1028-SOL-01", prio=2)
        self.job("J-b", seqs=[b], key="plan:PL-1028-ETH-01", plan="PL-1028-ETH-01", prio=0)
        self.job("J-c", seqs=[c], key="plan:PL-1028-BTC-01", plan="PL-1028-BTC-01", prio=1, not_before=self.t + 1000)
        r = self.runner({"ds-pro": [pm("hold", plan="PL-1028-ETH-01")]})
        self.assertTrue(r.step())                              # первым идёт приоритет 0 (J-b)
        self.assertEqual(self.get_job("J-b")["status"], "done")
        self.assertEqual(self.get_job("J-a")["status"], "new")
        self.assertEqual(bus.cursor_get("api", root=self.root)["acked"], 0)    # a < b не завершено: ack b нельзя
        r.factory = None
        self.factory.clients["ds-pro"].script = [pm("hold")]
        self.factory.clients["ds-pro"].n = 0
        self.assertTrue(r.step())
        self.assertEqual(self.get_job("J-a")["status"], "done")
        self.assertEqual(bus.cursor_get("api", root=self.root)["acked"], b)    # c ещё в очереди (not_before): ack до b включительно

    def test_no_role_for_event_type_is_skipped(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1", types=("scanner_signal",))
        r.step()
        self.assertEqual(self.get_job("J-1")["status"], "skipped")
        self.assertEqual(self.factory.calls(), 0)

    def test_reviewer_runs_tools_mode_on_position_closed(self):
        rev = {"schema": "tb.review/1", "journal_id": "BB-1", "errors": ["good"], "rule_followed": "yes", "lesson": "ок"}
        r = self.runner({"ds-flash": [rev]})
        self.job("J-1", types=("position_closed",))
        r.step()
        self.assertEqual(self.get_job("J-1")["status"], "done")
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["role"], "reviewer")
        req = self.factory.clients["ds-flash"].calls[0]["req"]
        self.assertIn("post_mortem", [t["name"] for t in req.tools])
        self.assertNotIn("submit_plan", [t["name"] for t in req.tools])       # инструменты вне роли модели не передаются

    def test_not_live_team_job_skipped(self):
        self.job("J-1", sub="apib")
        r = self.runner({"ds-pro": [pm("hold")]})
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["code"], "NOT_LIVE_TEAM")


class ToolFirst(BrainBase):
    """position_manager: действие вызовом pm_* исполняется сразу, объяснение приходит текстом постфактум и пишется как brain_why."""

    def test_tool_call_executes_then_why_is_recorded(self):
        from fakes.fake_llm import Tool
        r = self.runner({"ds-pro": [Tool("pm_move_stop", {"to": "be", "reason": "PROFIT_LOCK"}), '{"why": "ушла +1R | px 146.9 | риск 0"}']})
        s = self.emit()
        self.job("J-1", seqs=[s])
        self.assertTrue(r.step())
        j = self.get_job("J-1")
        self.assertEqual((j["status"], j["result"]["outcome"]), ("done", "decision"))
        c = self.gw.tool_calls("stop_plan")
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["args"]["to"], c[0]["args"]["rev"], c[0]["args"]["id"], c[0]["args"]["event_seq"]), ("be", 3, PLAN, s))    # plan и rev платформа подставила сама
        why = [x for x in self.decisions() if x["what"] == "brain_why"]
        self.assertEqual(len(why), 1)
        self.assertIn("+1R", why[0]["why"])
        self.assertEqual((why[0]["args"]["tool"], why[0]["args"]["decision"], why[0]["args"]["has_why"]), ("pm_move_stop", "move_stop", True))
        self.assertEqual(len(self.factory.clients["ds-pro"].calls), 2)           # вызов + текст с объяснением

    def test_action_happens_even_without_explanation(self):
        from fakes.fake_llm import Tool
        r = self.runner({"ds-pro": [Tool("pm_close", {"reason": "INVALIDATION"}), ""]})
        self.job("J-1")
        r.step()
        self.assertEqual(len(self.gw.tool_calls("close_plan")), 1)
        why = [x for x in self.decisions() if x["what"] == "brain_why"]
        self.assertEqual((why[0]["res"]["code"], why[0]["args"]["has_why"]), ("NO_WHY", False))

    def test_rejected_tool_call_is_not_executed_and_second_action_is_blocked(self):
        from fakes.fake_llm import Tool
        r = self.runner({"ds-pro": [Tool("pm_move_stop", {"to": "abc", "reason": "RISK"}), Tool("pm_hold", {"reason": "NO_ACTION"}), Tool("pm_close", {"reason": "RISK"}), '{"why": "держу"}']})
        self.job("J-1")
        r.step()
        self.assertEqual(self.gw.tool_calls("stop_plan") + self.gw.tool_calls("close_plan"), [])
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "hold")

    def test_old_json_answer_still_accepted(self):
        r = self.runner({"ds-pro": [pm("move_stop", {"to": "be"}, why="ушла +1R")]})
        self.job("J-1")
        r.step()
        self.assertEqual(len(self.gw.tool_calls("stop_plan")), 1)


class FormatHandling(BrainBase):
    def test_junk_around_json_accepted(self):
        r = self.runner({"ds-pro": ["вот ответ:\n```json\n" + json.dumps(pm("hold")) + "\n```"]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "hold")

    def test_invalid_then_valid_retries_with_codes_only(self):
        r = self.runner({"ds-pro": [pm("add"), pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "hold")
        c = self.factory.clients["ds-pro"]
        self.assertEqual(len(c.calls), 2)
        hint = c.calls[1]["req"].messages[-1]["content"]
        self.assertIn("CODES", hint)
        self.assertIn("BAD_ENUM", hint)
        self.assertEqual(len(c.format_errors), 1)

    def test_extra_key_size_rejected(self):
        bad = pm("hold", qty=5)
        r = self.runner({"ds-pro": [bad, bad, bad], "an-sonnet": [bad, bad, bad]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.gw.tool_calls("stop_plan"), [])
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "no_action")

    def test_args_must_match_decision(self):
        r = self.runner({"ds-pro": [pm("hold", {"to": 1}), pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertIn("ARGS_EXTRA:to", self.factory.clients["ds-pro"].format_errors[0])

    def test_invalid_all_attempts_then_fallback_once_then_no_action_bad_format(self):
        bad = "не json вообще"
        r = self.runner({"ds-pro": [bad], "an-sonnet": [bad]})
        self.job("J-1")
        r.step()
        j = self.get_job("J-1")
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["roles"][0]["code"], "BAD_FORMAT")
        self.assertEqual(len(self.factory.clients["ds-pro"].calls), 3)             # 1 + retries_format(2)
        self.assertEqual(len(self.factory.clients["an-sonnet"].calls), 3)          # запасной один раз, тоже с повторами формата
        self.assertNotIn("ds-flash", self.factory.clients)                         # третий в цепочке уже не пробуется
        self.assertEqual(self.gw.tool_calls("stop_plan") + self.gw.tool_calls("close_plan"), [])
        self.assertIn("BAD_FORMAT", [e["d"]["code"] for e in self.bus_events("error")])
        sk = [x for x in self.decisions() if x["kind"] == "skip"]
        self.assertEqual(sk[0]["res"]["code"], "BAD_FORMAT")
        self.assertFalse(r.health.protect_on)                                      # LLM жив: это не «только защита»

    def test_format_failure_on_primary_valid_on_fallback(self):
        r = self.runner({"ds-pro": ["мусор"], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        res = self.get_job("J-1")["result"]["roles"][0]
        self.assertEqual(res["outcome"], "hold")
        self.assertEqual(res["profile"], "an-sonnet")
        d = self.decisions()[0]
        self.assertEqual(d["basis"]["fallback_from"], "ds-pro")

    def test_plan_mismatch_is_format_error(self):
        r = self.runner({"ds-pro": [pm("hold", plan="PL-1028-ETH-01"), pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertIn("PLAN_MISMATCH", self.factory.clients["ds-pro"].format_errors[0])

    def test_stale_rev_redoes_dossier_once(self):
        r = self.runner({"ds-pro": [pm("close", rev=2), pm("close", rev=3)]})
        self.job("J-1")
        r.step()
        self.assertEqual(len(self.gw.tool_calls("get_brief")), 2)
        self.assertEqual(len(self.gw.tool_calls("close_plan")), 1)

    def test_stale_twice_no_action(self):
        r = self.runner({"ds-pro": [pm("close", rev=2)]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.gw.tool_calls("close_plan"), [])
        self.assertEqual([x["res"]["code"] for x in self.decisions()], ["STALE"])

    def test_gateway_reject_recorded_not_retried(self):
        self.gw.responses["stop_plan"] = {"ok": False, "status": "rejected", "errors": [{"code": "E_STOP_WIDEN", "msg": "x"}]}
        r = self.runner({"ds-pro": [pm("move_stop", {"to": 145.5})]})
        self.job("J-1")
        r.step()
        self.assertEqual(len(self.gw.tool_calls("stop_plan")), 1)
        d = self.decisions()[0]
        self.assertEqual((d["kind"], d["res"]["code"]), ("skip", "E_STOP_WIDEN"))
        self.assertEqual(self.get_job("J-1")["result"]["outcome"], "no_action")

    def test_dossier_failure_no_llm_call(self):
        self.gw.responses["get_brief"] = {"ok": False, "errors": [{"code": "E_INTERNAL"}]}
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.factory.calls(), 0)
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["code"], "DOSSIER")


class Fallbacks(BrainBase):
    def test_timeout_switches_to_fallback_and_cooldown_routes_next_jobs(self):
        r = self.runner({"ds-pro": [Err("timeout")], "an-sonnet": [pm("hold")]})
        self.job("J-1", key="plan:A1")
        r.step()
        j = self.get_job("J-1")["result"]["roles"][0]
        self.assertEqual((j["outcome"], j["profile"]), ("hold", "an-sonnet"))
        self.assertTrue(r.health.cooling("ds-pro", self.t))
        n_before = len(self.factory.clients["ds-pro"].calls)
        self.job("J-2", key="plan:A2")
        r.step()
        self.assertEqual(len(self.factory.clients["ds-pro"].calls), n_before)        # охлаждение: сразу на запасной
        self.assertEqual(self.get_job("J-2")["result"]["roles"][0]["profile"], "an-sonnet")
        self.assertEqual(r.health.last_fallback["kind"], "timeout")

    def test_return_to_primary_after_cooldown(self):
        r = self.runner({"ds-pro": [Err("server"), pm("hold")], "an-sonnet": [pm("hold")]})
        self.job("J-1", key="plan:A1")
        r.step()
        self.t += 2000
        self.job("J-2", key="plan:A2")
        r.step()
        self.assertEqual(self.get_job("J-2")["result"]["roles"][0]["profile"], "ds-pro")
        self.assertFalse(r.health.cooling("ds-pro", self.t))

    def test_each_trigger_kind_falls_back(self):
        for code in ("timeout", "network", "server", "rate", "auth"):
            self.factory = None
            self.gw.calls[:] = []
            r = self.runner({"ds-pro": [Err(code)], "an-sonnet": [pm("hold")]})
            self.job("J-%s" % code, key="plan:%s" % code)
            r.step()
            self.assertEqual(self.get_job("J-%s" % code)["result"]["roles"][0]["profile"], "an-sonnet", code)

    def test_all_profiles_down_protect_only_and_job_returns_to_queue(self):
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [Err("timeout")]})
        self.job("J-1")
        r.step()
        j = self.get_job("J-1")
        self.assertEqual(j["status"], "new")
        self.assertEqual(j["result"]["code"], "LLM_DOWN")
        self.assertEqual(j["attempts"], 0)                             # отложенное не тратит попытки
        self.assertTrue(r.health.protect_on)
        self.assertIn("BRAIN_DOWN", [e["d"]["code"] for e in self.bus_events("error")])
        self.assertEqual(r.snapshot()["health"], "red")
        self.assertEqual(self.gw.tool_calls("stop_plan"), [])

    def test_recovery_tick_leaves_protect_after_probe(self):
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [Err("timeout")]}, probe_fn=lambda pid, p: True)
        self.job("J-1")
        r.step()
        self.assertTrue(r.health.protect_on)
        self.t += 100
        self.assertTrue(r.recovery_tick(every_s=0))
        self.assertFalse(r.health.protect_on)
        self.assertIn("BRAIN_RECOVERED", [e["d"]["code"] for e in self.bus_events("error")])

    def test_live_role_fallback_without_live_ok_means_protect_only(self):
        self.brain_doc["profiles"]["an-sonnet"]["live_ok"] = False
        self.write_conf()
        self.set_platform("live")
        swap.record_probe(self.root, __import__("tb.brain.conf", fromlist=["x"]).load(self.root).profiles["an-sonnet"], True, self.t)
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["status"], "new")
        self.assertEqual(len(self.factory.clients.get("an-sonnet", __import__("types").SimpleNamespace(calls=[])).calls), 0)
        self.assertTrue(r.health.protect_on)

    def test_live_role_fallback_with_live_ok_and_probe_allowed(self):
        self.brain_doc["profiles"]["an-sonnet"]["live_ok"] = True
        self.write_conf()
        self.set_platform("live")
        swap.record_probe(self.root, __import__("tb.brain.conf", fromlist=["x"]).load(self.root).profiles["an-sonnet"], True, self.t)
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["profile"], "an-sonnet")

    def test_live_fallback_without_probe_is_refused(self):
        self.set_platform("live")
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["status"], "new")
        self.assertTrue(r.health.protect_on)

    def test_live_fallback_with_w2_waiver(self):
        self.brain_doc["profiles"]["an-sonnet"]["live_ok"] = False
        self.write_conf()
        self.set_platform("live", waivers=[{"id": "W2"}])
        swap.record_probe(self.root, __import__("tb.brain.conf", fromlist=["x"]).load(self.root).profiles["an-sonnet"], True, self.t)
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["profile"], "an-sonnet")

    def test_paper_mode_allows_fallback_without_live_ok(self):
        self.brain_doc["profiles"]["an-sonnet"]["live_ok"] = False
        self.write_conf()
        self.set_platform("paper")
        r = self.runner({"ds-pro": [Err("server")], "an-sonnet": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["profile"], "an-sonnet")


class Budgets(BrainBase):
    def spend(self, usd, n=1, provider="deepseek"):
        for _ in range(n):
            usage_mod.record({"job": "J-x", "role": "position_manager", "profile": "ds-pro"}, provider, "pro", None, usd, 5, True, None, 0, "stop", root=self.root, now=self.t)

    def test_day_cap_exhausted_enters_protect_only_and_defers(self):
        self.spend(2.5)
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        r.step()
        j = self.get_job("J-1")
        self.assertEqual((j["status"], j["result"]["code"]), ("new", "BUDGET"))
        self.assertEqual(self.factory.calls(), 0)
        self.assertTrue(r.health.protect_on)
        self.assertEqual(r.health.protect["code"], "brain_budget")
        self.assertIn("brain_budget", [e["d"]["which"] for e in self.bus_events("limit_hit")])

    def test_budget_restored_leaves_protect_only(self):
        self.spend(2.5)
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertTrue(r.health.protect_on)
        r.budget.limits["day_usd"] = 100.0
        r.budget.limits["hour_usd"] = 100.0
        self.assertTrue(r.recovery_tick())
        self.assertFalse(r.health.protect_on)
        self.t += 700
        self.assertTrue(r.step())
        self.assertEqual(self.get_job("J-1")["status"], "done")

    def test_provider_cap_exhausted_falls_back_to_other_provider(self):
        r = self.runner({"ds-pro": [pm("hold")], "an-sonnet": [pm("hold")]})
        r.budget.limits.setdefault("providers", {})["deepseek"] = {"day_usd": 0.01}
        self.spend(0.05)
        self.job("J-1")
        r.step()
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["profile"], "an-sonnet")
        self.assertEqual(len(self.factory.clients.get("ds-pro").calls) if "ds-pro" in self.factory.clients else 0, 0)

    def test_job_meter_exhausted_gives_no_action_not_protect(self):
        self.brain_doc["budget"]["job_usd"] = 0.0015
        self.write_conf()
        r = self.runner({"ds-pro": ["мусор"]}, )
        for c in (self.factory.clients,):
            pass
        self.job("J-1")
        r.step()
        j = self.get_job("J-1")
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["roles"][0]["code"], "JOB_BUDGET")
        self.assertFalse(r.health.protect_on)
        self.assertLessEqual(len(self.factory.clients["ds-pro"].calls), 3)

    def test_protect_only_blocks_submit_and_amend_but_allows_stop(self):
        r = self.runner({"ds-pro": [pm("amend", {"patch": {"x": 1}}), pm("move_stop", {"to": "be"})]})
        r.health.set_protect(True, "тест", "brain_down", self.t)
        self.job("J-1", key="plan:A1")
        r.step()
        self.assertEqual(self.gw.tool_calls("amend_plan"), [])
        self.assertEqual(self.decisions()[0]["res"]["code"], "PROTECT_ONLY")
        self.factory.clients["ds-pro"].n = 1
        self.job("J-2", key="plan:A2")
        r.step()
        self.assertEqual(len(self.gw.tool_calls("stop_plan")), 1)


class Resilience(BrainBase):
    def test_crash_mid_job_returns_to_queue_then_succeeds(self):
        self.gw.boom["stop_plan"] = RuntimeError("шлюз упал")
        r = self.runner({"ds-pro": [pm("move_stop", {"to": "be"})]})
        self.job("J-1")
        r.step()
        j = self.get_job("J-1")
        self.assertEqual((j["status"], j["attempts"]), ("new", 1))
        self.assertIn("JOB_ERROR", [e["d"]["code"] for e in self.bus_events("error")])
        self.assertEqual(bus.cursor_get("api", root=self.root)["acked"], 0)        # событие не подтверждено
        del self.gw.boom["stop_plan"]
        self.factory.clients["ds-pro"].n = 0
        self.t += 20
        r.reload()
        r.step()
        self.assertEqual(self.get_job("J-1")["status"], "done")
        self.assertEqual(self.get_job("J-1")["attempts"], 2)

    def test_llm_exception_attempts_exhausted_failed(self):
        r = self.runner({"ds-pro": [Boom()]})
        self.job("J-1")
        for i in range(5):
            r.step()
            self.t += 20
        self.assertEqual(self.get_job("J-1")["status"], "failed")

    def test_restart_does_not_lose_queue(self):
        self.job("J-1", key="plan:A1")
        self.job("J-2", key="plan:A2")
        j = jobs.pick(self.root, self.t, "brain")
        self.assertEqual(j["status"], "taken")
        # «процесс умер»: taken_by указывает на несуществующий pid
        d = self.get_job(j["id"])
        d["taken_by"] = 999999
        from tb.core.filelock import atomic_write_json
        atomic_write_json(os.path.join(jobs.jobs_dir(self.root), j["id"] + ".json"), d, indent=None)
        self.assertEqual(jobs.requeue_stale(self.root, self.t), 1)
        r = self.runner({"ds-pro": [pm("hold")]})
        self.assertEqual(r.drain(), 2)
        self.assertEqual(sorted(x["status"] for x in jobs.list_jobs(self.root)), ["done", "done"])

    def test_concurrent_workers_each_job_once(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        for i in range(8):
            self.job("J-%02d" % i, key="plan:PL-1028-SOL-%02d" % (i + 1), plan="PL-1028-SOL-%02d" % (i + 1))
        self.gw.brief = {"ok": True, "brief_id": "B-1", "plans": [], "positions": []}
        errs = []

        def work():
            try:
                while r.step():
                    pass
            except Exception as e:      # pragma: no cover
                errs.append(e)
        r.factory = None
        self.factory.scripts["ds-pro"] = [lambda req: json.dumps(pm("hold", plan=self._plan_in(req)))]
        th = [threading.Thread(target=work) for _ in range(3)]
        for t in th:
            t.start()
        for t in th:
            t.join(30)
        self.assertEqual(errs, [])
        rows = jobs.list_jobs(self.root)
        self.assertEqual([x["status"] for x in rows], ["done"] * 8)
        self.assertEqual(sorted(x["attempts"] for x in rows), [1] * 8)
        self.assertEqual(len([d for d in self.decisions() if d["what"] == "brain_decision"]), 8)

    @staticmethod
    def _plan_in(req):
        txt = req.messages[0]["content"]
        return json.loads(txt.split("<event>")[1].split("</event>")[0])["plan"]

    def test_same_key_jobs_not_run_concurrently(self):
        a = self.job("J-a", key="plan:K")
        b = self.job("J-b", key="plan:K")
        r = self.runner({"ds-pro": [pm("hold")]})
        j = jobs.pick(self.root, self.t, "brain")
        self.assertEqual(j["id"], "J-a")                                # J-b слит в J-a как дубль того же ключа
        self.assertEqual(self.get_job("J-b")["result"]["outcome"], "merged")
        self.assertEqual(sorted(self.get_job("J-a")["seqs"]), sorted(set(a["seqs"] + b["seqs"])))
        self.assertIsNone(jobs.pick(self.root, self.t, "brain", busy_keys={"plan:K"}))
        del r

    def test_hot_swap_between_jobs_current_job_finishes_on_old_model(self):
        r = self.runner({"ds-pro": [pm("hold")], "ds-flash": [pm("hold")]})
        self.set_platform("paper")
        self.job("J-1", key="plan:A1")
        self.job("J-2", key="plan:A2")
        done = {}

        def swap_during(client, req):
            if not done:
                done["r"] = swap.use(self.root, {"role": "A.position_manager", "profile": "ds-flash"}, self.t, by="test")
        self.runner_pro = self.factory("ds-pro", r.conf.profiles["ds-pro"])
        self.runner_pro.on_call = swap_during
        r.step()
        self.assertTrue(done["r"]["ok"], done["r"])
        self.assertEqual(self.get_job("J-1")["result"]["roles"][0]["profile"], "ds-pro")     # задание доделала старая модель
        self.assertEqual(self.get_job("J-1")["rev"], 0)
        r.step()
        self.assertEqual(self.get_job("J-2")["result"]["roles"][0]["profile"], "ds-flash")   # следующее на новой ревизии
        self.assertEqual(self.get_job("J-2")["rev"], 1)
        rows = [x for x in usage_mod.read_rows(self.root) if x.get("kind") != "fmt"]
        self.assertEqual([(x["profile"], x["rev"]) for x in rows], [("ds-pro", 0), ("ds-flash", 1)])

    def test_repeat_decision_guard(self):
        r = self.runner({"ds-pro": [pm("move_stop", {"to": "be"})]})
        for i in range(5):
            self.job("J-%d" % i, key="plan:R%d" % i)
            r.step()
        self.assertEqual(len(self.gw.tool_calls("stop_plan")), 3)                    # same_decision_max
        self.assertIn("REPEAT_DECISION", [e["d"]["code"] for e in self.bus_events("error")])

    def test_loop_guard_blocks_key(self):
        self.brain_doc["runtime"]["guards"]["loop_jobs_max"] = 3
        self.brain_doc["runtime"]["guards"]["jobs_total_per_hour"] = 100
        self.write_conf()
        r = self.runner({"ds-pro": [pm("hold")]})
        for i in range(5):
            self.job("J-%d" % i, key="plan:LOOP", seqs=[self.emit()])
            r.step()
        codes = [self.get_job("J-%d" % i)["result"].get("code") for i in range(5)]
        self.assertEqual(codes[:3], ["HOLD"] * 3)
        self.assertEqual(codes[3:], ["LOOP_BLOCKED"] * 2)
        self.assertIn("BRAIN_LOOP", [e["d"]["code"] for e in self.bus_events("error")])

    def test_jobs_per_hour_limit(self):
        self.brain_doc["runtime"]["guards"]["jobs_total_per_hour"] = 2
        self.write_conf()
        r = self.runner({"ds-pro": [pm("hold")]})
        for i in range(4):
            self.job("J-%d" % i, key="plan:L%d" % i)
            r.step()
        self.assertEqual([self.get_job("J-%d" % i)["status"] for i in range(4)], ["done", "done", "skipped", "skipped"])


class NoConfig(BrainBase):
    def test_missing_brain_json_defers_jobs_and_goes_protect(self):
        os.remove(self.p("config", "brain.json"))
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        self.assertTrue(r.step())
        j = self.get_job("J-1")
        self.assertEqual((j["status"], j["result"]["code"]), ("new", "NO_CONFIG"))
        self.assertTrue(r.health.protect_on)
        self.assertEqual(r.snapshot()["health"], "red")
        self.assertEqual(self.factory.calls(), 0)


class SecretsAndLayers(BrainBase):
    SECRET = "sk-ABCDEF0123456789SECRETKEY"

    def test_secret_in_error_and_why_never_reaches_files_or_output(self):
        why = "причина | ключ %s | риск" % self.SECRET
        r = self.runner({"ds-pro": [Err("auth", "bad key %s" % self.SECRET)], "an-sonnet": [pm("close", why=why[:200])]})        # WP11-C3: ответ с ключом отклоняется (SECRET_IN_ANSWER)
        self.job("J-1")
        import io
        from contextlib import redirect_stderr, redirect_stdout
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            r.step()
        blob = self.all_text() + o.getvalue() + e.getvalue() + json.dumps(self.gw.calls, ensure_ascii=False)
        self.assertNotIn(self.SECRET, self.all_text() + o.getvalue() + e.getvalue())
        self.assertNotIn(self.SECRET, json.dumps(self.gw.tool_calls("close_plan"), ensure_ascii=False))
        self.assertEqual(self.gw.tool_calls("close_plan"), [])              # ответ с ключом не исполняется: SECRET_IN_ANSWER -> повтор -> BAD_FORMAT
        self.assertEqual([d["res"]["code"] for d in self.decisions()], ["BAD_FORMAT"])

    def test_no_exchange_modules_and_no_bybit_env(self):
        code = ("import os,sys\n"
                "assert not [k for k in os.environ if k.upper().startswith('BYBIT')], 'env'\n"
                "import tb.brain.runner, tb.brain.api_daemon, tb.brain.brain_cli, tb.brain.swap, tb.brain.jobs, tb.brain.fallback, tb.gateway.rpc\n"
                "bad=[m for m in sys.modules if m.startswith(('tb.venues','tb.executor','tb.control','tb.launcher'))]\n"
                "print(bad)\nsys.exit(1 if bad else 0)\n")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("BYBIT", "ANTHROPIC"))}
        env["PYTHONPATH"] = os.path.join(_bootstrap.ROOT, "src")
        p = subprocess.run([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_runner_never_writes_secrets_into_environment(self):
        before = dict(os.environ)
        r = self.runner({"ds-pro": [pm("hold")]})
        self.job("J-1")
        r.step()
        self.assertEqual({k: v for k, v in os.environ.items() if k not in ("TB_ROOT", "TB_KEYS_DIR")}, {k: v for k, v in before.items() if k not in ("TB_ROOT", "TB_KEYS_DIR")})


if __name__ == "__main__":
    unittest.main()
