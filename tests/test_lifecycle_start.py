"""WP14: старт дня S1-S9, preflight P1-P16, план дня, откат, возобновление. Без сети; мозг и исполнитель фейковые."""
import copy
import json
import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import _lc
from tb.control import ctlstate
from tb.core import exec_cfg, session
from tb.lifecycle import api, briefing, days, preflight, runtime, starter

NOW = _lc.NOW


def without_check(name, ok=True):
    """Подменить проверку P-номер на заведомо зелёную/красную (CHECKS хранит ссылки на функции)."""
    fake = lambda f: preflight.chk(name.upper(), "x", "R", ok, "подмена")  # noqa: E731
    fake.__name__ = name
    return mock.patch.object(preflight, "CHECKS", tuple(fake if fn.__name__ == name else fn for fn in preflight.CHECKS))


def pf(e, mode=None, **args):
    a = {"mode": mode or e.mode, "session": "intraday", "minutes": 360, "allow_amber": True}
    a.update(args)
    ctx = runtime.Ctx(e.root, e.t[0])
    v = preflight.run(ctx, a, "D-20261027-9")
    return dict((c["id"], c) for c in v["checks"]), v


class TestFullStart(unittest.TestCase):
    def test_paper_start_all_steps(self):
        e = _lc.LC(self)
        day = e.start()
        d = e.day()
        self.assertEqual(d["state"], "running")
        self.assertEqual([s["name"] for s in d["steps"]], list(starter.STEPS))
        self.assertTrue(all(s["ok"] is True for s in d["steps"]))
        self.assertTrue(all(s["phase"] == "start" for s in d["steps"]))
        self.assertEqual(d["steps"][4]["detail"]["source"], "agent")                 # S5: план от мозга
        self.assertEqual(d["dayplan"]["source"], "agent")
        self.assertEqual(e.r("state/control/trading.json")["enabled"], True)
        self.assertEqual(e.r("state/control/trading.json")["day_id"], day)
        self.assertIsNotNone(session.active())
        self.assertEqual(d["session"]["id"], session.active()["id"])
        self.assertTrue(e.exists("state/days/%s/dayplan.json" % day))
        self.assertTrue(e.exists("state/days/%s/start.md" % day))
        self.assertTrue(e.exists("state/days/%s/preflight.json" % day))
        lim = e.r("state/control/day_limits.json")
        self.assertEqual((lim["day_id"], lim["max_new_plans"]), (day, 3))
        self.assertLessEqual(lim["max_risk_usdt"], 0.5)

    def test_start_events_on_bus(self):
        e = _lc.LC(self)
        e.start()
        st = [x["d"]["state"] for x in e.bus_events("day_state")]
        self.assertEqual(st, ["starting", "running"])
        rep = e.bus_events("day_report")
        self.assertEqual(len(rep), 1)
        self.assertEqual((rep[0]["d"]["kind"], rep[0]["pri"]), ("start", "wake"))

    def test_steps_recorded_before_and_after(self):
        """Запись шага ДО выполнения (ok=None) видна на диске во время шага."""
        e = _lc.LC(self)
        seen = []
        orig = starter.FUNCS["S4"]

        def spy(ctx, d):
            with open(os.path.join(days.day_dir(ctx.root, d["day_id"]), "day.json"), encoding="utf-8") as fh:
                seen.append(json.load(fh))
            return orig(ctx, d)
        with mock.patch.dict(starter.FUNCS, {"S4": spy}):
            e.start()
        s4 = [s for s in seen[0]["steps"] if s["name"] == "S4"][0]
        self.assertIsNone(s4["ok"])

    def test_observe_has_no_session_and_trading_off(self):
        e = _lc.LC(self, mode="observe")
        e.start()
        self.assertIsNone(session.active())
        self.assertFalse(e.r("state/control/trading.json")["enabled"])
        self.assertEqual(e.day()["steps"][6]["detail"], {"skipped": "observe"})

    def test_second_start_rejected(self):
        e = _lc.LC(self)
        e.start()
        r = e.enter_day()
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))

    def test_day_ids_increment(self):
        e = _lc.LC(self)
        a = e.start()
        e.stop()
        e.ex.pump()
        b = e.start()
        self.assertEqual((a, b), ("D-20261027-1", "D-20261027-2"))

    def test_session_budget_from_profile_not_larger(self):
        e = _lc.LC(self)
        e.start()
        s = session.active()
        self.assertAlmostEqual(s["budget_usdt"], 0.03 * 30.0, places=3)           # budget_frac intraday x equity, не из плана дня

    def test_carry_in_from_previous_day(self):
        e = _lc.LC(self)
        prev = days.create(e.root, {"mode": "paper"}, NOW - 86400, "api")
        prev["state"] = "stopped"
        days.save(e.root, prev)
        e.w("state/days/%s/carry.json" % prev["day_id"], {"items": [{"plan": "PL-1-SOL-01", "sym": "SOL", "side": "long", "stop": 100.0, "risk_usdt": 0.3}]})
        e.ex.add_position("PL-1-SOL-01", stype="swing")
        e.ready()
        e.start()
        d = e.day()
        self.assertEqual(d["carry_in"][0]["plan"], "PL-1-SOL-01")
        self.assertTrue(d["carry_in"][0]["alive"])
        self.assertEqual(e.brain.calls[0]["payload"]["carried_in"][0]["plan"], "PL-1-SOL-01")      # аналитик видит перенесённое


class TestRollback(unittest.TestCase):
    def aborted(self, e, step, code):
        d = e.rd_day("D-20261027-1", "day.json")
        self.assertEqual(d["state"], "stopped")
        self.assertEqual(d["abort"]["step"], step)
        self.assertEqual(d["abort"]["code"], code)
        self.assertFalse(e.r("state/control/trading.json")["enabled"])
        self.assertEqual(e.bus_events("day_state")[-1]["d"]["state"], "stopped")
        self.assertIsNone(days.current(e.root))
        return d

    def test_s1_red_preflight_blocks(self):
        e = _lc.LC(self)
        exec_cfg.set_kill(False, "t", e.root)
        r = e.enter_day()
        self.assertEqual((r["ok"], r["code"]), (False, "E_GATE"))
        self.assertEqual(r["data"]["step"], "S1")
        d = self.aborted(e, "S1", "E_GATE")
        self.assertIsNone(session.active())
        self.assertEqual([s["name"] for s in d["steps"]], ["S1"])
        self.assertIn("P5", [c["id"] for c in d["steps"][0]["detail"]["blocking"]])

    def test_s3_equity_mismatch_testnet(self):
        e = _lc.LC(self, mode="testnet")
        e.w("state/portfolio.json", {"venues": {"bybit": {"equity": 50.0, "free": 50.0, "ts": _lc.iso(NOW)}}, "hwm": 50.0})
        with without_check("p7"):                                                 # обойти S1, проверить именно S3
            r = e.enter_day()
        self.assertEqual((r["ok"], r["code"], r["data"]["step"]), (False, "E_GATE", "S3"))
        self.aborted(e, "S3", "E_GATE")

    def test_s4_brief_failure(self):
        e = _lc.LC(self)
        with mock.patch("tb.brain.brief.build", side_effect=RuntimeError("boom")):
            r = e.enter_day()
        self.assertEqual((r["ok"], r["data"]["step"]), (False, "S4"))
        self.aborted(e, "S4", "E_INTERNAL")

    def test_s5_live_without_brain(self):
        e = _lc.LC(self, mode="live")
        e.brain.avail = False
        r = e.enter_day(allow_amber=False)
        self.assertEqual(r["ok"], False)
        self.assertIn(r["data"]["step"], ("S1", "S5"))                            # P11 (H6) красная в live: стоп уже на S1
        self.assertEqual(e.rd_day("D-20261027-1", "day.json")["state"], "stopped")

    def test_s5_live_brain_down_after_preflight(self):
        e = _lc.LC(self, mode="live")
        e.brain.avail = False
        with without_check("p11"):
            r = e.enter_day()
        self.assertEqual((r["ok"], r["code"], r["data"]["step"]), (False, "E_GATE", "S5"))
        self.aborted(e, "S5", "E_GATE")
        self.assertIsNone(session.active())

    def test_s7_active_session_conflict(self):
        e = _lc.LC(self)
        session._append({"ev": "start", "id": "scalp-x", "type": "scalp", "venue": "platform", "thesis": "t", "ts": _lc.iso(NOW), "start": NOW, "expires": NOW + 900,
                         "budget_usdt": 0.4, "equity_at_start": 30.0})
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        r = e.enter_day()
        self.assertEqual((r["ok"], r["code"], r["data"]["step"]), (False, "E_LIMITS", "S7"))
        self.aborted(e, "S7", "E_LIMITS")

    def test_s8_failure_stops_the_session(self):
        e = _lc.LC(self)
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        with mock.patch.object(days, "set_trading", side_effect=[OSError("disk"), None, None, None]):
            r = e.enter_day()
        self.assertEqual((r["ok"], r["data"]["step"]), (False, "S8"))
        self.assertIsNone(session.active())                                      # откат после S7: сессия остановлена
        sess, order = session.load_all()
        self.assertTrue(sess[order[0]]["stop_note"].startswith("aborted"))

    def test_s9_failure_is_not_fatal(self):
        e = _lc.LC(self)
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        with mock.patch("tb.lifecycle.report.start_report", side_effect=RuntimeError("x")):
            r = e.enter_day()
        self.assertTrue(r["ok"])
        self.assertEqual(e.day()["state"], "running")
        self.assertFalse(e.step(e.day(), "S9", "start")["ok"])


class TestPreflight(unittest.TestCase):
    def test_all_16_present(self):
        e = _lc.LC(self)
        c, v = pf(e)
        self.assertEqual(sorted(c, key=lambda x: int(x[1:])), ["P%d" % i for i in range(1, 17)])
        self.assertEqual(len(v["checks"]), 16)

    def test_baseline_only_p4_amber(self):
        e = _lc.LC(self)
        c, v = pf(e)
        self.assertEqual(sorted(x["id"] for x in v["checks"] if not x["ok"]), ["P4"])
        self.assertEqual(v["red"], [])
        self.assertTrue(v["ok"])

    def test_amber_blocks_without_allow_amber_outside_live(self):
        e = _lc.LC(self)
        c, v = pf(e, allow_amber=False)
        self.assertFalse(v["ok"])
        self.assertEqual([x["id"] for x in v["blocking"]], ["P4"])

    def test_amber_does_not_block_in_live(self):
        e = _lc.LC(self, mode="live")
        c, v = pf(e, allow_amber=False)
        self.assertEqual(v["red"], [])
        self.assertTrue(v["ok"], [x["id"] for x in v["blocking"]])

    def test_p1_supervisor_required(self):
        e = _lc.LC(self, cfg_over={"preflight": {"require_supervisor": True}})
        c, v = pf(e)
        self.assertFalse(c["P1"]["ok"])
        self.assertEqual(c["P1"]["sev"], "R")

    def test_p2_variants(self):
        e = _lc.LC(self)
        for name, over in (("diffs", {"diffs": 2}), ("hold", {"hold_reasons": ["reconciling"]}), ("reconcile_age", {"reconcile_age_s": 120.0}),
                           ("reconcile_none", {"reconcile_age_s": None}), ("profile", {"profile": "testnet"})):
            e.good_exec(profile=over.pop("profile", "paper"), now=e.t[0], **over)
            c, _ = pf(e)
            self.assertFalse(c["P2"]["ok"], name)
        e.good_exec(profile="paper", now=e.t[0] - 100)
        self.assertFalse(pf(e)[0]["P2"]["ok"])                                    # hb старше 10 с
        e.ready()
        self.assertTrue(pf(e)[0]["P2"]["ok"])

    def test_p3_feed(self):
        e = _lc.LC(self)
        e.good_exec(profile="paper", now=e.t[0], ws={"public": {"up": False, "age_s": 50}, "private": {"up": True, "age_s": 0}})
        self.assertFalse(pf(e)[0]["P3"]["ok"])

    def test_p5_kill(self):
        e = _lc.LC(self)
        exec_cfg.set_kill(False, "t", e.root)
        c, v = pf(e)
        self.assertFalse(c["P5"]["ok"])
        self.assertFalse(v["ok"])

    def test_p6_key(self):
        e = _lc.LC(self, mode="testnet")
        self.assertTrue(pf(e)[0]["P6"]["ok"])                                     # файл ключа testnet есть (поддельные значения в TB_KEYS_DIR теста)
        os.remove(os.path.join(e.keys_dir, "bybit_testnet.env"))
        c = pf(e)[0]["P6"]
        self.assertFalse(c["ok"])
        self.assertNotIn("fake", str(c))                                          # значения не попадают в вывод
        with open(os.path.join(e.keys_dir, "bybit_testnet.env"), "w") as f:
            f.write("BYBIT_API_KEY=x\n")                                          # нет секрета -> тоже нет
        self.assertFalse(pf(e)[0]["P6"]["ok"])
        e2 = _lc.LC(self)                                                         # paper: ключ не нужен
        self.assertTrue(pf(e2)[0]["P6"]["ok"])

    def test_p7_by_mode(self):
        e = _lc.LC(self)
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": None, "free": None}, "positions": [], "orders": []})
        c, _ = pf(e)
        self.assertEqual((c["P7"]["ok"], c["P7"]["sev"]), (False, "A"))           # paper: жёлтая
        e2 = _lc.LC(self, mode="testnet")
        e2.w("runtime/exec_snapshot.json", {"ts_epoch": e2.t[0], "account": {"equity": 5.0, "free": 5.0}, "positions": [], "orders": []})
        c, _ = pf(e2)
        self.assertEqual((c["P7"]["ok"], c["P7"]["sev"]), (False, "R"))           # testnet: красная, equity 5 vs портфель 30

    def test_p7_low_free_margin(self):
        e = _lc.LC(self)
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": 30.0, "free": 0.2}, "positions": [], "orders": []})
        self.assertFalse(pf(e)[0]["P7"]["ok"])

    def test_p8_orphan_position(self):
        e = _lc.LC(self)
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": 30.0, "free": 30.0},
                                           "positions": [{"sym": "BTC", "side": "long", "size": 0.01, "owner": None}], "orders": []})
        c, v = pf(e)
        self.assertFalse(c["P8"]["ok"])
        self.assertIn("BTC", c["P8"]["msg"])
        e.w("runtime/exec_snapshot.json", {"ts_epoch": e.t[0], "account": {"equity": 30.0, "free": 30.0},
                                           "positions": [{"sym": "BTC", "side": "long", "size": 0.01, "owner": "PL-1-BTC-01"}], "orders": []})
        self.assertTrue(pf(e)[0]["P8"]["ok"])                                     # перенесённая позиция имеет план-владельца

    def test_p9_configs(self):
        e = _lc.LC(self)
        os.remove(e.p("config", "risk_levels.json"))
        self.assertFalse(pf(e)[0]["P9"]["ok"])

    def test_p10_data_age(self):
        e = _lc.LC(self)
        os.utime(e.p("runtime", "market.json"), (e.t[0] - 1000, e.t[0] - 1000))
        c, _ = pf(e)
        self.assertEqual((c["P10"]["ok"], c["P10"]["sev"]), (False, "A"))

    def test_p10_levels_not_required_without_plans(self):
        e = _lc.LC(self)
        os.remove(e.p("runtime", "levels.json"))
        c, _ = pf(e)
        self.assertTrue(c["P10"]["ok"], c["P10"]["msg"])                           # планов нет: levels.json исполнитель не пишет, это не замечание
        e.good_exec(profile="paper", now=e.t[0], plans={"armed": 1, "managing": 0, "draft": 0, "entered": 0})
        c, _ = pf(e)
        self.assertFalse(c["P10"]["ok"])
        self.assertIn("levels.json", c["P10"]["msg"])

    def test_p10_context_stale_still_amber(self):
        e = _lc.LC(self)
        os.remove(e.p("runtime", "levels.json"))
        os.utime(e.p("runtime", "context.json"), (e.t[0] - 99999, e.t[0] - 99999))
        c, _ = pf(e)
        self.assertFalse(c["P10"]["ok"])
        self.assertIn("context.json", c["P10"]["msg"])

    def test_p11_brain(self):
        e = _lc.LC(self)
        e.brain.avail = False
        c, _ = pf(e)
        self.assertEqual((c["P11"]["ok"], c["P11"]["sev"]), (False, "A"))
        e2 = _lc.LC(self, mode="live")
        e2.brain.avail = False
        self.assertEqual(pf(e2)[0]["P11"]["sev"], "R")

    def test_p12_disk(self):
        e = _lc.LC(self)
        with mock.patch("tb.lifecycle.preflight.shutil.disk_usage", return_value=mock.Mock(free=50 * 1048576)):
            c, _ = pf(e)
        self.assertFalse(c["P12"]["ok"])

    def test_p13_daily_loss_live(self):
        e = _lc.LC(self, mode="live")
        rows = [{"ev": "open", "id": "L1", "mode": "live", "stype": "intraday"}, {"ev": "close", "id": "L1", "ts_close": _lc.iso(NOW - 600), "pnl_usdt": -4.0}]
        e.w("state/trades.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n", raw=True)
        c, v = pf(e)
        self.assertFalse(c["P13"]["ok"])
        self.assertEqual(c["P13"]["sev"], "R")
        c, _ = pf(_lc.LC(self))
        self.assertTrue(c["P13"]["ok"])

    def test_p14_blackout(self):
        e = _lc.LC(self)
        e.facts(calendar={"next": [], "blackout": {"name": "FOMC", "in_min": 10, "from": 1, "to": 2, "v": True}})
        c, _ = pf(e)
        self.assertFalse(c["P14"]["ok"])
        e.facts(calendar={"next": [], "blackout": {"name": "FOMC", "in_min": 200, "from": 1, "to": 2, "v": True}})
        self.assertTrue(pf(e)[0]["P14"]["ok"])

    def test_p15_facts(self):
        e = _lc.LC(self)
        e.facts(degraded=["bybit_ann"])
        self.assertFalse(pf(e)[0]["P15"]["ok"])
        os.remove(e.p("runtime", "intel", "facts.json"))
        self.assertFalse(pf(e)[0]["P15"]["ok"])
        self.assertIn("facts.json нет", pf(e)[0]["P15"]["msg"])

    def test_p15_unverified_sources_message(self):
        e = _lc.LC(self)
        e.facts(sources={"fng": {"ok": True, "age_s": 10, "fails": 0, "v": False}})
        c = pf(e)[0]["P15"]
        self.assertFalse(c["ok"])
        self.assertIn("источники не проверены владельцем, запусти ./tb intel verify", c["msg"])
        e.facts(sources={"fng": {"ok": True, "age_s": 10, "fails": 0, "v": True}})
        self.assertTrue(pf(e)[0]["P15"]["ok"])

    def test_p16_previous_day_unfinished(self):
        e = _lc.LC(self)
        d = days.create(e.root, {"mode": "paper"}, NOW - 3600, "api")
        d["state"] = "closing"
        days.save(e.root, d)
        c, v = pf(e)
        self.assertFalse(c["P16"]["ok"])
        self.assertFalse(v["ok"])
        r = e.enter_day()
        self.assertEqual((r["ok"], r["code"]), (False, "E_STATE"))                # и сам api не даёт стартовать поверх closing


class TestDayplan(unittest.TestCase):
    def env(self, **kw):
        e = _lc.LC(self, **kw)
        e.ready()
        d = {"day_id": "D-20261027-1", "session": {"type": "intraday", "minutes": 360}, "agent": "api", "mode": "paper"}
        ctx = runtime.Ctx(e.root, NOW)
        return e, briefing.make_env(ctx, d, {}), d, ctx

    def test_valid_plan_accepted(self):
        e, env, d, ctx = self.env()
        p, errs, warns = briefing.validate_dayplan(copy.deepcopy(_lc.DAYPLAN_OK), env)
        self.assertEqual((errs, warns), ([], []))
        self.assertEqual(p["schema"], "tb.dayplan/1")
        self.assertEqual(p["day_id"], "D-20261027-1")

    def test_invalid_variants_rejected(self):
        e, env, d, ctx = self.env()
        bad = {
            "enum_regime": {"regime": "bull"}, "enum_bias": {"bias": "up"}, "extra_key": {"hack": 1}, "no_why": {"why": ""},
            "watch_symbol": {"watch": [{"sym": "XYZ", "side": "long", "level": 1.0, "setup": "level_reclaim", "note": ""}]},
            "watch_setup": {"watch": [{"sym": "SOL", "side": "long", "level": 1.0, "setup": "yolo", "note": ""}]},
            "watch_against_bias": {"bias": "long", "watch": [{"sym": "SOL", "side": "short", "level": 1.0, "setup": "level_reclaim", "note": ""}]},
            "setups": {"allowed_setups": ["moon"]}, "avoid": {"avoid": ["bad sym"]}, "limits_extra": {"limits": {"max_new_plans": 1, "leverage": 50}},
            "limits_range": {"limits": {"max_new_plans": 99}}, "carry": {"carry": {"allow": True, "max_positions": 9}}, "risks": {"risks": ["Bad Code"]},
            "questions": {"questions": ["a", "b", "c", "d"]}, "type": None,
        }
        for name, over in bad.items():
            raw = copy.deepcopy(_lc.DAYPLAN_OK)
            if over is None:
                raw = "строка"
            else:
                raw.update(over)
            p, errs, _w = briefing.validate_dayplan(raw, env)
            self.assertIsNone(p, name)
            self.assertTrue(errs, name)

    def test_limits_clamped_to_caps(self):
        e, env, d, ctx = self.env()
        raw = copy.deepcopy(_lc.DAYPLAN_OK)
        raw["limits"] = {"max_new_plans": 3, "max_risk_usdt": 50.0, "symbols": ["SOL", "ETH", "DOGE"], "size_mult": 1.0}
        p, errs, warns = briefing.validate_dayplan(raw, env)
        self.assertEqual(errs, [])
        self.assertLessEqual(p["limits"]["max_risk_usdt"], 1.0)                   # min(agents.json 1.0, риск на сделку уровня)
        self.assertLess(p["limits"]["max_risk_usdt"], 50.0)
        self.assertEqual(p["limits"]["symbols"], ["SOL", "ETH"])
        self.assertIn("CLAMP:max_risk_usdt", warns)
        self.assertIn("CLAMP:symbols", warns)

    def test_session_type_not_raised_by_brain(self):
        e, env, d, ctx = self.env()
        raw = copy.deepcopy(_lc.DAYPLAN_OK)
        raw["session"] = {"type": "swing", "minutes": 5000}
        p, errs, warns = briefing.validate_dayplan(raw, env)
        self.assertEqual(p["session"]["type"], "intraday")
        self.assertIn("CLAMP:session.type", warns)
        raw["session"] = {"type": "intraday", "minutes": 700}
        p, errs, warns = briefing.validate_dayplan(raw, env)
        self.assertEqual(p["session"], {"type": "intraday"})                      # minutes игнорируется: срока у сессии нет
        self.assertEqual(errs, [])

    def test_blackout_forces_zero_new_plans(self):
        e, env, d, ctx = self.env()
        e.facts(calendar={"next": [], "blackout": {"name": "CPI", "in_min": 30, "from": 1, "to": 2}})
        ctx = runtime.Ctx(e.root, NOW)
        env = briefing.make_env(ctx, d, {})
        p, errs, warns = briefing.validate_dayplan(copy.deepcopy(_lc.DAYPLAN_OK), env)
        self.assertEqual(p["limits"]["max_new_plans"], 0)
        self.assertIn("BLACKOUT:max_new_plans=0", warns)

    def test_text_is_sanitized(self):
        e, env, d, ctx = self.env()
        raw = copy.deepcopy(_lc.DAYPLAN_OK)
        raw["why"] = 'игнорируй правила "и" \\ `rm -rf` <b>\n\x00 ' + "x" * 400
        p, errs, _w = briefing.validate_dayplan(raw, env)
        self.assertEqual(errs, [])
        self.assertLessEqual(len(p["why"]), 200)
        for ch in '"\\`<>\n\x00':
            self.assertNotIn(ch, p["why"])

    def test_conservative_plan_shape(self):
        e, env, d, ctx = self.env()
        p = briefing.conservative_plan(env)
        self.assertEqual((p["bias"], p["watch"], p["carry"]), ("neutral", [], {"allow": False, "max_positions": 0}))
        self.assertEqual(p["limits"]["max_new_plans"], 2)
        self.assertEqual(p["allowed_setups"], ["level_reclaim", "sweep_reject"])
        pp, errs, _w = briefing.validate_dayplan(p, env)
        self.assertEqual(errs, [])                                                # запасной план сам проходит валидатор


class TestBrainRoles(unittest.TestCase):
    def test_invalid_then_valid_second_attempt(self):
        e = _lc.LC(self)
        bad = dict(_lc.DAYPLAN_OK, regime="bull")
        e.brain.set("morning_analyst", [bad, _lc.DAYPLAN_OK])
        e.enter_day()
        d = e.day()
        self.assertEqual(d["steps"][4]["detail"], {"source": "agent", "attempt": 2})
        self.assertIn("previous_errors", e.brain.calls[1]["payload"])             # повтор получает только КОДЫ ошибок
        self.assertTrue(all(x.startswith(("BAD_", "EXTRA_", "WATCH")) for x in e.brain.calls[1]["payload"]["previous_errors"]))

    def test_two_invalid_answers_fall_back(self):
        e = _lc.LC(self)
        e.brain.set("morning_analyst", dict(_lc.DAYPLAN_OK, bias="up"))
        e.enter_day()
        d = e.day()
        self.assertEqual(d["dayplan"]["source"], "fallback")
        self.assertEqual(d["state"], "running")
        self.assertEqual(len(e.brain.calls), 2)
        self.assertEqual(e.rd_day(d["day_id"], "dayplan.json")["meta"]["source"], "fallback")

    def test_brain_error_and_timeout_fall_back(self):
        for resp in (("err", "PROVIDER_DOWN"), "hang"):
            e = _lc.LC(self)
            e.brain.set("morning_analyst", resp)
            t0 = e.t[0]
            e.enter_day()
            self.assertEqual(e.day()["dayplan"]["source"], "fallback", resp)
            if resp == "hang":
                self.assertGreaterEqual(e.t[0] - t0, 150)                          # ждали таймаут шага на фейковых часах
                self.assertTrue(e.brain.cancelled)

    def test_brain_unavailable_non_live_continues(self):
        e = _lc.LC(self)
        e.brain.avail = False
        e.enter_day()
        d = e.day()
        self.assertEqual(d["state"], "running")
        self.assertEqual(e.brain.calls, [])
        self.assertEqual(d["steps"][4]["detail"]["fallback"], "brain_down")

    def test_payload_has_no_free_text_from_third_parties(self):
        e = _lc.LC(self)
        e.start()
        pl = e.brain.calls[0]["payload"]
        self.assertEqual(pl["kind"], "morning")
        self.assertEqual(set(pl) - {"schema", "kind", "day_id", "mode", "brief", "facts", "owner_choice", "caps", "symbols", "carried_in"}, set())

    def test_facts_refresh_waits_for_intel_service(self):
        e = _lc.LC(self)
        e.w("runtime/platform/intel.hb.json", {"ts_epoch": e.t[0], "pid": 5, "service": "intel"})
        new_ts = [0]
        orig = runtime._RT["sleep"]

        def sleep(s):
            orig(s)
            if e.exists("runtime/intel/refresh.req") and not new_ts[0]:
                new_ts[0] = 1
                e.facts(e.t[0] + 200)
        runtime.configure(sleep=sleep)
        e.start()
        self.assertEqual(e.day()["facts"]["status"], "fresh")
        self.assertEqual(e.r("runtime/intel/refresh.req")["source"], "all")

    def test_long_waits_keep_the_heartbeat_fresh(self):
        """S2 (ожидание intel 60 с) и S5 (ожидание мозга до 150 с) обновляют пульс дня: возобновление не должно запускать второй старт."""
        e = _lc.LC(self)
        e.w("runtime/platform/intel.hb.json", {"ts_epoch": e.t[0], "pid": 5, "service": "intel"})
        e.brain.set("morning_analyst", "hang")
        beats = []
        orig = days.touch
        with mock.patch.object(days, "touch", side_effect=lambda root, d, now: (beats.append(now), orig(root, d, now))):
            e.enter_day()
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        self.assertGreater(len(beats), 30)
        self.assertLess(max(gaps), 30.0, max(gaps))                              # меньше runner_stale_s=45

    def test_facts_refresh_timeout_is_amber_only(self):
        e = _lc.LC(self)
        e.w("runtime/platform/intel.hb.json", {"ts_epoch": e.t[0], "pid": 5, "service": "intel"})
        e.start()
        d = e.day()
        self.assertEqual(d["facts"]["status"], "refresh_timeout")
        self.assertTrue(d["steps"][1]["ok"])
        self.assertTrue(d["steps"][1]["detail"]["amber"])


class TestResume(unittest.TestCase):
    def crashed_start(self, e, upto):
        """Эмуляция падения ctl: выполнены шаги до `upto`, пульс старый."""
        a = {"mode": "paper", "session": "intraday", "minutes": 360, "allow_amber": True}
        d = days.create(e.root, a, e.t[0], "api")
        ctx = runtime.Ctx(e.root, e.t[0])
        for name in starter.STEPS[:upto]:
            days.step_begin(e.root, d, name, "start", e.t[0])
            st, det, code = starter.FUNCS[name](ctx, d)
            days.step_end(e.root, d, name, "start", st == "ok", e.t[0], det, code)
        d["runner"] = {"pid": 1, "hb": e.t[0] - 1000}
        days.save(e.root, d)
        return d["day_id"]

    def test_resume_after_crash_at_each_step(self):
        for upto in range(0, 9):
            e = _lc.LC(self)
            e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
            day = self.crashed_start(e, upto)
            e.advance_t(100)
            e.ready()
            r = api.tick(e.root, e.t[0])
            d = e.rd_day(day, "day.json")
            self.assertEqual(d["state"], "running", "upto=%d" % upto)
            self.assertEqual([s["name"] for s in d["steps"] if s["ok"]], list(starter.STEPS), "upto=%d" % upto)
            self.assertEqual(r["state"], "running")
            self.assertEqual(len(session.load_all()[1]), 1, "сессия не должна задваиваться (upto=%d)" % upto)

    def test_completed_steps_not_repeated(self):
        e = _lc.LC(self)
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        self.crashed_start(e, 6)                                                  # S1-S6 готовы
        n0 = len(e.brain.calls)
        e.advance_t(100)
        e.ready()
        api.tick(e.root, e.t[0])
        self.assertEqual(len(e.brain.calls), n0)                                  # S5 повторно не вызывался
        self.assertEqual(e.day()["state"], "running")

    def test_fresh_runner_is_left_alone(self):
        e = _lc.LC(self)
        a = {"mode": "paper", "session": "intraday", "minutes": 360}
        d = days.create(e.root, a, e.t[0], "api")
        r = api.tick(e.root, e.t[0] + 5)
        self.assertEqual(r["state"], "starting")
        self.assertEqual(e.rd_day(d["day_id"], "day.json")["steps"], [])

    def test_stuck_start_older_than_10_minutes_aborts(self):
        e = _lc.LC(self)
        day = self.crashed_start(e, 2)
        e.advance_t(700)
        r = api.tick(e.root, e.t[0])
        d = e.rd_day(day, "day.json")
        self.assertEqual(d["state"], "stopped")
        self.assertEqual(d["abort"]["step"], "resume")
        self.assertIn("aborted_start", d["abort"]["detail"]["why"])
        self.assertFalse(e.r("state/control/trading.json")["enabled"])


if __name__ == "__main__":
    unittest.main()


class TestDaySessionStart(unittest.TestCase):
    """Старт дня с session=day (дашборд): сессия типа day, dayplan.session.type становится потолком горизонта (day_limits.max_horizon)."""

    def test_day_session_and_max_horizon(self):
        e = _lc.LC(self)
        day = e.start(session_type="day", minutes=360)
        s = session.active()
        self.assertEqual(s["type"], "day")
        self.assertEqual(e.day()["session"]["type"], "day")
        lim = e.r("state/control/day_limits.json")
        self.assertEqual(lim["max_horizon"], "intraday")                                   # DAYPLAN_OK: session.type intraday = потолок горизонта
        p = e.rd_day(day, "dayplan.json")
        self.assertEqual(p["session"], {"type": "intraday"})

    def test_day_plan_horizon_any_and_no_minutes(self):
        e = _lc.LC(self)
        dp = dict(_lc.DAYPLAN_OK, session={"type": "position", "minutes": 43200})
        e.brain.set("morning_analyst", dp)
        r = e.enter_day(session_type="day", minutes=120)
        self.assertTrue(r["ok"], r)
        p = e.rd_day(r["data"]["day_id"], "dayplan.json")
        self.assertEqual(p["session"], {"type": "position"})                              # горизонт любой, минут у сессии нет
        self.assertEqual(session.active()["type"], "day")
        self.assertNotIn("expires", session.active())
        self.assertEqual(e.r("state/control/day_limits.json")["max_horizon"], "position")

    def test_legacy_single_horizon_start_unchanged(self):
        e = _lc.LC(self)
        e.start(session_type="intraday", minutes=360)
        self.assertEqual(session.active()["type"], "intraday")
        self.assertIsNone(e.r("state/control/day_limits.json")["max_horizon"])
