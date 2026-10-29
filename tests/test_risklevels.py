import json
import os
import time

import _bootstrap  # noqa: F401
import _wp16 as W
from tb.core import bus, exec_cfg, risklevels as R


class ConfigTest(W.Base):
    def test_real_config_valid_and_level3_neutral(self):
        cfg, w = R.load_config(self.root)
        self.assertEqual(w, [])
        L3 = cfg["levels"]["3"]
        for k in ("session_budget_mult", "leverage_mult", "size_mult_cap"):
            self.assertEqual(L3[k], 1.0)
        self.assertEqual(L3["min_net_rr_add"], 0.0)
        self.assertEqual(cfg["default_level"], 3)
        self.assertEqual(cfg["confirmed_levels"], [3])
        self.assertEqual(R.unconfirmed_levels(cfg), [1, 2, 4, 5])
        base = self.prof()      # уровень 3 совпадает с базой risk_profile.json
        self.assertEqual(L3["risk_per_trade_frac"], base["risk_per_trade_frac"])
        self.assertEqual(L3["daily_loss_frac"], base["daily_loss_frac"])
        self.assertEqual(L3["max_open_positions"], base["max_open_positions"])

    def test_ceilings_values_from_spec(self):
        ce = self.cfg()["ceilings"]
        self.assertEqual((ce["risk_per_trade_frac"], ce["daily_loss_frac"], ce["max_open_positions"], ce["total_open_risk_frac"]), (0.05, 0.15, 3, 0.12))

    def test_no_file_means_legacy(self):
        os.remove(os.path.join(self.root, "config", "risk_levels.json"))
        cfg, w = R.load_config(self.root)
        self.assertIsNone(cfg)
        p = self.prof()
        self.assertIs(R.effective(p, self.root), p)
        self.assertEqual(R.current_level(self.root)["level"], 3)

    def test_broken_config_falls_back_to_builtin_level1(self):
        with open(os.path.join(self.root, "config", "risk_levels.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"]), (1, "default"))
        self.assertTrue(cur["warnings"])
        e = R.effective(self.prof(), self.root)
        self.assertEqual(e["risk_per_trade_frac"], 0.01)
        c = W.jload(os.path.join(W.ROOT, "config", "risk_levels.json"))
        c["levels"]["2"]["risk_per_trade_frac"] = "x"
        self.put_cfg(c)
        self.assertEqual(R.effective(self.prof(), self.root)["risk_level"]["level"], 1)

    def test_unconfirmed_level_warns(self):
        self.sign_level(4)
        cur = R.current_level(self.root)
        self.assertEqual(cur["level"], 4)
        self.assertFalse(cur["confirmed"])
        self.assertTrue(any("не подтверждены" in w for w in cur["warnings"]))
        self.sign_level(3)
        self.assertEqual(R.current_level(self.root)["warnings"], [])


class EffectiveTest(W.Base):
    def test_level3_is_identity(self):
        p = self.prof()
        e = R.effective(p, self.root, level=3)
        self.assertEqual({k: v for k, v in e.items() if k != "risk_level"}, p)
        self.assertTrue(e["risk_level"]["identity"])
        self.assertFalse(R.level_changes_behavior(e))

    def test_monotonic_levels(self):
        p = self.prof()
        rows = [R.effective(p, self.root, level=n) for n in range(1, 6)]
        for key in ("risk_per_trade_frac", "daily_loss_frac", "max_open_positions"):
            vals = [r[key] for r in rows]
            self.assertEqual(vals, sorted(vals), key)
        self.assertEqual(rows[0]["risk_per_trade_frac"], 0.01)
        self.assertEqual(rows[4]["risk_per_trade_frac"], 0.05)
        # плечо и RR уровней 1-2 строже базы, выше базы не бывает
        for r in rows:
            self.assertLessEqual(r["max_leverage"], p["max_leverage"])
            for n, c in r["session_types"].items():
                self.assertLessEqual(c["max_leverage"], p["session_types"][n]["max_leverage"])
                self.assertGreaterEqual(c["min_rr"], p["session_types"][n]["min_rr"])
        self.assertGreater(rows[0]["min_rr"], p["min_rr"])
        self.assertEqual(rows[3]["session_types"]["intraday"]["budget_frac"], 0.0375)

    def test_ceilings_cannot_be_exceeded(self):
        c = self.cfg()
        L = c["levels"]["5"]
        L.update({"risk_per_trade_frac": 0.5, "daily_loss_frac": 0.9, "max_open_positions": 9, "session_budget_mult": 50, "total_open_risk_frac": 0.9})
        self.put_cfg(c)
        e = R.effective(self.prof(), self.root, level=5)
        ce = c["ceilings"]
        self.assertEqual(e["risk_per_trade_frac"], ce["risk_per_trade_frac"])
        self.assertEqual(e["daily_loss_frac"], ce["daily_loss_frac"])
        self.assertEqual(e["max_open_positions"], ce["max_open_positions"])
        for n, v in ce["session_budget_frac"].items():
            self.assertLessEqual(e["session_types"][n]["budget_frac"], v + 1e-12)
        self.assertLessEqual(e["total_open_risk_cap_frac"], ce["total_open_risk_frac"])
        self.assertTrue(e["risk_level"]["clamped"])
        self.assertTrue(any("потолок" in w for w in e["risk_level"]["warnings"]))
        s = R.limits_summary(self.root, level=5)
        self.assertEqual(s["risk_per_trade_frac"], ce["risk_per_trade_frac"])
        self.assertLessEqual(s["total_open_risk_frac"], ce["total_open_risk_frac"])

    def test_base_above_ceiling_clamped_even_at_identity(self):
        p = dict(self.prof(), risk_per_trade_frac=0.2)
        e = R.effective(p, self.root, level=3)
        self.assertEqual(e["risk_per_trade_frac"], 0.05)

    def test_total_risk_cap_only_when_stricter(self):
        p = self.prof()
        self.assertNotIn("total_open_risk_cap_frac", R.effective(p, self.root, level=3))
        self.assertNotIn("total_open_risk_cap_frac", R.effective(p, self.root, level=1))
        self.assertEqual(R.effective(p, self.root, level=4)["total_open_risk_cap_frac"], 0.09)
        self.assertEqual(R.effective(p, self.root, level=5)["total_open_risk_cap_frac"], 0.12)

    def test_min_with_agent_limits_and_exec_limits(self):
        p = R.effective(self.prof(), self.root, level=5)
        q, cap = R.apply_agent_limits(p, {"max_risk_pct": 2.0, "max_risk_usdt": 0.7})
        self.assertEqual(q["risk_per_trade_frac"], 0.02)
        self.assertEqual(cap, 0.7)
        q2, cap2 = R.apply_agent_limits(p, {"max_risk_pct": 9.0})      # агент мягче уровня: действует уровень
        self.assertEqual(q2["risk_per_trade_frac"], 0.05)
        self.assertIsNone(cap2)
        s = R.limits_summary(self.root, agent_limits={"max_risk_pct": 2.0, "max_risk_usdt": 0.7, "max_size_mult": 0.4, "allow_add": True}, equity=30.0, level=5)
        self.assertEqual(s["risk_per_trade_frac"], 0.02)
        self.assertEqual(s["sources"]["risk_per_trade_frac"], "agent")
        self.assertEqual(s["risk_per_trade_usdt"], 0.6)
        self.assertEqual(s["size_mult_cap"], 0.4)
        s2 = R.limits_summary(self.root, agent_limits={"max_risk_usdt": 0.3}, equity=30.0, level=5)
        self.assertEqual(s2["risk_per_trade_usdt"], 0.3)
        # exec_limits сужают политику уровня
        ex = exec_cfg.load(self.root)
        ex["entry"]["no_confirm_max_size_mult"] = 0.3
        self.assertEqual(R.no_confirm_policy(self.root, ex, level=5)["size_mult_max"], 0.3)
        self.assertEqual(R.no_confirm_policy(self.root, level=5)["size_mult_max"], 0.5)     # exec 0.5 < уровень 5 (0.75)
        self.assertFalse(R.no_confirm_policy(self.root, level=1)["allowed"])

    def test_agent_warnings_show_gap(self):
        w = R.agent_warnings({"max_risk_pct": 6.0, "max_size_mult": 1.0, "allow_add": True}, self.root, "chat")
        self.assertEqual(len(w), 2)      # pct 6 > 3 и allow_add при уровне 3 без добора; size_mult 1.0 = потолок


class PolicyTest(W.Base):
    def test_carry_policy_by_level(self):
        self.assertEqual([R.carry_policy(self.root, level=n)["policy"] for n in range(1, 6)], ["never", "owner_confirm", "owner_confirm", "agent", "agent"])
        for n in range(1, 6):
            self.assertFalse(R.carry_policy(self.root, level=n)["scalp"])
        self.sign_level(1)
        self.assertEqual(R.carry_policy(self.root)["policy"], "never")

    def test_pyramid_needs_level_exec_and_agent(self):
        ex_on = dict(exec_cfg.load(self.root), pyramid=dict(exec_cfg.load(self.root)["pyramid"], enabled=True, max_adds=5))
        self.assertFalse(R.pyramid_policy(self.root, ex_on, None, 3)["allowed"])             # уровень 3 не разрешает
        p4 = R.pyramid_policy(self.root, ex_on, {"allow_add": True}, 4)
        self.assertTrue(p4["allowed"])
        self.assertEqual(p4["max_adds"], 1)                                                    # min(уровень 1, exec 5)
        self.assertFalse(R.pyramid_policy(self.root, ex_on, {"allow_add": False}, 4)["allowed"])
        self.assertFalse(R.pyramid_policy(self.root, None, {"allow_add": True}, 4)["allowed"])   # exec_limits.pyramid.enabled=false
        self.assertEqual(R.pyramid_policy(self.root, ex_on, {"allow_add": True}, 5)["max_adds"], 2)

    def test_setup_aggr(self):
        self.assertEqual(R.setup_allowed("level_reclaim", self.root, 1), (True, None))
        self.assertEqual(R.setup_allowed("sweep_reject", self.root, 1), (False, "E_SETUP_AGGR"))
        self.assertEqual(R.setup_allowed("sweep_reject", self.root, 3), (True, None))
        self.assertEqual(R.setup_allowed("momentum_chase", self.root, 3), (False, "E_SETUP_AGGR"))
        self.assertEqual(R.setup_allowed("momentum_chase", self.root, 4), (True, None))
        self.assertEqual(R.setup_allowed("whatever", self.root, 3), (True, None))            # неизвестный = mid
        self.assertEqual(R.setup_allowed("whatever", self.root, 2), (False, "E_SETUP_AGGR"))

    def test_exec_cfg_warnings_against_level(self):
        p = os.path.join(self.root, "config", "exec_limits.json")
        d = W.jload(p)
        self.assertEqual(exec_cfg.warnings(self.root), [])
        d["pyramid"]["enabled"] = True
        W.jdump(d, p)
        w = exec_cfg.warnings(self.root)
        self.assertTrue(any("уровень риска 3" in x for x in w), w)
        self.sign_level(4)
        self.assertFalse(any("уровень риска" in x for x in exec_cfg.warnings(self.root)))


class SignatureTest(W.Base):
    def test_absent_file_default_level(self):
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"], cur["warnings"]), (3, "default", []))

    def test_valid_signature(self):
        self.sign_level(2)
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"]), (2, "signed"))

    def test_tampered_level_falls_back_to_1(self):
        self.sign_level(2)
        p = R.level_file(self.root)
        d = W.jload(p)
        d["level"] = 5
        W.jdump(d, p)
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"]), (1, "fallback"))
        self.assertTrue(any("подпись" in w for w in cur["warnings"]))
        self.assertEqual(R.effective(self.prof(), self.root)["risk_per_trade_frac"], 0.01)

    def test_wrong_key_and_missing_key_and_garbage(self):
        self.sign_level(5)
        self.put_key("cd" * 32)
        self.assertEqual(R.current_level(self.root)["level"], 1)
        self.put_key("ab" * 32)
        self.assertEqual(R.current_level(self.root)["level"], 5)
        self.drop_key()
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"]), (1, "fallback"))
        self.assertTrue(any("ключ" in w for w in cur["warnings"]))
        self.put_key("ab" * 32)
        with open(R.level_file(self.root), "w") as f:
            f.write("garbage")
        self.assertEqual(R.current_level(self.root)["level"], 1)

    def test_short_or_nonhex_key_is_no_key(self):
        self.put_key("abcd")
        self.assertIsNone(R._read_key())
        self.put_key("zz" * 32)
        self.assertIsNone(R._read_key())

    def test_unknown_level_in_signed_file(self):
        self.sign_level(9)
        self.assertEqual(R.current_level(self.root)["level"], 1)

    def test_mac_is_hmac_sha256_over_canonical_json(self):
        import hashlib
        import hmac
        body = {"level": 2, "since": "s", "by": "owner", "history": []}
        exp = hmac.new(bytes.fromhex(W.FAKE_KEY_HEX), json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(R.sign_state(body, bytes.fromhex(W.FAKE_KEY_HEX)), exp)


class SetLevelTest(W.Base):
    def test_phrase_required(self):
        r = R.set_level(2, "owner", self.root, phrase="неверно")
        self.assertFalse(r["ok"])
        self.assertTrue(any("фраза" in e for e in r["errors"]))
        self.assertFalse(os.path.exists(R.level_file(self.root)))

    def test_lower_with_phrase_writes_audit_and_event(self):
        r = R.set_level(2, "owner", self.root, phrase=self.phrase(2), now=1800000000.0)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["prev"], r["level"]), (3, 2))
        cur = R.current_level(self.root)
        self.assertEqual((cur["level"], cur["source"], cur["by"]), (2, "signed", "owner"))
        self.assertEqual(cur["history"][-1]["from"], 3)
        with open(R.audit_file(self.root), encoding="utf-8") as f:
            aud = [json.loads(x) for x in f]
        self.assertEqual((aud[-1]["from"], aud[-1]["to"], aud[-1]["by"]), (3, 2, "owner"))
        ev = [e for e in bus.read(0, 50, root=self.root) if e["type"] == "risk_level_changed"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["d"]["from"], ev[0]["d"]["to"]), (3, 2))

    def test_raise_to_4_needs_each_warning_and_ack(self):
        ws = R.level_warnings(4, self.root)
        req = [w["id"] for w in ws if w["required"]]
        self.assertIn("W_CARRY", req)
        self.assertIn("W_TRADE_LOSS", req)
        self.assertIn("W_UNCONFIRMED", req)
        r = R.set_level(4, "owner", self.root, accepted=req[:-1], phrase=self.phrase(4), ack=R.ACK_PHRASE)
        self.assertFalse(r["ok"])
        self.assertTrue(any("не приняты" in e for e in r["errors"]))
        r = R.set_level(4, "owner", self.root, accepted=req, phrase=self.phrase(4), ack="нет")
        self.assertFalse(r["ok"])
        self.assertTrue(any("я принимаю риск" in e for e in r["errors"]))
        self.assertEqual(R.current_level(self.root)["level"], 3)
        r = self.raise_to(4)
        self.assertTrue(r["ok"], r)
        self.assertEqual(R.current_level(self.root)["level"], 4)
        self.assertTrue(r["warnings"])

    def test_raise_cooldown(self):
        t = 1800000000.0
        self.assertTrue(R.set_level(2, "owner", self.root, phrase=self.phrase(2), now=t)["ok"])
        self.assertTrue(self.raise_to(3, now=t + 10)["ok"])
        r = self.raise_to(4, now=t + 20)
        self.assertFalse(r["ok"])
        self.assertTrue(any("кулдаун" in e for e in r["errors"]), r["errors"])
        self.assertTrue(self.raise_to(4, now=t + 3700)["ok"])

    def test_only_owner_raises(self):
        for by in ("agent", "brain:api", ""):
            r = R.set_level(1, by, self.root, phrase=self.phrase(1))
            self.assertFalse(r["ok"])
        self.assertFalse(R.set_level(4, "auto", self.root, phrase=self.phrase(4))["ok"])
        self.assertFalse(os.path.exists(R.level_file(self.root)))

    def test_no_key_no_write(self):
        self.drop_key()
        r = R.set_level(2, "owner", self.root, phrase=self.phrase(2))
        self.assertFalse(r["ok"])
        self.assertTrue(any("init-key" in e for e in r["errors"]))
        self.assertFalse(os.path.exists(R.level_file(self.root)))

    def test_bad_level_values(self):
        for n in (0, 6, "3", None, True):
            self.assertFalse(R.set_level(n, "owner", self.root, phrase="x")["ok"])

    def test_set_does_not_touch_real_state(self):
        self.raise_to(4)    # Base.tearDown сверяет mtime реальных файлов
        self.assertTrue(R.level_file(self.root).startswith(self.root))


class AutoDowngradeTest(W.Base):
    def test_reasons_pure(self):
        lad = [[0.05, 1.0], [0.1, 0.5], [0.2, 0.25]]
        self.assertEqual(R.auto_downgrade_reasons(0.01, lad, 0.0, 3.0, today="D"), [])
        self.assertEqual(R.auto_downgrade_reasons(0.06, lad, 0.0, 3.0, today="D"), ["dd_0.05_D"])
        self.assertEqual(R.auto_downgrade_reasons(0.12, lad, 0.0, 3.0, today="D"), ["dd_0.1_D"])
        self.assertEqual(R.auto_downgrade_reasons(0.0, lad, -2.1, 3.0, 0.7, today="D"), ["daily_loss_D"])
        self.assertEqual(R.auto_downgrade_reasons(0.0, lad, -2.0, 3.0, 0.7, today="D"), [])
        self.assertEqual(len(R.auto_downgrade_reasons(0.06, lad, -2.5, 3.0, today="D")), 2)

    def test_one_step_per_new_reason_never_up_never_below_1(self):
        self.sign_level(4)
        r = R.auto_downgrade(self.root, ["dd_0.05_D"])
        self.assertTrue(r["changed"])
        self.assertEqual(R.current_level(self.root)["level"], 3)
        self.assertEqual(R.current_level(self.root)["history"][-1]["by"], "auto")
        self.assertFalse(R.auto_downgrade(self.root, ["dd_0.05_D"])["changed"])        # та же причина: повторно не понижаем
        self.assertEqual(R.current_level(self.root)["level"], 3)
        self.assertTrue(R.auto_downgrade(self.root, ["dd_0.1_D"])["changed"])          # глубже ступень = новая причина
        self.assertTrue(R.auto_downgrade(self.root, ["daily_loss_D"])["changed"])
        self.assertEqual(R.current_level(self.root)["level"], 1)
        self.assertFalse(R.auto_downgrade(self.root, ["dd_0.2_D"])["changed"])
        self.assertEqual(R.current_level(self.root)["level"], 1)
        ev = [e for e in bus.read(0, 50, root=self.root) if e["type"] == "risk_level_changed"]
        self.assertEqual([(e["d"]["from"], e["d"]["to"], e["d"]["by"]) for e in ev], [(4, 3, "auto"), (3, 2, "auto"), (2, 1, "auto")])

    def test_level_does_not_come_back_by_itself(self):
        self.sign_level(3)
        R.auto_downgrade(self.root, ["daily_loss_D"])
        self.assertEqual(R.current_level(self.root)["level"], 2)
        self.assertEqual(R.auto_downgrade(self.root, [])["changed"], False)
        self.assertEqual(R.current_level(self.root)["level"], 2)
        self.assertFalse(R.set_level(3, "auto", self.root)["ok"])

    def test_default_level_downgrades_from_unsigned_default(self):
        r = R.auto_downgrade(self.root, ["dd_0.05_D"])
        self.assertTrue(r["changed"])
        self.assertEqual(R.current_level(self.root)["level"], 2)

    def test_no_key_reports(self):
        self.drop_key()
        r = R.auto_downgrade(self.root, ["dd_0.05_D"])
        self.assertFalse(r["changed"])
        self.assertEqual(r["error"], "no_key")

    def test_check_state_from_portfolio_and_journal(self):
        from tb.core import journal, portfolio
        portfolio.save({"venues": {"bybit": {"equity": 25.0, "free": 20.0, "ts": "x"}}, "hwm": 30.0})
        today = time.strftime("%Y-%m-%d", time.gmtime())
        journal._append({"ev": "open", "id": "c", "sym": "DOGE", "side": "long", "mode": "live", "risk_usdt": 0.5, "notional": 40, "entry": 1, "ts_open": today + "T01:00:00Z"})
        journal._append({"ev": "close", "id": "c", "ts_close": today + "T02:00:00Z", "pnl_usdt": -2.0, "reason": "manual"})
        st = R.check_state(self.root)
        self.assertIn("dd_0.1_%s" % today, st["reasons"])
        self.assertIn("daily_loss_%s" % today, st["reasons"])     # -2.0 при лимите 2.5 (10% от 25) = 80% >= 70%
        self.assertAlmostEqual(st["daily_limit"], 2.5)
