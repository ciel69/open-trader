"""WP16: эквивалентность уровня 3 прежнему guard (золотой файл записан ДО правок) и поведение других уровней в guard."""
import argparse
import json
import os
import time

import _bootstrap  # noqa: F401
import _wp16 as W
import _wp16_scen as S
from tb.core import guard, portfolio, risklevels as R, session


def gargs(**kw):
    d = dict(sym="BTC", side="long", entry=100.0, sl=99.0, tp=103.6, equity=100.0, free=80.0, score=1.0, regime="range", min_quote=1.0,
             contract_max_lev=50, no_session=True, profile=None, paper=False)
    d.update(kw)
    return argparse.Namespace(**d)


def set_port(eq=100.0, hwm=100.0):
    portfolio.save({"venues": {"platform": {"equity": eq, "free": eq * 0.8, "ts": "x"}}, "hwm": hwm})


class EquivalenceTest(W.Base):
    """Сетка из 260 evaluate + 7 evaluate_add: вывод идентичен золотому файлу без файла уровня, с конфигом, и с подписанным уровнем 3."""

    @classmethod
    def golden(cls):
        with open(W.GOLDEN, encoding="utf-8") as f:
            return json.load(f)

    def compare(self, got):
        gold = self.golden()
        self.assertEqual(sorted(got), sorted(gold))
        bad = [k for k in gold if got[k] != gold[k]]
        self.assertEqual(bad, [], "расхождения: %s" % [(k, gold[k], got[k]) for k in bad[:2]])

    def test_grid_has_variety(self):
        gold = self.golden()
        self.assertGreater(len(gold), 250)
        self.assertGreater(sum(1 for v in gold.values() if v.get("ok")), 30)
        self.assertTrue(any("max_open_positions" in " ".join(v["reasons"]) for v in gold.values()))
        self.assertTrue(any("drawdown_halt" in " ".join(v["reasons"]) for v in gold.values()))

    def test_without_levels_config(self):
        os.remove(os.path.join(self.root, "config", "risk_levels.json"))
        self.compare(S.run_all(self.root))

    def test_level3_default_no_state_file(self):
        self.compare(S.run_all(self.root))

    def test_level3_signed_file(self):
        self.sign_level(3)
        self.assertEqual(R.current_level(self.root)["source"], "signed")
        self.compare(S.run_all(self.root))

    def test_no_risk_level_key_in_output_at_level3(self):
        for v in S.run_all(self.root, 40).values():
            self.assertNotIn("risk_level", v.get("ctx", {}))

    def test_other_levels_do_change_grid(self):
        gold = self.golden()
        self.sign_level(1)
        got = S.run_all(self.root)
        self.assertNotEqual(got, gold)
        self.assertTrue(any(v.get("ctx", {}).get("risk_level", {}).get("level") == 1 for v in got.values()))


class LevelBehaviourTest(W.Base):
    def setUp(self):
        W.Base.setUp(self)
        set_port()

    def run_level(self, n, **kw):
        self.sign_level(n)
        return guard.evaluate(gargs(**kw))

    def test_risk_scales_with_level(self):
        r = {n: self.run_level(n, sl=99.0, tp=103.6) for n in (1, 2, 3, 4, 5)}
        for n in (1, 2, 3, 4, 5):
            self.assertTrue(r[n]["ok"], (n, r[n]["reasons"]))
        risks = [r[n]["risk_usdt"] for n in (1, 2, 3, 4, 5)]
        self.assertEqual(risks, sorted(risks))
        self.assertLessEqual(r[1]["risk_usdt"], 1.0 + 1e-6)          # 1% от 100
        self.assertLessEqual(r[5]["risk_usdt"], 5.0 + 1e-6)          # потолок 5%
        self.assertGreater(r[5]["risk_usdt"], 3.0)
        self.assertEqual(r[4]["ctx"]["risk_level"]["level"], 4)

    def test_ceiling_holds_even_if_file_is_greedy(self):
        c = self.cfg()
        c["levels"]["5"]["risk_per_trade_frac"] = 0.5
        self.put_cfg(c)
        r = self.run_level(5)
        self.assertLessEqual(r["risk_usdt"], 5.0 + 1e-6)

    def test_drawdown_ladder_still_applies_after_level(self):
        a = self.run_level(5)
        set_port(eq=88.0, hwm=100.0)         # просадка 12%: x0.5
        b = guard.evaluate(gargs(equity=None, free=None))
        self.assertEqual(b["ctx"]["dd_mult"], 0.5)
        self.assertLess(b["risk_usdt"], a["risk_usdt"])
        set_port(eq=75.0, hwm=100.0)          # >= drawdown_halt
        c = guard.evaluate(gargs(equity=None, free=None))
        self.assertFalse(c["ok"])
        self.assertTrue(any("drawdown_halt" in x for x in c["reasons"]))

    def test_max_open_positions_by_level(self):
        T = "2026-10-28T01:00:00Z"
        from tb.core import journal
        journal._append({"ev": "open", "id": "a", "sym": "AAA", "side": "long", "mode": "live", "risk_usdt": 0.1, "notional": 5, "entry": 1, "ts_open": T})
        self.assertFalse(self.run_level(1, sym="CCC")["ok"])
        self.assertTrue(any("max_open_positions" in x for x in self.run_level(1, sym="CCC")["reasons"]))
        self.assertTrue(self.run_level(3, sym="CCC")["ok"])
        journal._append({"ev": "open", "id": "b", "sym": "BBB", "side": "long", "mode": "live", "risk_usdt": 0.1, "notional": 5, "entry": 1, "ts_open": T})
        self.assertFalse(self.run_level(3, sym="CCC")["ok"])
        self.assertTrue(self.run_level(4, sym="CCC")["ok"])

    def test_total_open_risk_cap_level4(self):
        from tb.core import journal
        T = "2026-10-28T01:00:00Z"
        journal._append({"ev": "open", "id": "a", "sym": "AAA", "side": "long", "mode": "live", "risk_usdt": 9.5, "notional": 500, "entry": 1, "ts_open": T})
        r = self.run_level(4, sym="CCC")       # открытый риск 9.5 >= 9% от 100
        self.assertFalse(r["ok"])
        self.assertTrue(any("total_open_risk_cap" in x for x in r["reasons"]), r["reasons"])
        journal._append({"ev": "open", "id": "b", "sym": "BBB", "side": "long", "mode": "live", "risk_usdt": 8.0, "notional": 500, "entry": 1, "ts_open": T})
        # 3 позиции не открыть, но и риск строго ограничен: 8.0 + новая сделка <= 9%
        j = guard.evaluate(gargs(sym="DDD"))
        self.assertFalse(j["ok"])

    def test_total_cap_trims_new_trade_risk(self):
        from tb.core import journal
        T = "2026-10-28T01:00:00Z"
        journal._append({"ev": "open", "id": "a", "sym": "AAA", "side": "long", "mode": "live", "risk_usdt": 8.0, "notional": 500, "entry": 1, "ts_open": T})
        set_port(eq=100.0, hwm=100.0)
        r = self.run_level(4, sym="CCC")
        self.assertTrue(r["ok"], r["reasons"])
        self.assertLessEqual(r["risk_usdt"] + 8.0, 9.0 + 1e-6)

    def test_agent_limits_only_narrow(self):
        a = self.run_level(5)
        b = guard.evaluate(gargs(agent_limits={"max_risk_pct": 2.0, "max_risk_usdt": 1.5}))
        self.assertLessEqual(b["risk_usdt"], 1.5 + 1e-6)
        self.assertLess(b["risk_usdt"], a["risk_usdt"])
        c = guard.evaluate(gargs(agent_limits={"max_risk_pct": 99.0, "max_risk_usdt": 999.0}))
        self.assertEqual(c["risk_usdt"], a["risk_usdt"])      # агент мягче уровня: действует уровень

    def test_setup_aggr_rejected(self):
        self.sign_level(3)
        r = guard.evaluate(gargs(setup="momentum_chase"))
        self.assertFalse(r["ok"])
        self.assertTrue(any("E_SETUP_AGGR" in x for x in r["reasons"]))
        self.assertTrue(guard.evaluate(gargs(setup="sweep_reject"))["ok"])
        self.sign_level(4)
        self.assertTrue(guard.evaluate(gargs(setup="momentum_chase"))["ok"])

    def test_paper_follows_level(self):
        self.sign_level(1)
        p1 = guard.evaluate(gargs(paper=True, equity=100.0, free=None))
        self.sign_level(5)
        p5 = guard.evaluate(gargs(paper=True, equity=100.0, free=None))
        self.assertTrue(p1["ok"] and p5["ok"])
        self.assertLess(p1["risk_usdt"], p5["risk_usdt"])

    def test_session_budget_by_level(self):
        self.sign_level(4)
        prof = R.effective(guard.load_profile(os.path.join(self.root, "config", "risk_profile.json")), self.root)
        p = {"venues": {"platform": {"equity": 100.0, "free": 80.0, "ts": "x"}}, "hwm": 100.0}
        rec = session.start("intraday", "t", "platform", None, None, prof, p=p)
        self.assertAlmostEqual(rec["budget_usdt"], 3.75)          # 3% x 1.25
        self.sign_level(3)
        base = guard.load_profile(os.path.join(self.root, "config", "risk_profile.json"))
        # уровень 3: бюджет как в базе
        self.assertEqual(R.effective(base, self.root)["session_types"]["intraday"]["budget_frac"], base["session_types"]["intraday"]["budget_frac"])
