"""core/expstats.py и brain/exp_cli.py (WP7): известный набор сделок -> известные числа, n<30 помечено, Бонферрони, Уэлч на эталоне,
shadow-варианты, детерминированное присвоение, режимы не смешиваются, CLI на временном TB_ROOT."""
import contextlib
import io
import json
import math
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.brain import exp_cli
from tb.core import decisions, expstats as X, plan_schema as S


def tr(i, r, exp="e1", var="a", mode="paper", pnl=None, fees=0.01, risk=1.0, plan=None, extra=None, reason="sl", tags=None, ts="2026-10-2%dT10:00:00Z"):
    pnl = r * risk if pnl is None else pnl
    o = {"ev": "open", "id": "T%d-%s-%s-%s" % (i, exp, var, mode), "ts": ts % (i % 9), "sym": "SOL", "side": "long", "entry": 100.0, "lev": 10, "mode": mode,
         "stype": "intraday", "risk_usdt": risk, "tags": tags or [], "plan_id": plan or "PL-1028-SOL-%02d" % (i % 90 + 1)}
    if exp:
        o["exp"] = exp
    if var:
        o["var"] = var
    o.update(extra or {})
    c = {"ev": "close", "id": o["id"], "ts_close": "2026-10-28T12:00:00Z", "exit": 101.0, "reason": reason, "pnl_usdt": pnl, "fees_usdt": fees,
         "r": r}
    return [o, c]


class Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbexp_")
        os.makedirs(os.path.join(self.root, "state"))
        os.makedirs(os.path.join(self.root, "config"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, recs, junk=()):
        with open(os.path.join(self.root, "state", "trades.jsonl"), "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            for j in junk:
                f.write(j + "\n")


class TestLoad(Root):
    def test_missing_file_and_junk_lines(self):
        self.assertEqual(X.load_trades("all", self.root), [])
        recs = sum([tr(i, 1.0) for i in range(3)], [])
        self.write(recs, junk=["{оборванная", "", "[1,2]", '{"ev":"close","id":"nope","pnl_usdt":1}'])
        self.assertEqual(len(X.load_trades("all", self.root)), 3)

    def test_open_trade_excluded_and_mode_filter(self):
        recs = tr(1, 1.0, mode="paper") + tr(2, -1.0, mode="live") + [tr(3, 1.0)[0]]       # третья не закрыта
        self.write(recs)
        self.assertEqual(len(X.load_trades("all", self.root)), 2)
        self.assertEqual([t["mode"] for t in X.load_trades("paper", self.root)], ["paper"])
        self.assertEqual([t["mode"] for t in X.load_trades("live", self.root)], ["live"])
        with self.assertRaises(ValueError):
            X.load_trades("bogus", self.root)

    def test_tags_are_fallback_for_exp_var_setup(self):
        self.write(tr(1, 1.0, exp=None, var=None, tags=["bybit", "exp:sw-v1", "var:b", "setup:lv", "plan:PL-1028-ETH-03"]))
        t = X.load_trades("all", self.root)[0]
        self.assertEqual((t["exp"], t["var"], t["setup"]), ("sw-v1", "b", "lv"))

    def test_group_by_agent_and_model_from_gateway_tags(self):
        recs = tr(1, 1.0, tags=["agent.api", "model.deepseek/flash"]) + tr(2, -1.0, tags=["agent.api", "model.deepseek/pro"]) + tr(3, 0.5, tags=[])
        self.write(recs)
        ts = X.load_trades("all", self.root)
        ag = dict((g["key"], g["n"]) for g in X.group_stats(ts, "agent", 1))
        self.assertEqual(ag, {"api": 2, "(нет)": 1})
        md = dict((g["key"], g["n"]) for g in X.group_stats(ts, "model", 1))
        self.assertEqual(md, {"deepseek/flash": 1, "deepseek/pro": 1, "(нет)": 1})

    def test_r_fallback_from_pnl_and_risk(self):
        recs = tr(1, 1.0)
        del recs[1]["r"]
        recs[1]["pnl_usdt"] = 0.5
        self.write(recs)
        self.assertAlmostEqual(X.load_trades("all", self.root)[0]["r"], 0.5)

    def test_rules_from_decisions(self):
        self.write(tr(1, 1.0, plan="PL-1028-SOL-01"))
        decisions.append({"actor": "exec", "kind": "rule", "what": "move_stop", "plan": "PL-1028-SOL-01", "why": "RULE:be_1r", "res": {"ok": True}}, self.root)
        decisions.append({"actor": "exec", "kind": "rule", "what": "move_stop", "plan": "PL-1028-SOL-01", "why": "RULE:bad", "res": {"ok": False}}, self.root)
        t = X.load_trades("all", self.root)[0]
        self.assertEqual(t["rules"], ["be_1r"])
        rows = X.group_stats([t], "rule")
        self.assertEqual([r["key"] for r in rows], ["be_1r"])

    def test_meta_tolerant(self):
        self.assertEqual(X.load_meta(self.root), {})
        with open(os.path.join(self.root, "config", "experiments.json"), "w") as f:
            f.write("{мусор")
        self.assertEqual(X.load_meta(self.root), {})


class TestGroupStats(unittest.TestCase):
    def known(self):
        # 4 сделки: R = +2, -1, +1, -1; pnl = R * 1.0; комиссии по 0.1
        out = []
        for i, r in enumerate((2.0, -1.0, 1.0, -1.0)):
            out.append(X._norm(dict(tr(i, r, fees=0.1)[0], **tr(i, r, fees=0.1)[1])))
        return out

    def test_known_numbers(self):
        r = X.group_stats(self.known(), "exp")[0]
        self.assertEqual(r["n"], 4)
        self.assertAlmostEqual(r["win_rate"], 0.5)
        self.assertAlmostEqual(r["avg_r"], 0.25)
        sd = math.sqrt(((1.75) ** 2 + (1.25) ** 2 + (0.75) ** 2 + (1.25) ** 2) / 3.0)
        self.assertAlmostEqual(r["sd_r"], sd, 9)
        self.assertAlmostEqual(r["se_r"], sd / 2.0, 9)
        self.assertAlmostEqual(r["pf"], 3.0 / 2.0)
        self.assertAlmostEqual(r["fees"], 0.4)
        self.assertAlmostEqual(r["fees_share"], 0.4 / 5.0)
        self.assertAlmostEqual(r["expectancy_usdt"], 0.25)

    def test_small_sample_marked_without_p(self):
        r = X.group_stats(self.known(), "exp")[0]
        self.assertTrue(r["small"])
        self.assertEqual(r["mark"], "*")
        self.assertIsNone(r["p"])
        self.assertIsNone(r["below_alpha"])

    def test_n30_unmarked_and_p_computed(self):
        rows = [X._norm(dict(tr(i, 0.5 if i % 2 else -0.4)[0], **tr(i, 0.5 if i % 2 else -0.4)[1])) for i in range(30)]
        r = X.group_stats(rows, "exp")[0]
        self.assertEqual(r["mark"], "")
        self.assertIsNotNone(r["p"])
        self.assertEqual(X.group_stats(rows, "exp", min_n=31)[0]["mark"], "*")

    def test_pf_inf_when_no_losses(self):
        rows = [X._norm(dict(tr(i, 1.0)[0], **tr(i, 1.0)[1])) for i in range(3)]
        r = X.group_stats(rows, "exp")[0]
        self.assertIsNone(r["pf"])
        self.assertTrue(r["pf_inf"])

    def test_bonferroni_k_is_number_of_groups(self):
        rows = []
        for v in ("a", "b", "c", "d", "e"):
            rows += [X._norm(dict(tr(i, 1.0, var=v)[0], **tr(i, 1.0, var=v)[1])) for i in range(2)]
        g = X.group_stats(rows, "variant")
        self.assertEqual(len(g), 5)
        self.assertTrue(all(r["k"] == 5 and abs(r["alpha_adj"] - 0.01) < 1e-12 for r in g))
        self.assertAlmostEqual(X.bonferroni(5), 0.01)
        self.assertAlmostEqual(X.bonferroni(0), 0.05)

    def test_modes_never_mixed(self):
        rows = [X._norm(dict(tr(i, 1.0, mode=m)[0], **tr(i, 1.0, mode=m)[1])) for i in range(2) for m in ("paper", "live")]
        g = X.group_stats(rows, "exp")
        self.assertEqual(sorted((r["mode"], r["n"]) for r in g), [("live", 2), ("paper", 2)])

    def test_empty_and_unknown_by(self):
        self.assertEqual(X.group_stats([], "exp"), [])
        with self.assertRaises(ValueError):
            X.group_stats(self.known(), "nope")

    def test_mae_mfe_capture_rule_share(self):
        recs = []
        for i, (r, reason, mae, mfe) in enumerate(((2.0, "trail", 0.3, 3.0), (-1.0, "sl", 1.0, 0.2), (1.0, "be", 0.2, 2.0), (-1.0, "timeout", 0.9, 0.1))):
            o, c = tr(i, r, reason=reason, fees=0.0)
            c["path"] = {"mae_r": mae, "mfe_r": mfe}
            recs.append(X._norm(dict(o, **c)))
        g = X.group_stats(recs, "exp")[0]
        self.assertAlmostEqual(g["rule_close_share"], 0.75)       # trail, be, timeout
        self.assertAlmostEqual(g["mfe_capture"], (2.0 / 3.0 + 1.0 / 2.0) / 2.0)
        self.assertAlmostEqual(g["mae_r_win"], 0.25)
        self.assertAlmostEqual(g["mfe_r_loss"], 0.15)

    def test_mixed_rule_revisions_flagged(self):
        a = X._norm(dict(tr(1, 1.0, extra={"plan_rev": 1})[0], **tr(1, 1.0)[1]))
        b = X._norm(dict(tr(2, 1.0, extra={"plan_rev": 2})[0], **tr(2, 1.0)[1]))
        self.assertTrue(X.group_stats([a, b], "exp")[0]["mixed_rules"])
        self.assertFalse(X.group_stats([a], "exp")[0]["mixed_rules"])


class TestStudent(unittest.TestCase):
    def test_quantiles_against_tables(self):
        for df, want in ((1, 12.7062), (5, 2.5706), (10, 2.2281), (30, 2.0423), (120, 1.9799)):
            self.assertAlmostEqual(X.t_quantile(0.975, df), want, 3, msg=str(df))
        self.assertAlmostEqual(X.t_two_sided_p(2.0423, 30), 0.05, 4)
        self.assertAlmostEqual(X.t_two_sided_p(0.0, 10), 1.0, 9)

    def test_welch_reference_example(self):
        """Эталон (Wikipedia, Welch's t-test): t=-2.46, df=25.0, p=0.021."""
        x = [27.5, 21.0, 19.0, 23.6, 17.0, 17.9, 16.9, 20.1, 21.9, 22.6, 23.1, 19.6, 19.0, 21.7, 21.4]
        y = [27.1, 22.0, 20.8, 23.4, 23.4, 23.5, 25.8, 22.0, 24.8, 20.2, 21.9, 22.1, 22.9, 20.5, 24.4]
        w = X._welch(x, y)
        self.assertAlmostEqual(w["t"], -2.455, 2)
        self.assertAlmostEqual(w["df"], 24.99, 1)
        self.assertAlmostEqual(w["p"], 0.0214, 3)
        self.assertAlmostEqual(w["diff"], -2.1667, 3)


def groups(a, b, exp="e1", mode="paper", **kw):
    rows = []
    for i, r in enumerate(a):
        o, c = tr(i, r, exp=exp, var="a", mode=mode, **kw)
        rows.append(X._norm(dict(o, **c)))
    for i, r in enumerate(b):
        o, c = tr(i + 100, r, exp=exp, var="b", mode=mode, **kw)
        rows.append(X._norm(dict(o, **c)))
    return rows


class TestCompare(unittest.TestCase):
    def test_reference_numbers_and_ci(self):
        x = [27.5, 21.0, 19.0, 23.6, 17.0, 17.9, 16.9, 20.1, 21.9, 22.6, 23.1, 19.6, 19.0, 21.7, 21.4]
        y = [27.1, 22.0, 20.8, 23.4, 23.4, 23.5, 25.8, 22.0, 24.8, 20.2, 21.9, 22.1, 22.9, 20.5, 24.4]
        c = X.compare(groups(x, y), "e1", "a", "b", min_n=15)
        self.assertEqual(c["status"], "ok")
        self.assertAlmostEqual(c["diff"], -2.1667, 3)
        self.assertAlmostEqual(c["p"], 0.0214, 3)
        lo, hi = c["ci95"]
        self.assertAlmostEqual(lo, -2.1667 - 2.0595 * 0.88242, 2)        # t(0.975, 25) = 2.0595
        self.assertLess(hi, 0)
        self.assertTrue(c["significant"])                                # k=1: p < 0.05
        c2 = X.compare(groups(x, y), "e1", "a", "b", min_n=15, k=3)      # порог 0.0167 < p=0.0214
        self.assertFalse(c2["significant"])
        self.assertTrue(any("не значимо" in n for n in c2["notes"]))
        self.assertGreater(c2["ci_adj"][1], c["ci95"][1])                # скорректированный ДИ шире

    def test_small_n_never_significant(self):
        c = X.compare(groups([2.0, 2.1, 1.9, 2.2], [-2.0, -1.9, -2.1, -2.2]), "e1", "a", "b")
        self.assertEqual(c["status"], "insufficient_n")
        self.assertFalse(c["significant"])
        self.assertTrue(any("n=4" in n for n in c["notes"]))

    def test_no_data_and_case_insensitive(self):
        self.assertEqual(X.compare([], "e1", "a", "b")["status"], "no_data")
        c = X.compare(groups([1.0] * 3, [0.0] * 3), "e1", "A", "B")
        self.assertEqual(c["n_a"], 3)

    def test_zero_variance(self):
        c = X.compare(groups([1.0] * 30, [0.0] * 30), "e1", "a", "b")
        self.assertIsNone(c["p"])
        self.assertFalse(c["significant"])

    def test_mixed_modes_noted(self):
        a = groups([1.0, -1.0] * 15, [], mode="live")
        b = groups([], [0.5, -0.5] * 15, mode="paper")
        c = X.compare(a + b, "e1", "a", "b")
        self.assertEqual(c["modes"], ["live", "paper"])
        self.assertTrue(any("режимы различаются" in n for n in c["notes"]))

    def test_mixed_rule_revisions_block_verdict(self):
        rows = groups([1.0, -0.5] * 15, [0.0, 0.1] * 15, extra={"plan_rev": 1})
        for t in rows[:3]:
            t["rules_key"] = 2
        c = X.compare(rows, "e1", "a", "b")
        self.assertEqual(c["status"], "mixed_rules")
        self.assertFalse(c["significant"])

    def test_paired_by_parent_plan(self):
        rows = []
        for i in range(5):
            pid = "PL-1028-SOL-%02d" % (i + 1)
            for var, r, p in (("a", 1.0 + i * 0.1, pid), ("b", 0.5 + i * 0.1 + (0.05 if i % 2 else 0), pid + "-S1")):
                o, c = tr(i, r, var=var, plan=p)
                rows.append(X._norm(dict(o, **c)))
        c = X.compare(rows, "e1", "a", "b", min_n=3)
        self.assertEqual(c["paired"]["n_pairs"], 5)
        self.assertAlmostEqual(c["paired"]["mean_diff"], 0.5 - 0.05 * 2 / 5.0, 9)


class TestListAndShadow(Root):
    def test_list_experiments_status_and_meta(self):
        recs = []
        for i in range(30):
            recs += tr(i, 0.1, var="a")
            recs += tr(i, 0.1, var="b") if i < 10 else []
        self.write(recs)
        with open(os.path.join(self.root, "config", "experiments.json"), "w") as f:
            json.dump({"e1": {"hypothesis": "H", "min_n": 30, "started": "2026-10-28"}}, f)
        rows = X.list_experiments(X.load_trades("all", self.root), X.load_meta(self.root))
        self.assertEqual(rows[0]["n"], 40)
        self.assertEqual(rows[0]["variants"], {"a": 30, "b": 10})
        self.assertEqual(rows[0]["status"], "идёт")                    # b < 30
        self.assertEqual(rows[0]["hypothesis"], "H")

    def test_assign_variant_deterministic_and_balanced(self):
        vs = ["a", "b"]
        self.assertEqual([X.assign_variant("sig%d" % i, vs, "e1") for i in range(5)], [X.assign_variant("sig%d" % i, vs, "e1") for i in range(5)])
        got = [X.assign_variant("sig%d" % i, vs, "e1") for i in range(400)]
        self.assertTrue(150 < got.count("a") < 250)
        self.assertNotEqual([X.assign_variant("s%d" % i, vs, "e1") for i in range(50)], [X.assign_variant("s%d" % i, vs, "e2") for i in range(50)])
        with self.assertRaises(ValueError):
            X.assign_variant("x", [])

    def test_can_start_paper_limits(self):
        spec = {"symbol": "SOL", "mode": "paper", "tags": {"experiment": "e1", "variant": "a"}}
        self.assertEqual(X.can_start_paper([], spec), (True, "OK"))
        self.assertEqual(X.can_start_paper([spec], spec), (False, "PAPER_DUP_SYMBOL_VARIANT"))
        other = dict(spec, tags={"experiment": "e1", "variant": "b"})
        self.assertTrue(X.can_start_paper([spec], other)[0])
        live = dict(spec, mode="live")
        self.assertTrue(X.can_start_paper([live], spec)[0])             # живые планы бумажный лимит не занимают
        many = [dict(spec, symbol="S%d" % i) for i in range(20)]
        self.assertEqual(X.can_start_paper(many, spec), (False, "PAPER_MAX_ACTIVE"))


def parent_plan():
    return {"schema": "tb.plan/1", "id": "PL-1028-SOL-01", "rev": 3, "symbol": "SOL", "side": "long", "mode": "live", "session_type": "intraday",
            "source": {"kind": "brain"}, "thesis": "Прокол уровня и возврат на повышенном объёме", "tags": {"experiment": "e1", "variant": "A", "setup": "lv"},
            "entry": {"when": {"kind": "candle_close", "tf": "5m", "level": 100.0, "dir": "above"}, "how": {"type": "market"}},
            "stop": {"price": 95.0, "trigger_by": "last"},
            "targets": [{"id": "t1", "price": 110.0, "frac": 1.0, "exec": "resting"}],
            "risk": {"score": 0.7, "size_mult": 1.0}, "manage": [], "invalidation": [], "lifecycle": {"ttl_min": 45, "max_hold_min": 240},
            "prereg_hash": "abc", "armed_ts": 5, "created_ts": 1, "created_by": "brain:chat", "guard_preview": {"ok": True},
            "shadow_variants": [{"name": "wide-stop", "patch": {"stop": {"price": 93.0}}},
                                {"name": "no-when", "patch": {"entry": {"when": None}, "risk": {"size_mult": 0.5, "confirm_waiver": True}}}]}


class TestShadow(unittest.TestCase):
    def test_variants_built_with_allowed_patch(self):
        p = parent_plan()
        rep = X.shadow_variants_report(p)
        self.assertEqual([r["suffix"] for r in rep], ["S1", "S2"])
        s = rep[0]["spec"]
        self.assertTrue(rep[0]["ok"], rep[0]["issues"])
        self.assertEqual(s["mode"], "paper")
        self.assertEqual(s["source"], {"kind": "shadow", "parent_plan": "PL-1028-SOL-01"})
        self.assertEqual(s["stop"], {"price": 93.0, "trigger_by": "last"})
        self.assertEqual(s["tags"], {"experiment": "e1", "variant": "wide-stop", "setup": "lv"})
        for k in ("id", "rev", "prereg_hash", "armed_ts", "guard_preview", "shadow_variants", "created_by"):
            self.assertNotIn(k, s)
        self.assertEqual(s["entry"], p["entry"])                       # остальное как у родителя
        self.assertEqual(p["stop"]["price"], 95.0)                     # родитель не мутирован
        self.assertEqual(len(p["shadow_variants"]), 2)

    def test_same_entry_conditions_for_stop_only_patch(self):
        specs = X.shadow_variants(parent_plan())
        self.assertEqual(specs[0]["entry"]["when"], parent_plan()["entry"]["when"])

    def test_forbidden_fields_rejected(self):
        for patch in ({"symbol": "ETH"}, {"side": "short"}, {"mode": "live"}, {"tags": {"variant": "x"}}, {"thesis": "x" * 20}):
            p = parent_plan()
            p["shadow_variants"] = [{"name": "bad", "patch": patch}]
            rep = X.shadow_variants_report(p)
            self.assertFalse(rep[0]["ok"], patch)
            self.assertEqual(rep[0]["issues"][0]["code"], "E_UNKNOWN_FIELD")
            self.assertEqual(X.shadow_variants(p), [])

    def test_risk_up_forbidden(self):
        p = parent_plan()
        p["risk"]["size_mult"] = 0.5
        p["shadow_variants"] = [{"name": "bigger", "patch": {"risk": {"size_mult": 1.0}}}, {"name": "same", "patch": {"risk": {"size_mult": 0.5}}}]
        rep = X.shadow_variants_report(p)
        self.assertEqual(rep[0]["issues"][0]["code"], "E_RISK_UP")
        self.assertTrue(rep[1]["ok"], rep[1]["issues"])

    def test_invalid_result_dropped_by_static_validation(self):
        p = parent_plan()
        p["shadow_variants"] = [{"name": "bad-stop", "patch": {"stop": {"price": 150.0}}}]       # long: стоп выше входа
        rep = X.shadow_variants_report(p)
        self.assertFalse(rep[0]["ok"])
        self.assertTrue(rep[0]["issues"])

    def test_garbage_plan_and_limits(self):
        self.assertEqual(X.shadow_variants(None), [])
        self.assertEqual(X.shadow_variants({}), [])
        p = parent_plan()
        p["shadow_variants"] = [{"name": "v%d" % i, "patch": {"stop": {"price": 94.0 - i}}} for i in range(6)]
        self.assertEqual(len(X.shadow_variants_report(p)), 3)
        p["shadow_variants"] = ["строка", {"name": 5, "patch": {}}, {"name": "ok", "patch": 3}]
        self.assertEqual(X.shadow_variants(p), [])

    def test_deterministic(self):
        self.assertEqual(json.dumps(X.shadow_variants(parent_plan()), sort_keys=True), json.dumps(X.shadow_variants(parent_plan()), sort_keys=True))


class TestCli(Root):
    def run_cli(self, argv):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = exp_cli.main(argv, root=self.root)
        return code, buf.getvalue(), err.getvalue()

    def setUp(self):
        Root.setUp(self)
        recs = []
        for i in range(32):
            recs += tr(i, 0.6 if i % 3 else -1.0, var="a", mode="paper")
            recs += tr(i, 0.2 if i % 2 else -0.5, var="b", mode="paper")
        recs += tr(1, 1.0, exp="e2", var="a", mode="live")
        self.write(recs)

    def test_stats_json_and_text(self):
        code, out, _ = self.run_cli(["stats", "--by", "variant", "--mode", "paper", "--json"])
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["k"], 2)
        self.assertEqual(sorted(g["key"] for g in d["groups"]), ["e1/a", "e1/b"])
        self.assertAlmostEqual(d["alpha_adj"], 0.025)
        code, out, _ = self.run_cli(["stats", "--by", "exp"])
        self.assertIn("e2", out)
        self.assertIn("*", out)                 # e2 n=1
        self.assertIn("Бонферрони", out)
        self.assertNotIn("доказан", out.lower())

    def test_list_show_compare(self):
        code, out, _ = self.run_cli(["list", "--mode", "paper"])
        self.assertEqual(code, 0)
        self.assertIn("e1", out)
        self.assertNotIn("e2", out)
        code, out, _ = self.run_cli(["show", "e1", "--mode", "paper"])
        self.assertEqual(code, 0)
        self.assertIn("e1/a", out)
        self.assertIn("разность", out)
        code, out, _ = self.run_cli(["compare", "e1", "A", "B", "--mode", "paper", "--k", "2", "--json"])
        d = json.loads(out)
        self.assertEqual((d["n_a"], d["n_b"], d["k"]), (32, 32, 2))
        self.assertIn("ci95", d)

    def test_compare_small_n_text_has_no_verdict(self):
        code, out, _ = self.run_cli(["compare", "e2", "a", "b"])
        self.assertEqual(code, 0)
        self.assertIn("нет закрытых сделок", out)

    def test_usage_errors(self):
        self.assertEqual(self.run_cli([])[0], 2)
        self.assertEqual(self.run_cli(["stats", "--by", "nope"])[0], 2)
        self.assertEqual(self.run_cli(["stats", "--min-n", "0"])[0], 2)
        self.assertEqual(self.run_cli(["compare", "e1", "a", "b", "--k", "0"])[0], 2)

    def test_empty_root(self):
        os.remove(os.path.join(self.root, "state", "trades.jsonl"))
        code, out, _ = self.run_cli(["stats"])
        self.assertEqual(code, 0)
        self.assertIn("Нет закрытых", out)
        self.assertEqual(self.run_cli(["list"])[0], 0)

    def test_dispatcher_returns_code(self):
        os.environ["TB_ROOT"] = self.root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = exp_cli.main(["list", "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(buf.getvalue())["ok"])
        finally:
            del os.environ["TB_ROOT"]


if __name__ == "__main__":
    unittest.main()
