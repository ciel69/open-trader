"""Launcher: поиск env-файла ключа, разбор, снятие прокси, пути для macOS/Windows. Только синтетические файлы во временной папке."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb import fsutil, launcher

FAKE = "SYNTH_VALUE_not_a_real_key"


class TestParse(unittest.TestCase):
    def test_formats(self):
        text = "\ufeff# комментарий\nBYBIT_API_KEY=abc\nexport BYBIT_API_SECRET='s e c'\nBYBIT_ENV=\"mainnet\"  \nX=1 # хвост\nY=a#b\nbad line\n1BAD=2\n\nEMPTY=\n"
        d = launcher.parse_env_text(text)
        self.assertEqual(d, {"BYBIT_API_KEY": "abc", "BYBIT_API_SECRET": "s e c", "BYBIT_ENV": "mainnet", "X": "1", "Y": "a#b", "EMPTY": ""})

    def test_crlf(self):
        self.assertEqual(launcher.parse_env_text("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})


class TestProxy(unittest.TestCase):
    def test_strip_any_case(self):
        env = {"HTTPS_PROXY": "x", "http_proxy": "y", "All_Proxy": "z", "NO_PROXY": "k", "PATH": "p"}
        removed = launcher.strip_proxies(env)
        self.assertEqual(sorted(removed), ["All_Proxy", "HTTPS_PROXY", "http_proxy"])
        self.assertEqual(env, {"NO_PROXY": "k", "PATH": "p"})


class TestKeyDirs(unittest.TestCase):
    def test_mac(self):
        self.assertEqual(launcher.key_dirs({"HOME": "/Users/u"}, "darwin"), [os.path.join("/Users/u", ".config", "traider-bot")])

    def test_windows_primary_and_fallback(self):
        d = launcher.key_dirs({"USERPROFILE": "C:\\Users\\u", "APPDATA": "C:\\Users\\u\\AppData\\Roaming"}, "win32")
        self.assertEqual(d, [os.path.join("C:\\Users\\u", ".config", "traider-bot"), os.path.join("C:\\Users\\u\\AppData\\Roaming", "traider-bot")])

    def test_override(self):
        self.assertEqual(launcher.key_dirs({"TB_KEYS_DIR": "/k", "HOME": "/h"}, "darwin"), ["/k"])


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tbkeys_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, text, sub=None):
        d = os.path.join(self.d, sub) if sub else self.d
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_profile_files_and_env(self):
        self.write("bybit_mainnet.env", "BYBIT_ENV=mainnet\nBYBIT_API_KEY=%s_read\n" % FAKE)
        self.write("bybit_mainnet_trade.env", "export BYBIT_ENV=mainnet\nBYBIT_API_KEY=%s_trade\n" % FAKE)
        self.write("bybit_testnet.env", "BYBIT_ENV=testnet\nBYBIT_API_KEY=%s_test\n" % FAKE)
        for prof, env_name, suffix in (("read", "mainnet", "_read"), ("trade", "mainnet", "_trade"), ("testnet", "testnet", "_test")):
            env = {"TB_KEYS_DIR": self.d, "HTTPS_PROXY": "http://p", "https_proxy": "http://p", "KEEP": "1"}
            p, rest = launcher.prepare_bybit(["bybit", prof, "balance", "--x"], env)
            self.assertEqual((p, rest), (prof, ["balance", "--x"]))
            self.assertEqual((env["BYBIT_ENV"], env["BYBIT_API_KEY"], env["KEEP"]), (env_name, FAKE + suffix, "1"))
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("https_proxy", env)

    def test_windows_fallback_to_appdata(self):
        self.write("bybit_testnet.env", "BYBIT_API_KEY=%s\n" % FAKE, sub=os.path.join("AppData", "traider-bot"))
        env = {"USERPROFILE": os.path.join(self.d, "profile"), "APPDATA": os.path.join(self.d, "AppData")}
        self.assertTrue(launcher.find_key_file("testnet", env, "win32").endswith(os.path.join("AppData", "traider-bot", "bybit_testnet.env")))
        self.write("bybit_testnet.env", "BYBIT_API_KEY=other\n", sub=os.path.join("profile", ".config", "traider-bot"))
        self.assertIn(os.path.join(".config", "traider-bot"), launcher.find_key_file("testnet", env, "win32"))  # основной путь приоритетнее

    def test_errors_do_not_leak_values(self):
        self.write("bybit_mainnet.env", b"\xff\xfe\x00bad".decode("latin-1") + FAKE)
        for argv in (["bybit"], ["bybit", "nope"], ["bybit", "trade", "balance"]):
            with self.assertRaises(launcher.LauncherError) as cm:
                launcher.prepare_bybit(argv, {"TB_KEYS_DIR": self.d})
            self.assertNotIn(FAKE, str(cm.exception))
        self.assertIn("bybit_mainnet_trade.env", str(cm.exception))

    def test_main_usage_error_and_non_bybit_untouched(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(launcher.main(["bybit", "nope"]), 2)
        self.assertIn("использование", err.getvalue())
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://keepme"}):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                self.assertEqual(launcher.main(["help"]), 0)
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://keepme")  # не bybit: прокси остаются

    def test_subprocess_entry_point(self):
        env = dict(os.environ, PYTHONPATH=_bootstrap.SRC, TB_KEYS_DIR=self.d)
        r = subprocess.run([sys.executable, "-m", "tb.launcher", "help"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0)
        out = r.stdout.decode("utf-8")
        self.assertIn("dash", out)
        self.assertIn("использование", out)  # кириллица в выводе читается как UTF-8
        r = subprocess.run([sys.executable, "-m", "tb.launcher", "bybit", "read", "balance"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 2)
        self.assertIn("нет файла ключа", r.stderr.decode("utf-8"))


class TestFsutil(unittest.TestCase):
    def test_replace_retries_permission_error(self):
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("busy")
        with mock.patch("os.replace", flaky):
            fsutil.replace("a", "b", attempts=5, delay=0)
        self.assertEqual(len(calls), 3)
        calls.clear()
        with mock.patch("os.replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                fsutil.replace("a", "b", attempts=2, delay=0)

    def test_replace_real_files(self):
        d = tempfile.mkdtemp(prefix="tbfs_")
        try:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            for p, t in ((a, "1"), (b, "2")):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(t)
            fsutil.replace(a, b)
            with open(b, encoding="utf-8") as f:
                self.assertEqual(f.read(), "1")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestExecProfile(unittest.TestCase):
    """`exec run --profile trade|testnet` грузит ключ и снимает прокси; paper и остальные exec ничего не трогают. Только синтетические файлы."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tbkeys_")
        for name, body in (("bybit_mainnet_trade.env", "BYBIT_ENV=mainnet\nBYBIT_API_KEY=%s_trade\n" % FAKE),
                           ("bybit_testnet.env", "BYBIT_ENV=testnet\nBYBIT_API_KEY=%s_test\n" % FAKE)):
            with open(os.path.join(self.d, name), "w", encoding="utf-8") as f:
                f.write(body)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_exec_profile_parse(self):
        self.assertEqual(launcher.exec_profile(["exec", "run", "--profile", "trade"]), "trade")
        self.assertEqual(launcher.exec_profile(["exec", "run", "--observe", "--profile=testnet", "--max-seconds", "5"]), "testnet")
        self.assertEqual(launcher.exec_profile(["exec", "run", "--profile", "paper"]), "paper")
        self.assertIsNone(launcher.exec_profile(["exec", "run"]))
        self.assertIsNone(launcher.exec_profile(["exec", "run", "--profile"]))
        self.assertIsNone(launcher.exec_profile(["exec", "status", "--profile", "trade"]))
        self.assertIsNone(launcher.exec_profile(["bybit", "trade", "balance"]))
        self.assertIsNone(launcher.exec_profile([]))

    def test_trade_and_testnet_load_key_and_strip_proxy(self):
        for prof, env_name, suffix in (("trade", "mainnet", "_trade"), ("testnet", "testnet", "_test")):
            env = {"TB_KEYS_DIR": self.d, "HTTPS_PROXY": "http://p", "all_proxy": "x", "KEEP": "1"}
            p, rest = launcher.prepare_exec(["exec", "run", "--profile", prof, "--observe"], env)
            self.assertEqual(p, prof)
            self.assertEqual(rest, ["exec", "run", "--profile", prof, "--observe"])
            self.assertEqual((env["BYBIT_ENV"], env["BYBIT_API_KEY"], env["KEEP"]), (env_name, FAKE + suffix, "1"))
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("all_proxy", env)

    def test_paper_and_other_exec_do_not_touch_env(self):
        for argv in (["exec", "run", "--profile", "paper"], ["exec", "status"], ["exec", "halt"], ["exec", "run"]):
            env = {"TB_KEYS_DIR": self.d, "HTTPS_PROXY": "http://keep"}
            before = dict(env)
            launcher.prepare_exec(argv, env)
            self.assertEqual(env, before, argv)

    def test_missing_key_file_error_has_no_values(self):
        with self.assertRaises(launcher.LauncherError) as cm:
            launcher.prepare_exec(["exec", "run", "--profile", "trade"], {"TB_KEYS_DIR": os.path.join(self.d, "nope")})
        self.assertIn("bybit_mainnet_trade.env", str(cm.exception))
        self.assertNotIn(FAKE, str(cm.exception))

    def test_main_missing_key_returns_2_and_paper_runs_without_key(self):
        empty = tempfile.mkdtemp(prefix="tbkeys_empty_")
        try:
            with mock.patch.dict(os.environ, {"TB_KEYS_DIR": empty}):
                err = io.StringIO()
                with mock.patch.object(sys, "stderr", err):
                    self.assertEqual(launcher.main(["exec", "run", "--profile", "testnet"]), 2)
                self.assertIn("нет файла ключа", err.getvalue())
                err = io.StringIO()
                with tempfile.TemporaryDirectory(prefix="tbroot_") as troot, mock.patch.dict(os.environ, {"TB_ROOT": troot}), \
                        mock.patch.object(sys, "stderr", err):
                    # paper не требует ключа: без --profile отказ кода 2, с --selftest работает во временном корне и завершается сам
                    self.assertEqual(launcher.main(["exec", "run"]), 2)
                    self.assertEqual(launcher.main(["exec", "run", "--profile", "paper", "--selftest", "--max-seconds", "0.3"]), 0)
                self.assertNotIn("нет файла ключа", err.getvalue())
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class TestWrappers(unittest.TestCase):
    def test_wrappers_exist_and_call_launcher(self):
        root = _bootstrap.ROOT
        for name in ("tb", "tb.cmd", "tb.ps1"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                self.assertIn("tb.launcher", f.read(), name)
        with open(os.path.join(root, "tb.cmd"), "rb") as f:
            self.assertNotIn(b"\n", f.read().replace(b"\r\n", b""))  # CRLF для cmd


if __name__ == "__main__":
    unittest.main()
