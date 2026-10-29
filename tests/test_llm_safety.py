"""WP11-C1: утечки ключей (окружение, аргументы, логи, исключения, учёт, вывод CLI) и CLI `./tb llm` без живых запросов. Ключи поддельные."""
import contextlib
import io
import json
import os
import unittest

import _bootstrap  # noqa: F401
import _llm
from fakes.fake_llm_http import R, ds_ok
from tb.llm import cli, keys, providers, usage
from tb.llm.types import LLMError, Req, user

KEYS = (_llm.FAKE_DS_KEY, _llm.FAKE_AN_KEY)


def scan_tree(root, skip_dir):
    hits = []
    for d, _, fs in os.walk(root):
        if os.path.abspath(d).startswith(os.path.abspath(skip_dir)):
            continue
        for f in fs:
            try:
                with open(os.path.join(d, f), "rb") as fh:
                    data = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            hits += [f for k in KEYS if k in data or k[-12:] in data]
    return hits


class TestNoLeaks(_llm.LLMBase):
    def test_error_echoing_key_is_redacted(self):
        self.srv.push("/chat/completions", R(401, json={"error": {"message": "Incorrect API key provided: %s. See docs" % _llm.FAKE_DS_KEY}}))
        with self.assertRaises(LLMError) as c:
            self.client()(Req(messages=[user("q")]))
        e = c.exception
        for text in (str(e), e.msg, repr(e), repr(e.args)):
            self.assertNotIn(_llm.FAKE_DS_KEY, text)
            self.assertNotIn("FAKEDEEPSEEK", text)

    def test_anthropic_error_echo_redacted(self):
        self.srv.push("/v1/messages", R(401, json={"error": {"message": "invalid x-api-key %s" % _llm.FAKE_AN_KEY}}))
        with self.assertRaises(LLMError) as c:
            self.client("an", "son")(Req(messages=[user("q")]))
        self.assertNotIn("FAKEANTHROPIC", str(c.exception))

    def test_environ_args_state_and_logs_clean(self):
        before = dict(os.environ)
        self.srv.push("/chat/completions", R(200, ds_ok("x")), R(401, json={"error": {"message": "k=" + _llm.FAKE_DS_KEY}}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            c = self.client()
            c(Req(system="SYS", messages=[user("секретный промпт Z9")], meta={"job": "J"}))
            with self.assertRaises(LLMError):
                c(Req(messages=[user("q")]))
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(scan_tree(self.root, self.keys), [])
        raw = open(usage.calls_file(self.root), encoding="utf-8").read()
        self.assertNotIn("Z9", raw)                          # тексты промптов в учёт не пишутся
        self.assertNotIn("SYS", raw)

    def test_secret_object_not_serializable_or_printable(self):
        s = keys.load("ds.env", "DS_KEY", self.env)
        import pickle
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        self.assertNotIn(_llm.FAKE_DS_KEY, repr(s) + str(s) + "%s" % (s,) + json.dumps({"k": str(s)}))
        c = self.client()
        self.assertNotIn("FAKE", repr(c.adapter.__dict__))

    def test_key_only_in_auth_header(self):
        self.srv.push("/chat/completions", R(200, ds_ok("x")))
        self.client()(Req(system="S", messages=[user("q")], meta={"job": "J"}))
        rq = self.srv.requests[0]
        hits = [k for k, v in rq["headers"].items() if _llm.FAKE_DS_KEY in v]
        self.assertEqual(hits, ["authorization"])
        self.assertNotIn(_llm.FAKE_DS_KEY, rq["path"])


class TestCli(_llm.LLMBase):
    def run_cli(self, *args):
        out = io.StringIO()
        os.environ["TB_ROOT"] = self.root
        os.environ["TB_KEYS_DIR"] = self.keys
        try:
            with contextlib.redirect_stdout(out):
                code = cli.main(list(args))
        finally:
            os.environ.clear()
            os.environ.update(self._env_before)
        return code, out.getvalue()

    def write_cfg(self, d):
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        with open(os.path.join(self.root, "config", "providers.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_providers_lists_flags_and_masks_keys(self):
        self.write_cfg(_llm.doc(self.srv.url))
        code, text = self.run_cli("providers")
        o = json.loads(text)
        self.assertEqual(code, 0)
        ds = [p for p in o["providers"] if p["id"] == "ds"][0]
        self.assertTrue(ds["key"]["present"])
        self.assertTrue(ds["key"]["mask"].startswith("…"))
        self.assertEqual(ds["models"]["flash"]["json_mode"], True)
        for k in KEYS:
            self.assertNotIn(k, text)
            self.assertNotIn(k[-8:], text)
        self.assertEqual(self.srv.count(), 0)

    def test_check_without_live_makes_no_requests(self):
        self.write_cfg(_llm.doc(self.srv.url))
        code, text = self.run_cli("check")
        o = json.loads(text)
        self.assertEqual((code, o["ok"], o["live"]), (0, True, False))
        self.assertEqual(self.srv.count(), 0)
        self.assertIn("--live", o["note"])

    def test_check_without_key_gives_clear_message(self):
        self.write_cfg(_llm.doc(self.srv.url))
        os.remove(os.path.join(self.keys, "ds.env"))
        code, text = self.run_cli("check", "--provider", "ds", "--live")
        o = json.loads(text)
        row = o["checked"][0]
        self.assertFalse(row["key"]["present"])
        self.assertIn("ds.env", row["note"])
        self.assertIn("нет ключа", json.dumps(row["live"], ensure_ascii=False))
        self.assertEqual(self.srv.count(), 0)

    def test_check_live_probe_and_ping_against_fake(self):
        self.write_cfg(_llm.doc(self.srv.url))
        self.srv.push("/models", R(200, json={"data": [{"id": "m-flash"}, {"id": "other"}]}))
        self.srv.push("/chat/completions", R(200, ds_ok('{"ok":true}', hit=5, miss=5)))
        code, text = self.run_cli("check", "--live", "--provider", "ds", "--model", "flash")
        o = json.loads(text)
        live = o["checked"][0]["live"]
        self.assertEqual((code, live["model_name_in_list"], live["models_count"], live["ping"]["in_hit"]), (0, True, 2, 5))
        self.assertEqual(self.srv.requests[0]["method"], "GET")
        for k in KEYS:
            self.assertNotIn(k, text)

    def test_check_live_name_missing_and_error(self):
        self.write_cfg(_llm.doc(self.srv.url))
        self.srv.push("/models", R(200, json={"data": [{"id": "zzz"}]}))
        self.srv.push("/chat/completions", R(402, json={"error": {"message": "no balance"}}))
        code, text = self.run_cli("check", "--live", "--provider", "ds", "--model", "flash")
        o = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(o["checked"][0]["live"]["error"], "billing")

    def test_check_live_skips_disabled_provider(self):
        d = _llm.doc(self.srv.url)
        d["providers"]["ds"]["enabled"] = False
        self.write_cfg(d)
        code, text = self.run_cli("check", "--live", "--provider", "ds")
        self.assertIn("отключён", text)
        self.assertEqual(self.srv.count(), 0)

    def test_usage_command(self):
        self.write_cfg(_llm.doc(self.srv.url))
        from tb.llm.types import Usage
        usage.record({"profile": "p"}, "ds", "flash", Usage(miss=10, out=5), 0.5, 100, root=self.root)
        code, text = self.run_cli("usage", "--by", "provider")
        o = json.loads(text)
        self.assertEqual((code, o["groups"]["ds"]["calls"], o["today"]["calls"]), (0, 1, 1))
        self.assertAlmostEqual(o["today"]["usd"], 0.5)

    def test_bad_config_reports(self):
        self.write_cfg({"version": 9})
        code, text = self.run_cli("providers")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)["ok"])


if __name__ == "__main__":
    unittest.main()
