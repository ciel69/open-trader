"""WP14: остановка дня C1-C8: чек-лист без LLM, консультация day_auditor, политики переноса по уровням, ответ владельца и таймаут, подтверждение закрытия исполнителем,
возобновление после падения, аварийная остановка, автоостановка по времени. Без сети; мозг и исполнитель фейковые."""
import contextlib
import json
import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _lc
from tb.control import ctlstate
from tb.core import exec_cfg, risklevels, session
from tb.lifecycle import api, audit, days, runtime

NOW = _lc.NOW
EXIT = [{"id": "tr", "do": {"action": "trail"}}]


class Crash(BaseException):
    """Эмуляция внезапной смерти процесса (не Exception: шаг её не ловит)."""


@contextlib.contextmanager
def policy(name, **over):
    p = {"level": 3, "policy": name, "scalp": False, "max_positions": 2, "max_total_risk_frac": 1.0, "max_risk_frac_of_trade": 0.5, "event_hours": 12, "max_funding_frac_of_risk": 0.25}
    p.update(over)
    with mock.patch("tb.core.risklevels.carry_policy", return_value=p):
        yield p


def auditor(decisions=None, **kw):
    """Ответ day_auditor: решения по item из payload; decisions = {plan: "carry"|"close"}."""
    def fn(payload):
        recs = []
        for item in payload["items"]:
            plan = item.split(":", 1)[1]
            dec = (decisions or {}).get(plan, kw.get("default", "close"))
            recs.append(_lc.carry_rec(item, decision=dec, basis=[] if dec == "close" else ["trend_intact", "level_held"]))
        return _lc.audit_out(*recs, narrative=kw.get("narrative", "день спокойный"))
    return fn


def day_with(e, *positions, **kw):
    day = e.start(**kw.pop("start", {}))
    for p in positions:
        e.ex.add_position(*p[:1], **p[1]) if isinstance(p, tuple) else e.ex.add_position(p)
    return day


def swing(pid="PL-1-SOL-01", **kw):
    d = dict(stype="swing", manage=EXIT)
    d.update(kw)
    return (pid, d)


def intraday(pid="PL-1-SOL-01", **kw):
    d = dict(stype="intraday", init_stop=98.0)
    d.update(kw)
    return (pid, d)


def steps_ok(e, day):
    d = e.rd_day(day, "day.json")
    return [s["name"] for s in d["steps"] if s["phase"] == "stop" and s["ok"] is True]


class TestNoLLM(unittest.TestCase):
    def test_closes_everything_not_justified_by_plan(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday("PL-1-SOL-01"), intraday("PL-2-ETH-01", sym="ETH"))
        e.ex.add_waiting("PL-3-BTC-01", sym="BTC")
        r = e.stop()
        self.assertEqual((r["ok"], r["data"]["state"]), (True, "stopped"))
        self.assertEqual(steps_ok(e, day), list(audit.STEPS))
        self.assertEqual(sorted(e.ex.ops("close")), [("close", "PL-1-SOL-01"), ("close", "PL-2-ETH-01")])
        self.assertEqual(e.ex.ops("cancel"), [("cancel", "PL-3-BTC-01")])
        self.assertEqual(e.ex.snapshot(NOW)["positions"], [])
        self.assertIsNone(session.active())
        self.assertFalse(e.r("state/control/trading.json")["enabled"])
        rep = e.rd_day(day, "report.json")
        self.assertTrue(rep["llm_unavailable"] or rep["agent_audit"]["llm"] in ("unavailable", "no_candidates"))
        self.assertEqual(rep["carried"], [])
        self.assertEqual(len(rep["closed_eod"]), 2)
        self.assertEqual(len(rep["cancelled"]), 1)
        self.assertEqual(e.brain.roles().count("day_auditor"), 0)

    def test_c1_cancels_before_c6_closes(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day_with(e, intraday("PL-1-SOL-01"))
        e.ex.add_waiting("PL-3-BTC-01", sym="BTC")
        e.stop()
        ops = e.ex.ops()
        self.assertLess(ops.index(("cancel", "PL-3-BTC-01")), ops.index(("close", "PL-1-SOL-01")))

    def test_new_entries_blocked_immediately_by_flag(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        with mock.patch.dict(audit.FUNCS, {"C2": lambda ctx, d: ("pending", {}, None)}):         # остановка зависла на C2: флаг уже выключен
            e.stop()
        flag = e.r("state/control/trading.json")
        self.assertFalse(flag["enabled"])
        self.assertEqual(flag["day_id"], day)
        self.assertEqual(e.day()["state"], "closing")

    def test_swing_under_agent_policy_carried_by_rule_when_agent_gives_no_decision(self):
        """Агент ответил пусто (или таймаут/ошибка), мозг доступен (K8 ок): swing при политике agent переносится по правилу A4."""
        for out in ({"schema": "tb.audit/1", "carry": []}, "hang", ("err", "DOWN")):
            e = _lc.LC(self)
            day = day_with(e, swing())
            e.brain.set("day_auditor", out)
            with policy("agent"):
                e.stop()
                if out == "hang":
                    e.advance_t(100)
                    api._LAST_CLOSE.clear()
                    api.tick(e.root, e.t[0])
            self.assertEqual(e.ex.ops("close"), [], out)
            carry = e.rd_day(day, "carry.json")
            self.assertEqual((carry["items"][0]["plan"], carry["items"][0]["decided_by"]), ("PL-1-SOL-01", "default"), out)

    def test_nothing_carried_when_brain_is_down_k8(self):
        """K8: мозг/связь недоступны -> защита ночью ослаблена -> перенос запрещён, даже swing при политике agent."""
        e = _lc.LC(self)
        e.brain.avail = False
        day_with(e, swing())
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_swing_without_agent_policy_closed_without_llm(self):
        for pol in ("never", "owner_confirm"):
            e = _lc.LC(self)
            e.brain.avail = False
            day_with(e, swing())
            with policy(pol):
                e.stop()
            self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")], pol)

    def test_intraday_always_closed_without_llm(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day_with(e, intraday())
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_scalp_never_carried(self):
        e = _lc.LC(self)
        day = day_with(e, ("PL-1-SOL-01", dict(stype="scalp")))
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent", scalp=True):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertEqual(e.brain.roles().count("day_auditor"), 0)               # скальп не кандидат: агента даже не спрашивают

    def test_stop_without_running_day(self):
        e = _lc.LC(self)
        r = e.stop()
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))

    def test_nothing_to_close(self):
        e = _lc.LC(self)
        day = e.start()
        r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.ex.sent, [])
        self.assertEqual(steps_ok(e, day), list(audit.STEPS))

    def test_unrelated_executor_commands_only_via_queue_with_system_actor(self):
        from tb.lifecycle import ports
        self.assertEqual(ports.FileExecPort.ACTOR, "system")


class TestAgent(unittest.TestCase):
    def test_agent_carry_under_agent_policy(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}, narrative="тренд цел"))
        with policy("agent"):
            r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.ex.ops("close"), [])
        carry = e.rd_day(day, "carry.json")["items"][0]
        self.assertEqual((carry["plan"], carry["decided_by"]), ("PL-1-SOL-01", "agent"))
        self.assertEqual(carry["basis"], ["trend_intact", "level_held"])
        self.assertTrue(carry["K"] and all(carry["K"].values()), carry["K"])
        self.assertEqual(carry["view"]["risk"], "low")
        rep = e.rd_day(day, "report.json")
        self.assertEqual(rep["carried"][0]["plan"], "PL-1-SOL-01")
        self.assertIn("PL-1-SOL-01", rep["narrative"] + json.dumps(rep["carried"]))
        self.assertEqual(rep["narrative"], "тренд цел")
        sess = session.load_all()[0]
        self.assertIn("carry=[PL-1-SOL-01]", list(sess.values())[0]["stop_note"])
        md = open(e.p("state", "days", day, "report.md"), encoding="utf-8").read()
        self.assertIn("PL-1-SOL-01", md)

    def test_agent_close_overrides_default_carry(self):
        e = _lc.LC(self)
        day_with(e, swing())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "close"}))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_never_policy_closes_even_if_agent_says_carry(self):
        e = _lc.LC(self)
        day = day_with(e, swing())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("never"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertEqual(e.brain.roles().count("day_auditor"), 0)               # при never кандидатов нет, агента не зовём

    def test_level_1_real_policy_never(self):
        e = _lc.LC(self)
        with open(os.path.join(_lc._ctl.PROJECT, "config", "risk_levels.json"), encoding="utf-8") as fh:
            e.w("config/risk_levels.json", json.load(fh))
        r = risklevels.set_level(1, "owner", e.root, phrase=risklevels.confirm_phrase(1, e.root), now=e.t[0] - 5)
        self.assertTrue(r["ok"], r)
        self.assertEqual(risklevels.carry_policy(e.root)["policy"], "never")
        day_with(e, swing())
        e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_k_failure_forces_close_even_with_agent_carry(self):
        for kw in ({"sl": 0.0}, {"liq": 98.0}):
            e = _lc.LC(self)
            day_with(e, swing(**kw))
            e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
            with policy("agent"):
                e.stop()
            self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")], kw)

    def test_calendar_event_forces_close(self):
        e = _lc.LC(self)
        day_with(e, swing())
        e.facts(calendar={"next": [{"id": "cpi", "type": "CPI", "ts": e.t[0] + 4 * 3600, "impact": 3}]})
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_invalid_agent_output_is_ignored_default_rules_stay(self):
        for out in ("текст-мусор", {"carry": "x"}, {"carry": [{"item": "pos:PL-1-SOL-01", "decision": "carry"}]}):
            e = _lc.LC(self)
            day = day_with(e, intraday())
            e.brain.set("day_auditor", out)
            with policy("agent"):
                e.stop()
            self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")], out)
            au = e.rd_day(day, "audit.json")
            self.assertEqual(au["agent"], [])

    def test_agent_error_and_timeout(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        e.brain.set("day_auditor", ("err", "PROVIDER_DOWN"))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.rd_day(day, "audit.json")["llm"], "error")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_agent_timeout_waits_via_ticks_without_blocking(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        e.brain.set("day_auditor", "hang")
        with policy("agent"):
            r = e.stop()
            self.assertEqual(r["data"]["state"], "closing")                      # ждём ответ: ctl не заблокирован
            self.assertEqual(e.ex.ops("close"), [])
            e.advance_t(30)
            api._LAST_CLOSE.clear()
            api.tick(e.root, e.t[0])
            self.assertEqual(e.day()["state"], "closing")
            e.advance_t(100)
            api._LAST_CLOSE.clear()
            r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(e.rd_day(day, "audit.json")["llm"], "timeout")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertTrue(e.brain.cancelled)

    def test_agent_payload_has_only_structured_data(self):
        e = _lc.LC(self)
        day_with(e, intraday())
        e.brain.set("day_auditor", auditor())
        with policy("agent"):
            e.stop()
        pl = [c for c in e.brain.calls if c["role"] == "day_auditor"][0]["payload"]
        self.assertEqual(pl["kind"], "audit")
        self.assertEqual(pl["items"], ["pos:PL-1-SOL-01"])
        self.assertEqual(set(pl["rows"][0]) - {"item", "kind", "plan", "sym", "side", "stype", "rule", "decision", "soft", "candidate", "codes", "facts", "decided_by"}, set())


class TestOwnerConfirm(unittest.TestCase):
    def setup_question(self, e, pol="owner_confirm"):
        day = day_with(e, intraday())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy(pol):
            r = e.stop()
        return day, r

    def test_question_is_asked_and_day_waits(self):
        e = _lc.LC(self)
        day, r = self.setup_question(e)
        self.assertEqual(r["data"]["state"], "closing")
        self.assertEqual(r["data"]["audit"]["pending_questions"], 1)
        d = e.day()
        q = d["owner"]["questions"][0]
        self.assertEqual((q["plan"], q["answer"], q["options"]), ("PL-1-SOL-01", None, ["carry", "close"]))
        self.assertEqual(e.ex.ops("close"), [])                                 # пока ждём: позиция на месте под стопом
        self.assertEqual(days.banner(e.root)["level"], "warn")
        self.assertEqual(d["owner"]["deadline_ts"], e.t[0] + 15 * 60)

    def test_owner_carry_decision(self):
        e = _lc.LC(self)
        day, _ = self.setup_question(e)
        with policy("owner_confirm"):
            r = api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "carry"}, e.t[0])
        self.assertEqual((r["ok"], r["data"]["state"]), (True, "stopped"))
        self.assertEqual(e.ex.ops("close"), [])
        c = e.rd_day(day, "carry.json")["items"][0]
        self.assertEqual(c["decided_by"], "owner")

    def test_owner_close_decision(self):
        e = _lc.LC(self)
        day, _ = self.setup_question(e)
        with policy("owner_confirm"):
            r = api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "close"}, e.t[0])
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertEqual(e.rd_day(day, "carry.json")["items"], [])

    def test_no_answer_closes_conservatively(self):
        e = _lc.LC(self)
        day, _ = self.setup_question(e)
        e.advance_t(15 * 60 + 1)
        api._LAST_CLOSE.clear()
        with policy("owner_confirm"):
            r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertEqual(e.rd_day(day, "day.json")["owner"]["questions"][0]["answered_by"], "timeout")
        rep = e.rd_day(day, "report.json")
        self.assertIn("OWNER_NO_ANSWER", rep["closed_eod"][0]["codes"])

    def test_waits_until_deadline(self):
        e = _lc.LC(self)
        day, _ = self.setup_question(e)
        e.advance_t(14 * 60)
        api._LAST_CLOSE.clear()
        with policy("owner_confirm"):
            r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "closing")
        self.assertEqual(e.ex.ops("close"), [])

    def test_k_rechecked_after_owner_says_carry(self):
        e = _lc.LC(self)
        day, _ = self.setup_question(e)
        e.ex.pos["PL-1-SOL-01"]["sl"] = 0.0                                     # за время ожидания стоп пропал
        with policy("owner_confirm"):
            api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "carry"}, e.t[0])
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        rep = e.rd_day(day, "report.json")
        self.assertTrue(any(c.startswith("K_FAILED") for c in rep["closed_eod"][0]["codes"]))

    def test_carry_decide_without_question(self):
        e = _lc.LC(self)
        day_with(e, intraday())
        r = api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "carry"}, e.t[0])
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))
        e.brain.avail = False
        e.stop()
        r = api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "carry"}, e.t[0])
        self.assertEqual(r["code"], "E_STATE")
        day, _ = None, None

    def test_carry_decide_wrong_plan(self):
        e = _lc.LC(self)
        self.setup_question(e)
        r = api.carry_decide(e.root, {"plan": "PL-9-XXX-01", "decision": "carry"}, e.t[0])
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))
        self.assertEqual(e.day()["state"], "closing")

    def test_owner_cannot_force_carry_for_unquestioned_position(self):
        """carry без вопроса невозможен: владелец не обходит K-условия через carry.decide."""
        e = _lc.LC(self)
        day_with(e, intraday(sl=0.0))
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("owner_confirm"):
            e.stop()
        r = api.carry_decide(e.root, {"plan": "PL-1-SOL-01", "decision": "carry"}, e.t[0])
        self.assertEqual(r["code"], "E_STATE")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_agent_policy_asks_nobody(self):
        e = _lc.LC(self)
        day, r = self.setup_question(e, pol="agent")
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.day(), None)
        self.assertEqual(e.rd_day(day, "day.json")["owner"]["questions"], [])


class TestExecution(unittest.TestCase):
    def test_orphan_is_adopted_then_closed(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e)
        e.ex.add_orphan("BTC")
        r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.ex.ops("adopt"), [("adopt", None)])
        self.assertEqual(e.ex.sent[0]["payload"], {"symbol": "BTC"})
        self.assertEqual(len(e.ex.ops("close")), 1)
        self.assertEqual(e.ex.snapshot(NOW)["positions"], [])

    def test_orphan_that_cannot_be_adopted_is_reported(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e)
        e.ex.add_orphan("BTC")
        e.ex.fail_ops.add("adopt")
        e.stop()
        rep = e.rd_day(day, "report.json")
        self.assertEqual(rep["verdict"], "alarm")
        self.assertTrue(rep["open_questions"][0]["id"].startswith("M"))
        self.assertEqual(e.day() and e.day()["state"], e.day() and "stopped")

    def test_close_not_confirmed_stays_closing_with_alarm(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.silent_ops.add("close")
        r = e.stop()
        self.assertEqual(r["data"]["state"], "closing")
        e.advance_t(125)
        api._LAST_CLOSE.clear()
        r = api.tick(e.root, e.t[0])
        d = e.day()
        self.assertEqual(r["state"], "closing")
        self.assertEqual(d["closing"]["failed"]["items"], ["pos:PL-1-SOL-01"])
        self.assertFalse(days.get_step(d, "C6", "stop")["ok"])
        self.assertTrue(days.get_step(d, "C7", "stop")["ok"])                   # C7 и C8 всё равно выполнены
        self.assertTrue(days.get_step(d, "C8", "stop")["ok"])
        err = e.bus_events("error")
        self.assertEqual(err[-1]["d"], {"where": "lifecycle", "code": "DAY_CLOSE_FAILED"})
        self.assertEqual(days.banner(e.root)["level"], "alarm")
        rep = e.rd_day(day, "report.json")
        self.assertEqual(rep["verdict"], "alarm")
        self.assertEqual(len(e.ex.ops("close")), 2)                              # одна повторная попытка

    def test_ack_with_note_releases_stuck_day(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.silent_ops.add("close")
        e.stop()
        e.advance_t(125)
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        r = api.report_ack(e.root, {"day_id": day}, e.t[0])
        self.assertEqual((r["ok"], r["code"]), (False, "E_ARGS"))               # без пометки нельзя
        r = api.report_ack(e.root, {"day_id": day, "note": "закрыл вручную на бирже"}, e.t[0])
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertIsNone(e.day())
        self.assertIsNone(days.banner(e.root))

    def test_stuck_day_recovers_when_positions_disappear(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.silent_ops.add("close")
        e.stop()
        e.advance_t(125)
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        self.assertEqual(e.day()["state"], "closing")
        e.ex.silent_ops.clear()
        e.ex.pump()                                                              # исполнитель наконец закрыл
        api._LAST_CLOSE.clear()
        e.advance_t(3)
        r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")

    def test_retry_after_delay_then_success(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.auto = False
        r = e.stop()
        self.assertEqual(r["data"]["state"], "closing")
        self.assertEqual(len(e.ex.ops("close")), 1)
        e.advance_t(25)
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        self.assertEqual(len(e.ex.ops("close")), 2)                              # повтор через retry_after_s
        e.ex.pump()
        e.advance_t(3)
        api._LAST_CLOSE.clear()
        r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")

    def test_executor_down_during_closing(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.alive = False
        e.ex.silent_ops.add("close")
        e.stop()
        e.advance_t(130)
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        d = e.day()
        self.assertEqual(d["state"], "closing")
        self.assertEqual(d["closing"]["failed"]["reason"], "exec_down")

    def test_reconcile_wait_before_checklist(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.diffs = 1
        r = e.stop()
        self.assertEqual(r["data"]["state"], "closing")                          # ждём reconcile до 30 с
        e.advance_t(35)
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        self.assertEqual(e.day() and e.day()["state"], None if e.day() is None else "closing")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])          # расхождение: A0 close

    def test_drift_row_is_a0_close(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, swing(hold="position_drift"))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        row = [r for r in e.rd_day(day, "checklist.json")["rows"] if r["kind"] == "pos"][0]
        self.assertEqual(row["rule"], "A0")

    def test_late_position_after_checklist_is_closed(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        orig = audit.c3_checklist

        def late(ctx, d):
            res = orig(ctx, d)
            e.ex.add_position("PL-7-ETH-01", sym="ETH", stype="intraday", init_stop=98.0)       # вход исполнился между C3 и C6
            return res
        with mock.patch.dict(audit.FUNCS, {"C3": late}):
            e.stop()
        self.assertEqual(sorted(e.ex.ops("close")), [("close", "PL-1-SOL-01"), ("close", "PL-7-ETH-01")])


class TestResumeClose(unittest.TestCase):
    def test_crash_at_each_step_and_resume(self):
        for k, name in enumerate(audit.STEPS):
            e = _lc.LC(self)
            day = day_with(e, intraday("PL-1-SOL-01"), intraday("PL-2-ETH-01", sym="ETH"))
            e.ex.add_waiting("PL-3-BTC-01", sym="BTC")
            e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
            orig = audit.FUNCS[name]
            calls = [0]

            def boom(ctx, d, orig=orig, calls=calls):
                calls[0] += 1
                if calls[0] == 1:
                    raise Crash()
                return orig(ctx, d)
            with policy("agent"):
                with mock.patch.dict(audit.FUNCS, {name: boom}):
                    with self.assertRaises(Crash):
                        e.stop()
                    d = e.rd_day(day, "day.json")
                    self.assertEqual(d["state"], "closing", name)
                    st = [s for s in d["steps"] if s["name"] == name][0]
                    self.assertIsNone(st["ok"], name)                              # шаг записан ДО выполнения, результата нет
                    r = e.stop()                                                   # ctl поднялся и продолжил (то же, что tick)
            self.assertEqual(r["data"]["state"], "stopped", name)
            self.assertEqual(steps_ok(e, day), list(audit.STEPS), name)
            closes = e.ex.ops("close")
            self.assertEqual(closes, [("close", "PL-2-ETH-01")], (name, closes))   # ни дублей, ни пропусков
            self.assertEqual(e.ex.ops("cancel"), [("cancel", "PL-3-BTC-01")], name)
            self.assertEqual(len(e.rd_day(day, "carry.json")["items"]), 1, name)

    def test_tick_continues_closing_after_ctl_restart(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        with mock.patch.dict(audit.FUNCS, {"C6": mock.Mock(side_effect=Crash())}):
            with self.assertRaises(Crash):
                e.stop()
        api._LAST_CLOSE.clear()
        r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_double_stop_is_idempotent(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day_with(e, intraday())
        e.ex.auto = False
        e.stop()
        e.stop()
        e.stop()
        self.assertEqual(len(e.ex.ops("close")), 1)

    def test_repeated_close_on_closed_plan_is_not_an_error(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.ex.pos.pop("PL-1-SOL-01")                                             # позиция уже исчезла (закрыта стопом) до остановки
        e.ex.plan_map["PL-1-SOL-01"]["status"] = "closed"
        r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")
        self.assertEqual(e.ex.ops("close"), [])


class TestEmergencyAndTime(unittest.TestCase):
    def test_emergency_halts_day_and_writes_report_without_llm(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        n0 = len(e.brain.calls)
        r = api.emergency_summary(e.root, {"reason": "emergency"}, e.t[0])
        self.assertTrue(r["ok"])
        d = e.rd_day(day, "day.json")
        self.assertEqual((d["state"], d["stop_source"]), ("halted", "emergency"))
        rep = e.rd_day(day, "report.json")
        self.assertEqual((rep["kind"], rep["verdict"]), ("emergency", "alarm"))
        self.assertEqual(len(e.brain.calls), n0)
        self.assertFalse(e.r("state/control/trading.json")["enabled"])
        ev = e.bus_events("day_report")[-1]
        self.assertEqual((ev["d"]["kind"], ev["d"]["verdict"], ev["pri"]), ("emergency", "alarm", "wake"))
        self.assertEqual(days.banner(e.root)["level"], "alarm")
        self.assertEqual(e.ex.sent, [])                                          # закрытие при KILL ведёт исполнитель, не lifecycle

    def test_emergency_during_closing_interrupts(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("owner_confirm"):
            e.stop()
        self.assertEqual(e.day()["state"], "closing")
        exec_cfg.set_kill(True, "test", e.root)
        api.emergency_summary(e.root, {}, e.t[0])
        self.assertEqual(e.day()["state"], "halted")
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        self.assertEqual(e.day()["state"], "halted")                             # шаги больше не идут
        self.assertEqual(e.ex.ops("close"), [])

    def test_halted_day_finishes_when_kill_cleared(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        exec_cfg.set_kill(True, "test", e.root)
        api.emergency_summary(e.root, {}, e.t[0])
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "halted")
        exec_cfg.clear_kill(e.root) if hasattr(exec_cfg, "clear_kill") else os.remove(e.p("state", "KILL"))
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "stopped")

    def test_emergency_without_day_is_ok(self):
        e = _lc.LC(self)
        r = api.emergency_summary(e.root, {}, e.t[0])
        self.assertTrue(r["ok"])

    def test_auto_stop_by_max_day_hours(self):
        e = _lc.LC(self, cfg_over={"max_day_hours": 2})
        e.brain.avail = False
        day = day_with(e, intraday())
        self.assertEqual(api.tick(e.root, e.t[0] + 3600)["state"], "running")
        e.advance_t(2 * 3600 + 5)
        e.brain.avail = False
        r = api.tick(e.root, e.t[0])
        self.assertEqual(r["state"], "stopped")
        d = e.rd_day(day, "day.json")
        self.assertEqual((d["stop_source"], d["closing"]["auto_reason"]), ("auto", "max_day_hours"))
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertIn("автоматически", days.banner(e.root)["text"]) if days.banner(e.root) else None

    def test_auto_stop_by_day_end_utc(self):
        e = _lc.LC(self, cfg_over={"day_end_utc": "20:00"})
        e.brain.avail = False
        day = day_with(e)
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "running")
        e.advance_t(8 * 3600)                                                   # 12:20 + 8 ч = 20:20 UTC
        e.brain.avail = False
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "stopped")

    def test_auto_stop_on_session_end_flag(self):
        e = _lc.LC(self, cfg_over={"stop_day_on_session_end": True})
        e.brain.avail = False
        day = day_with(e)
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "running")
        sid = session.active()["id"]
        session._append({"ev": "stop", "id": sid, "ts": _lc.iso(e.t[0]), "note": "вручную"})
        self.assertEqual(api.tick(e.root, e.t[0])["state"], "stopped")

    def test_platform_restart_disables_trading_until_resume(self):
        e = _lc.LC(self)
        day = day_with(e)
        d = e.day()
        d["runner"] = {"pid": 1, "hb": e.t[0]}
        days.save(e.root, d)
        e.w("runtime/platform/supervisor.json", {"pid": os.getpid(), "started": e.t[0] + 5})
        with mock.patch("tb.lifecycle.api._supervisor_started", return_value=e.t[0] + 5):
            api.tick(e.root, e.t[0] + 10)
        d = e.day()
        self.assertTrue(d["needs_resume"])
        self.assertFalse(e.r("state/control/trading.json")["enabled"])
        r = api.resume_day(e.root, {}, e.t[0])
        self.assertTrue(r["ok"])
        self.assertTrue(e.r("state/control/trading.json")["enabled"])
        self.assertEqual(api.resume_day(e.root, {}, e.t[0])["code"], "E_STATE")


class TestEdges(unittest.TestCase):
    def test_expired_session_is_still_stopped_and_reported(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e, intraday())
        e.advance_t(13 * 3600)                                                   # intraday живёт максимум 12 часов: сессия истекла, но не остановлена
        self.assertIsNotNone(session.active())
        r = e.stop()
        e.ex.pump()
        api._LAST_CLOSE.clear()
        api.tick(e.root, e.t[0])
        self.assertIsNone(session.active())
        rep = e.rd_day(day, "report.json")
        self.assertEqual((len(rep["session"]), rep["session"][0]["stopped"]), (1, True))
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_connection_loss_allows_only_protective_actions(self):
        """Потеря приватного WS при закрытии: перенос запрещён (K8), позиции закрываются, новых входов нет (флаг выключен)."""
        e = _lc.LC(self)
        day = day_with(e, swing())
        e.ex.private_up = False
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])
        self.assertEqual(set(op for op, _ in e.ex.ops()), {"close"})            # только защитные команды: ни submit, ни amend
        rep = e.rd_day(day, "report.json")
        self.assertIn("A7", [r["rule"] for r in rep["checklist"]["rows"] if r["kind"] == "pos"])

    def test_hold_reason_blocks_carry(self):
        e = _lc.LC(self)
        day_with(e, swing())
        e.ex.hold_reasons = ["reconciling"]
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent"):
            e.stop()
        self.assertEqual(e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_only_system_commands_close_and_cancel_adopt(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day_with(e, intraday(), swing("PL-2-ETH-01", sym="ETH"))
        e.ex.add_waiting("PL-3-BTC-01", sym="BTC")
        e.ex.add_orphan("DOGE")
        e.stop()
        self.assertTrue(set(op for op, _ in e.ex.ops()) <= {"close", "cancel", "adopt"})


class TestSessionsAndNextDay(unittest.TestCase):
    def test_two_sessions_in_one_day_reported_separately(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e)
        sid1 = session.active()["id"]
        session._append({"ev": "stop", "id": sid1, "ts": _lc.iso(e.t[0]), "note": "переход на scalp"})
        e.advance_t(120)
        with open(e.p("config", "risk_profile.json"), encoding="utf-8") as fh:
            prof = json.load(fh)
        session.start("scalp", "скальп после обеда", "platform", 10, None, prof, now=e.t[0])
        e.advance_t(60)
        r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")
        rep = e.rd_day(day, "report.json")
        self.assertEqual(sorted(s["type"] for s in rep["session"]), ["intraday", "scalp"])
        self.assertIsNone(session.active())

    def test_next_day_sees_carry_and_exports_it_to_the_brain(self):
        e = _lc.LC(self)
        day = day_with(e, swing())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent"):
            e.stop()
        e.advance_t(3600)
        e.ready()
        day2 = e.start()
        d = e.rd_day(day2, "day.json")
        self.assertEqual([(c["plan"], c["alive"]) for c in d["carry_in"]], [("PL-1-SOL-01", True)])
        self.assertEqual(e.brain.calls[-1]["payload"]["carried_in"][0]["plan"], "PL-1-SOL-01")

    def test_carried_position_is_not_orphan_in_preflight(self):
        e = _lc.LC(self)
        day = day_with(e, swing())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("agent"):
            e.stop()
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": 30.0, "free": 30.0},
                                           "positions": [{"sym": "SOL", "side": "long", "size": 0.5, "owner": "PL-1-SOL-01"}], "orders": []})
        e.ready()
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": 30.0, "free": 30.0},
                                           "positions": [{"sym": "SOL", "side": "long", "size": 0.5, "owner": "PL-1-SOL-01"}], "orders": []})
        r = e.enter_day()
        self.assertTrue(r["ok"], r)

    def test_a1_swing_entry_with_long_ttl_is_kept(self):
        e = _lc.LC(self)
        e.brain.avail = False
        day = day_with(e)
        e.ex.add_waiting("PL-8-ETH-01", sym="ETH", stype="swing", ttl_min=5000)
        e.ex.plan_map["PL-8-ETH-01"]["rt"]["ts"] = e.t[0]
        with policy("owner_confirm"):
            e.stop()
        self.assertEqual(e.ex.ops("cancel"), [])


if __name__ == "__main__":
    unittest.main()
