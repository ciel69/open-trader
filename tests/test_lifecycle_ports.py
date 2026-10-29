"""WP14: боевые реализации портов (FileJobBrain, FileExecPort) на временных файлах: контракт заданий мозга и очередь команд плана."""
import json
import os
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb import paths
from tb.core import plan_store
from tb.lifecycle import ports


class TestFileJobBrain(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self, real_lifecycle=True)
        self.b = ports.FileJobBrain(self.e.root)

    def test_submit_writes_job_file_with_L_prefix(self):
        jid = self.b.submit("day_auditor", {"kind": "audit", "items": ["pos:PL-1-SOL-01"]}, 90, 1793106000.0, meta={"day_id": "D-20261027-1"})
        self.assertTrue(jid.startswith("L-"))
        p = os.path.join(paths.state("brain", "jobs", root=self.e.root), jid + ".json")
        d = json.load(open(p, encoding="utf-8"))
        self.assertEqual((d["schema"], d["kind"], d["role"], d["deadline_ts"]), ("tb.brainjob/1", "lifecycle", "day_auditor", 1793106090.0))
        self.assertEqual(d["payload"]["items"], ["pos:PL-1-SOL-01"])
        self.assertNotIn("status", d)                                         # не задание диспетчера: раннер дневных заданий его не берёт

    def test_poll_reads_result_file(self):
        jid = self.b.submit("morning_analyst", {}, 150, 1793106000.0)
        self.assertIsNone(self.b.poll(jid, 1793106001.0))
        self.e.w("state/brain/jobs/%s.result.json" % jid, {"id": jid, "ok": True, "output": {"schema": "tb.dayplan/1"}, "err": None})
        r = self.b.poll(jid, 1793106002.0)
        self.assertEqual((r["ok"], r["output"]["schema"]), (True, "tb.dayplan/1"))
        self.e.w("state/brain/jobs/%s.result.json" % jid, {"id": jid, "ok": False, "output": "строка", "err": "TIMEOUT"})
        r = self.b.poll(jid, 1793106002.0)
        self.assertEqual((r["ok"], r["output"], r["err"]), (False, None, "TIMEOUT"))

    def test_poll_rejects_path_tricks(self):
        self.assertEqual(self.b.poll("../../etc/passwd", 0)["err"], "BAD_JOB")

    def test_cancel_removes_job(self):
        jid = self.b.submit("day_auditor", {}, 90, 1793106000.0)
        self.b.cancel(jid)
        self.assertFalse(os.path.exists(os.path.join(paths.state("brain", "jobs", root=self.e.root), jid + ".json")))

    def test_null_brain(self):
        n = ports.NullBrain()
        self.assertFalse(n.available("/x", 0)[0])
        with self.assertRaises(RuntimeError):
            n.submit("x", {}, 1, 0)

    def test_dispatcher_listing_ignores_lifecycle_files(self):
        from tb.brain import jobs
        self.b.submit("day_auditor", {}, 90, 1793106000.0)
        self.assertEqual(jobs.list_jobs(self.e.root), [])


class TestFileExecPort(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self, real_lifecycle=True)
        self.x = ports.FileExecPort(self.e.root)

    def test_send_goes_to_plan_inbox_with_system_actor(self):
        cid = self.x.send("close", "PL-1-SOL-01", "eod", {"frac": 1.0}, now=1793106000.0, tag="c6.1")
        cmd = json.load(open(paths.plans_inbox(cid + ".json", root=self.e.root), encoding="utf-8"))
        self.assertEqual((cmd["actor"], cmd["op"], cmd["plan_id"], cmd["payload"]), ("system", "close", "PL-1-SOL-01", {"frac": 1.0}))
        from tb.core import decisions
        self.assertTrue(decisions.valid_actor(cmd["actor"]))

    def test_send_is_idempotent_per_tag_and_second(self):
        a = self.x.send("cancel", "PL-1-SOL-01", "r", now=1793106000.0, tag="c1")
        b = self.x.send("cancel", "PL-1-SOL-01", "r", now=1793106000.4, tag="c1")
        self.assertEqual(a, b)
        c = self.x.send("cancel", "PL-1-SOL-01", "r", now=1793106030.0, tag="c1")
        self.assertNotEqual(a, c)

    def test_adopt_has_no_plan_id(self):
        cid = self.x.send("adopt", None, "r", {"symbol": "BTC"}, now=1793106000.0, tag="adopt-BTC")
        cmd = json.load(open(paths.plans_inbox(cid + ".json", root=self.e.root), encoding="utf-8"))
        self.assertNotIn("plan_id", cmd)
        self.assertEqual(cmd["payload"], {"symbol": "BTC"})

    def test_result_reads_outbox(self):
        cid = self.x.send("close", "PL-1-SOL-01", "r", now=1793106000.0)
        self.assertIsNone(self.x.result(cid))
        plan_store.PlanStore(self.e.root).inbox_done(cid, {"ok": True, "status": "managing"})
        self.assertTrue(self.x.result(cid)["ok"])

    def test_snapshot_reading(self):
        self.assertFalse(self.x.snapshot(100.0)["present"])
        self.e.w("runtime/exec_snapshot.json", {"ts_epoch": 90.0, "account": {"equity": 31.5, "free": 20.0},
                                                "positions": [{"sym": "SOL", "side": "long", "size": 1.0, "owner": "PL-1-SOL-01"}, "мусор"], "orders": []})
        s = self.x.snapshot(100.0)
        self.assertEqual((s["present"], s["age_s"], s["equity"], len(s["positions"])), (True, 10.0, 31.5, 1))

    def test_plans_lists_only_active(self):
        st = plan_store.PlanStore(self.e.root)
        spec = {"schema": "tb.plan/1", "symbol": "SOL", "side": "long", "mode": "live", "session_type": "swing", "id": None, "rev": 1}
        pid = st.create(dict(spec), {"status": "managing", "pos": {}})
        pid2 = st.create(dict(spec), {"status": "closed"})
        got = self.x.plans()
        self.assertEqual([p["id"] for p in got], [pid])
        self.assertEqual((got[0]["stype"], got[0]["sym"], got[0]["status"]), ("swing", "SOL", "managing"))


if __name__ == "__main__":
    unittest.main()
