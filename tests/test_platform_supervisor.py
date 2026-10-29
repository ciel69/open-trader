"""WP11-B: супервизор на фиктивных дочерних процессах: перезапуск, потолок, блокировка, остановка, окружение без ключей, статус, автозапуск."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _platform_helpers as H
from tb.core import bus
from tb.core.filelock import pid_alive
from tb.platform import autostart, supervisor as SV
from tb.platform import services as S
from tb.platform import status as ST


def mk_platform(test, cmds, selected=None, cfg=None, base_env=None, profile="paper", **kw):
    root = H.mk_root(test)
    events = []
    pf = SV.Platform(root, profile, False, selected or list(cmds), cfg or H.fast_cfg(), base_env=base_env if base_env is not None else dict(os.environ),
                     cmds=cmds, emit=lambda t, d, **k: events.append((t, d)), **kw)
    test.addCleanup(pf.stop_all)
    return pf, root, events


def pump(pf, pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        pf.tick()
        if pred():
            return True
        time.sleep(0.02)
    return False


class RestartTest(unittest.TestCase):
    def test_crash_restarts_with_backoff_then_gives_up_with_event(self):
        root0 = H.mk_root(self)
        pf, root, events = mk_platform(self, {"scan": H.script(root0, "c.py", H.CRASH)})
        pf.start_all()
        svc = pf.svcs["scan"]
        self.assertTrue(pump(pf, lambda: svc.state == "failed"))
        self.assertGreater(svc.tries, 3)                                  # max_tries=3: сдался на четвёртом сбое подряд
        self.assertEqual(svc.last_exit, 1)
        downs = [e for e in events if e[0] == "service_down"]
        self.assertEqual(len(downs), 1)                                    # ровно одно событие
        self.assertEqual(downs[0][1]["service"], "scan")
        pf.tick()
        pf.tick()
        self.assertEqual(len([e for e in events if e[0] == "service_down"]), 1)
        self.assertEqual(svc.state, "failed")                              # сдавшийся не перезапускается (retry_after_giveup_s=0)

    def test_backoff_delay_grows_and_is_capped(self):
        pf, _, _ = mk_platform(self, {"scan": [H.PY, "-c", "pass"]})
        d = [pf._backoff_delay(n) for n in range(1, 8)]
        self.assertEqual(d[:3], [0.03, 0.06, 0.12])
        self.assertEqual(d[-1], 0.2)

    def test_fatal_exit_code_not_restarted(self):
        root0 = H.mk_root(self)
        pf, _, events = mk_platform(self, {"exec": H.script(root0, "c2.py", H.CODE2)})
        pf.start_all()
        svc = pf.svcs["exec"]
        self.assertTrue(pump(pf, lambda: svc.state == "failed"))
        self.assertTrue(svc.fatal)
        self.assertEqual(svc.restarts, 0)
        self.assertIn("код 2", svc.last_error)
        self.assertEqual([e[0] for e in events], ["service_down"])
        self.assertEqual(events[0][1]["positions"], 0)                     # у исполнителя в событии число позиций

    def test_stable_run_resets_tries(self):
        root0 = H.mk_root(self)
        cfg = H.fast_cfg(stable_s=0.0)
        pf, _, _ = mk_platform(self, {"scan": H.script(root0, "c.py", H.CRASH)}, cfg=cfg)
        pf.start_all()
        for _ in range(60):
            pf.tick()
            time.sleep(0.02)
        self.assertEqual(pf.svcs["scan"].state in ("backoff", "running"), True)   # при stable_s=0 счётчик обнуляется: не сдаётся
        self.assertLessEqual(pf.svcs["scan"].tries, 1)

    def test_spawn_failure_counts_as_crash(self):
        def bad(cmd, env, cwd):
            raise OSError("нет файла")
        pf, _, events = mk_platform(self, {"scan": ["x"]}, spawner=bad)
        pf.start_all()
        svc = pf.svcs["scan"]
        self.assertTrue(pump(pf, lambda: svc.state == "failed"))
        self.assertIn("OSError", svc.last_error)
        self.assertTrue(events)

    def test_unavailable_service_not_started(self):
        pf, _, events = mk_platform(self, {"mcp": None}, selected=["mcp"])
        pf.start_all()
        self.assertEqual(pf.svcs["mcp"].state, "unavailable")
        self.assertEqual(events, [])

    def test_stale_heartbeat_restarts_service(self):
        root0 = H.mk_root(self)
        cfg = H.fast_cfg()
        cfg["svc"]["scan"].update({"hb": "file", "hb_stale_s": 0.15, "grace_s": 0.1})
        pf, _, _ = mk_platform(self, {"scan": H.script(root0, "s.py", H.SLEEP)}, cfg=cfg)
        pf.start_all()
        svc = pf.svcs["scan"]
        self.assertTrue(pump(pf, lambda: svc.restarts >= 1))
        self.assertIn("heartbeat", svc.last_error)

    def test_fresh_heartbeat_keeps_service(self):
        root0 = H.mk_root(self)
        cfg = H.fast_cfg()
        cfg["svc"]["scan"].update({"hb": "file", "hb_stale_s": 0.5, "grace_s": 0.1})
        pf, root, _ = mk_platform(self, {"scan": H.script(root0, "s.py", H.SLEEP)}, cfg=cfg)
        pf.start_all()
        svc = pf.svcs["scan"]
        end = time.time() + 1.2
        while time.time() < end:
            S.write_hb("scan", root=root)
            # pid в heartbeat должен совпадать с pid процесса сервиса
            d = S.read_json(S.hb_file("scan", root))
            d["pid"] = svc.pid()
            S.write_json(S.hb_file("scan", root), d)
            pf.tick()
            time.sleep(0.05)
        self.assertEqual(svc.restarts, 0)
        self.assertTrue(svc.hb_ok)


class EnvTest(unittest.TestCase):
    SECRETS = {"BYBIT_API_KEY": "k-secret-1", "BYBIT_API_SECRET": "s-secret-2", "BYBIT_ENV": "mainnet", "ANTHROPIC_API_KEY": "a-secret-3",
               "HTTPS_PROXY": "http://proxy:1", "http_proxy": "http://proxy:2", "ALL_PROXY": "socks5://p:3"}

    def test_children_env_has_no_keys_and_no_proxies(self):
        root0 = H.mk_root(self)
        names = ["exec", "scan", "dispatch", "dash"]
        cmds = {n: H.script(root0, "e_%s.py" % n, H.ENVDUMP) for n in names}
        base = dict(os.environ, **self.SECRETS)
        pf, root, _ = mk_platform(self, cmds, selected=names, base_env=base)
        pf.start_all()
        self.assertTrue(H.wait_for(lambda: all(os.path.exists(os.path.join(pf.root, "env_%s.json" % n)) for n in names)))
        for n in names:
            env = json.load(open(os.path.join(pf.root, "env_%s.json" % n)))
            for k in self.SECRETS:
                self.assertNotIn(k, env, "%s получил %s" % (n, k))
            self.assertFalse([k for k, v in env.items() if v in ("k-secret-1", "s-secret-2", "a-secret-3")])
            self.assertEqual(env["TB_ROOT"], os.path.abspath(pf.root))
            self.assertEqual(env["TB_PLATFORM_SERVICE"], n)
        ex = json.load(open(os.path.join(pf.root, "env_exec.json")))
        self.assertEqual(ex["TB_SCAN_BRIDGE"], "0")                        # scan в списке: мост исполнителя выключен
        self.assertNotIn("TB_SCAN_BRIDGE", json.load(open(os.path.join(pf.root, "env_scan.json"))))

    def test_bridge_stays_on_without_scan_service(self):
        env = S.child_env("exec", {"A": "1"}, "/tmp/x", ["exec", "dash"], "paper")
        self.assertNotIn("TB_SCAN_BRIDGE", env)
        env = S.child_env("exec", {"TB_SCAN_BRIDGE": "0"}, "/tmp/x", ["exec"], "paper")
        self.assertNotIn("TB_SCAN_BRIDGE", env)

    def test_commands_carry_no_secrets_and_use_launcher_for_exec(self):
        cfg = S.load_cfg(None)
        for prof in S.PROFILES:
            cmd = S.service_cmd("exec", prof, True, cfg, py="py")
            self.assertEqual(cmd[:6], ["py", "-m", "tb.launcher", "exec", "run", "--profile"])
            self.assertIn(prof, cmd)
            self.assertIn("--observe", cmd)
        self.assertEqual(S.service_cmd("scan", "paper", False, cfg, py="py"), ["py", "-m", "tb.platform.scansvc", "run"])
        self.assertNotIn("--profile", S.service_cmd("dispatch", "paper", False, cfg, py="py"))

    def test_service_selection(self):
        self.assertEqual(S.parse_services("dash,exec,scan"), ["exec", "scan", "dash"])
        with self.assertRaises(S.ConfigError):
            S.parse_services("exec,bogus")
        self.assertEqual(S.stop_order(["exec", "scan", "dash"]), ["dash", "scan", "exec"])


class LockAndStopTest(unittest.TestCase):
    def test_second_copy_refused_and_stale_taken(self):
        root = H.mk_root(self)
        ok, _ = SV.acquire_lock(root, {"pid": os.getpid(), "profile": "paper"})
        self.assertTrue(ok)
        ok2, cur = SV.acquire_lock(root, {"pid": os.getpid()})
        self.assertFalse(ok2)
        self.assertEqual(cur["profile"], "paper")
        SV.release_lock(root)
        # устаревшая блокировка (процесс мёртв) снимается
        dead = subprocess.Popen([H.PY, "-c", "pass"])
        dead.wait()
        S.write_json(S.lock_file(root), {"pid": dead.pid})
        ok3, _ = SV.acquire_lock(root, {"pid": os.getpid()})
        self.assertTrue(ok3)
        SV.release_lock(root)

    def test_main_up_second_copy_exit_4(self):
        root = H.mk_root(self)
        SV.acquire_lock(root, {"pid": os.getpid(), "profile": "paper"})
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()) as err:
            rc = SV.main_up(["--services", "dash"])
        self.assertEqual(rc, SV.EXIT_ALREADY)
        self.assertIn("вторая копия", err.getvalue())

    def test_stop_request_file_stops_run_and_children(self):
        root0 = H.mk_root(self)
        pf, root, _ = mk_platform(self, {"scan": H.script(root0, "s.py", H.SLEEP), "dash": H.script(root0, "d.py", H.SLEEP)})
        t = threading.Thread(target=pf.run, kwargs={"handle_signals": False})
        t.start()
        self.assertTrue(H.wait_for(lambda: all(s.pid() for s in pf.svcs.values())))
        pids = [s.pid() for s in pf.svcs.values()]
        self.assertTrue(os.path.exists(S.pid_file("scan", root)))
        S.write_json(S.stop_req_file(root), {"ts": time.time()})
        t.join(10)
        self.assertFalse(t.is_alive())
        for p in pids:
            self.assertTrue(H.wait_for(lambda: not pid_alive(p), 3.0), "процесс %s остался" % p)
        self.assertFalse(os.path.exists(S.pid_file("scan", root)))
        st = S.read_json(S.status_file(root))
        self.assertFalse(st["running"])

    def test_max_seconds_and_request_stop(self):
        root0 = H.mk_root(self)
        pf, _, _ = mk_platform(self, {"scan": H.script(root0, "s.py", H.SLEEP)})
        t0 = time.time()
        self.assertEqual(pf.run(max_seconds=0.3, handle_signals=False), 0)
        self.assertLess(time.time() - t0, 6)
        self.assertEqual(pf.svcs["scan"].state, "stopped")

    def test_exec_stopped_last(self):
        root0 = H.mk_root(self)
        order = []
        pf, _, _ = mk_platform(self, {"exec": H.script(root0, "e.py", H.SLEEP), "scan": H.script(root0, "s.py", H.SLEEP), "dash": H.script(root0, "d.py", H.SLEEP)})
        pf.start_all()
        exec_pid = pf.svcs["exec"].proc.pid
        orig = SV.procs.terminate
        with mock.patch.object(SV.procs, "terminate", side_effect=lambda p, g: (order.append(p.pid), orig(p, g))[1]):
            pf.stop_all()
        self.assertEqual(len(order), 3)
        self.assertEqual(order[-1], exec_pid)


class LiveGuardTest(unittest.TestCase):
    def test_trade_requires_flag(self):
        root = H.mk_root(self)
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(SV.main_up(["--profile", "trade", "--dry-run"]), 2)
        self.assertIn("--i-understand-live", err.getvalue())

    def test_trade_refused_with_kill_file(self):
        root = H.mk_root(self)
        os.makedirs(os.path.join(root, "state"))
        with open(os.path.join(root, "state", "KILL"), "w") as f:
            json.dump({"flatten": False, "reason": "тест", "ts": 1}, f)
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(SV.main_up(["--profile", "trade", "--i-understand-live", "--dry-run"]), 2)
        self.assertIn("KILL", err.getvalue())

    def test_trade_clean_prints_plan_dry_run_only(self):
        root = H.mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_up(["--profile", "trade", "--i-understand-live", "--dry-run", "--services", "exec,dash"]), 0)
        text = out.getvalue()
        self.assertIn("профиль trade", text)
        self.assertIn("реальные деньги", text)
        self.assertIn("exec, dash", text)
        self.assertFalse(os.path.exists(S.lock_file(root)))                 # dry-run ничего не запускал

    def test_default_profile_is_paper_and_default_services(self):
        root = H.mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_up(["--dry-run"]), 0)
        self.assertIn("профиль paper", out.getvalue())
        self.assertIn("exec, scan, feed, context, intel, sync, dispatch, ctl, dash", out.getvalue())     # WP12: ctl; feed, context (context.json), intel (facts.json), sync (капитал) входят по умолчанию

    def test_unknown_service_and_profile(self):
        root = H.mk_root(self)
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(SV.main_up(["--services", "exec,xx", "--dry-run"]), 2)
            self.assertEqual(SV.main_up(["--profile", "mainnet", "--dry-run"]), 2)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.root = H.mk_root(self)
        self.now = 1_800_000_000.0

    def put_running(self, services, **extra):
        S.write_json(S.lock_file(self.root), {"pid": os.getpid(), "started": self.now - 100, "profile": "paper", "observe": False, "services": list(services)})
        S.write_json(S.status_file(self.root), dict({"ts_epoch": self.now, "running": True, "services": services}, **extra))

    def put_exec(self, alive=True, positions=0):
        os.makedirs(os.path.join(self.root, "runtime"), exist_ok=True)
        ts = self.now if alive else self.now - 500
        S.write_json(os.path.join(self.root, "runtime", "exec_health.json"), {"ts_epoch": ts, "alive": True, "mode": "paper", "positions": positions, "hold": None})
        os.utime(os.path.join(self.root, "runtime", "exec_health.json"), (ts, ts))

    def svc(self, state="running", **kw):
        d = {"state": state, "pid": 1, "started_ts": self.now - 50, "restarts": 0, "tries": 0, "hb_ok": True, "hb_age_s": 1.0}
        d.update(kw)
        return d

    def test_off_when_not_running(self):
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "off")
        self.assertFalse(st["running"])

    def test_green(self):
        self.put_running({"exec": self.svc(), "scan": self.svc()})
        self.put_exec(True)
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "green", st["reasons"])
        self.assertEqual(st["supervisor"]["profile"], "paper")
        self.assertEqual(st["services"]["exec"]["uptime_s"], 50.0)
        self.assertIn("OK", ST.format_line(st))
        self.assertIn("ЗЕЛЁНЫЙ", ST.format_text(st))

    def test_failed_service_is_red(self):
        self.put_running({"exec": self.svc(), "scan": self.svc("failed", last_error="упал, код 1", tries=5)})
        self.put_exec(True)
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "red")
        self.assertTrue([r for r in st["reasons"] if "scan" in r])

    def test_exec_dead_with_positions_is_red_with_reminder(self):
        self.put_running({"exec": self.svc("failed", last_error="упал"), "scan": self.svc()}, positions_seen=2)
        self.put_exec(False, positions=2)
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "red")
        self.assertTrue([r for r in st["reasons"] if "биржевым стопом" in r])

    def test_backoff_and_hb_yellow_and_kill(self):
        self.put_running({"exec": self.svc(), "scan": self.svc("backoff", tries=2), "dispatch": self.svc(hb_ok=False)})
        self.put_exec(True)
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "yellow")
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        with open(os.path.join(self.root, "state", "KILL"), "w") as f:
            json.dump({"flatten": True, "reason": "t", "ts": 1}, f)
        st = ST.build_status(self.root, self.now)
        self.assertTrue(st["kill"])
        self.assertIn("KILL", ST.format_line(st))

    def test_unread_and_links(self):
        self.put_running({"exec": self.svc(), "dash": self.svc()})
        self.put_exec(True)
        S.write_json(S.unread_file(self.root), {"chat": {"unread": 3, "wake_unread": 1}})
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["unread_total"], 3)
        self.assertEqual(st["links"]["dash"], "http://127.0.0.1:8765/")
        self.assertIn("chat:3!", ST.format_line(st))

    def test_dead_supervisor_pid_means_off(self):
        dead = subprocess.Popen([H.PY, "-c", "pass"])
        dead.wait()
        S.write_json(S.lock_file(self.root), {"pid": dead.pid, "profile": "paper"})
        S.write_json(S.status_file(self.root), {"ts_epoch": self.now, "services": {"exec": self.svc()}})
        st = ST.build_status(self.root, self.now)
        self.assertEqual(st["color"], "off")
        self.assertEqual(st["services"]["exec"]["state"], "stopped")

    def test_live_status_with_platform_thread(self):
        root0 = H.mk_root(self)
        pf, root, _ = mk_platform(self, {"scan": H.script(root0, "s.py", H.SLEEP)})
        S.write_json(S.lock_file(root), {"pid": os.getpid(), "started": time.time(), "profile": "paper", "services": ["scan"]})
        t = threading.Thread(target=pf.run, kwargs={"handle_signals": False})
        t.start()
        try:
            self.assertTrue(H.wait_for(lambda: (S.read_json(S.status_file(root)) or {}).get("services", {}).get("scan", {}).get("state") == "running"))
            st = ST.build_status(root)
            self.assertTrue(st["running"])
            self.assertEqual(st["services"]["scan"]["state"], "running")
        finally:
            pf.request_stop()
            t.join(10)


class AutostartTest(unittest.TestCase):
    def test_launchd_plist(self):
        txt = autostart.launchd_plist("/Users/me/tb & co", ["--profile", "paper", "--detach", "--services", "exec,scan"])
        self.assertIn("com.traider-bot.platform", txt)
        self.assertIn("<string>/Users/me/tb &amp; co/tb</string>", txt)
        self.assertIn("<string>--profile</string>", txt)
        self.assertNotIn("--detach", txt)
        self.assertIn("SuccessfulExit", txt)
        self.assertIn("RunAtLoad", txt)
        import plistlib
        d = plistlib.loads(txt.encode("utf-8"))                              # корректный plist
        self.assertEqual(d["ProgramArguments"][:2], ["/Users/me/tb & co/tb", "up"])
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})

    def test_windows_script(self):
        txt = autostart.windows_script("C:\\tb", ["--profile", "paper"])
        self.assertIn("Register-ScheduledTask", txt)
        self.assertIn("tb.cmd", txt)
        self.assertIn("--profile paper", txt)

    def test_print_options_write_nothing(self):
        root = H.mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_up(["--print-launchd", "--profile", "paper"]), 0)
            self.assertEqual(SV.main_up(["--print-windows"]), 0)
        self.assertIn("<plist", out.getvalue())
        self.assertIn("Register-ScheduledTask", out.getvalue())
        self.assertEqual(os.listdir(root), [])


class DownTest(unittest.TestCase):
    def test_down_when_not_running(self):
        root = H.mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_down([]), 0)
        self.assertIn("не запущена", out.getvalue())

    def test_status_cli_exit_codes_and_json(self):
        root = H.mk_root(self)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=out):
            self.assertEqual(SV.main_status(["--json"]), 1)
        self.assertEqual(json.loads(out.getvalue())["color"], "off")

    def test_down_writes_stop_request_for_live_supervisor(self):
        root = H.mk_root(self)
        sleeper = subprocess.Popen([H.PY, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sleeper.kill)
        S.write_json(S.lock_file(root), {"pid": sleeper.pid, "profile": "paper"})
        seen = {}

        def watcher():
            H.wait_for(lambda: os.path.exists(S.stop_req_file(root)))
            seen["req"] = os.path.exists(S.stop_req_file(root))
            sleeper.kill()                                              # «супервизор» останавливается по запросу
            sleeper.wait()

        t = threading.Thread(target=watcher)
        t.start()
        with mock.patch.dict(os.environ, {"TB_ROOT": root}), mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(SV.main_down(["--timeout", "10"]), 0)
        t.join(5)
        self.assertTrue(seen.get("req"))
        self.assertIsNotNone(sleeper.poll())


if __name__ == "__main__":
    unittest.main()
