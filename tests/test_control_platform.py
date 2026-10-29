"""WP12: подключение к платформе: сервис ctl в супервизоре, смена профиля исполнителя (reconf.req/ack), запуск самого сервиса. Дочерние процессы-пустышки, без сети."""
import json
import os
import threading
import time
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _ctl
import _platform_helpers as H
from tb.control import ctlstate, service
from tb.platform import reconf
from tb.platform import services as S
from tb.platform import supervisor as SV


def mk(test, cmds):
    root = H.mk_root(test)
    pf = SV.Platform(root, "paper", False, list(cmds), H.fast_cfg(), base_env=dict(os.environ), cmds=cmds, emit=lambda *a, **k: None)
    test.addCleanup(pf.stop_all)
    return pf, root


def put(root, d):
    S.write_json(S.reconf_req_file(root), d)


def ack(root):
    return S.read_json(S.reconf_ack_file(root))


class TestReconf(unittest.TestCase):
    def test_bad_requests(self):
        r0 = H.mk_root(self)
        pf, root = mk(self, {"exec": H.script(r0, "e.py", H.SLEEP)})
        pf.start_all()
        self.assertFalse(reconf.handle(pf))                                   # нет файла
        for bad in ({"id": "a", "profile": "live", "observe": False}, {"id": "a", "profile": "paper", "observe": "no"}, {"profile": "paper", "observe": False}, [], "x"):
            put(root, bad)
            self.assertTrue(reconf.handle(pf))
            self.assertEqual((ack(root)["ok"], ack(root)["code"]), (False, "BAD_REQUEST"))
            self.assertFalse(os.path.exists(S.reconf_req_file(root)))
        self.assertEqual(pf.profile, "paper")

    def test_same_profile_is_noop_and_no_exec(self):
        r0 = H.mk_root(self)
        pf, root = mk(self, {"exec": H.script(r0, "e.py", H.SLEEP)})
        pf.start_all()
        pid = pf.svcs["exec"].pid()
        put(root, {"id": "r1", "profile": "paper", "observe": False})
        reconf.handle(pf)
        self.assertEqual((ack(root)["id"], ack(root)["code"]), ("r1", "SAME"))
        self.assertEqual(pf.svcs["exec"].pid(), pid)
        pf2, root2 = mk(self, {"scan": H.script(r0, "s.py", H.SLEEP)})
        put(root2, {"id": "r2", "profile": "testnet", "observe": False})
        reconf.handle(pf2)
        self.assertEqual(ack(root2)["code"], "NO_EXEC")

    def test_switch_restarts_only_exec(self):
        r0 = H.mk_root(self)
        sleep_cmd = H.script(r0, "e2.py", H.SLEEP)
        pf, root = mk(self, {"exec": H.script(r0, "e.py", H.SLEEP), "scan": H.script(r0, "s.py", H.SLEEP)})
        pf.start_all()
        scan_pid, exec_pid = pf.svcs["scan"].pid(), pf.svcs["exec"].pid()
        S.write_json(S.lock_file(root), {"pid": os.getpid(), "profile": "paper", "observe": False})
        put(root, {"id": "r3", "profile": "testnet", "observe": True})
        with mock.patch.object(S, "service_cmd", return_value=sleep_cmd) as sc:
            reconf.handle(pf)
        self.assertEqual(sc.call_args[0][:3], ("exec", "testnet", True))
        a = ack(root)
        self.assertEqual((a["ok"], a["code"], a["profile"], a["observe"]), (True, "OK", "testnet", True))
        self.assertEqual(pf.svcs["scan"].pid(), scan_pid)                      # остальные сервисы не тронуты
        self.assertNotEqual(pf.svcs["exec"].pid(), exec_pid)
        self.assertEqual((pf.profile, pf.observe), ("testnet", True))
        self.assertEqual(S.read_json(S.lock_file(root))["profile"], "testnet")  # `./tb status` видит новый профиль
        self.assertEqual(S.read_json(S.status_file(root))["profile"], "testnet")

    def test_trade_profile_refused_under_kill(self):
        from tb.core import exec_cfg
        r0 = H.mk_root(self)
        pf, root = mk(self, {"exec": H.script(r0, "e.py", H.SLEEP)})
        pf.start_all()
        pid = pf.svcs["exec"].pid()
        exec_cfg.set_kill(False, "t", root)
        put(root, {"id": "r4", "profile": "trade", "observe": False})
        reconf.handle(pf)
        self.assertEqual((ack(root)["ok"], ack(root)["code"]), (False, "KILL"))
        self.assertEqual(pf.svcs["exec"].pid(), pid)
        self.assertEqual(pf.profile, "paper")


class TestServiceWiring(unittest.TestCase):
    def test_ctl_is_a_platform_service(self):
        self.assertIn("ctl", S.ORDER)
        self.assertLess(S.ORDER.index("ctl"), S.ORDER.index("dash"))
        cfg = S.load_cfg(_bootstrap.ROOT)
        for prof in S.PROFILES:
            self.assertIn("ctl", S.default_services(prof, cfg))
        cmd = S.service_cmd("ctl", "paper", False, cfg)
        self.assertEqual(cmd[1:], ["-m", "tb.control.service", "run"])
        self.assertTrue(S.module_available("tb.control.service"))
        self.assertEqual(S.stop_order(["exec", "ctl", "dash"])[-1], "exec")
        with open(os.path.join(_bootstrap.ROOT, "config", "platform.json"), encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("ctl", raw["svc"])
        self.assertEqual(raw["svc"]["ctl"]["hb"], "file")

    def test_ctl_child_env_has_no_exchange_or_llm_secrets(self):
        env = S.child_env("ctl", {"BYBIT_API_KEY": "k", "ANTHROPIC_API_KEY": "a", "HTTPS_PROXY": "p", "PATH": "/bin", "TB_KEYS_DIR": "/x"}, "/tmp/x", ["exec", "ctl"], "paper")
        for k in ("BYBIT_API_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY"):
            self.assertNotIn(k, env)
        self.assertEqual(env["TB_PLATFORM_SERVICE"], "ctl")


class TestReconfRequestSide(unittest.TestCase):
    def test_request_ack_roundtrip_and_timeout(self):
        e = _ctl.Env(self)
        c = e.ctl()
        seen = {}

        def fake_supervisor():
            for _ in range(200):
                req = S.read_json(S.reconf_req_file(e.root))
                if req:
                    seen.update(req)
                    S.write_json(S.reconf_ack_file(e.root), {"id": req["id"], "ok": True, "code": "OK"})
                    return
                time.sleep(0.01)
        t = threading.Thread(target=fake_supervisor)
        t.start()
        c.sleep = lambda s: time.sleep(0.02)
        c.clock = time.time
        self.assertEqual(c._reconf_files("trade", False, 5), (True, "OK"))
        t.join()
        self.assertEqual((seen["profile"], seen["observe"]), ("trade", False))
        c.clock_t = [0]
        t0 = time.time()
        c.clock = lambda: time.time() + (time.time() - t0) * 100                # ускоренное время: ожидание без ответа истекает
        self.assertEqual(c._reconf_files("paper", False, 1)[1], "timeout")


class TestServiceLoop(unittest.TestCase):
    def test_run_writes_status_heartbeat_and_refuses_second_instance(self):
        e = _ctl.Env(self)
        e.good_live()
        out = {}

        def first():
            out["rc"] = service.run(e.root, max_seconds=1.5)
        th = threading.Thread(target=first)
        th.start()
        time.sleep(0.8)
        self.assertEqual(service.run(e.root, max_seconds=0.5), service.EXIT_ALREADY)
        st = S.read_json(os.path.join(e.root, "state", "control", "status.json"))
        self.assertEqual(st["platform"]["state"], "stopped")
        self.assertTrue(st["ctl"]["alive"])
        age, hb = S.read_hb("ctl", e.root)
        self.assertLess(age, 3)
        th.join()
        self.assertEqual(out["rc"], 0)
        self.assertFalse(os.path.exists(os.path.join(e.root, "state", "control", "ctl.lock")))      # блокировка снята

    def test_dead_ctl_status_goes_stale(self):
        from tb.control import client
        e = _ctl.Env(self)
        service.run(e.root, max_seconds=0.6)
        self.assertFalse(client.read_status(e.root)["stale"])
        self.assertTrue(client.read_status(e.root, time.time() + 40)["stale"])


if __name__ == "__main__":
    unittest.main()
