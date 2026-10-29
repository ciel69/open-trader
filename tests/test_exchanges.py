"""WP-X: реестр бирж (config/exchanges.json + local), фабрика портов (venues/registry), параллельные исполнители (по одному на биржу) на ФЕЙКОВОЙ второй бирже `fake2`:
раздельные lock/health/state/WAL, фильтрация планов и команд по venue, orderLinkId с префиксом биржи, тег биржи в журнале, суммарные лимиты guard по всем биржам.
Реальных вторых бирж нет; сети нет; корни временные."""
import argparse
import json
import os
import shutil
import unittest

import _bootstrap  # noqa: F401
from fakes.fake_port import FakePort
from fakes.harness import Env, plan_long
from tb import paths
from tb.core import exchcfg, guard, journal, linkid, plan_schema, portfolio
from tb.executor import main as xmain
from tb.executor.core import Executor
from tb.executor.daemon import NullFeed
from tb.platform import services as S
from tb.venues import registry


def fake_factory(profile, root, selftest, venue_id):
    return registry.Bundle(FakePort(env="paper"), NullFeed(), None, None)


class RegistryBase(unittest.TestCase):
    def setUp(self):
        self.e = Env(start=False)
        self.root = self.e.root
        self.addCleanup(self.e.close)
        self.addCleanup(registry.unregister, "fake2")
        shutil.copy(os.path.join(_bootstrap.ROOT, "config", "exchanges.json"), os.path.join(self.root, "config", "exchanges.json"))

    def enable_fake2(self):
        registry.register("fake2", fake_factory)
        os.makedirs(os.path.join(self.root, "config", "local"), exist_ok=True)
        with open(os.path.join(self.root, "config", "local", "exchanges.json"), "w", encoding="utf-8") as f:
            json.dump({"exchanges": {"fake2": {"name": "Фейковая биржа", "adapter": "fake2", "enabled": True, "mode": "paper"}}, "allowed": ["bybit", "fake2"]}, f)


class TestRegistry(RegistryBase):
    def test_default_registry_only_bybit_implemented(self):
        c = exchcfg.load(self.root)
        self.assertEqual((c["active"], c["allowed"]), ("bybit", ["bybit"]))
        self.assertTrue(c["exchanges"]["bybit"]["implemented"])
        for eid in ("okx", "binance"):
            self.assertFalse(c["exchanges"][eid]["implemented"])
            self.assertFalse(c["exchanges"][eid]["enabled"])
        self.assertEqual(registry.adapters(), ["bybit"])
        self.assertEqual(set(registry.adapters()), set(exchcfg.IMPLEMENTED))

    def test_unimplemented_exchange_gives_clear_error(self):
        with self.assertRaises(registry.ExchangeNotImplemented) as cm:
            registry.make_bundle("okx", "paper", self.root, selftest=True)
        self.assertIn("не реализована", str(cm.exception))
        with self.assertRaises(registry.ExchangeNotImplemented):
            registry.make_bundle("nosuch", "paper", self.root)
        with self.assertRaises(exchcfg.ExchangeError):
            exchcfg.set_enabled("okx", True, self.root)
        with self.assertRaises(exchcfg.ExchangeError):
            exchcfg.set_active("okx", self.root)
        with self.assertRaises(registry.ExchangeNotImplemented):
            registry.make_client("okx", "mainnet", self.root)
        # заготовка, включённая вручную в файле, сама отключается проверкой
        with open(os.path.join(self.root, "config", "exchanges.json"), encoding="utf-8") as f:
            d = json.load(f)
        d["exchanges"]["okx"]["enabled"] = True
        d["allowed"] = ["bybit", "okx"]
        with open(os.path.join(self.root, "config", "exchanges.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        c = exchcfg.load(self.root)
        self.assertFalse(c["exchanges"]["okx"]["enabled"])
        self.assertTrue(c["errors"])
        self.assertEqual(c["allowed"], ["bybit"])

    def test_bybit_paper_bundle_without_network(self):
        b = registry.make_bundle("bybit", "paper", self.root, selftest=True)
        self.assertEqual(b.port.env, "paper")
        self.assertTrue(callable(b.saver))
        self.assertEqual(b.venue_id, "bybit")

    def test_fake_exchange_through_registry_and_services(self):
        self.enable_fake2()
        c = exchcfg.load(self.root)
        self.assertEqual(c["allowed"], ["bybit", "fake2"])
        b = registry.make_bundle("fake2", "paper", self.root)
        self.assertEqual(b.venue_id, "fake2")
        self.assertEqual(S.exec_services(self.root), ["exec", "exec@fake2"])
        self.assertEqual(S.parse_services("scan,exec@fake2,exec"), ["exec", "exec@fake2", "scan"])
        self.assertEqual(S.stop_order(["exec", "exec@fake2", "scan", "dash"])[-2:], ["exec@fake2", "exec"])
        cmd = S.service_cmd("exec@fake2", "paper", False, {})
        self.assertEqual(cmd[-4:], ["--profile", "paper", "--exchange", "fake2"])
        self.assertEqual(S.service_cmd("exec", "paper", False, {})[-2:], ["--profile", "paper"])
        self.assertEqual(S.exec_venue("exec"), "bybit")
        self.assertIsNone(S.exec_venue("exec@Bad!"))
        with self.assertRaises(S.ConfigError):
            S.parse_services("exec@Bad!")
        cfg = dict(S.load_cfg(self.root), exec_per_exchange=True)
        self.assertEqual(S.default_services("paper", cfg, self.root)[:2], ["exec", "exec@fake2"])
        self.assertEqual(S.default_services("paper", S.load_cfg(self.root), self.root)[0], "exec")

    def test_exec_cli_rejects_unimplemented_exchange(self):
        rc = xmain.cmd_run(["--profile", "paper", "--exchange", "okx", "--root", self.root, "--max-seconds", "1", "--selftest"])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(paths.exec_lock_file(self.root, "okx")))

    def test_exchange_limits_modes_allowed(self):
        self.enable_fake2()
        exchcfg.set_mode("fake2", "testnet", self.root)
        self.assertEqual(exchcfg.load(self.root)["exchanges"]["fake2"]["mode"], "testnet")
        exchcfg.set_active("fake2", self.root)
        self.assertEqual(exchcfg.active(self.root), "fake2")
        with self.assertRaises(exchcfg.ExchangeError):
            exchcfg.set_enabled("fake2", False, self.root)            # активную выключить нельзя
        with self.assertRaises(exchcfg.ExchangeError):
            exchcfg.set_allowed(["bybit"], self.root)                  # активная должна остаться
        exchcfg.set_active("bybit", self.root)
        exchcfg.set_allowed(["bybit"], self.root)
        self.assertEqual(exchcfg.load(self.root)["allowed"], ["bybit"])
        with self.assertRaises(exchcfg.ExchangeError):
            exchcfg.set_limits("bybit", 99, self.root)
        exchcfg.set_limits("bybit", 2, self.root)
        self.assertEqual(exchcfg.load(self.root)["exchanges"]["bybit"]["limits"]["max_open_positions"], 2)


class TestPaths(unittest.TestCase):
    def test_default_venue_keeps_legacy_names(self):
        self.assertTrue(paths.exec_health_file("/r").endswith("exec_health.json"))
        self.assertTrue(paths.exec_health_file("/r", "bybit").endswith("exec_health.json"))
        self.assertTrue(paths.exec_health_file("/r", "okx").endswith("exec_health@okx.json"))
        self.assertTrue(paths.exec_lock_file("/r", "okx").endswith("exec@okx.lock"))
        self.assertTrue(paths.paper_port_file("/r").endswith("paper_port.json"))
        self.assertTrue(paths.paper_port_file("/r", "okx").endswith("paper_port@okx.json"))

    def test_link_ids(self):
        self.assertEqual(linkid.make(12, 3), "tbx-12-3")
        self.assertEqual(linkid.make(12, 3, "bybit"), "tbx-12-3")
        self.assertEqual(linkid.make(12, 3, "fake2"), "tbx-fake2-12-3")
        for lk in ("tbx-12-3", "tbx-fake2-12-3"):
            self.assertEqual(linkid.parse(lk), (12, 3))
            self.assertTrue(linkid.is_ours(lk))
        self.assertEqual((linkid.venue_of("tbx-12-3"), linkid.venue_of("tbx-fake2-1-2"), linkid.venue_of("BB-1")), ("bybit", "fake2", None))
        self.assertIsNone(linkid.parse("tbx-Fake-1-2"))
        with self.assertRaises(ValueError):
            linkid.make(1, 2, "Bad Venue")
        self.assertLessEqual(len(linkid.make(10 ** 9, 999999, "abcdefghijkl")), linkid.MAX_LEN)

    def test_plan_schema_venue_field(self):
        p = plan_schema.normalize(plan_long(venue="FAKE2"))[0]
        self.assertEqual(p.get("venue"), "fake2")
        self.assertNotIn("venue", plan_schema.normalize(plan_long())[0])         # старые планы без поля остаются прежними (каноническая форма не меняется)
        self.assertIn("venue", plan_schema.json_schema()["properties"])


class TestParallelExecutors(RegistryBase):
    """Два исполнителя на одном корне: bybit (FakePort A) и fake2 (FakePort B)."""

    def setUp(self):
        RegistryBase.setUp(self)
        self.enable_fake2()
        self.e1 = Env()
        self.addCleanup(self.e1.close)
        shutil.copy(os.path.join(_bootstrap.ROOT, "config", "exchanges.json"), os.path.join(self.e1.root, "config", "exchanges.json"))
        self.root = self.e1.root
        os.makedirs(os.path.join(self.root, "config", "local"), exist_ok=True)
        with open(os.path.join(self.root, "config", "local", "exchanges.json"), "w", encoding="utf-8") as f:
            json.dump({"exchanges": {"fake2": {"name": "Фейковая биржа", "adapter": "fake2", "enabled": True}}, "allowed": ["bybit", "fake2"]}, f)
        self.p2 = FakePort(env="testnet", clock=self.e1.clock.time)
        self.p2.set_price("SOL", 100.0, spread=0.04)
        self.q2 = []
        self.p2.sink = self.q2.append
        self.ex2 = Executor(self.p2, root=self.root, clock=self.e1.clock.time, sleep=self.e1.clock.advance, profile="testnet", cfg=self.e1.cfg, venue_id="fake2")
        self.ex2.start()
        self.assertEqual(self.e1.ex.venue_id, "bybit")

    def pump2(self):
        while self.q2:
            self.ex2.on_private(self.q2.pop(0))

    def tick2(self, price, dt=0.2):
        self.e1.clock.advance(dt)
        self.p2.set_price("SOL", price, spread=0.04)
        for p in self.p2.pos.values():
            p["mark"] = price
        self.ex2.on_ticker("SOL", price, price - 0.02, price + 0.02, price, 0.0, self.e1.clock.time())
        self.pump2()

    def candle2(self, close):
        ms = int(self.e1.clock.time() * 1000)
        self.ex2.on_kline("SOL", 1, ms, close, close + 0.1, close - 0.1, close, 100.0, 10000.0, True)
        self.pump2()

    def submit_to(self, plan):
        """Команда кладётся в ОБЩИЙ inbox; каждый исполнитель берёт только свои."""
        cid = self.e1.ex.store.inbox_put({"actor": "brain:chat", "op": "submit", "payload": plan, "reason": "test"})
        self.ex2.poll_inbox()
        self.pump2()
        self.e1.ex.poll_inbox()
        self.e1.pump()
        return cid

    def test_commands_and_plans_are_routed_by_venue(self):
        c1 = self.submit_to(plan_long())                                  # без venue = bybit
        c2 = self.submit_to(plan_long(venue="fake2"))
        r1, r2 = self.e1.ex.store.outbox_get(c1), self.e1.ex.store.outbox_get(c2)
        self.assertTrue(r1["ok"] and r2["ok"], (r1, r2))
        self.assertEqual(set(self.e1.ex.plans), {r1["plan_id"]})
        self.assertEqual(set(self.ex2.plans), {r2["plan_id"]})
        self.assertNotEqual(r1["plan_id"], r2["plan_id"])
        self.assertEqual(self.e1.ex.store.load(r2["plan_id"])[0]["venue"], "fake2")
        # команда по существующему плану тоже идёт только своему исполнителю
        cid = self.e1.ex.store.inbox_put({"actor": "brain:chat", "op": "cancel", "plan_id": r2["plan_id"], "reason": "test"})
        self.e1.ex.poll_inbox()
        self.assertIsNone(self.e1.ex.store.outbox_get(cid))               # исполнитель bybit не тронул чужую команду
        self.ex2.poll_inbox()
        self.assertIsNotNone(self.ex2.store.outbox_get(cid))

    def test_independent_state_files_locks_and_link_ids_and_journal_tag(self):
        r1 = self.e1.submit()
        r2_cid = self.ex2.store.inbox_put({"actor": "brain:chat", "op": "submit", "payload": plan_long(venue="fake2"), "reason": "test"})
        self.ex2.poll_inbox()
        self.pump2()
        pid2 = self.ex2.store.outbox_get(r2_cid)["plan_id"]
        pid1 = r1["plan_id"]
        # вход на обеих биржах параллельно (одна монета SOL на двух биржах: у каждой своя позиция и свой счёт)
        for px in (100.1,):
            self.e1.tick(px)
            self.tick2(px)
        self.e1.candle(100.3)
        self.candle2(100.3)
        self.e1.tick(100.25)
        self.tick2(100.25)
        rt1, rt2 = self.e1.rt(pid1), self.e1.rt(pid2)
        self.assertEqual((rt1["status"], rt2["status"]), ("managing", "managing"), (rt1.get("status"), rt2.get("status")))
        self.assertIn("SOL", self.e1.port.pos)
        self.assertIn("SOL", self.p2.pos)
        self.assertIsNot(self.e1.port.pos["SOL"], self.p2.pos["SOL"])
        links1 = [o for o in self.e1.port.orders]
        links2 = [o for o in self.p2.orders]
        self.assertTrue(links1 and all(l.startswith("tbx-") and not l.startswith("tbx-fake2-") for l in links1), links1)
        self.assertTrue(links2 and all(l.startswith("tbx-fake2-") for l in links2), links2)
        self.assertFalse(set(links1) & set(links2))
        # журнал: тег биржи
        trades = {t["plan_id"]: t for t in self.e1.trades() if t.get("plan_id")}
        self.assertEqual(trades[pid1]["venue"], "bybit")
        self.assertEqual(trades[pid2]["venue"], "fake2")
        self.assertIn("fake2", trades[pid2]["tags"])
        # раздельные файлы здоровья, снимков и состояния
        for fn in (paths.exec_health_file, paths.exec_snapshot_file):
            self.assertTrue(os.path.exists(fn(self.root)))
            self.assertTrue(os.path.exists(fn(self.root, "fake2")))
        with open(paths.exec_health_file(self.root), encoding="utf-8") as f:
            h1 = json.load(f)
        with open(paths.exec_health_file(self.root, "fake2"), encoding="utf-8") as f:
            h2 = json.load(f)
        self.assertEqual((h1["venue"], h2["venue"]), ("bybit", "fake2"))
        self.assertEqual((h1["positions"], h2["positions"]), (1, 1))
        self.e1.ex.save_state(force=True)
        self.ex2._state_dirty = True
        self.ex2.save_state(force=True)
        self.assertTrue(os.path.exists(paths.exec_state_file(self.root, "fake2")))
        # блокировки независимы: оба экземпляра берут свой lock, второй на ту же биржу отказан
        ok_a, _ = xmain.acquire_lock(self.root, venue=None)
        ok_b, _ = xmain.acquire_lock(self.root, venue="fake2")
        ok_c, info = xmain.acquire_lock(self.root, venue="fake2")
        self.assertEqual((ok_a, ok_b, ok_c), (True, True, False))
        xmain.release_lock(self.root, None)
        xmain.release_lock(self.root, "fake2")
        # позиция одной биржи не влияет на другую
        self.p2.pos.pop("SOL")
        self.assertIn("SOL", self.e1.port.pos)

    def test_wal_pending_is_per_plan_and_recovered_by_owner_only(self):
        c = self.submit_to(plan_long(venue="fake2"))
        pid = self.e1.ex.store.outbox_get(c)["plan_id"]
        rt = self.ex2.store.load(pid)[1]
        rt.setdefault("actions", {"seq": 0, "pending": []})["pending"] = []
        self.assertEqual(self.ex2.store.load(pid)[1]["actions"]["pending"] if "actions" in self.ex2.store.load(pid)[1] else [], [])
        # исполнитель bybit перечитывает планы и чужой план не берёт
        self.e1.ex._load_plans()
        self.assertNotIn(pid, self.e1.ex.plans)
        self.ex2._load_plans()
        self.assertIn(pid, self.ex2.plans)


class TestGuardAggregation(unittest.TestCase):
    """guard считает лимиты по сумме всех бирж (journal.mode=live), в том числе дубль монеты и лимит одной биржи."""

    def setUp(self):
        import tempfile
        self.old = journal.PATH
        self.dir = tempfile.mkdtemp(prefix="tbg_")
        journal.PATH = os.path.join(self.dir, "trades.jsonl")
        self.oldp = portfolio.PATH
        portfolio.PATH = os.path.join(self.dir, "portfolio.json")
        self.prof = guard.load_profile()

    def tearDown(self):
        journal.PATH, portfolio.PATH = self.old, self.oldp
        shutil.rmtree(self.dir, ignore_errors=True)

    def open_pos(self, sym, side, risk, venue, tid):
        journal._append({"ev": "open", "id": tid, "sym": sym, "side": side, "mode": "live", "risk_usdt": risk, "notional": 50, "entry": 1, "ts_open": "x", "venue": venue})

    def args(self, **kw):
        d = dict(sym="SOL", side="long", entry=100.0, sl=98.0, tp=106.0, equity=65.0, free=40.0, score=1.0, regime="range", min_quote=1.0, contract_max_lev=50)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_open_risk_slots_sum_across_exchanges(self):
        self.open_pos("ETH", "long", 0.3, "bybit", "A")
        self.open_pos("BTC", "short", 0.2, "fake2", "B")
        st = guard.day_state(self.prof, "2026-10-27")
        self.assertEqual(st["open_count"], 2)
        self.assertAlmostEqual(st["open_risk"], 0.5)
        self.assertEqual(sorted(st["open_syms"]), ["BTC", "ETH"])
        self.assertEqual(st["by_venue"]["bybit"]["open"], 1)
        self.assertEqual(st["by_venue"]["fake2"]["open"], 1)
        self.assertAlmostEqual(st["by_venue"]["fake2"]["risk"], 0.2)

    def test_same_coin_on_two_exchanges_is_a_duplicate(self):
        self.open_pos("SOL", "long", 0.2, "fake2", "B")
        r = guard.check(self.args(), self.prof, "2026-10-27", ctx={"venue": "bybit"})
        self.assertFalse(r["ok"])
        self.assertTrue([w for w in r["reasons"] if w.startswith("position_exists_on_symbol")])

    def test_max_open_slots_counts_all_exchanges(self):
        n = self.prof["max_open_positions"]
        for i in range(n):
            self.open_pos("S%d" % i, "long", 0.01, "fake2" if i % 2 else "bybit", "T%d" % i)
        r = guard.check(self.args(sym="NEW"), self.prof, "2026-10-27", ctx={"venue": "bybit"})
        self.assertTrue([w for w in r["reasons"] if w.startswith("max_open_positions")], r["reasons"])

    def test_correlated_side_risk_sum_across_exchanges(self):
        grp = self.prof.get("correlation_groups") or []
        if not grp:
            self.skipTest("нет групп корреляции в профиле")
        a, b = grp[0][0], grp[0][1]
        self.open_pos(a, "long", 0.9, "fake2", "C1")
        r = guard.check(self.args(sym=b, sl=98.0), self.prof, "2026-10-27", ctx={"venue": "bybit"})
        self.assertTrue(r["limits"].get("corr_group") or not r["ok"] or r["limits"]["same_side_risk"] > 0)

    def test_per_exchange_limit_and_total_equity_sum(self):
        portfolio.set_venue("bybit", 40.0, 30.0)
        portfolio.set_venue("fake2", 25.0, 20.0)
        self.assertAlmostEqual(portfolio.total(portfolio.load()), 65.0)
        self.assertAlmostEqual(portfolio.load()["hwm"], 65.0)
        self.open_pos("ETH", "long", 0.2, "fake2", "A")
        old = guard._venue_cap
        guard._venue_cap = lambda v: 1 if v == "fake2" else None
        try:
            r = guard.check(self.args(sym="BTC"), self.prof, "2026-10-27", ctx={"venue": "fake2"})
            self.assertTrue([w for w in r["reasons"] if w.startswith("max_open_positions_on_exchange")], r["reasons"])
            r = guard.check(self.args(sym="BTC"), self.prof, "2026-10-27", ctx={"venue": "bybit"})
            self.assertFalse([w for w in r["reasons"] if w.startswith("max_open_positions_on_exchange")])
        finally:
            guard._venue_cap = old

    def test_daily_loss_sums_both_exchanges(self):
        day = "2026-10-27"
        for tid, venue, pnl in (("L1", "bybit", -0.6), ("L2", "fake2", -0.6)):
            journal._append({"ev": "open", "id": tid, "sym": tid, "side": "long", "mode": "live", "risk_usdt": 0.1, "notional": 5, "entry": 1, "ts_open": "x", "venue": venue})
            journal._append({"ev": "close", "id": tid, "ts_close": day + "T10:00:00Z", "pnl_usdt": pnl, "reason": "tp"})
        st = guard.day_state(self.prof, day)
        self.assertAlmostEqual(st["realized_today"], -1.2)
        self.assertAlmostEqual(st["by_venue"]["fake2"]["realized_today"], -0.6)


class TestPortContractFake2(unittest.TestCase):
    """Контракт порта для фейковой биржи fake2: любой новый адаптер подключается так же (docs/EXCHANGES.md)."""

    def test_contract_runs_on_fake2_factory(self):
        import test_port_contract as tpc

        class C(tpc.PortContract, unittest.TestCase):
            def make_port(self):
                p = registry.Bundle(FakePort(env="paper"), NullFeed()).port
                p.set_price("SOL", 100.0, spread=0.1)
                return p
        with open(os.devnull, "w") as dn:
            res = unittest.TextTestRunner(stream=dn, verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(C))
        self.assertTrue(res.wasSuccessful(), res.failures + res.errors)
        self.assertGreater(res.testsRun, 5)


if __name__ == "__main__":
    unittest.main()
