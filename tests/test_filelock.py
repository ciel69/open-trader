import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from tb.core import filelock
from tb.core.filelock import FileLock


def _noop():
    pass


def _proc_incr(args):
    lockp, counter, n = args
    for _ in range(n):
        with FileLock(lockp, wait=30):
            with open(counter) as f:
                v = int(f.read() or 0)
            time.sleep(0.0005)
            with open(counter, "w") as f:
                f.write(str(v + 1))


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tbfl_")
        self.lp = os.path.join(self.d, "x.lock")

    def test_acquire_release(self):
        with FileLock(self.lp):
            self.assertTrue(os.path.exists(self.lp))
        self.assertFalse(os.path.exists(self.lp))

    def test_creates_parent_dir(self):
        p = os.path.join(self.d, "a", "b", "y.lock")
        with FileLock(p):
            self.assertTrue(os.path.exists(p))

    def test_reentrant_same_thread(self):
        with FileLock(self.lp, wait=0.2):
            with FileLock(self.lp, wait=0.2):
                self.assertTrue(os.path.exists(self.lp))
            self.assertTrue(os.path.exists(self.lp))
        self.assertFalse(os.path.exists(self.lp))

    def test_other_thread_waits_then_times_out(self):
        got = []

        def other():
            try:
                with FileLock(self.lp, wait=0.2):
                    got.append("in")
            except TimeoutError:
                got.append("timeout")
        with FileLock(self.lp):
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(got, ["timeout"])

    def test_timeout_message(self):
        with FileLock(self.lp):
            t0 = time.time()
            res = []
            th = threading.Thread(target=lambda: res.append(self._try(0.15)))
            th.start()
            th.join()
        self.assertIn("занята", res[0])
        self.assertLess(time.time() - t0, 3)

    def _try(self, wait):
        try:
            with FileLock(self.lp, wait=wait):
                return "ok"
        except TimeoutError as e:
            return str(e)

    def test_stale_by_age(self):
        with open(self.lp, "w") as f:
            f.write("%d tok" % os.getpid())
        old = time.time() - 1000
        os.utime(self.lp, (old, old))
        with FileLock(self.lp, wait=1, stale_s=60):
            pass

    @unittest.skipUnless(os.name == "posix", "pid-проверка на POSIX")
    def test_stale_dead_pid(self):
        # pid заведомо несуществующего процесса: берём завершённый дочерний
        p = multiprocessing.Process(target=_noop)
        p.start()
        p.join()
        with open(self.lp, "w") as f:
            f.write("%d tok" % p.pid)
        t0 = time.time()
        with FileLock(self.lp, wait=5, stale_s=3600):
            pass
        self.assertLess(time.time() - t0, 2)

    def test_fresh_live_lock_not_stolen(self):
        with open(self.lp, "w") as f:
            f.write("%d tok" % os.getpid())
        with self.assertRaises(TimeoutError):
            with FileLock(self.lp, wait=0.2, stale_s=3600):
                pass
        self.assertTrue(os.path.exists(self.lp))

    def test_does_not_remove_foreign_lock_after_steal(self):
        lk = FileLock(self.lp, stale_s=60)
        with lk:
            os.remove(self.lp)                       # кто-то счёл блокировку устаревшей и перехватил
            with open(self.lp, "w") as f:
                f.write("%d othertoken" % (os.getpid()))
        self.assertTrue(os.path.exists(self.lp))     # чужую не трогаем

    def test_threads_mutual_exclusion(self):
        counter = [0]
        errs = []

        def work():
            for _ in range(50):
                with FileLock(self.lp, wait=20):
                    v = counter[0]
                    time.sleep(0.0001)
                    counter[0] = v + 1
        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(counter[0], 300)

    def test_processes_mutual_exclusion(self):
        counter = os.path.join(self.d, "cnt")
        with open(counter, "w") as f:
            f.write("0")
        try:
            pool = multiprocessing.Pool(4)
        except (OSError, ImportError) as e:
            self.skipTest("multiprocessing недоступен: %s" % e)
        try:
            pool.map(_proc_incr, [(self.lp, counter, 25)] * 4)
        finally:
            pool.close()
            pool.join()
        with open(counter) as f:
            self.assertEqual(int(f.read()), 100)

    def test_no_fcntl(self):
        src = open(filelock.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
        self.assertNotIn("import fcntl", src)

    def test_atomic_write_leaves_old_on_failure(self):
        p = os.path.join(self.d, "f.json")
        filelock.atomic_write(p, "old")

        class Boom(object):
            def __str__(self):
                raise RuntimeError("x")
        orig = filelock.fsutil.replace
        filelock.fsutil.replace = lambda a, b: (_ for _ in ()).throw(OSError("crash"))
        try:
            with self.assertRaises(OSError):
                filelock.atomic_write(p, "new")
        finally:
            filelock.fsutil.replace = orig
        self.assertEqual(open(p).read(), "old")
        self.assertEqual([n for n in os.listdir(self.d) if ".tmp." in n], [])


if __name__ == "__main__":
    unittest.main()
