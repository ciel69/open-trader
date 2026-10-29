"""WP16: CLI `./tb risk ...`, невозможность смены уровня агентом/шлюзом, диспетчер."""
import ast
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys

import _bootstrap  # noqa: F401
import _wp16 as W
from tb import __main__ as disp
from tb.control import risk_cmd
from tb.core import risklevels as R


class Cli(W.Base):
    def setUp(self):
        W.Base.setUp(self)
        self._tty = risk_cmd._is_tty
        risk_cmd._is_tty = lambda: True

    def tearDown(self):
        risk_cmd._is_tty = self._tty
        W.Base.tearDown(self)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = risk_cmd.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_show_default_and_json(self):
        code, out, _ = self.run_cli("level", "show")
        self.assertEqual(code, 0)
        self.assertIn("Уровень риска: 3", out)
        code, out, _ = self.run_cli("level", "show", "--json")
        d = json.loads(out)
        self.assertEqual(d["current"]["level"], 3)
        self.assertEqual(d["limits"]["risk_per_trade_frac"], 0.03)

    def test_list_marks_proposed(self):
        code, out, _ = self.run_cli("level", "list")
        self.assertEqual(code, 0)
        self.assertIn("ПРЕДЛОЖЕНО", out)
        self.assertIn("подтвердить владельцу", out)
        self.assertIn("потолки", out)
        self.assertEqual(out.count("ПРЕДЛОЖЕНО"), 5)      # заголовок + уровни 1, 2, 4, 5

    def test_set_requires_phrase(self):
        code, out, err = self.run_cli("level", "set", "2")
        self.assertEqual(code, 2)
        self.assertIn("УРОВЕНЬ РИСКА 2 Осторожный", out)
        self.assertFalse(os.path.exists(R.level_file(self.root)))
        code, out, err = self.run_cli("level", "set", "2", "--confirm", "да")
        self.assertEqual(code, 2)
        self.assertIn("отказ", err)
        self.assertEqual(R.current_level(self.root)["level"], 3)
        code, out, err = self.run_cli("level", "set", "2", "--confirm", self.phrase(2))
        self.assertEqual(code, 0, err)
        self.assertEqual(R.current_level(self.root)["level"], 2)
        self.assertEqual(self.run_cli("level", "show")[1].count("Осторожный"), 1)

    def test_set_4_needs_every_warning_and_ack(self):
        code, out, _ = self.run_cli("level", "set", "5")
        self.assertEqual(code, 2)
        for wid in ("W_TRADE_LOSS", "W_DAY_LOSS", "W_PYRAMID", "W_CARRY", "W_NO_CONFIRM", "W_UNCONFIRMED"):
            self.assertIn(wid, out)
        self.assertIn("5%", out)
        self.assertIn("БЕЗ вашего подтверждения", out)
        code, out, err = self.run_cli("level", "set", "4", "--confirm", self.phrase(4))
        self.assertEqual(code, 2)
        self.assertIn("не приняты", err)
        req = [w["id"] for w in R.level_warnings(4, self.root) if w["required"]]
        argv = ["level", "set", "4", "--confirm", self.phrase(4), "--ack", "я принимаю риск"] + sum((["--accept", i] for i in req), [])
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertIn("ПОВЫШЕНИЕ", out)
        self.assertEqual(R.current_level(self.root)["level"], 4)

    def test_set_refused_without_tty(self):
        risk_cmd._is_tty = lambda: False
        code, out, err = self.run_cli("level", "set", "2", "--confirm", self.phrase(2))
        self.assertEqual(code, 2)
        self.assertIn("TTY", err)
        self.assertFalse(os.path.exists(R.level_file(self.root)))
        self.assertEqual(self.run_cli("init-key")[0], 2)

    def test_set_bad_level(self):
        self.assertEqual(self.run_cli("level", "set", "9", "--confirm", "x")[0], 2)
        self.assertEqual(self.run_cli("bogus")[0], 2)

    def test_check_apply(self):
        from tb.core import portfolio
        portfolio.save({"venues": {"bybit": {"equity": 27.0, "free": 20.0, "ts": "x"}}, "hwm": 30.0})     # просадка 10%
        code, out, _ = self.run_cli("level", "check")
        self.assertEqual(code, 0)
        self.assertIn("условия авто-понижения", out)
        self.assertEqual(R.current_level(self.root)["level"], 3)
        code, out, _ = self.run_cli("level", "check", "--apply")
        self.assertEqual(R.current_level(self.root)["level"], 2)
        self.run_cli("level", "check", "--apply")
        self.assertEqual(R.current_level(self.root)["level"], 2)

    def test_init_key_creates_0600_only_if_absent_and_never_prints_key(self):
        self.drop_key()
        code, out, err = self.run_cli("init-key")
        self.assertEqual(code, 0, err)
        path = os.path.join(self.keys, "control.json")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        key = W.jload(path)["hmac_key"]
        self.assertEqual(len(key), 64)
        self.assertNotIn(key, out + err)
        self.assertIsNotNone(R._read_key())
        with open(path, "rb") as f:
            before = f.read()
        code, out, err = self.run_cli("init-key")
        self.assertEqual(code, 2)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)       # существующий ключ не затирается
        self.assertNotIn(key, out + err)

    def test_dispatcher_group(self):
        self.assertIn("risk", disp.V2)
        self.assertRegex(disp.USAGE, r"(?m)^  risk level")
        code = subprocess.call([sys.executable, "-m", "tb", "risk", "level", "show", "--json"], cwd=W.ROOT, env=dict(os.environ, PYTHONPATH=os.path.join(W.ROOT, "src"), TB_ROOT=self.root, TB_KEYS_DIR=self.keys), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(code, 0)


class NoAgentPath(W.Base):
    """Агент через шлюз/MCP/мозг/LLM уровень менять не может: инструмента нет, функции записи и ключ недоступны этим слоям."""
    FORBIDDEN_NAMES = ("set_level", "auto_downgrade", "sign_state", "_write_state", "init_key", "_read_key", "risk_cmd", "risk_level.json", "hmac_key", "risklevels")

    def layer_sources(self, layer):
        top = os.path.join(W.ROOT, "src", "tb", layer)
        for d, _, fs in os.walk(top):
            for f in fs:
                if f.endswith(".py"):
                    p = os.path.join(d, f)
                    with open(p, encoding="utf-8") as fh:
                        yield p, fh.read()

    def test_gateway_catalog_has_no_risk_tool(self):
        from tb.gateway import catalog
        names = " ".join(sorted(catalog.TOOLS)).lower()
        for bad in ("risk", "level", "limit", "control", "kill", "config", "profile"):
            self.assertNotIn(bad, names, names)

    def test_gateway_mcp_brain_llm_do_not_touch_level_writer(self):
        for layer in ("gateway", "mcp", "brain", "llm"):
            for p, src in self.layer_sources(layer):
                if layer == "brain" and p.endswith("brief.py"):
                    continue         # brief читает уровень (поле risk_level, WP16/WP13), но не пишет: проверяется ниже
                for n in self.FORBIDDEN_NAMES:
                    if n == "risklevels" and p.endswith(os.path.join("gateway", "catalog.py")):
                        continue     # WP11-C2: каталог только ЧИТАЕТ политики (pyramid_policy, setup_allowed); имена записи запрещены и там
                    self.assertNotIn(n, src, "%s содержит %s" % (p, n))
                imports = [x.module or "" for x in ast.walk(ast.parse(src)) if isinstance(x, ast.ImportFrom)]
                imports += [a.name for x in ast.walk(ast.parse(src)) if isinstance(x, ast.Import) for a in x.names]
                self.assertFalse([i for i in imports if i.startswith("tb.control")], (p, imports))

    def test_brain_reads_but_never_writes_level(self):
        for p, src in self.layer_sources("brain"):
            for n in ("set_level", "auto_downgrade", "sign_state", "_write_state", "init_key", "_read_key"):
                self.assertNotIn(n, src, p)

    def test_gateway_cannot_call_unknown_risk_tool(self):
        from tb.gateway import catalog
        for t in ("risk.set", "risk_set", "set_risk_level", "risk_level_set"):
            self.assertNotIn(t, catalog.TOOLS)

    def test_only_core_and_control_write_level_file(self):
        hits = []
        for d, _, fs in os.walk(os.path.join(W.ROOT, "src", "tb")):
            for f in fs:
                if f.endswith(".py"):
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        if re.search(r"\bset_level\b|_write_state\(", fh.read()):
                            hits.append(os.path.relpath(os.path.join(d, f), os.path.join(W.ROOT, "src", "tb")))
        # пишут уровень только ядро и пакет control (CLI владельца, адаптер/handlers ctl WP12); ни gateway, mcp, brain, llm, dash, executor
        outside = [h for h in hits if h != os.path.join("core", "risklevels.py") and not h.startswith("control" + os.sep)]
        self.assertEqual(outside, [])
        self.assertIn(os.path.join("core", "risklevels.py"), hits)

    def test_signed_file_cannot_be_forged_without_key(self):
        forged = {"level": 5, "since": "x", "by": "owner", "history": []}
        forged["mac"] = R.sign_state(forged, b"\x00" * 32)
        p = R.level_file(self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        W.jdump(forged, p)
        self.assertEqual(R.current_level(self.root)["level"], 1)
