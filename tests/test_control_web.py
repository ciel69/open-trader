"""WP12: веб-слой управления на дашборде (ctlweb + server): CSRF, Origin, Host, DNS rebinding, Content-Type, Sec-Fetch-Site, сессии, блокировка входа, лимит частоты,
challenge/command через ctl, KILL без ctl, отсутствие секретов в ответах, фаззинг, реальный HTTP-сервер. Без сети (только 127.0.0.1 во временном процессе)."""
import json
import os
import random
import threading
import unittest
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401
import _ctl
from tb.control import auth, client, ctlstate
from tb.core import exec_cfg
from tb.dash import ctlweb, server

NOW = _ctl.NOW
PORT = 8776
PAPER = {"mode": "paper", "session": "intraday", "minutes": 60}


class H(dict):
    """Заголовки без учёта регистра."""

    def get(self, k, d=None):
        return dict.get(self, k.lower(), d)


def hdr(port=PORT, **over):
    h = {"host": "127.0.0.1:%d" % port, "origin": "http://127.0.0.1:%d" % port, "content-type": "application/json", "sec-fetch-site": "same-origin"}
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
    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.good_live()
        self.web = ctlweb.CtlWeb(self.e.root)
        self.now = NOW

    def login(self, token=None, now=None, **hover):
        return self.web.post("/api/ctl/login", hdr(**hover), body({"token": token or self.e.token}), PORT, self.now if now is None else now)

    def session(self):
        code, obj, extra = self.login()
        self.assertEqual(code, 200, obj)
        ck = extra["Set-Cookie"].split(";")[0]
        return ck, obj["csrf"]

    def post(self, path, data, ck, csrf, now=None, **hover):
        h = hdr(cookie=ck, x_tb_csrf=csrf, **hover)
        return self.web.post(path, h, body(data) if not isinstance(data, bytes) else data, PORT, self.now if now is None else now)


class TestRequestChecks(Base):
    def test_login_sets_strict_httponly_cookie_and_csrf(self):
        code, obj, extra = self.login()
        self.assertEqual(code, 200)
        c = extra["Set-Cookie"]
        for part in ("tb_sess=", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, c)
        self.assertNotIn("Domain", c)
        self.assertTrue(len(obj["csrf"]) >= 20)
        self.assertNotIn(self.e.token, json.dumps(obj))

    def test_rejection_matrix(self):
        ck, csrf = self.session()
        good = self.post("/api/ctl/challenge", {"action": "day.stop", "args": {}}, ck, csrf)
        self.assertIn(good[0], (200, 504))                               # запрос принят проверками (ctl не запущен: 504)
        matrix = [
            ("host чужой", dict(host="evil.example.com:%d" % PORT), 403, "E_ORIGIN"),
            ("host rebinding на 127.0.0.1.evil.com", dict(host="127.0.0.1.evil.com:%d" % PORT), 403, "E_ORIGIN"),
            ("host без порта", dict(host="127.0.0.1"), 403, "E_ORIGIN"),
            ("host другой порт", dict(host="127.0.0.1:9999"), 403, "E_ORIGIN"),
            ("host пустой", dict(host=None), 403, "E_ORIGIN"),
            ("origin нет", dict(origin=None), 403, "E_ORIGIN"),
            ("origin чужой", dict(origin="http://evil.example.com"), 403, "E_ORIGIN"),
            ("origin null", dict(origin="null"), 403, "E_ORIGIN"),
            ("origin https", dict(origin="https://127.0.0.1:%d" % PORT), 403, "E_ORIGIN"),
            ("origin localhost при host 127.0.0.1", dict(origin="http://localhost:%d" % PORT), 403, "E_ORIGIN"),
            ("origin с путём", dict(origin="http://127.0.0.1:%d/" % PORT), 403, "E_ORIGIN"),
            ("origin другой порт", dict(origin="http://127.0.0.1:1234"), 403, "E_ORIGIN"),
            ("sec-fetch cross-site", dict(sec_fetch_site="cross-site"), 403, "E_ORIGIN"),
            ("sec-fetch same-site", dict(sec_fetch_site="same-site"), 403, "E_ORIGIN"),
            ("sec-fetch none", dict(sec_fetch_site="none"), 403, "E_ORIGIN"),
            ("ctype text/plain", dict(content_type="text/plain"), 415, "E_CTYPE"),
            ("ctype form", dict(content_type="application/x-www-form-urlencoded"), 415, "E_CTYPE"),
            ("ctype multipart", dict(content_type="multipart/form-data; boundary=x"), 415, "E_CTYPE"),
            ("ctype нет", dict(content_type=None), 415, "E_CTYPE"),
            ("ctype json5", dict(content_type="application/json5"), 415, "E_CTYPE"),
            ("ctype jsonp", dict(content_type="application/jsonp"), 415, "E_CTYPE"),
            ("cookie нет", dict(cookie=None), 401, "E_AUTH"),
            ("cookie чужая", dict(cookie="tb_sess=" + "x" * 43), 401, "E_AUTH"),
            ("cookie мусор", dict(cookie="tb_sess=;;;==="), 401, "E_AUTH"),
            ("csrf нет", dict(x_tb_csrf=None), 403, "E_CSRF"),
            ("csrf неверный", dict(x_tb_csrf="A" * len(csrf)), 403, "E_CSRF"),
            ("csrf пустой", dict(x_tb_csrf=""), 403, "E_CSRF"),
        ]
        for name, over, code, err in matrix:
            kw = dict(cookie=ck, x_tb_csrf=csrf)
            kw.update(over)
            h = hdr(**kw)
            r = self.web.post("/api/ctl/challenge", h, body({"action": "day.stop", "args": {}}), PORT, self.now)
            self.assertEqual((r[0], r[1]["code"]), (code, err), name)
            self.assertFalse(r[1]["ok"])
        self.assertGreaterEqual(len(matrix), 20)

    def test_accepted_content_type_with_charset(self):
        ck, csrf = self.session()
        r = self.post("/api/ctl/challenge", {"action": "day.stop", "args": {}}, ck, csrf, content_type="application/json; charset=utf-8")
        self.assertIn(r[0], (200, 504))

    def test_login_origin_also_checked(self):
        for over in (dict(origin=None), dict(origin="http://evil.example.com"), dict(sec_fetch_site="cross-site"), dict(content_type="text/plain"), dict(host="evil.com:%d" % PORT)):
            r = self.login(**over)
            self.assertIn(r[0], (403, 415), over)
            self.assertNotIn("Set-Cookie", r[2])

    def test_csrf_of_other_session_rejected(self):
        ck1, csrf1 = self.session()
        ck2, csrf2 = self.session()
        r = self.post("/api/ctl/challenge", {"action": "day.stop", "args": {}}, ck1, csrf2)
        self.assertEqual(r[1]["code"], "E_CSRF")

    def test_body_validation(self):
        ck, csrf = self.session()
        for raw in (b"{", b"[]", b'"x"', b"5", b"\xff\xfe", b"x" * 9000, b"null"):
            r = self.post("/api/ctl/challenge", raw, ck, csrf)
            self.assertEqual((r[0], r[1]["code"]), (400, "E_BODY"), raw[:10])
        r = self.post("/api/ctl/challenge", {"action": "evil", "args": {}}, ck, csrf)
        self.assertEqual((r[0], r[1]["code"]), (400, "E_UNKNOWN"))
        r = self.post("/api/ctl/challenge", {"action": "day.start", "args": {"mode": "x"}}, ck, csrf)
        self.assertEqual((r[0], r[1]["code"]), (400, "E_ARGS"))
        r = self.post("/api/ctl/command", {"action": "challenge", "args": {}}, ck, csrf)
        self.assertEqual(r[1]["code"], "E_UNKNOWN")
        r = self.post("/api/ctl/command", {"action": "day.stop", "args": {}, "phrases": "x"}, ck, csrf)
        self.assertEqual(r[1]["code"], "E_BODY")
        r = self.post("/api/ctl/nothing", {}, ck, csrf)
        self.assertEqual((r[0], r[1]["code"]), (404, "E_NOT_FOUND"))

    def test_session_expiry_and_rotation(self):
        ck, csrf = self.session()
        ok = self.post("/api/ctl/command", {"action": "day.stop", "args": {}}, ck, csrf, now=self.now + 30 * 60)       # активность продлевает
        self.assertEqual(ok[0], 200)
        late = self.post("/api/ctl/command", {"action": "day.stop", "args": {}}, ck, csrf, now=self.now + 30 * 60 + 61 * 60)
        self.assertEqual((late[0], late[1]["code"]), (401, "E_AUTH"))
        ck, csrf = self.session()
        auth.new_token()                                                   # ротация токена: сессии по старому токену недействительны
        r = self.post("/api/ctl/command", {"action": "day.stop", "args": {}}, ck, csrf)
        self.assertEqual(r[0], 401)

    def test_rate_limit_per_session(self):
        ck, csrf = self.session()
        codes = [self.post("/api/ctl/command", {"action": "intel.refresh", "args": {}}, ck, csrf, now=self.now + i * 0.1)[0] for i in range(32)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30:], [429, 429])
        self.assertEqual(self.post("/api/ctl/command", {"action": "intel.refresh", "args": {}}, ck, csrf, now=self.now + 70)[0], 200)

    def test_login_lockout_after_five_failures(self):
        for i in range(5):
            r = self.login(token="bad%d" % i, now=self.now + i)
            self.assertEqual((r[0], r[1]["code"]), (401, "E_LOGIN"))
        r = self.login(now=self.now + 6)                                    # верный токен, но блок 10 минут
        self.assertEqual((r[0], r[1]["code"]), (429, "E_BLOCKED"))
        r = self.login(now=self.now + 6 + 601)
        self.assertEqual(r[0], 200)

    def test_failures_below_threshold_reset_on_success(self):
        for i in range(4):
            self.login(token="bad", now=self.now + i)
        self.assertEqual(self.login(now=self.now + 5)[0], 200)
        for i in range(4):
            self.login(token="bad", now=self.now + 10 + i)
        self.assertEqual(self.login(now=self.now + 20)[0], 200)

    def test_not_configured(self):
        e2 = _ctl.Env(self, with_token=False)
        e2.w("config/control.json", {"version": 1, "auth": {"mode": "token"}})
        web = ctlweb.CtlWeb(e2.root)
        r = web.post("/api/ctl/login", hdr(), body({"token": "x"}), PORT, NOW)
        self.assertEqual((r[0], r[1]["code"]), (503, "E_CONFIG"))
        self.assertEqual(web.get("/api/ctl/session", {}, hdr(), NOW)[1]["configured"], False)

    def test_get_endpoints_do_not_leak(self):
        ck, csrf = self.session()
        code, s, _ = self.web.get("/api/ctl/session", {}, hdr(cookie=ck), NOW)
        self.assertEqual((s["authed"], s["csrf"]), (True, csrf))
        code, s, _ = self.web.get("/api/ctl/session", {}, hdr(), NOW)
        self.assertEqual((s["authed"], "csrf" in s), (False, False))        # CSRF-токен без cookie не выдаётся
        key = json.loads(_ctl.rd(auth.control_file()))
        for path in ("/api/ctl/session", "/api/ctl/status", "/api/ctl/audit", "/api/ctl/result"):
            txt = json.dumps(self.web.get(path, {"id": ["K-20261027T130000-aaaaaa"]}, hdr(cookie=ck), NOW)[1])
            for sec in (self.e.token, key["hmac_key"], key["token_hash"], key["salt"]):
                self.assertNotIn(sec, txt, path)
        self.assertEqual(self.web.get("/api/ctl/nothing", {}, hdr(), NOW)[0], 404)
        self.assertIsNone(self.web.get("/api/ctl/result", {"id": ["../../x"]}, hdr(), NOW)[1]["result"])

    def test_denied_requests_are_audited(self):
        self.web.post("/api/ctl/challenge", hdr(origin="http://evil.example.com"), body({}), PORT, NOW)
        self.login(token="bad")
        codes = [a["code"] for a in self.e.audit()]
        self.assertIn("E_ORIGIN", codes)
        self.assertIn("E_LOGIN", codes)


class TestFlowThroughCtl(Base):
    def setUp(self):
        Base.setUp(self)
        self.e.good_exec(profile="paper")
        self.c = self.e.ctl(clock=__import__("time").time, reconf=self.reconf, sync=True)
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        _ctl.pump(self.c, self.stop)
        self.real_now = __import__("time").time
        self.now = self.real_now()

    def reconf(self, profile, observe, timeout):
        self.e.good_exec(profile=profile, observe=observe, now=self.real_now())
        return True, "OK"

    def test_paper_start_through_dash(self):
        self.e.good_exec(profile="paper", now=self.real_now())
        t0 = self.real_now()
        ck, csrf = self.session()
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": PAPER}), PORT, t0)
        self.assertEqual(r[0], 200, r)
        cid = r[1]["id"]
        res = client.wait_result(cid, self.e.root, timeout=5)
        self.assertEqual(res["code"], "OK")
        a = [x for x in self.e.audit() if x["id"] == cid][-1]
        self.assertEqual(a["src"], "dash")

    def test_live_challenge_nonce_and_typing_rules(self):
        self.e.good_live(now=self.real_now())
        self.e.good_exec(profile="paper", now=self.real_now())
        t0 = self.real_now()
        ck, csrf = self.session()
        live = {"mode": "live", "session": "intraday", "minutes": 60}
        r = self.web.post("/api/ctl/challenge", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live}), PORT, t0)
        self.assertEqual(r[0], 200, r)
        d = r[1]["data"]
        self.assertTrue(d["nonce"] and d["phrases"])
        # слишком рано
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live, "nonce": d["nonce"], "phrases": d["phrases"]}), PORT, t0 + 0.5)
        self.assertEqual((r[0], r[1]["code"]), (403, "E_NONCE"))
        # неверная фраза (nonce жив)
        import time as _t
        _t.sleep(2.1)
        t1 = self.real_now()
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live, "nonce": d["nonce"], "phrases": ["неверно"]}), PORT, t1)
        self.assertEqual((r[0], r[1]["code"]), (400, "E_PHRASE"))
        # слишком быстрый «набор»: меньше min_type_s с момента выдачи
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live, "nonce": d["nonce"], "phrases": d["phrases"]}), PORT, t0 + 2.5)
        self.assertEqual((r[0], r[1]["code"]), (403, "E_NONCE"))
        # чужие аргументы
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": dict(live, team="B"), "nonce": d["nonce"], "phrases": d["phrases"]}),
                          PORT, t0 + 5)
        self.assertEqual(r[1]["code"], "E_NONCE")
        # верно
        self.e.good_exec(profile="paper", now=self.real_now())
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live, "nonce": d["nonce"], "phrases": d["phrases"]}), PORT, t0 + 5)
        self.assertEqual(r[0], 200, r)
        res = client.wait_result(r[1]["id"], self.e.root, timeout=8)
        self.assertEqual((res["status"], res["code"]), ("done", "OK"), res)
        # nonce одноразовый и на дашборде
        r = self.web.post("/api/ctl/command", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": live, "nonce": d["nonce"], "phrases": d["phrases"]}), PORT, t0 + 6)
        self.assertEqual(r[1]["code"], "E_NONCE")

    def test_challenge_with_ctl_down_times_out(self):
        self.stop.set()
        import time as _t
        _t.sleep(0.1)
        ck, csrf = self.session()
        orig = client.wait_result
        client.wait_result = lambda cid, root=None, timeout=4.0, **k: orig(cid, root, timeout=0.3, **k)
        try:
            r = self.web.post("/api/ctl/challenge", hdr(cookie=ck, x_tb_csrf=csrf), body({"action": "day.start", "args": PAPER}), PORT, self.real_now())
        finally:
            client.wait_result = orig
        self.assertEqual((r[0], r[1]["code"]), (504, "E_CTL_DOWN"))


class TestKillWithoutCtl(Base):
    def test_kill_creates_file_without_ctl_and_queues_notification(self):
        ck, csrf = self.session()
        r = self.post("/api/ctl/command", {"action": "kill.set", "args": {"flatten": False, "reason": "тест"}}, ck, csrf)
        self.assertEqual((r[0], r[1]["killed"], r[1]["flatten"]), (200, True, False))
        k = exec_cfg.kill_state(self.e.root)
        self.assertIsNotNone(k)
        self.assertIn("dash", k["reason"])
        self.assertEqual(len(os.listdir(self.e.p("state", "control", "_inbox"))), 1)       # ctl (когда оживёт) увидит уведомление
        self.assertIn(("kill.set", "OK"), [(a["action"], a["code"]) for a in self.e.audit()])

    def test_kill_needs_session_and_csrf(self):
        for over in (dict(), dict(cookie=None), dict(x_tb_csrf="bad")):
            ck, csrf = self.session()
            kw = dict(cookie=ck, x_tb_csrf=csrf)
            kw.update(over)
            h = hdr(**kw)
            r = self.web.post("/api/ctl/command", h, body({"action": "kill.set", "args": {}}), PORT, NOW)
            if over:
                self.assertIn(r[0], (401, 403))
                self.assertIsNone(exec_cfg.kill_state(self.e.root))
            else:
                self.assertEqual(r[0], 200)
                exec_cfg.clear_kill(self.e.root)

    def test_flatten_needs_local_challenge_nonce(self):
        ck, csrf = self.session()
        r = self.post("/api/ctl/command", {"action": "kill.set", "args": {"flatten": True}}, ck, csrf)
        self.assertEqual((r[0], r[1]["code"]), (403, "E_NONCE"))
        self.assertIsNone(exec_cfg.kill_state(self.e.root))
        r = self.post("/api/ctl/challenge", {"action": "stop.emergency", "args": {"flatten": True}}, ck, csrf)       # ctl не нужен
        self.assertEqual(r[0], 200)
        n = r[1]["data"]["nonce"]
        self.assertTrue(n)
        r = self.post("/api/ctl/command", {"action": "stop.emergency", "args": {"flatten": True}, "nonce": n}, ck, csrf, now=self.now + 0.5)
        self.assertEqual(r[1]["code"], "E_NONCE")                           # рано
        r = self.post("/api/ctl/command", {"action": "stop.emergency", "args": {"flatten": True}, "nonce": n}, ck, csrf, now=self.now + 3)
        self.assertEqual((r[0], r[1]["killed"], r[1]["flatten"]), (200, True, True))
        self.assertTrue(exec_cfg.kill_state(self.e.root)["flatten"])

    def test_no_endpoint_clears_kill(self):
        import inspect
        src = inspect.getsource(ctlweb) + inspect.getsource(server)
        self.assertNotIn("clear_kill", src)
        self.assertNotIn("exec resume", src)


class TestFuzz(Base):
    def test_random_headers_and_bodies_never_crash(self):
        rnd = random.Random(12)
        ck, csrf = self.session()
        paths = ["/api/ctl/login", "/api/ctl/logout", "/api/ctl/challenge", "/api/ctl/command", "/api/ctl/x", "/api/ctl/", "/api/ctl/command/../x"]
        junk = ["", "null", "{}", "[]", '{"action":1}', '{"action":"day.start","args":5}', '{"action":"day.start","args":{"mode":"live","accepted":[1]}}',
                '{"token":5}', '{"token":"' + "a" * 5000 + '"}', '{"action":"risk.set","args":{"level":1e999}}', '{"nonce":[],"action":"day.stop"}',
                '{"action":"kill.set","args":{"flatten":"yes","reason":{"a":1}}}', "\x00\x01", '{"a":' * 50]
        for i in range(400):
            h = hdr(cookie=rnd.choice([ck, None, "tb_sess=" + "z" * rnd.randint(0, 100), "a=b;" * rnd.randint(0, 5)]),
                    x_tb_csrf=rnd.choice([csrf, None, "", "x" * rnd.randint(0, 80)]), origin=rnd.choice(["http://127.0.0.1:%d" % PORT, None, "null", "x"]),
                    content_type=rnd.choice(["application/json", "text/plain", None]))
            raw = rnd.choice(junk).encode("utf-8", "replace")
            if rnd.random() < 0.3:
                raw = bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 60)))
            r = self.web.post(rnd.choice(paths), h, raw, PORT, NOW + i * 5)
            self.assertIn(r[0], (200, 400, 401, 403, 404, 415, 429, 503, 504), (r, raw[:30]))
            self.assertIsInstance(r[1], dict)
            if r[0] != 200:
                self.assertFalse(r[1].get("ok", False))
        # посторонние GET-запросы
        for p in ("/api/ctl/session", "/api/ctl/status", "/api/ctl/audit", "/api/ctl/result", "/api/ctl/%00", "/api/ctl/" + "a" * 300):
            r = self.web.get(p, {"n": ["x"], "id": ["%"]}, hdr(cookie="zz"), NOW)
            self.assertIn(r[0], (200, 404))

    def test_error_texts_do_not_contain_paths_or_exceptions(self):
        ck, csrf = self.session()
        for raw in (b"{", b"[]"):
            r = self.post("/api/ctl/command", raw, ck, csrf)
            self.assertNotRegex(json.dumps(r[1], ensure_ascii=False), r"Traceback|/Users|\.py|\.json|Error")


class TestRealServer(Base):
    def setUp(self):
        Base.setUp(self)
        self.srv = server.make_server(0, self.e.root)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.th.start()
        self.addCleanup(self._close)

    def _close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, data=None, headers=None):
        h = dict(headers or {})
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method, headers=h)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def jh(self, **extra):
        h = {"Origin": "http://127.0.0.1:%d" % self.port, "Content-Type": "application/json"}
        h.update(extra)
        return h

    def test_post_allowed_only_under_api_ctl(self):
        for path in ("/api/state", "/api/ping", "/api/v2/state", "/api/platform", "/", "/app.js", "/api/market", "/state/KILL", "/api/ctl"):
            code, _, _ = self.req("POST", path, b"{}", self.jh())
            self.assertEqual(code, 405, path)
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            for path in ("/api/ctl/login", "/api/ctl/command", "/api/state"):
                self.assertEqual(self.req(m, path, b"{}", self.jh())[0], 405, (m, path))

    def test_full_login_session_over_http(self):
        code, h, b = self.req("POST", "/api/ctl/login", body({"token": "wrong"}), self.jh())
        self.assertEqual(code, 401)
        code, h, b = self.req("POST", "/api/ctl/login", body({"token": self.e.token}), self.jh())
        self.assertEqual(code, 200)
        ck = h["Set-Cookie"].split(";")[0]
        csrf = json.loads(b)["csrf"]
        code, h2, b2 = self.req("GET", "/api/ctl/session", None, {"Cookie": ck})
        self.assertEqual(json.loads(b2)["authed"], True)
        # без CSRF — отказ, с CSRF — принято
        self.assertEqual(self.req("POST", "/api/ctl/command", body({"action": "intel.refresh", "args": {}}), self.jh(Cookie=ck))[0], 403)
        self.assertEqual(self.req("POST", "/api/ctl/command", body({"action": "intel.refresh", "args": {}}), self.jh(Cookie=ck, **{"X-TB-CSRF": csrf}))[0], 200)
        # чужой Origin
        bad = self.jh(Cookie=ck, **{"X-TB-CSRF": csrf})
        bad["Origin"] = "http://evil.example.com"
        self.assertEqual(self.req("POST", "/api/ctl/command", body({"action": "intel.refresh", "args": {}}), bad)[0], 403)
        # DNS rebinding: Host чужой
        evil = self.jh(Cookie=ck, **{"X-TB-CSRF": csrf, "Host": "evil.example.com"})
        self.assertEqual(self.req("POST", "/api/ctl/command", body({"action": "intel.refresh", "args": {}}), evil)[0], 403)
        # без Content-Length / слишком большое тело
        self.assertEqual(self.req("POST", "/api/ctl/login", b"x" * 9000, self.jh())[0], 400)

    def test_no_cors_headers_ever(self):
        for method, path, data in (("GET", "/api/ctl/status", None), ("POST", "/api/ctl/login", body({"token": "x"})), ("GET", "/api/state", None), ("POST", "/api/state", b"{}")):
            _, h, _ = self.req(method, path, data, self.jh() if data else {})
            for k in h.keys():
                self.assertFalse(k.lower().startswith("access-control-"), (path, k))

    def test_index_gets_extension_scripts_and_is_still_served(self):
        code, h, b = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"/v-control.js", b)
        self.assertNotIn(server.EXT_MARK.encode(), b)               # метка заменена
        code, h, b = self.req("GET", "/v-control.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", h["Content-Type"])

    def test_old_get_api_responses_unchanged(self):
        for p in ("/api/ping", "/api/state", "/api/market", "/api/health", "/api/v2/state", "/api/ideas"):
            code, h, b = self.req("GET", p)
            self.assertEqual(code, 200, p)
            json.loads(b)
        self.assertEqual(self.req("GET", "/api/nothing")[0], 404)


class TestExtensions(Base):
    def test_ext_api_registry_and_scripts(self):
        self.assertIn("tb.intel.dashapi", server.EXT_API)
        # модуль-расширение подключается, если существует; здесь подкладываем его в sys.modules
        import sys
        import types
        m = types.ModuleType("tb.fakeext.dashapi")
        m.api = lambda path, query, root, now: (200, {"hello": path}) if path == "/api/ext/hello" else None
        sys.modules["tb.fakeext.dashapi"] = m
        sys.modules["tb.fakeext"] = types.ModuleType("tb.fakeext")
        old = server.EXT_API
        server.EXT_API = ("tb.fakeext.dashapi",)
        self.addCleanup(lambda: (setattr(server, "EXT_API", old), sys.modules.pop("tb.fakeext.dashapi", None), sys.modules.pop("tb.fakeext", None)))
        import importlib.util
        orig = importlib.util.find_spec
        importlib.util.find_spec = lambda name, *a, **k: object() if name == "tb.fakeext.dashapi" else orig(name, *a, **k)
        self.addCleanup(lambda: setattr(importlib.util, "find_spec", orig))
        self.assertEqual(server.api_payload("/api/ext/hello", {}, self.e.root, NOW), (200, {"hello": "/api/ext/hello"}))
        self.assertEqual(server.api_payload("/api/ping", {}, self.e.root, NOW)[0], 200)

    def test_ext_scripts_autodiscovery(self):
        names = server.ext_scripts()
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(server.STATIC, n)))
        p = os.path.join(server.STATIC, "x-test-ext.js")
        _ctl.wr(p, "// ext")
        self.addCleanup(lambda: os.remove(p))
        self.assertIn("x-test-ext.js", server.ext_scripts())
        _ctl.wr(os.path.join(server.STATIC, "evil.js"), "//")
        self.addCleanup(lambda: os.remove(os.path.join(server.STATIC, "evil.js")))
        self.assertNotIn("evil.js", server.ext_scripts())


if __name__ == "__main__":
    unittest.main()
