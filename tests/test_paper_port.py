"""PaperPort (WP7): контракт VenuePort + сценарии исполнения (вход, стоп, лесенка тейков, trailing, частичное закрытие, комиссии, детерминизм).

Все тесты без сети и без реального state/: цены подаются вручную, время даёт FakeClock.
"""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from fakes.clock import FakeClock
from test_port_contract import PortContract
from tb.executor import ports
from tb.executor.paper_port import PaperPort, load_costs
from tb.executor.ports import PortError

CFG0 = {"latency_ms": 0, "slip_bps": 2.0, "stop_slip_bps": 5.0, "through_ticks": 1, "taker_fee": 0.001, "maker_fee": 0.00036}


def make(equity=40.0, **over):
    cfg = dict(CFG0, **over)
    clk = FakeClock()
    p = PaperPort(equity, cfg, clock=clk.time)
    p.clk = clk
    return p


def tick(p, last, spread=0.1, adv=1.0, sym="SOL", funding=None):
    """Тик: сдвигает часы и подаёт цену с bid/ask вокруг last."""
    p.clk.advance(adv)
    return p.on_ticker(sym, last, last - spread / 2.0, last + spread / 2.0, last, funding, p.clk.time())


class TestPaperPortContract(PortContract, unittest.TestCase):
    def make_port(self):
        p = make()
        tick(p, 100.0, spread=0.1)
        return p

    def test_env_paper(self):
        self.assertEqual(self.port.env, ports.ENV_PAPER)


class TestEntryAndFees(unittest.TestCase):
    def test_market_entry_price_slippage_fee(self):
        p = make()
        tick(p, 100.0)                       # bid 99.95 ask 100.05
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        pos = p.positions()[0]
        self.assertAlmostEqual(pos["avg"], 100.05 * 1.0002, 6)
        ex = p.executions(0)[0]
        self.assertFalse(ex["is_maker"])
        self.assertAlmostEqual(ex["fee"], pos["avg"] * 1.0 * 0.001, 6)
        self.assertEqual(ex["link_id"], "tbx-1-1")

    def test_short_market_entry_sells_at_bid_minus_slip(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "short", 1.0, "tbx-1-1", "market", sl=105.0)
        self.assertAlmostEqual(p.positions()[0]["avg"], 99.95 * (1 - 0.0002), 6)

    def test_latency_delays_fill_until_tick(self):
        p = make(latency_ms=300)
        tick(p, 100.0)
        r = p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        self.assertEqual(r["status"], "New")
        self.assertEqual(p.positions(), [])
        tick(p, 100.2, adv=0.1)              # 0.1 c < 0.3 c: ещё рано
        self.assertEqual(p.positions(), [])
        tick(p, 100.4, adv=0.3)              # прошло 0.4 c: исполнено по ask ЭТОГО тика, не по цене сигнала
        pos = p.positions()[0]
        self.assertAlmostEqual(pos["avg"], 100.45 * 1.0002, 6)

    def test_entry_without_quote_is_network_error(self):
        p = make()
        with self.assertRaises(PortError) as cm:
            p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        self.assertEqual(cm.exception.kind, "network")

    def test_duplicate_link_rejected(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        with self.assertRaises(PortError) as cm:
            p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        self.assertEqual(cm.exception.code, "110072")

    def test_min_qty_and_step(self):
        p = make()
        tick(p, 100.0)
        with self.assertRaises(PortError) as cm:
            p.place_entry("SOL", "long", 0.05, "tbx-1-1", "market", sl=95.0)
        self.assertEqual(cm.exception.code, "MIN_QTY")
        p.place_entry("SOL", "long", 1.37, "tbx-1-2", "market", sl=95.0)
        self.assertEqual(p.positions()[0]["size"], 1.3)

    def test_insufficient_margin_rejected(self):
        p = make(equity=5.0)
        tick(p, 100.0)
        with self.assertRaises(PortError) as cm:
            p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)      # 100 / lev 10 = 10 > 5
        self.assertEqual(cm.exception.code, "110007")

    def test_leverage_affects_margin_and_liq(self):
        p = make()
        tick(p, 100.0)
        p.set_leverage("SOL", 20)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        pos = p.positions()[0]
        self.assertAlmostEqual(pos["margin"], pos["avg"] / 20.0, 6)
        self.assertLess(pos["liq"], pos["avg"])
        with self.assertRaises(PortError):
            p.set_leverage("SOL", 500)


class TestLimitFills(unittest.TestCase):
    def test_touch_without_through_does_not_fill(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=99.0, sl=95.0)
        tick(p, 99.0)                         # ровно касание уровня
        self.assertEqual(p.positions(), [])
        tick(p, 98.99)                        # прошла на 1 тик
        pos = p.positions()[0]
        self.assertEqual(pos["avg"], 99.0)    # цена лимита, без проскальзывания
        ex = p.executions(0)[0]
        self.assertTrue(ex["is_maker"])
        self.assertAlmostEqual(ex["fee"], 99.0 * 0.00036, 6)

    def test_limit_not_filled_by_stale_last_at_placement(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=99.97, sl=95.0)   # ниже ask, выше bid
        self.assertEqual(p.positions(), [])
        self.assertEqual(len(p.open_orders("SOL")), 1)

    def test_post_only_cross_is_cancelled_like_exchange(self):
        p = make()
        tick(p, 100.0)
        r = p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=100.5, post_only=True, sl=95.0)
        self.assertEqual(r["status"], "Cancelled")
        self.assertEqual(r["cancel_type"], "CancelByPostOnly")
        self.assertEqual(p.positions(), [])

    def test_marketable_limit_not_post_only_fills_as_taker_capped(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=100.06, post_only=False, sl=95.0)
        pos = p.positions()[0]
        self.assertAlmostEqual(pos["avg"], 100.06, 6)         # ask*(1+slip)=100.07 ограничен ценой лимита
        self.assertFalse(p.executions(0)[0]["is_maker"])

    def test_gap_through_limit_fills_at_limit_price_not_better(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=99.0, sl=90.0)
        tick(p, 95.0)                         # гэп вниз
        self.assertEqual(p.positions()[0]["avg"], 99.0)

    def test_amend_loses_queue_and_cancel(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "limit", price=99.0, sl=95.0)
        self.assertTrue(p.amend_order("SOL", "tbx-1-1", price=98.0)["ok"])
        self.assertEqual(p.amend_order("SOL", "tbx-1-1", price=98.0)["unchanged"], True)
        tick(p, 98.5)
        self.assertEqual(p.positions(), [])
        self.assertTrue(p.cancel_order("SOL", "tbx-1-1")["ok"])
        self.assertTrue(p.cancel_order("SOL", "tbx-1-1")["gone"])
        with self.assertRaises(PortError):
            p.amend_order("SOL", "tbx-1-1", price=97.0)


class TestStops(unittest.TestCase):
    def setUp(self):
        self.p = make()
        tick(self.p, 100.0)
        self.p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        self.avg = self.p.positions()[0]["avg"]

    def test_stop_hit_fills_at_stop_minus_slip_and_clears_position(self):
        events = []
        self.p.on_private = events.append
        tick(self.p, 95.0, spread=0.0)
        self.assertEqual(self.p.positions(), [])
        ex = self.p.executions(0)[-1]
        self.assertAlmostEqual(ex["price"], 95.0 * (1 - 0.0005), 6)
        self.assertEqual(ex["stop_type"], "StopLoss")
        self.assertEqual(ex["link_id"], "")
        self.assertTrue([e for e in events if e["kind"] == "execution" and e["stop_type"] == "StopLoss"])
        cp = self.p.closed_pnl("SOL", 0)[0]
        self.assertAlmostEqual(cp["pnl"], (ex["price"] - self.avg) - cp["open_fee"] - cp["close_fee"], 6)
        self.assertLess(cp["pnl"], 0)

    def test_gap_through_stop_uses_worse_of_stop_and_bid(self):
        tick(self.p, 90.0, spread=0.2)        # bid 89.9 хуже стопа 95
        ex = self.p.executions(0)[-1]
        self.assertAlmostEqual(ex["price"], 89.9 * (1 - 0.0005), 6)

    def test_stop_not_triggered_above(self):
        tick(self.p, 95.01, spread=0.0)
        self.assertEqual(len(self.p.positions()), 1)

    def test_short_stop_gap_uses_ask(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "short", 1.0, "tbx-2-1", "market", sl=105.0)
        tick(p, 110.0, spread=0.2)
        self.assertAlmostEqual(p.executions(0)[-1]["price"], 110.1 * (1 + 0.0005), 6)

    def test_mark_trigger_ignores_last(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0, sl_trigger="mark")
        p.clk.advance(1)
        p.on_ticker("SOL", 94.0, 93.95, 94.05, 99.0, None, p.clk.time())     # last ниже стопа, mark выше
        self.assertEqual(len(p.positions()), 1)
        p.clk.advance(1)
        p.on_ticker("SOL", 94.0, 93.95, 94.05, 94.5, None, p.clk.time())
        self.assertEqual(p.positions(), [])

    def test_stop_before_limit_on_same_tick(self):
        """Консервативный порядок: на одном тике стоп раньше тейка (тейк при этом не сработал бы, но ордер снимается закрытием)."""
        self.p.place_reduce_only("SOL", "sell", 1.0, "tbx-1-2", price=101.0)
        tick(self.p, 94.0, spread=0.0)
        self.assertEqual(self.p.positions(), [])
        o = self.p.order_by_link("SOL", "tbx-1-2")
        self.assertEqual(o["status"], "Cancelled")
        self.assertEqual(o["cancel_type"], "CancelByReduceOnly")

    def test_trailing_stop_moves_and_triggers_at_new_level(self):
        tick(self.p, 104.0)
        self.assertEqual(self.p.set_stop("SOL", "long", 101.0)["stop"], 101.0)
        tick(self.p, 106.0)
        self.p.set_stop("SOL", "long", 103.5)
        with self.assertRaises(PortError) as cm:
            self.p.set_stop("SOL", "long", 102.0)       # трейл только вверх
        self.assertEqual(cm.exception.code, "STOP_NOT_TIGHTER")
        tick(self.p, 103.4, spread=0.0)
        self.assertEqual(self.p.positions(), [])
        cp = self.p.closed_pnl("SOL", 0)[0]
        self.assertGreater(cp["pnl"], 0)                 # трейл зафиксировал плюс
        self.assertEqual(cp["stop_type"], "StopLoss")
        self.assertEqual(cp["best_px"], 106.0)

    def test_set_stop_other_side_and_mark_reference(self):
        with self.assertRaises(PortError):
            self.p.set_stop("SOL", "short", 101.0)
        with self.assertRaises(PortError) as cm:
            self.p.set_stop("NOPE", "long", 90.0)
        self.assertEqual(cm.exception.kind, "position_gone")

    def test_wrong_side_stop_on_entry_rejected(self):
        with self.assertRaises(PortError) as cm:
            self.p.place_entry("SOL", "short", 1.0, "tbx-9-1", "market", sl=90.0)
        self.assertIn(cm.exception.code, ("SIDE_MISMATCH", "STOP_ON_WRONG_SIDE"))


class TestLadderAndPartial(unittest.TestCase):
    def setUp(self):
        self.p = make()
        tick(self.p, 100.0)
        self.p.place_entry("SOL", "long", 2.0, "tbx-1-1", "market", sl=95.0)
        self.p.place_reduce_only("SOL", "sell", 1.0, "tbx-1-2", price=103.0, post_only=True)   # t1
        self.p.place_reduce_only("SOL", "sell", 1.0, "tbx-1-3", price=106.0, post_only=True)   # t2

    def test_ladder_t1_fills_after_through_then_t2_closes(self):
        tick(self.p, 103.0)
        self.assertEqual(self.p.positions()[0]["size"], 2.0)           # касание без прохода
        tick(self.p, 103.02)
        self.assertEqual(self.p.positions()[0]["size"], 1.0)
        ex = [e for e in self.p.executions(0) if e["link_id"] == "tbx-1-2"][0]
        self.assertTrue(ex["is_maker"])
        self.assertEqual(ex["price"], 103.0)
        self.assertAlmostEqual(ex["fee"], 103.0 * 0.00036, 6)
        self.assertEqual(ex["closed_size"], 1.0)
        tick(self.p, 106.5)
        self.assertEqual(self.p.positions(), [])
        parts = self.p.closed_pnl("SOL", 0)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(sum(c["open_fee"] for c in parts), self.p.executions(0)[0]["fee"], 6)

    def test_equity_equals_sum_of_closed_pnl_after_flat(self):
        tick(self.p, 103.02)
        tick(self.p, 106.5)
        acc = self.p.account()
        self.assertAlmostEqual(acc["equity"], 40.0 + sum(c["pnl"] for c in self.p.closed_pnl("SOL", 0)), 6)
        self.assertAlmostEqual(acc["free"], acc["equity"], 6)

    def test_partial_close_market_then_stop_clears_remaining_orders(self):
        self.p.close_market("SOL", "sell", 1.0, "tbx-1-4")
        self.assertEqual(self.p.positions()[0]["size"], 1.0)
        tick(self.p, 94.0, spread=0.0)
        self.assertEqual(self.p.positions(), [])
        self.assertEqual(self.p.open_orders("SOL"), [])

    def test_reduce_only_larger_than_position_is_trimmed(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        p.place_reduce_only("SOL", "sell", 5.0, "tbx-1-2")        # market, больше позиции
        self.assertEqual(p.positions(), [])
        self.assertEqual(p.executions(0)[-1]["qty"], 1.0)

    def test_reduce_only_wrong_side_and_no_position(self):
        with self.assertRaises(PortError):
            self.p.place_reduce_only("SOL", "buy", 1.0, "tbx-1-9")
        with self.assertRaises(PortError) as cm:
            self.p.place_reduce_only("ETH", "sell", 1.0, "tbx-1-8")
        self.assertEqual(cm.exception.kind, "position_gone")

    def test_cancel_all_keeps_stop(self):
        r = self.p.cancel_all("SOL", reduce_only_only=True)
        self.assertEqual(r["cancelled"], 2)
        self.assertEqual(self.p.positions()[0]["sl"], 95.0)


class TestFundingAndAccount(unittest.TestCase):
    def test_funding_charged_once_per_mark_long_pays(self):
        p = make()
        p.clk.t = 8 * 3600.0 - 10      # за 10 с до метки 08:00 UTC
        tick(p, 100.0, funding=0.0001, adv=0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=90.0)
        eq0 = p.account()["equity"]
        tick(p, 100.0, funding=0.0001, adv=5)            # метку не пересекли
        self.assertAlmostEqual(p.account()["equity"], eq0, 6)
        tick(p, 100.0, funding=0.0001, adv=10)           # пересекли 08:00
        self.assertAlmostEqual(p.account()["equity"], eq0 - 100.0 * 0.0001, 6)
        tick(p, 100.0, funding=0.0001, adv=10)           # второй раз не списывается
        self.assertAlmostEqual(p.account()["equity"], eq0 - 100.0 * 0.0001, 6)
        p.close_market("SOL", "sell", None, "tbx-1-2")
        self.assertAlmostEqual(p.closed_pnl("SOL", 0)[0]["funding"], -0.01, 6)

    def test_old_tick_ignored(self):
        p = make()
        tick(p, 100.0)
        t = p.clk.time()
        p.on_ticker("SOL", 50.0, 49.9, 50.1, 50.0, None, t - 100)
        self.assertEqual(p.quote("SOL")[2], 100.0)

    def test_liquidation_before_far_stop(self):
        p = make()
        tick(p, 100.0)
        p.set_leverage("SOL", 20)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=50.0)
        liq = p.positions()[0]["liq"]
        tick(p, liq - 0.5, spread=0.0)
        self.assertEqual(p.positions(), [])
        self.assertEqual(p.closed_pnl("SOL", 0)[0]["stop_type"], "Liquidation")


class TestDeterminismAndPersistence(unittest.TestCase):
    PRICES = [100.0, 100.4, 101.2, 99.8, 98.9, 99.5, 103.1, 104.0, 94.0, 97.0]

    def run_scenario(self):
        p = make()
        tick(p, 100.0)
        p.place_entry("SOL", "long", 2.0, "tbx-1-1", "market", sl=95.0)
        p.place_reduce_only("SOL", "sell", 1.0, "tbx-1-2", price=103.0)
        for px in self.PRICES:
            tick(p, px)
        return json.dumps([p.executions(0), p.closed_pnl("SOL", 0), p.account()["equity"]], sort_keys=True)

    def test_same_ticks_same_result(self):
        self.assertEqual(self.run_scenario(), self.run_scenario())

    def test_save_load_roundtrip(self):
        d = tempfile.mkdtemp(prefix="tbpaper_")
        try:
            p = make()
            tick(p, 100.0)
            p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
            f = os.path.join(d, "paper.json")
            p.save(f)
            q = make()
            self.assertTrue(q.load(f))
            self.assertEqual(q.positions()[0]["avg"], p.positions()[0]["avg"])
            self.assertEqual(q.account()["equity"], p.account()["equity"])
            self.assertFalse(make().load(os.path.join(d, "none.json")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_load_costs_from_project_config(self):
        c = load_costs()
        self.assertEqual(c["taker_fee"], 0.001)
        self.assertEqual(c["maker_fee"], 0.00036)
        self.assertAlmostEqual(c["slip_bps"], 2.0)
        self.assertEqual(c["latency_ms"], 300)

    def test_load_costs_missing_root_defaults(self):
        d = tempfile.mkdtemp(prefix="tbpaper_")
        try:
            self.assertEqual(load_costs(d).get("taker_fee"), None)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPrivateEventsAndNoNetwork(unittest.TestCase):
    def test_events_have_link_id_and_kinds(self):
        p = make()
        evs = []
        p.on_private = evs.append
        tick(p, 100.0)
        p.place_entry("SOL", "long", 1.0, "tbx-1-1", "market", sl=95.0)
        kinds = [e["kind"] for e in evs]
        self.assertIn("order", kinds)
        self.assertIn("execution", kinds)
        self.assertIn("position", kinds)
        self.assertTrue(all("link_id" in e for e in evs if e["kind"] in ("order", "execution")))

    def test_klines_feed_and_sorted(self):
        p = make()
        p.on_kline("SOL", 5, 2000, 1, 2, 0.5, 1.5, 10, 10, True)
        p.on_kline("SOL", 5, 1000, 1, 2, 0.5, 1.5, 10, 10, True)    # старше последней: игнор
        p.on_kline("SOL", 5, 2000, 1, 3, 0.5, 2.5, 10, 10, False)   # обновление формирующейся
        rows = p.klines("SOL", 5, 10)
        self.assertEqual(rows, [(2000, 1, 3, 0.5, 2.5, 10, 10)])


if __name__ == "__main__":
    unittest.main()
