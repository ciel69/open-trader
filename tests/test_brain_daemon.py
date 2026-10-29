"""WP11-C2: демон `brain run` (воркеры, heartbeat, reload.req, рестарт без потери очереди), CLI `brain ...`, подключение сервиса в платформу и цвет статуса."""
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401
import _platform_helpers as H
from _brain import BrainBase, jdump, jload, pm
from tb.brain import api_daemon, brain_cli, conf as conf_mod, jobs, swap
from tb.core import bus
from tb.platform import services as S, status as ST


def cli(*argv):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rc = api_daemon.main(list(argv))
    return rc, o.getvalue(), e.getvalue()


class Daemon(BrainBase):
    def test_daemon_processes_queue_and_writes_heartbeat(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        r.clock = time.time
        for i in range(6):
            self.job("J-%d" % i, key="plan:PL-1028-SOL-%02d" % (i + 1), plan="PL-1028-SOL-%02d" % (i + 1))
        self.gw.brief = {"ok": True, "brief_id": "B-1", "plans": [], "positions": []}
        self.factory.scripts["ds-pro"] = [lambda req: json.dumps(pm("hold", plan=json.loads(req.messages[0]["content"].split("<event>")[1].split("</event>")[0])["plan"]))]
        r._clients.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            api_daemon.run_daemon(r, self.root, max_seconds=2.5, poll_s=0.05)
        self.assertEqual(sorted(j["status"] for j in jobs.list_jobs(self.root)), ["done"] * 6)
        age, hb = S.read_hb("brain", self.root, time.time())
        self.assertLess(age, 5)
        for k in ("protect_only", "health", "rev", "queue", "cfg_source"):
            self.assertIn(k, hb)
        self.assertEqual(hb["health"], "yellow" if r.errors else "green")
        self.assertIn("воркеров 2", out.getvalue())

    def test_restart_does_not_lose_jobs(self):
        self.job("J-1", key="plan:A1")
        self.job("J-2", key="plan:A2")
        j = jobs.pick(self.root, self.t, "brain")
        d = self.get_job(j["id"])
        d["taken_by"] = 999999                                  # прежний демон умер на задании
        jdump(d, os.path.join(jobs.jobs_dir(self.root), j["id"] + ".json"))
        r = self.runner({"ds-pro": [pm("hold")]})
        r.clock = time.time
        with redirect_stdout(io.StringIO()):
            api_daemon.run_daemon(r, self.root, max_seconds=1.5, poll_s=0.05)
        self.assertEqual(sorted(x["status"] for x in jobs.list_jobs(self.root)), ["done", "done"])
        self.assertEqual(self.get_job(j["id"])["requeued"], 1)

    def test_reload_req_consumed_and_conf_reloaded(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        r.clock = time.time
        swap.reload(self.root, {}, time.time(), by="t")
        self.brain_doc["budget"]["job_usd"] = 0.0123
        self.write_conf()
        with redirect_stdout(io.StringIO()):
            api_daemon.run_daemon(r, self.root, max_seconds=1.2, poll_s=0.05)
        self.assertFalse(os.path.exists(swap.reload_req_file(self.root)))
        self.assertEqual(r.conf.budget["job_usd"], 0.0123)

    def test_stop_event_ends_loop_and_returns_inflight_to_queue(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        r.clock = time.time
        self.job("J-1")
        gate = threading.Event()

        def slow(client, req):
            gate.set()
            time.sleep(0.6)
        self.factory("ds-pro", r.conf.profiles["ds-pro"]).on_call = slow
        t = threading.Thread(target=lambda: api_daemon.run_daemon(r, self.root, poll_s=0.05, join_s=0.1))
        with redirect_stdout(io.StringIO()):
            t.start()
            self.assertTrue(gate.wait(5))
            r.stop_event.set()
            t.join(10)
        st = self.get_job("J-1")["status"]
        self.assertIn(st, ("new", "done"))                     # не потеряно и не зависло в taken
        self.assertNotEqual(st, "taken")

    def test_worker_survives_exceptions(self):
        r = self.runner({"ds-pro": [pm("hold")]})
        r.clock = time.time
        calls = {"n": 0}
        orig = r.step

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("x")
            return orig()
        r.step = flaky
        self.job("J-1")
        with redirect_stdout(io.StringIO()):
            api_daemon.run_daemon(r, self.root, max_seconds=1.5, poll_s=0.05)
        self.assertEqual(self.get_job("J-1")["status"], "done")


class Cli(BrainBase):
    def test_check_and_status(self):
        rc, out, _ = cli("check")
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(out)["ok"])
        rc, out, _ = cli("status", "--json")
        st = json.loads(out)
        self.assertEqual((rc, st["live_team"], st["profiles"]["position_manager"], st["rev"]), (0, "A", "ds-pro", 0))
        rc, out, _ = cli("status")
        self.assertIn("ds-pro", out)

    def test_check_reports_errors(self):
        self.brain_doc["teams"]["A"]["roles"]["position_manager"]["primary"] = "нет"
        self.write_conf()
        rc, out, _ = cli("check")
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["brain_errors"])

    def test_use_t0_and_rollback_and_verify(self):
        rc, out, _ = cli("use", "shared.news_reader", "ds-pro")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["data"]["rev"], 1)
        self.assertEqual(swap.read_swaps(self.root)[-1]["by"], "owner-cli")
        rc, out, _ = cli("use", "shared.news_reader", "nope")
        self.assertEqual((rc, json.loads(out)["code"]), (1, "E_SWAP_PROFILE"))
        rc, out, _ = cli("rollback")
        self.assertEqual(rc, 1)                                  # предыдущая ревизия = brain.json
        rc, out, _ = cli("verify")
        self.assertEqual(rc, 0)

    def live_ready(self):
        self.set_platform("live")
        self.agents["api"]["limits"]["modes"] = ["paper", "live"]
        jdump({"agents": self.agents}, self.p("config", "agents.json"))
        for p in conf_mod.load(self.root).profiles.values():
            swap.record_probe(self.root, p, True, time.time())

    def test_live_swap_refused_without_tty(self):
        self.live_ready()
        with mock.patch.object(brain_cli, "_tty", return_value=False):
            rc, out, err = cli("use", "A.position_manager", "ds-flash", "--confirm", "СМЕНИТЬ МОДЕЛЬ position_manager ds-pro -> ds-flash", "--accept", "W_SWAP_NO_AB")
        self.assertEqual(rc, 2)
        self.assertIn("TTY", err)
        self.assertEqual(swap.load_effective(self.root).rev, 0)

    def test_live_swap_with_tty_needs_exact_phrase_and_acceptances(self):
        self.live_ready()
        with mock.patch.object(brain_cli, "_tty", return_value=True):
            rc, out, _ = cli("use", "A.position_manager", "ds-flash")
            self.assertEqual(rc, 2)
            d = json.loads(out)
            self.assertEqual(d["code"], "E_SWAP_PHRASE")
            self.assertEqual(d["phrase"], "СМЕНИТЬ МОДЕЛЬ position_manager ds-pro -> ds-flash")
            rc, out, _ = cli("use", "A.position_manager", "ds-flash", "--confirm", d["phrase"])
            self.assertEqual((rc, json.loads(out)["code"]), (2, "E_WARN"))
            rc, out, _ = cli("use", "A.position_manager", "ds-flash", "--confirm", d["phrase"], "--ack", "я принимаю риск", "--accept", "W_SWAP_NO_AB")
            self.assertEqual((rc, json.loads(out)["code"]), (1, "E_WARN_CHANGED"))
            rc, out, _ = cli("use", "A.position_manager", "ds-flash", "--confirm", d["phrase"], "--ack", "я принимаю риск", "--accept", "W_SWAP_NO_AB", "--accept", "W_SWAP_PAPER_HISTORY")
            self.assertEqual(rc, 0, out)
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "ds-flash")

    def test_jobs_listing_and_unknown_command(self):
        self.job("J-1")
        rc, out, _ = cli("jobs")
        self.assertIn("J-1", out)
        rc, _, _ = cli("bogus")
        self.assertEqual(rc, 2)
        rc, _, _ = cli()
        self.assertEqual(rc, 2)

    def test_probe_without_live_flag_uses_no_network(self):
        with mock.patch("tb.llm.transport.Transport", side_effect=AssertionError("сеть")):
            rc, out, _ = cli("probe", "ds-pro")
        self.assertEqual(rc, 0)
        self.assertIn("сеть не использовалась", out)
        rc, _, _ = cli("probe")
        self.assertEqual(rc, 2)

    def test_reload_from_config_cli(self):
        cli("use", "A.position_manager", "an-sonnet")
        rc, out, _ = cli("reload", "--from-config")
        self.assertEqual(rc, 0, out)
        self.assertEqual(swap.load_effective(self.root).teams["A"]["roles"]["position_manager"]["primary"], "ds-pro")
        rc, out, _ = cli("reload")
        self.assertEqual(rc, 0)


class PlatformIntegration(unittest.TestCase):
    def test_brain_service_in_default_set_except_trade_and_available(self):
        cfg = S.load_cfg(None)
        for prof in ("paper", "testnet"):
            self.assertIn("brain", S.default_services(prof, cfg))       # `start`/`up` отсеивают его, если нет условий (tests/test_platform_quick.py)
        self.assertNotIn("brain", S.default_services("trade", cfg))
        cmd = S.service_cmd("brain", "paper", False, cfg, py="py")
        self.assertEqual(cmd[-2:], ["brain", "run"])
        self.assertEqual(S.parse_services("exec,brain"), ["exec", "brain"])

    def test_brain_env_has_no_exchange_secrets(self):
        env = S.child_env("brain", {"BYBIT_API_SECRET": "x", "ANTHROPIC_API_KEY": "y", "HTTPS_PROXY": "p", "PATH": "/bin"}, "/tmp/r")
        self.assertNotIn("BYBIT_API_SECRET", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_status_color_follows_brain_heartbeat(self):
        root = H.mk_root(self)
        now = 1_800_000_000.0
        S.write_json(S.lock_file(root), {"pid": os.getpid(), "started": now - 100, "profile": "paper", "observe": False, "services": ["brain"]})
        svc = {"state": "running", "pid": 1, "started_ts": now - 50, "restarts": 0, "tries": 0, "hb_ok": True, "hb_age_s": 1.0}
        S.write_json(S.status_file(root), {"ts_epoch": now, "running": True, "services": {"brain": svc}})
        S.write_hb("brain", {"protect_only": False, "health": "green"}, root, now=now)
        self.assertEqual(ST.build_status(root, now)["color"], "green")
        S.write_hb("brain", {"protect_only": False, "health": "yellow"}, root, now=now)
        st = ST.build_status(root, now)
        self.assertEqual(st["color"], "yellow")
        S.write_hb("brain", {"protect_only": True, "reason": "бюджет LLM исчерпан", "health": "red"}, root, now=now)
        st = ST.build_status(root, now)
        self.assertEqual(st["color"], "red")
        self.assertTrue(any("только защита" in r for r in st["reasons"]))


if __name__ == "__main__":
    unittest.main()
