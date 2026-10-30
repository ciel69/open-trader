"""Звонок (core/doorbell.py): пробуждение вместо опроса, молчаливый отказ без получателя, изоляция по корню."""
import shutil
import tempfile
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from tb.core import doorbell


class DoorbellTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbbell_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_ring_without_receiver_is_silent(self):
        self.assertFalse(doorbell.ring("brain", self.root))

    def test_ring_wakes_waiter_fast(self):
        b = doorbell.Bell("brain", self.root)
        self.addCleanup(b.close)
        got = {}

        def w():
            t = time.time()
            got["ok"] = b.wait(5.0)
            got["dt"] = time.time() - t
        th = threading.Thread(target=w)
        th.start()
        time.sleep(0.2)
        self.assertTrue(doorbell.ring("brain", self.root))
        th.join(3)
        self.assertTrue(got["ok"])
        self.assertLess(got["dt"], 1.0)

    def test_ring_before_wait_is_not_lost(self):
        b = doorbell.Bell("exec", self.root)
        self.addCleanup(b.close)
        doorbell.ring("exec", self.root)
        time.sleep(0.2)
        t = time.time()
        self.assertTrue(b.wait(5.0))
        self.assertLess(time.time() - t, 0.5)
        self.assertFalse(b.wait(0.1))                     # один звонок будит один раз

    def test_timeout_returns_false(self):
        b = doorbell.Bell("dispatch", self.root)
        self.addCleanup(b.close)
        self.assertFalse(b.wait(0.1))

    def test_callback_and_isolation_between_roots(self):
        other = tempfile.mkdtemp(prefix="tbbell2_")
        self.addCleanup(shutil.rmtree, other, True)
        hits = []
        b = doorbell.Bell("exec", self.root, callback=lambda: hits.append(1))
        self.addCleanup(b.close)
        self.assertFalse(doorbell.ring("exec", other))     # другой корень платформы не звонит в чужой процесс
        doorbell.ring("exec", self.root)
        time.sleep(0.3)
        self.assertEqual(hits, [1])

    def test_close_removes_port_file(self):
        b = doorbell.Bell("outbox", self.root)
        b.close()
        time.sleep(0.1)
        doorbell._cache.clear()
        self.assertFalse(doorbell.ring("outbox", self.root))


if __name__ == "__main__":
    unittest.main()
