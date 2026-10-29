import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from fakes.fake_port import FakePort
from fakes.harness import Env, plan_long
from tb.core import exec_cfg
from tb.executor import gate as G
from tb.executor.ports import PortError
from tb.logic import rules


class GateDenials(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def armed(self, plan=None):
        r = self.e.submit(plan)
        self.assertTrue(r["ok"], r)
        return r["plan_id"]

    def trigger(self):
        self.e.tick(100.1)
        self.e.candle(100.3)
        self.e.tick(100.25)

    def test_kill_blocks_entry_and_cancels_armed(self):
        pid = self.armed()
        exec_cfg.set_kill(False, "t", root=self.e.root)
        self.trigger()
        self.assertEqual(self.e.port.pos, {})
        self.e.timer()
        self.assertEqual(self.e.rt(pid)["status"], "cancelled")
        self.assertTrue(self.e.bus("kill"))

    def test_kill_checked_inside_call_after_gate(self):
        """KILL появился между Gate и отправкой: Actions.call отказывает до порта."""
        pid = self.armed()
        self.e.tick(100.1)
        orig = self.e.ex.gate.entry

        def entry_then_kill(p, v):
            r = orig(p, v)
            exec_cfg.set_kill(False, "race", root=self.e.root)
            return r
        with mock.patch.object(self.e.ex.gate, "entry", entry_then_kill):
            self.e.candle(100.3)
        self.assertEqual([c for c in self.e.port.calls if c[0] == "place_entry"], [])
        self.assertEqual(self.e.port.pos, {})

    def test_kill_flatten_closes_all(self):
        pid = self.e.enter()
        exec_cfg.set_kill(True, "panic", root=self.e.root)
        self.e.timer(3, dt=2.5)
        self.assertEqual(self.e.port.pos, {})
        self.assertEqual(self.e.rt(pid)["status"], "closed")
        self.assertEqual(self.e.rt(pid)["close"]["reason"], "kill")

    def test_kill_allows_stop_tighten(self):
        pid = self.e.enter()
        exec_cfg.set_kill(False, "t", root=self.e.root)
        self.e.tick(101.5)
        self.assertGreater(self.e.port.pos["SOL"]["sl"], 100.0)

    def test_hold_blocks_entry(self):
        pid = self.armed()
        self.e.command("hold", pid)
        self.trigger()
        self.assertEqual(self.e.port.pos, {})
        self.e.command("release", pid)
        self.e.tick(100.26)
        self.e.candle(100.31)
        self.e.tick(100.27)
        self.assertIn("SOL", self.e.port.pos)

    def test_global_hold_blocks_entry(self):
        pid = self.armed()
        self.e.ex.holds["private_ws"] = 1
        self.trigger()
        self.assertEqual(self.e.port.pos, {})
        self.assertEqual(self.e.rt(pid)["status"], "armed")

    def test_guard_denial_blocks_and_exhausts(self):
        """Дневной лимит убытка достигнут: guard отказывает -> entry_blocked, после попыток invalidated."""
        from tb.core import journal
        import argparse
        pid = self.armed()
        with mock.patch("tb.core.guard.evaluate", return_value={"ok": False, "reasons": ["daily_loss_limit_reached: -3.10"]}):
            for i in range(3):
                self.e.candle(100.3 + i * 0.01)
                self.e.tick(100.25)
                self.e.timer(1, dt=16)
                self.e.tick(100.25)
        self.assertEqual(self.e.port.pos, {})
        self.assertTrue(self.e.bus("entry_blocked"))
        self.assertEqual(self.e.rt(pid)["status"], "invalidated")

    def test_min_size_denied_no_round_up(self):
        plan = plan_long(risk={"score": 0.6, "size_mult": 0.01})
        pid = self.armed(plan)
        self.trigger()
        self.assertEqual(self.e.port.pos, {})
        self.assertEqual(self.e.rt(pid)["entry"]["blocked_why"], "MIN_SIZE")

    def test_size_mult_reduces(self):
        a = Env()
        b = Env()
        try:
            for env, m in ((a, 1.0), (b, 0.5)):
                env.submit(plan_long(risk={"score": 0.6, "size_mult": m}, entry=dict(plan_long()["entry"])))
                env.tick(100.1)
                env.candle(100.3)
                env.tick(100.25)
            qa, qb = a.port.pos["SOL"]["size"], b.port.pos["SOL"]["size"]
            self.assertLess(qb, qa)
            self.assertAlmostEqual(qb, round(int(qa * 0.5 / 0.1 + 1e-9) * 0.1, 10), places=6)
        finally:
            a.close()
            b.close()

    def test_symbol_busy_second_plan_rejected(self):
        self.armed()
        r = self.e.submit()
        self.assertFalse(r["ok"])
        self.assertIn("E_SYMBOL_BUSY", [x["code"] for x in r["errors"]])

    def test_foreign_position_blocks_entry(self):
        pid = self.armed()
        self.e.port.pos["SOL"] = {"sym": "SOL", "side": "long", "size": 1.0, "avg": 99.0, "mark": 100.0, "sl": 90.0, "tp": 0.0, "liq": 0.0,
                                  "upnl": 0.0, "lev": 5.0, "margin": 1.0, "ts": 0.0}
        self.e.ex.exch_pos["SOL"] = dict(self.e.port.pos["SOL"])
        self.trigger()
        self.assertEqual(self.e.port.calls_of("place_entry") if hasattr(self.e.port, "calls_of") else [c for c in self.e.port.calls if c[0] == "place_entry"], [])

    def test_rate_limit_actions_per_symbol(self):
        pid = self.armed()
        self.e.ex.gate._acts["SOL"] = [self.e.clock.time()] * 4
        self.trigger()
        self.assertEqual(self.e.port.pos, {})

    def test_price_jump_ignored(self):
        self.e.tick(100.0)
        self.e.ex.on_ticker("SOL", 150.0, 149.9, 150.1, 150.0, 0, self.e.clock.time())
        self.assertEqual(self.e.ex.px["SOL"]["last"], 100.0)
        self.assertEqual(self.e.ex.bad_ticks, 1)
        for _ in range(5):
            self.e.ex.on_ticker("SOL", -1, 0, 0, 0, 0, 0)
        self.assertIn("bad_data", self.e.ex.holds)

    def test_observe_no_write_calls(self):
        o = Env(observe=True)
        try:
            r = o.submit()
            self.assertTrue(r["ok"])
            o.tick(100.1)
            o.candle(100.3)
            o.tick(100.25)
            self.assertEqual(o.port.writes(), [])
            self.assertEqual(o.port.pos, {})
        finally:
            o.close()


class StopMonotonic(unittest.TestCase):
    """Защита от расширения стопа во всех слоях."""

    def setUp(self):
        self.e = Env()
        self.pid = self.e.enter()

    def tearDown(self):
        self.e.close()

    def sets(self):
        return [c for c in self.e.port.calls if c[0] == "set_stop"]

    def test_broken_rule_engine_cannot_widen(self):
        """Правило ошибочно предлагает более слабый стоп: Gate отказывает, set_stop порта не вызывается, событие error INVARIANT_VIOLATION."""
        bad = {"actions": [{"type": "move_stop", "to": 98.0, "mode": "price", "rule": "bad", "why": "RULE:bad"}], "rt_patch": {}, "events": [], "skipped": []}
        with mock.patch.object(rules, "evaluate_manage", return_value=bad):
            self.e.tick(100.5)
            self.e.timer()
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)
        errs = [x for x in self.e.bus("error") if x["d"].get("code") == "INVARIANT_VIOLATION"]
        self.assertTrue(errs)

    def test_actions_layer_independent_check(self):
        """Даже при обходе Gate (vd подложен) Actions сверяет со СВЕЖЕЙ позицией биржи и не зовёт порт."""
        p = self.e.ex.plans[self.pid]
        self.e.port.raw_set_stop("SOL", 99.5)            # кто-то подтянул вручную, кэш исполнителя устарел
        kind, info = self.e.ex.act.move_stop(p, 99.2, by="rule", vd=G.allow(cur=99.0, to=99.2))
        self.assertEqual(kind, "denied")
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.5)

    def test_port_layer_rejects(self):
        with self.assertRaises(PortError) as cm:
            self.e.port.set_stop("SOL", "long", 98.0)
        self.assertEqual(cm.exception.code, "STOP_NOT_TIGHTER")

    def test_amend_widen_rejected(self):
        r = self.e.command("amend", self.pid, {"stop": {"price": 98.0}}, rev_expected=self.e.spec(self.pid)["rev"])
        self.assertFalse(r["ok"])
        self.assertIn("E_STOP_WIDEN", [x["code"] for x in r["errors"]])
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)

    def test_cmd_stop_tighten_and_widen(self):
        rev = self.e.spec(self.pid)["rev"]
        r = self.e.command("stop", self.pid, {"to": 99.5}, rev_expected=rev)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.5)
        r = self.e.command("stop", self.pid, {"to": 99.0}, rev_expected=rev)
        self.assertFalse(r["ok"])
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.5)

    def test_gate_codes(self):
        p = self.e.ex.plans[self.pid]
        g = self.e.ex.gate
        self.assertEqual(g.stop(p, 98.0, by="rule").code, "INVARIANT_VIOLATION")
        self.assertEqual(g.stop(p, 99.0, by="rule").code, "NOT_TIGHTER")
        self.assertEqual(g.stop(p, 100.2, by="rule").code, "STOP_TOO_CLOSE")
        self.assertTrue(g.stop(p, 99.5, by="rule"))

    def test_tighter_helper(self):
        self.assertTrue(G.tighter("short", 99.0, 100.0))
        self.assertFalse(G.tighter("short", 101.0, 100.0))
        self.assertFalse(G.tighter("long", float("nan"), 100.0))

    def test_cancel_all_never_removes_stop_in_fake(self):
        self.e.port.cancel_all("SOL")
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)


if __name__ == "__main__":
    unittest.main()
