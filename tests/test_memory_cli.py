"""WP15a: `./tb memory ...` на временном корне + тест слоёв пакета tb.memory."""
import ast
import contextlib
import io
import json
import os
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import TmpRoot, mk
from test_layering import imports_of, logic_violations, module_name, py_files, read, string_literals, under
from tb.memory import cases, cli, lessons
from test_memory_lessons import make_pool


class Cli(TmpRoot):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), root=self.root)
        return code, out.getvalue(), err.getvalue()

    def jfile(self, name, obj):
        p = os.path.join(self.root, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return p


class TestCli(Cli):
    def test_migrate_dry_run_and_real(self):
        self.copy_fixtures()
        code, out, _ = self.run_cli("migrate", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["would_write"], 13)
        self.assertFalse(os.path.exists(cases.cases_path(self.root)))
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(json.loads(out)["written"], 13)
        self.assertEqual(len(out.strip().splitlines()), 1)           # JSON одной строкой

    def test_verify_ok_and_broken(self):
        self.copy_fixtures()
        self.run_cli("migrate")
        code, out, _ = self.run_cli("verify")
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        with open(cases.cases_path(self.root), "r+", encoding="utf-8") as f:
            txt = f.read().replace('"regime":"unknown"', '"regime":"trend"', 1)
            f.seek(0)
            f.write(txt)
            f.truncate()
        code, out, _ = self.run_cli("verify")
        self.assertEqual((code, json.loads(out)["ok"]), (1, False))

    def test_similar_json_and_text(self):
        self.copy_fixtures()
        self.run_cli("migrate")
        code, out, _ = self.run_cli("similar", "--sym", "DOOD", "--feature", "confirmed=false", "--feature", "stop_pct=2.2")
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["warn"], "INSUFFICIENT")
        self.assertEqual({c["r"] for c in d["cases"][:2]}, {-1.07, -1.1})
        code, out, _ = self.run_cli("similar", "--sym", "DOOD", "--feature", "confirmed=false", "--text")
        self.assertIn("INSUFFICIENT", out)

    def test_similar_bad_feature(self):
        code, _, err = self.run_cli("similar", "--feature", "pnl_usdt=5")
        self.assertEqual(code, 2)
        self.assertIn("белого списка", err)
        self.assertEqual(self.run_cli("similar", "--feature", "regime=moon")[0], 2)
        self.assertEqual(self.run_cli("similar", "--feature", "range_5m_pct=abc")[0], 2)

    def test_stats_by(self):
        self.put([mk(i, r=-1.0 if i % 2 else 1.0, confirmed=bool(i % 2), err=["no-confirm"] if i % 2 else []) for i in range(8)])
        code, out, _ = self.run_cli("stats", "--by", "confirmed")
        d = json.loads(out)
        self.assertEqual(d["total"]["n_closed"], 8)
        self.assertEqual({r["value"] for r in d["by"]["confirmed"]}, {"true", "false"})
        self.assertTrue(all(r["small"] for r in d["by"]["confirmed"]))

    def test_add_and_show(self):
        rec = {"type": "trade", "situation": {"sym": "ETH", "side": "short", "setup": "sweep_reject", "confirmed": True}, "decision": {"action": "enter", "why": "перенос опыта"},
               "result": {"status": "closed", "r": 1.5, "exit_reason": "tp"}}
        code, out, _ = self.run_cli("add", "--file", self.jfile("c.json", rec))
        self.assertEqual(code, 0)
        cid = json.loads(out)["case_id"]
        code, out, _ = self.run_cli("show", cid)
        c = json.loads(out)
        self.assertEqual((c["src"]["actor"], c["src"]["role"]), ("owner", "manual"))
        self.assertIn("missing", c["situation"])
        self.assertEqual(self.run_cli("show", "C-20260101-NOPE-abc123")[0], 2)

    def test_add_invalid_refused(self):
        rec = {"type": "trade", "situation": {"sym": "ETH", "side": "short"}, "decision": {"action": "enter"}, "pnl_usdt": 3}
        code, _, err = self.run_cli("add", "--file", self.jfile("c.json", rec))
        self.assertEqual(code, 2)
        self.assertEqual(cases.load(self.root), [])

    def test_review_queue_and_review(self):
        self.put([mk(0, range_5m_pct=3.0, stop_pct=2.0)])
        code, out, _ = self.run_cli("review-queue")
        q = json.loads(out)
        self.assertEqual(q["n"], 1)
        cid = q["cases"][0]["case_id"]
        rev = {"schema": "tb.casereview/1", "verdict": "bad_process", "right": [], "improve": ["no_confirm"], "alternative": {"action": "wait_confirm", "detail": ""},
               "known_at_decision": ["no_confirm"], "lesson_hint": None, "conf": 0.5}
        self.assertEqual(self.run_cli("review", cid, "--file", self.jfile("r.json", rev))[0], 0)
        self.assertEqual(self.run_cli("review", cid, "--file", self.jfile("r.json", rev))[0], 2)          # повтор = отказ
        self.assertEqual(json.loads(self.run_cli("review-queue")[1])["n"], 0)

    def test_lesson_flow(self):
        self.put(make_pool())
        nc = {"cond": [{"feature": "confirmed", "op": "=", "value": False}], "claim": {"metric": "mean_r", "dir": "lower", "vs": "base", "effect": 1.0}}
        code, out, _ = self.run_cli("lesson", "add", "--file", self.jfile("l.json", nc))
        lid = json.loads(out)["lesson_id"]
        self.assertEqual(lid, "L-0001")
        self.assertIn(lid, self.run_cli("lesson", "list", "--text")[1])
        self.assertEqual(json.loads(self.run_cli("lesson", "show", lid)[1])["status"], "hyp")
        self.assertEqual(json.loads(self.run_cli("lesson", "confirm", lid)[1])["status"], "confirmed")
        self.assertEqual(json.loads(self.run_cli("lesson", "reject", lid)[1])["status"], "rejected")
        self.assertEqual(self.run_cli("lesson", "confirm", lid)[0], 2)
        self.assertEqual(self.run_cli("lesson", "show", "L-0099")[0], 2)
        bad = {"cond": [{"feature": "sym", "op": "=", "value": "X"}], "claim": nc["claim"]}
        self.assertEqual(self.run_cli("lesson", "add", "--file", self.jfile("b.json", bad))[0], 2)

    def test_lesson_propose_and_sync(self):
        self.put(make_pool())
        d = json.loads(self.run_cli("lesson", "propose")[1])
        self.assertTrue(d["candidates"])
        self.assertEqual(d["created"], [])
        d2 = json.loads(self.run_cli("lesson", "propose", "--write")[1])
        self.assertTrue(d2["created"])
        self.assertEqual(len(lessons.read_all(self.root)), len(d2["created"]))
        code, out, _ = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("lessons", json.loads(out))

    def test_no_command_and_unknown(self):
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("explode")[0], 2)


class TestLayers(unittest.TestCase):
    BANNED = ("tb.intel", "tb.venues", "tb.executor", "tb.gateway", "tb.launcher", "tb.mcp", "tb.marketdata", "tb.brain", "tb.dash", "socket", "urllib", "http", "ssl",
              "subprocess", "asyncio", "requests", "ctypes", "multiprocessing")

    def test_memory_imports_clean(self):
        bad = {}
        for p in py_files("memory"):
            mod = module_name(p)
            hit = [n for n in imports_of(read(p), mod) if under(n, self.BANNED)]
            if hit:
                bad[mod] = hit
        self.assertEqual(bad, {})

    def test_transitive_closure_has_no_exchange_layers(self):
        seen, stack, bad = set(), [module_name(p) for p in py_files("memory")], {}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        while stack:
            mod = stack.pop()
            if mod in seen:
                continue
            seen.add(mod)
            base = os.path.join(root, "src", *mod.split("."))
            path = os.path.join(base, "__init__.py") if os.path.isdir(base) else base + ".py"
            if not os.path.isfile(path):
                continue
            imps = imports_of(read(path), mod)
            hit = [n for n in imps if under(n, ("tb.venues", "tb.executor", "tb.intel", "tb.gateway"))]
            if hit:
                bad[mod] = hit
            stack.extend(n for n in imps if n.startswith("tb.") and n not in seen)
        self.assertEqual(bad, {})

    def test_similar_and_agg_are_pure(self):
        for name in ("similar", "agg"):
            self.assertEqual(logic_violations(read(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "tb", "memory", name + ".py"))), [], name)

    def test_no_news_items_or_user_config_literals(self):
        for p in py_files("memory"):
            for lit in string_literals(read(p)):
                self.assertNotIn("items.jsonl", lit, p)
                self.assertNotIn("traider-bot/", lit, p)

    def test_no_dynamic_exec_or_network_calls(self):
        for p in py_files("memory"):
            for n in ast.walk(ast.parse(read(p))):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, ("eval", "exec", "compile", "__import__"), p)

    def test_secret_name_absent(self):
        for p in py_files("memory"):
            self.assertNotIn("BYBIT_API_SECRET", read(p), p)


if __name__ == "__main__":
    unittest.main()
