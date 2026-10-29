"""Интеграция вечера: сервисы context и sync, наборы по умолчанию, P10/P15, дашборд (запас до лимитов, чип «Связь», замечания). Сеть не вызывается."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb import launcher
from tb.core import portfolio
from tb.platform import contextsvc, syncsvc
from tb.platform import services as S


class TestDefaults(unittest.TestCase):
    def test_default_sets_have_context_intel_sync(self):
        cfg = S.load_cfg(None)
        for prof in S.PROFILES:
            sv = S.default_services(prof, cfg)
            for n in ("context", "intel", "sync", "feed"):
                self.assertIn(n, sv, prof)
            self.assertEqual(sv, S.parse_services(",".join(sv)))           # порядок запуска согласован с ORDER

    def test_commands_and_secrets(self):
        cfg = S.load_cfg(None)
        self.assertEqual(S.service_cmd("context", "paper", False, cfg, py="py"), ["py", "-m", "tb.platform.contextsvc", "run"])
        self.assertEqual(S.service_cmd("sync", "paper", False, cfg, py="py"), ["py", "-m", "tb.launcher", "sync", "run"])
        env = S.child_env("sync", {"BYBIT_API_KEY": "x", "HTTPS_PROXY": "p", "PATH": "/bin"}, "/tmp/r", profile="paper")
        self.assertNotIn("BYBIT_API_KEY", env)                            # ключ read читает только процесс сервиса через launcher
        self.assertNotIn("HTTPS_PROXY", env)

    def test_launcher_only_run_and_once_need_key(self):
        self.assertTrue(launcher.sync_command(["sync", "run"]))
        self.assertTrue(launcher.sync_command(["sync", "once"]))
        self.assertFalse(launcher.sync_command(["sync", "status"]))
        self.assertFalse(launcher.sync_command(["sync", "confirm"]))
        self.assertFalse(launcher.sync_command(["bybit", "read", "balance"]))

    def test_launcher_no_key_degrades_without_error(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        env = {"TB_KEYS_DIR": d}
        self.assertFalse(launcher.prepare_sync(["sync", "run"], env))
        self.assertNotIn("BYBIT_API_KEY", env)
        with open(os.path.join(d, "bybit_mainnet.env"), "w") as f:
            f.write("BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\nHTTPS_PROXY=p\n")
        env = {"TB_KEYS_DIR": d}
        self.assertTrue(launcher.prepare_sync(["sync", "run"], env))
        self.assertEqual(env["BYBIT_ENV"], "mainnet")
        self.assertNotIn("HTTPS_PROXY", env)


class TestContextSvc(unittest.TestCase):
    def test_cycle_degrades_and_heartbeat(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "config"))
        shutil.copy(os.path.join(os.path.dirname(__file__), "..", "config", "context_config.json"), os.path.join(root, "config"))
        with open(os.path.join(root, "config", "macro_events.json"), "w") as f:
            json.dump([], f)
        from tb.marketdata import context as C

        def boom(url, params, timeout=10):
            raise OSError("нет сети")
        t = [1793000000.0]
        logs = []
        with mock.patch.object(C, "http_get_json", boom):
            rc = contextsvc.run(root, max_seconds=1.0, clock=lambda: t.__setitem__(0, t[0] + 1.0) or t[0], sleep=lambda s: None, poll_s=0.0, log=logs.append)
        self.assertEqual(rc, 0)
        ctx = json.load(open(os.path.join(root, "runtime", "context.json")))
        self.assertTrue(ctx["degraded_sources"])                          # источники недоступны: ok:false, но сервис жив и файл записан
        self.assertFalse(ctx["risk_off"])
        hb = S.read_json(S.hb_file("context", root))
        self.assertEqual(hb["service"], "context")
        self.assertTrue(hb["ok"])


class TestSync(unittest.TestCase):
    CFG = {"venue": "bybit", "max_dev_frac": 0.10, "stale_min": 15.0, "hwm_confirm_reads": 2, "interval_min": 5.0}

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "runtime", "platform"))
        self.pf = {"venues": {"bybit": {"equity": 40.0, "free": 30.0, "ts": "2026-10-28T00:00:00Z"}}, "hwm": 41.0}
        self.events = []

    def step(self, eq=None, free=30.0, err=None, now=1793000000.0, has_key=True):
        def fetch():
            if err:
                raise err
            return eq, free
        return syncsvc.step(self.root, self.CFG, now, fetch, has_key, emit=lambda r, n: self.events.append(n), load=lambda: self.pf, save=lambda p: self.pf.update(p))

    def test_small_change_updates(self):
        st = self.step(eq=40.8)
        self.assertEqual(st["status"], "ok")
        self.assertAlmostEqual(self.pf["venues"]["bybit"]["equity"], 40.8)
        self.assertFalse(st["stale"])

    def test_error_keeps_previous_and_marks_stale(self):
        st = self.step(err=OSError("сеть"))
        self.assertEqual(st["status"], "error")
        self.assertEqual(self.pf["venues"]["bybit"]["equity"], 40.0)
        self.assertTrue(st["stale"])

    def test_no_key_degrades(self):
        st = self.step(eq=99.0, has_key=False)
        self.assertEqual(st["status"], "no_key")
        self.assertEqual(self.pf["venues"]["bybit"]["equity"], 40.0)

    def test_zero_balance_is_error(self):
        with mock.patch("tb.venues.bybit.wallet_all", return_value={"UNIFIED": {"equity": 0.0, "free": 0.0}}):
            with self.assertRaises(RuntimeError):
                syncsvc.fetch_balance(client=object())
        with mock.patch("tb.venues.bybit.wallet_all", return_value={"UNIFIED": {"error": "x"}}):
            with self.assertRaises(RuntimeError):
                syncsvc.fetch_balance(client=object())

    def test_big_jump_needs_confirmation_and_emits_once(self):
        for eq in (30.0, 30.0):                                          # падение на 25%
            st = self.step(eq=eq)
        self.assertEqual(st["status"], "mismatch")
        self.assertEqual(self.pf["venues"]["bybit"]["equity"], 40.0)     # не применено
        self.assertEqual(len(self.events), 1)
        ok, msg = syncsvc.confirm(self.root, now=1793000100.0, load=lambda: self.pf, save=lambda p: self.pf.update(p))
        self.assertTrue(ok, msg)
        self.assertEqual(self.pf["venues"]["bybit"]["equity"], 30.0)
        self.assertEqual(self.pf["hwm"], 41.0)                           # вниз не трогается
        self.assertFalse(syncsvc.confirm(self.root, load=lambda: self.pf, save=lambda p: None)[0])

    def test_hwm_only_up_and_only_confirmed(self):
        self.step(eq=43.0)                                               # первое чтение выше hwm 41: рост ещё не подтверждён
        self.assertEqual(self.pf["hwm"], 41.0)
        self.step(eq=44.0)                                               # второе подряд: hwm = наименьшее из двух (43)
        self.assertEqual(self.pf["hwm"], 43.0)
        self.step(eq=42.0)                                               # снижение hwm не трогает
        self.assertEqual(self.pf["hwm"], 43.0)
        self.step(eq=44.5)                                               # всплеск одиночный
        self.step(eq=40.0)
        self.assertEqual(self.pf["hwm"], 43.0)

    def test_real_portfolio_module_roundtrip(self):
        path = os.path.join(self.root, "p.json")
        with mock.patch.object(portfolio, "PATH", path):
            portfolio.save(self.pf)
            st = syncsvc.step(self.root, self.CFG, 1793000000.0, lambda: (41.0, 31.0), True, emit=lambda r, n: None)
            self.assertEqual(st["status"], "ok")
            self.assertEqual(portfolio.load()["venues"]["bybit"]["free"], 31.0)


class TestDashNotes(unittest.TestCase):
    """Недочёты 3 и 4в: статус синхронизации капитала и «как закрыть» для каждого замечания в расшифровке индикатора."""

    def setUp(self):
        from test_dash import NOW as N
        self.now = N
        self.root = tempfile.mkdtemp(prefix="tbdashn_")
        self.addCleanup(shutil.rmtree, self.root, True)
        for d in ("state", "runtime/platform", "config"):
            os.makedirs(os.path.join(self.root, d))

    def w(self, rel, obj):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        from tb.dash import model
        model._CACHE.clear()

    def state(self):
        from tb.dash import model
        model._CACHE.clear()
        return model.build_state(self.root, now=self.now)

    def test_sync_state_in_account_and_status(self):
        self.w("state/portfolio.json", {"venues": {"bybit": {"equity": 40.0, "free": 30.0, "ts": "2026-10-27T12:00:00Z"}}, "hwm": 40.0})
        self.w("runtime/platform/sync.state.json", {"status": "no_key", "reason": "нет читающего ключа", "stale": True})
        st = self.state()
        self.assertEqual(st["account"]["sync"]["status"], "no_key")
        self.assertTrue(st["account"]["sync"]["stale"])
        item = [i for i in st["status"]["items"] if i["id"] == "capital"][0]
        self.assertEqual(item["level"], "warn")
        self.assertTrue(item["action"])

    def test_every_warning_has_action_and_preflight_notes(self):
        self.w("state/days/D-20261027-1/preflight.json", {"ts": self.now, "mode": "paper", "checks": [
            {"id": "P11", "name": "x", "sev": "A", "ok": False, "msg": "мозг недоступен", "numbers": {}},
            {"id": "P2", "name": "y", "sev": "R", "ok": True, "msg": "ок", "numbers": {}}]})
        d = os.path.join(self.root, "state", "days", "D-20261027-1", "preflight.json")
        os.utime(d, (self.now - 60, self.now - 60))
        st = self.state()
        items = st["status"]["items"]
        pf = [i for i in items if i["id"] == "pf-P11"]
        self.assertEqual(len(pf), 1)
        self.assertIn("brain probe", pf[0]["action"])
        for i in items:
            if i["level"] in ("warn", "bad"):
                self.assertTrue(i.get("action"), i)


if __name__ == "__main__":
    unittest.main()
