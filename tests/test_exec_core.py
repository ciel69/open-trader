import unittest

import _bootstrap  # noqa: F401
from fakes.harness import Env, plan_long


class CoreCycle(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_submit_armed_and_preview(self):
        r = self.e.submit()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "armed")
        self.assertIn("leverage", r["guard_preview"])
        self.assertEqual(self.e.rt(r["plan_id"])["status"], "armed")
        self.assertTrue(self.e.bus("plan_armed"))

    def test_entry_ladder_managing(self):
        pid = self.e.enter()
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "managing", rt)
        self.assertGreater(rt["pos"]["qty"], 0)
        self.assertEqual(rt["pos"]["cur_stop"], 99.0)
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)
        self.assertTrue(rt["pos"]["journal_id"])
        tg = rt["targets"]
        self.assertEqual(set(t["state"] for t in tg.values()), {"resting"})
        self.assertTrue(self.e.bus("entry_filled"))
        tr = self.e.trades()
        self.assertEqual(tr[0]["plan_id"], pid)
        self.assertIn("exp:t-exp", tr[0]["tags"])
        # намерение записано в журнал решений ДО результата
        kinds = [r["kind"] for r in self.e.decisions(what="place_entry")]
        self.assertEqual(kinds[:2], ["intent", "result"])
        self.assertEqual(rt["actions"]["pending"], [])

    def test_be_then_target_then_stop_close(self):
        pid = self.e.enter()
        self.e.tick(101.5)       # +1.5R
        rt = self.e.rt(pid)
        self.assertGreater(self.e.port.pos["SOL"]["sl"], 100.0)     # безубыток
        self.assertTrue(self.e.bus("stop_moved"))
        self.e.tick(102.6)       # t1 исполнен лимитом
        rt = self.e.rt(pid)
        self.assertEqual(rt["targets"]["t1"]["state"], "filled")
        self.assertEqual(rt["status"], "managing")
        self.e.tick(self.e.port.pos["SOL"]["sl"] - 0.01)     # стоп
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "closed", rt)
        self.assertEqual(rt["close"]["reason"], "sl")
        t = self.e.trades()[0]
        self.assertTrue(t.get("ts_close"))
        self.assertEqual(t["reason"], "sl")
        self.assertTrue(self.e.bus("position_closed"))
        # мартингейл-ловушка: в плане нет позиции и ордеров
        self.assertNotIn("SOL", self.e.port.pos)

    def test_close_reason_uses_latest_fill_not_last_written(self):
        """Сверка дописывает ранний фрагмент (сработавший тейк) уже после стопа: причиной остаётся стоп, а не manual (случай HAEDAL 2026-10-29)."""
        p = {"rt": {"pos": {"cur_stop": 0.02575, "initial_stop": 0.02575}, "close": {}, "targets": {"t1": {"link": "tbx-3-2"}}}, "spec": {"side": "long"}}
        stop_fill = {"ts": 1793261224.6, "px": 0.025687, "qty": 670.0, "stop_type": "StopLoss", "link": "", "exec_type": "Trade"}
        tp_fill = {"ts": 1793260914.6, "px": 0.02641, "qty": 660.0, "stop_type": "", "link": "", "exec_type": "closed_pnl"}
        avg = (0.025687 * 670.0 + 0.02641 * 660.0) / 1330.0                    # средняя цена выхода выше стопа
        ex = self.e.ex
        self.assertEqual(ex._close_reason(p, [stop_fill, tp_fill], avg), "sl")     # порядок записи: стоп первым, тейк (из сверки) последним
        self.assertEqual(ex._close_reason(p, [tp_fill, stop_fill], avg), "sl")
        no_type = dict(stop_fill, stop_type="")
        self.assertEqual(ex._close_reason(p, [no_type, tp_fill], avg), "sl")       # без stop_type: цена последнего фрагмента у стопа
        self.assertEqual(ex._close_reason(p, [tp_fill], 0.02641), "manual")        # единственный фрагмент вдали от стопа и без ссылки на цель: ручное

    def test_tp_both_targets(self):
        pid = self.e.enter()
        self.e.tick(104.1)
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "closed")
        self.assertEqual(rt["close"]["reason"], "tp")
        self.assertGreater(rt["close"]["pnl"], 0)

    def test_idempotent_command_redelivery(self):
        cmd = {"actor": "brain:chat", "op": "submit", "payload": plan_long(), "reason": "t", "cmd_id": "C-20260101T000000-aaaaaa"}
        self.e.ex.store.inbox_put(cmd)
        self.e.ex.poll_inbox()
        self.e.ex.store.inbox_put(cmd)
        self.e.ex.poll_inbox()
        self.assertEqual(len(self.e.ex.store.list()), 1)

    def test_stale_price_blocks_entry(self):
        self.e.submit()
        self.e.tick(100.1)
        self.e.clock.advance(10)
        self.e.candle(100.3)
        self.e.ex.eval_plan(list(self.e.ex.plans.values())[0], force=True)
        self.assertEqual(self.e.port.pos, {})

    def test_cancel_armed(self):
        r = self.e.submit()
        c = self.e.command("cancel", r["plan_id"])
        self.assertTrue(c["ok"])
        self.assertEqual(self.e.rt(r["plan_id"])["status"], "cancelled")

    def test_ttl_expires(self):
        r = self.e.submit()
        self.e.timer(1, dt=3700)
        self.assertEqual(self.e.rt(r["plan_id"])["status"], "expired")

    def test_invalidation_cancel_below(self):
        r = self.e.submit()
        self.e.tick(94.0)
        self.assertEqual(self.e.rt(r["plan_id"])["status"], "invalidated")


if __name__ == "__main__":
    unittest.main()
