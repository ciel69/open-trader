import unittest

import _bootstrap  # noqa: F401
from fakes.fake_port import FakePort
from fakes.harness import Env, plan_long
from tb.executor import reconcile


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def foreign_pos(e, sl=90.0, sym="SOL"):
    e.port.pos[sym] = {"sym": sym, "side": "long", "size": 1.0, "avg": 99.0, "mark": 100.0, "sl": sl, "tp": 0.0, "liq": 50.0, "upnl": 0.0, "lev": 5.0,
                       "margin": 20.0, "ts": 0.0}


class ReconcileTable(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def rec(self):
        self.e.timer(1, dt=31)

    def test_startup_blocks_until_reconcile(self):
        e = Env(start=False)
        try:
            e.ex.holds["reconciling"] = 1
            r = e.submit()
            e.tick(100.1)
            e.candle(100.3)
            self.assertEqual(e.port.pos, {})
        finally:
            e.close()

    def test_position_closed_externally_finalizes(self):
        pid = self.e.enter()
        self.e.port.sink = None
        self.e.port.trigger_stop("SOL")            # стоп сработал, пока исполнитель не слушал
        self.rec()
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "closed")
        self.assertEqual(rt["close"]["reason"], "sl")
        self.assertTrue(self.e.trades()[0]["ts_close"])
        self.assertLess(rt["close"]["pnl"], 0)

    def test_old_foreign_history_not_attributed(self):
        """Регрессия testnet: после рестарта реконсиляция подтягивает историю биржи; чужие/старые исполнения не должны менять сегодняшний план."""
        pid = self.e.enter()
        before = self.e.rt(pid)["pos"]["qty"]
        now_ms = int(self.e.clock.time() * 1000)
        old = [{"exec_id": "OLD%d" % i, "sym": "SOL", "side": "Buy" if i % 2 else "Sell", "price": 101.0, "qty": 0.5, "fee": 0.05, "is_maker": False,
                "link_id": ("tbx-w167910-%d" % i) if i % 3 == 0 else ("tbx-1-1" if i % 3 == 1 else ""), "order_id": "o", "stop_type": "", "exec_type": "Trade",
                "closed_size": 0.5 if i % 2 == 0 else 0.0, "pnl": 0.0, "ts_ms": now_ms - 3600000 - i} for i in range(9)]
        self.e.port.execs = old + self.e.port.execs
        self.e.ex.last_exec_ts_ms = 0
        self.rec()
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "managing")
        self.assertAlmostEqual(rt["pos"]["qty"], before)
        self.assertEqual(len(rt["pos"]["entries"]), 1)

    def test_exec_state_persisted_across_restart(self):
        import json
        from tb import paths
        pid = self.e.enter()
        self.e.ex.save_state(force=True)
        d = json.load(open(paths.runtime("exec_state.json", root=self.e.root), encoding="utf-8"))
        self.assertTrue(d["last_exec_ts_ms"] > 0)
        self.assertTrue(d["seen_exec"])
        from test_exec_wal import restart
        ex2 = restart(self.e)
        self.assertEqual(ex2.last_exec_ts_ms, d["last_exec_ts_ms"])
        self.assertIn(d["seen_exec"][0], ex2.seen_exec)

    def test_empty_positions_glitch_does_not_finalize(self):
        """Единичный пустой ответ positions без исполнений (сбой биржи) не закрывает план и не снимает лесенку."""
        pid = self.e.enter()
        self.e.port.script("positions", [[], [], FakePort.PASS])
        self.rec()
        self.assertEqual(self.e.rt(pid)["status"], "managing")
        self.assertEqual(len([o for o in self.e.port.orders.values() if o["reduce_only"] and o["status"] == "New"]), 2)
        self.rec()
        self.assertEqual(self.e.rt(pid)["status"], "managing")
        self.assertNotIn("gone_reads", self.e.rt(pid)["counters"])

    def test_really_gone_without_executions_confirmed_after_3_reads(self):
        pid = self.e.enter()
        self.e.port.sink = None
        del self.e.port.pos["SOL"]
        self.rec()
        self.assertEqual(self.e.rt(pid)["status"], "managing")
        self.e.timer(7, dt=2)
        self.assertEqual(self.e.rt(pid)["status"], "closed")

    def test_close_not_duplicated_in_journal(self):
        pid = self.e.enter()
        self.e.port.sink = None
        self.e.port.trigger_stop("SOL")
        self.rec()
        self.rec()
        from tb.core import journal
        txt = _read(journal.PATH)
        self.assertEqual(txt.count('"ev": "close"'), 1)

    def test_missing_stop_restored(self):
        pid = self.e.enter()
        self.e.port.pos["SOL"]["sl"] = 0.0
        self.rec()
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)
        self.assertTrue(self.e.bus("reconcile_alert"))

    def test_missing_stop_price_beyond_closes(self):
        pid = self.e.enter()
        self.e.port.pos["SOL"]["sl"] = 0.0
        self.e.port.set_price("SOL", 98.0)
        self.rec()
        self.assertEqual(self.e.port.pos, {})

    def test_stop_tighter_accepted_wider_restored(self):
        pid = self.e.enter()
        self.e.port.raw_set_stop("SOL", 99.5)
        self.rec()
        self.assertEqual(self.e.rt(pid)["pos"]["cur_stop"], 99.5)
        self.e.port.raw_set_stop("SOL", 98.0)       # кто-то ослабил вручную
        self.rec()
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.5)

    def test_size_less_accepted_ladder_trimmed(self):
        pid = self.e.enter()
        rt = self.e.rt(pid)
        self.e.port.sink = None
        q = self.e.port.pos["SOL"]["size"]
        self.e.port.pos["SOL"]["size"] = 0.1       # часть закрыта вручную
        self.rec()
        rt = self.e.rt(pid)
        self.assertAlmostEqual(rt["pos"]["qty"], 0.1)
        rest = sum(o["qty"] - o["filled"] for o in self.e.port.orders.values() if o["reduce_only"] and o["status"] == "New")
        self.assertLessEqual(rest, 0.1 + 1e-9)

    def test_size_more_hold_and_no_action(self):
        pid = self.e.enter()
        n = len(self.e.port.calls)
        self.e.port.pos["SOL"]["size"] += 0.5
        self.rec()
        rt = self.e.rt(pid)
        self.assertEqual(rt["hold_reason"], "position_drift")
        self.assertTrue(self.e.ex.diffs)
        self.assertIn("reconcile_diffs", self.e.ex.holds)
        self.assertEqual([c for c in self.e.port.calls[n:] if c[0] in ("close_market", "place_entry")], [])

    def test_ladder_order_missing_replaced(self):
        pid = self.e.enter()
        t1 = self.e.rt(pid)["targets"]["t1"]
        self.e.port.orders[t1["link"]]["status"] = "Cancelled"
        self.rec()
        t1b = self.e.rt(pid)["targets"]["t1"]
        self.assertEqual(t1b["state"], "resting")
        self.assertNotEqual(t1b["link"], t1["link"])

    def test_orphan_tbx_order_cancelled_unknown_order_kept(self):
        pid = self.e.enter()
        self.e.port.pos["SOL"]  # noqa
        self.e.port.orders["tbx-99-1"] = {"sym": "SOL", "link_id": "tbx-99-1", "order_id": "O1", "side": "Sell", "type": "limit", "price": 110.0,
                                          "qty": 0.1, "filled": 0.0, "reduce_only": True, "status": "New", "ts": 0}
        self.e.port.orders["manual-1"] = {"sym": "SOL", "link_id": "manual-1", "order_id": "O2", "side": "Sell", "type": "limit", "price": 111.0,
                                          "qty": 0.1, "filled": 0.0, "reduce_only": True, "status": "New", "ts": 0}
        self.rec()
        self.assertEqual(self.e.port.orders["tbx-99-1"]["status"], "Cancelled")
        self.assertEqual(self.e.port.orders["manual-1"]["status"], "New")
        self.assertTrue([x for x in self.e.bus("reconcile_alert") if x["d"]["kind"] == "unknown_order"])

    def test_unknown_position_untouched_even_without_stop(self):
        foreign_pos(self.e, sl=0.0)
        self.rec()
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 0.0)
        self.assertEqual(self.e.port.writes(), [])
        self.assertTrue([x for x in self.e.bus("reconcile_alert") if x["d"]["kind"] == "orphan_position"])
        self.assertEqual(self.e.ex.diffs[0]["kind"], "orphan_position")

    def test_legacy_position_gets_stop_from_journal(self):
        import argparse
        from tb.core import journal
        journal.cmd_open(argparse.Namespace(id="BB-T-SOL-1", sym="SOL", side="long", entry=99.0, lev=5.0, margin=20.0, sl=95.0, tp=110.0, thesis="x", news="",
                                            regime="r", tags="bybit", mode="testnet", ctx={}, ts_open=None, venue="bybit"))
        foreign_pos(self.e, sl=0.0)
        self.rec()
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 95.0)

    def test_armed_order_gone_returns_waiting(self):
        r = self.e.submit(plan_long(entry={"when": {"kind": "candle_close", "tf": "1m", "dir": "above", "level": 100.2, "n": 1},
                                           "how": {"type": "limit", "price": 100.1, "post_only": True}, "ref_price": 100.2, "valid": {"ttl_min": 60}}))
        self.e.tick(100.1)
        self.e.candle(100.3)
        pid = r["plan_id"]
        link = self.e.rt(pid)["entry"]["link_id"]
        self.e.port.orders[link]["status"] = "Cancelled"
        self.rec()
        self.assertEqual(self.e.rt(pid)["status"], "armed")
        self.assertIn(self.e.rt(pid)["entry"]["state"], ("waiting", "blocked"))

    def test_executions_missed_during_outage_applied(self):
        pid = self.e.enter()
        t1 = self.e.rt(pid)["targets"]["t1"]
        self.e.port.sink = None
        self.e.port.set_price("SOL", 102.6)
        self.e.port.match("SOL")
        self.rec()
        self.assertEqual(self.e.rt(pid)["targets"]["t1"]["state"], "filled")

    def test_equity_sync_and_mismatch(self):
        self.e.port.equity = 40.5
        self.e.ex._equity_sync(self.e.clock.time())
        import json
        from tb.core import portfolio
        self.assertEqual(portfolio.load()["venues"]["bybit"]["equity"], 40.5)
        self.e.port.equity = 80.0               # вывод/пополнение: большое расхождение
        self.e.ex._equity_sync(self.e.clock.time())
        self.assertIn("equity_mismatch", self.e.ex.holds)
        self.assertEqual(portfolio.load()["venues"]["bybit"]["equity"], 40.5)

    def test_adopt_unknown_position(self):
        foreign_pos(self.e, sl=95.0)
        r = self.e.command("adopt", None, {"symbol": "SOL"})
        self.assertTrue(r["ok"], r)
        rt = self.e.rt(r["plan_id"])
        self.assertEqual(rt["status"], "managing")
        self.assertEqual(rt["pos"]["cur_stop"], 95.0)
        self.assertEqual(self.e.port.writes(), [])

    def test_adopt_without_stop_rejected(self):
        foreign_pos(self.e, sl=0.0)
        r = self.e.command("adopt", None, {"symbol": "SOL"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["code"], "E_ADOPT_NO_STOP")


class Connectivity(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.ex.on_state("private", True)
        self.e.ex.on_state("public", True)

    def tearDown(self):
        self.e.close()

    def test_private_down_hold_then_reconcile_release(self):
        pid = self.e.submit()["plan_id"]
        self.e.ex.on_state("private", False, 0.0)
        self.e.timer(1, dt=21)
        self.assertIn("private_ws", self.e.ex.holds)
        self.e.tick(100.1)
        self.e.candle(100.3)
        self.assertEqual(self.e.port.pos, {})
        self.e.ex.on_state("private", True, 25.0)
        self.e.timer(1)
        self.assertNotIn("private_ws", self.e.ex.holds)

    def test_rest_blind_hold_no_entries(self):
        pid = self.e.submit()["plan_id"]
        self.e.port.script("positions", [__import__("tb.executor.ports", fromlist=["x"]).PortError("network", "x")] * 30)
        self.e.port.script("account", [__import__("tb.executor.ports", fromlist=["x"]).PortError("network", "x")] * 30)
        self.e.timer(5, dt=31)
        self.assertIn("blind", self.e.ex.holds)

    def test_protective_action_works_in_hold(self):
        pid = self.e.enter()
        self.e.ex.holds["private_ws"] = 1
        self.e.tick(101.5)
        self.assertGreater(self.e.port.pos["SOL"]["sl"], 100.0)


if __name__ == "__main__":
    unittest.main()
