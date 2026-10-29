"""WP12: протокол, подпись, токен, nonce, аудит-цепочка, права файла ключа. Без сети, во временных TB_ROOT и TB_KEYS_DIR."""
import json
import os
import stat
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb.control import audit, auth, client, protocol

NOW = _ctl.NOW


class TestProtocol(unittest.TestCase):
    def test_ids_and_digest(self):
        i = protocol.new_id(NOW)
        self.assertRegex(i, protocol.RE_ID.pattern)
        d1 = protocol.args_digest("day.start", {"mode": "live", "accepted": ["W1"]})
        d2 = protocol.args_digest("day.start", {"mode": "live", "accepted": []})
        self.assertEqual(d1, d2)                                  # принятые предупреждения не входят в привязку nonce
        self.assertNotEqual(d1, protocol.args_digest("day.start", {"mode": "paper"}))
        self.assertNotEqual(d1, protocol.args_digest("day.stop", {"mode": "live"}))

    def test_args_matrix(self):
        bad = [("day.start", {}), ("day.start", {"mode": "x"}), ("day.start", {"mode": "paper", "session": "zzz"}),
               ("day.start", {"mode": "live", "accepted": "W1"}), ("risk.set", {"level": 0}),
               ("risk.set", {"level": "3"}), ("mode.set", {"mode": "x"}), ("carry.decide", {"plan": "p", "decision": "x"}), ("day.stop", {"mode": "hard"}),
               ("nope", {}), ("challenge", {"action": "challenge", "args": {}}), ("challenge", {"action": "day.start", "args": {"mode": "bad"}}), ("day.start", [])]
        for a, args in bad:
            self.assertFalse(protocol.validate_args(a, args)[0], (a, args))
        ok, _m, a = protocol.validate_args("day.start", {"mode": "paper"})
        self.assertTrue(ok)
        self.assertEqual((a["session"], a["allow_amber"], a["accepted"]), ("day", False, []))
        self.assertNotIn("minutes", a)                                            # срока у дня нет
        self.assertEqual(protocol.validate_args("day.start", a)[2], a)             # нормализация идемпотентна

    def test_validate_command_structure(self):
        e = _ctl.Env(self)
        good = client.build("day.stop", {"mode": "soft"}, "dash", "s-abcd1234", now=NOW)
        self.assertEqual(protocol.validate_command(good), (True, "OK"))
        for k, v in (("schema", "x"), ("id", "bad"), ("ts", "1"), ("src", "web"), ("mac", "zz"), ("action", "evil"), ("sess", "no")):
            c = dict(good)
            c[k] = v
            self.assertFalse(protocol.validate_command(c)[0], k)
        self.assertFalse(protocol.validate_command([])[0])
        big = dict(good, args={"x": "y" * 9000})
        self.assertFalse(protocol.validate_command(big)[0])

    def test_error_texts_have_no_paths_or_secrets(self):
        for code, text in protocol.CODES.items():
            self.assertNotRegex(text, r"[/\\]|Traceback|Error|\.json")


class TestAuth(unittest.TestCase):
    def test_key_token_and_hash_only(self):
        e = _ctl.Env(self, with_key=False)
        self.assertEqual(auth.status(), {"key": False, "token": False, "perms_ok": None})
        self.assertIsNone(auth.new_token())                        # без ключа токен не создаётся
        self.assertTrue(auth.init_key())
        raw1 = _ctl.rd(auth.control_file())
        self.assertFalse(auth.init_key())                          # существующий ключ не перезаписывается
        self.assertEqual(raw1, _ctl.rd(auth.control_file()))
        tok = auth.new_token()
        raw = _ctl.rd(auth.control_file())
        self.assertNotIn(tok, raw)                                 # в файле только хеш
        d = json.loads(raw)
        self.assertEqual(set(d) - {"created", "token_created"}, {"hmac_key", "salt", "token_hash"})
        self.assertTrue(auth.verify_token(tok))
        for bad in ("", "x", tok + "x", tok[:-1], None, 5, "A" * 500):
            self.assertFalse(auth.verify_token(bad))
        ep = auth.token_epoch()
        tok2 = auth.new_token()                                    # ротация
        self.assertFalse(auth.verify_token(tok))
        self.assertTrue(auth.verify_token(tok2))
        self.assertNotEqual(ep, auth.token_epoch())
        self.assertEqual(json.loads(_ctl.rd(auth.control_file()))["hmac_key"], d["hmac_key"])   # ключ подписи не менялся

    @unittest.skipUnless(os.name == "posix", "права POSIX")
    def test_file_permissions_0600(self):
        _ctl.Env(self)
        mode = stat.S_IMODE(os.stat(auth.control_file()).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(auth.status()["perms_ok"])
        os.chmod(auth.control_file(), 0o644)
        self.assertFalse(auth.status()["perms_ok"])

    def test_sign_verify_tamper(self):
        _ctl.Env(self)
        c = client.build("risk.set", {"level": 2, "accepted": []}, "dash", "s-abcd1234", now=NOW)
        self.assertTrue(auth.verify_signed(c))
        for k, v in (("action", "day.start"), ("args", {"level": 5, "accepted": []}), ("ts", NOW + 1), ("src", "cli"), ("sess", "s-ffff0000"), ("nonce", "x"), ("id", "K-20261027T130000-aaaaaa")):
            t = dict(c)
            t[k] = v
            self.assertFalse(auth.verify_signed(t), k)
        t = dict(c)
        del t["mac"]
        self.assertFalse(auth.verify_signed(t))
        self.assertEqual(auth.verify_signed(dict(c, extra=1)), False)       # добавленное поле меняет подпись
        # порядок ключей не важен
        shuffled = dict(reversed(list(c.items())))
        self.assertTrue(auth.verify_signed(shuffled))

    def test_no_key_means_no_signature(self):
        _ctl.Env(self, with_key=False)
        self.assertIsNone(client.build("day.stop", {}, "dash", None, now=NOW))
        self.assertFalse(auth.verify_signed({"a": 1, "mac": "0" * 64}))

    def test_other_key_does_not_verify(self):
        _ctl.Env(self)
        c = client.build("day.stop", {"mode": "soft"}, "dash", None, now=NOW)
        os.remove(auth.control_file())
        auth.init_key()                                            # новый ключ
        self.assertFalse(auth.verify_signed(c))

    def test_nonce_rules(self):
        _ctl.Env(self)
        dg = protocol.args_digest("day.start", {"mode": "live"})
        n = auth.make_nonce("s-1", dg, NOW)
        ck = lambda **k: auth.check_nonce(k.get("n", n), k.get("sess", "s-1"), k.get("dg", dg), k.get("now", NOW + 3), 60, 2)
        self.assertEqual(ck(), (True, "ok"))
        self.assertEqual(ck(now=NOW + 1)[1], "early")
        self.assertEqual(ck(now=NOW + 61)[1], "late")
        self.assertEqual(ck(sess="s-2")[1], "mac")
        self.assertEqual(ck(dg=protocol.args_digest("day.start", {"mode": "paper"}))[1], "mac")
        self.assertEqual(ck(n="1.2")[1], "format")
        self.assertEqual(ck(n=n[:-1] + ("0" if n[-1] != "0" else "1"))[1], "mac")
        self.assertEqual(ck(n=None)[1], "format")


class TestAudit(unittest.TestCase):
    def test_chain_and_tamper(self):
        e = _ctl.Env(self)
        for i in range(5):
            audit.append(e.root, "K-%d" % i, "dash", "s-1", "day.stop", "d", "ok", "OK", now=NOW + i)
        v = audit.verify(e.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["n"], 5)
        path = e.p("state", "control", "audit.jsonl")
        lines = _ctl.rd(path).splitlines()
        # подмена
        bad = lines[:]
        bad[2] = bad[2].replace('"code":"OK"', '"code":"E_X"')
        _ctl.wr(path, "\n".join(bad) + "\n")
        self.assertEqual(audit.verify(e.root)["bad_seq"], 3)
        # удаление записи
        _ctl.wr(path, "\n".join(lines[:2] + lines[3:]) + "\n")
        self.assertFalse(audit.verify(e.root)["ok"])
        # перестановка
        _ctl.wr(path, "\n".join([lines[1], lines[0]] + lines[2:]) + "\n")
        self.assertFalse(audit.verify(e.root)["ok"])

    def test_truncated_line_and_unwritable(self):
        e = _ctl.Env(self)
        audit.append(e.root, "K-1", "dash", None, "x", "d", "ok", "OK", now=NOW)
        with open(e.p("state", "control", "audit.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "h": "ab')                          # оборванная запись
        audit.append(e.root, "K-2", "dash", None, "x", "d", "ok", "OK", now=NOW)
        v = audit.verify(e.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["broken"], 1)
        self.assertEqual(v["n"], 2)


class TestClient(unittest.TestCase):
    def test_submit_is_atomic_and_only_in_inbox(self):
        e = _ctl.Env(self)
        c = client.build("day.stop", {"mode": "soft"}, "dash", None, now=NOW)
        cid = client.submit(c, e.root)
        files = os.listdir(e.p("state", "control", "_inbox"))
        self.assertEqual(files, [cid + ".json"])
        self.assertEqual(json.loads(_ctl.rd(e.p("state", "control", "_inbox", cid + ".json"))), c)

    def test_status_stale_and_missing(self):
        e = _ctl.Env(self)
        s = client.read_status(e.root, NOW)
        self.assertTrue(s["stale"])
        e.w("state/control/status.json", {"ts": NOW - 20, "ctl": {"alive": True}, "platform": {"state": "running"}})
        self.assertTrue(client.read_status(e.root, NOW)["stale"])
        self.assertFalse(client.read_status(e.root, NOW - 15)["stale"])
        self.assertIsNone(client.read_result("../../etc/passwd", e.root))


if __name__ == "__main__":
    unittest.main()


class TestDayType(unittest.TestCase):
    def test_day_accepted_and_horizons_kept(self):
        for st in ("day", "scalp", "intraday", "swing", "position"):
            ok, _m, a = protocol.validate_args("day.start", {"mode": "paper", "session": st, "minutes": 10})
            self.assertTrue(ok, st)
            self.assertEqual(a["session"], st)

