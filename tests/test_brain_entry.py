"""Роль entry_scout (поиск входов, только paper): схема entry/1 и смысловая проверка, исполнение в раннере (план tb.plan/1 с mode=paper жёстко), блокировки NOT_PAPER/PROTECT_ONLY/KILL,
дедуп по монете, будильники диспетчера (hot_enter будит, idea_* нет), лимит заданий роли в час, замечание про NO_PLAN. Всё во временных корнях, без сети и LLM."""
import copy
import json
import os
import time
import unittest

import _bootstrap  # noqa: F401
import _platform_helpers as H
from _brain import BrainBase, FakeGateway
from tb import paths
from tb.brain import brief as brief_mod, entryplan, guards, plan_cli, roles_runtime, validate
from tb.core import bus
from tb.llm import schema as schema_mod
from tb.platform import dispatcher as D
from tb.platform import services as S

ROOT = _bootstrap.ROOT

SCAN_BRIEF = {"ok": True, "brief_id": "B-20261028-1015-02", "scope": "event", "system": {"exec": {"alive": True, "mode": "paper"}, "kill": False},
              "session": {"id": "S-1", "type": "intraday", "min_left": 200}, "room": {"allowed": True}, "limits": {"slots": 2, "max_open_positions": 2},
              "risk_level": {"level": 3, "no_confirm": {"allowed": True, "size_mult_max": 0.5}}, "positions": [], "plans": [],
              "struct": {"s": {"SOL": {"px": 145.2, "reg": "range", "a5_pct": 0.25, "sp": 1.8}}},
              "levels": {"SOL": {"px": 145.2, "atr_5m": 0.36}},
              "recent": [{"seq": 1, "type": "scanner_signal", "sym": "SOL", "d": {"kind": "hot_enter", "px": 145.2, "range_5m_pct": 0.5, "spread_bps": 1.8}}]}
EV = {"sym": "SOL", "kinds": ["hot_enter"], "types": ["scanner_signal"]}


def jl(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def propose(**kw):
    o = {"schema": "tb.entry/1", "event_seq": 1, "decision": "propose", "sym": "SOL", "side": "long", "horizon": "intraday", "level": 145.0, "stop": 143.4, "tp": [148.2, 150.5], "setup": "level_reclaim",
         "hypothesis": "reclaim-vol-surge", "ttl_min": 30, "confirm": "candle", "thesis": "возврат выше дневного уровня 145.0 после прокола, оборот 3.1x",
         "why": "возврат уровня | RR 2.3, a5 0.25% | стоп 1.1%", "conf": 0.6}
    o.update(kw)
    return o


def skip(**kw):
    o = {"schema": "tb.entry/1", "event_seq": 1, "decision": "skip", "skip_code": "no_setup", "why": "нет сетапа | цена между уровнями 145.2 | риск не оправдан", "conf": 0.7}
    o.update(kw)
    return o


def check(obj, brief=None, ev=None):
    """-> (ошибки схемы, смысловые коды) так, как их видит раннер."""
    o, errs = schema_mod.check_text(json.dumps(obj), validate.SCHEMAS["entry/1"])
    if errs:
        return errs, []
    return [], validate.semantic("entry/1", o, validate.entry_context(SCAN_BRIEF if brief is None else brief, EV if ev is None else ev))


def with_brief(**over):
    b = copy.deepcopy(SCAN_BRIEF)
    b.update(over)
    return b


class EntrySchema(unittest.TestCase):
    def test_good_propose_and_skip(self):
        self.assertEqual(check(propose()), ([], []))
        self.assertEqual(check(skip()), ([], []))

    def test_size_keys_forbidden(self):
        for k, v in (("leverage", 5), ("qty", 1), ("margin", 10), ("size", 2), ("risk_usdt", 3), ("notional", 100)):
            errs, sem = check(propose(**{k: v}))
            self.assertTrue(errs or sem, k)
        errs, _ = check(propose(size_mult=0.9))
        self.assertTrue(errs)                                          # size_mult выше 0.5 схема не пускает вовсе

    def test_skip_with_propose_fields_rejected(self):
        self.assertTrue(check(skip(sym="SOL"))[1])
        self.assertTrue(check(propose(skip_code="other"))[1])

    def test_skip_with_watch_level(self):
        """Пропуск может нести watch (уровень, стоп, цель) на будущее; порядок цен и дальность проверяются."""
        w = {"side": "long", "level": 144.0, "stop": 142.8, "tp": 147.0, "setup": "level_reclaim"}
        self.assertEqual(check(skip(watch=w)), ([], []))
        self.assertIn("WATCH_SIDE_PRICES", check(skip(watch=dict(w, stop=145.0)))[1])
        self.assertIn("WATCH_SIDE_PRICES", check(skip(watch=dict(w, side="short")))[1])
        self.assertIn("WATCH_LEVEL_FAR", check(skip(watch=dict(w, level=120.0, stop=118.0, tp=130.0)))[1])
        self.assertTrue(check(skip(watch={"side": "long", "level": 144.0}))[0])             # неполный watch схема не пускает

    def test_side_price_consistency(self):
        self.assertIn("SIDE_PRICES", check(propose(stop=146.0))[1])
        self.assertIn("SIDE_PRICES", check(propose(side="short"))[1])
        self.assertEqual(check(propose(side="short", level=145.0, stop=146.6, tp=[142.0, 140.0]))[1], [])

    def test_rr_below_two(self):
        self.assertIn("RR_LOW", check(propose(tp=[145.5]))[1])
        self.assertNotIn("RR_LOW", check(propose(tp=[149.0]))[1])

    def test_rejection_hints_carry_numbers_for_the_retry(self):
        """Подсказка повтора содержит наши числа (сколько есть и сколько нужно): модель видит, что исправить, а не только код."""
        sem = check(propose(tp=[145.5]))[1]
        self.assertIn("RR_LOW", sem)
        self.assertTrue(any(x.startswith("RR_GROSS:") and "_NEED:2.00" in x for x in sem), sem)
        sem = check(propose(stop=144.9, tp=[146.0]))[1]
        self.assertTrue(any(x.startswith("STOP_PCT:") and "_MIN:" in x for x in sem), sem)
        far = copy.deepcopy(SCAN_BRIEF)
        o = propose(side="short", level=151.0, stop=152.6, tp=[148.0, 146.0])
        self.assertTrue(any(x.startswith("LEVEL_DIST_PCT:") and "_MAX:3.0" in x for x in check(o, far)[1]))

    def test_stop_too_tight_or_range_wider(self):
        self.assertIn("STOP_TIGHT", check(propose(stop=144.9, tp=[146.0]))[1])
        b = copy.deepcopy(SCAN_BRIEF)
        b["recent"][0]["d"]["range_5m_pct"] = 2.0
        self.assertIn("RANGE5_GT_STOP", check(propose(), b)[1])

    def test_loose_rules_at_risk_levels_4_5(self):
        """Уровни 4-5 (paper-разведка): широкая свеча события не закрывает вход с подтверждением свечой, уровень допустим до 5%; на уровне 3 прежние правила."""
        wide = copy.deepcopy(SCAN_BRIEF)
        wide["recent"][0]["d"]["range_5m_pct"] = 2.0
        wide["risk_level"]["level"] = 4
        self.assertNotIn("RANGE5_GT_STOP", check(propose(), wide)[1])                         # confirm candle: ждёт новую свечу
        self.assertIn("RANGE5_GT_STOP", check(propose(confirm="none", size_mult=0.4, tags=["no-confirm"], zone={"low": 144.9, "high": 145.1}), wide)[1])
        self.assertIn("STOP_WIDE", check(propose(stop=100.0), wide)[1])                       # потолок стопа остаётся
        wide["risk_level"]["level"] = 3
        self.assertIn("RANGE5_GT_STOP", check(propose(), wide)[1])
        far = copy.deepcopy(SCAN_BRIEF)                                                       # уровень в 4% от цены 145.2: 139.4
        far["risk_level"]["level"] = 3
        o = propose(side="short", level=151.0, stop=152.6, tp=[148.0, 146.0])
        self.assertIn("LEVEL_FAR", check(o, far)[1])
        far["risk_level"]["level"] = 5
        self.assertNotIn("LEVEL_FAR", check(o, far)[1])

    def test_precheck_wide_range_off_at_levels_4_5(self):
        ctx = validate.entry_context(with_brief(session={"id": "S-1", "type": "scalp", "max_stop_pct": 0.8, "min_rr": 1.0, "cost_rt_pct": 0.28}), EV)
        ctx["range_5m_pct"], ctx["risk_level"] = 1.5, 3
        self.assertEqual(validate.entry_precheck(ctx), "WIDE_RANGE")
        ctx["risk_level"] = 4
        self.assertIsNone(validate.entry_precheck(ctx))

    def test_struct_missing_flag(self):
        b = with_brief(struct_missing=["SOL"], levels={})
        self.assertTrue(validate.entry_context(b, EV)["struct_missing"])
        self.assertFalse(validate.entry_context(SCAN_BRIEF, EV)["struct_missing"])
        b2 = with_brief(struct_missing=["SOL"])                                              # есть записи в levels: структура не нужна
        self.assertFalse(validate.entry_context(b2, EV)["struct_missing"])

    def test_blackout_block_and_warn(self):
        self.assertIn("BLACKOUT", check(propose(), with_brief(market={"blackout": {"event": "x", "in_min": 20, "mode": "block"}}))[1])
        self.assertIn("BLACKOUT", check(propose(), with_brief(market={"blackout": {"event": "x", "in_min": 20}}))[1])          # поля mode нет = block
        warn = with_brief(market={"blackout": {"event": "x", "in_min": 20, "mode": "warn"}})
        sem = check(propose(), warn)[1]
        self.assertIn("BLACKOUT_SIZE", sem)
        self.assertIn("BLACKOUT_TAG", sem)
        self.assertEqual(check(propose(size_mult=0.5, tags=["blackout"]), warn)[1], [])
        self.assertIn("TAG_MISMATCH:blackout", check(propose(size_mult=0.5, tags=["blackout"]))[1])           # вне окна тег blackout не ставят

    def test_no_confirm_rules(self):
        z = {"low": 144.9, "high": 145.1}
        sem = check(propose(confirm="none", zone=z))[1]
        self.assertIn("NO_CONFIRM_SIZE", sem)
        self.assertIn("NO_CONFIRM_TAG", sem)
        self.assertEqual(check(propose(confirm="none", zone=z, size_mult=0.5, tags=["no-confirm"]))[1], [])
        self.assertIn("ZONE_REQUIRED", check(propose(confirm="none", size_mult=0.5, tags=["no-confirm"]))[1])
        forbid = with_brief(risk_level={"level": 1, "no_confirm": {"allowed": False}})
        self.assertIn("NO_CONFIRM_FORBIDDEN", check(propose(confirm="none", zone=z, size_mult=0.5, tags=["no-confirm"]), forbid)[1])
        streak = with_brief(pause={"streak": 2})
        self.assertIn("NO_CONFIRM_AFTER_STOPS", check(propose(confirm="none", zone=z, size_mult=0.5, tags=["no-confirm"]), streak)[1])

    def test_symbol_busy_pause_slots_kill(self):
        self.assertIn("SYMBOL_NOT_EVENT", check(propose(sym="ETH"))[1])
        self.assertIn("SYM_BUSY", check(propose(), with_brief(plans=[{"id": "PL-1028-SOL-01", "sym": "SOL", "status": "armed"}]))[1])
        self.assertIn("PAUSE_SYM", check(propose(), with_brief(pause={"syms": {"SOL": 12}}))[1])
        self.assertIn("PAUSE_ALL", check(propose(), with_brief(pause={"all": 40, "streak": 2}))[1])
        self.assertIn("NO_SLOTS", check(propose(), with_brief(limits={"slots": 0}))[1])
        self.assertIn("KILL", check(propose(), with_brief(system={"exec": {"mode": "paper"}, "kill": True}))[1])

    def test_missing_data_does_not_crash(self):
        o, errs = schema_mod.check_text(json.dumps(propose()), validate.SCHEMAS["entry/1"])
        self.assertEqual(validate.semantic("entry/1", o, validate.entry_context({}, {})), [])           # нет досье: остаётся Gate исполнителя


class EntryPlanBuild(unittest.TestCase):
    def test_plan_is_paper_and_passes_precheck(self):
        o = propose(size_mult=0.5)
        plan = entryplan.build(o, {"session_type": "intraday", "px": 145.2, "event_seq": 7})
        self.assertEqual(plan["mode"], "paper")
        for bad in ("size", "qty", "leverage", "margin"):
            self.assertNotIn('"%s"' % bad, json.dumps(plan))
        self.assertEqual(plan["risk"]["size_mult"], 0.5)
        self.assertAlmostEqual(sum(t["frac"] for t in plan["targets"]), 1.0, places=6)
        self.assertEqual(plan["entry"]["when"]["kind"], "candle_close")
        p2, errs, _w, ready = plan_cli.precheck(plan, time.time())
        if ready:
            self.assertEqual([e for e in errs if e.get("severity") == "error"], [], errs)

    def test_no_confirm_plan_has_waiver_and_zone(self):
        o = propose(confirm="none", zone={"low": 144.9, "high": 145.1}, size_mult=0.5, tags=["no-confirm"])
        plan = entryplan.build(o, {"session_type": "scalp", "px": 145.2})
        self.assertEqual(plan["entry"]["when"]["kind"], "price_in_zone")
        self.assertTrue(plan["risk"]["confirm_waiver"])
        self.assertEqual(plan["risk"]["size_mult"], 0.5)
        self.assertIn("no-confirm", plan["tags"]["extra"])


class Scout(BrainBase):
    def setUp(self):
        BrainBase.setUp(self)
        self.set_platform(mode="paper")
        self.gw.brief = copy.deepcopy(SCAN_BRIEF)
        self.gw.responses["list_plans"] = {"ok": True, "plans": []}

    def scan_job(self, jid="J-s1", sym="SOL", kinds=("hot_enter",), **kw):
        s = self.emit("scanner_signal", {"kind": kinds[0], "px": 145.2, "wake": True}, plan=None, sym=sym)
        return self.job(jid, seqs=[s], types=("scanner_signal",), key="sym:%s" % sym, plan=None, kinds=list(kinds), **kw), s

    def run_job(self, answer, **kw):
        r = self.runner({"ds-flash": [answer]})
        j, s = self.scan_job(**kw)
        r.step()
        return r, self.get_job(j["id"]), s

    def test_propose_submits_paper_plan(self):
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["roles"][0]["role"], "entry_scout")
        c = self.gw.tool_calls("submit_plan")
        self.assertEqual(len(c), 1)
        plan = c[0]["args"]["plan"]
        self.assertEqual(plan["mode"], "paper")
        self.assertEqual(plan["source"], {"kind": "scanner", "event_seq": s})
        self.assertEqual(plan["session_type"], "intraday")
        self.assertEqual(c[0]["agent"], "api")
        self.assertEqual(c[0]["meta"]["role"], "entry_scout")
        self.assertEqual(c[0]["meta"]["profile"], "ds-flash")
        self.assertNotIn("leverage", json.dumps(plan))
        d = [x for x in self.decisions() if x["what"] == "brain_decision"]
        self.assertEqual(d[-1]["res"]["code"], "OK")

    def test_struct_missing_defers_instead_of_skipping(self):
        """Структуры по монете события ещё нет: задание откладывается (модель не зовём), а не получает skip no_levels; на последней попытке идёт к модели."""
        self.gw.brief = with_brief(struct_missing=["SOL"], levels={})
        r = self.runner({"ds-flash": [skip()]})
        j, _s = self.scan_job()
        r.step()
        j1 = self.get_job(j["id"])
        self.assertEqual(j1["status"], "new")
        self.assertEqual(j1["result"]["code"], "STRUCT")
        self.assertEqual(self.factory.calls(), 0)
        self.assertEqual([x for x in self.decisions() if x["what"] == "brain_decision"], [])      # в ленте решений шума нет

    def test_model_mode_never_used(self):
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(self.gw.tool_calls("submit_plan")[0]["args"]["plan"]["mode"], "paper")        # в схеме ответа поля mode нет, режим ставит код

    def test_skip_does_not_call_gateway(self):
        r, j, s = self.run_job(skip())
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])
        self.assertEqual(j["result"]["outcome"], "hold")
        self.assertEqual([x["res"]["code"] for x in self.decisions()], ["SKIP"])

    def test_skip_with_watch_writes_idea_once(self):
        """Пропуск с уровнем становится идеей watching (ttl 4 ч); повтор того же уровня дубль не создаёт."""
        from tb.core import ideas
        w = {"side": "long", "level": 144.0, "stop": 142.8, "tp": 147.0, "setup": "level_reclaim"}
        r, j, s = self.run_job(skip(skip_code="rr_low", watch=w))
        rows = [i for i in ideas.load_ideas(self.root) if i.get("src") == "entry_scout"]
        self.assertEqual(len(rows), 1, rows)
        i = rows[0]
        self.assertEqual((i["sym"], i["side"], i["level"], i["stop"], i["tp"], i["status"], i["ttl_h"]), ("SOL", "long", 144.0, 142.8, 147.0, "watching", 4.0))
        self.assertIn(i["id"], [x["why"] for x in self.decisions()][-1])
        r, j, s = self.run_job(skip(skip_code="rr_low", watch=dict(w, level=144.2)), jid="J-s2")
        self.assertEqual(len([i for i in ideas.load_ideas(self.root) if i.get("src") == "entry_scout"]), 1)

    def test_skip_without_watch_writes_no_idea(self):
        from tb.core import ideas
        self.run_job(skip())
        self.assertEqual([i for i in ideas.load_ideas(self.root) if i.get("src") == "entry_scout"], [])

    def test_not_paper_blocks_without_llm_call(self):
        for mode in ("live", "testnet", "observe"):
            self.set_platform(mode=mode)
            self.gw.calls[:] = []
            r, j, s = self.run_job(propose(event_seq=1), jid="J-%s" % mode, sym="SOL")
            self.assertEqual(j["result"]["roles"][0]["code"], "NOT_PAPER", mode)
            self.assertEqual(self.gw.tool_calls("submit_plan"), [], mode)
            self.assertEqual(self.factory.calls(), 0, mode)                   # до модели не дошло

    def test_agent_with_live_right_blocked(self):
        self.agents["api"]["limits"]["modes"] = ["paper", "live"]
        self.w("config/agents.json", {"agents": self.agents})
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "NOT_PAPER")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_brief_exec_mode_live_blocks_execution(self):
        self.gw.brief["system"]["exec"]["mode"] = "live"
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "NOT_PAPER")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_protect_only_blocks(self):
        r = self.runner({"ds-flash": [propose(event_seq=1)]})
        r.health.set_protect(True, "тест", "brain_down", self.t)
        j, s = self.scan_job()
        r.step()
        self.assertEqual(self.get_job(j["id"])["result"]["roles"][0]["code"], "PROTECT_ONLY")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_kill_blocks(self):
        os.makedirs(os.path.dirname(paths.kill_file(root=self.root)), exist_ok=True)
        open(paths.kill_file(root=self.root), "w").close()
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "KILL")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_kill_in_brief_rejected_by_semantic_and_blocked(self):
        self.gw.brief["system"]["kill"] = True
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_no_session_blocks(self):
        self.gw.brief.pop("session")
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "NO_SESSION")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_active_plan_on_symbol_blocks_second_plan(self):
        self.gw.responses["list_plans"] = {"ok": True, "plans": [{"id": "PL-1028-SOL-01", "sym": "SOL", "status": "armed", "mode": "paper"}]}
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "SYM_BUSY")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])
        self.gw.responses["list_plans"] = {"ok": True, "plans": [{"id": "PL-1028-SOL-01", "sym": "SOL", "status": "closed", "mode": "paper"}]}
        r2 = self.runner({"ds-flash": [propose(event_seq=1)]})
        j2, _ = self.scan_job("J-s2")
        r2.step()
        self.assertEqual(len(self.gw.tool_calls("submit_plan")), 1)           # завершённый план не мешает

    def test_unknown_plan_list_fails_closed(self):
        self.gw.responses["list_plans"] = {"ok": False, "errors": [{"code": "E_X"}]}
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "BUSY_UNKNOWN")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_repeat_guard_covers_entry(self):
        r = self.runner({"ds-flash": [propose(event_seq=1)]})
        key = guards.decision_key("entry_scout", "SOL", "propose", {"side": "long", "level": 145.0, "stop": 143.4, "tp": [148.2, 150.5]})
        for _ in range(3):
            self.assertTrue(r.repeat.allow(key, self.t))
        j, s = self.scan_job()
        r.step()
        self.assertEqual(self.get_job(j["id"])["result"]["roles"][0]["code"], "REPEAT_DECISION")
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])

    def test_semantic_reject_retries_then_no_action(self):
        r, j, s = self.run_job(propose(event_seq=1, tp=[145.5]))
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])
        self.assertEqual(j["result"]["roles"][0]["outcome"], "no_action")

    def test_idea_kinds_do_not_wake_role(self):
        r = self.runner({"ds-flash": [skip()]})
        j, _ = self.scan_job("J-i1", kinds=("idea_near",))
        r.step()
        self.assertEqual(self.get_job("J-i1")["status"], "skipped")
        self.assertEqual(self.factory.calls(), 0)

    def test_role_jobs_per_hour_limit(self):
        self.brain_doc["runtime"]["guards"]["role_jobs_per_hour"] = {"entry_scout": 2}
        self.brain_doc["runtime"]["guards"]["loop_jobs_max"] = 50
        self.write_conf()
        r = self.runner({"ds-flash": [skip()]})
        res = []
        for i, sym in enumerate(("SOL", "ETH", "BTC")):
            j, _ = self.scan_job("J-r%d" % i, sym=sym)
            r.step()
            res.append(self.get_job(j["id"]))
        self.assertEqual([x["result"]["roles"][0]["code"] for x in res], ["SKIP", "SKIP", "ROLE_RATE"])
        self.assertEqual(res[2]["status"], "skipped")

    def test_no_plan_dossier_error_is_not_entry_issue(self):
        # NO_PLAN-проверка относится только к pm/1: скан без планов и позиций для entry_scout доходит до модели
        self.gw.brief["plans"], self.gw.brief["positions"] = [], []
        r, j, s = self.run_job(skip())
        self.assertEqual(self.factory.calls(), 1)

    def test_dossier_contains_scan_blocks(self):
        r, j, s = self.run_job(skip())
        txt = self.factory.clients["ds-flash"].calls[0]["req"].messages[0]["content"]
        for k in ('"struct"', '"risk_level"', '"limits"', '"recent"', '"hot_enter"'):
            self.assertIn(k, txt)


class RoleFiles(unittest.TestCase):
    def test_role_registered_paper_only_no_live_risk(self):
        reg = roles_runtime.load(ROOT)
        self.assertEqual(reg.errors, [])
        r = reg.get("entry_scout")
        self.assertFalse(r.live_risk)
        self.assertEqual(r.output_schema, "entry/1")
        self.assertEqual(r.wake_on, ["scanner_signal"])
        self.assertEqual(r.wake_kinds, ["hot_enter", "range_5m", "turnover_surge", "patrol", "idea_level"])
        self.assertEqual(r.scope, "team")
        self.assertEqual([k for k in r.wake_kinds if k.startswith("idea_")], ["idea_level"])      # цена у уровня идеи (план дня) будит роль; idea_near/invalidated/tp нет
        brain = jl(os.path.join(ROOT, "config", "brain.json"))
        self.assertEqual(brain["teams"]["A"]["roles"]["entry_scout"]["primary"], "ds-flash")
        self.assertEqual(brain["role_opts"]["entry_scout"]["params"]["thinking"], "low")
        self.assertEqual(brain["runtime"]["guards"]["role_jobs_per_hour"]["entry_scout"], 36)
        ag = jl(os.path.join(ROOT, "config", "agents.json"))["agents"]["api"]
        self.assertEqual(ag["limits"]["modes"], ["paper"])

    def test_prompt_mentions_core_rules(self):
        p = roles_runtime.load(ROOT).get("entry_scout").system
        for w in ("size_mult", "no-confirm", "blackout", "paper ONLY", "skip"):
            self.assertIn(w, p)


class DispatcherWake(unittest.TestCase):
    def setUp(self):
        self.root = H.mk_root(self)
        self.t = time.time()
        self.cfg = S.load_cfg(self.root)

    def disp(self):
        shipped = jl(os.path.join(ROOT, "config", "subscriptions.json"))
        api = [s for s in shipped["subscribers"] if s["id"] == "api"][0]
        api["enabled"] = True
        p = os.path.join(self.root, "config", "subscriptions.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"subscribers": [api]}, f)
        return D.Dispatcher(self.root, self.cfg, clock=lambda: self.t)

    def emit(self, kind, sym, wake=True):
        self.t += 0.01
        return bus.emit("scanner_signal", {"kind": kind, "px": 1.0, "wake": wake}, sym=sym, root=self.root, now=self.t)

    def jobs(self):
        return D.Jobs(self.root, self.cfg["dispatch"]["api"]).pending()

    def test_hot_enter_creates_job_idea_does_not(self):
        d = self.disp()
        self.emit("idea_near", "SOL")
        self.emit("idea_level", "ETH")
        d.step(self.t)
        self.assertEqual(self.jobs(), [])
        self.emit("hot_enter", "SOL")
        self.emit("range_5m", "ETH")
        self.emit("turnover_surge", "BTC")
        d.step(self.t)
        js = self.jobs()
        self.assertEqual(sorted(j["sym"] for j in js), ["BTC", "ETH", "SOL"])
        self.assertTrue(all(j["types"] == ["scanner_signal"] and j["prio"] == 3 for j in js))

    def test_scanner_jobs_capped_separately_from_position_jobs(self):
        d = self.disp()
        d.step(self.t)                                                          # курсор подписчика встаёт на конец шины
        for i in range(40):
            self.emit("hot_enter", "S%d" % i)
        d.step(self.t)
        self.assertEqual(len(self.jobs()), 36)                                  # потолок класса 3: 36 заданий в час (обход + всплески)
        for i in range(3):                                                     # а позиции получают свои в час отдельно
            bus.emit("entry_filled", {"avg": 1.0}, plan="PL-1028-SOL-0%d" % (i + 1), sym="SOL", root=self.root, now=self.t + 1)
        d.step(self.t + 1)
        self.assertEqual(len(self.jobs()), 39)                                  # 36 сканерных + 3 по позициям

    def test_rate_buckets_reload_ok(self):
        d = self.disp()
        d.jobs._bump("api", "cls3", self.t)
        d.jobs._bump("api", False, self.t)
        r = d.jobs._rates()["api"]
        self.assertEqual((len(r["cls3"]), len(r["norm"])), (1, 1))


class BriefPause(unittest.TestCase):
    def test_pause_block_from_closed_stops(self):
        class C(object):
            now = 1_800_000_000.0
            trades = [{"status": "closed", "reason": "sl", "sym": "SOL", "ts_close": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_800_000_000.0 - 600))},
                      {"status": "closed", "reason": "sl", "sym": "ETH", "ts_close": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_800_000_000.0 - 300))}]
        p = brief_mod._b_pause(C, {"SOL", "BTC"})
        self.assertEqual(p["syms"], {"SOL": 20})
        self.assertEqual((p["streak"], p["all"]), (2, 55))
        C.trades = C.trades[:1] + [{"status": "closed", "reason": "tp", "sym": "ETH", "ts_close": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_800_000_000.0 - 100))}]
        self.assertNotIn("all", brief_mod._b_pause(C, {"SOL"}))


if __name__ == "__main__":
    unittest.main()


class IdleNoPlan(BrainBase):
    """NO_PLAN pm/1 = нейтральное «ждёт»: запись idle, без события error и без отказа в ленте/счётчике Gate."""

    def test_no_plan_is_idle_not_refusal(self):
        self.gw.brief = {"ok": True, "brief_id": "B-1", "scope": "event", "plans": [], "positions": []}
        r = self.runner({"ds-pro": [{}]})
        s = self.emit("connectivity", {"channel": "ws", "state": "down", "down_s": 30}, plan=None, sym=None)
        self.job("J-n1", seqs=[s], types=("connectivity",), plan=None, key="type:connectivity")
        r.step()
        j = self.get_job("J-n1")
        self.assertEqual(j["result"]["roles"][0]["code"], "IDLE_NO_PLAN")
        self.assertEqual(self.factory.calls(), 0)
        self.assertEqual([e for e in self.bus_events("error")], [])
        d = [x for x in self.decisions() if x["what"] == "brain_decision"]
        self.assertEqual(d[-1]["res"]["code"], "IDLE_NO_PLAN")
        self.assertIn("нет планов", d[-1]["why"])
        from tb.dash import model2
        row = model2.decision_row(d[-1])
        self.assertFalse(row["denied"])
        self.assertFalse(row["noop"])
        self.assertTrue(row["idle"])
        day = model2.decisions_day([row], row["t"])
        self.assertEqual(day["rejects"], 0)

    def test_real_dossier_error_still_refusal(self):
        from tb.dash import model2
        row = model2.decision_row({"seq": 1, "ts": "2026-10-28T10:00:00Z", "actor": "brain:api", "kind": "skip", "what": "brain_decision", "why": "x", "res": {"ok": False, "code": "DOSSIER"}})
        self.assertTrue(row["denied"])


class WatchApi(BrainBase):
    def api(self):
        from tb.brain import dashapi
        code, o = dashapi.api("/api/brain/watch", {}, self.root, self.t)
        self.assertEqual(code, 200)
        return o

    def test_other_path_not_ours(self):
        from tb.brain import dashapi
        self.assertIsNone(dashapi.api("/api/other", {}, self.root, self.t))

    def test_waiting_targets_with_no_data(self):
        o = self.api()
        self.assertEqual(o["state"]["code"], "waiting_targets")
        self.assertIn("ждёт целей", o["state"]["text"])
        for k in ("market", "hot", "struct", "intel", "news_digest", "ideas", "recent"):
            self.assertIn(k, o["blocks"])
        self.assertIsNone(o["last_job"])
        self.assertIn("roles", o["next_wake"])

    def test_protect_only_and_brain_down(self):
        from tb.brain import fallback
        h = fallback.Health(self.root, lambda: self.t)
        h.set_protect(True, "бюджет", "brain_budget", self.t)
        self.assertEqual(self.api()["state"]["code"], "protect_only")
        h.set_protect(True, "нет модели", "brain_down", self.t)
        self.assertEqual(self.api()["state"]["code"], "brain_down")

    def test_last_job_and_no_secrets_in_scrubbed_answer(self):
        import json as _j
        from tb.dash import model, server
        r = self.runner({"ds-pro": [{}]})
        self.gw.brief = {"ok": True, "brief_id": "B-1", "scope": "event", "plans": [], "positions": []}
        s = self.emit("connectivity", {"channel": "ws", "state": "down", "down_s": 30}, plan=None, sym=None)
        self.job("J-w1", seqs=[s], types=("connectivity",), plan=None, key="type:connectivity")
        r.step()
        code, o = server.api_payload("/api/brain/watch", {}, self.root, self.t)
        self.assertEqual(code, 200)
        self.assertEqual(o["last_job"]["code"], "IDLE_NO_PLAN")
        self.assertEqual(o["last_job"]["role"], "position_manager")
        txt = _j.dumps(o, ensure_ascii=False)
        self.assertNotIn("sk-", txt)
        self.assertEqual(model.scrub(o), o)                 # ни одно поле не вырезано scrub

    def test_blocked_by_dayplan_and_blackout(self):
        self.w("state/control/platform.json", {"state": "running", "mode": "paper", "day_id": "D-20261028-1"})
        self.w("state/days/D-20261028-1/dayplan.json", {"limits": {"max_new_plans": 0}, "regime": "unclear", "risks": ["blackout_soon"], "meta": {"source": "llm"}})
        o = self.api()
        self.assertEqual(o["state"]["code"], "blocked_by_dayplan")
        self.assertEqual(o["state"]["detail"]["risks"], ["blackout_soon"])
        self.assertEqual(o["dayplan"]["max_new_plans"], 0)


class SessionAndDayFrame(unittest.TestCase):
    """Аудит 2026-10-28, п. 3: пороги RR/стопа из типа сессии (как guard), рамка плана дня, отказ без вызова модели."""

    def test_rr_net_threshold_from_session(self):
        # level 145, stop 143.4 (1.10%), px 145.2 -> tp 148.0: брутто RR 1.56 (<2), после издержек 0.24%: (1.56*1.10-0.24)/(1.10+0.24) = 1.10
        b = with_brief(session={"id": "S-1", "type": "intraday", "min_rr": 1.0, "max_stop_pct": 3.0, "cost_rt_pct": 0.24})
        self.assertNotIn("RR_LOW", check(propose(tp=[148.0]), brief=b)[1])
        b["session"]["min_rr"] = 1.5
        self.assertIn("RR_LOW", check(propose(tp=[148.0]), brief=b)[1])
        self.assertNotIn("RR_LOW", check(propose(tp=[149.5]), brief=b)[1])        # брутто 2.39 -> нетто 1.78
        self.assertIn("RR_LOW", check(propose(tp=[148.0]))[1])                    # без порога сессии: брутто не ниже 2

    def test_stop_wide_from_session(self):
        b = with_brief(session={"id": "S-1", "type": "scalp", "min_rr": 1.0, "max_stop_pct": 0.8, "cost_rt_pct": 0.24})
        self.assertIn("STOP_WIDE", check(propose(), brief=b)[1])                   # стоп 1.1% шире 0.8% scalp
        self.assertNotIn("STOP_WIDE", check(propose())[1])

    def test_day_frame_checks(self):
        b = with_brief(day={"id": "D-20261028-1", "setups": ["range_fade"], "avoid": ["SOL"], "max_new_plans": 2, "size_mult": 0.5})
        codes = check(propose(), brief=b)[1]
        for c in ("SETUP_NOT_ALLOWED", "SYM_AVOID", "DAY_SIZE"):
            self.assertIn(c, codes)
        b["day"].update(avoid=[], size_mult=1.0)
        self.assertEqual(check(propose(setup="range_fade"), brief=b)[1], [])
        b["day"]["max_new_plans"] = 0
        self.assertIn("DAY_NO_PLANS", check(propose(setup="range_fade"), brief=b)[1])
        self.assertEqual(check(skip(), brief=b)[1], [])                            # skip рамка не трогает

    def test_entry_precheck(self):
        self.assertIsNone(validate.entry_precheck(validate.entry_context(SCAN_BRIEF, EV)))
        self.assertIsNone(validate.entry_precheck(validate.entry_context({}, EV)))             # нет досье: решает Gate
        b = with_brief(day={"max_new_plans": 0})
        self.assertEqual(validate.entry_precheck(validate.entry_context(b, EV)), "DAY_NO_PLANS")
        b = with_brief(session={"type": "scalp", "max_stop_pct": 0.8, "min_rr": 1.0, "cost_rt_pct": 0.24})
        b["recent"][0]["d"]["range_5m_pct"] = 1.2
        self.assertEqual(validate.entry_precheck(validate.entry_context(b, EV)), "WIDE_RANGE")
        b = with_brief(limits={"slots": 0})
        self.assertEqual(validate.entry_precheck(validate.entry_context(b, EV)), "NO_SLOTS")
        b = with_brief(pause={"syms": {"SOL": 12}})
        self.assertEqual(validate.entry_precheck(validate.entry_context(b, EV)), "PAUSE_SYM")

    def test_dayplan_context_symbols_from_struct_hot_ideas(self):
        b = {"struct": {"s": {"SOL": {"px": 145.0}, "BTC": {"px": 80000.0}}}, "hot": {"top": [{"sym": "KORU", "px": 13.1}, {"sym": "bad sym"}]},
             "ideas": {"active": [{"sym": "SOXS", "level": 44.0}]}, "market": {}, "account": {}}
        c = validate.dayplan_context(b)
        self.assertEqual(c["symbols"], ["BTC", "KORU", "SOL", "SOXS"])
        self.assertEqual(c["prices"], {"SOL": 145.0, "BTC": 80000.0, "KORU": 13.1})


class ScoutPipeline(BrainBase):
    setUp, scan_job, run_job = Scout.setUp, Scout.scan_job, Scout.run_job

    def test_get_brief_receives_job_seqs_and_sym(self):
        r, j, s = self.run_job(skip())
        a = self.gw.tool_calls("get_brief")[0]["args"]
        self.assertEqual((a["scope"], a["seqs"], a["sym"]), ("event", [s], "SOL"))

    def test_precheck_skips_without_llm_call(self):
        self.gw.brief["day"] = {"id": "D-20261028-1", "setups": [], "max_new_plans": 0}
        r, j, s = self.run_job(propose(event_seq=1))
        self.assertEqual(j["result"]["roles"][0]["code"], "DAY_NO_PLANS")
        self.assertEqual(self.factory.calls(), 0)
        self.assertEqual(self.gw.tool_calls("submit_plan"), [])
        self.assertEqual([x["res"]["code"] for x in self.decisions()], ["DAY_NO_PLANS"])

    def test_truncated_answer_retried_with_bigger_limit_not_hint(self):
        r = self.runner({"ds-flash": ['{"schema":"tb.entry/1","event_seq":1,"dec', propose(event_seq=1)]})
        c = r.client_for("ds-flash")
        c.on_call = lambda f, req: setattr(f, "finish", "length" if f.n == 0 else "stop")
        j, s = self.scan_job()
        r.step()
        j = self.get_job(j["id"])
        self.assertEqual(j["result"]["roles"][0]["code"], "OK")
        self.assertEqual(len(self.gw.tool_calls("submit_plan")), 1)
        self.assertEqual([x["req"].max_tokens for x in c.calls], [1500, 3000])          # лимит удвоен, вопрос тот же
        self.assertEqual([len(x["req"].messages) for x in c.calls], [1, 1])            # без подсказки по формату
        self.assertEqual(c.format_errors[0][0], "TRUNCATED")


class HorizonChoice(unittest.TestCase):
    """Сессия-день: горизонт (scalp/intraday/swing/position) называет модель, пороги стопа и RR берутся по нему; сессия одного горизонта: только её тип."""
    DAY = {"id": "day-1", "type": "day", "min_left": 400, "cost_rt_pct": 0.24,
           "horizons": {"scalp": {"max_stop_pct": 0.8, "min_rr": 1.0, "max_leverage": 30}, "intraday": {"max_stop_pct": 3.0, "min_rr": 1.5, "max_leverage": 10},
                        "swing": {"max_stop_pct": 15.0, "min_rr": 2.0, "max_leverage": 3}, "position": {"max_stop_pct": 25.0, "min_rr": 3.0, "max_leverage": 2}}}

    def test_horizon_required_and_enum(self):
        o = propose(); o.pop("horizon")
        self.assertIn("ARGS_MISSING:horizon", check(o)[1])
        self.assertTrue(check(propose(horizon="week"))[0])                             # схема: enum

    def test_thresholds_by_horizon_in_day_session(self):
        b = with_brief(session=copy.deepcopy(self.DAY))
        self.assertIn("STOP_WIDE", check(propose(horizon="scalp"), brief=b)[1])          # стоп 1.1% шире 0.8% scalp
        self.assertNotIn("STOP_WIDE", check(propose(horizon="intraday"), brief=b)[1])
        # swing: стоп 8% допустим, RR после издержек >= 2
        self.assertEqual(check(propose(horizon="swing", stop=133.4, tp=[175.0]), brief=b)[1], [])
        self.assertIn("STOP_WIDE", check(propose(horizon="intraday", stop=133.4, tp=[175.0]), brief=b)[1])
        self.assertIn("RR_LOW", check(propose(horizon="position", tp=[149.5]), brief=b)[1])   # position требует 3.0 нетто
        self.assertNotIn("RR_LOW", check(propose(horizon="intraday", tp=[149.5]), brief=b)[1])

    def test_single_horizon_session_requires_its_type(self):
        self.assertIn("HORIZON_MISMATCH", check(propose(horizon="swing"))[1])          # SCAN_BRIEF: сессия intraday
        self.assertNotIn("HORIZON_MISMATCH", check(propose(horizon="intraday"))[1])

    def test_day_frame_caps_horizon(self):
        b = with_brief(session=copy.deepcopy(self.DAY), day={"id": "D-1", "setups": [], "max_new_plans": 2, "max_horizon": "intraday"})
        self.assertIn("HORIZON_ABOVE_DAY", check(propose(horizon="swing", stop=133.4, tp=[175.0]), brief=b)[1])
        self.assertNotIn("HORIZON_ABOVE_DAY", check(propose(horizon="scalp", stop=144.3, tp=[147.0]), brief=b)[1])

    def test_precheck_has_no_wide_range_without_single_threshold(self):
        b = with_brief(session=copy.deepcopy(self.DAY))
        b["recent"][0]["d"]["range_5m_pct"] = 5.0
        self.assertIsNone(validate.entry_precheck(validate.entry_context(b, EV)))        # порог у горизонта, решает _entry по ответу

    def test_entryplan_takes_horizon_from_answer(self):
        ctx = validate.entry_context(with_brief(session=copy.deepcopy(self.DAY)), EV)
        ctx["event_seq"] = 1
        p = entryplan.build(propose(horizon="swing", stop=133.4, tp=[175.0]), ctx)
        self.assertEqual(p["session_type"], "swing")
        self.assertEqual(p["lifecycle"]["max_hold_min"], entryplan.MAX_HOLD_MIN["swing"])
        self.assertFalse(p["lifecycle"]["flatten_at_session_end"])
        p = entryplan.build(propose(horizon="scalp", stop=144.3, tp=[147.0]), ctx)
        self.assertEqual((p["session_type"], p["lifecycle"]["flatten_at_session_end"]), ("scalp", True))
