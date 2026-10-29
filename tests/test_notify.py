"""Уведомления ОС (WP-N): файл настроек владельца поверх конфига, категории, тихие часы (в т.ч. через полночь), обход для критичных, антиспам, шаблоны без чужого текста,
CLI, действия ctl notify.set / notify.test и аудит, чтение для дашборда. Показ подменён (sender / runner): настоящих уведомлений ОС нет."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401
import _ctl
from tb import paths
from tb.control import audit, protocol
from tb.core import bus
from tb.lifecycle import api, dashapi, notify, notify_cli, notifycfg, notifyd

NOW = 1793106000.0           # 2026-10-27T13:00:00Z
ALL_ON = {"master": True, "categories": dict((k, True) for k in protocol.NOTIFY_KEYS), "quiet": {"tz": "utc"}}


class Root(object):
    def __init__(self, test):
        self.root = tempfile.mkdtemp(prefix="tbnotify_")
        test.addCleanup(shutil.rmtree, self.root, True)
        self._old = os.environ.get("TB_ROOT")
        os.environ["TB_ROOT"] = self.root
        test.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = self._old

    def set(self, patch, now=NOW):
        ok, msg, s, ch = notifycfg.set_patch(self.root, patch, now, "cli")
        assert ok, msg
        return s


def make(test, patch=None, t=None):
    r = Root(test)
    if patch is not None:
        r.set(patch)
    clock = [NOW if t is None else t]
    shown = []
    n = notifyd.Notifier(r.root, clock=lambda: clock[0], sender=lambda text: (shown.append(text) or (True, "fake")))
    return r, n, shown, clock


def emit(r, type_, d, t=None, **kw):
    return bus.emit(type_, d, root=r.root, now=NOW if t is None else t, **kw)


class TestSettings(unittest.TestCase):
    def test_defaults_all_off_and_file_beats_config(self):
        r = Root(self)
        cur = notifycfg.load(r.root)
        self.assertEqual((cur["source"], cur["engine"], cur["settings"]["master"]), ("config", False, False))
        self.assertFalse(any(cur["settings"]["categories"].values()))
        self.assertEqual(sorted(cur["settings"]["categories"]), sorted(protocol.NOTIFY_KEYS))
        cur = notifycfg.load(r.root, {"notify": {"os": True}})
        self.assertEqual((cur["source"], cur["settings"]["master"], cur["engine"]), ("config", True, False))
        r.set({"master": False})                       # файл появился: приоритет над конфигом
        cur = notifycfg.load(r.root, {"notify": {"os": True}})
        self.assertEqual((cur["source"], cur["settings"]["master"], cur["engine"]), ("file", False, True))

    def test_quiet_defaults_bypass_critical(self):
        s = notifycfg.defaults()
        self.assertEqual(sorted(s["quiet"]["bypass"]), sorted(protocol.NOTIFY_CRITICAL))
        self.assertEqual(sorted(protocol.NOTIFY_CRITICAL), ["day_close_failed", "emergency", "service_down"])
        self.assertFalse(s["quiet"]["enabled"])

    def test_corrupt_file_turns_everything_off(self):
        r = Root(self)
        os.makedirs(paths.control_dir(root=r.root))
        with open(notifycfg.file_path(r.root), "w") as f:
            f.write("{не json")
        cur = notifycfg.load(r.root, {"notify": {"os": True}})
        self.assertFalse(cur["settings"]["master"])
        self.assertTrue(cur["warn"])

    def test_normalize_drops_garbage(self):
        s = notifycfg.normalize({"master": "yes", "categories": {"emergency": 1, "x": True}, "quiet": {"from": "25:99", "tz": "mars", "bypass": ["a", "emergency"]},
                                 "limits": {"per_hour": 0, "same_kind_min": "5"}})
        self.assertFalse(s["master"])
        self.assertFalse(s["categories"]["emergency"])
        self.assertEqual((s["quiet"]["from"], s["quiet"]["tz"], s["quiet"]["bypass"]), ("23:00", "local", ["emergency"]))
        self.assertEqual(s["limits"], notifycfg.DEFAULT_LIMITS)

    def test_validation(self):
        bad = [{}, {"master": "on"}, {"categories": {"nope": True}}, {"categories": {"emergency": "on"}}, {"quiet": {"from": "9:00"}}, {"quiet": {"enabled": 1}},
               {"quiet": {"bypass": ["zzz"]}}, {"limits": {"per_hour": 100}}, {"x": 1}, {"quiet": {}}]
        for a in bad:
            self.assertFalse(protocol.validate_args("notify.set", a)[0], a)
        self.assertTrue(protocol.validate_args("notify.set", {"master": True, "quiet": {"from": "22:30", "to": "06:00"}})[0])

    def test_atomic_file_roundtrip(self):
        r = Root(self)
        s = r.set({"categories": {"emergency": True}, "limits": {"per_hour": 3}})
        with open(notifycfg.file_path(r.root), encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["categories"]["emergency"], True)
        self.assertEqual(raw["limits"]["per_hour"], 3)
        self.assertEqual(s["by"], "cli")
        self.assertEqual([n for n in os.listdir(paths.control_dir(root=r.root)) if n.endswith(".tmp")], [])


class TestQuiet(unittest.TestCase):
    def q(self, a, b, h, m=0, **kw):
        s = notifycfg.normalize({"master": True, "quiet": dict({"enabled": True, "from": a, "to": b, "tz": "utc"}, **kw)})
        t = NOW - 13 * 3600 + h * 3600 + m * 60            # 00:00 UTC того же дня + h:m
        return notifycfg.in_quiet(s, t)

    def test_same_day_range(self):
        self.assertTrue(self.q("13:00", "15:00", 14))
        self.assertFalse(self.q("13:00", "15:00", 15))
        self.assertTrue(self.q("13:00", "15:00", 13))
        self.assertFalse(self.q("13:00", "15:00", 12, 59))

    def test_range_across_midnight(self):
        for h, m, exp in ((23, 0, True), (23, 59, True), (0, 0, True), (3, 30, True), (6, 59, True), (7, 0, False), (12, 0, False), (22, 59, False)):
            self.assertEqual(self.q("23:00", "07:00", h, m), exp, (h, m))

    def test_disabled_and_empty(self):
        self.assertFalse(self.q("23:00", "07:00", 1, enabled=False))
        self.assertFalse(self.q("10:00", "10:00", 10))

    def test_critical_bypass_and_release_digest(self):
        r, n, shown, clock = make(self, dict(ALL_ON, quiet={"enabled": True, "from": "11:00", "to": "14:00", "tz": "utc"}))
        n.poll()
        emit(r, "day_report", {"kind": "stop", "day": "D-20261027-1", "net": 1.5, "carried_n": 0, "questions_n": 0, "verdict": "ok"})   # обычная: копится
        emit(r, "macro_blackout_soon", {"event": "fomc1", "kind": "FOMC", "in_min": 30, "impact": 3})
        emit(r, "kill", {"state": "on", "flatten": True})                                                                         # критичная: сразу
        n.poll()
        self.assertEqual(len(shown), 1)
        self.assertIn("KILL", shown[0])
        self.assertEqual(len(n.st["held"]), 2)
        clock[0] = NOW + 2 * 3600                       # 15:00 UTC: тишина закончилась
        n.poll()
        self.assertEqual(len(shown), 2)
        self.assertIn("Накоплено за тихие часы: 2", shown[1])
        self.assertEqual(n.st["held"], [])

    def test_bypass_list_is_configurable(self):
        r, n, shown, _ = make(self, dict(ALL_ON, quiet={"enabled": True, "from": "11:00", "to": "14:00", "tz": "utc", "bypass": []}))
        n.poll()
        emit(r, "kill", {"state": "on", "flatten": False})
        n.poll()
        self.assertEqual(shown, [])
        self.assertEqual(len(n.st["held"]), 1)


class TestNotifier(unittest.TestCase):
    def test_nothing_shown_without_file_or_master_or_category(self):
        r, n, shown, _ = make(self)
        n.poll()
        emit(r, "kill", {"state": "on", "flatten": False})
        n.poll()
        self.assertEqual(shown, [])                    # файла нет: подписчик молчит
        r.set({"master": True})
        emit(r, "service_down", {"service": "exec", "tries": 5, "code": "1"}, t=NOW + 1)
        n.clock = lambda: NOW + 1
        n.poll()
        self.assertEqual(shown, [])                    # мастер вкл, категории выкл
        r.set({"categories": {"service_down": True}})
        emit(r, "service_down", {"service": "scan", "tries": 5, "code": "1"}, t=NOW + 2)
        n.clock = lambda: NOW + 2
        n.poll()
        self.assertEqual(len(shown), 1)
        r.set({"master": False})
        emit(r, "service_down", {"service": "ctl", "tries": 5, "code": "1"}, t=NOW + 3)
        n.clock = lambda: NOW + 3
        n.poll()
        self.assertEqual(len(shown), 1)                # мастер выкл: ничего

    def test_history_is_not_replayed_and_stale_events_skipped(self):
        r = Root(self)
        r.set(ALL_ON)
        emit(r, "kill", {"state": "on", "flatten": False}, t=NOW - 10)
        shown = []
        n = notifyd.Notifier(r.root, clock=lambda: NOW, sender=lambda t: (shown.append(t) or (True, "f")))
        n.poll()
        self.assertEqual(shown, [])                    # до первого запуска подписчика
        emit(r, "kill", {"state": "on", "flatten": False}, t=NOW - 5000)
        n.poll()
        self.assertEqual(shown, [])                    # старше STALE_S
        emit(r, "service_down", {"service": "exec", "tries": 5, "code": "1"}, t=NOW - 60)
        n.poll()
        self.assertEqual(len(shown), 1)

    def test_each_category_has_event_and_template(self):
        r, n, shown, clock = make(self, dict(ALL_ON, limits={"per_hour": 60, "same_kind_min": 0}))
        n.poll()
        cases = [("day_start", "day_report", {"kind": "start", "day": "D-20261027-1", "net": 0.0, "carried_n": 1, "questions_n": 2, "verdict": "ok"}, {}),
                 ("day_report", "day_report", {"kind": "stop", "day": "D-20261027-1", "net": -1.25, "carried_n": 0, "questions_n": 1, "verdict": "attention"}, {}),
                 ("emergency", "day_report", {"kind": "emergency", "day": "D-20261027-1", "net": -2.0, "carried_n": 0, "questions_n": 0, "verdict": "alarm"}, {}),
                 ("service_down", "service_down", {"service": "exec", "tries": 5, "code": "1", "positions": 2}, {}),
                 ("live_gate", "control_cmd", {"action": "day.start", "code": "OK", "src": "dash", "mode": "live"}, {}),
                 ("risk_level_changed", "risk_level_changed", {"from": 3, "to": 2, "by": "auto", "why": "dd"}, {}),
                 ("position_closed", "position_closed", {"reason": "sl", "pnl_usdt": -0.4, "r": -1.0}, {"plan": "PL-2026-ETH-01", "sym": "ETH"}),
                 ("day_close_failed", "error", {"where": "lifecycle", "code": "DAY_CLOSE_FAILED"}, {}),
                 ("macro_blackout_soon", "macro_blackout_soon", {"event": "cpi1", "kind": "CPI", "in_min": 45, "impact": 3}, {})]
        for cat, typ, d, kw in cases:
            c = notifyd.classify({"type": typ, "d": d, "seq": 1, "plan": kw.get("plan"), "sym": kw.get("sym")})
            self.assertEqual(c[0], cat, cat)
            emit(r, typ, d, **kw)
        n.poll()
        self.assertEqual(len(shown), len(cases))
        self.assertIn("Итог дня D-20261027-1: -1.25 USDT", shown[1])
        self.assertIn("ETH закрыта по стопу: -0.40 USDT (-1.00R)", shown[6])
        self.assertIn("45 мин", shown[8])

    def test_ignored_events(self):
        for typ, d in (("kill", {"state": "off", "flatten": False}), ("connectivity", {"channel": "public", "state": "up", "down_s": 99.0}),
                       ("connectivity", {"channel": "public", "state": "down", "down_s": 5.0}), ("position_closed", {"reason": "manual", "pnl_usdt": 1.0}),
                       ("control_cmd", {"action": "day.start", "code": "OK", "src": "dash", "mode": "paper"}), ("error", {"where": "x", "code": "Y"})):
            self.assertIsNone(notifyd.classify({"type": typ, "d": d}), typ)

    def test_same_kind_merged_and_series_counted(self):
        r, n, shown, clock = make(self, dict(ALL_ON, limits={"per_hour": 60, "same_kind_min": 10}))
        n.poll()
        for i in range(4):
            emit(r, "service_down", {"service": "exec", "tries": 5, "code": "1"}, t=NOW + i)
        n.poll()
        self.assertEqual(len(shown), 1)                 # три повтора объединены
        emit(r, "service_down", {"service": "scan", "tries": 5, "code": "1"}, t=NOW + 5)      # другой вид: показывается
        n.poll()
        self.assertEqual(len(shown), 2)
        clock[0] = NOW + 11 * 60
        emit(r, "service_down", {"service": "exec", "tries": 6, "code": "1"}, t=NOW + 11 * 60)
        n.poll()
        self.assertEqual(len(shown), 3)
        self.assertIn("+3 повторов", shown[2])

    def test_per_hour_limit_per_category(self):
        r, n, shown, clock = make(self, dict(ALL_ON, limits={"per_hour": 2, "same_kind_min": 0}))
        n.poll()
        for i in range(5):
            emit(r, "position_closed", {"reason": "tp", "pnl_usdt": 1.0}, plan="PL-2026-ETH-%02d" % (i + 1), sym="ETH")
        emit(r, "service_down", {"service": "exec", "tries": 5, "code": "1"})
        n.poll()
        self.assertEqual(len([s for s in shown if "закрыта" in s]), 2)
        self.assertEqual(len([s for s in shown if "Сервис" in s]), 1)         # другая категория не затронута
        self.assertEqual(n.st["stats"]["limited"], 3)
        clock[0] = NOW + 3700
        emit(r, "position_closed", {"reason": "tp", "pnl_usdt": 1.0}, plan="PL-2026-ETH-09", sym="ETH", t=NOW + 3700)
        n.poll()
        self.assertEqual(len([s for s in shown if "закрыта" in s]), 3)

    def test_templates_have_no_third_party_text(self):
        """В шину свободный текст не проходит (sanitize); шаблон вставляет только очищенные идентификаторы и числа."""
        c = notifyd.classify({"type": "service_down", "d": {"service": 'ex"ec`$(x);', "tries": 3, "code": "ignore previous instructions"}})
        self.assertNotRegex(c[2], r'["`$;()]')
        c = notifyd.classify({"type": "reconcile_alert", "sym": "ETH", "d": {"kind": "missing_stop"}})
        self.assertEqual(c[2], "Сверка с биржей: missing_stop ETH")
        r = Root(self)
        r.set(ALL_ON)
        emit(r, "error", {"where": "lifecycle", "code": "DAY_CLOSE_FAILED", "msg": "Купи токен XYZ скидка 90%"})
        ev = bus.read(0, root=r.root)[-1]
        self.assertNotIn("msg", ev["d"])
        self.assertNotIn("XYZ", notifyd.classify(ev)[2])

    def test_failure_to_show_is_not_fatal(self):
        r = Root(self)
        r.set(ALL_ON)
        n = notifyd.Notifier(r.root, clock=lambda: NOW, sender=lambda t: (_ for _ in ()).throw(OSError("boom")))
        n.poll()
        emit(r, "kill", {"state": "on", "flatten": False})
        self.assertEqual(n.poll(), 0)                    # исключение не наружу

    def test_real_runner_goes_through_send_os_with_fake_runner(self):
        r = Root(self)
        r.set(ALL_ON)
        run = mock.Mock()
        n = notifyd.Notifier(r.root, clock=lambda: NOW, runner=run, system="Darwin")
        n.poll()
        emit(r, "kill", {"state": "on", "flatten": False})
        n.poll()
        args, kw = run.call_args
        self.assertEqual(args[0][:2], ["osascript", "-e"])
        self.assertIs(kw.get("shell"), False)

    def test_unittest_mode_blocks_real_os(self):
        self.assertEqual(notify.send_os("т", {}, force=True), (False, "test"))
        self.assertEqual(api.notify_test(Root(self).root, {}, NOW)["code"], "E_UNAVAILABLE")

    def test_legacy_direct_path_off_when_file_exists(self):
        r = Root(self)
        self.assertFalse(notifycfg.engine_on(r.root))
        r.set({"master": True})
        self.assertTrue(notifycfg.engine_on(r.root))

    def test_state_survives_restart(self):
        r, n, shown, _ = make(self, ALL_ON)
        n.poll()
        emit(r, "kill", {"state": "on", "flatten": False})
        n.poll()
        n2 = notifyd.Notifier(r.root, clock=lambda: NOW, sender=lambda t: (shown.append(t) or (True, "f")))
        n2.poll()
        self.assertEqual(len(shown), 1)                  # курсор сохранён: повторного показа нет


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = notify_cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_status_on_off_set_quiet_test(self):
        r = Root(self)
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("мастер: выкл", out)
        self.assertEqual(self.run_cli("on")[0], 0)
        self.assertEqual(self.run_cli("set", "emergency", "on")[0], 0)
        self.assertEqual(self.run_cli("set", "quiet", "22:30-06:15")[0], 0)
        s = notifycfg.load(r.root)["settings"]
        self.assertTrue(s["master"] and s["categories"]["emergency"])
        self.assertEqual((s["quiet"]["enabled"], s["quiet"]["from"], s["quiet"]["to"]), (True, "22:30", "06:15"))
        self.assertEqual(self.run_cli("set", "quiet", "off")[0], 0)
        self.assertFalse(notifycfg.load(r.root)["settings"]["quiet"]["enabled"])
        self.assertEqual(self.run_cli("set", "bypass", "live_gate", "on")[0], 0)
        self.assertIn("live_gate", notifycfg.load(r.root)["settings"]["quiet"]["bypass"])
        self.assertEqual(self.run_cli("set", "limit", "per_hour", "3")[0], 0)
        rc, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out)["settings"]["limits"]["per_hour"], 3)
        self.assertEqual(self.run_cli("off")[0], 0)
        self.assertFalse(notifycfg.load(r.root)["settings"]["master"])
        rc, out, _ = self.run_cli("test")
        self.assertEqual(rc, 1)                          # в unittest ОС не вызывается
        self.assertIn("не показано", out)
        v = audit.verify(r.root)
        self.assertTrue(v["ok"])
        self.assertGreaterEqual(v["n"], 7)               # каждое изменение в аудите
        rows = [a for a in audit.tail(r.root, 50) if a["action"] == "notify.set"]
        self.assertTrue(all(a["src"] == "cli" for a in rows))

    def test_bad_input(self):
        Root(self)
        for argv in (("set", "nope", "on"), ("set", "quiet", "25:00-07:00"), ("set", "emergency", "maybe"), ("frob",), ()):
            rc, _o, _e = self.run_cli(*argv)
            self.assertEqual(rc, 2, argv)


class TestCtlAction(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self, real_lifecycle=True)
        self.c = self.e.ctl()

    def test_set_without_nonce_and_audit(self):
        r, cmd = self.e.send(self.c, "notify.set", {"master": True, "categories": {"emergency": True, "day_report": True}, "quiet": {"enabled": True, "from": "23:00", "to": "07:00"}})
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        s = notifycfg.load(self.e.root)["settings"]
        self.assertTrue(s["master"] and s["categories"]["emergency"] and s["quiet"]["enabled"])
        self.assertEqual(s["by"], "dash")
        a = [x for x in self.e.audit() if x["action"] == "notify.set"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["code"], "OK")
        self.assertIn("мастер вкл", a[0].get("note", ""))
        self.assertTrue(audit.verify(self.e.root)["ok"])
        r, _ = self.e.send(self.c, "notify.set", {"master": False})
        self.assertEqual(r["code"], "OK")
        self.assertEqual(len([x for x in self.e.audit() if x["action"] == "notify.set"]), 2)

    def test_challenge_not_dangerous(self):
        ch = self.e.challenge(self.c, "notify.set", {"master": True})
        self.assertFalse(ch["data"]["danger"])
        self.assertIsNone(ch["data"]["nonce"])

    def test_invalid_args_rejected_and_audited(self):
        r, _ = self.e.send(self.c, "notify.set", {"categories": {"nope": True}})
        self.assertEqual((r["status"], r["code"]), ("rejected", "E_ARGS"))
        self.assertFalse(os.path.exists(notifycfg.file_path(self.e.root)))

    def test_test_action_never_calls_os_in_tests(self):
        with mock.patch("subprocess.run") as run:
            r, _ = self.e.send(self.c, "notify.test", {})
        run.assert_not_called()
        self.assertEqual(r["code"], "E_UNAVAILABLE")
        self.assertEqual(r["data"]["how"], "test")
        self.assertTrue(any(x["action"] == "notify.test" for x in self.e.audit()))

    def test_stub_lifecycle_reports_unavailable(self):
        e = _ctl.Env(self)
        c = e.ctl()
        r, _ = e.send(c, "notify.set", {"master": True})
        self.assertEqual(r["code"], "E_UNAVAILABLE")


class TestDash(unittest.TestCase):
    def test_status_endpoint(self):
        r = Root(self)
        code, v = dashapi.api("/api/notify/status", {}, r.root, NOW)
        self.assertEqual(code, 200)
        self.assertEqual((v["source"], v["settings"]["master"]), ("config", False))
        self.assertEqual([c["id"] for c in v["categories"]], list(protocol.NOTIFY_KEYS))
        self.assertIn("Редактор скриптов", v["hint"])
        r.set({"master": True, "categories": {"emergency": True}})
        v = dashapi.api("/api/notify/status", {}, r.root, NOW)[1]
        self.assertEqual((v["source"], v["settings"]["master"]), ("file", True))
        self.assertTrue([c for c in v["categories"] if c["id"] == "emergency"][0]["on"])
        self.assertIsNone(dashapi.api("/api/other", {}, r.root, NOW))


class TestCtlService(unittest.TestCase):
    def test_thread_starts_and_stops_with_ctl(self):
        from tb.control import service
        r = Root(self)
        r.set(ALL_ON)
        th = service._start_notify(r.root, lambda: NOW)
        self.assertIsNotNone(th)
        th.stop()
        self.assertFalse(th.th.is_alive())


if __name__ == "__main__":
    unittest.main()
