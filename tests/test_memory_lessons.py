"""WP15a: уроки-гипотезы: условия, шаблоны, создание, подтверждение, опровержение, затухание, противоречия, слияние, вытеснение, лимиты."""
import json
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import NOW, TmpRoot, mk
from tb.memory import cases, chain, lessons, similar

NC = [{"feature": "confirmed", "op": "=", "value": False}]
CL = {"metric": "mean_r", "dir": "lower", "vs": "base", "effect": 1.0}


def day_ts(i, base_day=12, span=8):
    return "2026-10-%02dT10:%02d:00Z" % (base_day + i % span, i % 60)


def make_pool(n_bad=12, n_good=18, bad_r=-1.0, good_r=0.6, start=0, regimes=("range", "trend"), **kw):
    out = []
    for i in range(n_bad):
        out.append(mk(start + i, sym="B%d" % i, r=round(bad_r - (i % 3) * 0.1, 2), confirmed=False, regime=regimes[i % len(regimes)], ts=day_ts(i), err=["no-confirm"], **kw))
    for i in range(n_good):
        out.append(mk(start + 100 + i, sym="G%d" % i, r=round(good_r + (i % 3) * 0.1, 2), confirmed=True, regime=regimes[i % len(regimes)], ts=day_ts(i + 3), **kw))
    return out


class LessonBase(TmpRoot):
    def load(self):
        return cases.load(self.root)

    def mklesson(self, cond=None, claim=None, by="code", now=NOW, **kw):
        return lessons.create(self.root, cond or NC, claim or CL, by, self.load(), self.cfg, now, **kw)


class TestCond(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(lessons.validate_cond(NC), [])
        self.assertEqual(lessons.validate_cond([{"feature": "coin_class", "op": "in", "value": ["meme", "alt_small"]}, {"feature": "range_5m_pct", "op": ">=", "value": 1.5}]), [])

    def test_rejects(self):
        bad = [[], NC * 4, [{"feature": "sym", "op": "=", "value": "DOOD"}], [{"feature": "confirmed", "op": ">=", "value": True}],
               [{"feature": "confirmed", "op": "=", "value": "no"}], [{"feature": "coin_class", "op": "=", "value": "dragon"}],
               [{"feature": "range_5m_pct", "op": ">=", "value": "big"}], [{"feature": "range_5m_pct", "op": "~", "value": 1}],
               [{"feature": "regime", "op": "in", "value": "range"}], [{"feature": "confirmed", "op": "=", "value": True, "extra": 1}],
               NC + NC[:1]]
        for b in bad:
            self.assertTrue(lessons.validate_cond(b), b)

    def test_claim(self):
        self.assertEqual(lessons.validate_claim(CL), [])
        self.assertTrue(lessons.validate_claim(dict(CL, metric="vibes")))
        self.assertTrue(lessons.validate_claim(dict(CL, metric="win_rate", vs="zero")))
        self.assertTrue(lessons.validate_claim(dict(CL, effect=-1)))
        self.assertTrue(lessons.validate_claim({"metric": "mean_r"}))

    def test_matches_ops(self):
        c = mk(0, confirmed=False, range_5m_pct=2.0, coin_class="meme")
        self.assertTrue(lessons.matches(NC, c))
        self.assertTrue(lessons.matches([{"feature": "range_5m_pct", "op": ">=", "value": 1.5}], c))
        self.assertFalse(lessons.matches([{"feature": "range_5m_pct", "op": "<=", "value": 1.5}], c))
        self.assertTrue(lessons.matches([{"feature": "coin_class", "op": "in", "value": ["meme", "major"]}], c))
        self.assertFalse(lessons.matches([{"feature": "fng", "op": ">=", "value": 10}], c) if False else lessons.matches([{"feature": "funding_pct", "op": ">=", "value": 0}], c))

    def test_unknown_value_does_not_match(self):
        c = mk(0)
        del c["situation"]["confirmed"]
        self.assertFalse(lessons.matches(NC, c))

    def test_overlap(self):
        a = [{"feature": "range_5m_pct", "op": ">=", "value": 2.0}]
        self.assertTrue(lessons.overlap(a, [{"feature": "range_5m_pct", "op": ">=", "value": 1.0}]))
        self.assertFalse(lessons.overlap(a, [{"feature": "range_5m_pct", "op": "<=", "value": 1.0}]))
        self.assertTrue(lessons.overlap(NC, [{"feature": "coin_class", "op": "=", "value": "meme"}]))          # разные признаки не мешают
        self.assertFalse(lessons.overlap(NC, [{"feature": "confirmed", "op": "=", "value": True}]))
        self.assertTrue(lessons.overlap([{"feature": "regime", "op": "in", "value": ["range", "trend"]}], [{"feature": "regime", "op": "=", "value": "trend"}]))

    def test_jaccard(self):
        self.assertEqual(lessons.jaccard(NC, NC), 1.0)
        self.assertEqual(lessons.jaccard(NC, [{"feature": "blackout", "op": "=", "value": True}]), 0.0)
        two = NC + [{"feature": "blackout", "op": "=", "value": True}]
        self.assertEqual(lessons.jaccard(NC, two), 0.5)

    def test_txt_is_template(self):
        l = {"cond": NC, "claim": CL, "status": "hyp", "evidence": {"n_closed": 4}}
        t = lessons.render_txt(l)
        self.assertEqual(t, "нет подтверждения -> средний R ниже базового (4 случаев, гипотеза)")
        self.assertLessEqual(len(t), 140)


class TestCreateAndStore(LessonBase):
    def test_create_computes_evidence(self):
        self.put(make_pool())
        l = self.mklesson()
        self.assertEqual((l["lesson_id"], l["status"], l["rev"]), ("L-0001", "hyp", 1))
        ev = l["evidence"]
        self.assertEqual(ev["n_closed"], 12)
        self.assertLess(ev["mean_r"], -0.9)
        self.assertGreater(ev["base_r"], ev["mean_r"])
        self.assertEqual(len(ev["cases"]), 12)
        self.assertLessEqual(l["conf"], 0.9)
        self.assertEqual(l["txt"], lessons.render_txt(l))
        self.assertEqual(l["history"][0]["reason_code"], "NEW")

    def test_ids_increment_and_persist(self):
        self.put(make_pool())
        self.mklesson()
        l2 = self.mklesson([{"feature": "blackout", "op": "=", "value": True}])
        self.assertEqual(l2["lesson_id"], "L-0002")
        self.assertEqual(sorted(lessons.read_all(self.root)), ["L-0001", "L-0002"])
        self.assertTrue(chain.verify(lessons.lessons_path(self.root))["ok"])

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            self.mklesson([{"feature": "sym", "op": "=", "value": "X"}])
        with self.assertRaises(ValueError):
            self.mklesson(by="gpt")

    def test_no_free_text_in_record(self):
        self.put(make_pool())
        l = self.mklesson()
        self.assertEqual(set(l["claim"]), {"metric", "dir", "vs", "effect"})
        self.assertEqual(set(l["cond"][0]), {"feature", "op", "value"})

    def test_pool_excludes_mem_off_excluded_old_rules(self):
        bad = make_pool(n_bad=6, n_good=0)
        extra = [mk(200, sym="X1", r=5.0, confirmed=False, mem="off"), mk(201, sym="X2", r=5.0, confirmed=False, exclude=True),
                 mk(202, sym="X3", r=5.0, confirmed=False, rules_ver="2025-01-01"), mk(203, sym="X4", r=5.0, confirmed=False, role="mechanics")]
        self.put(bad + extra)
        self.assertEqual(self.mklesson()["evidence"]["n_closed"], 6)

    def test_reviewer_without_support_capped(self):
        self.put(make_pool(n_bad=2, n_good=3))
        l = self.mklesson(by="reviewer")
        self.assertLessEqual(l["conf"], 0.3)
        self.assertEqual(l["status"], "hyp")

    def test_owner_confirm_reject_expire(self):
        self.put(make_pool())
        a, b, c = self.mklesson(), self.mklesson([{"feature": "blackout", "op": "=", "value": True}]), self.mklesson([{"feature": "side", "op": "=", "value": "long"}])
        self.assertEqual(lessons.owner_set(self.root, a["lesson_id"], "confirm", self.cfg, NOW)["status"], "confirmed")
        self.assertEqual(lessons.owner_set(self.root, b["lesson_id"], "reject", self.cfg, NOW)["status"], "rejected")
        self.assertEqual(lessons.owner_set(self.root, c["lesson_id"], "expire", self.cfg, NOW)["status"], "expired")
        st = lessons.read_all(self.root)
        self.assertEqual(st["L-0001"]["history"][-1]["reason_code"], "OWNER")
        self.assertEqual(st["L-0001"]["rev"], 2)
        with self.assertRaises(ValueError):
            lessons.owner_set(self.root, "L-0002", "confirm", self.cfg, NOW)         # отвергнутый подтвердить нельзя
        with self.assertRaises(ValueError):
            lessons.owner_set(self.root, "L-9999", "reject", self.cfg, NOW)

    def test_confirmed_does_not_touch_guard(self):
        import os
        before = os.path.getmtime(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "risk_profile.json"))
        self.put(make_pool())
        l = self.mklesson()
        lessons.owner_set(self.root, l["lesson_id"], "confirm", self.cfg, NOW)
        self.assertEqual(before, os.path.getmtime(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "risk_profile.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "config", "risk_profile.json")))


class TestRefresh(LessonBase):
    def test_confirm_requires_n_days_regimes(self):
        self.put(make_pool(n_bad=30, n_good=20))
        l = self.mklesson()
        ch = lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "confirmed")
        self.assertEqual(ch[0]["reason"], "ENOUGH_N")

    def test_no_confirm_when_single_day(self):
        pool = [mk(i, sym="B%d" % i, r=-1.0 - (i % 3) * 0.1, confirmed=False, regime="range" if i % 2 else "trend", ts="2026-10-20T10:%02d:00Z" % i) for i in range(30)]
        pool += [mk(100 + i, sym="G%d" % i, r=0.6, confirmed=True, ts="2026-10-20T11:%02d:00Z" % i) for i in range(10)]
        self.put(pool)
        l = self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "hyp")

    def test_no_confirm_single_regime(self):
        self.put(make_pool(n_bad=30, n_good=20, regimes=("range",)))
        l = self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "hyp")

    def test_no_confirm_small_n(self):
        self.put(make_pool(n_bad=12, n_good=18))
        l = self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "hyp")

    def test_fast_route_n15_strong_effect(self):
        pool = make_pool(n_bad=16, n_good=30)
        self.put(pool)
        l = self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "confirmed")

    def test_refresh_idempotent(self):
        self.put(make_pool(n_bad=30, n_good=20))
        self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.refresh(self.root, self.load(), self.cfg, NOW), [])

    def test_dry_run_writes_nothing(self):
        self.put(make_pool(n_bad=30, n_good=20))
        self.mklesson()
        n = len(chain.read(lessons.lessons_path(self.root))[0])
        self.assertTrue(lessons.refresh(self.root, self.load(), self.cfg, NOW, dry_run=True))
        self.assertEqual(len(chain.read(lessons.lessons_path(self.root))[0]), n)

    def test_reject_when_new_cases_contradict(self):
        self.put(make_pool(n_bad=6, n_good=10))
        l = self.mklesson(now=similar.ts_epoch("2026-10-20T00:00:00Z"))
        late = [mk(300 + i, sym="N%d" % i, r=1.5, confirmed=False, ts="2026-10-%02dT12:00:00Z" % (22 + i % 5)) for i in range(12)]
        self.put(late)
        ch = lessons.refresh(self.root, self.load(), self.cfg, NOW)
        st = lessons.read_all(self.root)[l["lesson_id"]]
        self.assertEqual(st["status"], "rejected")
        self.assertEqual(ch[0]["reason"], "CONTRADICTED")

    def test_expire_after_ttl_without_support(self):
        self.put(make_pool(n_bad=6, n_good=6))
        created = similar.ts_epoch("2026-10-21T00:00:00Z")
        l = self.mklesson(now=created)
        ch = lessons.refresh(self.root, self.load(), self.cfg, created + 40 * 86400)
        self.assertEqual(lessons.read_all(self.root)[l["lesson_id"]]["status"], "expired")
        self.assertEqual(ch[0]["reason"], "EXPIRED")

    def test_new_supporting_case_extends_ttl(self):
        self.put(make_pool(n_bad=6, n_good=6))
        created = similar.ts_epoch("2026-10-21T00:00:00Z")
        l = self.mklesson(now=created)
        self.put([mk(500, sym="NEW", r=-1.0, confirmed=False, ts="2026-11-20T10:00:00Z")])
        now = created + 40 * 86400
        lessons.refresh(self.root, self.load(), self.cfg, now)
        st = lessons.read_all(self.root)[l["lesson_id"]]
        self.assertEqual(st["status"], "hyp")
        self.assertGreater(st["expires_ts"], now)

    def test_old_rules_lesson_shortens_ttl(self):
        self.put(make_pool(n_bad=6, n_good=6))
        l = self.mklesson()
        st = lessons.read_all(self.root)[l["lesson_id"]]
        lessons._update(self.root, st, NOW, rules_ver="2025-01-01")
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertLessEqual(lessons.read_all(self.root)[l["lesson_id"]]["expires_ts"], NOW + 14 * 86400 + 1)

    def test_contradiction_between_active_lessons(self):
        self.put(make_pool())
        a = self.mklesson(NC, CL)
        b = self.mklesson([{"feature": "side", "op": "=", "value": "long"}], dict(CL, dir="higher"), by="reviewer")
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        st = lessons.read_all(self.root)
        self.assertEqual(st[a["lesson_id"]]["contradicts"], [b["lesson_id"]])
        self.assertEqual(st[b["lesson_id"]]["contradicts"], [a["lesson_id"]])
        self.assertLess(lessons.conf_eff(st[a["lesson_id"]], NOW, self.cfg), st[a["lesson_id"]]["conf"] * 0.61)

    def test_non_overlapping_opposite_not_contradiction(self):
        self.put(make_pool())
        a = self.mklesson(NC, CL)
        b = self.mklesson([{"feature": "confirmed", "op": "=", "value": True}], dict(CL, dir="higher"))
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertEqual(lessons.read_all(self.root)[a["lesson_id"]]["contradicts"], [])
        self.assertEqual(lessons.read_all(self.root)[b["lesson_id"]]["contradicts"], [])

    def test_hyp_cannot_confirm_against_confirmed_contradiction(self):
        self.put(make_pool(n_bad=30, n_good=20))
        a = self.mklesson(NC, CL)
        lessons.owner_set(self.root, a["lesson_id"], "confirm", self.cfg, NOW)
        b = self.mklesson([{"feature": "range_5m_pct", "op": ">=", "value": 0.1}], dict(CL, dir="higher"))
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        self.assertNotEqual(lessons.read_all(self.root)[b["lesson_id"]]["status"], "confirmed")

    def test_merge_duplicates(self):
        self.put(make_pool())
        a = self.mklesson()
        b = self.mklesson()
        lessons.refresh(self.root, self.load(), self.cfg, NOW)
        st = lessons.read_all(self.root)
        self.assertEqual(st[a["lesson_id"]]["status"], "hyp")
        self.assertEqual((st[b["lesson_id"]]["status"], st[b["lesson_id"]]["merged_into"]), ("merged", a["lesson_id"]))

    def test_eviction_over_limit(self):
        self.put(make_pool())
        cfg = json.loads(json.dumps(self.cfg))
        cfg["lessons"]["max_active"] = 2
        conds = [[{"feature": "range_5m_pct", "op": ">=", "value": v}] for v in (0.1, 0.5, 0.9)]
        for c in conds:
            lessons.create(self.root, c, CL, "owner", self.load(), cfg, NOW)
        lessons.refresh(self.root, self.load(), cfg, NOW)
        st = lessons.read_all(self.root)
        self.assertEqual(sum(1 for l in st.values() if l["status"] in lessons.ACTIVE), 2)


class TestDecayAndBrief(LessonBase):
    def test_decay_half_life(self):
        self.put(make_pool())
        l = self.mklesson()
        last = similar.ts_epoch(l["evidence"]["last_ts"])
        self.assertAlmostEqual(lessons.conf_eff(l, last, self.cfg), l["conf"], places=3)
        self.assertAlmostEqual(lessons.conf_eff(l, last + 21 * 86400, self.cfg), l["conf"] / 2, places=2)
        self.assertAlmostEqual(lessons.conf_eff(l, last + 42 * 86400, self.cfg), l["conf"] / 4, places=2)

    def test_applicable_hides_decayed_and_inactive(self):
        self.put(make_pool())
        l = self.mklesson()
        feats = {"confirmed": False}
        last = similar.ts_epoch(l["evidence"]["last_ts"])
        self.assertEqual([x["id"] for x in lessons.applicable([l], feats, last, self.cfg)], ["L-0001"])
        self.assertEqual(lessons.applicable([l], feats, last + 200 * 86400, self.cfg), [])
        self.assertEqual(lessons.applicable([l], {"confirmed": True}, last, self.cfg), [])
        self.assertEqual(lessons.applicable([dict(l, status="rejected")], feats, last, self.cfg), [])

    def test_applicable_limit_and_order(self):
        self.put(make_pool())
        ls = [dict(self.mklesson([{"feature": "range_5m_pct", "op": ">=", "value": v}]), conf=0.6) for v in (0.1, 0.2, 0.3, 0.4, 0.5)]
        ls[2] = dict(ls[2], conf=0.95)
        out = lessons.applicable(ls, {"range_5m_pct": 1.8}, similar.ts_epoch(ls[0]["evidence"]["last_ts"]), self.cfg)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["id"], ls[2]["lesson_id"])
        self.assertEqual(set(out[0]), {"id", "st", "n", "conf", "txt", "contradicts"})


class TestPropose(LessonBase):
    def test_finds_no_confirm_deviation(self):
        self.put(make_pool())
        c = lessons.propose(self.root, self.load(), self.cfg, NOW)
        self.assertTrue(c)
        top = c[0]
        self.assertEqual(top["claim"]["dir"], "lower")
        self.assertIn(top["cond"], ([{"feature": "confirmed", "op": "=", "value": False}],))
        self.assertGreaterEqual(abs(top["diff"]), 0.5)
        self.assertGreaterEqual(top["share"], 0.75)

    def test_needs_n4_and_diff(self):
        self.put(make_pool(n_bad=3, n_good=10))
        self.assertEqual([c for c in lessons.propose(self.root, self.load(), self.cfg, NOW) if c["cond"] == NC], [])
        self.put(make_pool(n_bad=3, n_good=3, bad_r=0.5, good_r=0.5, start=1000))

    def test_no_deviation_no_candidates(self):
        pool = [mk(i, sym="A%d" % i, r=0.3, confirmed=bool(i % 2), ts=day_ts(i)) for i in range(20)]
        self.put(pool)
        self.assertEqual(lessons.propose(self.root, self.load(), self.cfg, NOW), [])

    def test_existing_condition_not_proposed_again(self):
        self.put(make_pool())
        self.mklesson()
        for c in lessons.propose(self.root, self.load(), self.cfg, NOW):
            self.assertNotEqual(c["cond"], NC)

    def test_daily_cap(self):
        self.put(make_pool())
        cfg = json.loads(json.dumps(self.cfg))
        cfg["lessons"]["max_new_per_day"] = 1
        self.assertLessEqual(len(lessons.propose(self.root, self.load(), cfg, NOW)), 1)
        lessons.create(self.root, [{"feature": "blackout", "op": "=", "value": True}], CL, "code", self.load(), cfg, NOW)
        self.assertEqual(lessons.propose(self.root, self.load(), cfg, NOW), [])

    def test_excluded_cases_do_not_raise_lessons(self):
        self.put([mk(i, sym="A%d" % i, r=-3.0, confirmed=False, exclude=True, ts=day_ts(i)) for i in range(10)]
                 + [mk(50 + i, sym="G%d" % i, r=0.5, confirmed=True, ts=day_ts(i)) for i in range(10)])
        self.assertEqual(lessons.propose(self.root, self.load(), self.cfg, NOW), [])

    def test_from_hint(self):
        self.put(make_pool())
        l = lessons.from_hint({"code": "L-NO_CONFIRM", "cond": "="}, self.root, self.load(), self.cfg, NOW)
        self.assertEqual(l["created_by"], "reviewer")
        self.assertEqual(l["cond"], NC)
        self.assertIsNone(lessons.from_hint({"code": "L-NO_CONFIRM", "cond": "="}, self.root, self.load(), self.cfg, NOW))          # дубль
        self.assertIsNone(lessons.from_hint({"code": "L-NOPE", "cond": "="}, self.root, self.load(), self.cfg, NOW))
        self.assertIsNone(lessons.from_hint({"code": "L-BLACKOUT", "cond": "="}, self.root, self.load(), self.cfg, NOW))              # нет данных

    def test_from_hint_needs_stats(self):
        self.put(make_pool(n_bad=2, n_good=10))
        self.assertIsNone(lessons.from_hint({"code": "L-NO_CONFIRM", "cond": "="}, self.root, self.load(), self.cfg, NOW))


if __name__ == "__main__":
    unittest.main()
