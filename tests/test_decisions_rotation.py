"""Ротация журнала решений: сегменты, непрерывная хеш-цепочка, чтение/последние/verify (полный и быстрый), подмена закрытого сегмента, параллельные писатели."""
import os
import shutil
import tempfile
import threading
import unittest

import _bootstrap  # noqa: F401
from tb.core import decisions


class Rotation(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbdecrot_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self._max = decisions.MAX_ACTIVE_BYTES
        decisions.MAX_ACTIVE_BYTES = 4000                 # около 15 записей на сегмент
        self.addCleanup(setattr, decisions, "MAX_ACTIVE_BYTES", self._max)

    def put(self, n, plan="PL-1030-SOL-01"):
        for i in range(n):
            decisions.append({"actor": "exec", "kind": "note", "what": "x", "plan": plan, "why": "запись %d" % i}, root=self.root)

    def test_rotates_and_keeps_chain_and_seq(self):
        self.put(80)
        segs = decisions.segments(self.root)
        self.assertGreaterEqual(len(segs), 3)
        recs = decisions.read(0, limit=0, root=self.root)
        self.assertEqual([r["seq"] for r in recs], list(range(1, 81)))
        self.assertEqual(decisions.verify(self.root), {"ok": True, "n": 80, "bad_seq": None, "broken": 0})
        self.assertEqual(decisions.verify(self.root, fast=True), {"ok": True, "n": 80, "bad_seq": None, "broken": 0})
        self.assertLess(os.path.getsize(os.path.join(self.root, "state", "decisions.jsonl")), 4000 + 600)

    def test_last_spans_segments_and_filters_by_plan(self):
        self.put(30, plan="PL-1030-SOL-01")
        self.put(30, plan="PL-1030-ETH-02")
        last = decisions.last(40, root=self.root)
        self.assertEqual([r["seq"] for r in last], list(range(21, 61)))
        only = decisions.last(10, plan="PL-1030-SOL-01", root=self.root)
        self.assertEqual([r["seq"] for r in only], list(range(21, 31)))
        self.assertEqual(decisions.last(0, root=self.root), [])

    def test_restart_continues_from_last_segment_when_active_empty(self):
        self.put(40)
        active = os.path.join(self.root, "state", "decisions.jsonl")
        decisions._rotate(self.root, active)               # активный файл пуст: следующая запись обязана продолжить цепочку по хвосту сегмента
        self.assertFalse(os.path.exists(active))
        self.assertEqual(decisions.append({"actor": "exec", "kind": "note", "what": "y"}, root=self.root), 41)
        self.assertTrue(decisions.verify(self.root)["ok"])

    def test_tampered_sealed_segment_is_detected(self):
        self.put(60)
        seg = decisions.segments(self.root)[0]
        with open(seg, "rb") as f:
            data = f.read()
        with open(seg, "wb") as f:
            f.write(data.replace("запись 3".encode("utf-8"), "запись 9".encode("utf-8"), 1))
        deep = decisions.verify(self.root)
        fast = decisions.verify(self.root, fast=True)
        self.assertFalse(deep["ok"])
        self.assertFalse(fast["ok"])                      # размер тот же, но mtime изменился: сегмент проверен целиком
        self.assertEqual(deep["bad_seq"], fast["bad_seq"])

    def test_removed_segment_breaks_chain(self):
        self.put(60)
        os.remove(decisions.segments(self.root)[1])
        self.assertFalse(decisions.verify(self.root)["ok"])
        self.assertFalse(decisions.verify(self.root, fast=True)["ok"])

    def test_parallel_writers_across_rotations(self):
        def w():
            for _ in range(25):
                decisions.append({"actor": "exec", "kind": "note", "what": "t"}, root=self.root)
        ts = [threading.Thread(target=w) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        v = decisions.verify(self.root)
        self.assertEqual((v["ok"], v["n"]), (True, 100))
        self.assertEqual([r["seq"] for r in decisions.read(0, limit=0, root=self.root)], list(range(1, 101)))
        self.assertGreaterEqual(len(decisions.segments(self.root)), 2)


if __name__ == "__main__":
    unittest.main()
