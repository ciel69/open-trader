"""`./tb start|stop|restart`: набор по умолчанию, условия мозга, повторный start, замороженный супервизор, отказ trade. Без сети и живых демонов; временный корень."""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _platform_helpers as H
from tb import __main__ as M
from tb.platform import freeze, quick
from tb.platform import services as S
from tb.platform import supervisor as SV

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL = ["exec", "scan", "feed", "context", "intel", "sync", "dispatch", "ctl", "dash", "brain"]
OURS = lambda pid: "python -m tb.launcher up"      # noqa: E731  подставная командная строка нашего процесса


def mk_root(test, local=None):
    """Временный корень с копией config/ проекта (без local) и необязательными локальными файлами {имя: объект}."""
    root = H.mk_root(test)
    shutil.copytree(os.path.join(PROJECT, "config"), os.path.join(root, "config"), ignore=shutil.ignore_patterns("local"))
    for name, obj in (local or {}).items():
        os.makedirs(os.path.join(root, "config", "local"), exist_ok=True)
        with open(os.path.join(root, "config", "local", name + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f)
    return root


def mk_keys(test, with_key):
    d = tempfile.mkdtemp(prefix="tbkeys_")
    test.addCleanup(shutil.rmtree, d, True)
    if with_key:
        with open(os.path.join(d, "deepseek.env"), "w", encoding="utf-8") as f:
            f.write("DEEPSEEK_API_KEY=test-value-not-real\n")
    return {"TB_KEYS_DIR": d, "HOME": d}


API_ON = {"subscriptions": {"subscribers": [{"id": "api", "enabled": True}]}}


def spawn(test, *args):
    """Дочерний процесс-«супервизор»; поток-жнец сразу убирает зомби (иначе pid_alive видит завершившийся процесс живым)."""
    p = subprocess.Popen([sys.executable, "-c"] + list(args))
    threading.Thread(target=p.wait, daemon=True).start()
    test.addCleanup(lambda: (p.kill(), p.wait()))
    return p


def sleeper(test, body="import time\ntime.sleep(60)\n"):
    return spawn(test, body)


class DefaultsTest(unittest.TestCase):
    def test_default_set_is_full_and_aliases_exist(self):
        self.assertEqual(S.DEFAULTS["services"]["paper"], FULL)
        for name in ("start", "stop", "restart", "up", "down", "status"):
            self.assertIn(name, M.V2)

    def test_trade_default_set_unchanged_without_brain(self):
        self.assertNotIn("brain", S.DEFAULTS["services"]["trade"])


class BrainConditionsTest(unittest.TestCase):
    def test_off_when_api_subscriber_disabled(self):
        root = mk_root(self)
        ok, reason, howto = quick.brain_readiness(root, mk_keys(self, True))
        self.assertFalse(ok)
        self.assertIn("api выключен", reason)
        self.assertIn("subscriptions.json", howto)

    def test_off_without_provider_key(self):
        root = mk_root(self, API_ON)
        ok, reason, howto = quick.brain_readiness(root, mk_keys(self, False))
        self.assertFalse(ok)
        self.assertIn("нет ключа", reason)

    def test_on_with_key_and_subscriber(self):
        root = mk_root(self, API_ON)
        self.assertEqual(quick.brain_readiness(root, mk_keys(self, True)), (True, "", ""))

    def test_off_when_brain_profiles_missing(self):
        root = mk_root(self, API_ON)
        os.remove(os.path.join(root, "config", "brain.json"))
        ok, reason, _ = quick.brain_readiness(root, mk_keys(self, True))
        self.assertFalse(ok)
        self.assertIn("не настроены", reason)

    def test_key_value_never_in_result(self):
        root = mk_root(self, API_ON)
        ok, reason, howto = quick.brain_readiness(root, mk_keys(self, False))
        self.assertNotIn("test-value-not-real", reason + howto)


class PlanTest(unittest.TestCase):
    def test_full_set_with_brain_when_ready(self):
        root = mk_root(self, API_ON)
        profile, sel, note, _ = quick.plan(root, environ=mk_keys(self, True))
        self.assertEqual((profile, sel, note), ("paper", FULL, None))

    def test_brain_dropped_with_one_line(self):
        root = mk_root(self)
        profile, sel, note, _ = quick.plan(root, environ=mk_keys(self, True))
        self.assertEqual(sel, [n for n in FULL if n != "brain"])
        self.assertTrue(note.startswith("мозг выключен: ") and "(как включить: " in note and "\n" not in note)

    def test_profile_from_local_platform_json(self):
        root = mk_root(self, {"platform": {"profile": "testnet"}})
        self.assertEqual(quick.plan(root, environ=mk_keys(self, False))[0], "testnet")

    def test_trade_refused_from_flag_and_from_local_config(self):
        root = mk_root(self)
        with self.assertRaises(S.ConfigError) as cm:
            quick.plan(root, "trade")
        self.assertIn("--i-understand-live", str(cm.exception))
        root2 = mk_root(self, {"platform": {"profile": "trade"}})
        with self.assertRaises(S.ConfigError):
            quick.plan(root2)

    def test_start_trade_cli_exit_2_and_nothing_started(self):
        root = mk_root(self)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stderr", new=err), mock.patch.object(SV, "_detach") as det:
            self.assertEqual(quick.main_start(["--profile", "trade"]), 2)
        det.assert_not_called()
        self.assertIn("trade", err.getvalue())

    def test_explicit_services_not_filtered(self):
        root = mk_root(self)
        self.assertEqual(quick.plan(root, services_arg="exec,brain", environ=mk_keys(self, False))[1], ["exec", "brain"])

    def test_up_without_params_dry_run_same_set(self):
        root = mk_root(self, API_ON)
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(mk_keys(self, True), TB_ROOT=root)), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_up(["--dry-run"]), 0)
        self.assertIn("сервисы: " + ", ".join(FULL), out.getvalue())
        root2 = mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(mk_keys(self, True), TB_ROOT=root2)), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_up(["--dry-run"]), 0)
        self.assertIn("мозг выключен", out.getvalue())
        self.assertNotIn("brain", out.getvalue().split("сервисы:")[1].splitlines()[0])


class StartTest(unittest.TestCase):
    def run_start(self, root, **kw):
        out, err = [], []
        kw.setdefault("environ", mk_keys(self, False))
        kw.setdefault("port_busy_fn", lambda p: False)          # на машине владельца 8765 занят живой платформой: проба порта в тестах подменена
        rc = quick.start_platform(root, out=out.append, err=err.append, sleep=lambda s: None, **kw)
        return rc, "\n".join(out), "\n".join(err)

    def test_start_detaches_with_full_set_and_prints_summary(self):
        root = mk_root(self)
        with mock.patch.object(SV, "_detach", return_value=0) as det:
            rc, out, err = self.run_start(root)
        self.assertEqual(rc, 0)
        args = det.call_args[0][0]
        self.assertEqual(args[:2], ["--profile", "paper"])
        self.assertEqual(args[3].split(","), [n for n in FULL if n != "brain"])
        self.assertIn("профиль: paper", out)
        self.assertIn("мозг выключен", out)
        self.assertIn("http://127.0.0.1:8765/", out)
        self.assertIn("«Старт»", out)

    def test_start_failure_reports_code(self):
        root = mk_root(self)
        with mock.patch.object(SV, "_detach", return_value=3):
            rc, out, err = self.run_start(root)
        self.assertEqual(rc, 3)
        self.assertIn("не удалось", err)

    def test_start_when_running_says_already_exit_0(self):
        root = mk_root(self)
        p = sleeper(self)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper", "services": ["exec", "dash"]})
        with mock.patch.object(SV, "_detach") as det:
            rc, out, err = self.run_start(root, state_fn=lambda pid: "S", cmd_fn=OURS)
        self.assertEqual(rc, 0)
        det.assert_not_called()
        self.assertIn("уже запущена", out)
        self.assertIn("http://127.0.0.1:8765/", out)

    def test_start_thaws_frozen_supervisor(self):
        root = mk_root(self)
        p = sleeper(self)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        state = {"v": "T"}
        sent = []
        rc, out, err = self.run_start(root, state_fn=lambda pid: state["v"], cmd_fn=OURS,
                                      cont_fn=lambda pid: (sent.append(pid), state.update(v="S")))
        self.assertEqual((rc, sent), (0, [p.pid]))
        self.assertIn("SIGCONT", out)
        self.assertIn("уже запущена", out)

    def test_start_frozen_not_thawing_gives_manual_instruction(self):
        root = mk_root(self)
        p = sleeper(self)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        with mock.patch.object(freeze.time, "time", side_effect=iter([0, 0, 100, 100, 100, 100])):
            rc, out, err = self.run_start(root, state_fn=lambda pid: "T", cmd_fn=OURS, cont_fn=lambda pid: None)
        self.assertEqual(rc, 1)
        self.assertIn("kill -CONT %d" % p.pid, err)

    def test_foreign_process_is_not_signalled(self):
        root = mk_root(self)
        p = sleeper(self)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        sent = []
        fz = freeze.frozen(root, state_fn=lambda pid: "T", cmd_fn=lambda pid: "/usr/bin/vim x")
        self.assertEqual(fz, [])
        freeze.thaw(root, out=lambda m: None, state_fn=lambda pid: "T", cmd_fn=lambda pid: "/usr/bin/vim x", cont_fn=sent.append, wait_s=0)
        self.assertEqual(sent, [])


class StopTest(unittest.TestCase):
    WAIT_STOP = ("import os, sys, time\nr = sys.argv[1]\n"
                 "while not os.path.exists(os.path.join(r, 'runtime', 'platform', 'stop.req')):\n    time.sleep(0.05)\n")

    def test_stop_not_running(self):
        root = mk_root(self)
        out = []
        self.assertEqual(quick.stop_platform(root, out=out.append), 0)
        self.assertIn("не запущена", "\n".join(out))

    def test_stop_frozen_supervisor_thaws_then_stops(self):
        root = mk_root(self)
        p = spawn(self, self.WAIT_STOP, root)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        state = {"v": "T"}
        out = []
        rc = quick.stop_platform(root, timeout=10, out=out.append, state_fn=lambda pid: state["v"] if p.poll() is None else None, cmd_fn=OURS,
                                 cont_fn=lambda pid: state.update(v="S"))
        self.assertEqual(rc, 0)
        self.assertIsNotNone(p.poll())
        self.assertIn("SIGCONT", "\n".join(out))
        self.assertIn("платформа остановлена", "\n".join(out))
        self.assertFalse(os.path.exists(S.lock_file(root)))

    def test_stop_unresponsive_gives_instruction_and_no_kill9(self):
        root = mk_root(self)
        p = sleeper(self)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        out, termed = [], []
        with mock.patch.object(quick, "TERM_WAIT_S", 0.3):
            rc = quick.stop_platform(root, timeout=0.3, out=out.append, state_fn=lambda pid: "S", cmd_fn=OURS, term_fn=termed.append)
        self.assertEqual((rc, termed), (1, [p.pid]))
        self.assertIsNone(p.poll())
        self.assertIn("kill -TERM %d" % p.pid, "\n".join(out))

    def test_down_also_thaws(self):
        root = mk_root(self)
        p = spawn(self, self.WAIT_STOP, root)
        S.write_json(S.lock_file(root), {"pid": p.pid, "profile": "paper"})
        state = {"v": "T"}
        real_kill = os.kill
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch.object(freeze, "proc_state", lambda pid: state["v"] if p.poll() is None else None), \
                mock.patch.object(freeze, "is_ours", lambda pid, cmd_fn=None: True), \
                mock.patch("os.kill", side_effect=lambda pid, sig: state.update(v="S") if sig == signal.SIGCONT else real_kill(pid, sig)):
            self.assertEqual(SV.main_down(["--timeout", "10"]), 0)
        self.assertEqual(state["v"], "S")

    def test_proc_state_of_live_process(self):
        if os.name != "posix":
            self.skipTest("POSIX")
        self.assertIn(freeze.proc_state(os.getpid()), ("R", "S", "U", "D"))
        self.assertIsNone(freeze.proc_state(2 ** 22 + 12345))


class PortTest(unittest.TestCase):
    run_start = StartTest.run_start

    def test_busy_port_gives_message_and_does_not_start(self):
        root = mk_root(self)
        with mock.patch.object(SV, "_detach", return_value=0) as det:
            rc, out, err = self.run_start(root, port_busy_fn=lambda p: True)
        self.assertEqual(rc, 1)
        det.assert_not_called()
        self.assertIn("порт дашборда 8765 занят", err)
        self.assertIn("./tb start --port 8775", err)
        self.assertIn("config/local/platform.json", err)

    def test_port_flag_saves_local_layer_and_starts(self):
        root = mk_root(self)
        asked = []
        with mock.patch.object(SV, "_detach", return_value=0):
            rc, out, err = self.run_start(root, port=8791, port_busy_fn=lambda p: asked.append(p) or False)
        self.assertEqual((rc, asked), (0, [8791]))
        with open(os.path.join(root, "config", "local", "platform.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"svc": {"dash": {"port": 8791}}})
        self.assertIn("http://127.0.0.1:8791/", out)
        with open(os.path.join(root, "config", "platform.json"), encoding="utf-8") as f:
            self.assertNotIn("8791", f.read())

    def test_bad_port_refused(self):
        root = mk_root(self)
        with mock.patch.object(SV, "_detach", return_value=0) as det:
            rc, out, err = self.run_start(root, port=80)
        self.assertEqual(rc, 2)
        det.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(root, "config", "local", "platform.json")))

    def test_port_probe_sees_listener(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        self.assertTrue(quick._port_busy(s.getsockname()[1]))
        p = s.getsockname()[1]
        s.close()
        self.assertFalse(quick._port_busy(p))


class BrainEnableTest(unittest.TestCase):
    def run_cmd(self, root, enable=True):
        from tb.brain import brain_cli
        out = []
        with mock.patch.dict(os.environ, dict(mk_keys(self, False))):
            rc = brain_cli.cmd_enable(root, enable, out=out.append)
        return rc, "\n".join(out)

    def local(self, root):
        with open(os.path.join(root, "config", "local", "subscriptions.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_enable_writes_local_layer_only_and_reports_missing_key(self):
        root = mk_root(self)
        shipped = open(os.path.join(root, "config", "subscriptions.json"), encoding="utf-8").read()
        rc, out = self.run_cmd(root)
        self.assertEqual(rc, 1)
        self.assertEqual(self.local(root), {"subscribers": [{"id": "api", "enabled": True}]})
        self.assertEqual(open(os.path.join(root, "config", "subscriptions.json"), encoding="utf-8").read(), shipped)
        self.assertIn("подписчик api включён", out)
        self.assertIn("ещё не хватает", out)
        self.assertIn("ключ", out)
        self.assertIn("./tb start", out)

    def test_enable_is_idempotent_and_keeps_other_local_keys(self):
        root = mk_root(self, {"subscriptions": {"subscribers": [{"id": "api", "rate": {"jobs_per_hour": 3}}], "x": 1}})
        self.run_cmd(root)
        first = self.local(root)
        self.assertEqual(first["x"], 1)
        self.assertEqual(first["subscribers"][0], {"id": "api", "rate": {"jobs_per_hour": 3}, "enabled": True})
        stamp = os.stat(os.path.join(root, "config", "local", "subscriptions.json")).st_mtime_ns
        rc, out = self.run_cmd(root)
        self.assertIn("уже включён", out)
        self.assertEqual(self.local(root), first)
        self.assertEqual(os.stat(os.path.join(root, "config", "local", "subscriptions.json")).st_mtime_ns, stamp)

    def test_disable_roundtrip_and_noop_when_already_off(self):
        root = mk_root(self)
        rc, out = self.run_cmd(root, False)
        self.assertEqual(rc, 0)
        self.assertIn("уже выключен", out)
        self.assertFalse(os.path.exists(os.path.join(root, "config", "local", "subscriptions.json")))
        self.run_cmd(root)
        rc, out = self.run_cmd(root, False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.local(root)["subscribers"], [{"id": "api", "enabled": False}])
        self.assertFalse(quick.brain_readiness(root, mk_keys(self, True))[0])

    def test_ready_when_key_present(self):
        root = mk_root(self)
        from tb.brain import brain_cli
        out = []
        with mock.patch.dict(os.environ, dict(mk_keys(self, True))):
            self.assertEqual(brain_cli.cmd_enable(root, True, out=out.append), 0)
        self.assertIn("условия мозга выполнены", "\n".join(out))
        self.assertEqual(quick.brain_readiness(root, mk_keys(self, True)), (True, "", ""))

    def test_broken_local_file_is_not_overwritten(self):
        root = mk_root(self)
        os.makedirs(os.path.join(root, "config", "local"), exist_ok=True)
        p = os.path.join(root, "config", "local", "subscriptions.json")
        open(p, "w").write("{broken")
        rc, out = self.run_cmd(root)
        self.assertEqual(rc, 2)
        self.assertEqual(open(p).read(), "{broken")

    def test_cli_entry_dispatches(self):
        from tb.brain import brain_cli
        root = mk_root(self)
        with mock.patch.dict(os.environ, dict(mk_keys(self, False), TB_ROOT=root)), mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(brain_cli.main(["enable"]), 1)
        self.assertTrue(os.path.exists(os.path.join(root, "config", "local", "subscriptions.json")))


class RestartTest(unittest.TestCase):
    def test_restart_is_stop_then_start(self):
        root = mk_root(self)
        calls = []
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), \
                mock.patch.object(quick, "stop_platform", side_effect=lambda *a, **k: calls.append("stop") or 0), \
                mock.patch.object(quick, "start_platform", side_effect=lambda *a, **k: calls.append("start") or 0):
            self.assertEqual(quick.main_restart([]), 0)
        self.assertEqual(calls, ["stop", "start"])

    def test_restart_stops_on_failed_stop_and_refuses_trade(self):
        root = mk_root(self)
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch.object(quick, "stop_platform", return_value=1), \
                mock.patch.object(quick, "start_platform") as st:
            self.assertEqual(quick.main_restart([]), 1)
            st.assert_not_called()
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(quick.main_restart(["--profile", "trade"]), 2)


if __name__ == "__main__":
    unittest.main()
