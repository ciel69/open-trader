"""WP12: жёсткие гейты H1-H8 (каждый как единственная причина отказа) и мягкие предупреждения W1-W9. Без сети, во временном корне."""
import json
import os
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb.control import ctlstate, gates
from tb.core import exec_cfg, risklevels

NOW = _ctl.NOW
ARGS = {"mode": "live", "session": "intraday"}


class Base(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.good_live()

    def ev(self, args=ARGS, now=None):
        return gates.evaluate(self.e.root, args, NOW if now is None else now, ctlstate.load_cfg(self.e.root))

    def failing(self, **kw):
        return [g["id"] for g in self.ev(**kw)["gates"] if not g["ok"]]

    def warn_ids(self, **kw):
        return [w["id"] for w in self.ev(**kw)["warnings"]]


class TestHard(Base):
    def test_baseline_all_green_no_warnings(self):
        ev = self.ev()
        self.assertTrue(ev["ok"])
        self.assertEqual([g["id"] for g in ev["gates"]], ["H%d" % i for i in range(1, 9)])
        self.assertEqual(ev["warnings"], [])

    def test_h1_kill(self):
        exec_cfg.set_kill(True, "test", self.e.root)
        self.assertEqual(self.failing(), ["H1"])
        g = self.ev()["gates"][0]
        self.assertIn("flatten=True", g["msg"])

    def test_h2_position_without_stop(self):
        snap = self.e.r("runtime/exec_snapshot.json")
        snap["positions"] = [{"sym": "SOL", "side": "long", "size": 1, "sl": 0}, {"sym": "ETH", "sl": 1900}]
        self.e.w("runtime/exec_snapshot.json", snap)
        self.assertEqual(self.failing(), ["H2"])
        self.assertIn("SOL", self.ev()["gates"][1]["msg"])
        snap["positions"] = [{"sym": "ETH", "sl": 1900}]
        self.e.w("runtime/exec_snapshot.json", snap)
        self.assertEqual(self.failing(), [])

    def test_h2_stale_snapshot(self):
        snap = self.e.r("runtime/exec_snapshot.json")
        snap["ts_epoch"] = NOW - 120
        self.e.w("runtime/exec_snapshot.json", snap)
        self.assertEqual(self.failing(), ["H2"])

    def test_h3_each_cause(self):
        cases = {"приватный WS": {"ws": {"public": {"up": True}, "private": {"up": False}}}, "REST": {"rest": {"ok": False}},
                 "hold": {"hold_reasons": ["bad_data"]}, "hold2": {"hold_reasons": ["private_ws"]}, "сверка": {"reconcile_age_s": 90.0},
                 "сверка2": {"reconcile_age_s": None}, "heartbeat": {"ts_epoch": NOW - 12}}
        for name, over in cases.items():
            self.e.good_exec(**over) if "ts_epoch" not in over else self.e.good_exec(now=NOW - 12)
            self.assertEqual(self.failing(), ["H3"], name)

    def test_h3_exec_dead_or_missing(self):
        self.e.good_exec(now=NOW - 600)
        self.assertIn("H3", self.failing())                       # снимок мёртвого исполнителя тоже устарел (H2)
        os.remove(self.e.p("runtime", "exec_health.json"))
        self.assertEqual(self.failing(), ["H3"])

    def test_h4_causes(self):
        self.e.good_exec(hold_reasons=["equity_mismatch"])
        self.assertEqual(self.failing(), ["H4"])
        self.e.good_exec(diffs=2)
        self.assertEqual(self.failing(), ["H4"])
        self.e.good_exec()
        snap = self.e.r("runtime/exec_snapshot.json")
        snap["account"]["equity"] = 20.0                           # 33% от портфеля при допуске 10%
        self.e.w("runtime/exec_snapshot.json", snap)
        self.assertEqual(self.failing(), ["H4"])
        self.assertIn("33", self.ev()["gates"][3]["msg"])

    def test_h5_daily_loss_and_drawdown(self):
        day = _ctl.iso(NOW).split("T")[0]
        rows = [{"ev": "open", "id": "X", "mode": "live", "stype": "intraday"}, {"ev": "close", "id": "X", "ts_close": day + "T08:00:00Z", "pnl_usdt": -3.5}]
        with open(self.e.p("state", "trades.jsonl"), "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(self.failing(), ["H5"])
        self.assertIn("3.50", self.ev()["gates"][4]["msg"])
        os.remove(self.e.p("state", "trades.jsonl"))              # убыток вчерашний не считается
        rows[1]["ts_close"] = _ctl.iso(NOW - 86400)
        self.e.w("state/trades.jsonl", "".join(json.dumps(r) + "\n" for r in rows), raw=True)
        self.assertEqual(self.failing(), [])
        self.e.w("state/portfolio.json", {"venues": {"bybit": {"equity": 30.0, "free": 30.0}}, "hwm": 45.0})    # просадка 33% >= 20%
        self.assertEqual(self.failing(), ["H5"])

    def test_h6_brain(self):
        os.remove(self.e.p("runtime", "platform", "brain.hb.json"))
        self.assertEqual(self.failing(), ["H6"])
        self.e.w("runtime/platform/brain.hb.json", {"ts_epoch": NOW - 1000, "pid": 1})
        self.assertEqual(self.failing(), ["H6"])
        self.e.w("runtime/platform/brain.hb.json", {"ts_epoch": NOW, "pid": 1})
        ag = self.e.r("config/agents.json")
        ag["agents"]["api"]["enabled"] = False
        self.e.w("config/agents.json", ag)
        self.assertEqual(self.failing(), ["H6"])

    def test_h6_budget_exhausted(self):
        self.e.brain_fixture(NOW, budget_day_usd=0.01)
        rec = {"ts": _ctl.iso(NOW - 60), "t": NOW - 60, "provider": "deepseek", "model": "pro", "in": 100, "out": 10, "usd": 0.5, "ok": True}
        self.e.w("state/llm_calls.jsonl", json.dumps(rec) + "\n", raw=True)
        self.assertEqual(self.failing(), ["H6"])
        self.assertIn("бюджет", self.ev()["gates"][5]["msg"])

    def test_h7_causes(self):
        # 1. risk_profile
        good = _ctl.rd(self.e.p("config", "risk_profile.json"))
        self.e.w("config/risk_profile.json", "{bad", raw=True)
        self.assertIn("H7", self.failing())
        self.e.w("config/risk_profile.json", good, raw=True)
        self.assertEqual(self.failing(), [])
        # 2. exec_limits ослабляет защиту
        self.e.w("config/exec_limits.json", {"version": 1, "pyramid": {"enabled": True}})
        self.assertEqual(self.failing(), ["H7"])
        self.assertTrue(exec_cfg.warnings(self.e.root))
        self.e.w("config/exec_limits.json", {"version": 1})
        # 3. агент без live
        base = self.e.r("config/agents.json")
        for mut in (lambda a: a["limits"].update(modes=["paper"]), lambda a: a["limits"].update(allow_add=True), lambda a: a["limits"].update(max_size_mult=2.0),
                    lambda a: a["limits"].pop("max_risk_usdt"), lambda a: a["limits"].update(max_risk_pct=float("inf") if False else None),
                    lambda a: a.update(autonomy={"live": False})):
            ag = json.loads(json.dumps(base))
            mut(ag["agents"]["api"])
            self.e.w("config/agents.json", ag)
            self.assertEqual(self.failing(), ["H7"])
        self.e.w("config/agents.json", base)
        self.assertEqual(self.failing(), [])
        # 4. подпись уровня риска испорчена -> fallback
        rl = self.e.r("state/control/risk_level.json")
        rl["level"] = 5
        self.e.w("state/control/risk_level.json", rl)
        self.assertEqual(self.failing(), ["H7"])
        # 5. нет risk_levels.json
        self.e.w("state/control/risk_level.json", {}, raw=False)
        os.remove(self.e.p("config", "risk_levels.json"))
        self.assertIn("H7", self.failing())

    def test_h8_limits_and_cooldown(self):
        for i in range(3):
            ctlstate.note_live_start(self.e.root, NOW - 1000 * (i + 1))
        self.assertEqual(self.failing(), ["H8"])
        os.remove(self.e.p("state", "control", "live_starts.json"))
        ctlstate.note_live_stop(self.e.root, NOW - 120)           # остановка 2 мин назад, пауза 10 мин
        self.assertEqual(self.failing(), ["H8"])
        f = gates.collect(self.e.root, NOW + 700, ctlstate.load_cfg(self.e.root), ARGS)
        self.assertTrue(gates.h8(f)["ok"])                          # пауза прошла

    def test_h8_prev_day_starts_do_not_count(self):
        for i in range(3):
            ctlstate.note_live_start(self.e.root, NOW - 2 * 86400 - i)
        self.assertEqual(self.failing(), [])

    def test_deferred_when_exec_not_in_target_profile(self):
        self.e.good_exec(profile="paper")
        ev = self.ev()
        self.assertTrue(ev["ok"])
        self.assertTrue(all(g.get("deferred") for g in ev["gates"] if g["id"] in ("H2", "H3", "H4")))
        os.remove(self.e.p("runtime", "exec_health.json"))
        self.assertEqual(self.failing(), ["H3"])                  # исполнитель вообще не запущен: отказ

    def test_non_live_modes_only_check_kill(self):
        self.e.good_exec(profile="paper")
        for m in ("paper", "testnet"):
            ev = gates.evaluate(self.e.root, {"mode": m}, NOW, ctlstate.load_cfg(self.e.root))
            self.assertEqual([g["id"] for g in ev["gates"]], ["H1"])
            self.assertEqual(ev["warnings"], [])
        exec_cfg.set_kill(False, "t", self.e.root)
        self.assertFalse(gates.evaluate(self.e.root, {"mode": "paper"}, NOW, ctlstate.load_cfg(self.e.root))["ok"])
        self.assertEqual(gates.evaluate(self.e.root, {"mode": "observe"}, NOW, ctlstate.load_cfg(self.e.root))["gates"], [])

    def test_monitor_flags_violations_only(self):
        self.assertEqual(gates.monitor(self.e.root, NOW), [])
        exec_cfg.set_kill(False, "t", self.e.root)
        self.assertEqual([g["id"] for g in gates.monitor(self.e.root, NOW)], ["H1"])
        os.remove(self.e.p("state", "KILL"))
        self.e.good_exec(profile="paper")                          # исполнитель вышел из live: нарушение
        self.assertEqual([g["id"] for g in gates.monitor(self.e.root, NOW)], ["H3"])

    def test_numbers_in_every_reason(self):
        exec_cfg.set_kill(False, "t", self.e.root)
        for g in self.ev()["gates"]:
            self.assertIn("msg", g)
            self.assertTrue(g["msg"])


class TestSoft(Base):
    def test_w1_paper_history(self):
        self.e.w("state/trades.jsonl", "", raw=True)
        ids = self.warn_ids()
        self.assertIn("W1", ids)
        w = [w for w in self.ev()["warnings"] if w["id"] == "W1"][0]
        self.assertIn("0 из рекомендованных 30", w["msg"])
        self.assertEqual(w["numbers"]["need_days"], 7)

    def test_w2_live_ok(self):
        self.e.brain_fixture(NOW, live_ok=False)               # WP11-C2: метка live_ok теперь в config/brain.json
        self.assertEqual(self.warn_ids(), ["W2"])
        self.e.brain_fixture(NOW, live_ok=True, eval_age_days=90)
        self.assertEqual(self.warn_ids(), ["W2"])

    def test_w3_facts(self):
        f = self.e.r("runtime/intel/facts.json")
        self.e.w("runtime/intel/facts.json", dict(f, ts=int(NOW) - 3000))
        self.assertEqual(self.warn_ids(), ["W3"])
        self.e.w("runtime/intel/facts.json", dict(f, degraded=["bybit_ann"]))
        self.assertEqual(self.warn_ids(), ["W3"])
        self.e.w("runtime/intel/facts.json", dict(f, calendar={"next": [{"id": "x", "v": False}]}))
        self.assertEqual(self.warn_ids(), ["W3"])
        os.remove(self.e.p("runtime", "intel", "facts.json"))
        self.assertEqual(self.warn_ids(), ["W3"])

    def test_w4_recent_swap(self):
        self.e.w("state/brain/swaps.jsonl", json.dumps({"ts": NOW - 5400}) + "\n", raw=True)
        self.assertEqual(self.warn_ids(), ["W4"])
        self.assertIn("90 мин", [w for w in self.ev()["warnings"]][0]["msg"])
        self.e.w("state/brain/swaps.jsonl", json.dumps({"ts": NOW - 5 * 86400}) + "\n", raw=True)
        self.assertEqual(self.warn_ids(), [])

    def test_w5_risk_level_4(self):
        warns = risklevels.level_warnings(4, self.e.root)
        r = risklevels.set_level(4, "owner", self.e.root, accepted=[w["id"] for w in warns if w["required"]], phrase=risklevels.confirm_phrase(4, self.e.root),
                                 ack=risklevels.ACK_PHRASE, now=NOW + 4000)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.warn_ids(), ["W5"])

    def test_w6_degraded_services(self):
        self.e.w("runtime/platform/supervisor.lock", {"pid": os.getpid(), "started": NOW - 100, "profile": "trade"})
        self.e.w("runtime/platform/status.json", {"ts_epoch": NOW, "services": {"scan": {"state": "failed", "last_error": "x"}, "dispatch": {"state": "running", "hb_ok": False},
                                                                              "exec": {"state": "failed"}}})
        w = [w for w in self.ev()["warnings"] if w["id"] == "W6"]
        self.assertEqual(len(w), 1)
        self.assertIn("scan", w[0]["msg"])
        self.assertNotIn("exec", w[0]["msg"])                       # исполнитель — не «второстепенный»

    def test_w7_carry(self):
        self.e.w("config/lifecycle.json", {"carry": {"mode": "agent"}})
        self.assertEqual(self.warn_ids(), ["W7"])
        os.remove(self.e.p("config", "lifecycle.json"))
        snap = self.e.r("runtime/exec_snapshot.json")
        snap["positions"] = [{"sym": "SOL", "sl": 100.0}]
        self.e.w("runtime/exec_snapshot.json", snap)
        self.assertEqual(self.warn_ids(), ["W7"])

    def test_w8_blackout(self):
        f = self.e.r("runtime/intel/facts.json")
        self.e.w("runtime/intel/facts.json", dict(f, calendar={"next": [], "blackout": {"name": "FOMC", "in_min": 12, "v": True}}))
        self.assertEqual(self.warn_ids(), ["W8"])
        self.assertIn("12 мин", self.ev()["warnings"][0]["msg"])
        self.e.w("runtime/intel/facts.json", dict(f, calendar={"next": [], "blackout": {"name": "FOMC", "in_min": 300, "v": True}}))
        self.assertEqual(self.warn_ids(), [])

    def test_w9_session_stats(self):
        self.assertEqual(self.warn_ids(args=dict(ARGS, session="scalp")), ["W9"])
        w = self.ev(args=dict(ARGS, session="swing"))["warnings"][0]
        self.assertIn("0 из 10", w["msg"])
        self.assertEqual(self.warn_ids(), [])

    def test_warnings_do_not_block(self):
        self.e.w("state/trades.jsonl", "", raw=True)
        ev = self.ev()
        self.assertTrue(ev["ok"])
        self.assertGreaterEqual(len(ev["warnings"]), 2)           # W1 и W9
        for w in ev["warnings"]:
            self.assertTrue(w["msg"] and isinstance(w["numbers"], dict))


if __name__ == "__main__":
    unittest.main()
