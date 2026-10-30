"""Макро-блэкаут: paper.blackout_mode=warn (предупреждение только в paper), live/testnet всегда запрет.

Покрытие: политика и слой config/local, Gate исполнителя (тег, потолок размера, журнал решений), preflight P14, план дня, валидатор мозга,
режим в досье, срезы статистики `--by blackout`, согласованность констант и текст промптов.
"""
import copy
import json
import os
import re
import tempfile
import time
import types
import unittest

import _bootstrap  # noqa: F401
import _lc
from fakes.harness import Env, plan_long
from tb import paths
from tb.brain import brief, validate
from tb.core import blackout, exec_cfg, expstats, journal
from tb.lifecycle import briefing, runtime

NOW = _lc.NOW


def wjson(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbbo_")

    def cfg(self, **paper):
        wjson(paths.exec_limits_file(root=self.root), {"paper": paper})

    def test_shipped_default_is_block(self):
        self.assertEqual(exec_cfg.DEFAULTS["paper"]["blackout_mode"], "block")
        with open(os.path.join(_bootstrap.ROOT, "config", "exec_limits.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["paper"]["blackout_mode"], "block")
        self.assertEqual(blackout.policy(self.root, "paper")["mode"], "block")

    def test_warn_only_in_paper(self):
        self.cfg(blackout_mode="warn")
        self.assertEqual(blackout.policy(self.root, "paper")["mode"], "warn")
        for mode in ("live", "testnet", "mainnet", "trade", "observe", None, "", "PAPER", 1):
            self.assertEqual(blackout.policy(self.root, mode)["mode"], "block", mode)
            self.assertEqual(blackout.policy(self.root, mode)["configured"], "warn")

    def test_size_cap_never_above_half(self):
        self.cfg(blackout_mode="warn", blackout_size_mult=0.3)
        self.assertEqual(blackout.policy(self.root, "paper")["size_mult"], 0.3)
        self.cfg(blackout_mode="warn", blackout_size_mult=0.9)
        self.assertEqual(blackout.policy(self.root, "paper")["size_mult"], 0.5)
        self.assertEqual(blackout.cap_size(1.0, blackout.policy(self.root, "paper")), 0.5)

    def test_bad_value_rejected_and_policy_falls_to_block(self):
        self.cfg(blackout_mode="allow")
        with self.assertRaises(ValueError):
            exec_cfg.load(self.root)
        self.assertEqual(blackout.policy(self.root, "paper")["mode"], "block")
        self.cfg(blackout_size_mult=0)
        with self.assertRaises(ValueError):
            exec_cfg.load(self.root)

    def test_local_layer(self):
        wjson(paths.exec_limits_file(root=self.root), {"paper": {"blackout_mode": "block", "max_active": 7}})
        wjson(os.path.join(self.root, "config", "local", "exec_limits.json"), {"paper": {"blackout_mode": "warn"}})
        c = exec_cfg.load(self.root)
        self.assertEqual((c["paper"]["blackout_mode"], c["paper"]["max_active"]), ("warn", 7))
        self.assertEqual(blackout.policy(self.root, "live")["mode"], "block")
        os.remove(paths.exec_limits_file(root=self.root))                      # нет основного файла: локальный слой работает поверх умолчаний
        self.assertEqual(exec_cfg.load(self.root)["paper"]["blackout_mode"], "warn")

    def test_state_reads_windows(self):
        self.assertIsNone(blackout.state(self.root, NOW))
        wjson(paths.runtime("intel", "blackouts.json", root=self.root), {"windows": [
            {"from": iso(NOW - 600), "to": iso(NOW + 3000), "name": "FOMC"}, {"from": iso(NOW + 9 * 3600), "to": iso(NOW + 10 * 3600), "name": "CPI"}, "junk"]})
        st = blackout.state(self.root, NOW)
        self.assertEqual((st["name"], st["in_min"] <= 0), ("FOMC", True))
        self.assertIsNone(blackout.state(self.root, NOW + 4000))               # окно кончилось, следующее далеко (> 60 мин)
        self.assertEqual(blackout.state(self.root, NOW + 4000, lead_min=600)["name"], "CPI")
        with open(paths.runtime("intel", "blackouts.json", root=self.root), "w") as f:
            f.write("{bad")
        self.assertIsNone(blackout.state(self.root, NOW))


def put_window(e):
    t = e.clock.time()
    wjson(paths.runtime("intel", "blackouts.json", root=e.root), {"windows": [{"from": iso(t - 600), "to": iso(t + 3000), "name": "FOMC", "src": "calendar", "v": True}]})


def run_entry(env, warn=True, window=True, plan=None):
    """Env с бирж-профилем env; -> (Env, plan_id, qty)."""
    e = Env(env=env, with_session=(env != "paper"), cfg_over={"paper": {"blackout_mode": "warn" if warn else "block"}})
    if window:
        put_window(e)
    pl = plan or plan_long(mode="paper" if env == "paper" else "live")
    pid = e.enter(pl)
    return e, pid, e.port.pos["SOL"]["size"] if "SOL" in e.port.pos else 0.0


class TestGate(unittest.TestCase):
    def test_paper_warn_passes_with_tag_cap_and_decision(self):
        base, _, qfull = run_entry("paper", warn=True, window=False)
        base.close()
        e, pid, q = run_entry("paper", warn=True, window=True)
        try:
            self.assertGreater(q, 0)
            self.assertLess(q, qfull)                                   # size_mult 1.0 урезан до 0.5
            self.assertAlmostEqual(q, round(int(qfull * 0.5 / 0.1 + 1e-9) * 0.1, 10), places=6)
            self.assertEqual(e.rt(pid)["entry"]["blackout"]["name"], "FOMC")
            tr = e.trades()
            self.assertEqual(len(tr), 1)
            self.assertIn("blackout", tr[0]["tags"])
            dec = e.decisions("event", "blackout_warn")
            self.assertEqual(len(dec), 1)
            self.assertIn("W_BLACKOUT", dec[0]["why"])
            self.assertEqual(dec[0]["plan"], pid)
        finally:
            e.close()

    def test_paper_warn_outside_window_is_normal(self):
        e, pid, q = run_entry("paper", warn=True, window=False)
        try:
            self.assertNotIn("blackout", e.rt(pid)["entry"])
            self.assertNotIn("blackout", e.trades()[0]["tags"])
            self.assertEqual(e.decisions("event", "blackout_warn"), [])
        finally:
            e.close()

    def test_paper_block_denies_in_window(self):
        """paper + block: вход в окне отклонён Gate (E_BLACKOUT), как в live (жёсткий отказ: tests/test_blackout_gate.py)."""
        e, pid, q = run_entry("paper", warn=False, window=True)
        try:
            self.assertEqual(q, 0.0)
            self.assertEqual(e.trades(), [])
            self.assertEqual(e.decisions("event", "blackout_warn"), [])
            self.assertTrue([d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]])
        finally:
            e.close()

    def test_testnet_and_live_ignore_warn(self):
        """warn не действует вне paper: вместо урезанного входа жёсткий отказ E_BLACKOUT, предупреждения и тега нет."""
        for env in ("testnet", "mainnet"):
            e, pid, q = run_entry(env, warn=True, window=True)
            try:
                self.assertEqual(q, 0.0, env)
                self.assertEqual(e.trades(), [], env)
                self.assertEqual(e.decisions("event", "blackout_warn"), [], env)
                self.assertIsNone(e.ex.gate.blackout_warn(e.clock.time()), env)
                self.assertTrue([d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]], env)
            finally:
                e.close()
            base, _, qfull = run_entry(env, warn=True, window=False)         # вне окна вход обычный, полного размера
            try:
                self.assertGreater(qfull, 0, env)
            finally:
                base.close()

    def test_observe_never_warns(self):
        e = Env(env="paper", with_session=False, observe=True, cfg_over={"paper": {"blackout_mode": "warn"}})
        try:
            put_window(e)
            self.assertIsNone(e.ex.gate.blackout_warn(e.clock.time()))
        finally:
            e.close()


class TestLifecycle(unittest.TestCase):
    FACTS = {"next": [], "blackout": {"name": "FOMC", "in_min": 10, "from": 1, "to": 2, "v": True}}

    def pf(self, e):
        import test_lifecycle_start as T
        return T.pf(e)[0]["P14"]

    def test_p14_yellow_in_paper_warn_and_live_unchanged(self):
        e = _lc.LC(self)
        e.w("config/exec_limits.json", {"paper": {"blackout_mode": "warn"}})
        e.facts(calendar=self.FACTS)
        c = self.pf(e)
        self.assertEqual((c["sev"], c["ok"]), ("A", False))              # жёлтая, не красная
        self.assertIn("paper: блэкаут = предупреждение", c["msg"])
        e2 = _lc.LC(self)
        e2.facts(calendar=self.FACTS)
        c2 = self.pf(e2)                                                  # block в paper: как раньше
        self.assertEqual((c2["sev"], c2["ok"]), ("A", False))
        self.assertNotIn("предупреждение", c2["msg"])
        e3 = _lc.LC(self, mode="live")
        e3.w("config/exec_limits.json", {"paper": {"blackout_mode": "warn"}})
        e3.facts(calendar=self.FACTS)
        c3 = self.pf(e3)
        self.assertEqual((c3["sev"], c3["ok"]), ("A", False))
        self.assertNotIn("предупреждение", c3["msg"])

    def env(self, mode, warn):
        e = _lc.LC(self)
        e.ready()
        if warn:
            e.w("config/exec_limits.json", {"paper": {"blackout_mode": "warn"}})
        e.facts(calendar=self.FACTS)
        d = {"day_id": "D-20261027-1", "session": {"type": "intraday", "minutes": 360}, "agent": "api", "mode": mode}
        return briefing.make_env(runtime.Ctx(e.root, NOW), d, {})

    def test_dayplan_warn_keeps_plans_with_cap_and_risk_code(self):
        env = self.env("paper", True)
        self.assertTrue(env["blackout_soon"] and env["blackout_warn"])
        p, errs, warns = briefing.validate_dayplan(copy.deepcopy(_lc.DAYPLAN_OK), env)
        self.assertEqual(errs, [])
        self.assertEqual(p["limits"]["max_new_plans"], 3)
        self.assertLessEqual(p["limits"]["size_mult"], 0.5)
        self.assertIn("blackout_warn", p["risks"])
        self.assertIn("BLACKOUT:warn_paper", warns)
        self.assertNotIn("BLACKOUT:max_new_plans=0", warns)
        self.assertEqual(briefing.conservative_plan(env)["limits"]["max_new_plans"], 0)      # консервативный план в окне остаётся нулевым

    def test_dayplan_block_and_live_zero_plans(self):
        for mode, warn in (("paper", False), ("live", True), ("testnet", True)):
            env = self.env(mode, warn)
            self.assertFalse(env["blackout_warn"], mode)
            p, errs, warns = briefing.validate_dayplan(copy.deepcopy(_lc.DAYPLAN_OK), env)
            self.assertEqual(p["limits"]["max_new_plans"], 0, mode)
            self.assertIn("BLACKOUT:max_new_plans=0", warns, mode)


def dayplan(n=2, size=0.5, risks=()):
    return {"schema": "tb.dayplan/1", "regime": "range", "bias": "neutral", "session": {"type": "intraday", "minutes": 360},
            "watch": [{"sym": "SOL", "side": "long", "level": 144.0, "setup": "level_reclaim", "note": "5м над уровнем"}], "allowed_setups": ["level_reclaim"], "avoid": [],
            "limits": {"max_new_plans": n, "max_risk_usdt": 0.5, "symbols": ["SOL"], "size_mult": size}, "carry": {"allow": False, "max_positions": 0}, "carried_review": [],
            "risks": list(risks), "questions": [], "why": "тихо | диапазон | осторожно"}


CTX = {"symbols": ["BTC", "ETH", "SOL"], "prices": {"SOL": 145.2}, "data_ok": True, "limits": {"max_new_plans": 3, "max_risk_usdt": 0.9, "size_mult": 1.0},
       "session_type": "intraday", "carry_policy": "owner_confirm", "blackout_min": 20, "max_level_distance_pct": 12.0}


class TestValidator(unittest.TestCase):
    def chk(self, o, **over):
        return validate.semantic("dayplan/1", o, dict(CTX, **over))

    def test_block_requires_zero_plans(self):
        self.assertIn("BLACKOUT_PLANS", self.chk(dayplan(risks=["blackout_warn"])))
        self.assertIn("BLACKOUT_PLANS", self.chk(dayplan(risks=["blackout_warn"]), blackout_warn=False))

    def test_warn_allows_with_size_and_risk_code(self):
        self.assertEqual(self.chk(dayplan(risks=["blackout_warn"]), blackout_warn=True), [])
        self.assertIn("BLACKOUT_SIZE", self.chk(dayplan(size=0.8, risks=["blackout_warn"]), blackout_warn=True))
        self.assertIn("BLACKOUT_RISK_CODE", self.chk(dayplan(), blackout_warn=True))

    def test_pm_context_reads_mode_from_brief(self):
        b = {"plans": [], "positions": [], "market": {"blackout": {"event": "fomc", "in_min": 20, "mode": "warn"}}}
        self.assertTrue(validate.pm_context(b, "PL-1028-SOL-01")["blackout_warn"])
        b["market"]["blackout"].pop("mode")
        self.assertFalse(validate.pm_context(b, "PL-1028-SOL-01")["blackout_warn"])

    def test_release_plan_in_window(self):
        pm = {"schema": "tb.pm/1", "event_seq": 1, "plan": "PL-1028-SOL-01", "rev": 3, "decision": "release_plan", "args": {}, "why": "x | y | z", "conf": 0.5, "next": ""}
        base = {"plan": "PL-1028-SOL-01", "has_position": False, "event_types": [], "blackout_min": 20, "carry_policy": None}
        r_block = validate.semantic("pm/1", pm, base)
        r_warn = validate.semantic("pm/1", pm, dict(base, blackout_warn=True))
        self.assertIn("RELEASE_UNSAFE", r_block)
        self.assertNotIn("RELEASE_UNSAFE", r_warn)

    def test_constants_match_core(self):
        self.assertEqual(validate.BLACKOUT_MAX_SIZE_MULT, blackout.MAX_SIZE_MULT)
        self.assertEqual(validate.BLACKOUT_RISK_CODE, blackout.RISK_CODE)
        self.assertRegex(blackout.RISK_CODE, briefing.RE_RISK.pattern)


class TestBrief(unittest.TestCase):
    def test_mode_in_blackout_block(self):
        root = tempfile.mkdtemp(prefix="tbbo_")
        wjson(paths.exec_limits_file(root=root), {"paper": {"blackout_mode": "warn"}})
        for exec_mode, want in (("paper", "warn"), ("live", "block"), ("observe", "block"), (None, "block")):
            b = {"market": {"blackout": {"event": "fomc", "in_min": 20}}}
            brief._blackout_mode(b, types.SimpleNamespace(root=root, health={"data": {"mode": exec_mode}}))
            self.assertEqual(b["market"]["blackout"]["mode"], want, exec_mode)
        b = {"market": {"btc": {}}}
        brief._blackout_mode(b, types.SimpleNamespace(root=root, health={"data": {"mode": "paper"}}))
        self.assertEqual(b["market"], {"btc": {}})


class TestStats(unittest.TestCase):
    def test_group_by_blackout_and_journal_by(self):
        def row(i, tags):
            return {"ev": "open", "id": "T%d" % i, "mode": "paper", "tags": tags, "sym": "SOL", "side": "long"}
        trades = [expstats._norm(dict(row(1, ["paper", "blackout"]), r=1.0, pnl_usdt=1.0)), expstats._norm(dict(row(2, ["paper"]), r=-1.0, pnl_usdt=-1.0)),
                  expstats._norm(dict(row(3, ["paper"]), r=0.5, pnl_usdt=0.5))]
        self.assertIn("blackout", expstats.GROUP_BY)
        g = dict((x["key"], x["n"]) for x in expstats.group_stats(trades, "blackout", 1))
        self.assertEqual(g, {"blackout": 1, "обычные": 2})
        rows = [{"id": "A", "tags": ["paper", "blackout"], "r": 1.0, "pnl_usdt": 1.0}, {"id": "B", "tags": ["paper"], "r": -1.0, "pnl_usdt": -1.0}]
        self.assertIn("blackout", journal.BY_KEYS)
        out = dict((x["value"], x["n"]) for x in journal.analyze_by(rows, "blackout", 1))
        self.assertEqual(out, {"blackout": 1, "обычные": 1})

    def test_case_situation_marks_blackout(self):
        from tb.memory import features
        sit = features.situation_from_trade({"sym": "SOLUSDT", "side": "long", "entry": 100.0, "sl": 99.0, "tp": 102.0, "tags": ["paper", "blackout"]}, None, {})
        self.assertIs(sit["blackout"], True)
        sit = features.situation_from_trade({"sym": "SOLUSDT", "side": "long", "entry": 100.0, "sl": 99.0, "tp": 102.0, "tags": ["paper"]}, None, {})
        self.assertIsNone(sit["blackout"])


class TestPrompts(unittest.TestCase):
    def test_rule_present_and_versions_bumped(self):
        from tb.brain import roles_runtime
        reg = roles_runtime.load(_bootstrap.ROOT)
        ma, pm = reg.get("morning_analyst"), reg.get("position_manager")
        self.assertEqual((ma.version, pm.version), (2, 3))      # position_manager v3: реакции на STALLED / FLOW_TURN сторожа
        for r in (ma, pm):
            self.assertIn("blackout.mode=warn", r.system)
            self.assertIn("only paper", r.system)
        self.assertIn("blackout_warn", ma.system)
        self.assertIn("not above 0.5", ma.system)
        self.assertIn("0 plans", ma.system)
        self.assertIn("blackout.mode", roles_runtime.read_common(_bootstrap.ROOT))
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", ma.system))


if __name__ == "__main__":
    unittest.main()
