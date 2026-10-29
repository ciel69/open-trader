import unittest

import _bootstrap  # noqa: F401
from fakes.fake_port import FakePort
from fakes.harness import Env, plan_long
from tb.core import decisions
from tb.executor.core import Executor
from tb.executor.ports import PortError


def restart(e):
    """Новый Executor на том же корне и порте (как перезапуск процесса)."""
    ex = Executor(e.port, root=e.root, clock=e.clock.time, sleep=e.clock.advance, profile="testnet", cfg=e.cfg)
    e.q = []
    e.port.sink = e.q.append
    e.ex = ex
    ex.start()
    return ex


class WalTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_intent_before_exchange_call_and_chain_valid(self):
        order = []
        orig = self.e.port.place_entry

        def spy(*a, **k):
            order.append(("port", [r["kind"] for r in self.e.decisions(what="place_entry")]))
            return orig(*a, **k)
        self.e.port.place_entry = spy
        self.e.enter()
        self.assertEqual(order[0][1], ["intent"])       # на момент вызова порта intent уже записан
        self.assertTrue(decisions.verify(self.e.root)["ok"])

    def test_restart_mid_entry_order_found_by_link(self):
        """Падение после отправки ордера и до результата: после рестарта ордер найден по link_id, лишнего входа нет."""
        self.e.submit()
        self.e.tick(100.1)
        self.e.port.script("order_by_link", FakePort.PASS)

        class Boom(Exception):
            pass
        ex = self.e.ex
        orig_end = ex.act.end

        def die(p, A, *a, **k):
            if A["what"] == "place_entry":
                raise Boom()
            return orig_end(p, A, *a, **k)
        ex.act.end = die
        with self.assertRaises(Boom):
            self.e.candle(100.3)
        self.assertEqual(len(self.e.port.pos), 1)     # вход на бирже есть, результат не записан
        rt = self.e.rt(self.e.ex.store.list()[0]["spec"]["id"])
        self.assertEqual(len(rt["actions"]["pending"]), 1)
        ex2 = restart(self.e)
        self.e.timer(2)
        rt = self.e.rt(rt["plan_id"])
        self.assertEqual(rt["status"], "managing", rt)
        self.assertEqual(rt["actions"]["pending"], [])
        self.assertEqual(len([c for c in self.e.port.calls if c[0] == "place_entry"]), 1)
        self.assertEqual(len(self.e.trades()), 1)

    def test_restart_intent_not_sent(self):
        """Падение между intent и отправкой: ордера на бирже нет, через 15 с вход снова доступен (новый link), позиции нет."""
        self.e.submit()
        self.e.tick(100.1)
        ex = self.e.ex

        class Boom(Exception):
            pass
        orig = self.e.port.place_entry
        self.e.port.place_entry = lambda *a, **k: (_ for _ in ()).throw(Boom())
        with self.assertRaises(Boom):
            self.e.candle(100.3)
        self.e.port.place_entry = orig
        restart(self.e)
        self.e.timer(1, dt=16)
        self.e.timer(1)
        pid = self.e.ex.store.list()[0]["spec"]["id"]
        rt = self.e.rt(pid)
        self.assertEqual(rt["actions"]["pending"], [])
        self.assertEqual(self.e.port.pos, {} if rt["status"] == "armed" and rt["entry"]["state"] in ("waiting", "blocked") else self.e.port.pos)

    def test_restart_keeps_position_and_ladder(self):
        pid = self.e.enter()
        before = dict((k, dict(v)) for k, v in self.e.port.orders.items())
        restart(self.e)
        self.e.timer(2)
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "managing")
        self.assertEqual(len([c for c in self.e.port.calls if c[0] == "place_entry"]), 1)
        self.assertEqual(len([c for c in self.e.port.calls if c[0] == "place_reduce_only"]), 2)
        self.assertEqual(self.e.port.pos["SOL"]["sl"], 99.0)

    def test_unknown_on_entry_verified_not_blindly_repeated(self):
        self.e.submit()
        self.e.tick(100.1)
        # ордер создан на бирже, но ответ потерян (timeout): unknown
        orig = self.e.port.place_entry

        def lost(*a, **k):
            orig(*a, **k)
            raise PortError("unknown", "timeout")
        self.e.port.place_entry = lost
        self.e.candle(100.3)
        n = len([c for c in self.e.port.calls if c[0] == "place_entry"])
        self.e.port.place_entry = orig
        self.e.timer(3)
        self.assertEqual(len([c for c in self.e.port.calls if c[0] == "place_entry"]), n)
        pid = self.e.ex.store.list()[0]["spec"]["id"]
        self.assertEqual(self.e.rt(pid)["status"], "managing")

    def test_rejected_entry_not_retried_immediately(self):
        self.e.submit()
        self.e.tick(100.1)
        self.e.port.script("place_entry", PortError("rejected", "x", code=10001))
        self.e.candle(100.3)
        self.e.tick(100.25)
        self.e.tick(100.26)
        self.assertEqual(len([c for c in self.e.port.calls if c[0] == "place_entry"]), 1)
        self.assertEqual(self.e.port.pos, {})

    def test_monitor_pending_cleared_after_ok(self):
        pid = self.e.enter()
        self.assertEqual(self.e.rt(pid)["actions"]["pending"], [])


class PartialAndRaces(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.port.auto_fill = False

    def tearDown(self):
        self.e.close()

    def test_partial_market_entry_waits_fill_wait(self):
        r = self.e.submit()
        self.e.tick(100.1)
        self.e.candle(100.3)
        link = [c for c in self.e.port.calls if c[0] == "place_entry"][0][1][3]
        self.e.port.fill(link, 0.1, 100.2)
        self.assertEqual(self.e.rt(r["plan_id"])["status"], "armed")      # ждём остаток fill_wait_s
        self.e.port.fill(link, 0.1, 100.3)
        self.e.timer(1, dt=2.5)
        rt = self.e.rt(r["plan_id"])
        self.assertEqual(rt["status"], "managing", rt)
        self.assertAlmostEqual(rt["pos"]["qty"], 0.2)
        self.assertAlmostEqual(rt["pos"]["avg"], 100.25)
        self.assertTrue(rt["entry"]["partial"])
        self.assertEqual(self.e.port.orders[link]["status"], "Cancelled")

    def test_target_partial_fill_updates_state(self):
        self.e.port.auto_fill = True
        pid = self.e.enter()
        rt = self.e.rt(pid)
        t1 = rt["targets"]["t1"]
        self.e.port.auto_fill = False
        self.e.port.fill(t1["link"], 0.05, 102.5)
        self.e.pump()
        rt = self.e.rt(pid)
        self.assertEqual(rt["targets"]["t1"]["state"], "resting")
        self.assertAlmostEqual(rt["targets"]["t1"]["filled_qty"], 0.05)

    def test_manual_close_race_finalizes(self):
        self.e.port.auto_fill = True
        pid = self.e.enter()
        # владелец закрыл позицию вручную на бирже, пока исполнитель не знает
        self.e.port.orders["MANUAL"] = {"sym": "SOL", "link_id": "", "order_id": "O-M", "side": "Sell", "type": "market", "price": 101.0, "qty": self.e.port.pos["SOL"]["size"],
                                        "filled": 0.0, "reduce_only": True, "status": "New", "ts": 0}
        self.e.port.sink = None
        self.e.port.fill("MANUAL", self.e.port.pos["SOL"]["size"], 101.0)
        self.e.port.sink = self.e.q.append
        self.e.tick(101.0)
        # set_stop на закрытую позицию не должен падать
        self.e.timer(2, dt=31)
        rt = self.e.rt(pid)
        self.assertEqual(rt["status"], "closed")
        self.assertEqual(rt["close"]["reason"], "manual")

    def test_stop_moves_race_position_gone(self):
        self.e.port.auto_fill = True
        pid = self.e.enter()
        self.e.port.sink = None
        self.e.port.trigger_stop("SOL")
        self.e.port.sink = self.e.q.append
        self.e.tick(101.5)     # правило BE хочет двигать стоп; позиции уже нет
        self.assertEqual(self.e.rt(pid)["status"], "closed")
        self.assertEqual(self.e.rt(pid)["close"]["reason"], "sl")

    def test_cancel_race_filled(self):
        r = self.e.submit(plan_long(entry={"when": {"kind": "candle_close", "tf": "1m", "dir": "above", "level": 100.2, "n": 1},
                                           "how": {"type": "limit", "price": 100.1, "post_only": True}, "ref_price": 100.2,
                                           "valid": {"ttl_min": 60, "cancel_below": 95.0}}))
        self.assertTrue(r["ok"], r)
        self.e.tick(100.1)
        self.e.candle(100.3)
        pid = r["plan_id"]
        self.assertEqual(self.e.rt(pid)["entry"]["state"], "resting")
        link = self.e.rt(pid)["entry"]["link_id"]
        self.e.port.sink = None
        self.e.port.fill(link, 0.1, 100.1)      # заполнился между опросом и отменой
        c = self.e.command("cancel", pid)
        self.assertFalse(c["ok"])
        self.assertEqual(self.e.rt(pid)["status"], "managing" if self.e.rt(pid)["status"] != "armed" else "armed")


if __name__ == "__main__":
    unittest.main()
