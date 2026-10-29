"""LivePort (executor/live_port.py, WP1A): общий контрактный тест VenuePort на состоящей из памяти бирже + классификация ошибок записи,
кэш инструмента, отказ входа без стопа без обращения к бирже, перепроверка position_gone."""
import unittest

import _bootstrap  # noqa: F401
from test_bybit_exec2 import FakeExchange, T0, err, mk
from test_port_contract import PortContract
from tb.executor import ports
from tb.executor.live_port import LivePort
from tb.executor.ports import PortError, VenuePort
from tb.venues import bybit as bb


def make(**kw):
    c, ex = mk(**kw)
    return LivePort(c, clock=lambda: T0), ex


class TestLivePortContract(PortContract, unittest.TestCase):
    def make_port(self):
        p, self.ex = make()
        return p


class LivePortTests(unittest.TestCase):
    def setUp(self):
        self.port, self.ex = make()

    def test_is_venue_port_and_env(self):
        self.assertIsInstance(self.port, VenuePort)
        self.assertEqual(self.port.env, "testnet")
        self.assertIn(self.port.env, ports.ENVS)

    def test_entry_without_stop_makes_no_exchange_calls(self):
        for sl in (None, 0, 0.0, -1):
            with self.assertRaises(PortError) as cm:
                self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=sl)
            self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", "NO_STOP"))
        self.assertEqual(self.ex.calls, [])

    def test_entry_has_embedded_stop_in_single_order(self):
        r = self.port.place_entry("SOL", "short", 1.5, "tbx-1-1", "market", sl=105, sl_trigger="mark")
        self.assertTrue(r["ok"])
        w = self.ex.writes()
        self.assertEqual([p for p, _ in w], ["/v5/order/create"])
        self.assertEqual((w[0][1]["stopLoss"], w[0][1]["slTriggerBy"], w[0][1]["tpslMode"]), ("105", "MarkPrice", "Full"))
        self.assertEqual(self.port.positions()[0]["sl"], 105.0)

    def test_instrument_cache_one_hour(self):
        t = [T0]
        c, ex = mk()
        port = LivePort(c, clock=lambda: t[0])
        a = port.instrument("SOL")
        port.instrument("SOL")
        port.quote("SOL")
        n = lambda: len([1 for _, p, _, _ in ex.calls if p == "/v5/market/instruments-info"])
        self.assertEqual(n(), 1)
        self.assertEqual((a["tick"], a["qty_step"], a["min_qty"], a["max_leverage"]), ("0.01", "0.1", "0.1", 50.0))
        t[0] += 3601
        port.instrument("SOL")
        self.assertEqual(n(), 2)

    def test_write_timeout_is_unknown(self):
        self.ex.push("/v5/order/create", TimeoutError("timed out"))
        with self.assertRaises(PortError) as cm:
            self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        self.assertEqual(cm.exception.kind, "unknown")

    def test_read_timeout_is_network(self):
        self.ex.push("/v5/account/wallet-balance", TimeoutError("timed out"))
        with self.assertRaises(PortError) as cm:
            self.port.account()
        self.assertEqual(cm.exception.kind, "network")

    def test_rejected_carries_retcode(self):
        self.ex.push("/v5/order/create", err(110007, "ab not enough for new order"))
        with self.assertRaises(PortError) as cm:
            self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", 110007))

    def test_rate_limit(self):
        for code in (10006, 10018):
            self.ex.push("/v5/order/create", err(code, "Too many visits"))
            with self.assertRaises(PortError) as cm:
                self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
            self.assertEqual((cm.exception.kind, cm.exception.code), ("rate", code))

    def test_duplicate_link_is_rejected_110072(self):
        self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        with self.assertRaises(PortError) as cm:
            self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", 110072))

    def test_reduce_only_without_position_confirmed_gone(self):
        with self.assertRaises(PortError) as cm:
            self.port.place_reduce_only("SOL", "sell", 1, "tbx-1-2")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("position_gone", 110017))

    def test_reduce_only_110017_with_live_position_stays_rejected(self):
        self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        self.ex.push("/v5/order/create", err(110017, "Reduce-only rule not satisfied"))
        with self.assertRaises(PortError) as cm:
            self.port.place_reduce_only("SOL", "sell", 5, "tbx-1-2")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("rejected", 110017))

    def test_set_stop_after_position_closed_is_position_gone(self):
        with self.assertRaises(PortError) as cm:
            self.port.set_stop("SOL", "long", 96)
        self.assertEqual(cm.exception.kind, "position_gone")

    def test_set_stop_never_writes_when_not_tighter(self):
        self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        before = len(self.ex.writes())
        for v in (94, 94.99):
            with self.assertRaises(PortError) as cm:
                self.port.set_stop("SOL", "long", v)
            self.assertEqual(cm.exception.code, "STOP_NOT_TIGHTER")
        self.assertEqual(len(self.ex.writes()), before)

    def test_set_leverage_not_modified_is_ok(self):
        self.ex.lev_same = True
        self.assertTrue(self.port.set_leverage("SOL", 10)["ok"])

    def test_tp_ladder_and_stop_coexist_and_cancel_keeps_stop(self):
        self.port.place_entry("SOL", "long", 2, "tbx-1-1", "market", sl=95)
        self.port.place_reduce_only("SOL", "sell", 1, "tbx-1-2", price=110, post_only=True)
        orders = self.port.open_orders("SOL")
        self.assertEqual([(o["link_id"], o["reduce_only"], o["side"]) for o in orders], [("tbx-1-2", True, "Sell")])
        self.assertEqual(self.port.positions()[0]["sl"], 95.0)
        self.port.amend_order("SOL", "tbx-1-2", price=111.234)
        self.assertEqual(self.port.order_by_link("SOL", "tbx-1-2")["price"], 111.23)
        self.port.cancel_all("SOL", reduce_only_only=True)
        self.assertEqual(self.port.open_orders("SOL"), [])
        self.assertEqual(self.port.positions()[0]["sl"], 95.0)

    def test_executions_and_closed_pnl_via_port(self):
        self.port.place_entry("SOL", "long", 1, "tbx-1-1", "market", sl=95)
        ex = self.port.executions(0)
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["link_id"], "tbx-1-1")
        self.assertEqual(self.port.closed_pnl("SOL", 0), [])

    def test_all_writes_in_allowlist(self):
        self.port.place_entry("SOL", "long", 2, "tbx-1-1", "market", sl=95)
        self.port.set_stop("SOL", "long", 96)
        self.port.place_reduce_only("SOL", "sell", 1, "tbx-1-2", price=110)
        self.port.amend_order("SOL", "tbx-1-2", qty=0.5)
        self.port.cancel_order("SOL", "tbx-1-2")
        self.port.set_leverage("SOL", 5)
        self.port.cancel_all("SOL")
        self.port.close_market("SOL", "sell", None, "tbx-1-3")
        self.assertTrue(set(self.ex.paths()) <= bb.ALLOWED)

    def test_no_stop_removal_api_surface(self):
        self.assertFalse(hasattr(LivePort, "remove_stop"))
        self.assertFalse(hasattr(LivePort, "clear_stop"))


if __name__ == "__main__":
    unittest.main()
