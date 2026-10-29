"""WP13a: слои. Текст новостей (tb.intel.items) недоступен торговому мозгу, шлюзу, mcp, памяти, исполнителю, шине; сеть только в fetch.py; подключение сервиса к платформе."""
import ast
import json
import os
import unittest

import _bootstrap  # noqa: F401
import test_layering as TL
from tb.platform import services as S

SRC = TL.SRC
INTEL = os.path.join(SRC, "intel")
ITEMS = "tb.intel.items"
# пакеты, которым текст третьих лиц нельзя видеть ни прямо, ни транзитивно (memory/llm/lifecycle: когда появятся; отсутствие каталога не ошибка)
SEALED = ("brain", "gateway", "mcp", "memory", "llm", "lifecycle", "core", "executor", "venues", "logic", "marketdata", "platform")


def layer_modules(layer):
    top = os.path.join(SRC, layer)
    return [TL.module_name(p) for p in TL.py_files(layer)] if os.path.isdir(top) else []


def closure(mods):
    seen, stack, out = set(), list(mods), set()
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        path = os.path.join(os.path.dirname(SRC), *m.split("."))
        path = os.path.join(path, "__init__.py") if os.path.isdir(path) else path + ".py"
        if not os.path.isfile(path):
            continue
        imps = TL.imports_of(TL.read(path), m)
        out |= {n for n in imps if n == ITEMS or n.startswith(ITEMS + ".")}
        stack.extend(n for n in imps if n.startswith("tb.") and n not in seen)
    return out


class TestLayers(unittest.TestCase):
    def test_nobody_outside_intel_and_dash_imports_items(self):
        bad = []
        for d, _, fs in os.walk(SRC):
            rel = os.path.relpath(d, SRC).split(os.sep)[0]
            if rel in ("intel", "dash"):
                continue
            for f in fs:
                if f.endswith(".py"):
                    p = os.path.join(d, f)
                    mod = TL.module_name(p)
                    if any(n == ITEMS for n in TL.imports_of(TL.read(p), mod)):
                        bad.append(mod)
        self.assertEqual(bad, [])

    def test_sealed_layers_do_not_import_intel_at_all(self):
        bad = {}
        for layer in SEALED:
            for p in TL.py_files(layer) if os.path.isdir(os.path.join(SRC, layer)) else []:
                mod = TL.module_name(p)
                hit = [n for n in TL.imports_of(TL.read(p), mod) if TL.under(n, ("tb.intel",))]
                if hit and not (layer == "platform" and mod == "tb.platform.services"):
                    bad[mod] = hit
        self.assertEqual(bad, {})

    def test_transitive_closure_never_reaches_items(self):
        bad = {}
        for layer in SEALED:
            reach = closure(layer_modules(layer))
            if reach:
                bad[layer] = sorted(reach)
        self.assertEqual(bad, {})

    def test_brief_gateway_brain_memory_named_explicitly(self):
        for mod in ("tb.brain.brief", "tb.brain.api_daemon", "tb.gateway.registry", "tb.mcp.server", "tb.core.bus"):
            self.assertEqual(closure([mod]), set(), mod)

    def test_brief_reads_facts_file_only(self):
        src = TL.read(os.path.join(SRC, "brain", "brief.py"))
        self.assertIn("facts.json", src)
        self.assertNotIn("items.jsonl", src)
        self.assertNotIn("tb.intel", src)

    def test_store_and_calendar_do_not_import_items(self):
        for name in ("store.py", "calendar.py", "service.py"):
            imps = TL.imports_of(TL.read(os.path.join(INTEL, name)), "tb.intel." + name[:-3])
            if name == "service.py":
                self.assertIn(ITEMS, imps)                      # сервис пишет заголовки
            else:
                self.assertNotIn(ITEMS, imps, name)

    def test_items_only_dashboard_and_intel_readers(self):
        readers = sorted(TL.module_name(os.path.join(d, f)) for d, _, fs in os.walk(SRC) for f in fs if f.endswith(".py")
                         and ITEMS in TL.imports_of(TL.read(os.path.join(d, f)), TL.module_name(os.path.join(d, f))))
        self.assertTrue(set(readers) <= {"tb.intel.service", "tb.intel.cli", "tb.intel.sources", "tb.intel.verify", "tb.intel.fetch", "tb.intel.reader", "tb.intel.dashapi"} | set(m for m in readers if m.startswith("tb.dash.")), readers)

    def test_network_only_in_fetch(self):
        banned = ("urllib", "socket", "http", "ssl", "requests", "ftplib", "smtplib", "xmlrpc", "asyncio", "websocket")
        bad = {}
        for f in sorted(os.listdir(INTEL)):
            if f.endswith(".py") and f != "fetch.py":
                hit = [n for n in TL.imports_of(TL.read(os.path.join(INTEL, f)), "tb.intel." + f[:-3]) if TL.under(n, banned)]
                if hit:
                    bad[f] = hit
        self.assertEqual(bad, {})

    def test_intel_does_not_touch_exchange_trading_or_keys(self):
        forbidden = ("tb.venues", "tb.executor", "tb.brain", "tb.gateway", "tb.mcp", "tb.llm", "tb.launcher", "subprocess", "multiprocessing", "ctypes")
        bad = {}
        for f in sorted(os.listdir(INTEL)):
            if not f.endswith(".py"):
                continue
            path = os.path.join(INTEL, f)
            src = TL.read(path)
            fb = tuple(x for x in forbidden if not (x == "tb.llm" and f == "reader.py"))      # WP13b: карантинный читатель - единственный модуль intel с доступом к слою llm
            issues = [n for n in TL.imports_of(src, "tb.intel." + f[:-3]) if TL.under(n, fb)]
            lits = list(TL.string_literals(src))
            if any(".config" in x or "API_SECRET" in x or "API_KEY" in x or x.endswith(".env") for x in lits):
                issues.append("key-literal")
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("eval", "exec", "compile", "__import__"):
                    issues.append("call:" + n.func.id)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "os" and n.func.attr in ("system", "popen"):
                    issues.append("os." + n.func.attr)
            if issues:
                bad[f] = issues
        self.assertEqual(bad, {})

    def test_no_trade_words_in_intel_sources(self):
        for f in os.listdir(INTEL):
            if f.endswith(".py"):
                src = TL.read(os.path.join(INTEL, f)).lower()
                for w in ("place_order", "order/create", "trading-stop", "bybit trade", "set-leverage"):
                    self.assertNotIn(w, src, f)

    def test_runtime_paths_only_under_runtime_intel(self):
        from tb.intel import calendar, fetch, items, store
        r = "/x"
        for p in (items.ItemStore(r).path, store.facts_path(r), calendar.blackouts_path(r), fetch.cache_path("fng", r)):
            self.assertTrue(p.startswith(os.path.join(r, "runtime", "intel")), p)


class TestPlatformWiring(unittest.TestCase):
    def test_service_registered(self):
        self.assertIn("intel", S.ORDER)
        self.assertEqual(S.stop_order(["exec", "intel", "dash"]), ["dash", "intel", "exec"])
        self.assertEqual(S.parse_services("intel,exec"), ["exec", "intel"])

    def test_in_default_sets(self):
        """Интеграция вечера: intel (степень проверки источников выставляет `./tb intel verify`) входит во все наборы по умолчанию, сам деградирует."""
        cfg = S.load_cfg(None)
        for prof in S.PROFILES:
            self.assertIn("intel", S.default_services(prof, cfg))

    def test_command_and_hb(self):
        cfg = S.load_cfg(None)
        self.assertEqual(S.service_cmd("intel", "paper", False, cfg, py="py"), ["py", "-m", "tb.intel.service", "run"])
        self.assertEqual(cfg["svc"]["intel"]["hb"], "file")
        self.assertGreater(cfg["svc"]["intel"]["hb_stale_s"], 30)

    def test_env_has_no_secrets(self):
        env = S.child_env("intel", {"BYBIT_API_KEY": "k", "ANTHROPIC_API_KEY": "a", "HTTPS_PROXY": "p", "PATH": "/bin"}, "/tmp/x", ["intel"])
        self.assertNotIn("BYBIT_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["TB_PLATFORM_SERVICE"], "intel")

    def test_platform_json_has_intel_entry(self):
        with open(os.path.join(TL._bootstrap.ROOT, "config", "platform.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["svc"]["intel"]["hb"], "file")

    def test_event_class_for_alert(self):
        self.assertEqual(S.DEFAULTS["dispatch"]["classes"]["macro_blackout_soon"], 1)

    def test_intel_json_valid_and_candidates_unverified(self):
        from tb.intel import calendar
        with open(os.path.join(TL._bootstrap.ROOT, "config", "intel.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        ev = calendar.candidate_events(cfg["calendar"])
        self.assertGreaterEqual(len(ev), 4)
        self.assertTrue(all(e["v"] is False for e in ev))
        for c in cfg["calendar"]["candidates"]:
            self.assertIn("НЕ ПРОВЕРЕНО", c["note"])
        # verified ставит только владелец после `./tb intel verify`: допустимы лишь флаги verified и только у источников, прошедших проверку
        src = cfg.get("sources") or {}
        self.assertTrue(all(set(v) <= {"verified"} for v in src.values()))
        for bad in ("rss_coindesk", "rss_cointelegraph", "cal_bls_ics"):    # провалили проверку владельца 2026-10-28
            self.assertFalse((src.get(bad) or {}).get("verified"), bad)

    def test_real_macro_events_untouched_by_intel(self):
        with open(os.path.join(TL._bootstrap.ROOT, "config", "macro_events.json"), encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), list)


if __name__ == "__main__":
    unittest.main()
