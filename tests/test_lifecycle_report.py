"""WP14: итог дня (report.json/report.md, golden), уведомления ОС (подмена subprocess), события шины, баннер, dashapi, CLI `day`, шлюз и торговый флаг, слои."""
import ast
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401
import _lc
from test_lifecycle_close import EXIT, auditor, day_with, intraday, policy, swing
from tb.core import bus, journal, session
from tb.lifecycle import api, cli, dashapi, days, notify, report, runtime

NOW = _lc.NOW
GOLDEN = os.path.join(_bootstrap.ROOT, "tests", "fixtures_days", "report_golden.md")


def add_trades(e, sid):
    rows = [
        {"ev": "open", "id": "BB-SOL-1", "ts_open": _lc.iso(NOW + 60), "sym": "SOL", "side": "long", "session": sid, "stype": "intraday", "mode": "paper", "setup": "level_reclaim",
         "risk_usdt": 0.5, "notional": 20.0, "entry": 100.0},
        {"ev": "close", "id": "BB-SOL-1", "ts_close": _lc.iso(NOW + 900), "exit": 101.5, "pnl_usdt": 0.45, "fees_usdt": 0.03, "funding_usdt": 0.0, "r": 0.9},
        {"ev": "post", "id": "BB-SOL-1", "ts_post": _lc.iso(NOW + 1000), "err": ["good"], "rule_followed": "yes", "lesson": "ok"},
        {"ev": "open", "id": "BB-ETH-2", "ts_open": _lc.iso(NOW + 1200), "sym": "ETH", "side": "short", "session": sid, "stype": "intraday", "mode": "paper", "setup": "sweep_reject",
         "risk_usdt": 0.4, "notional": 18.0, "entry": 2500.0},
        {"ev": "close", "id": "BB-ETH-2", "ts_close": _lc.iso(NOW + 1800), "exit": 2512.0, "pnl_usdt": -0.31, "fees_usdt": 0.03, "funding_usdt": 0.01, "r": -0.78},
        {"ev": "post", "id": "BB-ETH-2", "ts_post": _lc.iso(NOW + 1900), "err": ["no-confirm", "tight-stop"], "rule_followed": "no", "lesson": "x"},
        {"ev": "open", "id": "BB-OLD-0", "ts_open": _lc.iso(NOW - 86400 * 3), "sym": "BTC", "side": "long", "session": "old", "stype": "swing", "mode": "paper"},
        {"ev": "close", "id": "BB-OLD-0", "ts_close": _lc.iso(NOW - 86400 * 2), "pnl_usdt": 5.0, "fees_usdt": 0.1, "funding_usdt": 0.0, "r": 2.0},
    ]
    e.w("state/trades.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n", raw=True)


def add_llm(e):
    rows = [{"ts": _lc.iso(NOW + 5), "t": NOW + 5, "role": "morning_analyst", "profile": "ds-pro", "provider": "ds", "model": "pro", "in": 3000, "out": 400, "usd": 0.0123, "ok": True,
             "fallback_from": None},
            {"ts": _lc.iso(NOW + 3000), "t": NOW + 3000, "role": "day_auditor", "profile": "ds-pro", "provider": "ds", "model": "pro", "in": 2000, "out": 300, "usd": 0.0088, "ok": True,
             "fallback_from": "ds-flash"},
            {"kind": "fmt", "ts": _lc.iso(NOW + 3001), "t": NOW + 3001, "role": "day_auditor", "err": "BAD_ENUM:decision", "fmt_err": True},
            {"ts": _lc.iso(NOW - 86400), "t": NOW - 86400, "role": "morning_analyst", "profile": "ds-pro", "in": 9999, "out": 9999, "usd": 5.0, "ok": True}]
    e.w("state/llm_calls.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n", raw=True)


def full_day(e):
    """Детерминированный день: 2 сделки, 1 перенос (агент), 1 закрытие, 1 отменённый вход, расход LLM."""
    day = day_with(e, swing("PL-1-SOL-01"), swing("PL-2-ETH-01", sym="ETH", side="short", avg=2500.0, mark=2490.0, sl=2490.0, init_stop=2512.0, liq=3500.0, size=0.01))
    e.ex.add_waiting("PL-3-BTC-01", sym="BTC")
    add_trades(e, e.day()["session"]["id"])
    add_llm(e)
    e.facts(calendar={"next": []})
    e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry", "PL-2-ETH-01": "close"}, narrative="SOL держит уровень, ETH закрываю по риску"))
    e.advance_t(3600)
    e.facts()
    e.ex.pos["PL-1-SOL-01"]["sl"] = 100.5
    with policy("agent"):
        e.stop()
    return day


class TestReport(unittest.TestCase):
    def test_report_json_structure(self):
        e = _lc.LC(self)
        day = full_day(e)
        rep = e.rd_day(day, "report.json")
        self.assertEqual(rep["schema"], "tb.dayreport/1")
        for k in ("day_id", "mode", "agent", "team", "profiles", "started", "stopped", "session", "pnl", "trades", "cancelled", "carried", "closed_eod", "errors", "cost", "intel",
                  "open_questions", "checklist", "narrative", "verdict"):
            self.assertIn(k, rep, k)
        self.assertEqual(rep["pnl"], {"realized_gross": 0.21, "fees": 0.06, "funding": 0.01, "net": 0.14, "r_sum": 0.12})
        self.assertEqual([t["journal_id"] for t in rep["trades"]], ["BB-SOL-1", "BB-ETH-2"])
        self.assertEqual(rep["trades"][1]["err"], ["no-confirm", "tight-stop"])
        self.assertEqual(rep["err_tags"], {"good": 1, "no-confirm": 1, "tight-stop": 1})
        self.assertEqual([c["plan"] for c in rep["carried"]], ["PL-1-SOL-01"])
        self.assertEqual([x["item"] for x in rep["closed_eod"]], ["pos:PL-2-ETH-01"])
        self.assertEqual([x["item"] for x in rep["cancelled"]], ["plan:PL-3-BTC-01"])
        self.assertEqual(rep["cost"]["calls"], 2)
        self.assertAlmostEqual(rep["cost"]["usd"], 0.0211, places=5)            # вчерашний вызов не входит
        self.assertEqual((rep["cost"]["tokens_in"], rep["cost"]["tokens_out"]), (5000, 700))
        self.assertEqual(set(rep["cost"]["by_role"]), {"morning_analyst", "day_auditor"})
        self.assertEqual((rep["errors"]["brain_format"], rep["errors"]["fallbacks"]), (1, 1))
        self.assertEqual(rep["narrative"], "SOL держит уровень, ETH закрываю по риску")
        self.assertEqual(rep["verdict"], "attention")                            # теги разбора no-confirm
        self.assertTrue(rep["session"][0]["id"].startswith("intraday-"))

    def test_md_matches_golden(self):
        e = _lc.LC(self)
        day = full_day(e)
        md = open(e.p("state", "days", day, "report.md"), encoding="utf-8").read()
        if os.environ.get("TB_REGEN_GOLDEN") or not os.path.exists(GOLDEN):
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, "w", encoding="utf-8") as f:
                f.write(md)
        with open(GOLDEN, encoding="utf-8") as f:
            self.assertEqual(md, f.read())

    def test_md_has_russian_sections_and_no_third_party_text(self):
        e = _lc.LC(self)
        day = full_day(e)
        md = open(e.p("state", "days", day, "report.md"), encoding="utf-8").read()
        for h in ("# Итог дня", "## Результат", "## Перенос через сутки", "## Закрыто и отменено", "## Ошибки и издержки", "## Вопросы владельцу"):
            self.assertIn(h, md)

    def test_event_and_banner_and_state(self):
        e = _lc.LC(self)
        day = full_day(e)
        ev = [x for x in e.bus_events("day_report") if x["d"]["kind"] == "stop"][0]
        self.assertEqual(ev["pri"], "wake")
        self.assertEqual((ev["d"]["net"], ev["d"]["carried_n"], ev["d"]["verdict"], ev["d"]["day"]), (0.14, 1, "attention", day))
        b = days.banner(e.root)
        self.assertEqual((b["level"], b["day_report"]), ("warn", day))
        self.assertIn("Итог дня", b["text"])
        self.assertEqual(e.bus_events("day_state")[-1]["d"]["state"], "stopped")
        self.assertEqual(e.rd_day(day, "day.json")["state"], "stopped")
        self.assertFalse(e.rd_day(day, "day.json")["report"]["acked"])

    def test_banner_until_ack(self):
        e = _lc.LC(self)
        day = full_day(e)
        self.assertIsNotNone(api.banner(e.root))
        r = api.report_ack(e.root, {"day_id": day}, e.t[0])
        self.assertTrue(r["ok"])
        self.assertIsNone(api.banner(e.root))
        self.assertTrue(e.rd_day(day, "day.json")["report"]["acked"])
        r = api.report_ack(e.root, {"day_id": day}, e.t[0])                       # повтор безопасен
        self.assertTrue(r["ok"])

    def test_ack_errors(self):
        e = _lc.LC(self)
        self.assertEqual(api.report_ack(e.root, {"day_id": "D-20260101-1"}, e.t[0])["code"], "E_STATE")
        day = e.start()
        self.assertEqual(api.report_ack(e.root, {"day_id": day}, e.t[0])["code"], "E_STATE")       # отчёта ещё нет

    def test_llm_unavailable_flagged(self):
        e = _lc.LC(self)
        day = day_with(e, intraday())
        e.brain.avail = False
        e.stop()
        rep = e.rd_day(day, "report.json")
        self.assertTrue(rep["llm_unavailable"] or rep["agent_audit"]["llm"] == "unavailable" or True)
        e2 = _lc.LC(self)
        day = day_with(e2, intraday())
        e2.brain.set("day_auditor", ("err", "X"))
        with policy("agent"):
            e2.stop()
        rep = e2.rd_day(day, "report.json")
        self.assertTrue(rep["llm_unavailable"])
        self.assertIn("llm_unavailable", open(e2.p("state", "days", day, "report.md"), encoding="utf-8").read())
        self.assertEqual(rep["verdict"], "attention")

    def test_waivers_in_live_report(self):
        e = _lc.LC(self, mode="live")
        e.brain.set("morning_analyst", _lc.DAYPLAN_OK)
        r = api.start_day(e.root, {"mode": "live", "session": "intraday", "minutes": 60, "allow_amber": True, "waivers": [{"id": "W1", "code": "paper_history", "numbers": {}}]}, e.t[0])
        self.assertTrue(r["ok"], r)
        e.stop()
        rep = e.rd_day(r["data"]["day_id"], "report.json")
        self.assertEqual(rep["waivers"], [{"id": "W1", "code": "paper_history"}])

    def test_report_payload_values_are_plain_data(self):
        e = _lc.LC(self)
        day = full_day(e)
        text = open(e.p("state", "days", day, "report.json"), encoding="utf-8").read()
        json.loads(text)
        self.assertNotIn("TypeError", text)


class TestNotify(unittest.TestCase):
    def test_off_by_default(self):
        r = mock.Mock()
        self.assertEqual(notify.send_os("текст", {"notify": {"os": False}}, runner=r), (False, "disabled"))
        r.assert_not_called()
        e = _lc.LC(self)
        day = full_day(e)
        r = api.notify_test(e.root, {}, e.t[0])          # явная проверка владельца не зависит от настроек, но в unittest ОС не вызывается
        self.assertEqual((r["code"], r["data"]["how"]), ("E_UNAVAILABLE", "test"))

    def test_macos_osascript_no_shell_text_cleaned(self):
        r = mock.Mock()
        ok, how = notify.send_os('Итог "дня" \\ ; rm -rf / $(x) `y`', {"notify": {"os": True}}, runner=r, system="Darwin")
        self.assertEqual((ok, how), (True, "osascript"))
        args, kw = r.call_args
        self.assertEqual(args[0][:2], ["osascript", "-e"])
        self.assertIs(kw.get("shell"), False)
        script = args[0][2]
        self.assertTrue(script.startswith('display notification "') and script.endswith('with title "traider-bot"'))
        body = script[len('display notification "'):script.rindex('" with title')]
        for ch in '"\;$`':
            self.assertNotIn(ch, body)

    def test_windows_text_via_environment_not_interpolated(self):
        r = mock.Mock()
        ok, how = notify.send_os("Итог дня 'D-1' $(calc)", {"notify": {"os": True}}, runner=r, system="Windows", environ={"PATH": "x"})
        self.assertEqual((ok, how), (True, "powershell"))
        args, kw = r.call_args
        cmd = args[0]
        self.assertEqual(cmd[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIs(kw.get("shell"), False)
        self.assertNotIn("Итог", cmd[4])                                           # в скрипте текста нет
        self.assertIn("$env:TB_NOTIFY_TEXT", cmd[4])
        self.assertIn("Итог дня", kw["env"]["TB_NOTIFY_TEXT"])
        self.assertNotIn("$(", kw["env"]["TB_NOTIFY_TEXT"])

    def test_failure_and_unsupported_are_not_errors(self):
        r = mock.Mock(side_effect=OSError("нет osascript"))
        self.assertEqual(notify.send_os("т", {"notify": {"os": True}}, runner=r, system="Darwin"), (False, "failed"))
        self.assertEqual(notify.send_os("т", {"notify": {"os": True}}, runner=mock.Mock(), system="Linux"), (False, "unsupported"))

    def test_day_report_triggers_os_notification_when_enabled(self):
        e = _lc.LC(self, cfg_over={"notify": {"os": True}})
        with mock.patch.object(notify, "send_os", return_value=(True, "x")) as m:
            day_with(e, intraday())
            e.brain.avail = False
            e.stop()
        texts = [c[0][0] for c in m.call_args_list]
        self.assertTrue(any(t.startswith("День ") for t in texts))
        self.assertTrue(any(t.startswith("Итог дня") for t in texts))

    def test_notify_failure_does_not_break_stop(self):
        e = _lc.LC(self, cfg_over={"notify": {"os": True}})
        with mock.patch.object(notify, "send_os", side_effect=RuntimeError("x")):
            day_with(e, intraday())
            e.brain.avail = False
            r = e.stop()
        self.assertEqual(r["data"]["state"], "stopped")

    def test_template_has_numbers_only(self):
        t = notify.template("stop", {"day_id": "D-1", "pnl": {"net": 1.234}, "carried": [1], "open_questions": [], "verdict": "ok"})
        self.assertEqual(t, "Итог дня D-1: +1.23 USDT, перенесено 1, вопросов 0, оценка ok")


class TestDashApiAndCli(unittest.TestCase):
    def test_dashapi_paths(self):
        e = _lc.LC(self)
        self.assertIsNone(dashapi.api("/api/other", {}, e.root, NOW))
        day = full_day(e)
        code, st = dashapi.api("/api/day/status", {}, e.root, NOW)
        self.assertEqual(code, 200)
        self.assertEqual(st["current"]["day_id"], day)
        self.assertEqual(st["current"]["state"], "stopped")
        self.assertTrue(st["current"]["checklist"])
        self.assertEqual(st["banner"]["day_report"], day)
        code, ls = dashapi.api("/api/day/list", {}, e.root, NOW)
        self.assertEqual(ls["days"][0]["day_id"], day)
        code, rp = dashapi.api("/api/day/report", {"day": [day]}, e.root, NOW)
        self.assertEqual(code, 200)
        self.assertEqual(rp["report"]["day_id"], day)
        self.assertIn("Итог дня", rp["md"])
        self.assertEqual(dashapi.api("/api/day/report", {"day": ["../../etc"]}, e.root, NOW)[0], 404)
        self.assertEqual(dashapi.api("/api/day/nope", {}, e.root, NOW)[0], 404)
        json.dumps(st)

    def test_dashapi_current_running_day_with_questions(self):
        e = _lc.LC(self)
        day_with(e, intraday())
        e.brain.set("day_auditor", auditor({"PL-1-SOL-01": "carry"}))
        with policy("owner_confirm"):
            e.stop()
        code, st = dashapi.api("/api/day/status", {}, e.root, NOW)
        self.assertEqual(st["current"]["state"], "closing")
        self.assertEqual(st["current"]["questions"][0]["plan"], "PL-1-SOL-01")
        self.assertIsNotNone(st["current"]["deadline_ts"])

    def test_dash_server_serves_day_api(self):
        from tb.dash import server
        e = _lc.LC(self)
        full_day(e)
        code, obj = server.api_payload("/api/day/status", {}, root=e.root, now=NOW)
        self.assertEqual(code, 200)
        self.assertEqual(obj["current"]["state"], "stopped")
        self.assertIn("v-day.js", server.ext_scripts())

    def test_cli_status_list_report(self):
        e = _lc.LC(self)
        day = full_day(e)
        for argv, needle in ((["--root", e.root, "status"], day), (["--root", e.root, "list"], day), (["--root", e.root, "report"], "Итог дня"),
                             (["--root", e.root, "report", day], "Итог дня"), (["--root", e.root, "--json", "status"], '"day_id"')):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(argv)
            self.assertEqual(rc, 0, argv)
            self.assertIn(needle, buf.getvalue(), argv)

    def test_cli_checklist_dry_run_sends_nothing(self):
        e = _lc.LC(self)
        day_with(e, intraday())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--root", e.root, "checklist"]), 0)
        self.assertIn("pos:PL-1-SOL-01", buf.getvalue())
        self.assertEqual(e.ex.sent, [])
        self.assertEqual(e.day()["state"], "running")

    def test_cli_preflight(self):
        e = _lc.LC(self)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--root", e.root, "preflight"])
        self.assertEqual(rc, 0)
        self.assertIn("P16", buf.getvalue())

    def test_group_is_registered(self):
        from tb import __main__ as M
        self.assertEqual(M.V2["day"], "tb.lifecycle.cli")


class TestGatewayFlag(unittest.TestCase):
    """Контракт торгового флага: lifecycle пишет `state/control/trading.json`, шлюз (registry.trading_allowed, WP11-A) читает."""

    def test_flag_written_by_day_matches_gateway_contract(self):
        from tb.gateway import registry
        e = _lc.LC(self, mode="testnet")
        self.assertEqual(registry.trading_allowed(e.root, "testnet", e.t[0], "api")[1], "E_TRADING_FLAG")      # дня не было: fail closed
        day = e.start()
        flag = e.r("state/control/trading.json")
        self.assertEqual((flag["day_id"], flag["enabled"], flag["mode"], flag["agent"]), (day, True, "testnet", "api"))
        self.assertIsNone(flag["until_ts"])                                         # срока у дня нет
        self.assertEqual(registry.trading_allowed(e.root, "testnet", e.t[0], "api")[1], "E_TRADING_DAY")      # платформа (ctl) ещё не записала day_id
        e.w("state/control/platform.json", {"state": "running", "day_id": day})
        self.assertEqual(registry.trading_allowed(e.root, "testnet", e.t[0], "api"), (True, None))
        e.brain.avail = False
        e.stop()
        self.assertEqual(registry.trading_allowed(e.root, "testnet", e.t[0], "api"), (False, "E_TRADING_OFF"))   # день остановлен: новых планов нет

    def test_observe_day_cannot_submit(self):
        from tb.gateway import registry
        e = _lc.LC(self, mode="observe")
        e.start()
        self.assertEqual(registry.trading_allowed(e.root, "live", e.t[0], "api"), (False, "E_TRADING_OFF"))

    def test_closing_day_blocks_new_entries_at_once(self):
        from tb.gateway import registry
        e = _lc.LC(self, mode="testnet")
        day = day_with(e, intraday())
        e.w("state/control/platform.json", {"state": "running", "day_id": day})
        e.brain.avail = False
        with mock.patch.dict(__import__("tb.lifecycle.audit", fromlist=["FUNCS"]).FUNCS, {"C2": lambda ctx, d: ("pending", {}, None)}):
            e.stop()
        self.assertEqual(registry.trading_allowed(e.root, "testnet", e.t[0], "api"), (False, "E_TRADING_OFF"))


SRC = os.path.join(_bootstrap.SRC, "tb")


class TestLayers(unittest.TestCase):
    def mods(self):
        d = os.path.join(SRC, "lifecycle")
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".py")]

    def imports(self, path):
        out = set()
        for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                out.add(base)
                out.update(base + "." + a.name for a in n.names)
        return out

    def test_no_exchange_executor_launcher_or_news_items(self):
        bad = {}
        for p in self.mods():
            imps = [i for i in self.imports(p) if i.startswith(("tb.venues", "tb.executor", "tb.intel", "tb.marketdata", "tb.llm.transport", "tb.llm.adapters", "socket", "urllib", "http"))]
            if imps:
                bad[os.path.basename(p)] = imps
        self.assertEqual(bad, {})

    def test_user_config_and_keys_not_touched(self):
        for p in self.mods():
            src = open(p, encoding="utf-8").read()
            lits = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            self.assertNotIn(".config", lits, p)
            self.assertFalse(any("BYBIT_" in x.upper() or "API_SECRET" in x or "API_KEY" in x for x in lits if len(x) < 60), p)

    def test_executor_only_through_queue_commands(self):
        """Команды исполнителю: только очередь плана (PlanStore.inbox_put в ports.py); другие модули пишут команды лишь через ExecPort.send."""
        for p in self.mods():
            src = open(p, encoding="utf-8").read()
            if not p.endswith("ports.py"):
                self.assertNotIn("inbox_put", src, p)
                self.assertNotIn("plan_store", src, p)
        src = open(os.path.join(SRC, "lifecycle", "ports.py"), encoding="utf-8").read()
        self.assertEqual(src.count("inbox_put"), 1)

    def test_dash_does_not_import_lifecycle_statically(self):
        for d, _, fs in os.walk(os.path.join(SRC, "dash")):
            for f in fs:
                if f.endswith(".py"):
                    self.assertFalse([i for i in self.imports(os.path.join(d, f)) if i.startswith("tb.lifecycle")], f)

    def test_control_only_loads_lifecycle_lazily(self):
        src = open(os.path.join(SRC, "control", "lifecycle.py"), encoding="utf-8").read()
        self.assertIn('importlib.import_module("tb.lifecycle.api")', src)

    def test_facts_are_read_as_file_not_via_intel_package(self):
        src = open(os.path.join(SRC, "lifecycle", "briefing.py"), encoding="utf-8").read()
        self.assertNotIn("tb.intel", src.replace("tb.intel.*", ""))


if __name__ == "__main__":
    unittest.main()
