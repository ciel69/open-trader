import json
import multiprocessing
import os
import tempfile
import threading
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import decisions


def _proc_append(args):
    root, n = args
    for i in range(n):
        decisions.append({"actor": "exec", "kind": "note", "what": "x", "args": {"i": i}}, root=root)


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbdec_")
        self.path = paths.decisions_file(self.root)

    def rec(self, **kw):
        r = {"actor": "exec", "kind": "note", "what": "w"}
        r.update(kw)
        return r

    def lines(self):
        with open(self.path, "rb") as f:
            return f.read().split(b"\n")

    def test_seq_and_chain(self):
        s = [decisions.append(self.rec(), root=self.root) for _ in range(5)]
        self.assertEqual(s, [1, 2, 3, 4, 5])
        rs = decisions.read(root=self.root)
        self.assertEqual([r["seq"] for r in rs], [1, 2, 3, 4, 5])
        self.assertEqual(rs[0]["prev"], decisions.GENESIS)
        for a, b in zip(rs, rs[1:]):
            self.assertEqual(b["prev"], a["h"])
        self.assertEqual(decisions.verify(root=self.root), {"ok": True, "n": 5, "bad_seq": None, "broken": 0})

    def test_verify_empty(self):
        self.assertTrue(decisions.verify(root=self.root)["ok"])
        self.assertEqual(decisions.read(root=self.root), [])

    def test_required_fields(self):
        decisions.append(self.rec(plan="PL-1-A-01", rev=2, why="RULE:be_1r", res={"ok": True, "code": "OK"}), root=self.root)
        r = decisions.read(root=self.root)[0]
        for k in decisions.REQUIRED_KEYS:
            self.assertIn(k, r)
        self.assertTrue(r["ts"].endswith("Z"))

    def test_empty_optionals_omitted(self):
        decisions.append(self.rec(args={}, why="", plan=None, basis=None), root=self.root)
        r = decisions.read(root=self.root)[0]
        for k in ("args", "why", "plan", "basis"):
            self.assertNotIn(k, r)

    def test_bad_actor_kind_what_unknown_key(self):
        for bad in (self.rec(actor="x"), self.rec(kind="x"), self.rec(what=""), self.rec(foo=1)):
            with self.assertRaises(ValueError):
                decisions.append(bad, root=self.root)
        self.assertFalse(os.path.exists(self.path))

    def test_why_sanitized_and_truncated(self):
        decisions.append(self.rec(why="a\nb\x00c\t" + "z" * 500), root=self.root)
        w = decisions.read(root=self.root)[0]["why"]
        self.assertLessEqual(len(w), 200)
        self.assertNotIn("\n", w)
        self.assertNotIn("\x00", w)
        self.assertEqual(len(self.lines()), 2)   # запись + пустой хвост: одна строка

    def test_caller_cannot_forge_seq_h(self):
        decisions.append(self.rec(seq=99, h="x", prev="y"), root=self.root)
        r = decisions.read(root=self.root)[0]
        self.assertEqual(r["seq"], 1)
        self.assertEqual(r["prev"], decisions.GENESIS)

    def test_read_filters(self):
        for i in range(6):
            decisions.append(self.rec(plan="PL-1-A-01" if i % 2 else "PL-2-B-01"), root=self.root)
        self.assertEqual([r["seq"] for r in decisions.read(after_seq=2, root=self.root)], [3, 4, 5, 6])
        self.assertEqual([r["seq"] for r in decisions.read(plan="PL-1-A-01", root=self.root)], [2, 4, 6])
        self.assertEqual(len(decisions.read(limit=2, root=self.root)), 2)
        self.assertEqual([r["seq"] for r in decisions.last(2, root=self.root)], [5, 6])

    def test_torn_last_line_skipped_and_recovered(self):
        decisions.append(self.rec(), root=self.root)
        decisions.append(self.rec(), root=self.root)
        with open(self.path, "ab") as f:
            f.write(b'{"seq":3,"ts":"2026-10-28T10:15:03Z","t":1.0,"ac')   # обрыв посреди записи
        self.assertEqual([r["seq"] for r in decisions.read(root=self.root)], [1, 2])
        v = decisions.verify(root=self.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["broken"], 1)
        s = decisions.append(self.rec(), root=self.root)
        self.assertEqual(s, 3)                    # продолжили от последней целой записи
        rs = decisions.read(root=self.root)
        self.assertEqual([r["seq"] for r in rs], [1, 2, 3])
        self.assertEqual(rs[2]["prev"], rs[1]["h"])
        self.assertTrue(decisions.verify(root=self.root)["ok"])

    def test_torn_only_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as f:
            f.write(b'{"seq":1,"ts')
        self.assertEqual(decisions.append(self.rec(), root=self.root), 1)
        self.assertEqual(len(decisions.read(root=self.root)), 1)

    def test_tamper_content_detected(self):
        for i in range(4):
            decisions.append(self.rec(args={"i": i}), root=self.root)
        ls = self.lines()
        r = json.loads(ls[1])
        r["args"]["i"] = 77
        ls[1] = json.dumps(r, sort_keys=True, separators=(",", ":")).encode()
        with open(self.path, "wb") as f:
            f.write(b"\n".join(ls))
        v = decisions.verify(root=self.root)
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad_seq"], 2)

    def test_deleted_line_detected(self):
        for i in range(4):
            decisions.append(self.rec(args={"i": i}), root=self.root)
        ls = self.lines()
        del ls[1]
        with open(self.path, "wb") as f:
            f.write(b"\n".join(ls))
        v = decisions.verify(root=self.root)
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad_seq"], 3)

    def test_swapped_lines_detected(self):
        for i in range(4):
            decisions.append(self.rec(), root=self.root)
        ls = self.lines()
        ls[1], ls[2] = ls[2], ls[1]
        with open(self.path, "wb") as f:
            f.write(b"\n".join(ls))
        self.assertFalse(decisions.verify(root=self.root)["ok"])

    def test_tamper_hash_recomputed_for_one_line_still_breaks_next(self):
        for i in range(3):
            decisions.append(self.rec(args={"i": i}), root=self.root)
        ls = self.lines()
        r = json.loads(ls[0])
        r["args"]["i"] = 5
        r["h"] = decisions._hash(r["prev"], r)
        ls[0] = decisions._canon(r).encode()
        with open(self.path, "wb") as f:
            f.write(b"\n".join(ls))
        v = decisions.verify(root=self.root)
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad_seq"], 2)

    def test_restart_continues(self):
        for _ in range(3):
            decisions.append(self.rec(), root=self.root)
        # «рестарт»: никакого кэша в памяти, всё берётся из файла
        self.assertEqual(decisions.append(self.rec(), root=self.root), 4)

    def test_big_file_tail(self):
        for i in range(300):
            decisions.append(self.rec(why="x" * 150), root=self.root)
        self.assertEqual(decisions.append(self.rec(), root=self.root), 301)
        self.assertTrue(decisions.verify(root=self.root)["ok"])

    def test_threads(self):
        def w():
            for _ in range(40):
                decisions.append(self.rec(), root=self.root)
        ts = [threading.Thread(target=w) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        v = decisions.verify(root=self.root)
        self.assertEqual((v["ok"], v["n"]), (True, 200))
        self.assertEqual([r["seq"] for r in decisions.read(limit=0, root=self.root)], list(range(1, 201)))

    def test_processes(self):
        try:
            pool = multiprocessing.Pool(3)
        except (OSError, ImportError) as e:
            self.skipTest(str(e))
        try:
            pool.map(_proc_append, [(self.root, 30)] * 3)
        finally:
            pool.close()
            pool.join()
        v = decisions.verify(root=self.root)
        self.assertEqual((v["ok"], v["n"]), (True, 90))

    def test_fsync_flag(self):
        self.assertEqual(decisions.append(self.rec(), root=self.root, fsync=True), 1)

    def test_unicode(self):
        decisions.append(self.rec(why="стоп к безубытку"), root=self.root)
        self.assertEqual(decisions.read(root=self.root)[0]["why"], "стоп к безубытку")
        self.assertTrue(decisions.verify(root=self.root)["ok"])


if __name__ == "__main__":
    unittest.main()
