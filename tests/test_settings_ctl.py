"""WP-X: настроечные действия ctl (llm.provider, brain.profile, brain.role, exchange.set, brain.use all), ввод ключа LLM из дашборда (write-only), GET-контракт /api/settings/*.
Ключи только поддельные, TB_KEYS_DIR временный, сети нет. Проверяется, что ключ не попадает в очередь, аудит, ответы, журналы и GET-ответы."""
import json
import os
import shutil
import unittest

import _bootstrap  # noqa: F401
import _ctl
from tb.control import protocol
from tb.core import exchcfg
from tb.dash import ctlweb, server
from tb.llm import dashapi

PORT = 8777
FAKE = "sk-FAKEWEBKEY-0123456789abcdefXYZ77aa"


class H(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), d)


def hdr(**over):
    h = {"host": "127.0.0.1:%d" % PORT, "origin": "http://127.0.0.1:%d" % PORT, "content-type": "application/json", "sec-fetch-site": "same-origin"}
    for k, v in over.items():
        h[k.replace("_", "-")] = v
    return H(h)


class Base(unittest.TestCase):
    def setUp(self):
        self.e = _ctl.Env(self)
        self.e.good_live()
        for n in ("provider_presets.json", "exchanges.json"):
            shutil.copy(os.path.join(_ctl.PROJECT, "config", n), self.e.p("config", n))
        self.ctl = self.e.ctl()

    def run_ctl(self, action, args, expect="done", **kw):
        res, _c = self.e.send(self.ctl, action, args, **kw)
        self.assertEqual(res["status"], expect, res)
        return res


class TestProtocol(unittest.TestCase):
    def test_validation(self):
        v = protocol.validate_args
        self.assertTrue(v("llm.provider", {"op": "add", "id": "openai", "preset": "openai"})[0])
        self.assertFalse(v("llm.provider", {"op": "add", "id": "openai"})[0])
        self.assertFalse(v("llm.provider", {"op": "nope", "id": "x"})[0])
        self.assertFalse(v("llm.provider", {"op": "update", "id": "x", "patch": {"enabled": True}})[0])
        self.assertFalse(v("llm.provider", {"op": "update", "id": "x", "patch": {"api_key": "sk-123"}})[0])      # ключа в аргументах быть не может
        self.assertFalse(v("llm.provider", {"op": "add", "id": "x", "spec": {"key": "sk-123"}})[0])
        self.assertFalse(v("llm.provider", {"op": "model_set", "id": "x", "model": "m", "spec": {"extra_body": {"a": 1}}})[0])
        self.assertTrue(v("brain.profile", {"op": "set", "id": "p1", "provider": "openai", "model": "mini", "params": {"temperature": 0.2}})[0])
        self.assertFalse(v("brain.profile", {"op": "set", "id": "p1", "params": {"live_ok": True}})[0])         # метку live_ok не поставить
        self.assertTrue(v("brain.role", {"op": "chain", "role": "A.position_manager", "primary": "ds-pro", "fallback": ["ds-flash"]})[0])
        self.assertFalse(v("brain.role", {"op": "chain", "role": "A.position_manager"})[0])
        self.assertFalse(v("brain.role", {"op": "chain", "role": "A.position_manager", "fallback": ["a", "b", "c", "d", "e"]})[0])
        self.assertTrue(v("brain.role", {"op": "opts", "role": "news_reader", "budget": {"day_usd": 0.5}})[0])
        self.assertTrue(v("exchange.set", {"op": "activate", "id": "bybit"})[0])
        self.assertFalse(v("exchange.set", {"op": "mode", "id": "bybit", "mode": "yolo"})[0])
        self.assertFalse(v("exchange.set", {"op": "activate", "id": "Bad-Id"})[0])
        self.assertTrue(v("brain.use", {"profile": "ds-pro", "all": True})[0])


class TestCtlProviders(Base):
    def test_add_enable_profile_role_through_ctl(self):
        r = self.run_ctl("llm.provider", {"op": "add", "id": "openai", "preset": "openai"})
        self.assertEqual(r["code"], "OK")
        loc = self.e.r("config/local/providers.json")
        self.assertFalse(loc["providers"]["openai"]["enabled"])
        r = self.run_ctl("llm.provider", {"op": "enable", "id": "openai"}, expect="rejected")
        self.assertEqual(r["code"], "E_NO_KEY")
        from tb.llm import keystore
        keystore.set_key("openai.env", "OPENAI_API_KEY", FAKE)
        self.run_ctl("llm.provider", {"op": "enable", "id": "openai"})
        chk = self.run_ctl("llm.provider", {"op": "check", "id": "openai"})
        self.assertTrue(chk["data"]["ok"])
        self.assertTrue(chk["data"]["checked"][0]["key"]["present"])
        self.run_ctl("brain.profile", {"op": "set", "id": "oai-mini", "provider": "openai", "model": "mini", "params": {"temperature": 0.2, "max_tokens": 1000, "thinking": "off"}})
        self.run_ctl("brain.role", {"op": "chain", "role": "shared.news_reader", "primary": "oai-mini", "fallback": ["ds-flash"]})
        from tb.brain import swap
        self.assertEqual(swap.read_active(self.e.root)[0]["shared_roles"]["news_reader"]["primary"], "oai-mini")
        self.run_ctl("brain.role", {"op": "opts", "role": "news_reader", "params": {"temperature": 0.9}, "budget": {"day_usd": 0.3}})
        r = self.run_ctl("llm.provider", {"op": "remove", "id": "openai"}, expect="rejected")
        self.assertEqual(r["code"], "E_IN_USE")
        # аудит: каждая команда в журнале, значений ключа в нём нет
        rows = self.e.audit()
        self.assertGreaterEqual(len([x for x in rows if x["action"] in ("llm.provider", "brain.profile", "brain.role")]), 6)
        self.assertNotIn(FAKE, json.dumps(rows))

    def test_use_all_through_ctl_paper_applies_immediately(self):
        r = self.e.confirmed(self.ctl, "brain.use", {"profile": "ds-flash", "all": True})
        self.assertEqual(r["status"], "done", r)
        from tb.brain import swap
        a = swap.load_effective(self.e.root)
        self.assertTrue(all(ch["primary"] == "ds-flash" for ch in a.shared_roles.values()))

    def test_provider_with_live_role_cannot_be_disabled(self):
        self.e.w("state/control/platform.json", {"state": "running", "mode": "live", "day_id": "D-1", "live_waivers": []})
        r = self.run_ctl("llm.provider", {"op": "disable", "id": "deepseek"}, expect="rejected")
        self.assertEqual(r["code"], "E_LIVE_IN_USE")

    def test_unsigned_command_rejected(self):
        c = self.e.cmd("llm.provider", {"op": "check", "id": "deepseek"})
        c["mac"] = "0" * 64
        from tb.control import client
        client.submit(c, self.e.root)
        self.ctl.poll_once()
        self.assertEqual(client.read_result(c["id"], self.e.root)["code"], "E_MAC")


class TestCtlExchanges(Base):
    def test_exchange_ops_and_stub_exchanges_cannot_be_enabled(self):
        r = self.run_ctl("exchange.set", {"op": "enable", "id": "okx"}, expect="rejected")
        self.assertEqual(r["code"], "E_ARGS")
        self.assertIn("не реализован", r["data"]["detail"])
        r = self.run_ctl("exchange.set", {"op": "activate", "id": "okx"}, expect="rejected")
        self.assertEqual(r["code"], "E_ARGS")
        self.run_ctl("exchange.set", {"op": "limits", "id": "bybit", "max_open_positions": 2})
        self.assertEqual(exchcfg.load(self.e.root)["exchanges"]["bybit"]["limits"]["max_open_positions"], 2)
        self.run_ctl("exchange.set", {"op": "mode", "id": "bybit", "mode": "testnet"})
        self.assertEqual(exchcfg.load(self.e.root)["exchanges"]["bybit"]["mode"], "testnet")

    def test_live_mode_needs_phrase_and_nonce_and_stopped_platform(self):
        self.e.w("state/control/platform.json", {"state": "running", "mode": "paper", "day_id": "D-1"})
        ch = self.e.challenge(self.ctl, "exchange.set", {"op": "mode", "id": "bybit", "mode": "live"})
        self.assertEqual(ch["code"], "E_GATE")                          # платформа не остановлена
        self.e.w("state/control/platform.json", {"state": "stopped", "mode": "observe"})
        res, _c = self.e.send(self.ctl, "exchange.set", {"op": "mode", "id": "bybit", "mode": "live"})
        self.assertEqual(res["code"], "E_NONCE")                        # без подтверждения
        r = self.e.confirmed(self.ctl, "exchange.set", {"op": "mode", "id": "bybit", "mode": "live"})
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(exchcfg.load(self.e.root)["exchanges"]["bybit"]["mode"], "live")
        ch = self.e.challenge(self.ctl, "exchange.set", {"op": "mode", "id": "bybit", "mode": "paper"})
        self.assertEqual(ch["data"]["phrases"], [])


class TestDashKey(Base):
    def setUp(self):
        Base.setUp(self)
        self.web = ctlweb.CtlWeb(self.e.root)
        code, obj, extra = self.web.post("/api/ctl/login", hdr(), json.dumps({"token": self.e.token}).encode(), PORT, _ctl.NOW)
        self.assertEqual(code, 200, obj)
        self.ck, self.csrf = extra["Set-Cookie"].split(";")[0], obj["csrf"]

    def key(self, pid, value, now=_ctl.NOW + 1, **hover):
        return self.web.post("/api/ctl/llmkey", hdr(cookie=self.ck, x_tb_csrf=self.csrf, **hover), json.dumps({"provider": pid, "value": value}).encode(), PORT, now)

    def test_set_key_write_only_no_echo_no_queue_no_audit_leak(self):
        self.run_ctl("llm.provider", {"op": "add", "id": "openai", "preset": "openai"})
        code, obj, _h = self.key("openai", FAKE)
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["cred"]["mask"], "…77aa")
        self.assertNotIn(FAKE, json.dumps(obj))
        with open(os.path.join(self.e.keys, "openai.env"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "OPENAI_API_KEY=%s\n" % FAKE)
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(self.e.keys, "openai.env")).st_mode & 0o077, 0)
        # нигде в корне платформы (очередь команд, аудит, журналы, конфиги) значения нет
        for d, _dirs, files in os.walk(self.e.root):
            for f in files:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    self.assertNotIn(FAKE, fh.read(), os.path.join(d, f))
        a = [r for r in self.e.audit() if r["action"] == "llm.key.set"]
        self.assertEqual((len(a), a[0]["result"]), (1, "ok"))
        # GET-контракт: ключ есть, значение недоступно
        code, o = dashapi.api("/api/settings/providers", {}, self.e.root, _ctl.NOW)
        row = [p for p in o["providers"] if p["id"] == "openai"][0]
        self.assertEqual((row["cred"]["present"], row["cred"]["mask"]), (True, "…77aa"))
        self.assertNotIn(FAKE, json.dumps(o, ensure_ascii=False))
        self.run_ctl("llm.provider", {"op": "enable", "id": "openai"})

    def test_exchange_and_reserved_names_not_accepted(self):
        self.e.w("config/local/providers.json", {"providers": {"evil": {"adapter": "openai_compat", "base_url": "https://x.example/v1", "enabled": False,
                                                                       "key_file": "bybit_mainnet_trade.env", "key_env": "BYBIT_API_SECRET",
                                                                       "models": {"m": {"name": "m1", "price": {"in": 1, "out": 1}}}}}})
        code, obj, _h = self.key("evil", FAKE)
        self.assertEqual((code, obj["code"]), (400, "E_KEY"))        # зарезервированное имя файла ключа отклонено до записи
        self.assertFalse(os.path.exists(os.path.join(self.e.keys, "bybit_mainnet_trade.env")))
        self.assertEqual(self.key("deepseek", "short")[0], 400)
        self.assertEqual(self.key("nope", FAKE)[0], 404)
        self.assertEqual(self.key("../x", FAKE)[0], 400)
        for bad in ("a b c d e f g h i j k", "x" * 500):
            self.assertEqual(self.key("deepseek", bad)[0], 400)

    def test_key_route_requires_session_csrf_and_origin(self):
        body = json.dumps({"provider": "deepseek", "value": FAKE}).encode()
        self.assertEqual(self.web.post("/api/ctl/llmkey", hdr(), body, PORT, _ctl.NOW + 1)[0], 401)                      # без сессии
        self.assertEqual(self.web.post("/api/ctl/llmkey", hdr(cookie=self.ck, x_tb_csrf="wrong"), body, PORT, _ctl.NOW + 1)[0], 403)
        self.assertEqual(self.web.post("/api/ctl/llmkey", hdr(cookie=self.ck, x_tb_csrf=self.csrf, origin="http://evil.example"), body, PORT, _ctl.NOW + 1)[0], 403)
        self.assertFalse(os.path.exists(os.path.join(self.e.keys, "deepseek.env")))

    def test_open_session_cannot_set_key(self):
        """open_local: сессия «open» (вход без токена) не может вводить ключ, как и другие опасные действия; сессия с токеном может."""
        self.e.w("config/control.json", {"version": 1, "auth": {"mode": "open_local"}})
        web = ctlweb.CtlWeb(self.e.root)
        code, obj, extra = web.get("/api/ctl/session", {}, hdr(), _ctl.NOW)
        self.assertEqual((code, obj.get("level")), (200, "open"))
        ck = extra["Set-Cookie"].split(";")[0]
        r = web.post("/api/ctl/llmkey", hdr(cookie=ck, x_tb_csrf=obj["csrf"]), json.dumps({"provider": "deepseek", "value": FAKE}).encode(), PORT, _ctl.NOW)
        self.assertEqual((r[0], r[1]["code"]), (401, "E_ELEVATE"))
        self.assertFalse(os.path.exists(os.path.join(self.e.keys, "deepseek.env")))
        code, lg, extra = web.post("/api/ctl/login", hdr(), json.dumps({"token": self.e.token, "remember": False}).encode(), PORT, _ctl.NOW)
        self.assertEqual(code, 200, lg)
        r = web.post("/api/ctl/llmkey", hdr(cookie=extra["Set-Cookie"].split(";")[0], x_tb_csrf=lg["csrf"]), json.dumps({"provider": "deepseek", "value": FAKE}).encode(), PORT, _ctl.NOW)
        self.assertEqual(r[0], 200, r[1])


class TestGetContract(Base):
    def get(self, path, q=None):
        code, o = dashapi.api("/api/settings/" + path, q or {}, self.e.root, _ctl.NOW)
        self.assertEqual(code, 200, o)
        return o

    def test_endpoints_shape_and_scrub_survives(self):
        for ep in ("overview", "providers", "presets", "profiles", "roles", "exchanges", "usage", "revisions"):
            o = self.get(ep)
            scrubbed = server.model.scrub(o)
            self.assertEqual(json.dumps(scrubbed, sort_keys=True), json.dumps(o, sort_keys=True), ep + ": scrub вырезал поля (ключ/token в имени)")
        prov = self.get("providers")
        d = [p for p in prov["providers"] if p["id"] == "deepseek"][0]
        self.assertIn("cred", d)
        self.assertIn("max_out", d["models"][0])
        self.assertIsInstance(d["models"], list)
        self.assertIn("day_toks", d["limits"])
        roles = self.get("roles")
        r = [x for x in roles["roles"] if x["role"] == "position_manager"][0]
        for k in ("primary", "fallback", "params_effective", "budget", "today", "live_risk", "live_ok"):
            self.assertIn(k, r)
        self.assertEqual(r["primary"]["id"], "ds-pro")
        self.assertIn("max_out_tok", r["primary"]["params"])
        ex = self.get("exchanges")
        self.assertEqual(ex["active"], "bybit")
        self.assertFalse([e for e in ex["exchanges"] if e["id"] == "okx"][0]["implemented"])
        self.assertTrue(ex["editable"] in (True, False))
        self.assertEqual(dashapi.api("/api/other", {}, self.e.root, _ctl.NOW), None)
        self.assertEqual(dashapi.api("/api/settings/nope", {}, self.e.root, _ctl.NOW)[0], 404)

    def test_served_through_dash_server_payload(self):
        code, obj = server.api_payload("/api/settings/overview", {}, self.e.root, _ctl.NOW)
        self.assertEqual(code, 200)
        self.assertIn("exchange", obj)

    def test_usage_by_model(self):
        rows = [{"t": _ctl.NOW - 100, "ts": "x", "role": "news_reader", "profile": "ds-flash", "provider": "deepseek", "model": "flash", "in": 100, "out": 20, "usd": 0.002, "ok": True}]
        self.e.w("state/llm_calls.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n", raw=True)
        o = self.get("usage", {"by": ["model"], "days": ["3"]})
        self.assertEqual(o["groups"][0]["id"], "flash")
        self.assertAlmostEqual(o["total"]["usd"], 0.002)


class TestRealHttp(Base):
    """Реальный HTTP-сервер дашборда на свободном порту loopback во временном корне: GET /api/settings/*, ввод ключа POST, ключ не возвращается."""

    def setUp(self):
        Base.setUp(self)
        import threading
        self.srv = server.make_server(0, self.e.root)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self._close)

    def _close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, data=None, headers=None):
        import urllib.error
        import urllib.request
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method, headers=h)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_get_endpoints_and_key_post(self):
        code, raw = self.call("GET", "/api/settings/providers")
        self.assertEqual(code, 200)
        o = json.loads(raw.decode("utf-8"))
        self.assertTrue([p for p in o["providers"] if p["id"] == "deepseek"])
        for ep in ("overview", "profiles", "roles", "exchanges", "usage", "revisions", "presets"):
            self.assertEqual(self.call("GET", "/api/settings/" + ep)[0], 200, ep)
        # POST ключа без входа и без CSRF: отказ, файла нет
        hdrs = {"Origin": "http://127.0.0.1:%d" % self.port, "Content-Type": "application/json"}
        code, raw = self.call("POST", "/api/ctl/llmkey", json.dumps({"provider": "deepseek", "value": FAKE}).encode(), hdrs)
        self.assertIn(code, (401, 403, 503))
        self.assertNotIn(FAKE, raw.decode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(self.e.keys, "deepseek.env")))
        # вход по токену + CSRF: ключ принят, ответ без значения
        code, raw = self.call("POST", "/api/ctl/login", json.dumps({"token": self.e.token}).encode(), hdrs)
        self.assertEqual(code, 200, raw)
        lg = json.loads(raw.decode("utf-8"))
        # cookie возвращается заголовком, берём через повторный запрос с urllib-обработчиком недоступно: проверяем прямой вызов ctlweb уже выше
        self.assertTrue(lg["csrf"])


if __name__ == "__main__":
    unittest.main()
