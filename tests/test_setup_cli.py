"""`./tb setup` (помощник владельца): временный каталог ключей и корень; значения секретов нигде не печатаются; существующее не перезаписывается без «да»."""
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import _bootstrap  # noqa: F401
from tb import setup_cli
from tb.control import auth

SECRET = "sk-test-SECRET-VALUE-1234567890"


class Setup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbsetup_root_")
        self.keys = os.path.join(tempfile.mkdtemp(prefix="tbsetup_keys_"), "tb")       # каталога ещё нет
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.keys), True)
        os.makedirs(os.path.join(self.root, "config"))
        for n in ("agents.json", "providers.json"):
            shutil.copy(os.path.join(_bootstrap.ROOT, "config", n), os.path.join(self.root, "config", n))
        self.env = {"TB_KEYS_DIR": self.keys, "HOME": self.keys}
        self.lines = []

    def helper(self, answers, secrets=()):
        a, s = list(answers), list(secrets)
        return setup_cli.Helper(self.root, self.env, ask=lambda q: a.pop(0) if a else "", secret=lambda q: s.pop(0) if s else "", out=self.lines.append)

    def text(self):
        return "\n".join(self.lines)

    def test_full_run_creates_everything_without_leaking(self):
        h = self.helper(["да", "да", "да", "1"], secrets=[SECRET])
        self.assertEqual(h.run(), 0)
        st = auth.status(self.env)
        self.assertTrue(st["key"] and st["token"])
        self.assertEqual(st["perms_ok"], True if os.name == "posix" else None)       # права доступа к файлу проверяются только на POSIX
        self.assertTrue(setup_cli.has_provider_key(self.root, self.env))
        a = setup_cli.agent_state(self.root, self.env)
        self.assertTrue(a["hash"] and a["file"])
        with open(os.path.join(self.keys, "deepseek.env")) as f:
            self.assertEqual(f.read(), "DEEPSEEK_API_KEY=%s\n" % SECRET)
        if os.name == "posix":                      # биты доступа есть только на POSIX (на Windows os.stat даёт 0o666 всегда)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.keys, "deepseek.env")).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.keys, "agents", "api.token")).st_mode), 0o600)
        out = self.text()
        self.assertNotIn(SECRET, out)
        with open(os.path.join(self.keys, "agents", "api.token")) as f:
            self.assertNotIn(f.read().strip(), out)                      # токен агента на экран не выводится
        with open(os.path.join(self.root, "config", "agents.json")) as f:
            self.assertTrue(json.load(f)["agents"]["api"]["token_hash"])
        self.assertIn("ТОКЕН", out)                                       # токен дашборда показан один раз
        rows = dict((n, ok) for n, ok, _ in setup_cli.collect(self.root, self.env))
        self.assertTrue(all(v for v in rows.values() if v is not None), rows)

    def test_bybit_key_written_hidden_with_right_format_and_mode(self):
        key, sec = "AbCd1234EfGh5678", "SeCrEtSeCrEt0123456789abcdefgh01"
        h = self.helper([], secrets=[key, sec])
        h.step_dir()
        self.assertTrue(h.step_bybit_key("testnet"))
        p = os.path.join(self.keys, "bybit_testnet.env")
        with open(p) as f:
            self.assertEqual(f.read(), "BYBIT_ENV=testnet\nBYBIT_API_KEY=%s\nBYBIT_API_SECRET=%s\n" % (key, sec))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertNotIn(key, self.text())
        self.assertNotIn(sec, self.text())                                         # значения на экран не выводятся
        from tb import launcher
        self.assertTrue(launcher.key_status("testnet", self.env)["ok"])
        self.assertTrue(setup_cli.collect(self.root, self.env))                   # таблица строится и с ключом Bybit

    def test_bybit_mainnet_profiles_use_mainnet_env(self):
        h = self.helper([], secrets=["AbCd1234EfGh5678", "SeCrEtSeCrEt0123456789abcdefgh01", "ZzZz9999YyYy8888", "QqQqQqQq77776666555544443333"])
        h.step_dir()
        self.assertTrue(h.step_bybit_key("read"))
        self.assertTrue(h.step_bybit_key("trade"))
        for name in ("bybit_mainnet.env", "bybit_mainnet_trade.env"):
            with open(os.path.join(self.keys, name)) as f:
                self.assertTrue(f.read().startswith("BYBIT_ENV=mainnet\n"), name)

    def test_bybit_empty_or_bad_value_writes_nothing_and_existing_kept(self):
        h = self.helper([], secrets=["", "x"])
        h.step_dir()
        self.assertFalse(h.step_bybit_key("trade"))
        self.assertFalse(os.path.exists(os.path.join(self.keys, "bybit_mainnet_trade.env")))
        h2 = self.helper(["да"], secrets=["AbCd1234EfGh5678", "SeCrEtSeCrEt0123456789abcdefgh01"])
        self.assertTrue(h2.step_bybit_key("read"))
        with open(os.path.join(self.keys, "bybit_mainnet.env")) as f:
            before = f.read()
        h3 = self.helper([""], secrets=["ZzZz9999YyYy8888", "QqQqQqQq77776666555544443333"])   # Enter = не перезаписывать
        self.assertFalse(h3.step_bybit_key("read"))
        with open(os.path.join(self.keys, "bybit_mainnet.env")) as f:
            self.assertEqual(f.read(), before)

    def test_bybit_cli_requires_tty_and_valid_profile(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(setup_cli.main(["bybit", "--profile", "nope"]), 2)
            self.assertEqual(setup_cli.main(["bybit", "extra"]), 2)

    def test_existing_not_overwritten_without_yes(self):
        self.helper(["да", "да", "да", "1"], secrets=[SECRET]).run()
        with open(os.path.join(self.keys, "control.json")) as f:
            before = f.read()
        with open(os.path.join(self.keys, "deepseek.env")) as f:
            kbefore = f.read()
        self.lines[:] = []
        self.helper(["", "", "", ""], secrets=["другой-ключ"]).run()     # Enter на каждом вопросе = нет
        with open(os.path.join(self.keys, "control.json")) as f:
            self.assertEqual(f.read(), before)
        with open(os.path.join(self.keys, "deepseek.env")) as f:
            self.assertEqual(f.read(), kbefore)
        self.assertNotIn("другой-ключ", self.text())

    def test_template_only_and_empty_secret(self):
        h = self.helper(["2"])
        h.step_dir()
        self.assertTrue(h.step_provider_key())
        p = os.path.join(self.keys, "deepseek.env")
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(setup_cli.has_provider_key(self.root, self.env))      # пустой шаблон ключом не считается
        h2 = self.helper(["1"], secrets=[""])
        self.assertFalse(h2.step_provider_key())

    def test_permissions_fixed(self):
        h = self.helper(["да"])
        h.step_dir()
        h.step_sign_key()
        os.chmod(os.path.join(self.keys, "control.json"), 0o644)
        self.assertFalse(setup_cli.perms_ok(os.path.join(self.keys, "control.json")))
        h.step_perms()
        self.assertEqual(setup_cli.perms_ok(os.path.join(self.keys, "control.json")), True if os.name == "posix" else None)   # на Windows права не проверяются (None)

    def test_cli_needs_tty_and_check_works(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(setup_cli.main([]), 2)                      # unittest: не TTY
        self.assertIn("--check", err.getvalue())
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(setup_cli.main(["--check"]), 0)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn("Ключ подписи", out.getvalue())
        self.assertFalse(os.path.exists(self.keys))                     # --check ничего не создаёт


if __name__ == "__main__":
    unittest.main()
