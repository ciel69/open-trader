"""Сторож: тесты не пишут в реальные state/, config/, runtime/ проекта (снимок при импорте _bootstrap сверяется с текущим).
Имя test_zz_*: файл идёт последним при discover. Отключить: TB_TEST_NOGUARD=1 (например, когда параллельно правится config/ руками)."""
import os
import unittest

import _bootstrap
from tb.core import filelock


class RealStateGuard(unittest.TestCase):
    @staticmethod
    def _diff(before, now):
        added = sorted(set(now) - set(before))
        removed = sorted(set(before) - set(now))
        changed = sorted(k for k in set(now) & set(before) if now[k] != before[k])
        return added, removed, changed

    def test_real_dirs_untouched(self):
        if os.environ.get("TB_TEST_NOGUARD"):
            self.skipTest("сторож отключён TB_TEST_NOGUARD")
        before, now = _bootstrap.REAL_SNAPSHOT, _bootstrap._snapshot_real()
        # config/ платформа сама не пишет: проверяется ВСЕГДА, даже при живой платформе
        cfg = lambda d: dict((k, v) for k, v in d.items() if k.startswith("config" + os.sep))
        self.assertEqual(self._diff(cfg(before), cfg(now)), ([], [], []), "тесты изменили реальный config/")
        foreign = self._foreign_heartbeats()
        if foreign:
            # живая платформа владельца пишет в runtime/ и state/ сама (шина, контекст, синхронизация): утечку тестов от неё не отличить
            self.skipTest("запущена живая платформа (heartbeat чужого процесса: %s): state/ и runtime/ не проверяются, config/ проверен; "
                          "остановите платформу (./tb down) для строгой проверки" % ", ".join(sorted(os.path.basename(x) for x in foreign)))
        self.assertEqual(self._diff(before, now), ([], [], []), "тесты изменили реальные каталоги проекта (added, removed, changed)")

    def test_env_not_leaked(self):
        """Окружение герметичности цело, а временный TB_ROOT другого теста не утёк."""
        self.assertFalse(os.environ.get("TB_ROOT"), "утечка TB_ROOT=%s из какого-то теста" % os.environ.get("TB_ROOT"))
        for k in ("TB_STATE_DIR_CONTROL", "TB_STATE_DIR_BRAIN", "TB_KEYS_DIR", "TB_CONFIG_LOCAL"):
            v = os.environ.get(k, "")
            self.assertTrue(v and os.path.isdir(v), "%s не указывает на временный каталог теста: %r" % (k, v))
            self.assertNotIn(os.path.join(".config", "traider-bot"), v, k)
            self.assertFalse(os.path.abspath(v).startswith(os.path.join(_bootstrap.ROOT, "")), "%s указывает внутрь реального проекта" % k)

    @staticmethod
    def _foreign_heartbeats(root=None):
        """Heartbeat-файлы runtime/platform/*.hb.json, которые пишет ЧУЖОЙ живой процесс (платформа владельца, зависший прогон другого сеанса): не наша утечка."""
        import json
        out = set()
        d = os.path.join(root or _bootstrap.ROOT, "runtime", "platform")
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for n in names:
            if not n.endswith(".hb.json"):
                continue
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    pid = json.load(f).get("pid")
                if isinstance(pid, int) and pid != os.getpid() and filelock.pid_alive(pid):      # не os.kill(pid, 0): на Windows это TerminateProcess и убило бы живую платформу
                    out.add(os.path.join("runtime", "platform", n))
            except (OSError, ValueError, AttributeError):
                pass
        return out

    def test_guard_strict_when_platform_stopped(self):
        """Платформа остановлена (heartbeat мёртвого процесса) -> «чужих» нет, строгая сверка работает и ловит запись."""
        import json
        import shutil
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="tbguard_")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "runtime", "platform"))
        os.makedirs(os.path.join(d, "state"))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(os.path.join(d, "runtime", "platform", "x.hb.json"), "w") as f:
            json.dump({"pid": p.pid}, f)           # процесс завершён -> filelock.pid_alive даёт False
        self.assertEqual(self._foreign_heartbeats(d), set())
        a = _bootstrap._snapshot_real(d)
        with open(os.path.join(d, "state", "leak.json"), "w") as f:
            f.write("{}")
        self.assertEqual(self._diff(a, _bootstrap._snapshot_real(d))[0], [os.path.join("state", "leak.json")])
        # живой чужой процесс (родитель) определяется как чужой
        with open(os.path.join(d, "runtime", "platform", "y.hb.json"), "w") as f:
            json.dump({"pid": os.getppid()}, f)
        self.assertEqual(self._foreign_heartbeats(d), {os.path.join("runtime", "platform", "y.hb.json")})

    def test_snapshot_detects_change(self):
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="tbguard_")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "state"))
        a = _bootstrap._snapshot_real(d)
        with open(os.path.join(d, "state", "x.json"), "w") as f:
            f.write("{}")
        self.assertNotEqual(a, _bootstrap._snapshot_real(d))

    def test_localhost_probe_returns_reason(self):
        ok, why = _bootstrap.localhost_ok()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
