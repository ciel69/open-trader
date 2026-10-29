"""Контрактный тест VenuePort (06 п. 6.2): инварианты, которые обязан соблюдать ЛЮБОЙ порт (FakePort, LivePort на фейковом транспорте, PaperPort).

Как подключить свою реализацию (WP1: LivePort, WP7: PaperPort):

    class TestLivePortContract(PortContract, unittest.TestCase):
        def make_port(self):
            return LivePort(fake_client_with_sol_at_100)

`make_port()` возвращает порт с символом SOL по цене 100 (bid 99.95 / ask 100.05) без позиций.
Здесь подключён только FakePort (заготовка WP0; WP5 расширяет).
"""
import unittest

import _bootstrap  # noqa: F401
from fakes.fake_port import FakePort
from tb.executor import ports
from tb.executor.ports import PortError, VenuePort


class PortContract(object):
    SYM = "SOL"

    def make_port(self):
        raise NotImplementedError

    def setUp(self):
        self.port = self.make_port()

    def open_long(self, link="tbx-1-1", sl=95.0, qty=1.0):
        return self.port.place_entry(self.SYM, "long", qty, link, "market", sl=sl)

    # ---- вход без стопа запрещён на уровне порта ----
    def test_entry_without_stop_rejected(self):
        for sl in (None, 0, 0.0):
            with self.assertRaises(PortError) as cm:
                self.port.place_entry(self.SYM, "long", 1.0, "tbx-1-9", "market", sl=sl)
            self.assertEqual(cm.exception.kind, "rejected")
            self.assertEqual(cm.exception.code, "NO_STOP")
        self.assertEqual(self.port.positions(), [])

    # ---- стоп только строже ----
    def test_stop_tighten_ok_and_unchanged(self):
        self.open_long()
        r = self.port.set_stop(self.SYM, "long", 96.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["stop"], 96.0)
        self.assertFalse(r["unchanged"])
        r = self.port.set_stop(self.SYM, "long", 96.0)
        self.assertTrue(r["unchanged"])
        self.assertEqual([p for p in self.port.positions() if p["sym"] == self.SYM][0]["sl"], 96.0)

    def test_stop_loosen_rejected_without_change(self):
        self.open_long()
        self.port.set_stop(self.SYM, "long", 96.0)
        with self.assertRaises(PortError) as cm:
            self.port.set_stop(self.SYM, "long", 94.0)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", "STOP_NOT_TIGHTER"))
        self.assertEqual([p for p in self.port.positions() if p["sym"] == self.SYM][0]["sl"], 96.0)

    def test_stop_remove_forbidden(self):
        self.open_long()
        for v in (0, 0.0, None):
            with self.assertRaises(PortError):
                self.port.set_stop(self.SYM, "long", v)
        self.assertEqual([p for p in self.port.positions() if p["sym"] == self.SYM][0]["sl"], 95.0)

    def test_stop_wrong_side_of_price_rejected(self):
        self.open_long()
        with self.assertRaises(PortError) as cm:
            self.port.set_stop(self.SYM, "long", 101.0)   # выше last: сработал бы мгновенно
        self.assertEqual(cm.exception.code, "STOP_ON_WRONG_SIDE")

    # ---- форма данных ----
    def test_read_shapes(self):
        a = self.port.account()
        for k in ("equity", "free", "ts"):
            self.assertIn(k, a)
        bid, ask, last = self.port.quote(self.SYM)
        self.assertLessEqual(bid, last)
        self.assertGreaterEqual(ask, last)
        ins = self.port.instrument(self.SYM)
        for k in ("tick", "qty_step", "min_qty", "min_notional", "max_leverage"):
            self.assertIn(k, ins)
        self.open_long()
        p = self.port.positions()[0]
        for k in ("sym", "side", "size", "avg", "mark", "sl", "tp", "liq", "upnl", "lev", "margin", "ts"):
            self.assertIn(k, p)
        self.assertIn(p["side"], ("long", "short"))
        kl = self.port.klines(self.SYM, 5, 10)
        self.assertEqual([r[0] for r in kl], sorted(r[0] for r in kl))

    def test_close_market_reduces_position(self):
        self.open_long(qty=2.0)
        self.port.close_market(self.SYM, "sell", 1.0, "tbx-1-2")
        self.assertEqual(self.port.positions()[0]["size"], 1.0)
        self.port.close_market(self.SYM, "sell", None, "tbx-1-3")
        self.assertEqual(self.port.positions(), [])

    def test_close_without_position_is_position_gone(self):
        with self.assertRaises(PortError) as cm:
            self.port.close_market(self.SYM, "sell", None, "tbx-1-4")
        self.assertEqual(cm.exception.kind, "position_gone")

    def test_cancel_by_link(self):
        self.port.place_entry(self.SYM, "long", 1.0, "tbx-1-5", "limit", price=90.0, sl=85.0)
        self.assertTrue(self.port.order_by_link(self.SYM, "tbx-1-5"))
        self.port.cancel_order(self.SYM, "tbx-1-5")
        self.assertFalse([o for o in self.port.open_orders(self.SYM) if o["link_id"] == "tbx-1-5"])

    def test_set_leverage_idempotent(self):
        self.port.set_leverage(self.SYM, 10)
        self.port.set_leverage(self.SYM, 10)

    def test_env_is_known(self):
        self.assertIn(self.port.env, ports.ENVS)


class TestFakePortContract(PortContract, unittest.TestCase):
    def make_port(self):
        p = FakePort()
        p.set_price("SOL", 100.0, spread=0.1)
        return p


class TestBaseClasses(unittest.TestCase):
    def test_base_port_methods_not_implemented(self):
        base = VenuePort()
        calls = {
            "account": (), "positions": (), "open_orders": (), "order_by_link": ("S", "l"), "executions": (0,), "closed_pnl": ("S", 0),
            "instrument": ("S",), "quote": ("S",), "klines": ("S", 5), "set_leverage": ("S", 5),
            "place_entry": ("S", "long", 1, "l", "market"), "place_reduce_only": ("S", "sell", 1, "l"), "amend_order": ("S", "l"),
            "cancel_order": ("S", "l"), "cancel_all": ("S",), "set_stop": ("S", "long", 1.0), "close_market": ("S", "sell", 1, "l"),
        }
        for name, args in calls.items():
            with self.assertRaises(NotImplementedError, msg=name):
                getattr(base, name)(*args)

    def test_script_error_propagates(self):
        p = FakePort()
        p.script("account", PortError("unknown", "таймаут"))
        with self.assertRaises(PortError) as cm:
            p.account()
        self.assertEqual(cm.exception.kind, "unknown")
        self.assertEqual(p.account()["equity"], 40.0)   # сценарий одноразовый


if __name__ == "__main__":
    unittest.main()
