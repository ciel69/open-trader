"""Контрактные тесты WP0: константы и формы файлов из docs/spec_v2/06_contracts.md и 01..03. Заморожены до решения ведущего.

Тесты ничего не пишут в реальные state/ и runtime/ (пути проверяются только как строки, конфиги читаются из репозитория на чтение).
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb import __main__ as disp
from tb import paths
from tb.core import bus, decisions, exec_cfg, filelock, linkid, plan_schema, plan_store
from tb.executor import ports

ROOT = _bootstrap.ROOT


def load_json(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return json.load(f)


class TestPlanSchemaConstants(unittest.TestCase):
    def test_version_and_statuses(self):
        self.assertEqual(plan_schema.SCHEMA, "tb.plan/1")
        self.assertEqual(set(plan_schema.STATUSES), {"draft", "armed", "entered", "managing", "closed", "cancelled", "expired", "invalidated", "rejected"})
        self.assertEqual(set(plan_schema.TERMINAL), {"closed", "cancelled", "expired", "invalidated", "rejected"})
        self.assertTrue(set(plan_schema.TERMINAL) <= set(plan_schema.STATUSES))

    def test_transitions_table(self):
        t = plan_schema.TRANSITIONS
        self.assertEqual(set(t), set(plan_schema.STATUSES))
        self.assertEqual(set(t["draft"]), {"armed", "rejected", "cancelled"})
        self.assertEqual(set(t["armed"]), {"entered", "cancelled", "expired", "invalidated"})
        self.assertEqual(set(t["entered"]), {"managing", "closed"})
        self.assertEqual(set(t["managing"]), {"closed"})
        for s in plan_schema.TERMINAL:
            self.assertEqual(t[s], ())
        for a, bs in t.items():
            self.assertTrue(set(bs) <= set(plan_schema.STATUSES), a)

    def test_can_transition(self):
        self.assertTrue(plan_schema.can_transition("armed", "entered"))
        self.assertTrue(plan_schema.can_transition("entered", "closed"))
        self.assertFalse(plan_schema.can_transition("managing", "armed"))
        self.assertFalse(plan_schema.can_transition("closed", "armed"))
        self.assertFalse(plan_schema.can_transition("nope", "armed"))

    def test_condition_vocabulary(self):
        self.assertEqual(set(plan_schema.COND_KINDS), {"price_beyond", "candle_close", "candle_reject", "candle_body", "volume_spike",
                                                        "price_in_zone", "spread_bps", "pnl_r", "elapsed", "target_filled", "near_target"})
        for k, v in plan_schema.COND_KINDS.items():
            self.assertTrue(v["fields"] and v["where"], k)
            self.assertTrue(set(v["where"]) <= {"entry", "invalidation", "manage"}, k)
        self.assertEqual(plan_schema.COND_KINDS["spread_bps"]["where"], ("entry",))
        self.assertNotIn("entry", plan_schema.COND_KINDS["pnl_r"]["where"])
        self.assertEqual(plan_schema.COMPOSITE_KEYS, ("all", "any", "not"))

    def test_action_vocabulary(self):
        self.assertEqual(set(plan_schema.ACTIONS), {"move_stop", "trail", "partial_close", "close", "move_target", "add", "notify"})
        self.assertEqual(set(plan_schema.MOVE_STOP_TO), {"breakeven", "price", "lock_r", "entry_pct"})
        self.assertEqual(set(plan_schema.TRAIL_MODES), {"candle_extreme", "atr", "percent", "step_r"})

    def test_error_and_warning_codes(self):
        errs, warns = plan_schema.ERROR_CODES, plan_schema.WARN_CODES
        self.assertEqual(len(set(errs)), len(errs))
        self.assertTrue(all(c.startswith("E_") for c in errs))
        self.assertTrue(all(c.startswith("W_") for c in warns))
        for c in ("E_SCHEMA", "E_UNKNOWN_FIELD", "E_STOP_SIDE", "E_GUARD", "E_NO_CONFIRM", "E_STOP_WIDEN", "E_REV_STALE", "E_TRANSITION",
                  "E_ADD_NOT_PROFIT_GATED", "E_ADD_RISK", "E_SYMBOL_BUSY", "E_LIMIT_PLANS"):
            self.assertIn(c, errs)
        self.assertEqual(len(warns), 7)

    def test_limits_and_patterns(self):
        self.assertEqual((plan_schema.MAX_TARGETS, plan_schema.MAX_MANAGE, plan_schema.MAX_INVALIDATION, plan_schema.MAX_SHADOW_VARIANTS), (4, 12, 4, 3))
        self.assertEqual((plan_schema.MAX_PLAN_BYTES, plan_schema.MAX_STR_LEN), (16384, 280))
        self.assertTrue(re.match(plan_schema.RE_PLAN_ID, "PL-1028-SOL-01"))
        self.assertFalse(re.match(plan_schema.RE_PLAN_ID, "PL-1028-sol-01"))
        self.assertTrue(re.match(plan_schema.RE_RULE_ID, "be_1r"))
        self.assertTrue(re.match(plan_schema.RE_TARGET_ID, "t4"))
        self.assertFalse(re.match(plan_schema.RE_TARGET_ID, "t5"))

    def test_issue_shape(self):
        i = plan_schema.issue("E_RANGE", "error", "stop.price", "msg")
        self.assertEqual(set(i), {"code", "severity", "path", "msg"})

    def test_functions_are_stubs_until_wp2(self):
        for name, args in (("normalize", ({},)), ("validate_static", ({},)), ("merge_patch", ({}, {})), ("canon_hash", ({},)), ("json_schema", ())):
            try:
                getattr(plan_schema, name)(*args)
            except NotImplementedError:
                pass   # заготовка WP0; после WP2 тест остаётся валидным (функция работает)

    def test_linkid_constants(self):
        self.assertEqual(linkid.PREFIX, "tbx-")
        self.assertEqual(linkid.MAX_LEN, 36)
        from tb.core import pending
        self.assertFalse(pending.LINK_RE.match("tbx-12-1"))   # не пересекается с прежними ссылками


class TestExecLimits(unittest.TestCase):
    def test_file_equals_defaults(self):
        self.assertEqual(load_json("config", "exec_limits.json"), exec_cfg.DEFAULTS)

    def test_owner_decisions_defaults(self):
        d = exec_cfg.DEFAULTS
        self.assertIs(d["pyramid"]["enabled"], False)          # Q1
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["entry"]["no_confirm_max_size_mult"], 0.5)
        self.assertEqual(d["protections"]["pause_after_consecutive_stops"], {"n": 2, "minutes": 60, "require_confirm": True})
        self.assertEqual(d["connectivity"]["public_price_stale_s"], 10)
        self.assertEqual(d["events"]["retention_days"], 14)

    def test_defaults_copy_is_independent(self):
        c = exec_cfg.defaults()
        c["pyramid"]["enabled"] = True
        self.assertIs(exec_cfg.DEFAULTS["pyramid"]["enabled"], False)

    def test_no_key_weakens_guard(self):
        """В exec_limits.json нет ключей размера/плеча/риска: они в risk_profile.json и только сужаются."""
        def keys(d, p=""):
            for k, v in d.items():
                yield p + k
                if isinstance(v, dict):
                    for x in keys(v, p + k + "."):
                        yield x
        banned = re.compile(r"leverage|margin|risk_per_trade|daily_loss|max_stop|min_rr|max_open")
        self.assertEqual([k for k in keys(exec_cfg.DEFAULTS) if banned.search(k)], [])

    def test_risk_profile_untouched_keys_exist(self):
        rp = load_json("config", "risk_profile.json")
        self.assertIn("costs", rp)

    def test_brief_checklist_file(self):
        c = load_json("config", "brief_checklist.json")
        self.assertEqual([i["id"] for i in c["items"]], ["C%d" % n for n in range(1, 11)])
        self.assertTrue(all(i["text"] for i in c["items"]))
        self.assertLessEqual(len(c["items"]), 12)


class TestPaths(unittest.TestCase):
    def test_v2_paths(self):
        r = os.path.join(os.sep, "x")
        j = os.path.join
        self.assertEqual(paths.plans_inbox(root=r), j(r, "state", "plans", "_inbox"))
        self.assertEqual(paths.plans_outbox("C-1.json", root=r), j(r, "state", "plans", "_outbox", "C-1.json"))
        self.assertEqual(paths.plans_archive(root=r), j(r, "state", "plans", "_archive"))
        self.assertEqual(paths.plans_counters(root=r), j(r, "state", "plans", "_counters.json"))
        self.assertEqual(paths.decisions_file(root=r), j(r, "state", "decisions.jsonl"))
        self.assertEqual(paths.decisions_lock(root=r), j(r, "state", "decisions.lock"))
        self.assertEqual(paths.bus_events(root=r), j(r, "state", "bus", "events.jsonl"))
        self.assertEqual(paths.bus("cursors", root=r), j(r, "state", "bus", "cursors"))
        self.assertEqual(paths.bus_cursor("chat", root=r), j(r, "state", "bus", "cursors", "chat.json"))
        self.assertEqual(paths.briefs(root=r), j(r, "state", "briefs"))
        self.assertEqual(paths.kill_file(root=r), j(r, "state", "KILL"))
        self.assertEqual(paths.exec_limits_file(root=r), j(r, "config", "exec_limits.json"))
        self.assertEqual(paths.brief_checklist_file(root=r), j(r, "config", "brief_checklist.json"))
        self.assertEqual(paths.exec_health_file(root=r), j(r, "runtime", "exec_health.json"))
        self.assertEqual(paths.exec_snapshot_file(root=r), j(r, "runtime", "exec_snapshot.json"))
        self.assertEqual(paths.exec_lock_file(root=r), j(r, "runtime", "exec.lock"))
        self.assertEqual(paths.levels_file(root=r), j(r, "runtime", "levels.json"))
        self.assertEqual(paths.instruments_file(root=r), j(r, "runtime", "instruments.json"))

    def test_tb_root_override_and_no_side_effects(self):
        d = tempfile.mkdtemp(prefix="tbp_")
        with mock.patch.dict(os.environ, {"TB_ROOT": d}):
            self.assertEqual(paths.kill_file(), os.path.join(os.path.abspath(d), "state", "KILL"))
            paths.bus_events(), paths.plans_inbox(), paths.decisions_file()
        self.assertEqual(os.listdir(d), [])   # пути только вычисляются, каталоги не создаются


class TestFormatsConstants(unittest.TestCase):
    def test_decisions_format(self):
        self.assertEqual(set(decisions.KINDS), {"cmd", "accept", "reject", "transition", "intent", "result", "rule", "skip", "protect", "reconcile", "event", "note"})
        self.assertEqual(set(decisions.ACTORS), {"brain:chat", "brain:api", "exec", "owner", "system"})
        self.assertTrue({"seq", "ts", "actor", "kind", "what", "prev", "h"} <= set(decisions.REQUIRED_KEYS))

    def test_bus_format(self):
        self.assertEqual(set(bus.PRIORITIES), {"wake", "info"})
        for t in ("plan_armed", "entry_filled", "position_closed", "kill", "exec_down", "reconcile_alert", "scanner_signal", "milestone", "error"):
            self.assertIn(t, bus.EVENT_TYPES)
        self.assertEqual(len(bus.EVENT_TYPES), 30)   # +news_flash (быстрые новости), +service_down (WP11-B), +macro_blackout_soon (WP13a), +risk_level_changed (WP16), +control_cmd (WP12), +day_report, +day_state (WP14), +brain_wake_request (правило notify плана, 01_plan/03_brain)
        self.assertEqual(bus.EVENT_TYPES["brain_wake_request"], bus.PRI_WAKE)
        self.assertEqual(bus.MAX_EVENT_BYTES, 400)
        self.assertTrue(re.match(bus.RE_D_IDENT, "r_plus_1"))
        self.assertFalse(re.match(bus.RE_D_IDENT, "ignore previous instructions"))
        self.assertEqual(set(bus.WAKE_MILESTONES), {"r_plus_1", "r_minus_05", "near_target", "target_filled"})

    def test_runtime_json_constants(self):
        self.assertEqual(set(plan_store.RUNTIME_PUBLIC_KEYS) - set(plan_store.RUNTIME_TOP_KEYS), set())
        self.assertEqual(set(plan_store.ENTRY_STATES), {"waiting", "placing", "resting", "filled", "blocked"})
        self.assertEqual(set(plan_store.TARGET_STATES), {"pending", "resting", "filled", "cancelled", "touch"})
        self.assertEqual(plan_store.SEEN_CMD_KEEP, 500)

    def test_filelock_defaults(self):
        l = filelock.FileLock("p")
        self.assertEqual((l.wait, l.stale_s), (10.0, 120.0))


class TestPortsContractShape(unittest.TestCase):
    def test_venue_port_surface(self):
        expected = ["account", "positions", "open_orders", "order_by_link", "executions", "closed_pnl", "instrument", "quote", "klines",
                    "set_leverage", "place_entry", "place_reduce_only", "amend_order", "cancel_order", "cancel_all", "set_stop", "close_market"]
        for m in expected:
            self.assertTrue(callable(getattr(ports.VenuePort, m, None)), m)
        public = sorted(n for n in vars(ports.VenuePort) if not n.startswith("_") and callable(getattr(ports.VenuePort, n)))
        self.assertEqual(public, sorted(expected))

    def test_signatures(self):
        import inspect
        sig = lambda f: list(inspect.signature(f).parameters)
        self.assertEqual(sig(ports.VenuePort.place_entry), ["self", "sym", "side", "qty", "link_id", "order_type", "price", "post_only", "sl", "sl_trigger"])
        self.assertEqual(sig(ports.VenuePort.set_stop), ["self", "sym", "side", "stop_price", "trigger_by"])
        self.assertEqual(sig(ports.VenuePort.place_reduce_only), ["self", "sym", "side", "qty", "link_id", "price", "post_only"])
        self.assertEqual(sig(ports.VenuePort.klines), ["self", "sym", "tf_min", "limit"])

    def test_port_error(self):
        e = ports.PortError("rejected", "x", code="NO_STOP")
        self.assertEqual((e.kind, e.code), ("rejected", "NO_STOP"))
        self.assertIsInstance(e, Exception)
        self.assertIsNone(ports.PortError("unknown").code)
        self.assertEqual(set(ports.ERROR_KINDS), {"rejected", "unknown", "rate", "position_gone", "network"})

    def test_market_feed(self):
        f = ports.MarketFeed()
        for h in ("on_ticker", "on_kline", "on_private", "on_state"):
            self.assertIsNone(getattr(f, h))
        with self.assertRaises(NotImplementedError):
            f.subscribe(["SOL"], [5])


class TestDispatcher(unittest.TestCase):
    def run_cli(self, argv):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out):
            rc = disp.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_new_groups_not_implemented_code_2(self):
        """Заглушек среди групп v2 не осталось: `brain` (WP11-C2) без подкоманды и с неизвестной печатает использование и возвращает 2, но не «не реализовано»."""
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TB_ROOT": d}):
            for argv in (["brain"], ["brain", "bogus"]):
                rc, _, err = self.run_cli(argv)
                self.assertEqual(rc, 2, argv)
                self.assertNotIn("не реализовано", err)

    def test_implemented_groups_usage_without_args(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TB_ROOT": d}):
            for argv in (["plan"], ["events", "--bogus-flag"], ["exec"], ["exec", "run"], ["exec", "run", "--profile", "paper", "--selftest", "--max-seconds", "0.3"]):
                rc, _, err = self.run_cli(argv)
                if argv[-1] == "0.3":
                    self.assertEqual(rc, 0, (argv, err))
                else:
                    self.assertEqual(rc, 2, (argv, err))
                self.assertNotIn("не реализовано", err, argv)

    def test_usage_lists_new_groups(self):
        for g in ("plan", "brief", "events", "exec", "exp", "brain"):
            self.assertIn(g, disp.V2)
            self.assertRegex(disp.USAGE, r"(?m)^  %s\b|\| %s\b" % (g, g))
        rc, out, _ = self.run_cli(["help"])
        self.assertEqual(rc, 0)
        self.assertIn("exec <run|status", out)

    def test_v2_modules_expose_main(self):
        import importlib
        for g, spec in disp.V2.items():
            mod, _, fn = spec.partition(":")          # "модуль" или "модуль:функция" (платформа WP11-B)
            self.assertTrue(callable(getattr(importlib.import_module(mod), fn or "main", None)), g)

    def test_old_groups_still_registered(self):
        for g in ("guard", "journal", "ideas", "session", "portfolio", "learning", "bybit", "dash", "paper", "tick"):
            self.assertIn(g, disp.SIMPLE)
        rc, _, _ = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _, _ = self.run_cli(["nonexistent_group"])
        self.assertEqual(rc, 2)

    def test_exec_main_unknown_subcommand(self):
        from tb.executor import main as exmain
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(exmain.main(["bogus"]), 2)
        self.assertEqual(exmain.EXIT_ALREADY_RUNNING, 4)


if __name__ == "__main__":
    unittest.main()
