"""Приоритетные ворота провайдера LLM и быстрая полоса воркера: срочное задание не ждёт за менее срочным."""
import os
import shutil
import tempfile
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from tb.brain import jobs
from tb.llm.gate import PriorityGate


class Gate(unittest.TestCase):
    def test_priority_order_when_slot_frees(self):
        g = PriorityGate(1)
        self.assertTrue(g.acquire(3))
        order = []

        def waiter(name, prio):
            if g.acquire(prio, timeout=5):
                order.append(name)
                g.release()
        ts = []
        for name, prio in (("entry_a", 3), ("entry_b", 3), ("position", 1)):
            t = threading.Thread(target=waiter, args=(name, prio))
            t.start()
            ts.append(t)
            time.sleep(0.05)               # порядок прихода: entry_a, entry_b, position
        self.assertEqual(g.snapshot(), {"capacity": 1, "busy": 1, "waiting": 3})
        g.release()
        [t.join(5) for t in ts]
        self.assertEqual(order, ["position", "entry_a", "entry_b"])

    def test_timeout_does_not_hold_slot_and_zero_capacity_is_free(self):
        g = PriorityGate(1)
        self.assertTrue(g.acquire(1))
        self.assertFalse(g.acquire(1, timeout=0.1))
        self.assertEqual(g.snapshot()["waiting"], 0)
        g.release()
        self.assertEqual(g.snapshot()["busy"], 0)
        free = PriorityGate(0)
        self.assertTrue(free.acquire(9) and free.acquire(9))
        free.release()

    def test_context_manager_releases_on_error(self):
        g = PriorityGate(1)
        with self.assertRaises(RuntimeError):
            with g.slot(2):
                raise RuntimeError("x")
        self.assertEqual(g.snapshot()["busy"], 0)


class FastLane(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tblane_")
        self.addCleanup(shutil.rmtree, self.root, True)
        jobs._TERM_CACHE.clear()

    def put(self, jid, prio):
        d = jobs.jobs_dir(self.root)
        os.makedirs(d, exist_ok=True)
        import json
        now = time.time()
        with open(os.path.join(d, jid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": jid, "sub": "api", "status": "new", "prio": prio, "key": "k" + jid, "ts_first": now, "ts_last": now, "not_before": 0, "seqs": [1], "types": ["x"]}, f)

    def test_fast_lane_skips_entry_search_jobs(self):
        self.put("J-20261030T000000-001", 3)           # поиск входа по сканеру
        self.assertIsNone(jobs.pick(self.root, time.time(), owner="t", max_prio=2))
        self.put("J-20261030T000000-002", 2)           # веха позиции
        j = jobs.pick(self.root, time.time(), owner="t", max_prio=2)
        self.assertEqual(j["id"], "J-20261030T000000-002")
        self.assertEqual(jobs.pick(self.root, time.time(), owner="t")["id"], "J-20261030T000000-001")      # обычный воркер берёт остальное


if __name__ == "__main__":
    unittest.main()
