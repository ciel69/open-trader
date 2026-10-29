"""WP11-B: процессы и логи: кроссплатформенные ветки, остановка группы, ротация. Без сети и без живых демонов проекта."""
import io
import os
import subprocess
import sys
import time
import unittest

import _bootstrap  # noqa: F401
import _platform_helpers as H
from tb.core.filelock import pid_alive
from tb.platform import logs as L
from tb.platform import procs
from tb.platform import services as S


class PlatformBranchTest(unittest.TestCase):
    def test_popen_kwargs_windows_posix(self):
        w = procs.popen_kwargs("win32")
        self.assertEqual(w["creationflags"] & procs.CREATE_NEW_PROCESS_GROUP, procs.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(w["creationflags"] & procs.CREATE_NO_WINDOW)
        self.assertNotIn("start_new_session", w)
        wd = procs.popen_kwargs("win32", detached=True)
        self.assertTrue(wd["creationflags"] & procs.DETACHED_PROCESS)
        for system in ("darwin", "linux"):
            k = procs.popen_kwargs(system)
            self.assertEqual(k, {"start_new_session": True})

    def test_stop_plan_branches(self):
        self.assertEqual([s[0] for s in procs.stop_plan("win32")], ["signal", "terminate", "kill"])
        self.assertEqual(procs.stop_plan("win32")[0][1], "CTRL_BREAK_EVENT")
        self.assertEqual(procs.stop_plan("darwin"), [("signal", "SIGTERM"), ("kill", "SIGKILL")])

    def test_is_windows(self):
        self.assertTrue(procs.is_windows("win32"))
        self.assertFalse(procs.is_windows("darwin"))


@unittest.skipUnless(sys.platform.startswith("win"), "Windows")
class WindowsProcTest(unittest.TestCase):
    def test_cmdline_of_returns_command_line(self):
        """Без командной строки freeze.is_ours() считал любой процесс чужим и `./tb stop` не посылал сигнал (супервизор не гасился)."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)  # marker-tb-cmdline"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            self.assertIn("marker-tb-cmdline", procs.cmdline_of(p.pid) or "")
        finally:
            p.kill()
            p.wait()

    def test_terminate_without_console_does_not_wait_grace(self):
        """CTRL_BREAK недоступен без консоли: сразу terminate(), а не ожидание grace/2 на каждый сервис."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **procs.popen_kwargs())
        time.sleep(0.5)
        t0 = time.time()
        procs.terminate(p, 20.0)
        self.assertIsNotNone(p.poll())
        self.assertLess(time.time() - t0, 8.0)


@unittest.skipUnless(os.name == "posix", "группа процессов POSIX")
class PosixProcTest(unittest.TestCase):
    def test_terminate_kills_group_including_grandchild(self):
        root = H.mk_root(self)
        pidf = os.path.join(root, "gc.pid")
        body = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time\\nwhile True: time.sleep(0.05)'])\n"
                "open(%r, 'w').write(str(p.pid))\n"
                "while True: time.sleep(0.05)\n") % pidf
        cmd = H.script(root, "parent.py", body)
        proc = procs.spawn(cmd, dict(os.environ), root)
        H.wait_for(lambda: H.read(pidf).strip())
        gc = int(H.read(pidf))
        self.assertTrue(pid_alive(gc))
        t0 = time.time()
        procs.terminate(proc, 5.0)
        self.assertLess(time.time() - t0, 4.0)
        self.assertTrue(H.wait_for(lambda: not pid_alive(gc), 3.0), "внук группы остался жив")

    def test_terminate_escalates_to_kill(self):
        root = H.mk_root(self)
        body = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen(%r, 'w').write('1')\nwhile True: time.sleep(0.05)\n" % os.path.join(root, "up")
        proc = procs.spawn(H.script(root, "stubborn.py", body), dict(os.environ), root)
        H.wait_for(lambda: os.path.exists(os.path.join(root, "up")))
        procs.terminate(proc, 0.5)
        self.assertIsNotNone(proc.poll())

    def test_line_reader_to_log(self):
        root = H.mk_root(self)
        log = L.RotatingLog(os.path.join(root, "x.log"))
        proc = procs.spawn(H.script(root, "p.py", "print('привет')\nprint('два')\n"), dict(os.environ), root)
        rd = procs.LineReader(proc, log)
        proc.wait(5)
        rd.join(3)
        text = H.read(os.path.join(root, "x.log"))
        self.assertIn("привет", text)
        self.assertEqual(len(text.splitlines()), 2)


class LogsTest(unittest.TestCase):
    def test_rotation_keeps_limit(self):
        root = H.mk_root(self)
        p = os.path.join(root, "logs", "s.log")
        log = L.RotatingLog(p, max_bytes=300, keep=2)
        for i in range(60):
            log.write("строка номер %d xxxxxxxxxx" % i)
        log.close()
        names = sorted(os.listdir(os.path.join(root, "logs")))
        self.assertEqual(names, ["s.log", "s.log.1", "s.log.2"])
        self.assertTrue(all(os.path.getsize(os.path.join(root, "logs", n)) <= 400 for n in names))
        self.assertIn("строка номер 59", L.tail(p, 3)[-1])

    def test_tail_and_missing(self):
        self.assertEqual(L.tail("/nonexistent/file.log", 5), [])
        root = H.mk_root(self)
        p = os.path.join(root, "a.log")
        log = L.RotatingLog(p)
        for i in range(10):
            log.write("l%d" % i)
        self.assertEqual([x.split(" ", 1)[1] for x in L.tail(p, 3)], ["l7", "l8", "l9"])

    def test_follow_survives_rotation(self):
        root = H.mk_root(self)
        p = os.path.join(root, "f.log")
        log = L.RotatingLog(p, max_bytes=200, keep=1)
        log.write("старт")
        out = io.StringIO()
        n = {"i": 0}

        def sleeper(_):
            n["i"] += 1
            log.write("новая %d xxxxxxxxxxxxxxxxxx" % n["i"])

        L.follow(p, out, stop=lambda: n["i"] >= 8, sleep=sleeper)
        got = out.getvalue()
        self.assertIn("новая 7", got)
        self.assertIn("новая 1 ", got)


if __name__ == "__main__":
    unittest.main()
