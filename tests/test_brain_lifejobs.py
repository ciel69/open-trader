"""Шов WP14 <-> WP11-C2: рантайм мозга отвечает на задания жизненного цикла L-*.json (ФЕЙКОВЫЙ LLM, временный корень)."""
import os
import unittest
import _bootstrap  # noqa: F401
from _brain import BrainBase
from test_roles_c3 import good_dayplan
from tb.brain import lifejobs
from tb.lifecycle.ports import FileJobBrain

BRIEF = {"brief_id": "B-1", "scope": "start", "levels": {"SOL": {"px": 145.2}, "ETH": {"px": 2510.0}}, "market": {"btc": {"px": 67000.0}}, "plans": [], "positions": []}
PAYLOAD = {"schema": "tb.lifecycle.job/1", "kind": "morning", "day_id": "D-20261028-1", "mode": "paper", "brief": BRIEF, "facts": None,
           "owner_choice": {"session": "intraday", "minutes": 360}, "caps": {"max_risk_usdt": 0.9, "size_mult": 1.0, "max_minutes": 720}, "symbols": ["SOL", "ETH"], "carried_in": []}


class LifeJobs(BrainBase):
    def test_morning_roundtrip(self):
        r = self.runner({"ds-pro": [good_dayplan()]})
        fb = FileJobBrain(self.root)
        jid = fb.submit("morning_analyst", PAYLOAD, 60, r.clock(), meta={"day_id": "D-20261028-1"})
        self.assertTrue(jid.startswith("L-"))
        self.assertIsNone(fb.poll(jid, r.clock()))
        self.assertTrue(lifejobs.step(r))
        res = fb.poll(jid, r.clock())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["output"]["schema"], "tb.dayplan/1")
        self.assertFalse(lifejobs.step(r))                        # повторно не берётся
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "brain", "jobs", jid + ".claim")))

    def test_semantic_reject_gives_not_ok(self):
        r = self.runner({"ds-pro": [good_dayplan(watch=[{"sym": "DOGE", "side": "long", "level": 1.0, "setup": "level_reclaim", "note": "x"}])]})
        fb = FileJobBrain(self.root)
        jid = fb.submit("morning_analyst", PAYLOAD, 60, r.clock())
        lifejobs.step(r)
        res = fb.poll(jid, r.clock())
        self.assertFalse(res["ok"])
        self.assertIsNone(res["output"])

    def test_audit_and_bad_role_and_expired(self):
        aud = {"schema": "tb.audit/1", "day_id": "D-1", "carry": [], "changes": [], "owner_questions": [], "narrative": "ок"}
        r = self.runner({"ds-flash": [aud], "ds-pro": [aud]})
        fb = FileJobBrain(self.root)
        j1 = fb.submit("day_auditor", {"kind": "audit", "day_id": "D-1", "items": [], "rows": []}, 60, r.clock())
        j2 = fb.submit("hacker", {}, 60, r.clock() + 1)
        j3 = fb.submit("day_auditor", {"day_id": "D-1"}, 1, r.clock() - 100, meta={"x": 3})
        while lifejobs.step(r):
            pass
        self.assertTrue(fb.poll(j1, r.clock())["ok"])
        self.assertEqual(fb.poll(j2, r.clock())["err"], "BAD_ROLE")
        self.assertEqual(fb.poll(j3, r.clock())["err"], "EXPIRED")

    def test_reset_claims(self):
        d = os.path.join(self.root, "state", "brain", "jobs")
        os.makedirs(d)
        open(os.path.join(d, "L-x.claim"), "w").close()
        self.assertEqual(lifejobs.reset_claims(self.root), 1)

    def test_ignores_dispatcher_jobs(self):
        r = self.runner({})
        self.job("J-1")
        self.assertEqual(lifejobs.pending(self.root), [])
        self.assertFalse(lifejobs.step(r))


class Seams(BrainBase):
    def test_brief_has_limits_risk_level_paper_history(self):
        from tb.brain import brief
        b = brief.build("start", root=self.root)
        self.assertEqual(b["paper_history"], {"closed": 0, "days": 0})
        self.assertIn("level", b["risk_level"])
        self.assertIn("carry", b["risk_level"])
        self.assertIn("size_mult", b["limits"])

    def test_brain_down_emits_limit_hit_and_fallback_swap(self):
        from tb.core import bus
        r = self.runner({})
        r.enter_protect("тест", "brain_down")
        evs = [(e["type"], e["d"]) for e in bus.read(0, limit=50, root=self.root)]
        self.assertIn(("limit_hit", {"which": "brain_down"}), evs)
        self.assertIn(("error", {"where": "brain", "code": "BRAIN_DOWN"}), evs)
        self.assertIn("brain_swap", bus.EVENT_TYPES)
        self.assertIn("brain_down", bus.LIMIT_WHICH)


if __name__ == "__main__":
    unittest.main()
