"""Вход в дашборд без токена (auth.mode=open_local) и токен один раз для опасных действий; автосоздание ключа и токена.
Проверки Origin/Host/CSRF остаются в обоих режимах. Временные TB_ROOT и TB_KEYS_DIR, без сети, настоящие ключи не затрагиваются."""
import json
import os
import stat
import tempfile
import shutil
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb.control import auth, cli
from tb.dash import ctlweb

NOW = _ctl.NOW
PORT = 8777
DAY = 86400


class H(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), d)


def hdr(**over):
    h = {"host": "127.0.0.1:%d" % PORT, "origin": "http://127.0.0.1:%d" % PORT, "content-type": "application/json", "sec-fetch-site": "same-origin"}
    for k, v in over.items():
        k = k.replace("_", "-")
        if v is None:
            h.pop(k, None)
        else:
            h[k] = v
    return H(h)


def body(o):
    return json.dumps(o).encode("utf-8")


class Base(unittest.TestCase):
    mode = "open_local"

    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.w("config/control.json", {"version": 1, "auth": {"mode": self.mode, "remember_days": 30}})
        self.web = ctlweb.CtlWeb(self.e.root)

    def open_session(self, web=None, now=NOW):
        code, obj, extra = (web or self.web).get("/api/ctl/session", {}, hdr(), now)
        self.assertEqual(code, 200)
        return ((extra.get("Set-Cookie") or "").split(";")[0], obj)

    def post(self, path, data, ck, csrf, web=None, now=NOW, **over):
        return (web or self.web).post(path, hdr(cookie=ck, x_tb_csrf=csrf, **over), body(data), PORT, now)

    def login(self, web=None, now=NOW, **data):
        d = {"token": self.e.token}
        d.update(data)
        return (web or self.web).post("/api/ctl/login", hdr(), body(d), PORT, now)


class TestOpenLocal(Base):
    def test_session_without_token_and_cookie_flags(self):
        ck, obj = self.open_session()
        self.assertTrue(ck.startswith("tb_sess="))
        self.assertEqual((obj["authed"], obj["mode"], obj["level"], obj["full"]), (True, "open_local", "open", False))
        self.assertTrue(obj["configured"] and len(obj["csrf"]) >= 20)
        _c, _o, extra = self.web.get("/api/ctl/session", {}, hdr(), NOW)
        for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, extra["Set-Cookie"])
        # с cookie повторной выдачи нет
        c2, o2, e2 = self.web.get("/api/ctl/session", {}, hdr(cookie=ck), NOW)
        self.assertEqual((o2["csrf"], e2), (obj["csrf"], {}))

    def test_no_session_for_cross_site_fetch(self):
        c, o, extra = self.web.get("/api/ctl/session", {}, hdr(sec_fetch_site="cross-site"), NOW)
        self.assertEqual((o["authed"], "csrf" in o, extra), (False, False, {}))
        c, o, extra = self.web.get("/api/ctl/session", {}, hdr(sec_fetch_site="same-site"), NOW)
        self.assertFalse(o["authed"])

    def test_safe_actions_pass_without_token(self):
        self.e.w("state/control/status.json", {"schema": "tb.ctlstatus/1", "ts": NOW, "platform": {"state": "running", "mode": "paper"}})
        ck, o = self.open_session()
        for action, args in (("day.stop", {"mode": "soft"}), ("intel.refresh", {}), ("notify.test", {}), ("mode.set", {"mode": "observe"}),
                             ("day.start", {"mode": "paper", "session": "intraday", "minutes": 60}), ("day.start", {"mode": "testnet", "session": "scalp", "minutes": 30}),
                             ("brain.reload", {}), ("report.ack", {"day_id": "d1"})):
            code, obj, _ = self.post("/api/ctl/command", {"action": action, "args": args}, ck, o["csrf"])
            self.assertEqual((code, obj.get("ok")), (200, True), (action, obj))

    def test_kill_from_open_session(self):
        ck, o = self.open_session()
        code, obj, _ = self.post("/api/ctl/command", {"action": "kill.set", "args": {"reason": "тест"}}, ck, o["csrf"])
        self.assertEqual((code, obj["killed"]), (200, True))
        self.assertTrue(os.path.exists(os.path.join(self.e.root, "state", "KILL")))

    def test_dangerous_actions_need_token(self):
        ck, o = self.open_session()
        live = {"mode": "live", "session": "intraday", "minutes": 60}
        for action, args in (("day.start", live), ("mode.set", {"mode": "live"}), ("risk.set", {"level": 4}), ("risk.set", {"level": 1}),
                             ("brain.use", {"role": "live", "profile": "ds-pro"}), ("brain.use", {"team": "t1"}), ("brain.rollback", {}), ("platform.shutdown", {}),
                             ("carry.decide", {"plan": "p1", "decision": "carry"})):
            for path in ("/api/ctl/challenge", "/api/ctl/command"):
                code, obj, _ = self.post(path, {"action": action, "args": args}, ck, o["csrf"])
                self.assertEqual((code, obj["code"]), (401, "E_ELEVATE"), (path, action, args))
        # любая команда с nonce: токен обязателен, даже если действие со статического списка не опасное
        code, obj, _ = self.post("/api/ctl/command", {"action": "day.stop", "args": {}, "nonce": "1.a.b"}, ck, o["csrf"])
        self.assertEqual((code, obj["code"]), (401, "E_ELEVATE"))
        self.assertIn("E_ELEVATE", [a["code"] for a in self.e.audit()])
        self.assertFalse(os.listdir(os.path.join(self.e.root, "state", "control", "_inbox")) if os.path.isdir(os.path.join(self.e.root, "state", "control", "_inbox")) else False)

    def test_ctl_marked_danger_blocks_open_session(self):
        # ctl сам сказал «опасно» (например, смена в live-режиме платформы): сессия open отказана, nonce не регистрируется
        ck, o = self.open_session()
        self.e.w("state/control/status.json", {"schema": "tb.ctlstatus/1", "ts": NOW, "platform": {"state": "running", "mode": "live"}})
        code, obj, _ = self.post("/api/ctl/challenge", {"action": "brain.use", "args": {"profile": "ds-pro"}}, ck, o["csrf"], now=NOW)
        self.assertEqual((code, obj["code"]), (401, "E_ELEVATE"))

    def test_unknown_action_needs_full_by_default(self):
        self.assertTrue(ctlweb.needs_full("provider.add_key", {}, "paper"))
        self.assertTrue(ctlweb.needs_full("risk.set", {"level": 1}, "paper"))
        self.assertFalse(ctlweb.needs_full("brain.use", {"profile": "x"}, "paper"))
        self.assertTrue(ctlweb.needs_full("brain.use", {"profile": "x"}, None))       # режим неизвестен = как live
        self.assertTrue(ctlweb.needs_full("day.start", {"mode": "live"}, "paper"))
        for a in ("brain.role", "brain.profile", "llm.provider"):
            self.assertFalse(ctlweb.needs_full(a, {"op": "chain"}, "paper"), a)
            self.assertTrue(ctlweb.needs_full(a, {"op": "chain"}, "live"), a)
            self.assertTrue(ctlweb.needs_full(a, {"op": "chain"}, None), a)
        self.assertTrue(ctlweb.needs_full("exchange.set", {"op": "mode"}, "paper"))

    def test_origin_host_csrf_still_enforced(self):
        ck, o = self.open_session()
        act = {"action": "intel.refresh", "args": {}}
        for name, over, want in (("origin чужой", dict(origin="http://evil.example.com"), "E_ORIGIN"), ("host чужой", dict(host="evil.example.com:%d" % PORT), "E_ORIGIN"),
                                 ("rebinding", dict(host="127.0.0.1.evil.com:%d" % PORT), "E_ORIGIN"), ("origin нет", dict(origin=None), "E_ORIGIN"),
                                 ("sec-fetch cross", dict(sec_fetch_site="cross-site"), "E_ORIGIN"), ("ctype", dict(content_type="text/plain"), "E_CTYPE")):
            code, obj, _ = self.post("/api/ctl/command", act, ck, o["csrf"], **over)
            self.assertEqual(obj["code"], want, name)
        code, obj, _ = self.post("/api/ctl/command", act, ck, "x" * 24)
        self.assertEqual((code, obj["code"]), (403, "E_CSRF"))
        code, obj, _ = self.post("/api/ctl/command", act, "tb_sess=" + "y" * 43, o["csrf"])
        self.assertEqual((code, obj["code"]), (401, "E_AUTH"))
        code, obj, _ = self.web.post("/api/ctl/command", hdr(), body(act), PORT, NOW)      # без cookie и CSRF
        self.assertEqual((code, obj["code"]), (401, "E_AUTH"))

    def test_login_origin_checked_and_lockout(self):
        code, obj, _ = self.web.post("/api/ctl/login", hdr(origin="http://evil.example.com"), body({"token": self.e.token}), PORT, NOW)
        self.assertEqual((code, obj["code"]), (403, "E_ORIGIN"))
        for i in range(5):
            self.login(token="bad", now=NOW + i)
        code, obj, _ = self.login(now=NOW + 6)
        self.assertEqual((code, obj["code"]), (429, "E_BLOCKED"))

    def test_login_remembers_browser_and_survives_restart(self):
        code, obj, extra = self.login()
        self.assertEqual((code, obj["ok"], obj["level"], obj["remembered"]), (200, True, "full", True))
        c = extra["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Max-Age=%d" % (30 * DAY)):
            self.assertIn(part, c)
        ck, csrf = c.split(";")[0], obj["csrf"]
        self.assertNotIn(self.e.token, json.dumps(obj) + c)
        live = {"mode": "live", "session": "intraday", "minutes": 60}
        # токен введён: опасное проходит проверку уровня (дальше решает ctl; здесь он не запущен: 504)
        r = self.post("/api/ctl/challenge", {"action": "day.start", "args": live}, ck, csrf, now=NOW + 5)
        self.assertNotEqual(r[1].get("code"), "E_ELEVATE")
        # перезапуск дашборда: новый объект CtlWeb, та же cookie
        web2 = ctlweb.CtlWeb(self.e.root)
        c2, s, extra2 = web2.get("/api/ctl/session", {}, hdr(cookie=ck), NOW + 10 * DAY)
        self.assertEqual((s["authed"], s["full"], s["remembered"], s["csrf"], extra2), (True, True, True, csrf, {}))
        r = self.post("/api/ctl/challenge", {"action": "risk.set", "args": {"level": 4}}, ck, csrf, web=web2, now=NOW + 10 * DAY)
        self.assertNotEqual(r[1].get("code"), "E_ELEVATE")
        # CSRF обязателен и у запомненной сессии
        r = self.post("/api/ctl/command", {"action": "intel.refresh", "args": {}}, ck, "z" * 24, web=web2, now=NOW + 10 * DAY)
        self.assertEqual(r[1]["code"], "E_CSRF")
        # чужой Origin у запомненной сессии
        r = self.post("/api/ctl/command", {"action": "intel.refresh", "args": {}}, ck, csrf, web=web2, now=NOW + 10 * DAY, origin="http://evil.example.com")
        self.assertEqual(r[1]["code"], "E_ORIGIN")

    def test_remember_expires_and_token_rotation_revokes(self):
        _c, obj, extra = self.login()
        ck = extra["Set-Cookie"].split(";")[0]
        late = ctlweb.CtlWeb(self.e.root)
        _c, s, _e = late.get("/api/ctl/session", {}, hdr(cookie=ck), NOW + 31 * DAY)
        self.assertFalse(s.get("full"))                                # срок 30 суток истёк: обычная сессия open
        self.assertEqual(s["level"], "open")
        auth.new_token()                                               # новый токен отзывает запомненные браузеры
        web3 = ctlweb.CtlWeb(self.e.root)
        _c, s, _e = web3.get("/api/ctl/session", {}, hdr(cookie=ck), NOW + DAY)
        self.assertFalse(s.get("full"))
        r = self.web.post("/api/ctl/challenge", hdr(cookie=ck, x_tb_csrf=obj["csrf"]), body({"action": "risk.set", "args": {"level": 4}}), PORT, NOW + DAY)
        self.assertEqual(r[1]["code"], "E_AUTH")                        # мёртвая cookie: страница получит новую сессию open при ближайшем опросе
        _c, s2, extra2 = self.web.get("/api/ctl/session", {}, hdr(cookie=ck), NOW + DAY)
        self.assertEqual((s2["level"], "tb_sess=" in extra2.get("Set-Cookie", "")), ("open", True))

    def test_forged_remember_cookie_rejected(self):
        _c, obj, extra = self.login()
        ck = extra["Set-Cookie"].split(";")[0].split("=", 1)[1]
        rnd, exp, mac = ck.split(".")
        for bad in ("%s.%d.%s" % (rnd, int(exp) + DAY, mac), "%s.%s.%s" % ("0" * 12, exp, mac), "%s.%s.%s" % (rnd, exp, "0" * 32), rnd + "." + exp):
            web = ctlweb.CtlWeb(self.e.root)
            s = web.get("/api/ctl/session", {}, hdr(cookie="tb_sess=" + bad), NOW)[1]
            self.assertFalse(s.get("full"), bad)

    def test_login_without_remember_is_memory_session(self):
        _c, obj, extra = self.login(remember=False)
        self.assertFalse(obj["remembered"])
        self.assertIn("Max-Age=%d" % 3600, extra["Set-Cookie"])
        ck = extra["Set-Cookie"].split(";")[0]
        web2 = ctlweb.CtlWeb(self.e.root)       # после рестарта дашборда такая сессия не восстанавливается
        self.assertFalse(web2.get("/api/ctl/session", {}, hdr(cookie=ck), NOW)[1].get("full"))

    def test_logout_clears_cookie(self):
        _c, obj, extra = self.login()
        ck = extra["Set-Cookie"].split(";")[0]
        code, o2, e2 = self.post("/api/ctl/logout", {}, ck, obj["csrf"])
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", e2["Set-Cookie"])

    def test_remember_cookie_ignored_in_token_mode(self):
        _c, obj, extra = self.login()
        ck = extra["Set-Cookie"].split(";")[0]
        self.e.w("config/control.json", {"version": 1, "auth": {"mode": "token"}})
        web2 = ctlweb.CtlWeb(self.e.root)
        s = web2.get("/api/ctl/session", {}, hdr(cookie=ck), NOW)[1]
        self.assertEqual((s["authed"], s["mode"]), (False, "token"))

    def test_no_token_configured_still_open_but_login_refused(self):
        e2 = _ctl.Env(self, with_token=False)
        e2.w("config/control.json", {"version": 1, "auth": {"mode": "open_local"}})
        web = ctlweb.CtlWeb(e2.root)
        _ck, s = self.open_session(web)
        self.assertTrue(s["authed"] and s["configured"] and not s["token"])
        r = web.post("/api/ctl/login", hdr(), body({"token": "x"}), PORT, NOW)
        self.assertEqual((r[0], r[1]["code"]), (503, "E_CONFIG"))

    def test_no_key_means_not_configured(self):
        e2 = _ctl.Env(self, with_key=False)
        web = ctlweb.CtlWeb(e2.root)
        code, s, extra = web.get("/api/ctl/session", {}, hdr(), NOW)
        self.assertEqual((s["authed"], s["configured"], extra), (False, False, {}))

    def test_no_secrets_in_session_answer(self):
        ck, o = self.open_session()
        key = json.loads(_ctl.rd(auth.control_file()))
        txt = json.dumps(o) + json.dumps(self.web.get("/api/ctl/status", {}, hdr(cookie=ck), NOW)[1])
        for sec in (self.e.token, key["hmac_key"], key["token_hash"], key["salt"]):
            self.assertNotIn(sec, txt)


class TestTokenModeUnchanged(Base):
    mode = "token"

    def test_no_session_without_login(self):
        code, s, extra = self.web.get("/api/ctl/session", {}, hdr(), NOW)
        self.assertEqual((s["authed"], s["mode"], extra), (False, "token", {}))
        r = self.web.post("/api/ctl/command", hdr(), body({"action": "intel.refresh", "args": {}}), PORT, NOW)
        self.assertEqual((r[0], r[1]["code"]), (401, "E_AUTH"))

    def test_login_is_memory_session_not_remembered(self):
        code, obj, extra = self.login()
        self.assertEqual((code, obj["remembered"]), (200, False))
        self.assertIn("Max-Age=3600", extra["Set-Cookie"])
        ck = extra["Set-Cookie"].split(";")[0]
        self.assertFalse(ctlweb.CtlWeb(self.e.root).get("/api/ctl/session", {}, hdr(cookie=ck), NOW)[1]["authed"])

    def test_dangerous_allowed_after_login(self):
        _c, obj, extra = self.login()
        r = self.post("/api/ctl/challenge", {"action": "risk.set", "args": {"level": 4}}, extra["Set-Cookie"].split(";")[0], obj["csrf"])
        self.assertNotEqual(r[1].get("code"), "E_ELEVATE")

    def test_unknown_mode_value_falls_back_to_token(self):
        self.e.w("config/control.json", {"version": 1, "auth": {"mode": "whatever"}})
        self.assertEqual(ctlweb.CtlWeb(self.e.root).mode(), "token")
        self.e.w("config/control.json", {"version": 1, "auth": "open_local"})
        self.assertEqual(ctlweb.CtlWeb(self.e.root).mode(), "token")


class TestEnsure(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="tbkeys_")
        self.addCleanup(shutil.rmtree, self.keys, True)
        self.env = {"TB_KEYS_DIR": self.keys}

    def read(self):
        return json.loads(_ctl.rd(auth.control_file(self.env)))

    def test_first_run_creates_key_and_token_once(self):
        r = auth.ensure(self.env)
        self.assertTrue(r["key_created"] and r["token"])
        self.assertTrue(auth.verify_token(r["token"], self.env))
        d1 = self.read()
        r2 = auth.ensure(self.env)
        self.assertEqual(r2, {"key_created": False, "token": None})
        self.assertEqual(self.read(), d1)                      # ничего не затёрто
        self.assertTrue(auth.verify_token(r["token"], self.env))

    def test_existing_key_kept_token_added(self):
        auth.init_key(self.env)
        key = self.read()["hmac_key"]
        r = auth.ensure(self.env)
        self.assertEqual((r["key_created"], bool(r["token"])), (False, True))
        self.assertEqual(self.read()["hmac_key"], key)

    def test_existing_token_not_rotated(self):
        auth.init_key(self.env)
        tok = auth.new_token(self.env)
        before = self.read()
        r = auth.ensure(self.env)
        self.assertEqual(r, {"key_created": False, "token": None})
        self.assertEqual(self.read(), before)
        self.assertTrue(auth.verify_token(tok, self.env))

    @unittest.skipUnless(os.name == "posix", "права 0600 только на POSIX")
    def test_permissions_0600(self):
        auth.ensure(self.env)
        self.assertEqual(stat.S_IMODE(os.stat(auth.control_file(self.env)).st_mode), 0o600)
        self.assertTrue(auth.status(self.env)["perms_ok"])

    def test_cli_prints_token_once_without_key_value(self):
        old = os.environ.get("TB_KEYS_DIR")
        os.environ["TB_KEYS_DIR"] = self.keys
        self.addCleanup(lambda: os.environ.__setitem__("TB_KEYS_DIR", old) if old is not None else os.environ.pop("TB_KEYS_DIR", None))
        out = []
        cli.ensure_auth(out.append)
        txt = "\n".join(out)
        self.assertIn("ТОКЕН", txt)
        self.assertNotIn(self.read()["hmac_key"], txt)
        out2 = []
        cli.ensure_auth(out2.append)
        self.assertEqual(out2, [])

    def test_remember_helpers_need_token(self):
        auth.init_key(self.env)
        self.assertIsNone(auth.make_remember(NOW, 30, self.env))
        auth.new_token(self.env)
        ck = auth.make_remember(NOW, 30, self.env)
        self.assertTrue(auth.check_remember(ck, NOW + 1, self.env))
        self.assertFalse(auth.check_remember(ck, NOW + 31 * DAY, self.env))
        self.assertEqual(auth.csrf_of(ck, self.env), auth.csrf_of(ck, self.env))
        self.assertTrue(16 <= len(ck) <= 80)


if __name__ == "__main__":
    unittest.main()
