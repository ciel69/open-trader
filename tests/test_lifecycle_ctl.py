"""WP14 + WP12: сервис ctl с НАСТОЯЩИМ tb.lifecycle.api (а не заглушкой): старт/стоп дня кнопками, закрытие с ожиданием владельца, синхронизация состояния
платформы через tick, аварийная остановка, report.ack, carry.decide, notify.test. Без сети; переключение исполнителя подменено."""
import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _lc
from test_lifecycle_close import audit, auditor, policy
from tb.control import ctlstate, lifecycle
from tb.core import exec_cfg, session
from tb.lifecycle import api, days

PAPER = {"mode": "paper", "session": "intraday", "minutes": 60, "allow_amber": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.e = _lc.LC(self)
        self.c = self.e.ctl(reconf=self.fake_reconf)
        self.e.brain.set("morning_analyst", _lc.DAYPLAN_OK)

    def fake_reconf(self, profile, observe, timeout):
        self.e.good_exec(profile=profile, observe=observe, now=self.e.t[0])
        return True, "OK"

    def start(self):
        r = self.e.confirmed(self.c, "day.start", PAPER)
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        return r["data"]["day_id"]

    def plat(self):
        return ctlstate.platform_state(self.e.root)


class TestAmberStartViaCtl(Base):
    """Пункт 5: жёлтые проверки в окне «Старт» не отказ молча: challenge показывает их, команда принимает; отказ различает жёсткие и жёлтые."""
    BASE = {"mode": "paper", "session": "intraday", "minutes": 60}

    def test_challenge_lists_amber_with_action(self):
        ch = self.e.challenge(self.c, "day.start", self.BASE)
        self.assertEqual((ch["status"], ch["code"]), ("done", "OK"))
        pa = ch["data"]["pre_amber"]
        self.assertIn("P4", [x["id"] for x in pa])
        self.assertTrue(all(x["msg"] and x["action"] for x in pa))

    def test_start_without_accepting_amber_names_them(self):
        r, _ = self.e.send(self.c, "day.start", self.BASE)
        res = self.e.confirmed(self.c, "day.start", self.BASE)
        self.assertEqual(res["code"], "E_AMBER")
        self.assertIn("жёлтые", res["msg"])
        det = res["data"]["detail"]
        self.assertEqual(det["kind"], "amber")
        self.assertIn("P4", [x["id"] for x in det["blocking"]])

    def test_start_with_accepted_amber_runs(self):
        a = dict(self.BASE, allow_amber=True, accepted=["P4"])
        r = self.e.confirmed(self.c, "day.start", a)
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)

    def test_new_amber_not_in_accepted_is_refused(self):
        a = dict(self.BASE, allow_amber=True, accepted=["P99"])
        r = self.e.confirmed(self.c, "day.start", a)
        self.assertEqual(r["code"], "E_AMBER")

    def test_red_check_is_hard_gate_not_amber(self):
        exec_cfg.set_kill(False, "t", self.e.root)
        r = self.e.confirmed(self.c, "day.start", dict(self.BASE, allow_amber=True))
        self.assertEqual(r["code"], "E_GATE")

    def test_dash_js_sends_allow_amber(self):
        st = os.path.join(os.path.dirname(__file__), "..", "src", "tb", "dash", "static")
        js = "".join(open(os.path.join(st, n), encoding="utf-8").read() for n in ("v-control.js", "x-dialog.js"))
        for k in ("allow_amber", "pre_amber", "Запустить с предупреждениями", "Жёсткие условия не выполнены"):
            self.assertIn(k, js)


class TestRealLifecycleViaCtl(Base):
    def test_module_is_real(self):
        self.assertTrue(lifecycle.real())

    def test_start_and_stop_cycle(self):
        day = self.start()
        d = self.e.r("state/days/%s/day.json" % day)
        self.assertEqual((d["schema"], d["state"], d["mode"]), ("tb.day/1", "running", "paper"))
        self.assertNotIn("stub", d)
        p = self.plat()
        self.assertEqual((p["state"], p["day_id"], p["trading"], p["agent"]), ("running", day, True, "api"))
        self.assertTrue(self.e.r("state/control/trading.json")["enabled"])
        r, _ = self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        self.assertNotIn("stub", r["data"]["audit"])
        self.assertEqual(r["data"]["state"], "stopped")
        p = self.plat()
        self.assertEqual((p["state"], p["day_id"]), ("stopped", None))
        self.assertFalse(self.e.r("state/control/trading.json")["enabled"])
        self.assertTrue(self.e.exists("state/days/%s/report.md" % day))
        st = self.c.status()
        self.assertEqual(st["banner"]["day_report"], day)                          # баннер «итог дня готов» до report.ack
        self.assertEqual(self.e.send(self.c, "day.stop", {"mode": "soft"})[0]["code"], "E_STATE")

    def test_day_resume_action(self):
        self.assertEqual(self.e.send(self.c, "day.resume", {})[0]["code"], "E_STATE")             # платформа остановлена
        day = self.start()
        self.assertEqual(self.e.send(self.c, "day.resume", {})[0]["code"], "E_STATE")             # возобновлять нечего
        d = days.current(self.e.root)
        d["needs_resume"] = True
        days.set_trading(self.e.root, False, d)
        days.save(self.e.root, d)
        self.c.save_platform(trading=False)
        r, _ = self.e.send(self.c, "day.resume", {})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        self.assertTrue(self.e.r("state/control/trading.json")["enabled"])
        self.assertTrue(self.plat()["trading"])
        self.assertEqual(self.plat()["day_id"], day)

    def test_preflight_failure_rejects_start_and_keeps_platform_stopped(self):
        self.e.good_exec(profile="paper", now=self.e.t[0], diffs=3)
        self.c = self.e.ctl(reconf=lambda p, o, t: (True, "OK"))
        r = self.e.confirmed(self.c, "day.start", PAPER)
        self.assertEqual((r["status"], r["code"]), ("rejected", "E_GATE"))
        self.assertEqual(self.plat()["state"], "stopped")
        d = self.e.rd_day("D-20261027-1", "day.json")
        self.assertEqual(d["abort"]["step"], "S1")
        self.assertIsNone(session.active())

    def test_status_banner_follows_lifecycle(self):
        self.start()
        b = self.c.status()["banner"]
        self.assertIn(b["level"], ("info", "warn"))                                  # «день запущен» (warn, если на старте были замечания)
        self.assertIn("запущен", b["text"])

    def test_closing_waits_for_owner_and_tick_syncs_platform(self):
        day = self.start()
        self.e.ex.add_position("PL-1-SOL-01", stype="intraday", init_stop=98.0)
        self.e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("owner_confirm"):
            r, _ = self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.assertEqual((r["status"], r["code"], r["data"]["state"]), ("done", "OK", "closing"))
        self.assertEqual(self.plat()["state"], "closing")                           # платформа не объявлена stopped, пока ждём владельца
        self.assertFalse(self.plat()["trading"])
        self.assertEqual(self.c.status()["banner"]["level"], "warn")
        r, _ = self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.assertEqual(r["code"], "E_STATE")                                      # повтор мягкой остановки во время closing
        with policy("owner_confirm"):
            r, _ = self.e.send(self.c, "carry.decide", {"plan": "PL-1-SOL-01", "decision": "carry"})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        self.assertEqual(r["data"]["state"], "stopped")
        self.c.watch()                                                              # tick: platform.json синхронизируется с day.json
        self.assertEqual(self.plat()["state"], "stopped")
        self.assertEqual(self.e.ex.ops("close"), [])
        self.assertEqual(self.e.rd_day(day, "carry.json")["items"][0]["decided_by"], "owner")

    def test_owner_timeout_through_watch(self):
        day = self.start()
        self.e.ex.add_position("PL-1-SOL-01", stype="intraday", init_stop=98.0)
        self.e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("owner_confirm"):
            self.e.send(self.c, "day.stop", {"mode": "soft"})
            self.e.advance_t(15 * 60 + 5)
            api._LAST_CLOSE.clear()
            self.c.watch()
        self.assertEqual(self.plat()["state"], "stopped")
        self.assertEqual(self.e.ex.ops("close"), [("close", "PL-1-SOL-01")])

    def test_watch_auto_stop_by_time(self):
        self.e.w("config/lifecycle.json", {"preflight": {"require_supervisor": False}, "max_day_hours": 1})
        day = self.start()
        self.e.brain.avail = False
        self.e.advance_t(3700)
        self.c.watch()
        self.assertIn(self.plat()["state"], ("closing", "stopped"))
        api._LAST_CLOSE.clear()
        self.e.advance_t(3)
        self.c.watch()
        self.assertEqual(self.plat()["state"], "stopped")

    def test_kill_halts_day_and_writes_emergency_report(self):
        day = self.start()
        r, _ = self.e.send(self.c, "stop.emergency", {"flatten": False, "reason": "тест"})
        self.assertEqual(r["code"], "OK")
        self.assertEqual(self.plat()["state"], "halted")
        d = self.e.r("state/days/%s/day.json" % day)
        self.assertEqual(d["state"], "halted")
        rep = self.e.r("state/days/%s/report.json" % day)
        self.assertEqual(rep["kind"], "emergency")
        self.assertEqual(self.c.status()["banner"]["level"], "alarm")
        exec_cfg.clear_kill(self.e.root) if hasattr(exec_cfg, "clear_kill") else os.remove(self.e.p("state", "KILL"))
        self.c.watch()
        self.assertEqual(self.plat()["state"], "stopped")
        self.assertEqual(self.e.r("state/days/%s/day.json" % day)["state"], "stopped")

    def test_report_ack_and_notify_actions(self):
        day = self.start()
        self.e.send(self.c, "day.stop", {"mode": "soft"})
        r, _ = self.e.send(self.c, "report.ack", {"day_id": day})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        self.assertIsNone(self.c.status()["banner"])
        r, _ = self.e.send(self.c, "notify.test", {})
        self.assertEqual((r["status"], r["code"]), ("rejected", "E_UNAVAILABLE"))        # в unittest настоящее уведомление ОС заблокировано
        r, _ = self.e.send(self.c, "carry.decide", {"plan": "PL-9-XXX-01", "decision": "close"})
        self.assertEqual(r["code"], "E_STATE")
        r, _ = self.e.send(self.c, "report.ack", {"day_id": "D-20260101-1"})
        self.assertEqual(r["code"], "E_STATE")

    def test_restart_of_ctl_resumes_unfinished_start(self):
        """ctl упал во время старта: platform.json = starting, день starting с устаревшим пульсом -> tick доводит старт и синхронизирует платформу."""
        a = dict(PAPER, mode="paper")
        d = days.create(self.e.root, a, self.e.t[0], "api")
        d["runner"] = {"pid": 1, "hb": self.e.t[0] - 500}
        days.save(self.e.root, d)
        c0 = ctlstate.platform_state(self.e.root)
        c0.update(state="starting", mode="paper", session="intraday")
        ctlstate.save_platform_state(c0, self.e.root)
        self.e.advance_t(100)
        self.e.ready()
        self.c.watch()
        self.assertEqual(self.e.rd_day(d["day_id"], "day.json")["state"], "running")
        p = self.plat()
        self.assertEqual((p["state"], p["day_id"], p["trading"]), ("running", d["day_id"], True))

    def test_live_start_not_blocked_by_lifecycle_when_gates_pass(self):
        """Живой путь: те же гейты ctl, затем S1-S9; предупреждения принимаются как раньше, waivers попадают в day.json."""
        e = _lc.LC(self, mode="live")
        e.good_live(now=e.t[0])
        e.ready("live")
        e.ex.profile = "trade"
        e.w("config/lifecycle.json", {"preflight": {"require_supervisor": False}})
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        c = e.ctl(reconf=lambda p, o, t: (e.good_exec(profile=p, observe=o, now=e.t[0]) or True, "OK"))
        e.good_exec(profile="paper", now=e.t[0])
        r = e.confirmed(c, "day.start", {"mode": "live", "session": "intraday", "minutes": 60, "allow_amber": True})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        d = e.r("state/days/%s/day.json" % r["data"]["day_id"])
        self.assertEqual(d["mode"], "live")
        self.assertEqual(e.r("state/control/trading.json")["mode"], "live")


class TestStubStillWorksWithoutModule(unittest.TestCase):
    def test_stub_when_api_missing(self):
        import _ctl
        e = _ctl.Env(self)                                                           # real_lifecycle=False: заглушка WP12
        self.assertFalse(lifecycle.real())
        r = lifecycle.start_day(e.root, {"mode": "paper", "session": "intraday", "minutes": 5}, e.now, "api")
        self.assertTrue(r["data"]["day_id"].startswith("D-"))
        self.assertTrue(e.r("state/days/%s/day.json" % r["data"]["day_id"])["stub"])
        self.assertIsNone(lifecycle.tick(e.root, e.now))
        self.assertIsNone(lifecycle.banner(e.root))


if __name__ == "__main__":
    unittest.main()
