"""WP11-C1: проверка JSON по схеме (schema.py) и цикл инструментов (toolloop.py)."""
import json
import unittest

import _bootstrap  # noqa: F401
import _gw
import _llm
from fakes.fake_llm_http import R, ds_ok
from tb.llm import schema, toolloop, usage
from tb.llm.types import LLMError, Req, Resp, ToolCall, Usage, user

SCH = {"type": "object", "required": ["decision", "why"], "additionalProperties": False,
       "properties": {"decision": {"type": "string", "enum": ["hold", "close"]}, "why": {"type": "string", "maxLength": 20, "minLength": 1},
                      "qty": {"type": "number", "minimum": 0, "maximum": 10}, "n": {"type": "integer"},
                      "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2}}}


class TestExtract(unittest.TestCase):
    def test_garbage_around(self):
        o, e = schema.extract_json('Вот ответ: {"a": 1} спасибо')
        self.assertEqual((o, e), ({"a": 1}, None))

    def test_fenced(self):
        self.assertEqual(schema.extract_json('```json\n{"a": "}"}\n```')[0], {"a": "}"})

    def test_two_objects_takes_first(self):
        self.assertEqual(schema.extract_json('{"a":1}{"b":2}')[0], {"a": 1})

    def test_truncated(self):
        self.assertEqual(schema.extract_json('{"a": "xx", "b": {"c": 1')[1], "TRUNCATED")

    def test_errors(self):
        self.assertEqual(schema.extract_json("")[1], "EMPTY")
        self.assertEqual(schema.extract_json("нет json")[1], "NO_JSON")
        self.assertEqual(schema.extract_json('{"a":1,"a":2}')[1], "DUP_KEY")
        self.assertEqual(schema.extract_json('{"a":NaN}')[1], "NOT_FINITE")
        self.assertEqual(schema.extract_json('{"a":1,}')[1], "BAD_JSON")
        self.assertEqual(schema.extract_json('{"a":"%s"}' % ("x" * 9000))[1], "TOO_BIG")
        self.assertEqual(schema.extract_json("[1,2]")[1], "NO_JSON")


class TestValidate(unittest.TestCase):
    def v(self, o):
        return schema.validate(SCH, o)

    def test_ok(self):
        self.assertEqual(self.v({"decision": "hold", "why": "x", "qty": 1.5, "n": 2, "tags": ["a"]}), [])

    def test_codes(self):
        self.assertIn("BAD_ENUM:$.decision", self.v({"decision": "buy", "why": "x"}))
        self.assertIn("MISSING:$.why", self.v({"decision": "hold"}))
        self.assertIn("EXTRA_KEY:$.foo", self.v({"decision": "hold", "why": "x", "foo": 1}))
        self.assertIn("TOO_LONG:$.why", self.v({"decision": "hold", "why": "x" * 21}))
        self.assertIn("TOO_SHORT:$.why", self.v({"decision": "hold", "why": ""}))
        self.assertIn("RANGE:$.qty", self.v({"decision": "hold", "why": "x", "qty": 11}))
        self.assertIn("TOO_MANY:$.tags", self.v({"decision": "hold", "why": "x", "tags": ["a", "b", "c"]}))
        self.assertIn("BAD_TYPE:$.tags[0]", self.v({"decision": "hold", "why": "x", "tags": [1]}))

    def test_types_strict(self):
        self.assertIn("BAD_TYPE:$.n", self.v({"decision": "hold", "why": "x", "n": True}))
        self.assertIn("BAD_TYPE:$.n", self.v({"decision": "hold", "why": "x", "n": 1.5}))
        self.assertIn("BAD_TYPE:$.qty", self.v({"decision": "hold", "why": "x", "qty": "1"}))
        self.assertIn("BAD_TYPE:$.qty", self.v({"decision": "hold", "why": "x", "qty": True}))
        self.assertEqual(schema.validate({"type": "object"}, [1]), ["BAD_TYPE:$"])

    def test_extra_default_false_and_explicit_true(self):
        self.assertTrue(schema.validate({"type": "object", "properties": {}}, {"a": 1}))
        self.assertFalse(schema.validate({"type": "object", "properties": {}, "additionalProperties": True}, {"a": 1}))

    def test_misc(self):
        self.assertTrue(schema.validate({"type": "string", "pattern": "^a+$"}, "b"))
        self.assertEqual(schema.validate({"type": ["string", "null"]}, None), [])
        self.assertEqual(schema.validate({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 3), [])
        self.assertEqual(schema.validate({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 3.5), ["NO_MATCH:$"])
        self.assertEqual(schema.validate({"const": 1}, 2), ["BAD_CONST:$"])

    def test_errors_capped(self):
        self.assertLessEqual(len(schema.validate({"type": "array", "items": {"type": "string"}}, [1] * 100)), schema.MAX_ERRORS)


class Scripted(object):
    """Фейковый call: выдаёт Resp по очереди, запоминает запросы."""

    def __init__(self, *resps):
        self.resps, self.reqs, self.fmt = list(resps), [], []

    def __call__(self, req):
        self.reqs.append(req)
        r = self.resps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def note_format_error(self, resp, codes):
        self.fmt.append(codes)


def T(text="", calls=None, finish=None, out=1):
    return Resp(text, [ToolCall(*c) if isinstance(c, tuple) else c for c in (calls or [])], Usage(miss=10, out=out),
                finish or ("tool_calls" if calls else "stop"))


TOOLS = [{"name": "ping", "description": "d", "schema": {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}},
         {"name": "other", "description": "d", "schema": {"type": "object", "properties": {}, "additionalProperties": False}}]


class TestAskJson(unittest.TestCase):
    def req(self):
        return Req(system="S", messages=[user("q")])

    def test_valid_first_try(self):
        call = Scripted(T('{"decision":"hold","why":"ok"}'))
        r = schema.ask_json(call, self.req(), SCH)
        self.assertTrue(r.ok)
        self.assertEqual((r.obj["decision"], r.attempts, r.action), ("hold", 1, "ok"))

    def test_reprompt_with_codes_without_quote_then_ok(self):
        call = Scripted(T('{"decision":"BUYBUY","why":"ok"}'), T('{"decision":"close","why":"ok"}'))
        r = schema.ask_json(call, self.req(), SCH)
        self.assertEqual((r.ok, r.attempts), (True, 2))
        hint = call.reqs[1].messages[-1]["content"]
        self.assertIn("КОДЫ: BAD_ENUM:$.decision", hint)
        self.assertNotIn("BUYBUY", hint)
        self.assertEqual(call.fmt, [["BAD_ENUM:$.decision"]])
        self.assertEqual(len(call.reqs[0].messages), 1)               # исходный запрос не изменён

    def test_exhausted_gives_no_action(self):
        call = Scripted(T("мусор"), T("{}"), T('{"decision":"x","why":"y"}'))
        r = schema.ask_json(call, self.req(), SCH, retries=2)
        self.assertEqual((r.ok, r.action, r.obj, r.attempts), (False, "none", None, 3))
        self.assertIn("BAD_ENUM:$.decision", r.errors)
        self.assertEqual(len(call.fmt), 3)

    def test_truncated_by_length(self):
        call = Scripted(T('{"decision":"ho', finish="length"), T('{"decision":"hold","why":"w"}'))
        r = schema.ask_json(call, self.req(), SCH)
        self.assertTrue(r.ok)
        self.assertIn("TRUNCATED", call.fmt[0])

    def test_empty_answer_counts_as_format_error(self):
        call = Scripted(T(""), T('{"decision":"hold","why":"w"}'))
        self.assertTrue(schema.ask_json(call, self.req(), SCH).ok)
        self.assertEqual(call.fmt, [["EMPTY"]])

    def test_llm_error_propagates(self):
        with self.assertRaises(LLMError):
            schema.ask_json(Scripted(LLMError("server")), self.req(), SCH)

    def test_usage_summed(self):
        call = Scripted(T("x", out=3), T('{"decision":"hold","why":"w"}', out=4))
        self.assertEqual(schema.ask_json(call, self.req(), SCH).usage.out, 7)


class TestToolLoop(unittest.TestCase):
    def run_loop(self, call, tools=TOOLS, run_tool=None, **kw):
        executed = []

        def rt(name, args):
            executed.append((name, args))
            return run_tool(name, args) if run_tool else {"ok": True, "v": 1}
        return toolloop.run(call, Req(system="S", messages=[user("q")], tools=tools, meta={"job": "J"}), rt, **kw), executed

    def test_direct_answer(self):
        res, ex = self.run_loop(Scripted(T("готово")))
        self.assertEqual((res.status, res.text, res.rounds, ex), ("ok", "готово", 1, []))

    def test_tool_round_then_answer_history(self):
        call = Scripted(T("", [("c1", "ping", {"x": 1}, '{"x":1}')]), T("итог"))
        res, ex = self.run_loop(call)
        self.assertEqual((res.status, ex, res.rounds), ("ok", [("ping", {"x": 1})], 2))
        roles = [m["role"] for m in call.reqs[1].messages]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(call.reqs[1].messages[2]["tool_call_id"], "c1")
        self.assertEqual(json.loads(call.reqs[1].messages[2]["content"]), {"ok": True, "v": 1})
        self.assertEqual(call.reqs[1].meta["round"], 2)
        self.assertEqual(res.calls, [{"name": "ping", "ok": True, "round": 1}])

    def test_parallel_calls_each_answered_in_order(self):
        call = Scripted(T("", [("a", "ping", {"x": 1}, ""), ("b", "other", {}, "")]), T("ok"))
        res, ex = self.run_loop(call)
        self.assertEqual([e[0] for e in ex], ["ping", "other"])
        self.assertEqual([m["tool_call_id"] for m in call.reqs[1].messages if m["role"] == "tool"], ["a", "b"])

    def test_reasoning_preserved_in_history(self):
        r1 = T("", [("c1", "ping", {}, "{}")])
        r1.reasoning = "рассуждение"
        call = Scripted(r1, T("ok"))
        self.run_loop(call)
        self.assertEqual(call.reqs[1].messages[1]["reasoning"], "рассуждение")

    def test_repeat_detector_blocks_third_identical_call(self):
        same = ("c", "ping", {"x": 1}, "")
        call = Scripted(T("", [same]), T("", [same]), T("", [same]), T("", [same]), T("fin"))
        res, ex = self.run_loop(call, repeat_limit=2, loop_abort=2)
        self.assertEqual(len(ex), 2)                         # третий и четвёртый не исполнены
        self.assertEqual(res.status, "loop")
        blocked = json.loads(call.reqs[3].messages[-1]["content"])
        self.assertEqual(blocked["error"], "REPEAT")

    def test_repeat_then_model_recovers(self):
        same = ("c", "ping", {"x": 1}, "")
        call = Scripted(T("", [same]), T("", [same]), T("", [same]), T("итог"))
        res, ex = self.run_loop(call)
        self.assertEqual((res.status, len(ex), res.loop_hits), ("ok", 2, 1))

    def test_different_args_not_a_repeat(self):
        call = Scripted(*[T("", [("c", "ping", {"x": i}, "")]) for i in range(4)] + [T("ok")])
        res, ex = self.run_loop(call)
        self.assertEqual((res.status, len(ex)), ("ok", 4))

    def test_max_rounds_forces_tool_choice_none_on_last(self):
        call = Scripted(T("", [("a", "ping", {"x": 1}, "")]), T("", [("b", "ping", {"x": 2}, "")]), T("итог"))
        res, _ = self.run_loop(call, max_rounds=3)
        self.assertEqual(call.reqs[-1].tool_choice, "none")
        self.assertEqual((res.status, res.text), ("ok", "итог"))

    def test_max_rounds_exceeded_if_model_insists(self):
        call = Scripted(T("", [("a", "ping", {"x": 1}, "")]), T("", [("b", "ping", {"x": 2}, "")]))
        res, ex = self.run_loop(call, max_rounds=2)
        self.assertEqual((res.status, len(ex)), ("max_rounds", 1))

    def test_unknown_tool_not_executed(self):
        call = Scripted(T("", [("a", "rm_rf", {}, "")]), T("ok"))
        res, ex = self.run_loop(call)
        self.assertEqual(ex, [])
        self.assertEqual(json.loads(call.reqs[1].messages[-1]["content"])["error"], "UNKNOWN_TOOL")

    def test_bad_args_reported_to_model(self):
        call = Scripted(T("", [("a", "ping", {}, '{"x": 1'), ], finish="length"), T("ok"))
        call.resps[0].tool_calls[0].error = "bad_json"
        res, ex = self.run_loop(call)
        self.assertEqual(ex, [])
        self.assertEqual(json.loads(call.reqs[1].messages[-1]["content"])["error"], "BAD_ARGS")
        self.assertEqual(res.status, "ok")

    def test_bad_args_repeatedly_truncated(self):
        def bad():
            r = T("", [("a", "ping", {}, '{"x')], finish="length")
            r.tool_calls[0].error = "bad_json"
            return r
        res, _ = self.run_loop(Scripted(bad(), bad(), bad()), truncated_retries=1)
        self.assertEqual(res.status, "truncated")

    def test_empty_response_nudge_then_ok(self):
        call = Scripted(T(""), T("ответ"))
        res, _ = self.run_loop(call)
        self.assertEqual((res.status, res.text), ("ok", "ответ"))
        self.assertIn("пуст", call.reqs[1].messages[-1]["content"])

    def test_empty_twice_is_empty_status(self):
        res, _ = self.run_loop(Scripted(T(""), T("")))
        self.assertEqual(res.status, "empty")

    def test_truncated_text_without_schema(self):
        res, _ = self.run_loop(Scripted(T("половина отв", finish="length")))
        self.assertEqual((res.status, res.text), ("truncated", "половина отв"))

    def test_tool_exception_text_not_leaked(self):
        def boom(n, a):
            raise RuntimeError("секрет /Users/x/key")
        call = Scripted(T("", [("a", "ping", {}, "")]), T("ok"))
        res, _ = self.run_loop(call, run_tool=boom)
        content = call.reqs[1].messages[-1]["content"]
        self.assertNotIn("секрет", content)
        self.assertIn("RuntimeError", content)

    def test_big_result_truncated(self):
        call = Scripted(T("", [("a", "ping", {}, "")]), T("ok"))
        self.run_loop(call, run_tool=lambda n, a: {"ok": True, "blob": "z" * 50000}, max_result_chars=500)
        self.assertLess(len(call.reqs[1].messages[-1]["content"]), 700)

    def test_final_json_validated_with_reprompt(self):
        call = Scripted(T("", [("a", "ping", {"x": 1}, "")]), T('{"decision":"zzz","why":"w"}'), T('{"decision":"hold","why":"w"}'))
        res, _ = self.run_loop(call, schema=SCH)
        self.assertEqual((res.status, res.obj["decision"]), ("ok", "hold"))
        self.assertIn("КОДЫ", call.reqs[2].messages[-1]["content"])
        self.assertEqual(len(call.fmt), 1)

    def test_final_json_format_exhausted(self):
        res, _ = self.run_loop(Scripted(T("a"), T("b"), T("c")), schema=SCH, format_retries=2)
        self.assertEqual(res.status, "format")
        self.assertIsNone(res.obj)

    def test_cancel(self):
        res, _ = self.run_loop(Scripted(T("x")), should_stop=lambda: True)
        self.assertEqual(res.status, "cancelled")

    def test_llm_error_propagates_and_usage_sums(self):
        with self.assertRaises(LLMError):
            self.run_loop(Scripted(T("", [("a", "ping", {}, "")]), LLMError("server")))
        res, _ = self.run_loop(Scripted(T("", [("a", "ping", {}, "")], out=3), T("ok", out=4)))
        self.assertEqual(res.usage.out, 7)


class TestEndToEndHttp(_llm.LLMBase):
    def test_loop_over_fake_server_records_rounds_and_reasoning(self):
        self.srv.push("/chat/completions",
                      R(200, ds_ok("", tool_calls=[("c1", "ping", {"x": 1})], reasoning="R1", hit=50, miss=50)),
                      R(200, ds_ok('{"decision":"hold","why":"ok"}', hit=100, miss=20)))
        c = self.client()
        res = toolloop.run(c, Req(system="S", messages=[user("q")], tools=TOOLS, response_format="json", meta={"job": "J1", "role": "pm"}),
                           lambda n, a: {"ok": True}, schema=SCH)
        self.assertEqual((res.status, res.obj["decision"]), ("ok", "hold"))
        second = self.srv.requests[1]["json"]["messages"]
        self.assertEqual(second[2]["reasoning_content"], "R1")          # DeepSeek thinking + tools: reasoning возвращается
        self.assertNotIn("response_format", self.srv.requests[0]["json"])    # json_object несовместим с tools -> не шлётся
        rows = usage.read_rows(self.root)
        self.assertEqual([(r["job"], r["round"]) for r in rows], [("J1", 1), ("J1", 2)])
        self.assertEqual([r["in_hit"] for r in rows], [50, 100])

    def test_ask_json_over_http_marks_format_error(self):
        self.srv.push("/chat/completions", R(200, ds_ok('{"decision":"nope","why":"w"}')), R(200, ds_ok('{"decision":"hold","why":"w"}')))
        c = self.client()
        r = schema.ask_json(c, Req(system="S", messages=[user("q")], response_format="json", meta={"job": "J2"}), SCH)
        self.assertTrue(r.ok)
        rows = usage.read_rows(self.root)
        self.assertEqual([x.get("kind") for x in rows], [None, "fmt", None])
        self.assertTrue(rows[1]["fmt_err"])
        self.assertEqual(usage.aggregate(rows, "provider")["ds"]["fmt_errors"], 1)

    def test_truncated_json_over_http(self):
        self.srv.push("/chat/completions", R(200, ds_ok('{"decision":"ho', finish="length")), R(200, ds_ok('{"decision":"hold","why":"w"}')))
        r = schema.ask_json(self.client(), Req(messages=[user("q")], response_format="json"), SCH)
        self.assertTrue(r.ok)
        self.assertIn("TRUNCATED", self.srv.requests[1]["json"]["messages"][-1]["content"])


class TestGatewayBridge(_gw.GwBase):
    def test_tools_and_runner(self):
        tools = toolloop.gateway_tools("ro", root=self.root)
        names = {t["name"] for t in tools}
        self.assertIn("get_status", names)
        self.assertNotIn("submit_plan", names)                     # агенту ro доступно только чтение
        for t in tools:
            self.assertEqual(t["schema"]["type"], "object")
            self.assertIs(t["schema"].get("additionalProperties"), False)
        self.assertEqual({t["name"] for t in toolloop.gateway_tools("ro", names=["get_status"], root=self.root)}, {"get_status"})
        out = toolloop.gateway_runner("ro", root=self.root)("get_status", {})
        self.assertIn("ok", out)
        denied = toolloop.gateway_runner("ro", root=self.root)("submit_plan", {})
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["errors"][0]["code"], "E_TOOL_DENIED")


if __name__ == "__main__":
    unittest.main()
