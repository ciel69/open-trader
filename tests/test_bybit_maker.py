import argparse
import json
import os
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
from tb.venues import bybit as bb
from tb.core import guard
from tb.core import journal
from tb.core import portfolio
from tb.core import session

PROF = guard.load_profile()
OK = {"retCode": 0, "retMsg": "OK"}


class MakerFake:
    """Скриптуемая биржа. script: статусы входного ордера по очереди опросов (последний повторяется); после cancel берётся следующий."""

    def __init__(self, script=(), bid="1.99995", ask="2.00005", positions=None, cancel_err=False, tp_fail=0, closed=None, cancel_all_fail=False):
        self.calls, self.script, self.bid, self.ask = [], list(script), bid, ask
        self.positions, self.cancel_err, self.tp_fail, self.closed = positions or [], cancel_err, tp_fail, closed or []
        self.cancel_all_fail = cancel_all_fail
        self.n_create = 0

    def st(self, status, qty="0", avg="0", fee="0"):
        return {"orderStatus": status, "cumExecQty": qty, "avgPrice": avg, "cumExecFee": fee}

    def __call__(self, method, url, headers, body):
        path = url.split("bybit.com")[1].split("?")[0]
        b = json.loads(body) if body else None
        self.calls.append((method, path, b))
        if path == "/v5/market/time":
            return dict(OK, result={"timeNano": "1700000000000000000"})
        if path == "/v5/market/instruments-info":
            return dict(OK, result={"list": [{"lotSizeFilter": {"minOrderQty": "1", "qtyStep": "1", "minNotionalValue": "5"},
                                               "priceFilter": {"tickSize": "0.0001"}, "leverageFilter": {"maxLeverage": "25"}}]})
        if path == "/v5/market/tickers":
            return dict(OK, result={"list": [{"bid1Price": self.bid, "ask1Price": self.ask, "lastPrice": "2.0"}]})
        if path == "/v5/position/list":
            return dict(OK, result={"list": self.positions})
        if path == "/v5/position/set-leverage":
            return dict(OK, result={})
        if path == "/v5/position/closed-pnl":
            return dict(OK, result={"list": self.closed})
        if path == "/v5/order/cancel-all":
            return {"retCode": 10001, "retMsg": "x"} if self.cancel_all_fail else dict(OK, result={"list": []})
        if path == "/v5/order/cancel":
            if self.cancel_err:
                return {"retCode": 110001, "retMsg": "Order does not exist"}
            return dict(OK, result={"orderId": b["orderId"]})
        if path == "/v5/order/create":
            self.n_create += 1
            if b.get("reduceOnly") and b.get("orderType") == "Limit":
                if self.tp_fail and (self.tp_fail >= 2 or b["timeInForce"] == "PostOnly"):
                    return {"retCode": 110017, "retMsg": "rejected"}
                return dict(OK, result={"orderId": "T%d" % self.n_create})
            return dict(OK, result={"orderId": "E1"})
        if path == "/v5/order/realtime":
            if b is None and self.script:
                s = self.script[0] if len(self.script) == 1 else self.script.pop(0)
                return dict(OK, result={"list": [s]})
            return dict(OK, result={"list": [self.script[0] if self.script else {}]})
        return {"retCode": 10001, "retMsg": "unknown"}

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]

    def bodies(self, path):
        return [b for _, p, b in self.calls if p == path]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.old = (journal.PATH, portfolio.PATH, session.PATH)
        journal.PATH, portfolio.PATH, session.PATH = (os.path.join(d, x) for x in ("t.jsonl", "p.json", "s.jsonl"))
        portfolio.set_venue("bybit", 10.0, 10.0)
        session.start("intraday", "t", "bybit", None, None, PROF)
        self.clk = Clock()

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH = self.old

    def client(self, fake, env="testnet"):
        return bb.Client(env, "KEY", "SECRET", transport=fake, clock=lambda: 1700000000.0)

    def args(self, **kw):
        d = dict(sym="XRP", side="long", sl=1.96, tp=2.10, thesis="t", news="", score=0.8, regime="range", dry_run=False,
                 mainnet_confirm=False, reason="manual", exec="maker", timeout=3.0)
        d.update(kw)
        return argparse.Namespace(**d)

    def open_m(self, fake, env="testnet", **kw):
        return bb.cmd_open(self.client(fake, env), self.args(**kw), self.clk.sleep, self.clk.now)

    def trade(self):
        tr, ids = journal._load()
        return tr[ids[0]]


class FillTest(Base):
    def test_fill_in_time(self):
        f = MakerFake([{"orderStatus": "New", "cumExecQty": "0"}, MakerFake().st("Filled", "9", "1.9999", "0.0064")])
        r = self.open_m(f)
        self.assertTrue(r["ok"] and r["filled"], r)
        self.assertNotIn("warning", r)
        self.assertTrue(set(f.paths()) <= bb.ALLOWED)
        self.assertEqual(f.bodies("/v5/order/cancel"), [])
        # перед входом снимаются остатки старых ордеров, потом создаётся вход
        self.assertLess(f.paths().index(("POST", "/v5/order/cancel-all")), f.paths().index(("POST", "/v5/order/create")))
        entry, tp = f.bodies("/v5/order/create")
        self.assertEqual((entry["side"], entry["orderType"], entry["timeInForce"], entry["price"]), ("Buy", "Limit", "PostOnly", "1.9999"))
        self.assertEqual((entry["stopLoss"], entry["tpslMode"], entry["positionIdx"]), ("1.96", "Full", 0))
        self.assertNotIn("takeProfit", entry)
        self.assertIsInstance(entry["qty"], str)
        self.assertIsInstance(entry["price"], str)
        self.assertEqual((tp["side"], tp["reduceOnly"], tp["orderType"], tp["qty"], tp["price"]), ("Sell", True, "Limit", "9", "2.1"))
        t = self.trade()
        self.assertEqual((t["mode"], t["venue"], t["entry"], t["fee_open"]), ("testnet", "bybit", 1.9999, 0.0064))
        self.assertIn("maker", t["tags"])
        self.assertAlmostEqual(t["margin"], 9 * 1.9999 / t["lev"], places=6)

    def test_timeout_cancels_no_trade(self):
        f = MakerFake([MakerFake().st("New"), MakerFake().st("New"), MakerFake().st("Cancelled")])
        r = self.open_m(f)
        self.assertTrue(r["ok"])
        self.assertFalse(r["filled"])
        self.assertEqual(len(f.bodies("/v5/order/cancel")), 1)
        self.assertEqual(len(f.bodies("/v5/order/create")), 1)       # только вход, тейка нет
        self.assertEqual(journal._load()[1], [])

    def test_partial_fill_tp_for_filled_qty(self):
        pf = MakerFake().st("PartiallyFilled", "3", "1.9999", "0.002")
        f = MakerFake([pf, pf, MakerFake().st("PartiallyFilledCanceled", "3", "1.9999", "0.002")])
        r = self.open_m(f)
        self.assertTrue(r["filled"], r)
        self.assertEqual(r["filled_qty"], "3")
        self.assertEqual(len(f.bodies("/v5/order/cancel")), 1)
        tp = f.bodies("/v5/order/create")[-1]
        self.assertEqual((tp["qty"], tp["reduceOnly"]), ("3", True))
        t = self.trade()
        self.assertAlmostEqual(t["margin"], 3 * 1.9999 / t["lev"], places=6)
        self.assertEqual(t["fee_open"], 0.002)

    def test_post_only_rejected_immediately(self):
        f = MakerFake([MakerFake().st("Cancelled")])
        r = self.open_m(f)
        self.assertFalse(r["filled"])
        self.assertIn("Cancelled", r["reason"])
        self.assertEqual(f.bodies("/v5/order/cancel"), [])
        self.assertEqual(journal._load()[1], [])

    def test_cancel_race_order_already_filled(self):
        st = MakerFake().st
        f = MakerFake([st("New"), st("New"), st("Filled", "9", "1.9999", "0.0064")], cancel_err=True)
        r = self.open_m(f)
        self.assertTrue(r["filled"], r)                      # отмена вернула ошибку, но ордер исполнен: тейк и журнал есть
        self.assertEqual(len(f.bodies("/v5/order/cancel")), 1)
        self.assertEqual(f.bodies("/v5/order/create")[-1]["reduceOnly"], True)
        self.assertEqual(len(journal._load()[1]), 1)

    def test_cancel_error_and_still_open_raises_loudly(self):
        f = MakerFake([MakerFake().st("New")], cancel_err=True)
        with self.assertRaises(bb.BybitError) as cm:
            self.open_m(f)
        self.assertIn("ВНИМАНИЕ", str(cm.exception))
        self.assertEqual(journal._load()[1], [])

    def test_poll_failure_cancels_order_and_raises(self):
        class Boom(MakerFake):
            def __call__(s, method, url, headers, body):
                if "/v5/order/realtime" in url:
                    raise OSError("сеть")
                return MakerFake.__call__(s, method, url, headers, body)
        f = Boom()
        with self.assertRaises(bb.BybitError):
            self.open_m(f)
        self.assertEqual(len(f.bodies("/v5/order/cancel")), 1)

    def test_short_uses_ask_rounded_up(self):
        f = MakerFake([MakerFake().st("Filled", "9", "2.0001", "0.006")])
        r = self.open_m(f, side="short", sl=2.04, tp=1.90)
        self.assertTrue(r["filled"], r)
        entry, tp = f.bodies("/v5/order/create")
        self.assertEqual((entry["side"], entry["price"], entry["stopLoss"]), ("Sell", "2.0001", "2.04"))
        self.assertEqual((tp["side"], tp["price"], tp["reduceOnly"]), ("Buy", "1.9", True))

    def test_tp_failure_warns_and_journals(self):
        f = MakerFake([MakerFake().st("Filled", "9", "1.9999", "0.006")], tp_fail=2)
        r = self.open_m(f)
        self.assertTrue(r["ok"] and r["filled"])
        self.assertIn("ТЕЙК", r["warning"])
        self.assertIsNone(r["tp_order_id"])
        self.assertIn("no_tp", self.trade()["tags"])

    def test_tp_postonly_reject_falls_back_to_gtc(self):
        f = MakerFake([MakerFake().st("Filled", "9", "1.9999", "0.006")], tp_fail=1)
        r = self.open_m(f)
        self.assertNotIn("warning", r)
        self.assertEqual(f.bodies("/v5/order/create")[-1]["timeInForce"], "GTC")

    def test_closed_trade_not_overwritten_by_new_entry(self):
        self.open_m(MakerFake([MakerFake().st("Filled", "9", "1.9999", "0.006")]))
        jid = journal._load()[1][0]
        journal.cmd_close(argparse.Namespace(id=jid, exit=2.1, reason="tp", funding=0.0, fee=0.001, fees_usdt=0.01, note=""))
        self.open_m(MakerFake([MakerFake().st("Filled", "9", "1.9999", "0.006")]))
        tr, ids = journal._load()
        self.assertTrue(tr[jid].get("ts_close"))           # id не совпал с закрытой записью того же символа и секунды
        self.assertEqual((len(ids), len(set(ids))), (2, 2))


class SafetyTest(Base):
    def test_dry_run_no_orders_no_journal(self):
        f = MakerFake()
        r = self.open_m(f, dry_run=True)
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertEqual([m for m, _ in f.paths() if m == "POST"], [])
        self.assertEqual(journal._load()[1], [])

    def test_mainnet_needs_confirm_in_maker_branch(self):
        f = MakerFake()
        with self.assertRaises(bb.BybitError):
            self.open_m(f, env="mainnet")
        with self.assertRaises(bb.BybitError):
            bb.open_maker(self.client(f, "mainnet"), self.args(), self.clk.sleep, self.clk.now)
        self.assertEqual(f.calls, [])

    def test_guard_refusal_places_nothing(self):
        session.stop(session.active()["id"]) if hasattr(session, "stop") else None
        f = MakerFake()
        r = self.open_m(f, sl=2.2)                           # стоп не с той стороны
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "guard")
        self.assertEqual([m for m, _ in f.paths() if m == "POST"], [])

    def test_existing_position_blocks(self):
        with self.assertRaises(bb.BybitError):
            self.open_m(MakerFake(positions=[{"size": "5", "side": "Buy"}]))


class CloseSyncTest(Base):
    def opened(self, **kw):
        self.open_m(MakerFake([MakerFake().st("Filled", "9", "1.9999", "0.0064")]), **kw)
        return journal._load()[1][0]

    def row(self, px, fee_open="0.0064", fee_close="0.0100", t=None):
        return {"avgExitPrice": str(px), "openFee": fee_open, "closeFee": fee_close, "updatedTime": str(t or int(time.time() * 1000) + 5000)}

    def test_close_cancels_orders_before_market_close(self):
        jid = self.opened()
        f = MakerFake([MakerFake().st("Filled", "9", "2.0", "0.01")], positions=[{"size": "9", "side": "Buy"}])
        r = bb.cmd_close(self.client(f), self.args(), sleep=lambda s: None)
        self.assertTrue(r["ok"], r)
        p = f.paths()
        self.assertLess(p.index(("POST", "/v5/order/cancel-all")), p.index(("POST", "/v5/order/create")))
        o = f.bodies("/v5/order/create")[0]
        self.assertEqual((o["side"], o["reduceOnly"], o["qty"], o["orderType"]), ("Sell", True, "9", "Market"))
        self.assertTrue(journal._load()[0][jid].get("ts_close"))
        self.assertNotIn("warning", r)

    def test_close_warns_if_cancel_all_failed(self):
        self.opened()
        f = MakerFake([MakerFake().st("Filled", "9", "2.0", "0.01")], positions=[{"size": "9", "side": "Buy"}], cancel_all_fail=True)
        self.assertIn("ВНИМАНИЕ", bb.cmd_close(self.client(f), self.args(), sleep=lambda s: None)["warning"])

    def test_close_dry_run_cancels_nothing(self):
        self.opened()
        f = MakerFake(positions=[{"size": "9", "side": "Buy"}])
        bb.cmd_close(self.client(f), self.args(dry_run=True), sleep=lambda s: None)
        self.assertEqual([m for m, _ in f.paths() if m == "POST"], [])

    def sync(self, fake):
        return bb.cmd_sync(self.client(fake))

    def test_sync_after_tp(self):
        jid = self.opened()
        f = MakerFake(closed=[self.row(2.1)])
        r = self.sync(f)
        self.assertEqual(r["closed"][0]["reason"], "tp")
        self.assertIn(("POST", "/v5/order/cancel-all"), f.paths())
        t = journal._load()[0][jid]
        self.assertEqual(t["reason"], "tp")
        self.assertAlmostEqual(t["fees_usdt"], 0.0164, places=4)
        self.assertGreater(t["pnl_usdt"], 0)

    def test_sync_after_stop_cancels_leftover_tp(self):
        jid = self.opened()
        f = MakerFake(closed=[self.row(1.9585)])               # чуть хуже стопа 1.96: проскальзывание
        r = self.sync(f)
        self.assertEqual(r["closed"][0]["reason"], "sl")
        self.assertIn(("POST", "/v5/order/cancel-all"), f.paths())
        self.assertLess(journal._load()[0][jid]["pnl_usdt"], 0)

    def test_sync_gap_through_stop_still_sl(self):
        self.opened()
        self.assertEqual(self.sync(MakerFake(closed=[self.row(1.90)]))["closed"][0]["reason"], "sl")

    def test_sync_exit_between_levels_is_closed(self):
        self.opened()
        self.assertEqual(self.sync(MakerFake(closed=[self.row(2.02)]))["closed"][0]["reason"], "closed")

    def test_sync_short_directions(self):
        self.open_m(MakerFake([MakerFake().st("Filled", "9", "2.0001", "0.006")]), side="short", sl=2.04, tp=1.90)
        self.assertEqual(self.sync(MakerFake(closed=[self.row(1.9)]))["closed"][0]["reason"], "tp")
        self.open_m(MakerFake([MakerFake().st("Filled", "9", "2.0001", "0.006")]), side="short", sl=2.04, tp=1.90)
        self.assertEqual(self.sync(MakerFake(closed=[self.row(2.05)]))["closed"][0]["reason"], "sl")

    def test_sync_open_position_closes_nothing(self):
        jid = self.opened()
        f = MakerFake(positions=[{"size": "9", "side": "Buy"}], closed=[self.row(2.1)])
        r = self.sync(f)
        self.assertEqual((r["closed"], r["still_open"]), ([], [jid]))
        self.assertNotIn(("POST", "/v5/order/cancel-all"), f.paths())
        self.assertFalse(journal._load()[0][jid].get("ts_close"))

    def test_sync_no_closed_row_stays_pending_but_cancels_orders(self):
        jid = self.opened()
        f = MakerFake(closed=[])
        r = self.sync(f)
        self.assertEqual(r["still_open"], [jid])
        self.assertIn(("POST", "/v5/order/cancel-all"), f.paths())

    def test_sync_ignores_old_closed_rows(self):
        jid = self.opened()
        r = self.sync(MakerFake(closed=[self.row(2.1, t=1000)]))
        self.assertEqual(r["still_open"], [jid])


class GuardMakerTest(Base):
    def a(self, **kw):
        d = dict(sym="SOL", side="long", entry=100.0, sl=98.0, tp=104.0, equity=40.0, free=30.0, score=1.0, regime="range",
                 min_quote=1.0, contract_max_lev=50)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_costs(self):
        self.assertEqual(guard.trade_costs_pct(PROF, "bybit", "taker"), (guard.round_trip_cost_pct(PROF, "bybit"),) * 2)
        w, l = guard.trade_costs_pct(PROF, "bybit", "maker")
        self.assertAlmostEqual(w, 0.072, places=6)             # 2 x maker 0.036%
        self.assertAlmostEqual(l, 0.156, places=6)             # maker вход + taker стоп + проскальзывание
        self.assertEqual(guard.trade_costs_pct(PROF, "platform", "maker"), (guard.round_trip_cost_pct(PROF, "platform"),) * 2)

    def test_maker_vs_taker(self):
        t = guard.check(self.a(), PROF, "2026-10-27", ctx={"venue": "bybit", "exec": "taker"})
        m = guard.check(self.a(), PROF, "2026-10-27", ctx={"venue": "bybit", "exec": "maker"})
        self.assertAlmostEqual(t["net_rr"], round((4 - 0.24) / (2 + 0.24), 2))
        self.assertAlmostEqual(m["net_rr"], round((4 - 0.072) / (2 + 0.156), 2))
        self.assertEqual(t["cost_win_pct"], 0.24)
        self.assertGreater(m["net_rr"], t["net_rr"])
        self.assertGreater(m["notional"], t["notional"])
        self.assertLessEqual(m["risk_usdt"], 40.0 * PROF["risk_per_trade_frac"] + 1e-6)   # риск со стопом и издержками не выше бюджета
        # без ctx.exec режим taker, как раньше
        d = guard.check(self.a(), PROF, "2026-10-27", ctx={"venue": "bybit"})
        self.assertEqual((d["net_rr"], d["notional"]), (t["net_rr"], t["notional"]))

    def test_limits_not_loosened_in_maker(self):
        # тейк 3.0% при стопе 2%: taker и maker отклонены одинаково (net_rr < 1.5)
        for ex in ("taker", "maker"):
            r = guard.check(self.a(tp=103.0), PROF, "2026-10-27", ctx={"venue": "bybit", "exec": ex})
            self.assertFalse(r["ok"])
        r = guard.check(self.a(sl=96.0, tp=110.0), PROF, "2026-10-27", ctx={"venue": "bybit", "exec": "maker"})
        self.assertIn("stop 4.00% > max 3.00%", r["reasons"][0])

    def test_evaluate_passes_exec(self):
        ns = lambda ex: argparse.Namespace(sym="SOL", side="long", entry=100.0, sl=98.0, tp=104.0, equity=None, free=None, score=1.0,
                                           regime="range", min_quote=0.0, contract_max_lev=50, profile=None, no_session=False, exec=ex)
        self.assertEqual(guard.evaluate(ns("maker"))["cost_win_pct"], 0.072)
        self.assertEqual(guard.evaluate(ns("taker"))["cost_win_pct"], 0.24)


if __name__ == "__main__":
    unittest.main()
