"""MCP-сервер шлюза (WP11-A): протокол JSON-RPC, права, отмена, подпроцесс по stdio. Временный TB_ROOT, биржи и сети нет."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest

import _bootstrap  # noqa: F401
import _gw
from tb import paths
from tb.gateway import registry
from tb.mcp import server

SECRET = "TOPSECRET-TOKEN-VALUE-123"


class Sink(object):
    def __init__(self):
        self.buf = io.BytesIO()
        self.lock = threading.Lock()

    def write(self, b):
        with self.lock:
            self.buf.write(b)

    def flush(self):
        pass

    def msgs(self):
        with self.lock:
            return [json.loads(x) for x in self.buf.getvalue().decode("utf-8").splitlines() if x.strip()]


class McpBase(_gw.GwBase):
    def srv(self, agent="chat"):
        self.sink = Sink()
        return server.Server(self.gw, agent, self.sink)

    def req(self, s, method, params=None, rid=1):
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        return s.handle(m)


class TestProtocol(McpBase):
    def test_initialize_versions(self):
        s = self.srv()
        for v in server.PROTOCOLS:
            self.assertEqual(self.req(s, "initialize", {"protocolVersion": v})["result"]["protocolVersion"], v)
        r = self.req(s, "initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], server.PROTOCOLS[0])
        self.assertIn("tools", r["capabilities"])
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_per_agent(self):
        names = lambda a: {t["name"] for t in self.req(self.srv(a), "tools/list")["result"]["tools"]}
        self.assertEqual(len(names("chat")), 19)
        self.assertNotIn("submit_plan", names("ro"))
        self.assertIn("get_brief", names("ro"))
        self.assertEqual(names("ghost"), set())
        for t in self.req(self.srv(), "tools/list")["result"]["tools"]:
            sch = t["inputSchema"]
            self.assertEqual(sch["type"], "object")
            self.assertIs(sch["additionalProperties"], False)
            self.assertTrue(t["description"].isascii())
            json.dumps(t)

    def test_call_ok_and_denied(self):
        s = self.srv("ro")
        r = self.req(s, "tools/call", {"name": "get_status", "arguments": {}})["result"]
        self.assertFalse(r["isError"])
        self.assertTrue(r["structuredContent"]["ok"])
        self.assertEqual(json.loads(r["content"][0]["text"])["actor"], "brain:ro")
        r = self.req(s, "tools/call", {"name": "submit_plan", "arguments": {"plan": _gw.plan()}})["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["errors"][0]["code"], "E_TOOL_DENIED")
        self.assertEqual(self.req(s, "tools/call", {"name": 5})["error"]["code"], server.E_PARAMS)
        self.assertEqual(self.req(s, "tools/call", {"name": "get_status", "arguments": [1]})["error"]["code"], server.E_PARAMS)

    def test_errors(self):
        s = self.srv()
        self.assertEqual(self.req(s, "nope")["error"]["code"], server.E_METHOD)
        self.assertEqual(s.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})["error"]["code"], server.E_REQUEST)
        s.dispatch([])
        self.assertEqual(self.sink.msgs()[-1]["error"]["code"], server.E_REQUEST)
        self.assertEqual(self.req(s, "ping")["result"], {})
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "whatever/notify"}))

    def test_resources(self):
        s = self.srv()
        uris = {r["uri"] for r in self.req(s, "resources/list")["result"]["resources"]}
        self.assertEqual(uris, {"tb://status", "tb://brief", "tb://events"})
        c = self.req(s, "resources/read", {"uri": "tb://status"})["result"]["contents"][0]
        self.assertTrue(json.loads(c["text"])["ok"])
        self.assertEqual(self.req(s, "resources/read", {"uri": "file:///etc/passwd"})["error"]["code"], server.E_NOT_FOUND)
        self.assertEqual(self.req(s, "resources/read", {"uri": "tb://../x"})["error"]["code"], server.E_NOT_FOUND)

    def test_serve_loop_parse_errors_batch_and_oversize(self):
        s = self.srv()
        data = b"garbage\n\n" + json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]).encode() + b"\n"
        data += b"[]\n" + b"x" * (server.MAX_LINE + 10) + b"\n" + b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
        s.serve(io.BytesIO(data))
        m = self.sink.msgs()
        self.assertEqual(m[0]["error"]["code"], server.E_PARSE)
        self.assertEqual(m[1], [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        self.assertEqual(m[2]["error"]["code"], server.E_REQUEST)
        self.assertEqual(m[3]["error"]["code"], server.E_REQUEST)
        self.assertEqual(m[4]["id"], 9)

    def test_tools_call_threaded_and_cancel(self):
        s = self.srv()
        r, w = os.pipe()
        rf = os.fdopen(r, "rb")
        wf = os.fdopen(w, "wb")
        t = threading.Thread(target=s.serve, args=(rf,), daemon=True)
        t.start()
        wf.write(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "wait_events", "arguments": {"timeout_s": 30}}}).encode() + b"\n")
        wf.write(b'{"jsonrpc":"2.0","id":8,"method":"ping"}\n')
        wf.flush()
        time.sleep(0.6)
        self.assertEqual([m["id"] for m in self.sink.msgs()], [8])                 # ping отвечает, пока wait_events ждёт
        wf.write(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}}\n')
        wf.flush()
        time.sleep(1.2)
        wf.close()
        t.join(5)
        self.assertEqual([m["id"] for m in self.sink.msgs()], [8])                 # отменённый запрос без ответа
        rf.close()

    def test_wait_events_returns_through_mcp(self):
        s = self.srv()
        t0 = time.time()
        r = self.req(s, "tools/call", {"name": "wait_events", "arguments": {"timeout_s": 1}})["result"]
        self.assertTrue(r["structuredContent"]["timed_out"])
        self.assertGreaterEqual(time.time() - t0, 0.9)


class TestSubprocess(_gw.GwBase):
    def run_server(self, agent, lines, env_extra=None, wait=0.0):
        env = dict(os.environ, PYTHONPATH=_bootstrap.SRC, TB_ROOT=self.root, TB_KEYS_DIR=os.path.join(self.root, "keys"))
        env.pop("TB_AGENT_TOKEN", None)
        env.update(env_extra or {})
        p = subprocess.Popen([sys.executable, "-m", "tb.mcp.server", "serve", "--agent", agent], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        out, err = p.communicate("".join(json.dumps(x) + "\n" for x in lines).encode("utf-8"), timeout=30)
        return p.returncode, [json.loads(x) for x in out.decode("utf-8").splitlines() if x.strip()], err.decode("utf-8", "replace")

    def test_stdio_roundtrip_local_agent(self):
        rc, msgs, err = self.run_server("chat", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_brief", "arguments": {"scope": "start"}}}])
        self.assertEqual(rc, 0, err)
        by = {m["id"]: m for m in msgs}
        self.assertEqual(sorted(by), [1, 2, 3, 4])
        self.assertEqual(len(by[2]["result"]["tools"]), 19)
        self.assertTrue(by[3]["result"]["structuredContent"]["ok"])
        self.assertTrue(by[4]["result"]["structuredContent"]["brief_id"].startswith("B-"))
        self.assertTrue(os.path.exists(paths.state("gateway_audit.jsonl", root=self.root)))

    def test_token_agent_needs_valid_token_from_env_or_file_not_argv(self):
        tok = registry.new_token()
        registry.set_token_hash("bot", registry.hash_token(tok), self.root)
        ping = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]
        rc, msgs, err = self.run_server("bot", ping)
        self.assertEqual(rc, 3)
        self.assertEqual(msgs, [])
        rc, msgs, err = self.run_server("bot", ping, {"TB_AGENT_TOKEN": "wrong-" + SECRET})
        self.assertEqual(rc, 3)
        self.assertNotIn(SECRET, err)
        rc, msgs, err = self.run_server("bot", ping, {"TB_AGENT_TOKEN": tok})
        self.assertEqual(rc, 0, err)
        self.assertNotIn(tok, err)
        registry.write_token_file("bot", tok)
        rc, msgs, err = self.run_server("bot", ping)
        self.assertEqual(rc, 0, err)
        rc, _, _ = self.run_server("ghost", ping)
        self.assertEqual(rc, 3)

    def test_bad_usage(self):
        env = dict(os.environ, PYTHONPATH=_bootstrap.SRC, TB_ROOT=self.root)
        for argv in ([], ["serve"], ["serve", "--agent"], ["serve", "--agent", "chat", "--token", "x"]):
            p = subprocess.run([sys.executable, "-m", "tb.mcp.server"] + argv, input=b"", capture_output=True, env=env, timeout=20)
            self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
