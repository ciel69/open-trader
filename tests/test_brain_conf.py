"""WP11-C2: реестр ролей и config/brain.json (загрузка с проверкой), схемы ответов, секреты в тексте, защиты от зацикливания, очередь заданий."""
import copy
import json
import os
import shutil
import threading
import unittest

import _bootstrap  # noqa: F401
from _brain import PLAN, BrainBase, jdump, jload, pm
from tb.brain import conf as conf_mod, guards, jobs, roles_runtime, validate
from tb.core import bus
from tb.llm import providers, schema as schema_mod

ROOT = _bootstrap.ROOT


class RealConfig(unittest.TestCase):
    def test_shipped_brain_json_valid_with_real_providers(self):
        reg = providers.load(ROOT)
        c = conf_mod.load(ROOT, reg, conf_mod.read_agents(ROOT))
        self.assertEqual(c.errors, [])
        self.assertEqual(c.live_team, "A")
        self.assertFalse(c.compare["enabled"])
        self.assertEqual(sorted(c.profiles), ["an-sonnet", "ds-flash", "ds-pro"])
        self.assertEqual(c.teams["A"]["agent_id"], "api")
        self.assertTrue(all(not p.live_ok for p in c.profiles.values()))          # live_ok ставит только владелец

    def test_shipped_roles_valid(self):
        r = roles_runtime.load(ROOT)
        self.assertEqual(r.errors, [])
        self.assertEqual(sorted(r.roles), ["day_auditor", "entry_scout", "morning_analyst", "news_reader", "position_manager", "reviewer"])
        for role in r.roles.values():
            self.assertTrue(role.prompt.lstrip().startswith("# prompt v%d" % role.version))
            self.assertIn("JSON", role.system)

    def test_system_prompt_is_stable_and_has_no_dynamic_parts(self):
        a, b = roles_runtime.load(ROOT).get("position_manager"), roles_runtime.load(ROOT).get("position_manager")
        self.assertEqual(a.system, b.system)
        self.assertEqual(a.prompt_hash, b.prompt_hash)
        import re
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d|J-\d{8}", a.system))

    def test_roles_wake_on_routes(self):
        r = roles_runtime.load(ROOT)
        self.assertEqual([x.id for x in r.for_event_types(["milestone"])], ["position_manager"])
        self.assertEqual([x.id for x in r.for_event_types(["position_closed"])], ["reviewer"])
        self.assertEqual([x.id for x in r.for_event_types(["scanner_signal"])], ["entry_scout"])
        self.assertEqual(r.for_event_types(["scanner_signal"], kinds=["idea_near"]), [])         # idea_* роль не будят
        self.assertEqual([x.id for x in r.for_event_types(["scanner_signal"], kinds=["hot_enter"])], ["entry_scout"])

    def test_agents_of_shipped_config_are_paper_only(self):
        self.assertEqual(conf_mod.read_agents(ROOT)["api"]["limits"]["modes"], ["paper"])


class ConfValidation(BrainBase):
    def load(self):
        return conf_mod.load(self.root, providers.load(self.root), conf_mod.read_agents(self.root))

    def test_unknown_profile_in_chain_drops_role(self):
        self.brain_doc["teams"]["A"]["roles"]["position_manager"]["fallback"] = ["нет-такого"]
        self.write_conf()
        c = self.load()
        self.assertNotIn("position_manager", c.teams["A"]["roles"])
        self.assertTrue(any("запасной профиль неизвестен" in e for e in c.errors))

    def test_unknown_provider_model_disables_profile(self):
        self.brain_doc["profiles"]["bad"] = {"provider": "nope", "model": "x"}
        self.brain_doc["profiles"]["bad2"] = {"provider": "deepseek", "model": "nomodel"}
        self.write_conf()
        c = self.load()
        self.assertNotIn("bad", c.profiles)
        self.assertNotIn("bad2", c.profiles)
        self.assertEqual(len([e for e in c.errors if "отключён" in e]), 2)

    def test_bad_params(self):
        self.brain_doc["profiles"]["ds-flash"]["params"]["thinking"] = "max"
        self.brain_doc["profiles"]["ds-pro"]["params"]["temperature"] = 9
        self.write_conf()
        c = self.load()
        self.assertEqual(sorted(c.profiles), ["an-sonnet"])

    def test_live_team_must_exist(self):
        self.brain_doc["trading"]["live_team"] = "Z"
        self.write_conf()
        c = self.load()
        self.assertIsNone(c.live_team)
        self.assertTrue(c.errors)

    def test_compare_enabled_is_only_a_warning_and_starts_nothing(self):
        self.brain_doc["trading"]["compare"] = {"enabled": True, "teams": ["B"]}
        self.write_conf()
        c = self.load()
        self.assertTrue(c.compare["enabled"])
        self.assertTrue(any("отложены" in w for w in c.warnings))
        self.assertEqual(c.live_team, "A")

    def test_compare_team_with_live_right_is_config_error(self):
        self.brain_doc["trading"]["compare"] = {"enabled": False, "teams": ["B"]}
        self.write_conf()
        c = self.load()
        self.assertTrue(any(e.startswith("COMPARE_LIVE_RIGHT") for e in c.errors))

    def test_chain_without_live_ok_is_warning(self):
        self.brain_doc["profiles"]["ds-pro"]["live_ok"] = False
        self.write_conf()
        c = self.load()
        self.assertTrue(any("без live_ok" in w for w in c.warnings))

    def test_shared_role_in_team_rejected(self):
        self.brain_doc["teams"]["A"]["roles"]["news_reader"] = {"primary": "ds-flash", "fallback": []}
        self.write_conf()
        self.assertTrue(any("не командная" in e for e in self.load().errors))

    def test_broken_file_does_not_raise(self):
        with open(self.p("config", "brain.json"), "w") as f:
            f.write("{не json")
        c = conf_mod.load(self.root)
        self.assertFalse(c.loaded)
        self.assertTrue(c.errors)

    def test_missing_file(self):
        os.remove(self.p("config", "brain.json"))
        self.assertFalse(conf_mod.load(self.root).loaded)

    def test_defaults_filled(self):
        d = {"version": 1, "profiles": self.brain_doc["profiles"], "teams": self.brain_doc["teams"], "trading": {"live_team": "A"}}
        c = conf_mod.parse(d)
        self.assertEqual(c.swap_rules["max_swaps_day"], 3)
        self.assertEqual(c.runtime["guards"]["same_decision_max"], 3)


class RoleValidation(BrainBase):
    def role_files(self, rid, **over):
        j = jload(self.p("config", "roles", rid + ".json"))
        j.update(over)
        jdump(j, self.p("config", "roles", rid + ".json"))

    def test_bad_version_line_disables_role(self):
        with open(self.p("config", "roles", "reviewer.md"), "w", encoding="utf-8") as f:
            f.write("без версии\n")
        r = roles_runtime.load(self.root)
        self.assertNotIn("reviewer", r.roles)
        self.assertTrue(any("prompt v" in e for e in r.errors))

    def test_unknown_tool_schema_event_disable_role(self):
        self.role_files("reviewer", tools=["rm_rf"])
        self.role_files("position_manager", output_schema="x/1")
        self.role_files("morning_analyst", wake_on=["нет_события"])
        r = roles_runtime.load(self.root)
        for rid in ("reviewer", "position_manager", "morning_analyst"):
            self.assertNotIn(rid, r.roles)
        self.assertEqual(len(r.errors), 3)
        self.assertIn("news_reader", r.roles)               # остальные работают

    def test_id_must_match_file(self):
        self.role_files("reviewer", id="other")
        self.assertNotIn("reviewer", roles_runtime.load(self.root).roles)

    def test_numeric_ranges(self):
        self.role_files("news_reader", max_rounds=0)
        self.assertNotIn("news_reader", roles_runtime.load(self.root).roles)

    def test_missing_md(self):
        os.remove(self.p("config", "roles", "day_auditor.md"))
        self.assertNotIn("day_auditor", roles_runtime.load(self.root).roles)

    def test_no_roles_dir(self):
        shutil.rmtree(self.p("config", "roles"))
        r = roles_runtime.load(self.root)
        self.assertEqual(r.roles, {})
        self.assertTrue(r.errors)


class Schemas(unittest.TestCase):
    def ok(self, obj, sid="pm/1"):
        return schema_mod.validate(validate.SCHEMAS[sid], obj) + validate.semantic(sid, obj, {"plan": PLAN})

    def test_pm_good_cases(self):
        for obj in (pm("hold"), pm("move_stop", {"to": "be"}), pm("move_stop", {"to": 146.2}), pm("partial_close", {"frac": 0.5}), pm("close"),
                    pm("amend", {"patch": {"targets": {"t1": {"price": 148.4}}}}), pm("escalate", {"reason_code": "DATA_GAP"}), pm("cancel"), pm("hold_plan"), pm("release_plan")):
            self.assertEqual(self.ok(obj), [], obj["decision"])

    def test_pm_regression_bad_answers_rejected(self):
        bad = [pm("add"), pm("hold", qty=1), pm("hold", leverage=5), pm("hold", why="x" * 201), pm("hold", why=""), pm("move_stop", {}), pm("move_stop", {"to": 1, "frac": 0.2}),
               pm("partial_close", {"frac": 2}), pm("partial_close", {"frac": 0.01}), pm("amend", {}), pm("escalate", {"reason_code": "XX"}), pm("close", {"to": 1}),
               pm("hold", plan="plan-1"), pm("hold", conf=2), pm("hold", next="never"), pm("hold", schema="tb.pm/2"), {k: v for k, v in pm("hold").items() if k != "why"}]
        for obj in bad:
            self.assertTrue(self.ok(obj), obj)

    def test_pm_plan_mismatch_semantic(self):
        self.assertIn("PLAN_MISMATCH", validate.semantic("pm/1", pm("hold", plan="PL-1028-ETH-01"), {"plan": PLAN}))

    def test_two_json_objects_and_prose_rejected_by_extract(self):
        o = json.dumps(pm("hold"))
        self.assertEqual(schema_mod.check_text(o + " " + o, validate.SCHEMAS["pm/1"])[0]["decision"], "hold")      # первый сбалансированный объект
        self.assertEqual(schema_mod.check_text("просто слова", validate.SCHEMAS["pm/1"])[1], ["NO_JSON"])
        self.assertEqual(schema_mod.check_text(o[:-5], validate.SCHEMAS["pm/1"])[1], ["TRUNCATED"])
        dup = o[:-1] + ',"conf":0.1}'
        self.assertEqual(schema_mod.check_text(dup, validate.SCHEMAS["pm/1"])[1], ["DUP_KEY"])

    def test_dayplan_stub_and_review_stub(self):
        dp = {"schema": "tb.dayplan/1", "regime": "unclear", "bias": "neutral", "session": {"type": "intraday", "minutes": 360}, "watch": [], "allowed_setups": [], "avoid": [],
              "limits": {"max_new_plans": 0, "max_risk_usdt": 0, "symbols": [], "size_mult": 0}, "carry": {"allow": False, "max_positions": 0}, "carried_review": [], "risks": [], "questions": [],
              "why": "мало данных"}
        self.assertEqual(self.ok(dp, "dayplan/1"), [])
        dp2 = dict(dp, limits=dict(dp["limits"], max_new_plans=9))
        self.assertTrue(self.ok(dp2, "dayplan/1"))
        rv = {"schema": "tb.review/1", "journal_id": "BB-1", "errors": ["chase"], "rule_followed": "no", "lesson": "урок"}
        self.assertEqual(self.ok(rv, "review/1"), [])
        self.assertTrue(self.ok(dict(rv, errors=["придумал"]), "review/1"))

    def test_redact_cuts_key_like_strings(self):
        for s in ("ключ sk-ABCDEFGH12345678 тут", "tok_" + "a1" * 12, "x" * 40, "hex " + "ab" * 20):
            self.assertIn("[скрыто]", validate.redact(s))
            self.assertTrue(validate.has_secret(s))
        self.assertEqual(validate.redact("обычный текст PL-1028-SOL-01 R=1.02"), "обычный текст PL-1028-SOL-01 R=1.02")


class Guards(unittest.TestCase):
    def test_job_rate_total_and_per_sub(self):
        g = guards.JobRate(total_per_hour=3, per_sub=2, critical=5)
        self.assertEqual([g.allow("api", 1, 0)[0], g.allow("api", 1, 1)[0], g.allow("api", 1, 2)], [True, True, (False, "JOBS_SUB")])
        self.assertTrue(g.allow("api", 0, 3)[0])                                  # критичные считаются отдельно
        self.assertEqual(g.allow("x", 1, 4), (False, "JOBS_TOTAL"))
        self.assertTrue(g.allow("x", 1, 3700)[0])                                  # окно часа

    def test_loop_guard_blocks_then_releases(self):
        g = guards.LoopGuard(max_jobs=2, window_s=100, block_s=50)
        self.assertEqual([g.check("k", 0), g.check("k", 1), g.check("k", 2), g.check("k", 3)], [(True, False), (True, False), (False, True), (False, False)])
        self.assertEqual(g.check("other", 3), (True, False))
        self.assertTrue(g.check("k", 400)[0])

    def test_repeat_guard(self):
        g = guards.RepeatGuard(max_same=2, window_s=100)
        k = guards.decision_key("pm", PLAN, "close", {})
        self.assertEqual([g.allow(k, 0), g.allow(k, 1), g.allow(k, 2), g.allow(k, 200)], [True, True, False, True])
        self.assertNotEqual(k, guards.decision_key("pm", PLAN, "close", {"frac": 0.5}))


class JobQueue(BrainBase):
    def test_priority_not_before_and_retry_at(self):
        self.job("J-a", prio=3, key="plan:a")
        self.job("J-b", prio=0, key="plan:b")
        self.job("J-c", prio=1, key="plan:c", not_before=self.t + 100)
        self.job("J-d", prio=1, key="plan:d", retry_at=self.t + 100)
        order = []
        while True:
            j = jobs.pick(self.root, self.t, "t")
            if not j:
                break
            order.append(j["id"])
        self.assertEqual(order, ["J-b", "J-a"])
        j = jobs.pick(self.root, self.t + 101, "t")
        self.assertIn(j["id"], ("J-c", "J-d"))

    def test_taken_marks_owner_pid_attempts_rev(self):
        self.job("J-a")
        j = jobs.pick(self.root, self.t, "brain", rev=7)
        d = self.get_job("J-a")
        self.assertEqual((d["status"], d["attempts"], d["rev"], d["taken_by"]), ("taken", 1, 7, os.getpid()))
        self.assertEqual(j["id"], "J-a")
        self.assertIsNone(jobs.pick(self.root, self.t, "brain"))

    def test_ttl_expires_old_jobs(self):
        self.job("J-old", ts_last=self.t - 5000)
        self.assertIsNone(jobs.pick(self.root, self.t, "brain", ttl_s=900))
        self.assertEqual(self.get_job("J-old")["status"], "expired")

    def test_max_attempts_fails(self):
        self.job("J-a", attempts=3)
        self.assertIsNone(jobs.pick(self.root, self.t, "brain", max_attempts=3))
        self.assertEqual(self.get_job("J-a")["status"], "failed")

    def test_merge_duplicates_union_seqs_best_priority(self):
        self.job("J-a", seqs=[1, 2], prio=2, key="plan:K", types=("entry_blocked",))
        self.job("J-b", seqs=[3], prio=0, key="plan:K", types=("kill",))
        j = jobs.pick(self.root, self.t, "brain")
        self.assertEqual(j["id"], "J-b")
        self.assertEqual((j["seqs"], j["prio"], j["count"]), ([1, 2, 3], 0, 3))
        self.assertEqual(sorted(j["types"]), ["entry_blocked", "kill"])
        self.assertEqual(self.get_job("J-a")["result"]["into"], "J-b")

    def test_defer_does_not_spend_attempt(self):
        self.job("J-a")
        j = jobs.pick(self.root, self.t, "brain")
        jobs.finish(self.root, j, "new", {"defer": True, "retry_in_s": 5}, self.t)
        d = self.get_job("J-a")
        self.assertEqual((d["status"], d["attempts"]), ("new", 0))

    def test_ack_watermark_never_skips_open_jobs(self):
        s = [self.emit() for _ in range(4)]
        self.job("J-1", seqs=[s[0]], key="plan:1")
        self.job("J-2", seqs=[s[1]], key="plan:2")
        self.job("J-4", seqs=[s[3]], key="plan:4")
        j2 = jobs.pick(self.root, self.t, "b")          # J-1
        jobs.finish(self.root, j2, "done", {}, self.t)
        self.assertEqual(jobs.ack_through(self.root, "api"), s[0])
        j = jobs.pick(self.root, self.t, "b")
        jobs.finish(self.root, j, "done", {}, self.t)
        self.assertEqual(jobs.ack_through(self.root, "api"), s[1])
        self.assertIsNone(jobs.ack_through(self.root, "api"))
        self.assertEqual(bus.cursor_get("api", root=self.root)["acked"], s[1])

    def test_concurrent_pick_each_once(self):
        for i in range(30):
            self.job("J-%02d" % i, key="plan:k%02d" % i)
        got, lock = [], threading.Lock()

        def work():
            while True:
                j = jobs.pick(self.root, self.t, "w")
                if not j:
                    return
                with lock:
                    got.append(j["id"])
        th = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in th]
        [t.join(30) for t in th]
        self.assertEqual(sorted(got), ["J-%02d" % i for i in range(30)])

    def test_requeue_stale_and_attempt_limit(self):
        self.job("J-a")
        j = jobs.pick(self.root, self.t, "b")
        d = self.get_job("J-a")
        d["taken_by"] = 999999
        jdump(d, os.path.join(jobs.jobs_dir(self.root), "J-a.json"))
        self.assertEqual(jobs.requeue_stale(self.root, self.t, 300, 3), 1)
        self.assertEqual(self.get_job("J-a")["status"], "new")
        self.assertEqual(self.get_job("J-a")["requeued"], 1)
        # живой владелец и свежая метка времени: не трогаем
        j = jobs.pick(self.root, self.t, "b")
        d = self.get_job("J-a")
        d["taken_by"] = os.getppid()
        jdump(d, os.path.join(jobs.jobs_dir(self.root), "J-a.json"))
        self.assertEqual(jobs.requeue_stale(self.root, self.t, 300, 3), 0)
        # исчерпаны попытки -> failed
        d["taken_by"], d["attempts"] = 999999, 3
        jdump(d, os.path.join(jobs.jobs_dir(self.root), "J-a.json"))
        jobs.requeue_stale(self.root, self.t, 300, 3)
        self.assertEqual(self.get_job("J-a")["status"], "failed")

    def test_prune_old_terminal_only(self):
        self.job("J-a", status="done", done_ts=self.t - 10 ** 6)
        self.job("J-b", status="done", done_ts=self.t)
        self.job("J-c")
        self.assertEqual(jobs.prune(self.root, self.t, 1000), 1)
        self.assertEqual(sorted(x["id"] for x in jobs.list_jobs(self.root)), ["J-b", "J-c"])

    def test_corrupt_job_file_ignored(self):
        os.makedirs(jobs.jobs_dir(self.root), exist_ok=True)
        with open(os.path.join(jobs.jobs_dir(self.root), "J-bad.json"), "w") as f:
            f.write("{oops")
        self.job("J-ok")
        self.assertEqual([j["id"] for j in jobs.list_jobs(self.root)], ["J-ok"])

    def test_dispatcher_format_job_is_consumed(self):
        """Задание, созданное настоящим диспетчером, подходит мозгу без доработок."""
        from tb.platform import dispatcher as D
        self.t = D.time.time()
        s = bus.emit("entry_filled", {"avg": 1.0, "qty": 1, "partial": False, "sl": 0.9, "risk_usdt": 0.3, "journal_id": "BB-1"}, plan=PLAN, sym="SOL", root=self.root, now=self.t)
        jb = D.Jobs(self.root, {"jobs_per_hour": 12, "critical_per_hour": 30, "coalesce_s": 60.0, "settle_s": 0.0, "max_seqs": 20})
        ev = bus.read(0, limit=5, root=self.root)[0]
        res, jid = jb.add("api", ev, 1, self.t)
        self.assertEqual(res, "created")
        j = jobs.pick(self.root, self.t + 1, "brain")
        self.assertEqual((j["id"], j["seqs"], j["types"]), (jid, [s], ["entry_filled"]))


if __name__ == "__main__":
    unittest.main()
