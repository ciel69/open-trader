"""./tb audit-public: фикстуры во временной папке с ПОДДЕЛЬНЫМИ секретами (собираются конкатенацией, чтобы сам тест не срабатывал). Без сети и реальных ключей."""
import contextlib
import io
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.audit import public

MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\n"
FAKE_SK = "sk-" + "Q7xZk2Lm9PwR4tYv8NcB3hJd6"
FAKE_AKIA = "AKIA" + "Q7ZXK2M9PW4RT8YV"[:16]


def write(root, rel, text="", mode="w"):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, **({"encoding": "utf-8"} if mode == "w" else {})) as f:
        f.write(text)


def run(root, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = public.main(["--root", root] + list(args))
    return code, buf.getvalue()


class AuditPublic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(self.root, ".gitignore", "state/\nruntime/\ndata/\n*.env\nCLAUDE.local.md\n.claude/\n")  # audit: ok
        write(self.root, "LICENSE", MIT)
        write(self.root, "README.md", "hello\n")

    def tearDown(self):
        self.tmp.cleanup()

    def rules(self, root=None):
        return set(f["rule"] for f in public.audit(root or self.root)["findings"])

    def test_clean_tree(self):
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual(public.audit(self.root)["findings"], [])

    def test_secret_patterns_found_and_masked(self):
        write(self.root, "src/a.py", 'K = "%s"\nA = "%s"\napi_key = "%s"\n' % (FAKE_SK, FAKE_AKIA, "Zq9Lm2Xk7Pw4Rt8Yv3Nc5Bh"))
        code, out = run(self.root)
        self.assertEqual(code, 1)
        self.assertTrue({"sk-key", "aws-key", "key-assignment"} <= self.rules())
        for secret in (FAKE_SK, FAKE_AKIA, "Zq9Lm2Xk7Pw4Rt8Yv3Nc5Bh"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret[3:], out)
        code, js = run(self.root, "--json")
        self.assertNotIn(FAKE_SK, js)
        self.assertEqual(json.loads(js)["ok"], False)

    def test_tests_and_docs_downgrade_to_low(self):
        write(self.root, "tests/t.py", 'K = "%s"\n' % FAKE_SK)
        code, _ = run(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(run(self.root, "--strict")[0], 1)

    def test_secret_files(self):
        write(self.root, "deepseek_cfg.token", "x")
        write(self.root, "keys/prod.pem", "x")
        write(self.root, "ignored.env", "x")           # скрыт .gitignore
        fs = [f["file"] for f in public.audit(self.root)["findings"] if f["rule"] == "secret-file"]
        self.assertEqual(sorted(fs), ["deepseek_cfg.token", "keys/prod.pem"])

    def test_personal_data(self):
        write(self.root, "docs/a.md", "cd /Users/realname/proj\nC:\\Users\\Someguy\\x\nmail: bob.smith@gmail.com\nhost 203.0.113.9 and 8.8.4.4\n")  # audit: ok
        write(self.root, "src/b.py", 'W = "0x%s"\nOK_LINE = "me@example.com /Users/x/y"\n' % "ab12cd34ef56ab78cd90ef12ab34cd56ef78ab90")  # audit: ok
        r = self.rules()
        self.assertTrue({"home-path", "email", "ip-address", "wallet-address"} <= r, r)
        masked = json.dumps(public.audit(self.root)["findings"])
        self.assertNotIn("realname", masked)
        self.assertNotIn("bob.smith", masked)

    def test_extra_term(self):
        write(self.root, "x.md", "written by Zorbax Quux\n")
        res = public.audit(self.root, ["zorbax"])
        self.assertIn("personal-term", [f["rule"] for f in res["findings"]])
        self.assertNotIn("Zorbax", json.dumps(res))

    def test_private_dirs_must_be_ignored(self):
        write(self.root, "state/trades.jsonl", "secret trade\n")          # игнорируется
        self.assertEqual(public.audit(self.root)["findings"], [])
        write(self.root, "logs_dump/x.txt", "x")
        os.remove(os.path.join(self.root, ".gitignore"))
        write(self.root, ".gitignore", "*.env\n")
        res = public.audit(self.root)
        rules = [(f["rule"], f["file"]) for f in res["findings"]]
        self.assertIn(("private-dir-not-ignored", "state/"), rules)
        self.assertEqual(run(self.root)[0], 1)

    def test_missing_gitignore_and_license(self):
        os.remove(os.path.join(self.root, ".gitignore"))
        os.remove(os.path.join(self.root, "LICENSE"))
        self.assertTrue({"missing-gitignore", "missing-license"} <= self.rules())
        write(self.root, "LICENSE", "All rights reserved\n")
        self.assertIn("nonstandard-license", self.rules())

    def test_large_binary_junk(self):
        write(self.root, "big.txt", "a" * (public.MAX_TEXT + 10))
        write(self.root, "bin.dat", b"\x00\x01\x02", mode="wb")
        write(self.root, ".DS_Store", "x")
        r = self.rules()
        self.assertTrue({"large-file", "binary-file", "os-junk"} <= r, r)

    def test_gitignore_semantics(self):
        rules = public.parse_gitignore(self.root)
        self.assertTrue(public.ignored("state/x/y.json", False, rules))
        self.assertTrue(public.ignored("a/b/k.env", False, rules))
        self.assertFalse(public.ignored("src/state.py", False, rules))
        self.assertFalse(public.ignored("docs/state", False, [("state", False, True, False)]))   # правило только для каталогов

    def test_read_only(self):
        write(self.root, "a.py", 'K = "%s"\n' % FAKE_SK)
        before = sorted(os.listdir(self.root)), os.path.getmtime(os.path.join(self.root, "a.py"))
        run(self.root)
        self.assertEqual(before, (sorted(os.listdir(self.root)), os.path.getmtime(os.path.join(self.root, "a.py"))))

    def test_bad_arg(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(public.main(["--nope"]), 2)


if __name__ == "__main__":
    unittest.main()
