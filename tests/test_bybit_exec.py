import argparse
import hashlib
import hmac
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.venues import bybit as bb
from tb.core import guard
from tb.core import journal
from tb.core import portfolio
from tb.core import session

PROF = guard.load_profile()


class FakeBybit:
    def __init__(self, price=2.0, positions=None, qty_min="1", qty_step="1"):
        self.calls, self.price, self.positions = [], price, positions or []
        self.qty_min, self.qty_step = qty_min, qty_step

    def __call__(self, method, url, headers, body):
        path = url.split("bybit.com")[1].split("?")[0]
        self.calls.append((method, path, json.loads(body) if body else None, headers))
        ok = {"retCode": 0, "retMsg": "OK"}
        if path == "/v5/market/time":
            return dict(ok, result={"timeSecond": "1700000000", "timeNano": "1700000000000000000"})
        if path == "/v5/market/instruments-info":
            return dict(ok, result={"list": [{"lotSizeFilter": {"minOrderQty": self.qty_min, "qtyStep": self.qty_step, "minNotionalValue": "5"},
                                               "priceFilter": {"tickSize": "0.0001"}, "leverageFilter": {"maxLeverage": "25"}}]})
        if path == "/v5/market/tickers":
            return dict(ok, result={"list": [{"lastPrice": str(self.price)}]})
        if path == "/v5/position/list":
            return dict(ok, result={"list": self.positions})
        if path == "/v5/position/set-leverage":
            return dict(ok, result={})
        if path == "/v5/order/create":
            return dict(ok, result={"orderId": "OID1"})
        if path == "/v5/order/realtime":
            return dict(ok, result={"list": [{"avgPrice": str(self.price + 0.01), "cumExecFee": "0.001"}]})
        return {"retCode": 10001, "retMsg": "unknown"}

    def paths(self):
        return [(m, p) for m, p, _, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.old = (journal.PATH, portfolio.PATH, session.PATH)
        journal.PATH, portfolio.PATH, session.PATH = (os.path.join(d, x) for x in ("t.jsonl", "p.json", "s.jsonl"))
        portfolio.set_venue("bybit", 10.0, 10.0)

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH = self.old

    def client(self, fake, env="testnet"):
        return bb.Client(env, "KEY", "SECRET", transport=fake, clock=lambda: 1700000000.0)

    def args(self, **kw):
        d = dict(sym="XRP", side="long", sl=1.96, tp=2.10, thesis="t", news="", score=0.8, regime="range",
                 dry_run=False, mainnet_confirm=False, reason="manual")
        d.update(kw)
        return argparse.Namespace(**d)


class ClientTest(Base):
    def test_clock_offset_applied_and_10002_resync(self):
        state = {"n": 0}

        def transport(method, url, headers, body):
            if url.endswith("/v5/market/time"):
                return {"retCode": 0, "result": {"timeNano": str(1700000020000 * 1000000)}}  # сервер на 20 с впереди
            state["n"] += 1
            state["ts"] = headers["X-BAPI-TIMESTAMP"]
            return {"retCode": 10002, "retMsg": "ts"} if state["n"] == 1 else {"retCode": 0, "result": {}}
        c = bb.Client("testnet", "KEY", "SECRET", transport=transport, clock=lambda: 1700000000.0)
        c.call("GET", "/v5/position/list", {})
        self.assertEqual(state["n"], 2)                      # повтор после 10002
        self.assertEqual(state["ts"], "1700000020000")       # время сервера, не локальное

    def test_signature_and_headers(self):
        f = FakeBybit()
        c = self.client(f)
        c.call("POST", "/v5/order/create", {"a": 1})
        _, _, body, h = f.calls[-1]
        payload = json.dumps({"a": 1}, separators=(",", ":"))
        exp = hmac.new(b"SECRET", ("1700000000000" + "KEY" + bb.RECV_WINDOW + payload).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(h["X-BAPI-SIGN"], exp)
        self.assertEqual(h["X-BAPI-API-KEY"], "KEY")

    def test_allowlist_blocks_money_movement(self):
        c = self.client(FakeBybit())
        for path in ("/v5/asset/withdraw/create", "/v5/asset/transfer/inter-transfer", "/v5/user/create-sub-api"):
            with self.assertRaises(bb.BybitError):
                c.call("POST", path, {})

    def test_no_keys_blocks_private_calls_only(self):
        c = bb.Client("testnet", None, None, transport=FakeBybit())
        self.assertTrue(c.call("GET", "/v5/market/tickers", {"category": "linear"}, auth=False) is not None)
        with self.assertRaises(bb.BybitError):
            c.call("GET", "/v5/position/list", {})

    def test_bad_env(self):
        with self.assertRaises(bb.BybitError):
            bb.Client("prod", "k", "s")


class OpenCloseTest(Base):
    def test_open_requires_session(self):
        f = FakeBybit()
        r = bb.cmd_open(self.client(f), self.args(), sleep=lambda s: None)
        self.assertFalse(r["ok"])
        self.assertIn("no_active_session", r["reasons"])
        self.assertNotIn(("POST", "/v5/order/create"), f.paths())

    def test_open_flow_places_order_with_sl_tp_and_journals(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        f = FakeBybit()
        r = bb.cmd_open(self.client(f), self.args(), sleep=lambda s: None)
        self.assertTrue(r["ok"], r)
        paths = f.paths()
        self.assertLess(paths.index(("POST", "/v5/position/set-leverage")), paths.index(("POST", "/v5/order/create")))
        order = [c for c in f.calls if c[1] == "/v5/order/create"][0][2]
        self.assertEqual(order["side"], "Buy")
        self.assertEqual(order["stopLoss"], "1.96")
        self.assertEqual(order["takeProfit"], "2.1")
        self.assertGreaterEqual(float(order["qty"]), 1)
        tr, ids = journal._load()
        t = tr[ids[0]]
        self.assertEqual((t["venue"], t["mode"], t["stype"]), ("bybit", "testnet", "intraday"))
        self.assertAlmostEqual(t["entry"], 2.01)

    def test_dry_run_sends_no_orders(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        f = FakeBybit()
        r = bb.cmd_open(self.client(f), self.args(dry_run=True), sleep=lambda s: None)
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertNotIn(("POST", "/v5/order/create"), f.paths())
        self.assertEqual(journal._load()[1], [])

    def test_min_size_rejected_without_order(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        f = FakeBybit(price=80000.0, qty_min="0.001", qty_step="0.001")
        r = bb.cmd_open(self.client(f), self.args(sym="BTC", sl=78400.0, tp=84000.0), sleep=lambda s: None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "min_size")
        self.assertNotIn(("POST", "/v5/order/create"), f.paths())

    def test_existing_position_blocks_open(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        f = FakeBybit(positions=[{"size": "5", "side": "Buy"}])
        with self.assertRaises(bb.BybitError):
            bb.cmd_open(self.client(f), self.args(), sleep=lambda s: None)

    def test_mainnet_needs_confirm_flag(self):
        with self.assertRaises(bb.BybitError):
            bb.cmd_open(self.client(FakeBybit(), "mainnet"), self.args(), sleep=lambda s: None)

    def test_close_reduce_only_and_journal_close(self):
        session.start("intraday", "t", "bybit", None, None, PROF)
        bb.cmd_open(self.client(FakeBybit()), self.args(), sleep=lambda s: None)
        f = FakeBybit(price=2.05, positions=[{"size": "3", "side": "Buy"}])
        r = bb.cmd_close(self.client(f), self.args(), sleep=lambda s: None)
        self.assertTrue(r["ok"], r)
        order = [c for c in f.calls if c[1] == "/v5/order/create"][0][2]
        self.assertEqual((order["side"], order["reduceOnly"], order["qty"]), ("Sell", True, "3"))
        tr, ids = journal._load()
        self.assertIsNotNone(tr[ids[0]].get("ts_close"))
        self.assertAlmostEqual(tr[ids[0]]["fees_usdt"], 0.002, places=6)   # фактические комиссии биржи: вход 0.001 + выход 0.001


class WalletTest(Base):
    def test_wallet_all_reports_every_account_type_and_coins(self):
        def transport(method, url, headers, body):
            if "accountType=UNIFIED" in url:
                return {"retCode": 0, "result": {"list": [{"totalEquity": "0", "totalAvailableBalance": "0", "coin": []}]}}
            return {"retCode": 0, "result": {"list": [{"totalEquity": "100", "totalAvailableBalance": "90",
                                                       "coin": [{"coin": "USDT", "walletBalance": "100"}, {"coin": "BTC", "walletBalance": "0"}]}]}}
        cl = self.client(transport)
        cl.offset_ms = 0
        out = bb.wallet_all(cl)
        self.assertEqual(out["UNIFIED"]["equity"], 0.0)
        self.assertEqual(out["CONTRACT"]["coins"], {"USDT": 100.0})

    def test_isolated_margin_free_computed_from_coin(self):
        def transport(method, url, headers, body):
            return {"retCode": 0, "result": {"list": [{"totalEquity": "10.92", "totalAvailableBalance": "0",
                                                       "coin": [{"coin": "USDT", "walletBalance": "10.9418", "locked": "0",
                                                                 "totalPositionIM": "2.0", "totalOrderIM": ""}]}]}}
        cl = self.client(transport)
        cl.offset_ms = 0
        w = bb.wallet(cl)
        self.assertAlmostEqual(w["free"], 8.9418, places=4)
        self.assertAlmostEqual(w["equity"], 10.92)

    def test_wallet_all_survives_errors(self):
        cl = self.client(lambda m, u, h, b: {"retCode": 10001, "retMsg": "x"})
        cl.offset_ms = 0
        out = bb.wallet_all(cl)
        self.assertIn("error", out["UNIFIED"])


if __name__ == "__main__":
    unittest.main()
