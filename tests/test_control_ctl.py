"""WP12: сервис ctl (handlers.Ctl) на фейках: подпись, срок, повтор, nonce, фразы, предупреждения, старт/стоп дня, автопонижение, режим, KILL, уровень риска,
очередь под нагрузкой, отсутствие секретов в файлах. Без сети; биржа и процессы не запускаются (переключение исполнителя подменено)."""
import json
import os
import time
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb.control import audit, auth, client, ctlstate
from tb.core import bus, exec_cfg

NOW = _ctl.NOW
LIVE = {"mode": "live", "session": "intraday", "minutes": 60}
PAPER = {"mode": "paper", "session": "intraday", "minutes": 60}


class Base(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.good_live()
        self.reconfs = []
        self.rc_ok = True
        self.c = self.e.ctl(reconf=self.fake_reconf)
        self.e.good_exec(profile="paper")

    def fake_reconf(self, profile, observe, timeout):
        self.reconfs.append((profile, observe))
        if not self.rc_ok:
            return False, "timeout"
        self.e.good_exec(profile=profile, observe=observe, now=self.e.clock_t[0])
        return True, "OK"

    def start_live(self, **kw):
        return self.e.confirmed(self.c, "day.start", LIVE, **kw)

    def codes(self):
        return [(a["action"], a["code"]) for a in self.e.audit()]


class TestPipeline(Base):
    def test_tampered_command_is_rejected_and_audited(self):
        c = self.e.cmd("day.stop", {"mode": "soft"})
        c["args"] = {"mode": "soft", "x": 1}
        client.submit(c, self.e.root)
        self.c.poll_once()
        r = client.read_result(c["id"], self.e.root)
        self.assertEqual((r["status"], r["code"]), ("rejected", "E_MAC"))
        self.assertIn(("day.stop", "E_MAC"), self.codes())
        self.assertEqual(os.listdir(self.e.p("state", "control", "_inbox")), [])

    def test_garbage_inbox_files(self):
        d = self.e.p("state", "control", "_inbox")
        os.makedirs(d, exist_ok=True)
        _ctl.wr(os.path.join(d, "K-20261027T130000-aaaaaa.json"), "{не json")
        _ctl.wr(os.path.join(d, "K-20261027T130000-bbbbbb.json"), "[]")
        _ctl.wr(os.path.join(d, "K-20261027T130000-cccccc.json"), '{"x":"' + "y" * 20000 + '"}')
        _ctl.wr(os.path.join(d, "K-20261027T130000-dddddd.json"), json.dumps({"schema": "tb.ctl/1", "id": "K-20261027T130000-dddddd", "ts": NOW, "src": "dash", "action": "day.stop",
                                                                               "args": {}, "mac": "0" * 64}))
        self.assertEqual(self.c.poll_once(), 4)
        self.assertEqual(os.listdir(d), [])
        codes = [a["code"] for a in self.e.audit()]
        self.assertEqual(codes.count("E_ARGS") + codes.count("E_MAC"), 4)
        self.assertTrue(audit.verify(self.e.root)["ok"])

    def test_expired_and_future(self):
        r, _ = self.e.send(self.c, "day.stop", {"mode": "soft"}, ts=NOW - 500)
        self.assertEqual((r["status"], r["code"]), ("expired", "E_EXPIRED"))
        r, _ = self.e.send(self.c, "day.stop", {"mode": "soft"}, ts=NOW + 500)
        self.assertEqual(r["code"], "E_EXPIRED")

    def test_replay_is_rejected_even_after_restart(self):
        c = self.e.cmd("day.stop", {"mode": "soft"})
        client.submit(c, self.e.root)
        self.c.poll_once()
        first = client.read_result(c["id"], self.e.root)
        self.assertEqual(first["code"], "E_STATE")                  # день не идёт
        client.submit(c, self.e.root)
        self.c.poll_once()
        self.assertEqual(client.read_result(c["id"], self.e.root)["code"], "E_STATE")      # результат первого исполнения не перезаписан
        self.assertEqual(self.codes().count(("day.stop", "E_DUP")), 1)
        c2 = self.e.ctl()                                           # перезапуск сервиса
        client.submit(c, self.e.root)
        c2.poll_once()
        self.assertEqual(self.codes().count(("day.stop", "E_DUP")), 2)

    def test_no_key_everything_rejected(self):
        os.remove(auth.control_file())
        c = {"schema": "tb.ctl/1", "id": "K-20261027T130000-aaaaaa", "ts": NOW, "src": "dash", "action": "day.stop", "args": {"mode": "soft"}, "mac": "1" * 64}
        client.submit(c, self.e.root)
        self.c.poll_once()
        self.assertEqual(client.read_result(c["id"], self.e.root)["code"], "E_MAC")


class TestPaperDay(Base):
    def test_cycle(self):
        r = self.e.confirmed(self.c, "day.start", PAPER)
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        day = r["data"]["day_id"]
        st = ctlstate.platform_state(self.e.root)
        self.assertEqual((st["state"], st["mode"], st["day_id"], st["trading"]), ("running", "paper", day, True))
        self.assertTrue(self.e.r("state/control/trading.json")["enabled"])
        self.assertEqual(self.e.r("state/days/%s/day.json" % day)["mode"], "paper")
        r2, _ = self.e.send(self.c, "day.start", PAPER)
        self.assertEqual(r2["code"], "E_STATE")
        r3, _ = self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.assertEqual((r3["status"], r3["code"]), ("done", "OK"))
        self.assertIn("stub", r3["data"]["audit"])                  # точка интеграции WP14: аудит закрытия дня — заглушка
        self.assertFalse(self.e.r("state/control/trading.json")["enabled"])
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")
        self.assertEqual(self.e.send(self.c, "day.stop", {"mode": "soft"})[0]["code"], "E_STATE")
        self.assertEqual(ctlstate.live_starts(self.e.root, NOW)["starts"], [])
        self.assertEqual(self.reconfs, [])                          # исполнитель уже в paper: перезапуск не нужен

    def test_observe_day_does_not_enable_trading_and_switches_exec(self):
        self.e.good_exec(profile="paper", observe=False)
        r = self.e.confirmed(self.c, "day.start", {"mode": "observe", "session": "intraday", "minutes": 30})
        self.assertEqual(r["code"], "OK")
        self.assertEqual(self.reconfs, [("paper", True)])
        self.assertFalse(self.e.r("state/control/trading.json")["enabled"])

    def test_kill_blocks_start(self):
        exec_cfg.set_kill(False, "t", self.e.root)
        r, _ = self.e.send(self.c, "day.start", PAPER)
        self.assertEqual(r["code"], "E_GATE")

    def test_background_thread_mode(self):
        c = self.e.ctl(sync=False, clock=time.time, reconf=self.fake_reconf)
        self.e.good_exec(profile="paper", now=time.time())
        cm = self.e.cmd("day.start", PAPER, ts=time.time())
        client.submit(cm, self.e.root)
        c.poll_once()
        r = None
        for _ in range(100):
            r = client.read_result(cm["id"], self.e.root)
            if r and r["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(r["code"], "OK")


class TestLiveStart(Base):
    def test_challenge_gives_phrases_nonce_and_gates(self):
        ch = self.e.challenge(self.c, "day.start", LIVE)
        d = ch["data"]
        self.assertEqual((ch["status"], ch["code"]), ("done", "OK"))
        self.assertTrue(d["danger"] and d["nonce"])
        self.assertEqual(d["phrases"], ["ТОРГУЮ LIVE 2026-10-27 УРОВЕНЬ 3"])
        self.assertEqual(d["expires_in"], 60)
        self.assertEqual(len(d["gates"]), 8)

    def test_challenge_blocked_by_hard_gate_has_reasons_and_no_nonce(self):
        exec_cfg.set_kill(False, "t", self.e.root)
        ch = self.e.challenge(self.c, "day.start", LIVE)
        self.assertEqual((ch["status"], ch["code"]), ("rejected", "E_GATE"))
        self.assertIsNone(ch["data"]["nonce"])
        self.assertEqual([g["id"] for g in ch["data"]["gates"] if not g["ok"]], ["H1"])

    def test_without_nonce_or_with_bad_nonce(self):
        r, _ = self.e.send(self.c, "day.start", LIVE, phrases=["ТОРГУЮ LIVE 2026-10-27 УРОВЕНЬ 3"])
        self.assertEqual(r["code"], "E_NONCE")
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])           # слишком рано
        self.assertEqual((r["code"], r["data"]["why"]), ("E_NONCE", "early"))
        self.e.advance(61)                                                                              # слишком поздно
        self.e.good_exec(profile="paper", now=self.e.clock_t[0])
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])
        self.assertEqual((r["code"], r["data"]["why"]), ("E_NONCE", "late"))
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")

    def test_nonce_bound_to_args_and_session_and_single_use(self):
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        self.e.advance(3)
        r, _ = self.e.send(self.c, "day.start", dict(LIVE, team="B"), nonce=d["nonce"], phrases=d["phrases"])
        self.assertEqual(r["code"], "E_NONCE")
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"], sess="s-ffff0000")
        self.assertEqual(r["code"], "E_NONCE")
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.e.good_exec(profile="trade", now=self.e.clock_t[0])
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])
        self.assertIn(r["code"], ("E_NONCE", "E_GATE"))             # повторное использование nonce невозможно

    def test_wrong_phrase(self):
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        self.e.advance(3)
        for bad in (["торгую live 2026-10-27 уровень 3"], ["ТОРГУЮ LIVE 2026-10-26 УРОВЕНЬ 3"], None, ["ТОРГУЮ LIVE 2026-10-27 УРОВЕНЬ 3", "я принимаю риск"]):
            r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=bad)
            self.assertEqual(r["code"], "E_PHRASE", bad)

    def test_success_switches_exec_records_start_and_wakes_bus(self):
        r = self.start_live()
        self.assertEqual((r["status"], r["code"]), ("done", "OK"), r)
        self.assertEqual(self.reconfs, [("trade", False)])
        st = ctlstate.platform_state(self.e.root)
        self.assertEqual((st["state"], st["mode"], st["trading"]), ("running", "live", True))
        self.assertEqual(len(ctlstate.live_starts(self.e.root, NOW)["starts"]), 1)
        ev = [x for x in bus.read(0, 100, root=self.e.root) if x["type"] == "control_cmd" and x["d"].get("action") == "day.start"]
        self.assertEqual(ev[-1]["pri"], "wake")
        self.assertEqual(ev[-1]["d"]["code"], "OK")

    def test_soft_warnings_need_each_acceptance_and_two_phrases(self):
        self.e.w("state/trades.jsonl", "", raw=True)                # W1 и W9
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        ids = sorted(w["id"] for w in d["warnings"])
        self.assertEqual(ids, ["W1", "W9"])
        self.assertEqual(d["phrases"][1], "я принимаю риск")
        self.e.advance(3)
        ph = d["phrases"]
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=ph)
        self.assertEqual(r["code"], "E_WARN")
        self.assertEqual(sorted(w["id"] for w in r["data"]["warnings"]), ids)
        r, _ = self.e.send(self.c, "day.start", dict(LIVE, accepted=["W1"]), nonce=d["nonce"], phrases=ph)
        self.assertEqual(r["code"], "E_WARN_CHANGED")
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")
        r, _ = self.e.send(self.c, "day.start", dict(LIVE, accepted=ids), nonce=d["nonce"], phrases=ph[:1])
        self.assertEqual(r["code"], "E_PHRASE")                     # при предупреждениях нужна и вторая фраза
        r, _ = self.e.send(self.c, "day.start", dict(LIVE, accepted=ids), nonce=d["nonce"], phrases=ph)
        self.assertEqual((r["status"], r["code"]), ("done", "OK"))
        a = [x for x in self.e.audit() if x["action"] == "day.start" and x["code"] == "OK"][-1]
        self.assertEqual(sorted(w["id"] for w in a["warnings_accepted"]), ids)
        self.assertEqual(sorted(w["id"] for w in ctlstate.platform_state(self.e.root)["live_waivers"]), ids)
        day = r["data"]["day_id"]
        self.assertEqual(sorted(w["id"] for w in self.e.r("state/days/%s/day.json" % day)["live_waivers"]), ids)

    def test_warning_disappears_between_challenge_and_execution(self):
        self.e.w("state/trades.jsonl", "", raw=True)
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        ids = sorted(w["id"] for w in d["warnings"])
        self.e.good_live(now=self.e.clock_t[0])                      # история пополнилась: W1/W9 исчезли
        self.e.advance(3)
        self.e.good_exec(profile="paper", now=self.e.clock_t[0])
        r, _ = self.e.send(self.c, "day.start", dict(LIVE, accepted=ids), nonce=d["nonce"], phrases=d["phrases"])
        self.assertIn(r["code"], ("E_WARN_CHANGED", "E_PHRASE"))     # набор предупреждений изменился (или изменились требуемые фразы)
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")

    def test_new_warning_appears_after_challenge(self):
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        self.assertEqual(d["warnings"], [])
        self.e.w("state/trades.jsonl", "", raw=True)
        self.e.advance(3)
        self.e.good_exec(profile="paper", now=self.e.clock_t[0])
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])
        self.assertEqual(r["code"], "E_WARN")

    def test_gate_failure_at_execution_even_with_valid_nonce(self):
        d = self.e.challenge(self.c, "day.start", LIVE)["data"]
        self.e.advance(3)
        exec_cfg.set_kill(False, "t", self.e.root)
        r, _ = self.e.send(self.c, "day.start", LIVE, nonce=d["nonce"], phrases=d["phrases"])
        self.assertEqual(r["code"], "E_GATE")
        self.assertEqual(self.reconfs, [])

    def test_gates_rechecked_after_switch_and_rolled_back(self):
        def rc(profile, observe, timeout):
            self.reconfs.append((profile, observe))
            if profile == "trade":
                self.e.good_exec(profile="trade", observe=False, now=self.e.clock_t[0], ws={"public": {"up": True}, "private": {"up": False}})
            else:
                self.e.good_exec(profile=profile, observe=observe, now=self.e.clock_t[0])
            return True, "OK"
        self.c.reconf_fn = rc
        r = self.start_live()
        self.assertEqual(r["code"], "E_GATE")
        self.assertEqual(r["data"]["phase"], "after_switch")
        self.assertEqual([g["id"] for g in r["data"]["gates"]], ["H3"])
        self.assertEqual(self.reconfs, [("trade", False), ("paper", False)])    # вернули прежний профиль
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")
        self.assertEqual(ctlstate.live_starts(self.e.root, NOW)["starts"], [])
        self.assertFalse(self.e.exists("state/control/trading.json") and self.e.r("state/control/trading.json")["enabled"])

    def test_reconf_failure_rolls_back(self):
        self.rc_ok = False
        r = self.start_live()
        self.assertEqual(r["code"], "E_STATE")
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")

    def test_live_start_limits_h8(self):
        for i in range(3):
            ctlstate.note_live_start(self.e.root, NOW - 1000 * (i + 1))
        ch = self.e.challenge(self.c, "day.start", LIVE)
        self.assertEqual(ch["code"], "E_GATE")
        self.assertEqual([g["id"] for g in ch["data"]["gates"] if not g["ok"]], ["H8"])

    def test_cooldown_after_live_stop(self):
        self.assertEqual(self.start_live()["code"], "OK")
        self.e.send(self.c, "day.stop", {"mode": "soft"})
        self.e.good_exec(profile="trade", now=self.e.clock_t[0])
        ch = self.e.challenge(self.c, "day.start", LIVE)
        self.assertEqual((ch["code"], [g["id"] for g in ch["data"]["gates"] if not g["ok"]]), ("E_GATE", ["H8"]))

    def test_second_start_while_running_is_state_error(self):
        self.start_live()
        ch = self.e.challenge(self.c, "day.start", PAPER)
        self.assertEqual(ch["code"], "E_STATE")


class TestAutoDowngrade(Base):
    def run_live(self):
        self.assertEqual(self.start_live()["code"], "OK")
        self.e.good_exec(profile="trade", now=self.e.clock_t[0])

    def test_healthy_live_is_not_touched(self):
        self.run_live()
        self.e.advance(6)
        self.e.good_exec(profile="trade", now=self.e.clock_t[0])
        self.c.watch()
        st = ctlstate.platform_state(self.e.root)
        self.assertEqual((st["mode"], st["trading"]), ("live", True))

    def test_violation_downgrades_to_observe(self):
        self.run_live()
        self.e.advance(6)
        self.e.good_exec(profile="trade", now=self.e.clock_t[0], ws={"public": {"up": True}, "private": {"up": False}})
        self.c.watch()
        st = ctlstate.platform_state(self.e.root)
        self.assertEqual((st["state"], st["mode"], st["trading"]), ("running", "observe", False))
        self.assertFalse(self.e.r("state/control/trading.json")["enabled"])
        self.assertEqual(self.c.banner["level"], "alarm")
        self.assertIn("H3", self.c.banner["text"])
        self.assertIn(("auto.downgrade", "AUTO_DOWNGRADE"), self.codes())
        ev = [x for x in bus.read(0, 100, root=self.e.root) if x["type"] == "control_cmd" and x["d"].get("action") == "auto.downgrade"]
        self.assertTrue(ev and ev[-1]["pri"] == "wake")
        self.assertEqual(self.c.status()["platform"]["mode"], "observe")
        self.c.watch()                                               # повторно не срабатывает
        self.assertEqual(self.codes().count(("auto.downgrade", "AUTO_DOWNGRADE")), 1)

    def test_daily_loss_downgrades_and_emits_limit_hit(self):
        self.run_live()
        day = _ctl.iso(self.e.clock_t[0]).split("T")[0]
        rows = [{"ev": "open", "id": "Z", "mode": "live", "stype": "intraday"}, {"ev": "close", "id": "Z", "ts_close": day + "T08:00:00Z", "pnl_usdt": -4.0}]
        with open(self.e.p("state", "trades.jsonl"), "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        self.e.advance(6)
        self.e.good_exec(profile="trade", now=self.e.clock_t[0])
        self.c.watch()
        self.assertEqual(ctlstate.platform_state(self.e.root)["mode"], "observe")
        self.assertTrue([x for x in bus.read(0, 100, root=self.e.root) if x["type"] == "limit_hit"])


class TestModeAndKill(Base):
    def test_mode_set_only_safer(self):
        self.e.confirmed(self.c, "day.start", PAPER)
        for m in ("live", "testnet", "paper"):
            r, _ = self.e.send(self.c, "mode.set", {"mode": m})
            self.assertEqual(r["code"], "E_STATE", m)
        r, _ = self.e.send(self.c, "mode.set", {"mode": "observe"})
        self.assertEqual((r["code"], ctlstate.platform_state(self.e.root)["mode"]), ("OK", "observe"))
        self.assertEqual(self.e.send(self.c, "mode.set", {"mode": "observe"})[0]["code"], "E_STATE")

    def test_live_to_paper_refused_with_positions(self):
        self.start_live()
        snap = self.e.r("runtime/exec_snapshot.json")
        snap["positions"] = [{"sym": "SOL", "sl": 100.0}]
        self.e.w("runtime/exec_snapshot.json", snap)
        r, _ = self.e.send(self.c, "mode.set", {"mode": "paper"})
        self.assertEqual((r["code"], r["data"]["why"]), ("E_STATE", "open_positions"))
        snap["positions"] = []
        self.e.w("runtime/exec_snapshot.json", snap)
        r, _ = self.e.send(self.c, "mode.set", {"mode": "paper"})
        self.assertEqual(r["code"], "OK")
        self.assertEqual(self.reconfs[-1], ("paper", False))
        self.assertEqual(ctlstate.platform_state(self.e.root)["mode"], "paper")

    def test_mode_set_when_stopped(self):
        self.assertEqual(self.e.send(self.c, "mode.set", {"mode": "observe"})[0]["code"], "E_STATE")

    def test_kill_without_ctl_then_ctl_notices(self):
        self.e.confirmed(self.c, "day.start", PAPER)
        exec_cfg.set_kill(False, "из дашборда", self.e.root)        # так делает дашборд сам
        self.c.watch()
        st = ctlstate.platform_state(self.e.root)
        self.assertEqual((st["state"], st["trading"]), ("halted", False))
        self.assertEqual(self.c.banner["level"], "alarm")
        self.assertEqual(self.c.status()["kill"]["active"], True)
        r, _ = self.e.send(self.c, "day.start", PAPER)
        self.assertEqual(r["code"], "E_STATE")                       # halted
        exec_cfg.clear_kill(self.e.root)                              # только ./tb exec resume
        self.c.watch()
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "stopped")

    def test_kill_command_notification_and_flatten_nonce(self):
        self.e.confirmed(self.c, "day.start", PAPER)
        r, _ = self.e.send(self.c, "kill.set", {"flatten": False, "reason": "тест"})
        self.assertEqual(r["code"], "OK")
        self.assertIsNotNone(exec_cfg.kill_state(self.e.root))
        self.assertEqual(ctlstate.platform_state(self.e.root)["state"], "halted")
        exec_cfg.clear_kill(self.e.root)
        self.c.watch()
        # flatten без nonce и без «applied»: отказ, KILL не создан
        r, _ = self.e.send(self.c, "kill.set", {"flatten": True, "reason": "x"})
        self.assertEqual(r["code"], "E_NONCE")
        self.assertIsNone(exec_cfg.kill_state(self.e.root))
        # с подтверждением ctl
        r = self.e.confirmed(self.c, "kill.set", {"flatten": True, "reason": "x"})
        self.assertEqual(r["code"], "OK")
        self.assertTrue(exec_cfg.kill_state(self.e.root)["flatten"])

    def test_no_clear_kill_action_exists(self):
        from tb.control import protocol
        for a in protocol.ACTIONS:
            if a == "day.resume":          # возобновление торгового флага дня (не KILL): отдельный тест в test_lifecycle_ctl
                continue
            self.assertNotIn("resume", a)
            self.assertNotIn("clear", a)


class TestRisk(Base):
    def test_down_is_one_click_up_needs_everything(self):
        r, _ = self.e.send(self.c, "risk.set", {"level": 2})
        self.assertEqual((r["code"], r["data"]["level"]), ("OK", 2))
        ch = self.e.challenge(self.c, "risk.set", {"level": 4})
        self.assertEqual((ch["code"], len(ch["data"]["warnings"]) > 0, ch["data"]["phrases"]), ("OK", True, []))       # текстовых фраз нет
        self.e.advance(3)
        ids = [w["id"] for w in ch["data"]["warnings"]]
        r, _ = self.e.send(self.c, "risk.set", {"level": 4}, nonce=ch["data"]["nonce"])
        self.assertEqual(r["code"], "E_WARN")                                                                         # риски не приняты
        r, _ = self.e.send(self.c, "risk.set", {"level": 4, "accepted": ids})
        self.assertEqual(r["code"], "E_NONCE")                                                                        # без nonce повышение невозможно
        r, _ = self.e.send(self.c, "risk.set", {"level": 4, "accepted": ids}, nonce=ch["data"]["nonce"])
        self.assertEqual((r["code"], r["data"]["level"]), ("OK", 4), r)
        self.assertEqual(self.c.status()["risk"]["level"], 4)
        a = [x for x in self.e.audit() if x["action"] == "risk.set" and x["code"] == "OK"][-1]
        self.assertEqual(sorted(w["id"] for w in a["warnings_accepted"]), sorted(ids))

    def test_cooldown_between_raises_and_unknown_level(self):
        self.e.send(self.c, "risk.set", {"level": 2})
        r = self.e.confirmed(self.c, "risk.set", {"level": 3})
        self.assertEqual(r["code"], "OK", r)
        r = self.e.confirmed(self.c, "risk.set", {"level": 4})
        self.assertIn(r["code"], ("E_COOLDOWN", "E_STATE"))
        self.assertEqual(self.e.send(self.c, "risk.set", {"level": 9})[0]["code"], "E_ARGS")

    def test_status_lists_levels(self):
        s = self.c.status()
        self.assertEqual([l["n"] for l in s["risk"]["levels"]], [1, 2, 3, 4, 5])


class TestMisc(Base):
    def test_unavailable_and_disabled_actions(self):
        for a, args, code in (("platform.shutdown", {}, "E_DISABLED"),
                              ("carry.decide", {"plan": "PL-1", "decision": "close"}, "E_UNAVAILABLE"), ("notify.test", {}, "E_UNAVAILABLE"),
                              ("report.ack", {"day_id": "D-1"}, "E_UNAVAILABLE")):
            r, _ = self.e.send(self.c, a, args)
            self.assertEqual(r["code"], code, a)
        self.assertFalse(self.e.exists("runtime/platform/stop.req"))

    def test_intel_refresh_queues_request(self):
        r, _ = self.e.send(self.c, "intel.refresh", {"source": "all"})
        self.assertEqual(r["code"], "OK")
        self.assertEqual(self.e.r("runtime/intel/refresh.req")["source"], "all")

    def test_queue_of_200_commands(self):
        t = time.time()
        ids = []
        for i in range(200):
            c = self.e.cmd("day.stop", {"mode": "soft"})
            client.submit(c, self.e.root)
            ids.append(c["id"])
        self.assertEqual(self.c.poll_once(), 200)
        self.assertEqual(os.listdir(self.e.p("state", "control", "_inbox")), [])
        self.assertTrue(all(client.read_result(i, self.e.root) is not None for i in ids))
        self.assertTrue(audit.verify(self.e.root)["ok"])
        self.assertLess(time.time() - t, 30)

    def test_status_file_shape(self):
        self.c.write_status()
        s = client.read_status(self.e.root, NOW)
        self.assertFalse(s["stale"])
        for k in ("schema", "ctl", "platform", "checks", "team", "pending", "last", "banner", "risk"):
            self.assertIn(k, s)
        self.assertEqual([c["id"] for c in s["checks"] if c["sev"] == "red"], ["H%d" % i for i in range(1, 9)])
        self.assertEqual(s["platform"]["state"], "stopped")

    def test_no_secrets_in_any_state_file(self):
        self.start_live()
        self.e.send(self.c, "risk.set", {"level": 2})
        self.c.write_status()
        key = json.loads(_ctl.rd(auth.control_file()))
        secrets = [self.e.token, key["hmac_key"], key["token_hash"], key["salt"]]
        for d, _, fs in os.walk(self.e.root):
            for f in fs:
                p = os.path.join(d, f)
                try:
                    txt = _ctl.rd(p)
                except (OSError, UnicodeDecodeError):
                    continue
                for s in secrets:
                    self.assertNotIn(s, txt, p)

    def test_outbox_cleanup(self):
        self.e.send(self.c, "day.stop", {"mode": "soft"})
        d = self.e.p("state", "control", "_outbox")
        for n in os.listdir(d):
            os.utime(os.path.join(d, n), (NOW - 9 * 86400, NOW - 9 * 86400))
        c = self.e.ctl(clock=lambda: NOW)
        c.cleanup_outbox()
        self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
