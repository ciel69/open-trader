"""WP15a: сходство Gower, фильтры, top-k, порядок, покрытие, детерминизм, скорость."""
import copy
import random
import time
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import NOW, mk, synthetic, ts_at
from tb.memory import cfg as memcfg, similar

CFG = memcfg.load()


def fold(lst):
    out = []
    for i, c in enumerate(lst):
        c = copy.deepcopy(c)
        c["rev"], c["seq"] = 1, i + 1
        c["tags"] = c.get("tags") or {"err": []}
        out.append(c)
    return out


def sel(lst, feats, side=None, **kw):
    return similar.select(fold(lst), {"features": feats, "side": side, "mode": kw.pop("mode", "live")}, CFG, NOW, **kw)


class TestFeatureSim(unittest.TestCase):
    def test_categorical(self):
        self.assertEqual(similar.feature_sim("regime", "range", "range", CFG), 1.0)
        self.assertEqual(similar.feature_sim("regime", "range", "trend", CFG), 0.0)

    def test_setup_family_partial(self):
        self.assertEqual(similar.feature_sim("setup", "level_reclaim", "level_reject", CFG), 0.5)
        self.assertEqual(similar.feature_sim("setup", "level_reject", "level_reclaim", CFG), 0.5)
        self.assertEqual(similar.feature_sim("setup", "level_reclaim", "range_fade", CFG), 0.0)

    def test_bool(self):
        self.assertEqual(similar.feature_sim("confirmed", True, True, CFG), 1.0)
        self.assertEqual(similar.feature_sim("confirmed", True, False, CFG), 0.0)

    def test_numeric_scale_linear(self):
        self.assertAlmostEqual(similar.feature_sim("stop_pct", 1.0, 2.5, CFG), 0.5)          # шкала 3.0
        self.assertEqual(similar.feature_sim("stop_pct", 0.0, 9.0, CFG), 0.0)

    def test_numeric_log(self):
        lin = abs(1.0 - 2.0)
        self.assertGreater(similar.feature_sim("turn_surge_5m", 20.0, 21.0, CFG), similar.feature_sim("turn_surge_5m", 1.0, 2.0, CFG))
        self.assertTrue(lin)

    def test_cyclic_hour(self):
        self.assertEqual(similar.feature_sim("utc_hour", 23, 1, CFG), 1 - 2 / 12.0)
        self.assertEqual(similar.feature_sim("utc_hour", 0, 12, CFG), 0.0)
        self.assertEqual(similar.feature_sim("utc_hour", 5, 5, CFG), 1.0)

    def test_identical_vectors_sim_one(self):
        v = similar.vector(mk(0))
        s, cov, n = similar.similarity(v, v, CFG)
        self.assertEqual((s, cov), (1.0, 1.0))
        self.assertGreater(n, 5)

    def test_missing_features_skipped_and_reduce_coverage(self):
        q = {"side": "long", "confirmed": False, "range_5m_pct": 1.8, "regime": "range"}
        v = {"side": "long", "confirmed": False}
        s, cov, n = similar.similarity(q, v, CFG)
        self.assertEqual(s, 1.0)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(cov, 6 / 10.0, places=3)

    def test_no_overlap(self):
        self.assertEqual(similar.similarity({"side": "long"}, {"regime": "range"}, CFG), (0.0, 0.0, 0))

    def test_weights_matter(self):
        q = {"side": "long", "fng": 10}
        a = similar.similarity(q, {"side": "long", "fng": 90}, CFG)[0]
        b = similar.similarity(q, {"side": "short", "fng": 10}, CFG)[0]
        self.assertGreater(a, b)             # side весит 3, fng 0.5


class TestSelect(unittest.TestCase):
    def test_exact_first_order(self):
        a = mk(0, sym="AAA", r=-1.0)
        b = mk(1, sym="BBB", r=0.5, stop_pct=2.5, range_5m_pct=2.2)
        c = mk(2, sym="CCC", r=0.5, stop_pct=3.9, range_5m_pct=3.5, regime="trend")
        feats = {"confirmed": False, "setup": "level_reclaim", "regime": "range", "stop_pct": 2.2, "range_5m_pct": 1.8}
        r = sel([c, b, a], feats, "long")
        self.assertEqual([h["case"]["situation"]["sym"] for h in r["hits"]], ["AAA", "BBB", "CCC"])
        self.assertEqual(r["hits"][0]["sim"], 1.0)

    def test_shuffle_invariance(self):
        lst = synthetic(40)
        feats = {"confirmed": False, "setup": "level_reclaim", "range_5m_pct": 1.6}
        base = [(h["case"]["case_id"], h["sim"]) for h in sel(lst, feats, "long", k=10)["hits"]]
        for seed in (1, 2, 3):
            l2 = list(lst)
            random.Random(seed).shuffle(l2)
            self.assertEqual([(h["case"]["case_id"], h["sim"]) for h in sel(l2, feats, "long", k=10)["hits"]], base)

    def test_tie_prefers_fresher_then_id(self):
        old, new = mk(0, sym="OLD"), mk(5, sym="NEW")
        r = sel([old, new], {"confirmed": False, "side": "long"}, "long")
        self.assertEqual([h["case"]["situation"]["sym"] for h in r["hits"]], ["NEW", "OLD"])

    def test_k_limits_and_cap(self):
        lst = [mk(i, sym="S%d" % i) for i in range(30)]
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long", k=5)["hits"]), 5)
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long", k=500)["hits"]), 20)      # k_max
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long")["hits"]), 8)

    def test_side_filter(self):
        lst = [mk(0, side="long"), mk(1, side="short")]
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long")["hits"]), 1)
        self.assertEqual(len(sel(lst, {"confirmed": False}, None)["hits"]), 2)
        self.assertEqual(len(sel(lst, {"confirmed": False}, "watch")["hits"]), 2)

    def test_mode_scope(self):
        lst = [mk(0, mode="live"), mk(1, mode="paper"), mk(2, mode="testnet")]
        f = {"confirmed": False}
        self.assertEqual(len(sel(lst, f, "long")["hits"]), 1)
        self.assertEqual(len(sel(lst, f, "long", mode="paper")["hits"]), 1)
        self.assertEqual(len(sel(lst, f, "long", scope="all")["hits"]), 3)

    def test_rules_ver_filter(self):
        lst = [mk(0, rules_ver="2026-10-27"), mk(1, rules_ver="2026-01-01")]
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long")["hits"]), 1)
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long", rules="any")["hits"]), 2)

    def test_rules_previous_allowed(self):
        cfg = dict(CFG, rules_ver="2026-11-01", rules_history=["2026-10-27", "2026-11-01"])
        r = similar.select(fold([mk(0, rules_ver="2026-10-27"), mk(1, rules_ver="2026-09-01")]), {"features": {"confirmed": False}, "side": "long"}, cfg, NOW)
        self.assertEqual(len(r["hits"]), 1)

    def test_type_filter_default_excludes_signals(self):
        lst = [mk(0), mk(1, type="signal"), mk(2, type="idea"), mk(3, type="skip")]
        types = sorted(h["case"]["type"] for h in sel(lst, {"confirmed": False}, "long")["hits"])
        self.assertEqual(types, ["idea", "skip", "trade"])
        self.assertEqual(len(sel(lst, {"confirmed": False}, "long", types=("signal",))["hits"]), 1)

    def test_excluded_flags_dropped(self):
        lst = [mk(0, exclude=True), mk(1, flags=["dup_of_trade"]), mk(2, flags=["inj_suspect"]), mk(3)]
        r = sel(lst, {"confirmed": False}, "long")
        self.assertEqual(len(r["hits"]), 1)
        self.assertEqual(sel(lst, {"confirmed": False}, "long", include_excluded=True)["hits"].__len__(), 2)

    def test_age_filter(self):
        old = mk(0, ts="2026-06-01T10:00:00Z")
        self.assertEqual(sel([old], {"confirmed": False}, "long")["hits"], [])
        self.assertEqual(len(sel([old], {"confirmed": False}, "long", max_age_days=0)["hits"]), 1)

    def test_as_of_blocks_future(self):
        lst = [mk(0), mk(10)]
        cut = similar.ts_epoch(ts_at(5))
        r = sel(lst, {"confirmed": False}, "long", as_of=cut)
        self.assertEqual(len(r["hits"]), 1)
        self.assertEqual(r["filtered"].get("as_of"), 1)

    def test_min_sim_and_coverage(self):
        lst = [mk(0, regime="trend", setup="range_fade", confirmed=True, stop_pct=0.3, range_5m_pct=9.0)]
        r = sel(lst, {"confirmed": False, "regime": "range", "setup": "level_reclaim", "stop_pct": 2.2, "range_5m_pct": 1.8}, "long")
        self.assertEqual(r["hits"], [])
        self.assertTrue(r["filtered"].get("min_sim"))
        # покрытие: у случая известна только сторона
        c = mk(1)
        c["situation"] = {"sym": "DOOD", "side": "long"}
        r2 = sel([c], {"confirmed": False, "regime": "range", "stop_pct": 2.2}, "long", min_sim=0.0)
        self.assertEqual(r2["hits"], [])
        self.assertTrue(r2["filtered"].get("coverage"))

    def test_report_fields(self):
        r = sel(synthetic(20), {"confirmed": False, "setup": "level_reclaim"}, "long")
        for k in ("hits", "considered", "filtered", "k", "min_sim_used", "avg_sim", "coverage"):
            self.assertIn(k, r)

    def test_compact(self):
        h = sel([mk(0, err=["no-confirm"])], {"confirmed": False}, "long")["hits"][0]
        c = similar.compact(h)
        self.assertEqual(set(c), {"case_id", "sim", "ts", "side", "setup", "confirmed", "r", "err", "exit", "why"})
        self.assertEqual(c["err"], ["no-confirm"])

    def test_speed_5000(self):
        base = synthetic(40)
        big = []
        for i in range(5000):
            c = copy.deepcopy(base[i % 40])
            c["case_id"] = "C-20261020-SYM0-%06d" % i
            c["ts"] = ts_at(i % 200)
            big.append(c)
        t0 = time.time()
        r = similar.select(big, {"features": {"confirmed": False, "setup": "level_reclaim", "range_5m_pct": 1.6}, "side": "long", "mode": "live"}, CFG, NOW)
        dt = time.time() - t0
        self.assertTrue(r["hits"])
        self.assertLess(dt, 1.0)        # цель из ТЗ: чтение индекса < 100 мс на быстрой машине; в тесте запас на медленные CI


if __name__ == "__main__":
    unittest.main()
