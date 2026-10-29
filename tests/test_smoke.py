"""`./tb smoke` без сети и без процессов: сценарные ответы фейкового LLM проходят настоящие схемы и смысловые проверки ролей; таблица проверок; защита от реального корня."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr

import _bootstrap  # noqa: F401
from tb import smoke, smoke_llm
from tb.brain import validate
from tb.llm import schema as schema_mod

BRIEF = {"market": {"btc": {"px": 81000.0}}, "levels": {}, "plans": [{"id": "PL-1028-SOL-01", "rev": 2, "sym": "SOL"}], "positions": []}


def tag(name, obj):
    return "<%s>\n%s\n</%s>" % (name, json.dumps(obj), name)


class Answers(unittest.TestCase):
    def check(self, schema_id, obj, ctx=None):
        errs = schema_mod.check_text(json.dumps(obj), validate.schema_for(schema_id))[1]
        self.assertEqual(errs, [])
        self.assertEqual(validate.semantic(schema_id, obj, ctx or {}), [])

    def test_dayplan(self):
        user = "\n".join([tag("brief", BRIEF), tag("owner_choice", {"session": "intraday", "minutes": 120}), tag("caps", {"max_risk_usdt": 0.9, "size_mult": 1.0}),
                          tag("symbols", ["BTC", "ETH", "SOL"])])
        a = smoke_llm.dayplan_answer(user)
        ctx = validate.dayplan_context(BRIEF, None, session_type="intraday", limits={"max_risk_usdt": 0.9, "size_mult": 1.0})
        ctx["symbols"] = sorted(set(ctx["symbols"]) | {"SOL"})
        self.check("dayplan/1", a, ctx)
        self.assertEqual(a["watch"][0]["sym"], "BTC")
        self.assertIn("SOL", a["limits"]["symbols"])

    def test_dayplan_without_prices_is_no_trade(self):
        a = smoke_llm.dayplan_answer(tag("brief", {}) + tag("owner_choice", {"session": "scalp", "minutes": 30}))
        self.assertEqual((a["limits"]["max_new_plans"], a["watch"]), (0, []))
        self.check("dayplan/1", a, {"symbols": None, "data_ok": False})

    def test_pm_audit_review(self):
        pm = smoke_llm.pm_answer(tag("brief", BRIEF) + tag("event", {"plan": "PL-1028-SOL-01", "seq_last": 9}))
        self.assertEqual((pm["rev"], pm["event_seq"], pm["decision"]), (2, 9, "hold"))
        self.check("pm/1", pm, {"plan": "PL-1028-SOL-01", "has_position": True})
        self.check("audit/1", smoke_llm.audit_answer(tag("audit_input", {"day_id": "D-1"})), {"day_id": "D-1", "items": [], "det": {}})
        self.check("casereview/1", smoke_llm.review_answer(""))

    def test_role_of_and_server(self):
        self.assertEqual(smoke_llm.role_of("x\n# prompt v1 day_auditor\n"), "day_auditor")
        srv = smoke_llm.Server()
        self.assertTrue(srv.url.startswith("http://127.0.0.1:"))
        srv.httpd.server_close()


class Tables(unittest.TestCase):
    def test_checks_table_and_exit(self):
        ck = smoke.Checks()
        ck.add("a", True, "x")
        self.assertTrue(ck.all_ok())
        ck.add("b", False, "y|z")
        self.assertFalse(ck.all_ok())
        self.assertIn("СБОЙ", ck.table())
        self.assertIn("y/z", ck.markdown())

    def test_refuses_real_root(self):
        err = io.StringIO()
        with redirect_stderr(err):
            from tb import paths
            self.assertEqual(smoke.main(["--root", paths.root()]), 2)

    def test_prepare_only_touches_given_root(self):
        _bootstrap.require_localhost()
        d = tempfile.mkdtemp(prefix="tbsmokeprep_")
        self.addCleanup(shutil.rmtree, d, True)
        old = dict((k, os.environ.get(k)) for k in ("TB_ROOT", "TB_KEYS_DIR"))
        os.environ["TB_ROOT"] = _bootstrap.ROOT
        try:
            tok = smoke.prepare(d, os.path.join(d, "_keys"), "http://127.0.0.1:1", 8999)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(tok)
        with open(os.path.join(d, "config", "providers.json")) as f:
            self.assertEqual(json.load(f)["providers"]["deepseek"]["base_url"], "http://127.0.0.1:1")
        with open(os.path.join(d, "config", "subscriptions.json")) as f:
            self.assertTrue([s for s in json.load(f)["subscribers"] if s["id"] == "api"][0]["enabled"])
        with open(os.path.join(_bootstrap.ROOT, "config", "subscriptions.json")) as f:
            self.assertFalse([s for s in json.load(f)["subscribers"] if s["id"] == "api"][0]["enabled"])      # реальный конфиг не тронут
        self.assertTrue(os.path.isfile(os.path.join(d, "_keys", "agents", "api.token")))


if __name__ == "__main__":
    unittest.main()
