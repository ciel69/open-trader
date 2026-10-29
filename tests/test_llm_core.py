"""WP11-C1: реестр провайдеров, ключи, цены и учёт, транспорт (повторы, таймауты), бюджет. Фейковый HTTP, настоящих ключей и сети нет."""
import calendar
import json
import os
import time
import unittest

import _bootstrap  # noqa: F401
import _llm
from fakes.fake_llm_http import R, ds_ok
from tb import paths
from tb.core import bus
from tb.llm import budget, keys, providers, usage
from tb.llm.transport import Transport
from tb.llm.types import LLMError, Req, Usage, user


def ts(y, mo, d, h=0):
    return calendar.timegm((y, mo, d, h, 0, 0))


class TestProviders(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        reg = providers.load()
        self.assertEqual(reg.errors, [])
        self.assertTrue(reg.providers["deepseek"].enabled)
        self.assertFalse(reg.providers["anthropic"].enabled)
        self.assertTrue(reg.model("deepseek", "pro").strict)

    def test_bad_entries_disable_only_that_provider(self):
        d = _llm.doc("http://127.0.0.1:1")
        d["providers"]["ds"]["key_file"] = "../evil"
        d["providers"]["an"]["retry"]["on"] = [401]
        d["providers"]["loc"]["adapter"] = "nope"
        reg = providers.parse(d)
        self.assertEqual(len(reg.errors), 3)
        self.assertFalse(any(p.enabled for p in reg.providers.values()))

    def test_price_and_names_validated(self):
        d = _llm.doc("http://127.0.0.1:1")
        d["providers"]["ds"]["models"]["flash"]["price"]["out"] = -1
        d["providers"]["an"]["base_url"] = "ftp://x"
        reg = providers.parse(d)
        self.assertEqual({e["provider"] for e in reg.errors}, {"ds", "an"})

    def test_unknown_provider_and_model(self):
        reg = providers.parse(_llm.doc("http://127.0.0.1:1"))
        with self.assertRaises(providers.ProviderError):
            reg.provider("zzz")
        with self.assertRaises(providers.ProviderError):
            reg.model("ds", "zzz")

    def test_bad_file(self):
        with self.assertRaises(providers.ProviderError):
            providers.load(path="/nonexistent/providers.json")
        with self.assertRaises(providers.ProviderError):
            providers.parse({"version": 2})


class TestKeys(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="tbk_")
        self.env = {"TB_KEYS_DIR": self.d}

    def test_file_then_env_and_mask(self):
        with open(os.path.join(self.d, "a.env"), "w") as f:
            f.write("# c\nexport A_KEY='abcdefghijkl1234'\n")
        s = keys.load("a.env", "A_KEY", self.env)
        self.assertEqual(s.reveal(), "abcdefghijkl1234")
        self.assertNotIn("abcdefghijkl", repr(s) + str(s))
        self.assertTrue(repr(s).endswith("1234)"))
        s2 = keys.load("missing.env", "B_KEY", dict(self.env, B_KEY="envvalue-123456"))
        self.assertEqual(s2.reveal(), "envvalue-123456")

    def test_missing_message_has_no_path(self):
        with self.assertRaises(LLMError) as c:
            keys.load("zz.env", "ZZ_KEY", self.env)
        self.assertEqual(c.exception.code, "no_key")
        self.assertNotIn(self.d, str(c.exception))

    def test_no_key_needed(self):
        self.assertIsNone(keys.load(None, None, self.env))

    def test_redact(self):
        s = keys.Secret("supersecretvalue99")
        t = keys.redact("bad key supersecretvalue99 / sk-abcdefgh12345678 end", s)
        self.assertNotIn("supersecret", t)
        self.assertNotIn("sk-abcdefgh", t)

    def test_load_does_not_touch_environ(self):
        before = dict(os.environ)
        with open(os.path.join(self.d, "a.env"), "w") as f:
            f.write("A_KEY=zzzzzzzzzzzz1111\n")
        keys.load("a.env", "A_KEY", self.env)
        self.assertEqual(dict(os.environ), before)


class TestUsage(_llm.LLMBase):
    def test_price_deepseek_peak_and_offpeak(self):
        m = self.reg.model("ds", "flash")
        u = Usage(miss=1000000, hit=1000000, out=1000000)
        peak = ts(2026, 10, 28, 2)        # среда 02:00 UTC
        off = ts(2026, 10, 31, 2)         # суббота
        self.assertAlmostEqual(usage.price_usd(m.price, u, peak), 0.30 + 0.006 + 1.20, 6)
        self.assertAlmostEqual(usage.price_usd(m.price, u, off), (0.30 + 0.006 + 1.20) * 0.5, 6)
        self.assertAlmostEqual(usage.price_usd(m.price, u, ts(2026, 10, 28, 12)), (0.30 + 0.006 + 1.20) * 0.5, 6)   # будни вне окон

    def test_price_anthropic_cache_write(self):
        m = self.reg.model("an", "son")
        u = Usage(miss=1000000, hit=1000000, write=1000000, out=1000000)
        self.assertAlmostEqual(usage.price_usd(m.price, u, 0), 2.0 + 0.2 + 2.5 + 10.0, 6)

    def test_no_price(self):
        self.assertEqual(usage.price_usd(None, Usage(miss=5)), 0.0)

    def test_record_has_no_content_and_filters_meta(self):
        usage.record({"job": "J1", "role": "pm", "profile": "x y!", "rev": 3, "prompt_hash": "abc"}, "ds", "flash", Usage(miss=5, hit=3, out=2), 0.001, 12,
                     root=self.root)
        rows = usage.read_rows(self.root)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["job"], r["rev"], r["in"], r["in_hit"], r["out"], r["ok"]), ("J1", 3, 8, 3, 2, True))
        self.assertIsNone(r["profile"])      # недопустимые символы отброшены
        for k in ("prompt", "messages", "text", "content", "system"):
            self.assertNotIn(k, r)

    def test_aggregate(self):
        for lat, ok in ((100, True), (300, True), (200, True), (50, False)):
            usage.record({"profile": "p1"}, "ds", "flash", Usage(miss=10, hit=10, out=5), 0.01, lat, ok=ok, err=None if ok else "server", root=self.root)
        usage.record_format_error({"profile": "p1"}, "ds", "flash", ["BAD_ENUM:$.a"], root=self.root)
        g = usage.aggregate(usage.read_rows(self.root), "profile")["p1"]
        self.assertEqual((g["calls"], g["errors"], g["fmt_errors"], g["in"]), (4, 1, 1, 80))
        self.assertEqual(g["lat_p50_ms"], 200)
        self.assertAlmostEqual(g["usd"], 0.04)
        self.assertAlmostEqual(g["cache_hit_rate"], 0.5)

    def test_client_records_every_call_and_costs(self):
        self.srv.push("/chat/completions", R(200, ds_ok("ok", hit=900000, miss=100000, completion=1000000)))
        c = self.client()
        resp = c(Req(system="S", messages=[user("hi")], meta={"job": "J9", "role": "r"}))
        self.assertEqual((resp.usage.hit, resp.usage.miss, resp.usage.out), (900000, 100000, 1000000))
        row = usage.read_rows(self.root)[-1]
        self.assertEqual((row["job"], row["provider"], row["model"], row["in_hit"], row["ok"]), ("J9", "ds", "flash", 900000, True))
        self.assertEqual(len(row["prompt_hash"]), 12)
        want = usage.price_usd(self.reg.model("ds", "flash").price, resp.usage)
        self.assertAlmostEqual(row["usd"], want, 5)
        self.assertAlmostEqual(resp.usd, want, 5)


class TestTransport(_llm.LLMBase):
    def post(self, path="/x", retry=None, timeouts=None, body=b"{}", total=None):
        return self.tr.request("POST", self.srv.url + path, {"Content-Type": "application/json"}, body, timeouts or {"connect_s": 2, "read_s": 2, "total_s": 10},
                               retry or {"max": 3, "base_s": 1, "max_s": 8, "on": [429, 500, 503]}, total_s=total)

    def test_success_keepalive_reuse(self):
        self.srv.push("/x", R(200, json={"a": 1}), R(200, json={"a": 2}))
        self.assertEqual(self.post().status, 200)
        self.assertEqual(self.post().status, 200)
        self.assertEqual(self.srv.connections, 1)

    def test_retry_429_503_exponential_then_ok(self):
        self.srv.push("/x", R(429, json={}), R(503, json={}), R(200, json={"ok": 1}))
        rep = self.post()
        self.assertEqual((rep.status, rep.retries, self.srv.count("/x")), (200, 2, 3))
        self.assertEqual(self.sleeps, [0.75, 1.5])          # base*2^n * (0.5+0.5*rand=0.75)

    def test_retry_exhausted_returns_last_status(self):
        self.srv.default["/x"] = R(503, json={})
        rep = self.post(retry={"max": 2, "base_s": 1, "max_s": 8, "on": [503]})
        self.assertEqual((rep.status, rep.retries, self.srv.count("/x")), (503, 2, 3))

    def test_delay_capped_by_max_s(self):
        self.srv.default["/x"] = R(500, json={})
        self.post(retry={"max": 5, "base_s": 4, "max_s": 6, "on": [500]})
        self.assertTrue(all(d <= 6 for d in self.sleeps))
        self.assertEqual(len(self.sleeps), 5)

    def test_retry_after_honored(self):
        self.srv.push("/x", R(429, json={}, headers={"Retry-After": "5"}), R(200, json={}))
        self.post()
        self.assertEqual(self.sleeps, [5.0])

    def test_no_retry_on_client_errors(self):
        for st in (400, 401, 402, 422):
            self.srv.default["/e%d" % st] = R(st, json={"error": {"message": "x"}})
            rep = self.post("/e%d" % st)
            self.assertEqual(rep.status, st)
        self.assertEqual(self.srv.count(), 4)           # по одному запросу, без повторов
        self.assertEqual(self.sleeps, [])

    def test_read_timeout_is_error_without_retry(self):
        self.srv.push("/x", R(200, json={}, delay=1.0))
        with self.assertRaises(LLMError) as c:
            self.post(timeouts={"connect_s": 1, "read_s": 0.2, "total_s": 5})
        self.assertEqual(c.exception.code, "timeout")
        self.assertEqual(c.exception.fallback_kind, "timeout")
        self.assertEqual(self.srv.count("/x"), 1)

    def test_connection_refused(self):
        tr = Transport()
        with self.assertRaises(LLMError) as c:
            tr.request("POST", "http://127.0.0.1:1/x", {}, b"{}", {"connect_s": 1, "read_s": 1, "total_s": 3}, {"max": 0})
        self.assertEqual(c.exception.code, "network")

    def test_stale_keepalive_reconnects_once(self):
        self.srv.push("/x", R(200, json={"n": 1}), R(200, json={"n": 2}))
        self.post()
        for c in self.tr._pool.values():
            c.sock.close()              # соединение «умерло» на стороне клиента
        self.assertEqual(self.post().status, 200)

    def test_server_drop_is_network_error(self):
        self.srv.push("/x", R(drop=True))
        with self.assertRaises(LLMError) as c:
            self.post()
        self.assertEqual(c.exception.code, "network")

    def test_total_deadline(self):
        self.srv.default["/x"] = R(503, json={})
        rep = self.post(retry={"max": 9, "base_s": 3, "max_s": 8, "on": [503]}, total=2)
        self.assertEqual(rep.status, 503)
        self.assertEqual(self.sleeps, [])       # пауза вышла бы за общий таймаут: повторов нет


class TestClientErrors(_llm.LLMBase):
    def test_401_no_retry_and_logged(self):
        self.srv.push("/chat/completions", R(401, json={"error": {"message": "Incorrect API key"}}))
        with self.assertRaises(LLMError) as c:
            self.client()(Req(messages=[user("x")], meta={"job": "J"}))
        self.assertEqual((c.exception.code, c.exception.status), ("auth", 401))
        self.assertEqual(self.srv.count(), 1)
        row = usage.read_rows(self.root)[-1]
        self.assertEqual((row["ok"], row["err"]), (False, "auth"))

    def test_status_codes_map(self):
        for st, code in ((402, "billing"), (422, "invalid_params"), (400, "bad_request")):
            self.srv.push("/chat/completions", R(st, json={"error": {"message": "m"}}))
            with self.assertRaises(LLMError) as c:
                self.client()(Req(messages=[user("x")]))
            self.assertEqual(c.exception.code, code)
        self.assertEqual(self.srv.count(), 3)

    def test_429_retried_then_ok(self):
        self.srv.push("/chat/completions", R(429, json={}), R(503, json={}), R(200, ds_ok("fine")))
        r = self.client()(Req(messages=[user("x")]))
        self.assertEqual((r.text, r.retries), ("fine", 2))
        self.assertEqual(usage.read_rows(self.root)[-1]["retries"], 2)

    def test_503_exhausted_is_server_error(self):
        self.srv.default["/chat/completions"] = R(503, json={"error": {"message": "busy"}})
        with self.assertRaises(LLMError) as c:
            self.client()(Req(messages=[user("x")]))
        self.assertEqual((c.exception.code, c.exception.fallback_kind, c.exception.retries), ("server", "http_5xx", 3))

    def test_garbage_and_empty_body(self):
        self.srv.push("/chat/completions", R(200, raw=b"<html>not json"), R(200, raw=b""), R(200, json=[1, 2]), R(200, json={"choices": []}))
        for code in ("bad_body", "empty", "bad_body", "bad_body"):
            with self.assertRaises(LLMError) as c:
                self.client()(Req(messages=[user("x")]))
            self.assertEqual(c.exception.code, code)

    def test_missing_key(self):
        c = self.client(environ={"TB_KEYS_DIR": os.path.join(self.root, "nokeys")})
        with self.assertRaises(LLMError) as e:
            c(Req(messages=[user("x")]))
        self.assertEqual(e.exception.code, "no_key")
        self.assertEqual(self.srv.count(), 0)

    def test_disabled_provider(self):
        d = _llm.doc(self.srv.url)
        d["providers"]["ds"]["enabled"] = False
        self.reg = providers.parse(d)
        with self.assertRaises(LLMError) as e:
            self.client()(Req(messages=[user("x")]))
        self.assertEqual(e.exception.code, "disabled")


class TestBudget(_llm.LLMBase):
    def seed(self, usd, tokens=0, when=None, provider="ds"):
        usage.record({}, provider, "flash", Usage(miss=tokens // 2, out=tokens - tokens // 2), usd, 1, root=self.root, now=when or time.time())

    def test_ok_warn_exhausted_and_event(self):
        b = budget.Budget({"day_usd": 1.0, "warn_frac": 0.8}, root=self.root)
        self.assertEqual(b.check()["state"], "ok")
        self.seed(0.85)
        self.assertEqual(b.check()["state"], "warn")
        self.seed(0.2)
        with self.assertRaises(LLMError) as c:
            b.check()
        self.assertEqual((c.exception.code, c.exception.fallback_kind), ("budget", "budget"))
        evs = [e for e in bus.read(0, 50, root=self.root) if e["type"] == "limit_hit"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["d"]["which"], "brain_budget")
        with self.assertRaises(LLMError):
            b.check()
        self.assertEqual(len([e for e in bus.read(0, 50, root=self.root) if e["type"] == "limit_hit"]), 1)      # один раз на окно

    def test_hour_and_token_caps_and_window_rollover(self):
        now = ts(2026, 10, 28, 12) + 600
        b = budget.Budget({"hour_usd": 0.5, "day_tokens": 1000}, root=self.root, clock=lambda: now, emit=False)
        self.seed(0.6, when=now - 5000)             # прошлый час: не считается в hour
        self.assertEqual(b.status()["state"], "ok")
        self.seed(0.5, tokens=10, when=now - 100)
        self.assertEqual(b.status()["key"], "hour_usd")
        b2 = budget.Budget({"day_tokens": 100}, root=self.root, clock=lambda: now, emit=False)
        self.seed(0.0, tokens=200, when=now - 50)
        self.assertEqual(b2.status()["state"], "exhausted")
        later = budget.Budget({"hour_usd": 0.5}, root=self.root, clock=lambda: now + 3600, emit=False)
        self.assertEqual(later.status()["state"], "ok")         # новое окно

    def test_per_provider_cap_isolated(self):
        b = budget.Budget({"providers": {"ds": {"day_usd": 0.1}}}, root=self.root, emit=False)
        self.seed(0.5, provider="an")
        self.assertEqual(b.status("ds")["state"], "ok")
        self.seed(0.2, provider="ds")
        self.assertEqual(b.status("ds")["state"], "exhausted")
        self.assertEqual(b.status("an")["state"], "ok")

    def test_client_refuses_without_request_when_exhausted(self):
        self.seed(5.0)
        with self.assertRaises(LLMError) as c:
            self.client()(Req(messages=[user("x")]))
        self.assertEqual(c.exception.code, "budget")
        self.assertEqual(self.srv.count(), 0)

    def test_call_that_exhausts_publishes_event(self):
        self.srv.push("/chat/completions", R(200, ds_ok("a", miss=1000000, completion=1000000)))
        c = self.client(budget=budget.Budget({"day_usd": 0.05}, root=self.root))
        c(Req(messages=[user("x")]))            # этот вызов дороже потолка: ответ получен, событие опубликовано
        self.assertEqual(len([e for e in bus.read(0, 50, root=self.root) if e["type"] == "limit_hit"]), 1)
        with self.assertRaises(LLMError):
            c(Req(messages=[user("y")]))

    def test_job_meter(self):
        self.srv.push("/chat/completions", R(200, ds_ok("a", miss=1000000, completion=1000000)))
        m = budget.JobMeter(max_usd=0.01)
        c = self.client()
        c(Req(messages=[user("x")], meter=m))
        with self.assertRaises(LLMError) as e:
            c(Req(messages=[user("y")], meter=m))
        self.assertEqual(e.exception.code, "budget")

    def test_from_registry(self):
        b = budget.from_registry(self.reg, root=self.root)
        self.assertEqual(b.limits["day_usd"], 2.0)
        self.assertEqual(b.limits["providers"]["ds"]["day_usd"], 3.0)


if __name__ == "__main__":
    unittest.main()
