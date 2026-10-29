import copy
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import filelock, plan_store
from tb.core.plan_store import PlanStore


def _proc_counter(args):
    root, n = args
    st = PlanStore(root)
    return [st.counters_next("plan_n") for _ in range(n)]


def _proc_inbox(args):
    root, i = args
    return PlanStore(root).inbox_put({"cmd_id": "C-20261028T101502-same", "op": "submit", "i": i})


def _spec(sym="SOL", **kw):
    d = {"schema": "tb.plan/1", "symbol": sym, "side": "long", "mode": "live", "thesis": "t"}
    d.update(kw)
    return d


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbps_")
        self.st = PlanStore(self.root)

    def mk(self, sym="SOL", status="armed", **kw):
        pid = self.st.create(_spec(sym, **kw), {"status": "draft"})
        if status != "draft":
            spec, rt = self.st.load(pid)
            rt["status"] = status
            if status in ("entered", "managing", "closed"):
                rt["status"] = "armed"
                self.st.save_rt(pid, rt)
                rt["status"] = "entered"
                self.st.save_rt(pid, rt)
                if status in ("managing", "closed"):
                    rt["status"] = "managing"
                    self.st.save_rt(pid, rt)
                if status == "closed":
                    rt["status"] = "closed"
                    self.st.save_rt(pid, rt)
            else:
                self.st.save_rt(pid, rt)
        return pid

    # --- create / load ---
    def test_create_load(self):
        pid = self.st.create(_spec(), {"status": "draft"})
        self.assertRegex(pid, r"^PL-\d{4}-SOL-01$")
        spec, rt = self.st.load(pid)
        self.assertEqual(spec["id"], pid)
        self.assertEqual(spec["rev"], 1)
        self.assertEqual(rt["plan_id"], pid)
        self.assertEqual(rt["plan_n"], 1)
        self.assertEqual(rt["status"], "draft")
        self.assertIn("created", rt["ts"])
        revs = self.st.revs(pid)
        self.assertEqual([r["rev"] for r in revs], [1])
        self.assertEqual(revs[0]["why"], "create")

    def test_create_does_not_mutate_args(self):
        s, r = _spec(), {"status": "draft"}
        s0, r0 = copy.deepcopy(s), copy.deepcopy(r)
        self.st.create(s, r)
        self.assertEqual((s, r), (s0, r0))

    def test_id_numbering_and_plan_n(self):
        a = self.st.create(_spec("SOL"), {})
        b = self.st.create(_spec("SOL"), {})
        c = self.st.create(_spec("ETH"), {})
        self.assertTrue(a.endswith("SOL-01") and b.endswith("SOL-02") and c.endswith("ETH-01"))
        ns = [self.st.load(x)[1]["plan_n"] for x in (a, b, c)]
        self.assertEqual(ns, [1, 2, 3])

    def test_explicit_id_and_collision(self):
        pid = self.st.create(_spec(id="PL-1028-SOL-01"), {})
        self.assertEqual(pid, "PL-1028-SOL-01")
        with self.assertRaises(ValueError):
            self.st.create(_spec(id="PL-1028-SOL-01"), {})
        self.assertEqual(len(self.st.list()), 1)

    def test_bad_ids_rejected(self):
        for bad in ("../x", "PL-../x", "x", "", None):
            self.assertEqual(self.st.load(bad), (None, None))
        with self.assertRaises(ValueError):
            self.st.create(_spec(id="PL-../../etc"), {})
        with self.assertRaises(ValueError):
            self.st.save_rt("../x", {})

    def test_create_threads_unique_ids_and_plan_n(self):
        out = []

        def w():
            for _ in range(5):
                out.append(self.st.create(_spec("SOL"), {}))
        ts = [threading.Thread(target=w) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(out)), 20)
        ns = sorted(self.st.load(p)[1]["plan_n"] for p in out)
        self.assertEqual(ns, list(range(1, 21)))

    def test_failed_create_leaves_nothing(self):
        orig = plan_store.atomic_write_json

        def boom(path, obj, **kw):
            if path.endswith("runtime.json"):
                raise OSError("диск")
            return orig(path, obj, **kw)
        plan_store.atomic_write_json = boom
        try:
            with self.assertRaises(OSError):
                self.st.create(_spec(), {})
        finally:
            plan_store.atomic_write_json = orig
        self.assertEqual(self.st.list(), [])
        self.assertEqual([n for n in os.listdir(paths.plans(root=self.root)) if n.startswith("PL-") or n.startswith("_tmp")], [])

    def test_old_md_plans_untouched(self):
        d = paths.plans(root=self.root)
        os.makedirs(d)
        open(os.path.join(d, "2026-10-27.md"), "w").write("# plan")
        self.mk()
        self.assertEqual(len(self.st.list()), 1)
        self.assertTrue(os.path.exists(os.path.join(d, "2026-10-27.md")))

    # --- list ---
    def test_list_filters(self):
        a = self.mk("SOL", "armed")
        b = self.mk("ETH", "draft", mode="paper")
        self.assertEqual({x["spec"]["id"] for x in self.st.list()}, {a, b})
        self.assertEqual([x["spec"]["id"] for x in self.st.list(status="armed")], [a])
        self.assertEqual([x["spec"]["id"] for x in self.st.list(status=("armed", "draft"), mode="paper")], [b])
        self.assertEqual([x["spec"]["id"] for x in self.st.list(mode="live")], [a])

    def test_list_skips_broken(self):
        a = self.mk()
        b = self.mk("ETH")
        open(os.path.join(paths.plans(b, root=self.root), "spec.json"), "w").write("{oops")
        os.makedirs(os.path.join(paths.plans(root=self.root), "PL-0000-EMPTY-01"))
        res = self.st.list()
        self.assertEqual([x["spec"]["id"] for x in res], [a])
        self.assertEqual(self.st.broken, 2)

    # --- save_spec ---
    def test_save_spec_rev_and_revs(self):
        pid = self.mk()
        spec, _ = self.st.load(pid)
        spec["thesis"] = "новый"
        new = self.st.save_spec(spec, "amend")
        self.assertEqual(new["rev"], 2)
        self.assertEqual(spec["rev"], 2)
        self.assertEqual(self.st.load(pid)[0]["thesis"], "новый")
        spec["thesis"] = "ещё"
        self.st.save_spec(spec, "amend2")
        revs = self.st.revs(pid)
        self.assertEqual([r["rev"] for r in revs], [1, 2, 3])
        self.assertEqual(revs[1]["why"], "amend")
        self.assertEqual(revs[1]["spec"]["thesis"], "новый")
        self.assertEqual(revs[0]["spec"]["thesis"], "t")      # история хранит снимки

    def test_save_spec_conflict(self):
        pid = self.mk()
        spec, _ = self.st.load(pid)
        stale = copy.deepcopy(spec)
        self.st.save_spec(spec, "a")
        with self.assertRaises(ValueError) as cm:
            self.st.save_spec(stale, "b")
        self.assertIn("конфликт", str(cm.exception))
        self.assertEqual(self.st.load(pid)[0]["rev"], 2)

    def test_save_spec_unknown_plan(self):
        with self.assertRaises(KeyError):
            self.st.save_spec({"id": "PL-0101-X-01", "rev": 1}, "x")

    def test_save_spec_threads_serialised(self):
        pid = self.mk()
        ok, conflicts = [], []

        def w():
            for _ in range(5):
                while True:
                    spec, _ = self.st.load(pid)
                    try:
                        self.st.save_spec(spec, "t")
                        ok.append(1)
                        break
                    except ValueError:
                        conflicts.append(1)
        ts = [threading.Thread(target=w) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.st.load(pid)[0]["rev"], 21)
        self.assertEqual([r["rev"] for r in self.st.revs(pid)], list(range(1, 22)))

    # --- save_rt ---
    def test_save_rt_transitions(self):
        pid = self.mk(status="draft")
        _, rt = self.st.load(pid)
        rt["status"] = "managing"                     # draft -> managing запрещён
        with self.assertRaises(ValueError):
            self.st.save_rt(pid, rt)
        self.assertEqual(self.st.load(pid)[1]["status"], "draft")
        rt["status"] = "armed"
        self.st.save_rt(pid, rt)
        rt["status"] = "entered"
        self.st.save_rt(pid, rt)
        rt["status"] = "managing"
        self.st.save_rt(pid, rt)
        rt["status"] = "closed"
        self.st.save_rt(pid, rt)
        rt["status"] = "armed"                        # из терминала выхода нет
        with self.assertRaises(ValueError):
            self.st.save_rt(pid, rt)
        rt["status"] = "closed"
        rt["close"] = {"reason": "x"}
        self.st.save_rt(pid, rt)                      # правка полей в терминале допустима
        self.assertEqual(self.st.load(pid)[1]["close"]["reason"], "x")

    def test_save_rt_same_status_ok_and_plan_n_immutable(self):
        pid = self.mk(status="armed")
        _, rt = self.st.load(pid)
        n = rt["plan_n"]
        rt["plan_n"] = 999
        rt["entry"] = {"state": "waiting"}
        self.st.save_rt(pid, rt)
        rt2 = self.st.load(pid)[1]
        self.assertEqual(rt2["plan_n"], n)
        self.assertEqual(rt2["entry"]["state"], "waiting")

    def test_save_rt_wrong_plan_id_and_unknown(self):
        pid = self.mk()
        with self.assertRaises(ValueError):
            self.st.save_rt(pid, {"plan_id": "PL-0101-X-09", "status": "armed"})
        with self.assertRaises(KeyError):
            self.st.save_rt("PL-0101-X-09", {"status": "armed"})

    def test_save_rt_interrupted_write_keeps_old_file(self):
        pid = self.mk(status="armed")
        _, rt = self.st.load(pid)
        rt["marker"] = 1
        self.st.save_rt(pid, rt)
        orig = filelock.fsutil.replace
        filelock.fsutil.replace = lambda a, b: (_ for _ in ()).throw(OSError("обрыв"))
        try:
            rt["marker"] = 2
            with self.assertRaises(OSError):
                self.st.save_rt(pid, rt)
        finally:
            filelock.fsutil.replace = orig
        self.assertEqual(self.st.load(pid)[1]["marker"], 1)
        left = [n for n in os.listdir(paths.plans(pid, root=self.root)) if ".tmp." in n]
        self.assertEqual(left, [])

    def test_stray_tmp_file_harmless(self):
        pid = self.mk()
        open(os.path.join(paths.plans(pid, root=self.root), "runtime.json.tmp.1.2.abc"), "w").write("{обр")
        self.assertIsNotNone(self.st.load(pid)[1])
        self.assertEqual(len(self.st.list()), 1)

    def test_restart_new_instance_sees_state(self):
        pid = self.mk(status="armed")
        st2 = PlanStore(self.root)
        self.assertEqual(st2.load(pid)[1]["status"], "armed")
        self.assertEqual(st2.create(_spec(), {}) != pid, True)
        self.assertEqual(st2.load(st2.list()[-1]["spec"]["id"])[1]["plan_n"], 2)

    def test_save_rt_threads(self):
        pid = self.mk(status="armed")

        def w(k):
            for i in range(30):
                _, rt = self.st.load(pid)
                rt["w%d" % k] = i
                self.st.save_rt(pid, rt)
        ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        rt = self.st.load(pid)[1]                # файл цел и парсится
        self.assertEqual(rt["status"], "armed")

    # --- counters ---
    def test_counters_sequence(self):
        self.assertEqual([self.st.counters_next("x") for _ in range(3)], [1, 2, 3])
        self.assertEqual(self.st.counters_next("y"), 1)

    def test_counters_threads_unique(self):
        out = []

        def w():
            for _ in range(25):
                out.append(self.st.counters_next("plan_n"))
        ts = [threading.Thread(target=w) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), list(range(1, 101)))

    def test_counters_processes_unique(self):
        try:
            pool = multiprocessing.Pool(3)
        except (OSError, ImportError) as e:
            self.skipTest(str(e))
        try:
            res = pool.map(_proc_counter, [(self.root, 20)] * 3)
        finally:
            pool.close()
            pool.join()
        flat = sorted(x for r in res for x in r)
        self.assertEqual(flat, list(range(1, 61)))

    def test_counters_corrupt_recovers_plan_n(self):
        self.mk()
        self.mk("ETH")
        open(paths.plans_counters(root=self.root), "w").write("{мусор")
        self.assertEqual(self.st.counters_next("plan_n"), 3)    # не 1: коллизий plan_n нет
        self.assertTrue(any(".corrupt-" in n for n in os.listdir(paths.plans(root=self.root))))

    def test_counters_bad_name(self):
        with self.assertRaises(ValueError):
            self.st.counters_next("")

    # --- archive ---
    def test_archive_rules(self):
        live = self.mk(status="armed")
        closed = self.mk("ETH", "closed")
        self.assertIsNone(self.st.archive(live, force=True))                # не терминален
        self.assertIsNone(self.st.archive(closed))                          # свеж
        self.assertTrue(os.path.isdir(paths.plans(closed, root=self.root)))
        dst = self.st.archive(closed, now=time.time() + 8 * 86400)
        self.assertTrue(os.path.isdir(dst))
        self.assertIn("_archive", dst)
        self.assertRegex(dst, r"_archive[/\\]\d{4}-\d{2}[/\\]" + closed)
        self.assertEqual(self.st.load(closed), (None, None))
        self.assertEqual([x["spec"]["id"] for x in self.st.list()], [live])
        # id из архива занят: коллизия
        with self.assertRaises(ValueError):
            self.st.create(_spec(id=closed), {})

    def test_archive_uses_ts_closed(self):
        pid = self.mk(status="closed")
        _, rt = self.st.load(pid)
        rt["ts"]["closed"] = time.time() - 10 * 86400
        self.st.save_rt(pid, rt)
        self.assertIsNotNone(self.st.archive(pid))

    def test_archive_all(self):
        a = self.mk(status="closed")
        self.mk("ETH", "armed")
        self.assertEqual(self.st.archive_all(now=time.time() + 9 * 86400), [a])

    def test_plan_n_survives_archive(self):
        a = self.mk(status="closed")
        self.st.archive(a, force=True)
        open(paths.plans_counters(root=self.root), "w").write("{")
        self.assertEqual(self.st.counters_next("plan_n"), 2)

    # --- inbox / outbox ---
    def test_inbox_put_list_done(self):
        cid = self.st.inbox_put({"op": "submit", "payload": {"a": 1}})
        self.assertRegex(cid, r"^C-\d{8}T\d{6}-[0-9a-f]{6}$")
        lst = self.st.inbox_list()
        self.assertEqual([c["cmd_id"] for c in lst], [cid])
        self.assertEqual(lst[0]["op"], "submit")
        self.assertFalse(self.st.seen_cmd(cid))
        self.assertIsNone(self.st.outbox_get(cid))
        self.st.inbox_done(cid, {"ok": True, "status": "armed"})
        self.assertEqual(self.st.inbox_list(), [])
        self.assertEqual(self.st.outbox_get(cid)["status"], "armed")
        self.assertEqual(self.st.outbox_get(cid)["cmd_id"], cid)
        self.assertTrue(self.st.seen_cmd(cid))

    def test_inbox_order(self):
        ids = []
        for i in range(5):
            ids.append(self.st.inbox_put({"cmd_id": "C-20261028T1015%02d-aaaaa%d" % (i, i), "i": i}))
        self.assertEqual([c["cmd_id"] for c in self.st.inbox_list()], sorted(ids))

    def test_inbox_unique_under_race_threads(self):
        out = []

        def w():
            for _ in range(20):
                out.append(self.st.inbox_put({"op": "x"}))
        ts = [threading.Thread(target=w) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(out)), 80)
        self.assertEqual(len(self.st.inbox_list()), 80)

    def test_inbox_same_cmd_id_idempotent(self):
        a = self.st.inbox_put({"cmd_id": "C-1-x", "v": 1})
        b = self.st.inbox_put({"cmd_id": "C-1-x", "v": 2})
        self.assertEqual(a, b)
        self.assertEqual(self.st.inbox_list()[0]["v"], 1)            # не перезаписана
        self.st.inbox_done("C-1-x", {"ok": True})
        self.st.inbox_put({"cmd_id": "C-1-x", "v": 3})               # повторная доставка после обработки
        self.assertEqual(self.st.inbox_list(), [])                   # обратно в очередь не попала

    def test_inbox_same_cmd_id_processes(self):
        try:
            pool = multiprocessing.Pool(3)
        except (OSError, ImportError) as e:
            self.skipTest(str(e))
        try:
            res = pool.map(_proc_inbox, [(self.root, i) for i in range(6)])
        finally:
            pool.close()
            pool.join()
        self.assertEqual(set(res), {"C-20261028T101502-same"})
        self.assertEqual(len(self.st.inbox_list()), 1)

    def test_inbox_bad_id(self):
        with self.assertRaises(ValueError):
            self.st.inbox_put({"cmd_id": "../evil"})
        self.assertIsNone(self.st.outbox_get("../x"))

    def test_inbox_broken_file_reported(self):
        d = paths.plans_inbox(root=self.root)
        os.makedirs(d)
        open(os.path.join(d, "C-1-bad.json"), "w").write("{")
        lst = self.st.inbox_list()
        self.assertEqual(lst, [{"cmd_id": "C-1-bad", "_broken": True}])
        self.st.inbox_done("C-1-bad", {"ok": False})
        self.assertEqual(self.st.inbox_list(), [])

    def test_inbox_ignores_tmp_files(self):
        self.st.inbox_put({"cmd_id": "C-1-a"})
        d = paths.plans_inbox(root=self.root)
        open(os.path.join(d, "C-2-b.json.tmp.1.2.x"), "w").write("{")
        self.assertEqual([c["cmd_id"] for c in self.st.inbox_list()], ["C-1-a"])

    def test_seen_cmd_keeps_last_500(self):
        for i in range(520):
            self.st.mark_seen("C-%04d-x" % i)
        self.assertFalse(self.st.seen_cmd("C-0000-x"))
        self.assertTrue(self.st.seen_cmd("C-0519-x"))
        self.assertTrue(self.st.seen_cmd("C-0020-x"))
        self.assertEqual(len(json.load(open(paths.plans("_seen_cmds.json", root=self.root)))), 500)

    def test_seen_via_outbox(self):
        cid = self.st.inbox_put({"cmd_id": "C-9-z"})
        os.remove(os.path.join(paths.plans_inbox(root=self.root), "C-9-z.json"))
        os.makedirs(paths.plans_outbox(root=self.root), exist_ok=True)
        open(os.path.join(paths.plans_outbox(root=self.root), "C-9-z.json"), "w").write("{}")
        self.assertTrue(self.st.seen_cmd(cid))

    def test_outbox_gc(self):
        cid = self.st.inbox_put({"op": "x"})
        self.st.inbox_done(cid, {"ok": True})
        self.assertEqual(self.st.outbox_gc(max_age_s=3600), 0)
        self.assertEqual(self.st.outbox_gc(max_age_s=3600, now=time.time() + 7200), 1)

    def test_root_isolated(self):
        other = tempfile.mkdtemp(prefix="tbps2_")
        self.mk()
        self.assertEqual(PlanStore(other).list(), [])
        self.assertTrue(paths.plans(root=self.root).startswith(self.root))


if __name__ == "__main__":
    unittest.main()
