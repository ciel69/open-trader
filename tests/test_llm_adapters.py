"""WP11-C1: адаптеры DeepSeek / Anthropic / OpenAI-совместимый на фейковом HTTP: формат запроса, разбор ответа, usage и кэш, tool calls, thinking."""
import json
import unittest

import _bootstrap  # noqa: F401
import _llm
from fakes.fake_llm_http import R, an_ok, ds_ok
from tb.llm import types, usage
from tb.llm.types import LLMError, Req, assistant, tool_result, user

TOOL = {"name": "get_brief", "description": "досье", "schema": {"type": "object", "properties": {"scope": {"type": "string"}}, "required": ["scope"],
                                                                "additionalProperties": False}}
LOOSE = {"name": "loose", "description": "x", "schema": {"type": "object", "properties": {"a": {"type": "string"}}}}
CHAT = "/chat/completions"


class Contract(object):
    """Общие проверки LLMClient.complete для трёх адаптеров (одинаковый набор тестов)."""
    pid, mkey, path = None, None, None

    def ok_body(self, text):
        raise NotImplementedError

    def test_plain_text(self):
        self.srv.push(self.path, R(200, self.ok_body("привет")))
        r = self.client(self.pid, self.mkey)(Req(system="S", messages=[user("q")], max_tokens=50, temperature=0.1))
        self.assertEqual((r.text, r.finish, r.tool_calls), ("привет", "stop", []))
        self.assertGreaterEqual(r.latency_ms, 0)
        self.assertTrue(r.raw_id)

    def test_model_name_from_config_and_max_tokens(self):
        self.srv.push(self.path, R(200, self.ok_body("x")))
        self.client(self.pid, self.mkey)(Req(messages=[user("q")], max_tokens=77))
        body = self.srv.requests[-1]["json"]
        self.assertEqual(body["model"], self.reg.model(self.pid, self.mkey).name)
        self.assertEqual(body["max_tokens"], 77)

    def test_retry_then_ok(self):
        self.srv.push(self.path, R(503, json={}), R(200, self.ok_body("a")))
        r = self.client(self.pid, self.mkey)(Req(messages=[user("q")]))
        self.assertEqual((r.text, r.retries, self.srv.count(self.path)), ("a", 1, 2))

    def test_auth_error_no_retry(self):
        self.srv.push(self.path, R(401, json={"error": {"message": "bad"}}))
        with self.assertRaises(LLMError):
            self.client(self.pid, self.mkey)(Req(messages=[user("q")]))
        self.assertEqual(self.srv.count(self.path), 1)

    def test_key_not_in_body(self):
        self.srv.push(self.path, R(200, self.ok_body("x")))
        self.client(self.pid, self.mkey)(Req(system="S", messages=[user("q")]))
        self.assertNotIn("FAKE", json.dumps(self.srv.requests[-1]["json"]))


class TestDeepSeek(Contract, _llm.LLMBase):
    pid, mkey, path = "ds", "flash", CHAT

    def ok_body(self, text):
        return ds_ok(text)

    def test_headers_and_messages(self):
        self.srv.push(CHAT, R(200, ds_ok("x")))
        self.client()(Req(system="SYS", messages=[user("q"), assistant("a"), user("q2")], max_tokens=10, temperature=0.2))
        rq = self.srv.requests[-1]
        self.assertEqual(rq["headers"]["authorization"], "Bearer " + _llm.FAKE_DS_KEY)
        b = rq["json"]
        self.assertEqual([m["role"] for m in b["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual((b["temperature"], b["stream"]), (0.2, False))
        self.assertNotIn("tools", b)

    def test_cache_usage_fields(self):
        self.srv.push(CHAT, R(200, ds_ok("x", hit=800, miss=200, completion=50, reasoning_tokens=30)))
        r = self.client()(Req(messages=[user("q")]))
        self.assertEqual((r.usage.hit, r.usage.miss, r.usage.inp, r.usage.out, r.usage.reasoning), (800, 200, 1000, 50, 30))
        row = usage.read_rows(self.root)[-1]
        self.assertEqual((row["in"], row["in_hit"], row["out"], row["reasoning"]), (1000, 800, 50, 30))

    def test_usage_without_cache_fields(self):
        b = ds_ok("x")
        r_body = b
        self.srv.push(CHAT, R(200, r_body))
        r = self.client()(Req(messages=[user("q")]))
        self.assertEqual((r.usage.hit, r.usage.miss), (0, 10))

    def test_json_mode_and_json_word(self):
        self.srv.push(CHAT, R(200, ds_ok("{}")))
        self.client()(Req(system="Отвечай объектом", messages=[user("q")], response_format="json"))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["response_format"], {"type": "json_object"})
        self.assertIn("json", b["messages"][0]["content"].lower())

    def test_tools_choice_and_tool_calls(self):
        self.srv.push(CHAT, R(200, ds_ok("", tool_calls=[("c1", "get_brief", {"scope": "start"}), ("c2", "get_brief", '{"scope":"event"}')])))
        r = self.client()(Req(messages=[user("q")], tools=[TOOL], tool_choice={"name": "get_brief"}))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["tools"][0]["function"]["name"], "get_brief")
        self.assertNotIn("strict", b["tools"][0]["function"])          # у flash strict=false
        self.assertEqual(b["tool_choice"], {"type": "function", "function": {"name": "get_brief"}})
        self.assertNotIn("response_format", b)
        self.assertEqual((r.finish, [(c.id, c.name, c.args) for c in r.tool_calls]),
                         ("tool_calls", [("c1", "get_brief", {"scope": "start"}), ("c2", "get_brief", {"scope": "event"})]))

    def test_tool_choice_strings(self):
        for tc in ("auto", "none", "required"):
            self.srv.push(CHAT, R(200, ds_ok("x")))
            self.client()(Req(messages=[user("q")], tools=[TOOL], tool_choice=tc))
            self.assertEqual(self.srv.requests[-1]["json"]["tool_choice"], tc)

    def test_truncated_tool_args_flagged(self):
        self.srv.push(CHAT, R(200, ds_ok("", tool_calls=[("c1", "get_brief", '{"scope": "sta')], finish="length")))
        r = self.client()(Req(messages=[user("q")], tools=[TOOL]))
        self.assertEqual(r.finish, "length")
        self.assertEqual((r.tool_calls[0].error, r.tool_calls[0].args), ("bad_json", {}))

    def test_history_with_tool_results_and_reasoning_passback(self):
        hist = [user("q"), assistant("", [types.ToolCall("c1", "get_brief", {"scope": "start"}, '{"scope":"start"}')], reasoning="думаю"),
                tool_result("c1", "get_brief", '{"ok":true}')]
        self.srv.push(CHAT, R(200, ds_ok("готово")), R(200, ds_ok("готово")))
        self.client()(Req(messages=hist, tools=[TOOL]))
        m = self.srv.requests[-1]["json"]["messages"]
        self.assertEqual(m[1]["reasoning_content"], "думаю")
        self.assertEqual(m[1]["tool_calls"][0]["function"]["arguments"], '{"scope":"start"}')
        self.assertEqual((m[2]["role"], m[2]["tool_call_id"]), ("tool", "c1"))
        self.client()(Req(messages=hist))                       # без tools reasoning_content не возвращается
        self.assertNotIn("reasoning_content", self.srv.requests[-1]["json"]["messages"][1])

    def test_reasoning_content_parsed_into_message(self):
        self.srv.push(CHAT, R(200, ds_ok("", tool_calls=[("c1", "get_brief", {"scope": "a"})], reasoning="мысли")))
        r = self.client()(Req(messages=[user("q")], tools=[TOOL]))
        self.assertEqual(r.reasoning, "мысли")
        self.assertEqual(r.message["reasoning"], "мысли")
        self.assertEqual(r.message["tool_calls"][0]["name"], "get_brief")

    def test_thinking_param_drops_temperature(self):
        self.srv.push(CHAT, R(200, ds_ok("x")), R(200, ds_ok("x")))
        c = self.client()
        c(Req(messages=[user("q")], thinking="high", temperature=0.1))
        b = self.srv.requests[-1]["json"]
        self.assertEqual((b["thinking"], b["reasoning_effort"]), ({"type": "enabled"}, "high"))
        self.assertNotIn("temperature", b)
        c(Req(messages=[user("q")], thinking="off", temperature=0.1))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["thinking"], {"type": "disabled"})
        self.assertEqual(b["temperature"], 0.1)

    def test_strict_tools_use_beta_url_only_when_compatible(self):
        self.srv.push("/beta" + CHAT, R(200, ds_ok("x")))
        self.client("ds", "pro")(Req(messages=[user("q")], tools=[TOOL, LOOSE]))
        rq = self.srv.requests[-1]
        self.assertEqual(rq["path"], "/beta" + CHAT)
        fns = {t["function"]["name"]: t["function"] for t in rq["json"]["tools"]}
        self.assertTrue(fns["get_brief"]["strict"])
        self.assertNotIn("strict", fns["loose"])
        self.srv.push(CHAT, R(200, ds_ok("x")))
        self.client("ds", "pro")(Req(messages=[user("q")], tools=[LOOSE]))
        self.assertEqual(self.srv.requests[-1]["path"], CHAT)

    def test_empty_content_and_length(self):
        self.srv.push(CHAT, R(200, ds_ok("", finish="length")))
        r = self.client()(Req(messages=[user("q")]))
        self.assertEqual((r.text, r.finish, r.empty), ("", "length", True))

    def test_content_null(self):
        b = ds_ok("x")
        b["choices"][0]["message"]["content"] = None
        self.srv.push(CHAT, R(200, b))
        self.assertEqual(self.client()(Req(messages=[user("q")])).text, "")

    def test_tools_flag_enforced(self):
        d = _llm.doc(self.srv.url)
        d["providers"]["ds"]["models"]["flash"]["tools"] = False
        from tb.llm import providers
        self.reg = providers.parse(d)
        with self.assertRaises(LLMError) as c:
            self.client()(Req(messages=[user("q")], tools=[TOOL]))
        self.assertEqual(c.exception.code, "config")
        self.assertEqual(self.srv.count(), 0)


class TestOpenAICompat(Contract, _llm.LLMBase):
    pid, mkey, path = "loc", "q", "/v1" + CHAT

    def ok_body(self, text):
        return ds_ok(text)

    def test_no_auth_header_without_key(self):
        self.srv.push(self.path, R(200, ds_ok("x")))
        self.client("loc", "q")(Req(messages=[user("q")]))
        self.assertNotIn("authorization", self.srv.requests[-1]["headers"])

    def test_cached_tokens_standard_field(self):
        b = ds_ok("x")
        b["usage"] = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}}
        self.srv.push(self.path, R(200, b))
        r = self.client("loc", "q")(Req(messages=[user("q")]))
        self.assertEqual((r.usage.hit, r.usage.miss), (60, 40))

    def test_thinking_none_ignored(self):
        self.srv.push(self.path, R(200, ds_ok("x")))
        self.client("loc", "q")(Req(messages=[user("q")], thinking="high", temperature=0.3))
        b = self.srv.requests[-1]["json"]
        self.assertNotIn("thinking", b)
        self.assertEqual(b["temperature"], 0.3)


class TestAnthropic(Contract, _llm.LLMBase):
    pid, mkey, path = "an", "son", "/v1/messages"

    def ok_body(self, text):
        return an_ok(text)

    def test_headers_system_cache_control(self):
        self.srv.push(self.path, R(200, an_ok("x")))
        self.client("an", "son")(Req(system="SYS", messages=[user("q")], temperature=0.1))
        rq = self.srv.requests[-1]
        self.assertEqual((rq["headers"]["x-api-key"], rq["headers"]["anthropic-version"]), (_llm.FAKE_AN_KEY, "2023-06-01"))
        self.assertNotIn("authorization", rq["headers"])
        b = rq["json"]
        self.assertEqual(b["system"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual((b["temperature"], b["messages"]), (0.1, [{"role": "user", "content": [{"type": "text", "text": "q"}]}]))

    def test_cache_usage(self):
        self.srv.push(self.path, R(200, an_ok("x", inp=50, out=7, cache_read=900, cache_write=100)))
        r = self.client("an", "son")(Req(messages=[user("q")]))
        self.assertEqual((r.usage.miss, r.usage.hit, r.usage.write, r.usage.inp, r.usage.out), (50, 900, 100, 1050, 7))
        row = usage.read_rows(self.root)[-1]
        self.assertEqual((row["in"], row["in_hit"], row["in_write"]), (1050, 900, 100))
        want = (50 * 2.0 + 900 * 0.2 + 100 * 2.0 * 1.25 + 7 * 10.0) / 1e6
        self.assertAlmostEqual(row["usd"], want, 7)

    def test_tool_use_roundtrip(self):
        self.srv.push(self.path, R(200, an_ok("думаю", tool_uses=[("tu1", "get_brief", {"scope": "start"})])), R(200, an_ok("итог")))
        c = self.client("an", "son")
        r = c(Req(messages=[user("q")], tools=[TOOL], tool_choice="required"))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["tool_choice"], {"type": "any"})
        self.assertEqual(b["tools"][0]["input_schema"], TOOL["schema"])
        self.assertTrue(b["tools"][0]["strict"])
        self.assertEqual((r.finish, r.text, r.tool_calls[0].args), ("tool_calls", "думаю", {"scope": "start"}))
        am = r.message
        am["tool_calls"].append({"id": "tu2", "name": "get_brief", "args": {}, "raw_args": "{}"})
        msgs = [user("q"), am, tool_result("tu1", "get_brief", '{"ok":true}'), tool_result("tu2", "get_brief", "{}")]
        c(Req(messages=msgs, tools=[TOOL]))
        m = self.srv.requests[-1]["json"]["messages"]
        self.assertEqual([x["role"] for x in m], ["user", "assistant", "user"])             # tool_result склеены в один user
        self.assertEqual([x["type"] for x in m[1]["content"]], ["text", "tool_use", "tool_use"])
        self.assertEqual([x["tool_use_id"] for x in m[2]["content"]], ["tu1", "tu2"])

    def test_structured_output_via_respond_tool(self):
        sch = {"type": "object", "properties": {"d": {"type": "string"}}, "required": ["d"], "additionalProperties": False}
        self.srv.push(self.path, R(200, an_ok("", tool_uses=[("t", "respond", {"d": "hold"})], stop="tool_use")))
        r = self.client("an", "son")(Req(system="S", messages=[user("q")], response_format={"schema": sch}))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["tool_choice"], {"type": "tool", "name": "respond"})
        self.assertEqual(b["tools"][0]["input_schema"], sch)
        self.assertEqual((json.loads(r.text), r.tool_calls), ({"d": "hold"}, []))

    def test_thinking_budget_and_blocks_passback(self):
        self.srv.push(self.path, R(200, an_ok("", tool_uses=[("tu1", "get_brief", {"scope": "s"})], thinking="мысль")), R(200, an_ok("ok")))
        c = self.client("an", "son")
        r = c(Req(messages=[user("q")], tools=[TOOL], thinking="medium", max_tokens=500, temperature=0.2))
        b = self.srv.requests[-1]["json"]
        self.assertEqual(b["thinking"], {"type": "enabled", "budget_tokens": 4096})
        self.assertEqual(b["max_tokens"], 4596)
        self.assertNotIn("temperature", b)
        self.assertEqual(r.reasoning, "мысль")
        c(Req(messages=[user("q"), r.message, tool_result("tu1", "get_brief", "{}")], tools=[TOOL], thinking="medium"))
        blocks = self.srv.requests[-1]["json"]["messages"][1]["content"]
        self.assertEqual(blocks[0]["type"], "thinking")
        self.assertEqual(blocks[0]["signature"], "SIG")

    def test_stop_reasons(self):
        for stop, fin in (("max_tokens", "length"), ("refusal", "filtered"), ("end_turn", "stop")):
            self.srv.push(self.path, R(200, an_ok("x", stop=stop)))
            self.assertEqual(self.client("an", "son")(Req(messages=[user("q")])).finish, fin)

    def test_529_not_in_default_is_error_but_503_retried(self):
        self.srv.push(self.path, R(529, json={"error": {"message": "overloaded"}}))
        with self.assertRaises(LLMError) as c:
            self.client("an", "son")(Req(messages=[user("q")]))
        self.assertEqual(c.exception.code, "server")


if __name__ == "__main__":
    unittest.main()


class OpenAICompatThinkingBudget(_llm.LLMBase):
    """У DeepSeek рассуждения входят в max_tokens: при thinking адаптер добавляет запас (как budget_tokens у Anthropic), не выше max_out модели."""

    def test_thinking_adds_budget_to_max_tokens(self):
        self.srv.push(CHAT, R(200, ds_ok("x")), R(200, ds_ok("x")), R(200, ds_ok("x")))
        c = self.client()
        c(Req(messages=[user("q")], thinking="low", max_tokens=700))
        self.assertEqual(self.srv.requests[-1]["json"]["max_tokens"], 700 + 1024)
        c(Req(messages=[user("q")], thinking="off", max_tokens=700, temperature=0.1))
        self.assertEqual(self.srv.requests[-1]["json"]["max_tokens"], 700)
        self.reg.model("ds", "flash").max_out = 8192
        c(Req(messages=[user("q")], thinking="high", max_tokens=5000))
        self.assertEqual(self.srv.requests[-1]["json"]["max_tokens"], 8192)                # потолок max_out модели
