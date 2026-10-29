"""WP15a: агрегат, предупреждения малой выборки, базовая частота, разрезы с поправкой Бонферрони."""
import math
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import NOW, mk, synthetic
from tb.memory import agg, cfg as memcfg, similar
from test_memory_similar import fold

CFG = memcfg.load()


def rs(values, **kw):
    return fold([mk(i, r=v, **kw) for i, v in enumerate(values)])


class TestSummarize(unittest.TestCase):
    def test_basic_numbers(self):
        s = agg.summarize(rs([-1.0, 0.5, 1.5, -0.5]), 0.0, CFG)
        self.assertEqual((s["n"], s["n_closed"]), (4, 4))
        self.assertAlmostEqual(s["mean_r"], 0.125)
        self.assertAlmostEqual(s["win"], 0.5)
        self.assertAlmostEqual(s["median_r"], 0.0)
        sd = math.sqrt(sum((x - 0.125) ** 2 for x in (-1.0, 0.5, 1.5, -0.5)) / 3)
        self.assertAlmostEqual(s["se"], sd / 2, places=4)

    def test_se_not_computed_below_3(self):
        self.assertIsNone(agg.summarize(rs([1.0, 2.0]), None, CFG)["se"])
        self.assertIsNotNone(agg.summarize(rs([1.0, 2.0, 0.0]), None, CFG)["se"])

    def test_warn_levels(self):
        self.assertEqual(agg.summarize(rs([1.0] * 4), None, CFG)["warn"], "INSUFFICIENT")
        self.assertEqual(agg.summarize(rs([1.0] * 5), None, CFG)["warn"], "LOW_N")
        self.assertEqual(agg.summarize(rs([1.0] * 29), None, CFG)["warn"], "LOW_N")
        self.assertEqual(agg.summarize(rs([1.0] * 30), None, CFG)["warn"], "OK_NO_MULTIPLE")
        self.assertEqual(agg.summarize([], None, CFG)["warn"], "INSUFFICIENT")

    def test_warn_text_has_no_advice(self):
        for lvl in agg.WARN_TEXT.values():
            self.assertNotRegex(lvl.lower(), r"рекоменду|надо|следует|нужно")

    def test_non_trades_not_counted_as_closed(self):
        lst = fold([mk(0, r=1.0), mk(1, type="idea"), mk(2, type="skip")])
        s = agg.summarize(lst, None, CFG)
        self.assertEqual((s["n"], s["n_closed"]), (3, 1))

    def test_top_err_and_rule_followed(self):
        lst = fold([mk(0, err=["no-confirm", "chase"], rule_followed="no"), mk(1, err=["no-confirm"], rule_followed="yes"), mk(2, err=[], rule_followed="partial"),
                    mk(3, err=["no-confirm"], rule_followed="na")])
        s = agg.summarize(lst, None, CFG)
        self.assertEqual(s["top_err"][0], ["no-confirm", 0.75])
        self.assertEqual(s["top_err"][1], ["chase", 0.25])
        self.assertAlmostEqual(s["rule_followed_no_share"], 1 / 3.0, places=3)

    def test_mae_mfe(self):
        lst = fold([mk(0, r=1.0), mk(1, r=-1.0)])
        lst[0]["result"].update({"mae_r": -0.2, "mfe_r": 2.0})
        lst[1]["result"].update({"mae_r": -1.0, "mfe_r": 0.4})
        s = agg.summarize(lst, None, CFG)
        self.assertAlmostEqual(s["mean_mae_r"], -0.6)
        self.assertAlmostEqual(s["mean_mfe_r"], 1.2)

    def test_skip_whatif(self):
        lst = fold([mk(0, type="skip", whatif={"h_min": 15, "net_dir_pct": -2.0}), mk(1, type="idea", whatif={"h_min": 15, "net_dir_pct": 3.0}),
                    mk(2, type="skip", whatif={"h_min": 15, "net_dir_pct": -1.0})])
        w = agg.summarize(lst, None, CFG)["skip_whatif"]
        self.assertEqual(w["n"], 3)
        self.assertAlmostEqual(w["mean_net_dir_pct"], 0.0)
        self.assertAlmostEqual(w["correct_share"], 2 / 3.0, places=3)

    def test_no_closed_uses_n_for_warn(self):
        lst = fold([mk(i, type="idea") for i in range(6)])
        self.assertEqual(agg.summarize(lst, None, CFG)["warn"], "LOW_N")


class TestBase(unittest.TestCase):
    def test_base_rate_same_mode_only(self):
        lst = fold([mk(0, r=1.0), mk(1, r=-1.0, mode="paper"), mk(2, r=0.0)])
        self.assertEqual(agg.base_rate(lst, "live")["base_r"], 0.5)
        self.assertEqual(agg.base_rate(lst, "paper")["base_r"], -1.0)

    def test_base_rate_excludes(self):
        lst = fold([mk(0, r=1.0), mk(1, r=-5.0, exclude=True), mk(2, r=-5.0, role="mechanics")])
        self.assertEqual(agg.base_rate(lst, "live", ("mechanics",)), {"base_r": 1.0, "base_n": 1})
        self.assertIsNone(agg.base_rate([], "live")["base_r"])

    def test_report_includes_base_and_sim_fields(self):
        allc = fold(synthetic(40))
        s = similar.select(allc, {"features": {"confirmed": False}, "side": "long", "mode": "live"}, CFG, NOW)
        rep = agg.build_report(s, allc, CFG)
        self.assertIsNotNone(rep["base_r"])
        self.assertEqual(rep["min_sim_used"], 0.55)
        self.assertIn("avg_sim", rep)
        self.assertEqual(rep["base_n"], 40)


class TestBreakdown(unittest.TestCase):
    def test_groups_and_bonferroni(self):
        lst = fold(synthetic(40))
        rows = agg.breakdown(lst, "confirmed", 0.3, CFG)
        self.assertEqual(sorted(r["value"] for r in rows), ["false", "true"])
        for r in rows:
            self.assertEqual(r["k"], 2)
            self.assertAlmostEqual(r["alpha_adj"], 0.025)
        no = [r for r in rows if r["value"] == "false"][0]
        self.assertLess(no["mean_r"], 0)
        self.assertTrue(no["below_alpha"])
        self.assertEqual(no["warn"], "LOW_N")

    def test_err_membership(self):
        lst = fold(synthetic(40))
        rows = agg.breakdown(lst, "err", None, CFG)
        self.assertEqual(sorted(r["value"] for r in rows), ["no-confirm", "нет"])

    def test_bad_key(self):
        with self.assertRaises(ValueError):
            agg.breakdown([], "weather", None, CFG)

    def test_multiple_comparison_note_for_large_groups(self):
        lst = fold([mk(i, r=0.1 * (i % 5), confirmed=bool(i % 2)) for i in range(70)])
        rows = agg.breakdown(lst, "confirmed", 0.2, CFG)
        self.assertEqual({r["warn"] for r in rows}, {"OK_MULTIPLE"})


if __name__ == "__main__":
    unittest.main()
