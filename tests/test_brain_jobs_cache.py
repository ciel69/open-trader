"""Очередь заданий мозга: терминальные задания не перечитываются на каждом опросе (кэш по mtime), правка файла снаружи видна сразу."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb.brain import jobs


def put(root, jid, status, **kw):
    d = jobs.jobs_dir(root)
    os.makedirs(d, exist_ok=True)
    j = {"id": jid, "sub": "api", "status": status, "prio": 1, "key": "k" + jid, "ts_first": 1.0, "ts_last": 1.0, "not_before": 0, "seqs": [1], "types": ["milestone"]}
    j.update(kw)
    with open(os.path.join(d, jid + ".json"), "w", encoding="utf-8") as f:
        json.dump(j, f)
    return j


class JobsCache(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbjobs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        jobs._TERM_CACHE.clear()

    def test_terminal_jobs_are_read_once(self):
        for i in range(5):
            put(self.root, "J-20261030T000000-%03d" % i, "done", done_ts=2.0)
        put(self.root, "J-20261030T000000-100", "new")
        self.assertEqual(len(jobs.list_jobs(self.root)), 6)
        with mock.patch.object(jobs, "_read", wraps=jobs._read) as rd:
            new = jobs.list_jobs(self.root, "new")
            self.assertEqual([j["id"] for j in new], ["J-20261030T000000-100"])
            self.assertEqual(rd.call_count, 1)              # перечитано только незавершённое, пять терминальных взяты из памяти

    def test_external_edit_invalidates_cache(self):
        put(self.root, "J-20261030T000000-001", "done", done_ts=2.0)
        self.assertEqual(jobs.list_jobs(self.root, "new"), [])
        time.sleep(0.02)
        put(self.root, "J-20261030T000000-001", "new")      # файл переписан: задание снова открыто
        os.utime(os.path.join(jobs.jobs_dir(self.root), "J-20261030T000000-001.json"), (time.time() + 5, time.time() + 5))
        self.assertEqual([j["status"] for j in jobs.list_jobs(self.root, "new")], ["new"])

    def test_returned_jobs_are_copies_and_prune_forgets_files(self):
        put(self.root, "J-20261030T000000-001", "done", done_ts=1.0)
        a = jobs.list_jobs(self.root)[0]
        a["status"] = "new"
        self.assertEqual(jobs.list_jobs(self.root)[0]["status"], "done")        # правка копии не портит кэш
        self.assertEqual(jobs.prune(self.root, 10 ** 9, 60.0), 1)
        self.assertEqual(jobs.list_jobs(self.root), [])

    def test_pick_still_takes_ready_job_among_many_done(self):
        for i in range(50):
            put(self.root, "J-20261030T000000-%03d" % i, "done", done_ts=2.0)
        put(self.root, "J-20261030T000000-900", "new", ts_last=time.time(), ts_first=time.time())
        j = jobs.pick(self.root, time.time(), owner="t")
        self.assertEqual(j["id"], "J-20261030T000000-900")
        self.assertEqual(j["status"], "taken")


if __name__ == "__main__":
    unittest.main()
