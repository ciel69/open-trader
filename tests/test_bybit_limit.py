"""Ждущие лимитные входы Bybit (venues/bybit_limit.py, core/pending.py): постановка, post-only, заполнение/частичное через sync, ttl, инвалидация,
рестарт, учёт в guard/сессии, события watch. Без сети: поддельная биржа, временные файлы."""
import argparse
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse

import _bootstrap  # noqa: F401
from tb.core import guard
from tb.core import ideas
from tb.core import journal
from tb.core import learning
from tb.core import pending
from tb.core import portfolio
from tb.core import session
from tb.venues import bybit as bb
from tb.venues import bybit_limit as bl
from tb.venues import bybit_stream as bs

PROF = guard.load_profile()
OK = {"retCode": 0, "retMsg": "OK"}
T0 = 1700000000          # секунды: момент «заполнения» на поддельной бирже


class LimitFake:
    """Биржа с состоянием ордеров. fill()/partial() имитируют исполнение и, как настоящая биржа, вешают стоп/тейк ордера на позицию."""

    def __init__(self, bid="1.9990", ask="2.0010", last="2.0", positions=None, closed=None, no_sl=False, create_exc=None, hide_created=False):
        self.calls, self.bid, self.ask, self.last = [], bid, ask, last
        self.orders, self.history, self.positions, self.closed = {}, {}, positions or [], closed or []
        self.no_sl, self.create_exc, self.hide_created = no_sl, create_exc, hide_created
        self.n = 0
        self.cancel_fail = False

    def __call__(self, method, url, headers, body):
        path = url.split("bybit.com")[1].split("?")[0]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        b = json.loads(body) if body else None
        self.calls.append((method, path, b if b is not None else q))
        if path == "/v5/market/time":
            return dict(OK, result={"timeNano": "1700000000000000000"})
        if path == "/v5/market/instruments-info":
            return dict(OK, result={"list": [{"lotSizeFilter": {"minOrderQty": "1", "qtyStep": "1", "minNotionalValue": "5"},
                                               "priceFilter": {"tickSize": "0.0001"}, "leverageFilter": {"maxLeverage": "25"}}]})
        if path == "/v5/market/tickers":
            return dict(OK, result={"list": [{"bid1Price": self.bid, "ask1Price": self.ask, "lastPrice": self.last}]})
        if path == "/v5/position/list":
            return dict(OK, result={"list": self.positions})
        if path in ("/v5/position/set-leverage", "/v5/order/cancel-all"):
            return dict(OK, result={})
        if path == "/v5/position/closed-pnl":
            return dict(OK, result={"list": self.closed})
        if path == "/v5/order/create":
            if self.create_exc:
                if self.create_exc == "after":      # ордер создан, но ответ потерян
                    self._add(b)
                raise OSError("сеть")
            if b.get("reduceOnly"):
                self.n += 1
                return dict(OK, result={"orderId": "M%d" % self.n})
            return dict(OK, result={"orderId": self._add(b)})
        if path == "/v5/order/cancel":
            o = self._find(b)
            if self.cancel_fail or not o or o["orderStatus"] not in ("New", "PartiallyFilled"):
                return {"retCode": 110001, "retMsg": "Order does not exist"}
            o["orderStatus"] = "PartiallyFilledCanceled" if float(o["cumExecQty"]) > 0 else "Cancelled"
            return dict(OK, result={"orderId": o["orderId"]})
        if path == "/v5/order/realtime":
            o = self._find(q)
            return dict(OK, result={"list": [o] if o and not self.hide_created else []})
        if path == "/v5/order/history":
            o = self.history.get(q.get("orderId")) or next((x for x in self.history.values() if x["orderLinkId"] == q.get("orderLinkId")), None)
            return dict(OK, result={"list": [o] if o else []})
        return {"retCode": 10001, "retMsg": "unknown " + path}

    def _add(self, b):
        self.n += 1
        oid = "O%d" % self.n
        self.orders[oid] = {"orderId": oid, "orderLinkId": b.get("orderLinkId"), "orderStatus": "New", "cumExecQty": "0", "avgPrice": "0", "cumExecFee": "0",
                            "updatedTime": str(T0 * 1000), "body": b}
        return oid

    def _find(self, k):
        if k.get("orderId"):
            return self.orders.get(k["orderId"])
        return next((o for o in self.orders.values() if o["orderLinkId"] == k.get("orderLinkId")), None)

    # --- сценарные помощники
    def only(self):
        return list(self.orders.values())[0]

    def fill(self, qty=None, avg=None, fee="0.0064", status="Filled"):
        o = self.only()
        b = o["body"]
        o.update(orderStatus=status, cumExecQty=qty or b["qty"], avgPrice=avg or b["price"], cumExecFee=fee, updatedTime=str(T0 * 1000))
        sl = {} if self.no_sl else {"stopLoss": b["stopLoss"]}
        self.positions = [dict({"size": o["cumExecQty"], "side": b["side"], "takeProfit": b["takeProfit"]}, **sl)]

    def partial(self, qty, avg=None):
        self.fill(qty=str(qty), avg=avg, fee="0.002", status="PartiallyFilled")

    def kinds(self, path):
        return [x for m, p, x in self.calls if p == path]

    def posts(self):
        return [p for m, p, _ in self.calls if m == "POST"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = (journal.PATH, portfolio.PATH, session.PATH, pending.PATH, os.environ.get("TB_ROOT"))
        journal.PATH, portfolio.PATH, session.PATH = (os.path.join(self.dir, x) for x in ("t.jsonl", "p.json", "s.jsonl"))
        pending.PATH = os.path.join(self.dir, "pending_orders.json")
        os.environ["TB_ROOT"] = self.dir
        os.makedirs(os.path.join(self.dir, "state"))
        shutil.copytree(os.path.join(_bootstrap.ROOT, "config"), os.path.join(self.dir, "config"))   # guard.evaluate читает профиль из TB_ROOT
        portfolio.set_venue("bybit", 10.0, 10.0)
        self.sess = session.start("intraday", "t", "bybit", None, None, PROF)
        self.clk = Clock()

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH, pending.PATH, root = self.old
        if root is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = root
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, fake, env="testnet"):
        return bb.Client(env, "KEY", "SECRET", transport=fake, clock=lambda: 1700000000.0)

    def args(self, **kw):
        d = dict(sym="XRP", side="long", sl=1.96, tp=2.10, thesis="ретест", news="", score=0.8, regime="range", dry_run=False, mainnet_confirm=False,
                 reason="manual", exec="taker", timeout=3.0, limit_price=1.98, ttl=None, cancel_above=None, cancel_below=None, snap=False, idea=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def place(self, fake, env="testnet", **kw):
        return bb.cmd_open(self.client(fake, env), self.args(**kw), self.clk.sleep, self.clk.now)

    def sync(self, fake, env="testnet"):
        return bb.cmd_sync(self.client(fake, env), sleep=self.clk.sleep, clock=self.clk.now)

    def rec(self):
        rows = pending.orders()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]


class PlaceTest(Base):
    def test_place_far_from_market(self):
        f = LimitFake()
        r = self.place(f, limit_price=1.90, sl=1.86, tp=2.0)             # 5% ниже рынка
        self.assertTrue(r["ok"] and r["pending"], r)
        self.assertTrue(set(m_p for m_p in [(m, p) for m, p, _ in f.calls]) <= bb.ALLOWED)
        o = f.kinds("/v5/order/create")[0]
        self.assertEqual((o["side"], o["orderType"], o["timeInForce"], o["price"]), ("Buy", "Limit", "PostOnly", "1.9"))
        self.assertEqual((o["stopLoss"], o["takeProfit"], o["tpslMode"], o["slTriggerBy"], o["positionIdx"]), ("1.86", "2", "Full", "LastPrice", 0))
        self.assertEqual(o["orderLinkId"], r["pending_id"])
        self.assertLessEqual(len(o["orderLinkId"]), 36)
        self.assertIsInstance(o["qty"], str)
        rec = self.rec()
        self.assertEqual((rec["id"], rec["order_id"], rec["status"], rec["mode"], rec["env"]), (r["pending_id"], "O1", "open", "testnet", "testnet"))
        self.assertEqual((rec["session"], rec["price"], rec["sl"], rec["tp"]), (self.sess["id"], 1.9, 1.86, 2.0))
        self.assertAlmostEqual(rec["expires"] - rec["ts"], 3600.0)       # ttl по умолчанию 60 мин
        self.assertEqual(journal._load()[1], [])                          # в журнале сделки нет, пока не заполнено
        self.assertEqual(r["state"]["action"], "waiting")
        self.assertLess(r["plan"]["dist_pct"], -4.9)
        with open(pending.PATH, encoding="utf-8") as fh:                  # состояние лежит в файле и читается заново
            self.assertEqual(list(json.load(fh)["orders"]), [r["pending_id"]])

    def test_post_only_cross_refused_for_long_and_short(self):
        for kw in (dict(limit_price=2.001), dict(limit_price=2.5), dict(side="short", limit_price=1.999, sl=2.04, tp=1.90), dict(side="short", limit_price=1.5, sl=1.54, tp=1.4)):
            f = LimitFake()
            r = self.place(f, **kw)
            self.assertFalse(r["ok"], kw)
            self.assertEqual(r["stage"], "post_only")
            self.assertIn("--snap", r["reasons"][0])
            self.assertEqual(f.posts(), [])
        self.assertEqual(pending.orders(), [])

    def test_snap_moves_to_best_price(self):
        f = LimitFake()
        r = self.place(f, limit_price=2.5, sl=1.96, tp=2.10, snap=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(f.kinds("/v5/order/create")[0]["price"], "1.999")
        self.assertTrue(r["plan"]["snapped"])
        pending.remove(self.rec()["id"])
        f = LimitFake()
        r = self.place(f, side="short", limit_price=1.5, sl=2.04, tp=1.90, snap=True)
        self.assertEqual(f.kinds("/v5/order/create")[0]["price"], "2.001")

    def test_rounding_to_tick(self):
        f = LimitFake()
        self.place(f, limit_price=1.98007, sl=1.9599, tp=2.10005)
        o = f.kinds("/v5/order/create")[0]
        self.assertEqual(o["price"], "1.98")                              # лонг: вниз
        self.assertEqual((o["stopLoss"], o["takeProfit"]), ("1.9599", "2.1"))   # стоп не ближе, тейк не дальше
        self.assertEqual(self.rec()["price"], 1.98)
        pending.remove(self.rec()["id"])
        f = LimitFake()
        self.place(f, side="short", limit_price=2.01003, sl=2.05003, tp=1.9)
        o = f.kinds("/v5/order/create")[0]
        self.assertEqual((o["price"], o["stopLoss"], o["takeProfit"]), ("2.0101", "2.0501", "1.9"))   # шорт: цена и стоп вверх

    def test_guard_refusal_uses_limit_price_not_market(self):
        f = LimitFake()
        r = self.place(f, limit_price=1.98, sl=1.99, tp=2.10)             # стоп выше лимита лонга: от рынка 2.0 стоп 1.99 был бы валиден
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "guard")
        self.assertEqual(f.posts(), [])
        self.assertEqual(pending.orders(), [])

    def test_guard_costs_are_limit_mode(self):
        f = LimitFake()
        r = self.place(f, dry_run=True)
        self.assertTrue(r["ok"] and r["dry_run"], r)
        self.assertEqual(r["plan"]["cost_win_pct"], 0.156)                # maker вход + taker выход + проскальзывание
        self.assertEqual(f.posts(), [])
        self.assertEqual(pending.orders(), [])
        w, l = guard.trade_costs_pct(PROF, "bybit", "limit")
        self.assertAlmostEqual(w, 0.156, places=9)
        self.assertEqual(w, l)
        self.assertGreater(w, guard.trade_costs_pct(PROF, "bybit", "maker")[0])

    def test_ttl_not_clamped_by_session_and_validation(self):
        f = LimitFake()
        r = self.place(f, ttl=100000)
        self.assertFalse(r["plan"]["ttl_clamped_to_session"])             # у сессии нет срока: ttl задаёт только владелец
        self.assertEqual(r["plan"]["ttl_min"], 100000.0)
        pending.remove(self.rec()["id"])
        with self.assertRaises(bb.BybitError):
            self.place(LimitFake(), ttl=-5)

    def test_cancel_levels_validated(self):
        r = self.place(LimitFake(), cancel_above=1.9)                     # ниже рынка: сработало бы сразу
        self.assertEqual((r["ok"], r["stage"]), (False, "levels"))
        r = self.place(LimitFake(), cancel_below=1.99)                    # между лимитом и рынком: отменило бы заявку на подходе к лимиту
        self.assertEqual(r["stage"], "levels")
        r = self.place(LimitFake(), cancel_above=2.05, cancel_below=1.9)
        self.assertTrue(r["ok"], r)
        pending.remove(self.rec()["id"])
        r = self.place(LimitFake(), cancel_below=2.5)
        self.assertEqual(r["stage"], "levels")

    def test_one_per_symbol_and_no_averaging(self):
        self.place(LimitFake())
        with self.assertRaises(bb.BybitError):
            self.place(LimitFake())
        with self.assertRaises(bb.BybitError):                            # вход по рынку тоже: при заполнении лимита это было бы усреднением
            self.place(LimitFake(), limit_price=None)
        with self.assertRaises(bb.BybitError):
            self.place(LimitFake(), limit_price=None, exec="maker")
        with self.assertRaises(bb.BybitError):
            self.place(LimitFake(positions=[{"size": "5", "side": "Buy"}]), sym="SOL", limit_price=1.98)

    def test_mainnet_needs_confirm(self):
        f = LimitFake()
        with self.assertRaises(bb.BybitError):
            self.place(f, env="mainnet")
        self.assertEqual(f.calls, [])

    def test_exchange_reject_leaves_no_record(self):
        class Rej(LimitFake):
            def __call__(s, method, url, headers, body):
                if url.endswith("/v5/order/create"):
                    s.calls.append((method, "/v5/order/create", json.loads(body)))
                    return {"retCode": 110007, "retMsg": "insufficient"}
                return LimitFake.__call__(s, method, url, headers, body)
        r = self.place(Rej())
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "exchange")
        self.assertEqual(pending.orders(), [])

    def test_post_only_rejected_by_exchange_right_after(self):
        class Rej(LimitFake):
            def _add(s, b):
                oid = LimitFake._add(s, b)
                s.orders[oid]["orderStatus"] = "Cancelled"                # биржа сразу снимает post-only
                return oid
        r = self.place(Rej())
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "post_only_rejected")
        self.assertEqual(pending.orders(), [])

    def test_network_failure_on_create_keeps_record_and_sync_recovers(self):
        f = LimitFake(create_exc="after")                                 # ордер создан, ответ потерян
        with self.assertRaises(bb.BybitError) as cm:
            self.place(f)
        self.assertIn("ВНИМАНИЕ", str(cm.exception))
        rec = self.rec()
        self.assertEqual((rec["status"], rec["order_id"]), ("placing", None))
        f.create_exc = None
        r = self.sync(f)                                                  # находит ордер по orderLinkId
        self.assertEqual(r["pending"]["waiting"][0]["id"], rec["id"])
        self.assertEqual(self.rec()["order_id"], "O1")
        f.fill()
        r = self.sync(f)
        self.assertEqual(r["pending"]["filled"][0]["id"], rec["id"])
        self.assertEqual(len(journal._load()[1]), 1)

    def test_network_failure_and_order_never_created_is_dropped(self):
        f = LimitFake(create_exc="before")
        with self.assertRaises(bb.BybitError):
            self.place(f)
        self.assertEqual(len(pending.orders()), 1)
        r = self.sync(f)                                                  # слишком рано: может ещё появиться
        self.assertEqual(len(r["pending"]["waiting"]), 1)
        self.clk.t += bl.PLACING_GRACE_S + 5
        r = self.sync(f)
        self.assertEqual(r["pending"]["cancelled"][0]["reason"][:6], "ордер ")
        self.assertEqual(pending.orders(), [])


class SyncFillTest(Base):
    def test_full_fill_journal_and_protection(self):
        f = LimitFake()
        self.place(f, limit_price=1.99)
        rec = self.rec()
        f.fill(avg="1.9895", fee="0.0064")
        r = self.sync(f)
        fl = r["pending"]["filled"][0]
        self.assertEqual((fl["id"], fl["partial"], fl["qty"]), (rec["id"], False, rec["qty"]))
        self.assertNotIn("warning", fl)
        self.assertEqual(pending.orders(), [])
        tr, ids = journal._load()
        t = tr[ids[0]]
        self.assertEqual((t["entry"], t["fee_open"], t["mode"], t["venue"], t["sl"], t["tp"]), (1.9895, 0.0064, "testnet", "bybit", 1.96, 2.1))
        self.assertEqual((t["pending_id"], t["session"], t["stype"]), (rec["id"], self.sess["id"], "intraday"))
        self.assertIn("limit_entry", t["tags"])
        self.assertEqual(t["ts_open"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0)))
        self.assertAlmostEqual(t["margin"], float(rec["qty"]) * 1.9895 / rec["lev"], places=6)
        # стоп/тейк вешает биржа вместе с исполнением: никаких отдельных ордеров после заполнения нет
        self.assertEqual(len(f.kinds("/v5/order/create")), 1)
        self.assertEqual(f.kinds("/v5/order/cancel"), [])

    def test_fill_idempotent_if_crash_between_journal_and_pending_removal(self):
        f = LimitFake()
        self.place(f)
        rec = self.rec()
        f.fill()
        st = bl.lookup(self.client(f), rec)
        orig = pending.remove
        pending.remove = lambda pid: (_ for _ in ()).throw(RuntimeError("упали"))
        try:
            with self.assertRaises(RuntimeError):
                bl.settle_fill(self.client(f), rec, st)
        finally:
            pending.remove = orig
        self.assertEqual((len(journal._load()[1]), len(pending.orders())), (1, 1))
        self.sync(f)                                                      # повтор: записи в журнале не прибавляется
        self.assertEqual((len(journal._load()[1]), len(pending.orders())), (1, 0))

    def test_partial_fill_cancels_remainder_and_journals_filled_qty(self):
        f = LimitFake()
        self.place(f, limit_price=1.99)
        rec = self.rec()
        f.partial(3, avg="1.99")
        r = self.sync(f)
        fl = r["pending"]["filled"][0]
        self.assertTrue(fl["partial"] and fl["qty"] == "3" and fl["planned_qty"] == rec["qty"], fl)
        self.assertEqual(fl["remainder_cancelled"], "partial_fill")
        self.assertEqual(len(f.kinds("/v5/order/cancel")), 1)             # остаток снят
        self.assertEqual(pending.orders(), [])
        t = list(journal._load()[0].values())[0]
        self.assertAlmostEqual(t["margin"], 3 * 1.99 / rec["lev"], places=6)
        self.assertIn("partial", t["tags"])
        self.assertEqual(t["fee_open"], 0.002)

    def test_cancel_race_order_filled_meanwhile(self):
        f = LimitFake()
        self.place(f, ttl=1)
        f.cancel_fail = True                                              # биржа отвечает «ордера нет»: он исполнился
        f.fill()
        self.clk.t += 3600
        r = self.sync(f)
        self.assertEqual(len(r["pending"]["filled"]), 1)                  # статус Filled: просто заполнение, без попытки отмены
        self.assertEqual(len(journal._load()[1]), 1)

    def test_cancel_error_and_still_open_reports_error_and_keeps_record(self):
        f = LimitFake()
        self.place(f, ttl=1)
        f.cancel_fail = True
        self.clk.t += 3600
        r = self.sync(f)
        self.assertEqual(len(r["pending"]["errors"]), 1)
        self.assertEqual(len(pending.orders()), 1)

    def test_fill_then_stop_hit_closed_in_same_sync(self):
        f = LimitFake()
        self.place(f, limit_price=1.99)
        f.fill(avg="1.99")
        f.positions = []                                                  # позиция уже закрыта стопом
        f.closed = [{"avgExitPrice": "1.9585", "openFee": "0.006", "closeFee": "0.01", "updatedTime": str((T0 + 600) * 1000)}]
        r = self.sync(f)
        self.assertEqual(len(r["pending"]["filled"]), 1)
        self.assertEqual(r["closed"][0]["reason"], "sl")                  # ts_open = время исполнения, closed-pnl после него найден
        t = list(journal._load()[0].values())[0]
        self.assertTrue(t.get("ts_close"))

    def test_missing_stop_closes_position_and_warns(self):
        f = LimitFake(no_sl=True)
        self.place(f)
        f.fill()
        r = self.sync(f)
        self.assertIn("не оказалось стопа", r["pending"]["filled"][0]["warning"])
        m = [b for b in f.kinds("/v5/order/create") if b.get("reduceOnly")]
        self.assertEqual((m[0]["orderType"], m[0]["reduceOnly"]), ("Market", True))

    def test_status_from_history_when_not_in_realtime(self):
        f = LimitFake()
        self.place(f)
        f.fill()
        f.history = dict(f.orders)
        f.hide_created = True
        r = self.sync(f)
        self.assertEqual(len(r["pending"]["filled"]), 1)


class ExpiryTest(Base):
    def test_ttl_cancels(self):
        f = LimitFake()
        self.place(f, ttl=10)
        r = self.sync(f)
        self.assertEqual((len(r["pending"]["waiting"]), f.kinds("/v5/order/cancel")), (1, []))
        self.clk.t += 11 * 60
        r = self.sync(f)
        self.assertEqual(r["pending"]["cancelled"][0]["reason"], "ttl")
        self.assertEqual(len(f.kinds("/v5/order/cancel")), 1)
        self.assertEqual((pending.orders(), journal._load()[1]), ([], []))
        self.assertEqual(f.only()["orderStatus"], "Cancelled")

    def test_invalidation_levels(self):
        f = LimitFake()
        self.place(f, cancel_above=2.05, cancel_below=1.97)
        f.last = "2.04"
        self.assertEqual(len(self.sync(f)["pending"]["waiting"]), 1)      # уровни не достигнуты
        f.last = "2.05"
        r = self.sync(f)
        self.assertTrue(r["pending"]["cancelled"][0]["reason"].startswith("invalidated_above"))
        self.assertEqual(pending.orders(), [])
        f = LimitFake()
        self.place(f, cancel_below=1.9)
        f.last = "1.89"
        self.assertTrue(self.sync(f)["pending"]["cancelled"][0]["reason"].startswith("invalidated_below"))

    def test_session_stopped_cancels(self):
        f = LimitFake()
        self.place(f)
        session._append({"ev": "stop", "id": self.sess["id"], "ts": "x", "note": ""})
        r = self.sync(f)
        self.assertEqual(r["pending"]["cancelled"][0]["reason"], "session_ended")

    def test_restart_state_from_file(self):
        f = LimitFake()
        self.place(f, limit_price=1.99)
        pid = self.rec()["id"]
        # «перезапуск»: новый клиент, состояние только из файла; ордер на бирже тем временем исполнился
        f.fill()
        f2 = LimitFake()
        f2.orders = f.orders
        f2.positions = f.positions
        r = bb.cmd_sync(self.client(f2), sleep=self.clk.sleep, clock=self.clk.now)
        self.assertEqual(r["pending"]["filled"][0]["id"], pid)

    def test_sync_without_pending_makes_no_extra_calls_and_no_key(self):
        f = LimitFake()
        r = self.sync(f)
        self.assertNotIn("pending", r)
        self.assertEqual(f.calls, [])

    def test_other_env_pending_untouched(self):
        f = LimitFake()
        self.place(f)
        r = self.sync(LimitFake(), env="mainnet")
        self.assertNotIn("pending", r)
        self.assertEqual(len(pending.orders()), 1)


class CancelListCloseTest(Base):
    def test_pending_list(self):
        f = LimitFake()
        self.place(f, ttl=30, cancel_above=2.2)
        self.clk.t += 120
        out = bl.list_pending(self.client(f), now=self.clk.t)
        it = out["pending"][0]
        self.assertEqual((out["count"], it["sym"], it["price"], it["exchange_status"], it["cancel_above"]), (1, "XRP", 1.98, "New", 2.2))
        self.assertAlmostEqual(it["dist_pct"], -1.0, places=2)
        self.assertEqual((it["age_min"], it["ttl_min"], it["left_min"]), (2.0, 30.0, 28.0))

    def test_cancel_by_id_and_all(self):
        f = LimitFake()
        self.place(f)
        pid = self.rec()["id"]
        a = argparse.Namespace(id="nope", all_pending=False)
        self.assertFalse(bl.cmd_cancel(self.client(f), a, self.clk.sleep)["ok"])
        r = bl.cmd_cancel(self.client(f), argparse.Namespace(id=pid, all_pending=False), self.clk.sleep)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["cancelled"][0]["reason"], "manual")
        self.assertEqual(pending.orders(), [])
        self.assertEqual(f.only()["orderStatus"], "Cancelled")
        self.place(LimitFake())
        r = bl.cmd_cancel(self.client(LimitFake()), argparse.Namespace(id=None, all_pending=True), self.clk.sleep)
        self.assertFalse(r["ok"])                                         # на этой «бирже» ордера нет: ошибка видна, запись остаётся
        self.assertEqual(len(pending.orders()), 1)

    def test_cancel_after_fill_journals_instead(self):
        f = LimitFake()
        self.place(f)
        f.fill()
        r = bl.cmd_cancel(self.client(f), argparse.Namespace(id=None, all_pending=True), self.clk.sleep)
        self.assertEqual(len(r["filled"]), 1)
        self.assertEqual(len(journal._load()[1]), 1)

    def test_cancel_requires_target(self):
        self.assertFalse(bl.cmd_cancel(self.client(LimitFake()), argparse.Namespace(id=None, all_pending=False))["ok"])

    def test_close_cancels_waiting_limit(self):
        f = LimitFake()
        self.place(f)
        r = bb.cmd_close(self.client(f), self.args(), sleep=self.clk.sleep)
        self.assertFalse(r["ok"])                                         # позиции нет
        self.assertEqual(pending.orders(), [])
        self.assertEqual(f.only()["orderStatus"], "Cancelled")


class IdeaLinkTest(Base):
    def test_idea_watching_then_taken(self):
        rec = ideas.add_idea("XRP", "long", "ретест 1.98")
        f = LimitFake()
        self.place(f, idea=rec["id"])
        self.assertEqual(ideas.load_ideas()[0]["status"], "watching")
        f.fill()
        self.sync(f)
        i = ideas.load_ideas()[0]
        self.assertEqual(i["status"], "taken")
        self.assertEqual(i["trade"], list(journal._load()[0])[0])
        self.assertEqual(list(journal._load()[0].values())[0]["idea"], rec["id"])

    def test_idea_expired_on_ttl_and_unknown_idea_harmless(self):
        rec = ideas.add_idea("XRP", "long", "x")
        f = LimitFake()
        self.place(f, idea=rec["id"], ttl=1)
        self.clk.t += 120
        self.sync(f)
        self.assertEqual(ideas.load_ideas()[0]["status"], "expired")
        r = self.place(LimitFake(), idea="I999")
        self.assertTrue(r["ok"])


class AccountingTest(Base):
    def pend(self, sym, side="long", risk=0.2, mode="live", session_id=None):
        pending.put({"id": "PM-%s-1027-120000" % sym, "env": "mainnet", "mode": mode, "sym": sym, "side": side, "order_id": "X", "link_id": "x", "status": "open",
                     "price": 100.0, "qty": "1", "sl": 98.0, "tp": 104.0, "lev": 5, "margin": 2.0, "risk_usdt": risk, "ts": 1.0, "expires": 9e9, "session": session_id})

    def a(self, **kw):
        d = dict(sym="SOL", side="long", entry=100.0, sl=98.0, tp=104.0, equity=40.0, free=30.0, score=1.0, regime="range", min_quote=0.0, contract_max_lev=50)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_pending_counts_as_open_risk_and_slot(self):
        base = guard.check(self.a(), PROF, "2026-10-27", ctx={"venue": "bybit"})
        self.pend("DOGE", risk=0.5)
        st = guard.day_state(PROF, "2026-10-27")
        self.assertEqual((st["open_count"], st["pending_count"], st["open_risk"]), (0, 1, 0.5))
        r = guard.check(self.a(), PROF, "2026-10-27", ctx={"venue": "bybit"})
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["risk_usdt"], base["risk_usdt"])
        self.pend("ADA")
        r = guard.check(self.a(sym="LTC"), PROF, "2026-10-27", ctx={"venue": "bybit"})
        self.assertFalse(r["ok"])                                         # max_open_positions = 2: два ждущих лимита занимают оба слота
        self.assertIn("ждущих лимитов 2", r["reasons"][0])

    def test_pending_slot_plus_open_position(self):
        journal._append({"ev": "open", "id": "o", "sym": "AAA", "side": "long", "mode": "live", "risk_usdt": 0.1, "notional": 10, "entry": 1, "ts_open": "x"})
        self.assertTrue(guard.check(self.a(), PROF, "2026-10-27")["ok"])
        self.pend("DOGE")
        self.assertFalse(guard.check(self.a(), PROF, "2026-10-27")["ok"])

    def test_pending_reduces_daily_room(self):
        self.pend("DOGE", risk=PROF["daily_loss_limit_usdt"] - 0.01)
        r = guard.check(self.a(sym="LTC"), PROF, "2026-10-27")
        self.assertTrue(r["ok"], r)
        self.assertLess(r["risk_usdt"], 0.02)                             # бюджет дня почти исчерпан ждущими заявками
        self.pend("ADA", risk=0.5)
        self.assertFalse(guard.check(self.a(sym="LTC"), PROF, "2026-10-27")["ok"])

    def test_same_symbol_pending_blocks(self):
        self.pend("SOL")
        r = guard.check(self.a(), PROF, "2026-10-27")
        self.assertFalse(r["ok"])
        self.assertTrue(any("pending_limit_exists_on_symbol" in x for x in r["reasons"]))

    def test_correlation_group_counts_pending(self):
        cap = PROF["corr_cap_mult"] * PROF["risk_per_trade_frac"] * 40.0
        self.pend("ETH", risk=cap + 0.01)                                 # ждущий лонг ETH уже съел лимит группы BTC/ETH/SOL
        r = guard.check(self.a(), PROF, "2026-10-27")
        self.assertFalse(r["ok"])
        self.assertTrue(any("correlated_same_side" in x for x in r["reasons"]))
        self.assertTrue(guard.check(self.a(side="short", sl=102.0, tp=96.0), PROF, "2026-10-27")["ok"])   # другая сторона не ограничена
        self.assertTrue(guard.check(self.a(sym="DOGE"), PROF, "2026-10-27")["ok"])                      # вне группы тоже

    def test_testnet_pending_not_counted_by_live_guard(self):
        self.pend("DOGE", mode="testnet")
        self.assertEqual(guard.day_state(PROF, "2026-10-27")["pending_count"], 0)

    def test_session_stats_and_max_trades(self):
        sid = self.sess["id"]
        self.pend("DOGE", risk=0.3, session_id=sid)
        st = session.stats(self.sess)
        self.assertEqual((st["pending"], st["open_risk"], st["trades"]), (1, 0.3, 0))
        for i in range(PROF["session_types"]["intraday"]["max_trades"] - 1):
            pending.put({"id": "PM-S%d-1027-120000" % i, "env": "mainnet", "mode": "live", "sym": "S%d" % i, "side": "long", "price": 1.0, "risk_usdt": 0.0,
                         "ts": 1.0, "expires": 9e9, "session": sid})
        ok, why = session.verdict(self.sess, PROF)
        self.assertFalse(ok)
        self.assertIn("session_max_trades", why)


class LearningTest(Base):
    pend = AccountingTest.pend

    def test_learning_counts_pending(self):
        cfg = {"active": True, "venue": "bybit", "start": "2000-01-01T00:00:00Z", "max_trades": 2, "max_loss_usdt": 1.0}
        self.assertEqual(learning.progress(cfg)["room"], 1.0)
        self.pend("DOGE", risk=0.4)
        p = learning.progress(cfg)
        self.assertEqual((p["open_risk"], p["room"], p["reasons"]), (0.4, 0.6, []))
        self.pend("ADA", risk=0.7)
        self.assertEqual(len(learning.progress(cfg)["reasons"]), 2)       # лимит сделок и лимит убытка


class StoreTest(Base):
    def test_atomic_save_and_corrupt_file(self):
        pending.put({"id": "PT-A-1027-120000", "ts": 1.0})
        self.assertEqual([r["id"] for r in pending.orders()], ["PT-A-1027-120000"])
        self.assertFalse([n for n in os.listdir(self.dir) if ".tmp." in n or n.endswith(".lock")])
        with open(pending.PATH, "w") as fh:
            fh.write("{битый")
        self.assertEqual(pending.orders(), [])                            # битый файл не роняет guard

    def test_lock_is_exclusive_reentrant_and_stale_released(self):
        with pending.lock():
            with pending.lock():
                pass
            self.assertTrue(os.path.exists(pending.PATH + ".lock"))
        self.assertFalse(os.path.exists(pending.PATH + ".lock"))
        open(pending.PATH + ".lock", "w").close()
        with self.assertRaises(TimeoutError):
            with pending.lock(wait=0.3):
                pass
        os.utime(pending.PATH + ".lock", (1, 1))                          # брошенная блокировка (процесс упал)
        with pending.lock(wait=0.3):
            pass

    def test_lock_of_dead_process_released_at_once(self):
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(pending.PATH + ".lock", "w") as fh:
            fh.write(str(p.pid))                                          # pid завершённого процесса
        if os.name == "posix":
            with pending.lock(wait=0.3):
                pass
        else:
            os.remove(pending.PATH + ".lock")

    def test_sync_skips_when_locked(self):
        self.place(LimitFake())
        orig = pending.lock
        pending.lock = lambda wait=0.0, **kw: orig(wait=0.0)
        try:
            open(pending.PATH + ".lock", "w").close()
            r = self.sync(LimitFake())
            self.assertIn("skipped", r["pending"])
        finally:
            pending.lock = orig
            os.remove(pending.PATH + ".lock")

    def test_link_id_recognition(self):
        self.assertEqual(pending.link_id("PT-XRP-1027-120000"), "PT-XRP-1027-120000")
        self.assertEqual(pending.link_id("PM-SOL-1027-120000-2"), "PM-SOL-1027-120000-2")
        for bad in (None, "", "abc", "P-XRP", 5):
            self.assertIsNone(pending.link_id(bad))
        self.assertTrue(pending.new_id("testnet", "XRP", 1700000000).startswith("PT-XRP-"))


class WatchEventsTest(Base):
    def test_execution_partial_and_order_cancel_carry_pending_id(self):
        p = bs.PrivateParser()
        ev = p.handle({"topic": "execution", "data": [{"category": "linear", "symbol": "XRPUSDT", "side": "Buy", "execPrice": "1.98", "execQty": "3", "execFee": "0.001",
                                                       "orderId": "O1", "execId": "e1", "isMaker": True, "orderLinkId": "PT-XRP-1027-120000", "leavesQty": "6"}]})
        self.assertEqual((ev[0]["event"], ev[0]["pending_id"], ev[0]["partial"], ev[0]["leaves"]), ("fill", "PT-XRP-1027-120000", True, 6.0))
        ev = p.handle({"topic": "execution", "data": [{"category": "linear", "symbol": "XRPUSDT", "side": "Buy", "execPrice": "1.98", "execQty": "6", "orderId": "O1",
                                                       "execId": "e2", "orderLinkId": "PT-XRP-1027-120000", "leavesQty": "0"}]})
        self.assertFalse(ev[0]["partial"])
        ev = p.handle({"topic": "order", "data": [{"symbol": "XRPUSDT", "side": "Buy", "orderId": "O1", "orderStatus": "Cancelled", "orderLinkId": "PT-XRP-1027-120000",
                                                   "cancelType": "CancelByUser", "orderType": "Limit", "price": "1.98", "qty": "9", "cumExecQty": "0"}]})
        self.assertEqual((ev[0]["event"], ev[0]["pending_id"]), ("order_cancelled", "PT-XRP-1027-120000"))
        ev = p.handle({"topic": "execution", "data": [{"category": "linear", "symbol": "XRPUSDT", "side": "Buy", "execPrice": "1", "execQty": "1", "execId": "e3"}]})
        self.assertNotIn("pending_id", ev[0])                             # обычное исполнение без привязки к заявке не меняется

    def test_pending_events_mapping(self):
        res = {"filled": [{"id": "PT-X", "sym": "XRP", "side": "long", "qty": "3", "planned_qty": "9", "partial": True, "avg": 1.98, "journal_id": "J", "warning": "w",
                           "remainder_cancelled": "partial_fill"}],
               "cancelled": [{"id": "PT-Y", "sym": "SOL", "side": "short", "reason": "ttl"}], "waiting": [{"id": "PT-Z"}], "errors": [{"id": "PT-E", "error": "boom"}]}
        evs = bs.pending_events(res)
        self.assertEqual([e["event"] for e in evs], ["pending_filled", "pending_cancelled", "error"])
        self.assertEqual((evs[0]["partial"], evs[0]["warning"], evs[1]["reason"], evs[1]["symbol"]), (True, "w", "ttl", "SOLUSDT"))
        self.assertEqual(bs.pending_events(None), [])
        self.assertEqual(bs.pending_events({"skipped": "x"}), [])

    def test_levels_from_pending_and_tagged_level_events(self):
        pending.put({"id": "PT-XRP-1027-120000", "env": "testnet", "sym": "XRP", "side": "long", "price": 1.98, "cancel_above": 2.1, "cancel_below": 1.9, "ts": 1.0})
        pending.put({"id": "PT-SOL-1027-120000", "env": "testnet", "sym": "SOL", "side": "short", "price": 150.0, "ts": 2.0})
        pending.put({"id": "PM-ETH-1027-120000", "env": "mainnet", "sym": "ETH", "side": "long", "price": 1000.0, "ts": 3.0})
        above, below, tags, syms = bs.pending_levels("testnet")
        self.assertEqual(syms, {"XRPUSDT", "SOLUSDT"})
        self.assertEqual((above["SOLUSDT"], below["XRPUSDT"], above["XRPUSDT"]), ([150.0], [1.98, 1.9], [2.1]))
        w = bs.PriceWatch(above, below)
        w.tags.update(tags)
        evs = w.on_price("XRPUSDT", 1.97, 0.0)
        self.assertEqual([(e["level"], e["pending_id"], e["role"]) for e in evs], [(1.98, "PT-XRP-1027-120000", "limit")])
        evs = w.on_price("XRPUSDT", 1.89, 1.0)
        self.assertEqual((evs[0]["level"], evs[0]["role"]), (1.9, "invalidation"))

    def test_touch_of_invalidation_level_forces_cancel_immediately(self):
        import asyncio
        out, calls = [], []

        def tick(force=None, ident=None):
            calls.append((force, ident))
            return {"filled": [], "cancelled": [{"id": ident, "sym": "ADA", "side": "long", "reason": force}], "waiting": [], "errors": []} if force else None

        async def go():
            q = asyncio.Queue()
            q.put_nowait(("PT-ADA-1027-120000", "invalidated_above 0.2042 (касание)"))
            t = asyncio.ensure_future(bs.pending_task(None, out.append, tick, 5.0, q))
            await asyncio.sleep(0.3)
            t.cancel()
        asyncio.run(go())
        self.assertEqual(calls, [("invalidated_above 0.2042 (касание)", "PT-ADA-1027-120000")])    # без ожидания 5 секунд
        self.assertEqual(out[0]["event"], "pending_cancelled")
        # forced-причина доходит до process_one: открытая заявка снимается с этой причиной
        f = LimitFake()
        self.place(f, ttl=30, cancel_above=2.2)
        r = bl.sync_pending(self.client(f), now=self.clk.t, sleep=self.clk.sleep, force="invalidated_above 2.2 (касание)", ident=self.rec()["id"])
        self.assertEqual(r["cancelled"][0]["reason"], "invalidated_above 2.2 (касание)")

    def test_run_watch_pending_only_runs_tick_and_emits(self):
        import asyncio
        pending.put({"id": "PT-XRP-1027-120000", "env": "testnet", "sym": "XRP", "side": "long", "price": 1.98, "ts": 1.0})
        out = []
        a = argparse.Namespace(symbols="", above=[], below=[], move_pct=0.0, move_window=60.0, private=False, down_after=30.0, max_seconds=0.6,
                               pending=True, pending_every=0.2)
        calls = []

        def tick():
            calls.append(1)
            return {"filled": [], "cancelled": [{"id": "PT-XRP-1027-120000", "sym": "XRP", "side": "long", "reason": "ttl"}], "waiting": [], "errors": []} if len(calls) == 1 else None
        c = self.client(LimitFake())
        code = asyncio.run(bs.run_watch(c, a, out.append, urls={"private": "ws://x", "public": "ws://127.0.0.1:1"}, opts={"allow_plain": True, "backoff_base": 0.1, "backoff_max": 0.1},
                                        pending_tick=tick))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(calls), 2)
        names = [e["event"] for e in out]
        self.assertIn("watch_start", names)
        self.assertIn("pending_cancelled", names)
        self.assertEqual([e for e in out if e["event"] == "watch_start"][0]["symbols"], ["XRPUSDT"])


if __name__ == "__main__":
    unittest.main()
