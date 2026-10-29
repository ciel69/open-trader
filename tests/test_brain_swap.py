"""WP11-C2: горячая смена моделей/команд (swap): подпись active.json, уровни T0/T1/T2, жёсткие условия и мягкие предупреждения, фраза, откат, журнал смен,
статус/проба для ctl и сквозной путь через настоящий ctl (challenge -> команда с nonce и фразами)."""
import json
import os
import unittest

import _bootstrap  # noqa: F401
import _ctl
from _brain import PLAN, BrainBase, jdump, jload
from tb.brain import conf as conf_mod, jobs, swap
from tb.control import ctlstate
from tb.core import bus, decisions


class SwapBase(BrainBase):
    def live_ready(self, rights=True):
        """Режим live; у api есть права live; проверки провайдеров свежие."""
        self.set_platform("live")
        if rights:
            self.agents["api"]["limits"]["modes"] = ["paper", "live"]
            jdump({"agents": self.agents}, self.p("config", "agents.json"))
        c = conf_mod.load(self.root)
        for p in c.profiles.values():
            swap.record_probe(self.root, p, True, self.t)

    def use(self, role="position_manager", profile="ds-flash", **kw):
        kw.setdefault("by", "test")
        return swap.use(self.root, {"role": role, "profile": profile, "accepted": kw.pop("accepted", [])}, self.t, **kw)

    def swap_live_ok(self, **kw):
        """Смена live-роли в live со всеми принятыми предупреждениями."""
        chk = swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)
        return self.use(accepted=[w["id"] for w in chk["warnings"]], phrase_ok=True, **kw)


class SignedState(SwapBase):
    def test_use_writes_signed_active_history_and_chain(self):
        r = self.use("shared.news_reader", "ds-pro")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["rev"], 1)
        o, st = swap.read_active(self.root)
        self.assertEqual(st, "ok")
        self.assertEqual(o["shared_roles"]["news_reader"]["primary"], "ds-pro")
        self.assertEqual(o["prev_rev"], 0)
        self.assertTrue(os.path.exists(self.p("state", "brain", "history", "R-1.json")))
        rows = swap.read_swaps(self.root)
        self.assertEqual((rows[0]["kind"], rows[0]["rev"], rows[0]["from"], rows[0]["to"], rows[0]["tier"]), ("use", 1, "ds-flash", "ds-pro", 0))
        self.assertTrue(swap.verify_swaps(self.root)["ok"])

    def test_swap_journal_tamper_detected(self):
        self.use("shared.news_reader", "ds-pro")
        self.use("shared.news_reader", "ds-flash")
        p = self.p("state", "brain", "swaps.jsonl")
        txt = open(p, encoding="utf-8").read().replace('"ds-pro"', '"ds-xxx"', 1)
        open(p, "w", encoding="utf-8").write(txt)
        v = swap.verify_swaps(self.root)
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad_seq"], 1)

    def test_tampered_active_json_is_ignored_and_config_wins(self):
        self.use("shared.news_reader", "ds-pro")
        o = jload(self.p("state", "brain", "active.json"))
        o["shared_roles"]["news_reader"]["primary"] = "an-sonnet"
        jdump(o, self.p("state", "brain", "active.json"))
        a = swap.load_effective(self.root)
        self.assertEqual(a.source, "untrusted")
        self.assertEqual(a.shared_roles["news_reader"]["primary"], "ds-flash")           # как в config/brain.json
        self.assertIn("не подписан", a.warn)
        self.assertEqual(swap.status(self.root)["source"], "untrusted")

    def test_unsigned_or_foreign_key_file_is_ignored(self):
        body = {"schema": swap.SCHEMA, "rev": 5, "live_team": "A", "teams": self.brain_doc["teams"], "shared_roles": self.brain_doc["shared_roles"]}
        jdump(body, self.p("state", "brain", "active.json"))
        self.assertEqual(swap.load_effective(self.root).source, "untrusted")
        jdump(swap.sign(body, bytes.fromhex("11" * 32)), self.p("state", "brain", "active.json"))
        self.assertEqual(swap.load_effective(self.root).source, "untrusted")
        jdump(swap.sign(body, bytes.fromhex("cd" * 32)), self.p("state", "brain", "active.json"))
        a = swap.load_effective(self.root)
        self.assertEqual((a.source, a.rev), ("active", 5))

    def test_garbage_active_json(self):
        with open(self.p("state", "brain", "active.json") if os.makedirs(self.p("state", "brain"), exist_ok=True) is None else "", "w") as f:
            f.write("{не json")
        self.assertEqual(swap.load_effective(self.root).source, "untrusted")

    def test_no_signing_key_refuses_swap(self):
        os.remove(os.path.join(self.keys, "control.json"))
        r = self.use("shared.news_reader", "ds-pro")
        self.assertEqual((r["ok"], r["code"]), (False, "E_SWAP_NO_KEY"))
        self.assertFalse(os.path.exists(self.p("state", "brain", "active.json")))

    def test_effective_cache_sees_swap_immediately(self):
        c = swap.EffectiveCache(self.root, clock=lambda: self.t)
        conf = conf_mod.load(self.root)
        self.assertEqual(c.get(conf).rev, 0)
        self.use("shared.news_reader", "ds-pro")
        self.assertEqual(c.get(conf).rev, 1)
        self.assertEqual(c.get(conf).chain("news_reader")[0][0], "ds-pro")


class Targets(SwapBase):
    def test_noop_and_unknown_targets(self):
        r = self.use("shared.news_reader", "ds-flash")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["changes"], [])
        self.assertFalse(os.path.exists(self.p("state", "brain", "active.json")))
        for role, prof, code in (("shared.nope", "ds-pro", "E_SWAP_TARGET"), ("Z.position_manager", "ds-pro", "E_SWAP_TARGET"), ("A.nope", "ds-pro", "E_SWAP_TARGET"),
                                 ("A.position_manager", "нет", "E_SWAP_PROFILE"), ("live", "Z", "E_SWAP_TARGET")):
            r = self.use(role, prof)
            self.assertEqual((r["ok"], r["code"]), (False, code), (role, prof))

    def test_team_and_agent_shortcuts(self):
        r = self.use("A", "ds-flash")                       # все роли команды
        self.assertTrue(r["ok"], r)
        a = swap.load_effective(self.root)
        self.assertEqual({v["primary"] for v in a.teams["A"]["roles"].values()}, {"ds-flash"})
        r = self.use("apib", "ds-pro")                      # по id агента
        self.assertTrue(r["ok"], r)
        self.assertEqual({v["primary"] for v in swap.load_effective(self.root).teams["B"]["roles"].values()}, {"ds-pro"})

    def test_unqualified_role_goes_to_live_team_or_shared(self):
        self.assertTrue(self.use("position_manager", "ds-flash")["ok"])
        self.assertTrue(self.use("reviewer", "ds-pro")["ok"])
        a = swap.load_effective(self.root)
        self.assertEqual(a.teams["A"]["roles"]["position_manager"]["primary"], "ds-flash")
        self.assertEqual(a.shared_roles["reviewer"]["primary"], "ds-pro")

    def test_new_primary_removed_from_fallback(self):
        self.use("A.position_manager", "an-sonnet")
        ch = swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]
        self.assertEqual(ch, {"primary": "an-sonnet", "fallback": []})

    def test_tools_role_needs_tool_capable_model(self):
        prov = jload(self.p("config", "providers.json"))
        prov["providers"]["deepseek"]["models"]["pro"]["tools"] = False
        jdump(prov, self.p("config", "providers.json"))
        r = self.use("shared.reviewer", "ds-pro")
        self.assertEqual((r["code"], "инструмент" in r["msg"]), ("E_SWAP_PROFILE", True))
        self.assertTrue(self.use("shared.news_reader", "ds-pro")["ok"])                  # decision_json без инструментов подходит

    def test_disabled_provider_refused(self):
        prov = jload(self.p("config", "providers.json"))
        prov["providers"]["anthropic"]["enabled"] = False
        jdump(prov, self.p("config", "providers.json"))
        self.assertEqual(self.use("A.position_manager", "an-sonnet")["code"], "E_SWAP_PROFILE")

    def test_change_in_non_live_mode_needs_no_phrase_even_for_live_roles(self):
        for mode in ("observe", "paper", "testnet"):
            self.set_platform(mode)
            r = self.use("A.position_manager", "ds-flash" if mode != "paper" else "an-sonnet")
            self.assertTrue(r["ok"], (mode, r))
            self.assertEqual(r["data"]["tier"], 0)
        self.set_platform("live", state="stopped")                         # остановленная платформа = не live
        self.assertTrue(self.use("A.position_manager", "ds-pro")["ok"])


class LiveGates(SwapBase):
    def setUp(self):
        SwapBase.setUp(self)
        self.live_ready()

    def hard(self, **over):
        return {h["id"]: h["ok"] for h in swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)["hard"]}

    def test_baseline_tier1_all_green_with_four_soft_warnings(self):
        c = swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)
        self.assertEqual((c["tier"], c["ok"], c["danger"]), (1, True, True))
        self.assertEqual(sorted(h["id"] for h in c["hard"]), ["E_SWAP_AMBIGUOUS_STATE", "E_SWAP_LIMIT_DAY", "E_SWAP_NO_LIVE_RIGHTS", "E_SWAP_PROBE"])
        self.assertTrue(all(h["ok"] for h in c["hard"]))
        self.assertEqual([w["id"] for w in c["warnings"]], ["W_SWAP_PAPER_HISTORY", "W_SWAP_NO_AB"])       # live_ok есть, cooldown 0
        self.assertEqual(c["phrases"], ["СМЕНИТЬ МОДЕЛЬ position_manager ds-pro -> ds-flash"])

    def test_phrase_required_then_warnings_accepted_one_by_one(self):
        r = self.use()
        self.assertEqual(r["code"], "E_SWAP_PHRASE")
        self.assertEqual(len(r["data"]["phrases"]), 1)
        r = self.use(phrase_ok=True)
        self.assertEqual(r["code"], "E_WARN")
        r = self.use(phrase_ok=True, accepted=["W_SWAP_NO_AB"])
        self.assertEqual(r["code"], "E_WARN_CHANGED")
        self.assertFalse(os.path.exists(self.p("state", "brain", "active.json")))
        r = self.use(phrase_ok=True, accepted=["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY"])
        self.assertTrue(r["ok"], r)
        row = swap.read_swaps(self.root)[-1]
        self.assertEqual(row["tier"], 1)
        self.assertEqual(sorted(w["id"] for w in row["warnings_accepted"]), ["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY"])
        self.assertTrue(row["phrase_ok"])
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "ds-flash")

    def test_hard_no_live_rights_each_cause(self):
        for mut in (lambda a: a["limits"].update(modes=["paper"]), lambda a: a.update(autonomy={"live": False}), lambda a: a.update(enabled=False),
                    lambda a: a["limits"].update(max_risk_usdt=0), lambda a: a["limits"].update(max_size_mult=2.0), lambda a: a["limits"].pop("max_risk_pct")):
            self.live_ready()
            ag = json.loads(json.dumps(self.agents))
            mut(ag["api"])
            jdump({"agents": ag}, self.p("config", "agents.json"))
            h = self.hard()
            self.assertFalse(h["E_SWAP_NO_LIVE_RIGHTS"])
            self.assertTrue(all(v for k, v in h.items() if k != "E_SWAP_NO_LIVE_RIGHTS"), h)
            r = self.use(phrase_ok=True, accepted=["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY"])
            self.assertEqual(r["code"], "E_SWAP_NO_LIVE_RIGHTS")

    def ambiguous_causes(self):
        def inbox():
            os.makedirs(self.p("state", "plans", "_inbox"), exist_ok=True)
            jdump({"op": "stop"}, self.p("state", "plans", "_inbox", "C-1.json"))

        def plan(rt, status="managing"):
            os.makedirs(self.p("state", "plans", PLAN), exist_ok=True)
            jdump({"id": PLAN, "symbol": "SOL"}, self.p("state", "plans", PLAN, "spec.json"))
            jdump(dict({"status": status}, **rt), self.p("state", "plans", PLAN, "runtime.json"))
        yield "inbox", inbox
        yield "queue", lambda: self.job("J-1")
        yield "draft", lambda: plan({}, "draft")
        yield "placing", lambda: plan({"entry": {"state": "placing"}})
        yield "blocked", lambda: plan({"entry": {"state": "blocked"}})
        yield "pending", lambda: plan({"actions": {"pending": ["a-1"]}})
        yield "hold_brain", lambda: plan({"hold": "brain"})
        yield "unacked", lambda: self.emit("kill", {"state": "on", "flatten": False}, plan=None, sym=None)
        yield "hold_reasons", lambda: self.w("runtime/exec_health.json", {"ts_epoch": self.t, "alive": True, "hold_reasons": ["blind"]})

    def test_hard_ambiguous_state_each_cause_alone(self):
        for name, setup in self.ambiguous_causes():
            self.setUp()
            setup()
            h = self.hard()
            self.assertFalse(h["E_SWAP_AMBIGUOUS_STATE"], name)
            self.assertTrue(all(v for k, v in h.items() if k != "E_SWAP_AMBIGUOUS_STATE"), (name, h))
            self.assertEqual(self.use(phrase_ok=True, accepted=["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY"])["code"], "E_SWAP_AMBIGUOUS_STATE", name)

    def test_hard_probe_stale_or_missing(self):
        os.remove(self.p("state", "brain", "probe.json"))
        h = self.hard()
        self.assertFalse(h["E_SWAP_PROBE"])
        self.live_ready()
        self.t += 400                                         # проверка старше 300 с
        self.assertFalse(self.hard()["E_SWAP_PROBE"])

    def test_hard_limit_per_day(self):
        for i in range(3):
            swap.append_swap(self.root, {"rev": i + 1, "kind": "use", "tier": 1, "target": ["x"], "from": "a", "to": "b", "by": "t", "phrase_ok": True, "gates": [],
                                         "warnings_accepted": [], "result": "ok"}, self.t)
        h = self.hard()
        self.assertFalse(h["E_SWAP_LIMIT_DAY"])
        self.assertEqual(self.use(phrase_ok=True, accepted=["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY", "W_SWAP_COOLDOWN"])["code"], "E_SWAP_LIMIT_DAY")

    def test_soft_cooldown_and_not_live_ok(self):
        self.brain_doc["swap_rules"]["cooldown_min"] = 240
        self.brain_doc["profiles"]["ds-flash"]["live_ok"] = False
        self.write_conf()
        swap.append_swap(self.root, {"rev": 1, "kind": "use", "tier": 1, "target": ["x"], "from": "a", "to": "b", "by": "t", "phrase_ok": True, "gates": [],
                                     "warnings_accepted": [], "result": "ok"}, self.t - 3600)
        ids = [w["id"] for w in swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)["warnings"]]
        self.assertEqual(ids, ["W_SWAP_PAPER_HISTORY", "W_SWAP_NOT_LIVE_OK", "W_SWAP_COOLDOWN", "W_SWAP_NO_AB"])

    def test_soft_paper_history_counts_agent_tag_days_and_violations(self):
        rows = []
        for i in range(30):
            rows.append({"ev": "open", "id": "P%d" % i, "mode": "paper", "tags": "bybit,paper,plan,agent.api,model.ds-pro"})
            rows.append({"ev": "close", "id": "P%d" % i, "ts_close": "2026-10-%02dT10:00:00Z" % (10 + i % 8)})
        rows.append({"ev": "open", "id": "X", "mode": "paper", "tags": "agent.other"})
        rows.append({"ev": "close", "id": "X", "ts_close": "2026-10-11T10:00:00Z"})
        self.w("state/trades.jsonl", "")
        open(self.p("state", "trades.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
        h = swap.history_ok(self.root, "api", ["ds-flash"], 30, 7, self.t)
        self.assertEqual((h["trades"], h["days"], h["violations"], h["ok"]), (30, 8, 0, True))
        self.assertNotIn("W_SWAP_PAPER_HISTORY", [w["id"] for w in swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)["warnings"]])
        decisions.append({"actor": "brain:api", "kind": "reject", "what": "stop", "res": {"ok": False, "code": "STOP_NOT_TIGHTER"}}, root=self.root)
        decisions.append({"actor": "brain:other", "kind": "reject", "what": "stop", "res": {"ok": False, "code": "STOP_NOT_TIGHTER"}}, root=self.root)
        h = swap.history_ok(self.root, "api", ["ds-flash"], 30, 7, self.t)
        self.assertEqual((h["violations"], h["ok"]), (1, False))
        self.assertTrue(os.path.exists(self.p("state", "brain", "history_ok.json")))
        self.assertIn("W_SWAP_PAPER_HISTORY", [w["id"] for w in swap.check(self.root, {"role": "position_manager", "profile": "ds-flash"}, self.t)["warnings"]])

    def test_tier2_team_swap_phrase_and_agent_rights(self):
        c = swap.check(self.root, {"role": "live", "team": "B"}, self.t)
        self.assertEqual((c["tier"], c["ok"]), (2, True), c)
        self.assertEqual(c["phrases"], ["СМЕНИТЬ ТОРГОВОГО АГЕНТА A -> B"])
        r = swap.use(self.root, {"role": "live", "team": "B"}, self.t, by="test")
        self.assertEqual(r["code"], "E_SWAP_PHRASE")
        acc = [w["id"] for w in c["warnings"]]
        r = swap.use(self.root, {"role": "live", "team": "B", "accepted": acc}, self.t, by="test", phrase_ok=True)
        self.assertTrue(r["ok"], r)
        st = swap.status(self.root)
        self.assertEqual((st["live_team"], st["agent"]), ("B", "apib"))
        self.assertEqual(swap.swaps_today(self.root, self.t), 1)

    def test_tier2_requires_rights_of_new_agent(self):
        self.agents["apib"]["limits"]["modes"] = ["paper"]
        jdump({"agents": self.agents}, self.p("config", "agents.json"))
        c = swap.check(self.root, {"role": "live", "team": "B"}, self.t)
        self.assertEqual((c["ok"], c["code"]), (False, "E_SWAP_NO_LIVE_RIGHTS"))

    def test_tier2_same_team_is_noop(self):
        c = swap.check(self.root, {"role": "live", "team": "A"}, self.t)
        self.assertTrue(c["ok"] and c.get("noop"))

    def test_shared_news_reader_in_live_needs_nothing(self):
        r = swap.use(self.root, {"role": "shared.news_reader", "profile": "ds-pro"}, self.t, by="test")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["tier"], 0)

    def test_shared_day_auditor_is_live_role(self):
        c = swap.check(self.root, {"role": "shared.day_auditor", "profile": "ds-flash"}, self.t)
        self.assertEqual(c["tier"], 1)

    def test_bus_event_emitted_when_type_known(self):
        # тип brain_swap давно в шине (bus.EVENT_TYPES/D_FIELDS): событие пишется; ничего в модуле bus не меняем и не «чистим»
        self.assertIn("brain_swap", bus.EVENT_TYPES)
        self.assertTrue(swap._emit_swap(self.root, "use", "a", "b", 3, "owner", self.t))
        self.assertEqual(bus.read(0, root=self.root)[-1]["type"], "brain_swap")

    def test_bus_event_skipped_quietly_when_type_unknown(self):
        from unittest import mock
        with mock.patch.dict(bus.EVENT_TYPES):          # patch.dict возвращает таблицу как была, даже при падении
            bus.EVENT_TYPES.pop("brain_swap")
            self.assertFalse(swap._emit_swap(self.root, "use", "a", "b", 3, "owner", self.t))
        self.assertIn("brain_swap", bus.EVENT_TYPES)


class Rollback(SwapBase):
    def test_rollback_restores_previous_and_toggles(self):
        self.use("A.position_manager", "ds-flash")
        self.use("A.position_manager", "an-sonnet")
        r = swap.rollback(self.root, {}, self.t, by="test")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["data"]["rev"], r["data"]["to"]), (3, 1))
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "ds-flash")
        r = swap.rollback(self.root, {}, self.t, by="test")                         # откат отката: возвращает ревизию 2
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "an-sonnet")
        self.assertEqual(swap.read_swaps(self.root)[-1]["kind"], "rollback")

    def test_rollback_to_explicit_revision(self):
        self.use("A.position_manager", "ds-flash")
        self.use("A.position_manager", "an-sonnet")
        self.use("shared.news_reader", "ds-pro")
        self.assertTrue(swap.rollback(self.root, {"to": 1}, self.t, by="t")["ok"])
        a = swap.load_effective(self.root)
        self.assertEqual((a.teams["A"]["roles"]["position_manager"]["primary"], a.shared_roles["news_reader"]["primary"]), ("ds-flash", "ds-flash"))

    def test_rollback_without_history(self):
        r = swap.rollback(self.root, {}, self.t)
        self.assertEqual((r["ok"], r["code"]), (False, "E_SWAP_ROLLBACK"))
        self.use("A.position_manager", "ds-flash")
        r = swap.rollback(self.root, {}, self.t)                                     # предыдущая = конфигурация brain.json (rev 0)
        self.assertEqual(r["code"], "E_SWAP_ROLLBACK")
        self.assertEqual(swap.rollback(self.root, {"to": 77}, self.t)["code"], "E_SWAP_ROLLBACK")

    def test_rollback_refused_when_profile_vanished(self):
        self.use("A.position_manager", "an-sonnet")
        self.use("A.position_manager", "ds-flash")
        del self.brain_doc["profiles"]["an-sonnet"]
        self.brain_doc["teams"]["B"]["roles"] = {}
        self.brain_doc["teams"]["A"]["roles"]["morning_analyst"]["fallback"] = []
        self.write_conf()
        r = swap.rollback(self.root, {}, self.t)
        self.assertEqual((r["ok"], r["code"]), (False, "E_SWAP_PROFILE"))
        self.assertEqual(swap.load_effective(self.root).rev, 2)                      # состояние не тронуто

    def test_rollback_in_live_needs_no_phrase_or_gates(self):
        self.use("A.position_manager", "ds-flash")
        self.set_platform("live")
        self.assertEqual(swap.classify(self.root, {}, self.t, "rollback"), {"danger": True, "phrases": [], "warnings": [], "blocked": False, "gates": [], "tier": 1, "code": "OK"})
        self.job("J-open")                                                             # даже при очереди откат возможен (возврат на известную ревизию)
        self.use("shared.news_reader", "ds-pro")                                       # ревизия 2 (T0 даже в live); откат возвращает ревизию 1
        r = swap.rollback(self.root, {}, self.t, by="t")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["to"], 1)

    def test_history_keeps_last_50(self):
        for i in range(55):
            self.use("shared.news_reader", "ds-pro" if i % 2 == 0 else "ds-flash")
        names = os.listdir(self.p("state", "brain", "history"))
        self.assertEqual(len(names), 50)
        self.assertIn("R-55.json", names)
        self.assertNotIn("R-1.json", names)


class Reload(SwapBase):
    def test_reload_signal_file_and_journal(self):
        r = swap.reload(self.root, {}, self.t, by="t")
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.exists(swap.reload_req_file(self.root)))
        self.assertEqual(swap.read_swaps(self.root)[-1]["kind"], "reload")
        self.assertFalse(os.path.exists(self.p("state", "brain", "active.json")))

    def test_reload_from_config_makes_new_rev(self):
        self.use("A.position_manager", "an-sonnet")
        r = swap.reload(self.root, {"from_config": True}, self.t, by="t")
        self.assertTrue(r["ok"], r)
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "ds-pro")
        self.assertEqual(swap.load_effective(self.root).rev, 2)

    def test_reload_from_config_refused_in_live_when_it_changes_live_roles(self):
        self.use("A.position_manager", "an-sonnet")
        self.set_platform("live")
        r = swap.reload(self.root, {"from_config": True}, self.t, by="t")
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))


class StatusAndProbe(SwapBase):
    def test_status_contract(self):
        st = swap.status(self.root)
        self.assertEqual((st["live_team"], st["agent"], st["rev"], st["source"]), ("A", "api", 0, "config"))
        self.assertEqual(st["profiles"]["position_manager"], "ds-pro")
        self.assertEqual(st["profiles"]["news_reader"], "ds-flash")
        self.assertTrue(st["live_ok"]["ds-pro"]["ok"])
        self.assertIsNone(st["last_swap_ts"])
        self.set_platform("live")
        self.live_ready()
        self.swap_live_ok()
        st = swap.status(self.root)
        self.assertEqual(st["rev"], 1)
        self.assertIsNotNone(st["last_swap_ts"])
        self.use("shared.news_reader", "ds-pro")                                     # T0 «последней смены live» не двигает
        self.assertEqual(swap.status(self.root)["last_swap_ts"], st["last_swap_ts"])

    def test_probe_h6_states(self):
        from tb.platform import services as S
        self.assertFalse(swap.probe(self.root, self.t)["ok"])                       # нет heartbeat
        S.write_hb("brain", {}, self.root, now=self.t)
        self.assertTrue(swap.probe(self.root, self.t)["ok"])
        S.write_hb("brain", {"protect_only": True, "reason": "бюджет"}, self.root, now=self.t)
        p = swap.probe(self.root, self.t)
        self.assertFalse(p["ok"])
        self.assertIn("только защита", p["msg"])
        S.write_hb("brain", {}, self.root, now=self.t)
        self.agents["api"]["enabled"] = False
        jdump({"agents": self.agents}, self.p("config", "agents.json"))
        self.assertFalse(swap.probe(self.root, self.t)["ok"])

    def test_probe_no_usable_profile_and_budget(self):
        from tb.platform import services as S
        S.write_hb("brain", {}, self.root, now=self.t)
        prov = jload(self.p("config", "providers.json"))
        prov["providers"]["deepseek"]["enabled"] = False
        prov["providers"]["anthropic"]["enabled"] = False
        jdump(prov, self.p("config", "providers.json"))
        self.assertFalse(swap.probe(self.root, self.t)["ok"])
        prov["providers"]["deepseek"]["enabled"] = True
        prov["budget"] = {"day_usd": 0.001}
        jdump(prov, self.p("config", "providers.json"))
        from tb.llm import usage as U
        U.record({"job": "J-x"}, "deepseek", "pro", None, 0.5, 1, True, None, 0, "stop", root=self.root, now=self.t)
        p = swap.probe(self.root, self.t)
        self.assertEqual((p["ok"], "бюджет" in p["msg"]), (False, True))


class ThroughCtl(unittest.TestCase):
    """Настоящий ctl: challenge -> команда с nonce, фразами и принятыми предупреждениями; откат; перезагрузка."""

    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.good_live()
        self.c = self.e.ctl(reconf=lambda p, o, t: (True, "OK"))
        self.e.w("config/agents.json", {"agents": {
            "api": {"role": "brain-api", "enabled": True, "consumer": "api", "tools": ["@read"],
                    "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "modes": ["paper", "live"], "max_size_mult": 1.0, "allow_add": False}}}})
        self.e.w("state/control/platform.json", {"state": "running", "mode": "live", "day_id": "D-1"})
        c = conf_mod.load(self.e.root)
        for p in c.profiles.values():
            swap.record_probe(self.e.root, p, True, _ctl.NOW)
        if bus.last_seq(self.e.root):
            bus.ack("api", bus.last_seq(self.e.root), root=self.e.root)       # события подготовки окружения (смена уровня риска) обработаны

    def test_t0_swap_one_command_no_nonce(self):
        self.e.w("state/control/platform.json", {"state": "stopped", "mode": "observe"})
        r, _ = self.e.send(self.c, "brain.use", {"role": "shared.news_reader", "profile": "ds-pro"})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        self.assertEqual(r["data"]["rev"], 1)
        self.assertEqual(swap.load_effective(self.e.root).shared_roles["news_reader"]["primary"], "ds-pro")
        self.assertEqual(swap.status(self.e.root)["profiles"]["news_reader"], "ds-pro")

    def test_live_swap_challenge_phrases_warnings_and_execution(self):
        args = {"role": "A.position_manager", "profile": "ds-flash"}
        ch = self.e.challenge(self.c, "brain.use", args)
        d = ch["data"]
        self.assertEqual((ch["status"], ch["code"]), ("done", "OK"), ch)
        self.assertTrue(d["danger"] and d["nonce"])
        self.assertEqual(d["phrases"][0], "СМЕНИТЬ МОДЕЛЬ position_manager ds-pro -> ds-flash")
        self.assertEqual(d["phrases"][-1], "я принимаю риск")
        self.assertEqual(sorted(w["id"] for w in d["warnings"]), ["W_SWAP_NO_AB", "W_SWAP_PAPER_HISTORY"])
        self.e.advance(3)
        r, _ = self.e.send(self.c, "brain.use", args, nonce=d["nonce"], phrases=d["phrases"])               # предупреждения не приняты
        self.assertEqual(r["code"], "E_WARN")
        r = self.e.confirmed(self.c, "brain.use", args)
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        a = swap.load_effective(self.e.root)
        self.assertEqual(a.teams["A"]["roles"]["position_manager"]["primary"], "ds-flash")
        self.assertEqual(swap.read_swaps(self.e.root)[-1]["by"], "ctl")
        self.assertEqual(len(swap.read_swaps(self.e.root)[-1]["warnings_accepted"]), 2)

    def test_live_swap_wrong_phrase_and_no_nonce(self):
        args = {"role": "A.position_manager", "profile": "ds-flash"}
        d = self.e.challenge(self.c, "brain.use", args)["data"]
        self.e.advance(3)
        a = dict(args, accepted=[w["id"] for w in d["warnings"]])
        r, _ = self.e.send(self.c, "brain.use", a, nonce=d["nonce"], phrases=["СМЕНИТЬ МОДЕЛЬ"])
        self.assertEqual(r["code"], "E_PHRASE")
        r, _ = self.e.send(self.c, "brain.use", a)
        self.assertEqual(r["code"], "E_NONCE")
        self.assertEqual(swap.load_effective(self.e.root).rev, 0)

    def test_live_swap_blocked_by_hard_gate_in_challenge(self):
        jobs_dir = jobs.jobs_dir(self.e.root)
        os.makedirs(jobs_dir)
        jdump({"id": "J-1", "status": "new", "seqs": [1]}, os.path.join(jobs_dir, "J-1.json"))
        ch = self.e.challenge(self.c, "brain.use", {"role": "A.position_manager", "profile": "ds-flash"})
        self.assertEqual((ch["status"], ch["code"]), ("rejected", "E_GATE"))
        self.assertIsNone(ch["data"]["nonce"])
        self.assertEqual([g["id"] for g in ch["data"]["gates"] if not g["ok"]], ["E_SWAP_AMBIGUOUS_STATE"])

    def test_team_swap_phrase_is_swap_rules_template(self):
        self.e.w("config/agents.json", {"agents": {k: dict(v, limits=dict(v["limits"], modes=["paper", "live"])) for k, v in {
            "api": {"role": "x", "enabled": True, "consumer": "api", "tools": ["@read"], "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "max_size_mult": 1.0}},
            "apib": {"role": "x", "enabled": True, "consumer": "api", "tools": ["@read"], "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "max_size_mult": 1.0}}}.items()}})
        doc = jload(os.path.join(self.e.root, "config", "brain.json"))
        doc["teams"]["B"] = {"agent_id": "apib", "roles": {"position_manager": {"primary": "ds-flash", "fallback": []}}}
        jdump(doc, os.path.join(self.e.root, "config", "brain.json"))
        ch = self.e.challenge(self.c, "brain.use", {"role": "live", "team": "B"})
        self.assertEqual(ch["data"]["phrases"][0], "СМЕНИТЬ ТОРГОВОГО АГЕНТА A -> B")

    def test_rollback_and_reload_through_ctl(self):
        self.e.w("state/control/platform.json", {"state": "stopped", "mode": "observe"})
        self.e.send(self.c, "brain.use", {"role": "shared.news_reader", "profile": "ds-pro"})
        r, _ = self.e.send(self.c, "brain.use", {"role": "shared.news_reader", "profile": "an-sonnet"})
        self.assertEqual(r["code"], "OK")
        r, _ = self.e.send(self.c, "brain.rollback", {})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        self.assertEqual(swap.load_effective(self.e.root).shared_roles["news_reader"]["primary"], "ds-pro")
        r, _ = self.e.send(self.c, "brain.reload", {})
        self.assertEqual(r["code"], "OK")
        self.assertTrue(os.path.exists(swap.reload_req_file(self.e.root)))
        r, _ = self.e.send(self.c, "brain.rollback", {"to": 99})
        self.assertEqual(r["code"], "E_SWAP_ROLLBACK")

    def test_status_in_ctl_shows_team_and_profiles(self):
        s = self.c.status()
        self.assertEqual(s["team"]["live_team"], "A")
        self.assertEqual(s["team"]["profiles"]["position_manager"], "ds-pro")
        self.assertTrue(s["team"]["available"])


if __name__ == "__main__":
    unittest.main()


class ChainWithBrokenFallback(SwapBase):
    """Дашборд, смена основной модели роли: уже стоящий запасной профиль с выключенным провайдером не блокирует смену основного (была ошибка E_SWAP_PROFILE
    на любую попытку переключить position_manager, пока в запасных стоял an-sonnet при выключенном anthropic). Новый запасной с таким провайдером отклоняется."""

    def disable_anthropic(self):
        prov = jload(self.p("config", "providers.json"))
        prov["providers"]["anthropic"]["enabled"] = False
        jdump(prov, self.p("config", "providers.json"))

    def test_primary_change_allowed_with_disabled_fallback(self):
        self.disable_anthropic()
        ch = swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]
        self.assertIn("an-sonnet", ch["fallback"])
        r = swap.set_chain(self.root, {"role": "A.position_manager", "primary": "ds-flash"}, self.t, by="test")
        self.assertTrue(r["ok"], r)
        ch = swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]
        self.assertEqual((ch["primary"], ch["fallback"]), ("ds-flash", ["an-sonnet"]))        # запасной остаётся как был

    def test_new_fallback_with_disabled_provider_refused(self):
        self.disable_anthropic()
        swap.set_chain(self.root, {"role": "A.position_manager", "primary": "ds-flash", "fallback": []}, self.t, by="test")
        r = swap.set_chain(self.root, {"role": "A.position_manager", "fallback": ["an-sonnet"]}, self.t + 1, by="test")
        self.assertEqual(r["code"], "E_SWAP_PROFILE")
        self.assertIn("an-sonnet", r["msg"])
        self.assertIn("anthropic", r["msg"])
