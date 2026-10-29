"""Ордерфлоу: книга заявок, CVD, дисбаланс, устойчивые стены, расхождение, разрыв с Binance, файлы и цикл на фейковом сокете. Форматы сообщений из живой проверки Bybit. Без сети."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import bus
from tb.marketdata import flowwatch as F
from tb.platform import scansvc

NOW = 1793270000.0


def snap(sym, bids, asks, u=10):
    return {"topic": "orderbook.200.%sUSDT" % sym, "type": "snapshot", "data": {"s": sym + "USDT", "b": [[str(p), str(s)] for p, s in bids], "a": [[str(p), str(s)] for p, s in asks], "u": u, "seq": 1}}


def delta(sym, bids=(), asks=(), u=11):
    return {"topic": "orderbook.200.%sUSDT" % sym, "type": "delta", "data": {"s": sym + "USDT", "b": [[str(p), str(s)] for p, s in bids], "a": [[str(p), str(s)] for p, s in asks], "u": u, "seq": 2}}


def trades(sym, rows, t0=NOW):
    return {"topic": "publicTrade.%sUSDT" % sym, "type": "snapshot", "data": [{"T": int((t0 + dt) * 1000), "s": sym + "USDT", "S": side, "v": str(v), "p": str(p)} for dt, side, v, p in rows]}


def ladder(mid, n=30, size=1.0, tick=1.0):
    bids = [(mid - tick * (i + 1), size) for i in range(n)]
    asks = [(mid + tick * (i + 1), size) for i in range(n)]
    return bids, asks


class Book(unittest.TestCase):
    def test_snapshot_delta_and_delete(self):
        b = F.OrderBook()
        bids, asks = ladder(100.0)
        b.apply(snap("X", bids, asks)["data"], "snapshot")
        self.assertEqual(b.best(), (99.0, 101.0))
        self.assertAlmostEqual(b.mid(), 100.0)
        b.apply(delta("X", bids=[(99.0, 0), (99.5, 3.0)], asks=[(101.0, 2.0)])["data"], "delta")
        self.assertEqual(b.best(), (99.5, 101.0))                       # размер 0 удалил уровень, новый лучший bid
        self.assertEqual(b.a[101.0], 2.0)
        b.apply({"b": [], "a": [], "u": 1}, "delta")                    # u == 1: сброс книги
        self.assertFalse(b.ready)

    def test_imbalance_and_spread(self):
        b = F.OrderBook()
        bids, asks = ladder(100.0, size=1.0)
        bids = [(p, 3.0) for p, _ in bids]                              # bid втрое плотнее
        b.apply(snap("X", bids, asks)["data"], "snapshot")
        bid, ask, imb = b.imbalance(1.0)
        self.assertGreater(bid, ask)
        self.assertAlmostEqual(imb, 50.0, delta=2.0)                    # (3-1)/(3+1), взвешено по цене уровней
        self.assertAlmostEqual(b.spread_bps(), 200.0, places=0)

    def test_wall_needs_size_over_median_and_min_usd(self):
        b = F.OrderBook()
        bids, asks = ladder(100.0, size=10.0, tick=0.1)
        bids[5] = (bids[5][0], 500.0)                                   # стена 500 монет * 99.4 = около 49 700 USD
        b.apply(snap("X", bids, asks)["data"], "snapshot")
        w = b.wall_candidates(5.0, 6.0, 10000.0)
        self.assertEqual((w[0][0], round(w[0][1], 1)), ("b", 99.4))
        self.assertEqual(b.wall_candidates(5.0, 6.0, 100000.0), [])     # меньше min_usd


class TapeTests(unittest.TestCase):
    def test_cvd_window_and_big_prints(self):
        t = F.Tape()
        t.add(trades("X", [(0, "Buy", 10, 100.0), (1, "Sell", 4, 100.0), (2, "Buy", 1500, 100.0)])["data"], NOW)
        w = t.window(NOW + 3, 60, 100000.0)
        self.assertAlmostEqual(w["buy"], 1000.0 + 150000.0)
        self.assertAlmostEqual(w["sell"], 400.0)
        self.assertEqual((w["n"], w["big_n"]), (3, 1))
        self.assertGreater(w["pct"], 99.0)
        self.assertEqual(t.window(NOW + 500, 60, 1e5)["n"], 0)           # окно 60 с: старое не считается

    def test_garbage_trades_ignored(self):
        t = F.Tape()
        self.assertEqual(t.add([{"p": "x"}, {"p": "1", "v": "1", "S": "Hold"}, "junk", {"p": "0", "v": "1", "S": "Buy"}], NOW), 0)

    def test_ret_needs_history(self):
        t = F.Tape()
        t.add(trades("X", [(0, "Buy", 1, 100.0)])["data"], NOW)
        self.assertIsNone(t.ret(NOW + 60, 300))
        for k in range(1, 301):
            t.add(trades("X", [(k, "Buy", 1, 100.0 + k * 0.01)], )["data"], NOW)
        self.assertAlmostEqual(t.ret(NOW + 300, 300), 3.0, places=1)


class Detect(unittest.TestCase):
    def feed(self, fb, sym, price_path, side, v=100.0):
        for k, p in enumerate(price_path):
            fb.handle(trades(sym, [(k, side, v, p)]), NOW + k)

    def test_cvd_divergence_price_up_sellers_aggressive(self):
        fb = F.FlowBook({"min_flow_usd": 100000})
        path = [100.0 + k * 0.005 for k in range(301)]                      # +1.5% за 5 минут
        for k, p in enumerate(path):
            fb.handle(trades("X", [(k, "Sell" if k % 4 else "Buy", 40.0, p)]), NOW + k)         # 75% продаж по объёму
        ev = fb.detect(NOW + 300)
        d = [e for e in ev if e["type"] == "cvd_divergence"]
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0]["direction"], d[0]["sym"]), ("up", "X"))
        self.assertLess(d[0]["cvd_pct"], -12)
        self.assertEqual(fb.detect(NOW + 301), [])                           # cooldown

    def test_no_divergence_when_flow_agrees_or_too_small(self):
        fb = F.FlowBook({"min_flow_usd": 100000})
        for k in range(301):
            fb.handle(trades("X", [(k, "Buy", 40.0, 100.0 + k * 0.005)]), NOW + k)             # растёт и покупают: согласие
        self.assertEqual(fb.detect(NOW + 300), [])
        small = F.FlowBook({"min_flow_usd": 1e9})
        for k in range(301):
            small.handle(trades("Y", [(k, "Sell", 1.0, 100.0 + k * 0.005)]), NOW + k)
        self.assertEqual(small.detect(NOW + 300), [])

    def test_wall_event_only_after_persisting_and_near_price(self):
        fb = F.FlowBook({"min_wall_usd": 20000, "persist_s": 30, "wall_range_pct": 2.0})
        bids, asks = ladder(100.0, size=10.0, tick=0.1)
        asks[0] = (asks[0][0], 400.0)                                           # стена на ask рядом с ценой
        fb.handle(snap("X", bids, asks), NOW)
        self.assertEqual(fb.detect(NOW + 1), [])                                # только появилась: может мигать
        ev = fb.detect(NOW + 31)
        w = [e for e in ev if e["type"] == "book_wall"]
        self.assertEqual((w[0]["wall_side"], w[0]["sym"]), ("ask", "X"))
        self.assertGreater(w[0]["wall_usd"], 20000)

    def test_flickering_wall_is_dropped(self):
        fb = F.FlowBook({"min_wall_usd": 20000, "persist_s": 30, "wall_range_pct": 2.0})
        bids, asks = ladder(100.0, size=10.0, tick=0.1)
        big = [(asks[0][0], 400.0)] + asks[1:]
        fb.handle(snap("X", bids, big), NOW)
        fb.detect(NOW + 5)
        fb.handle(delta("X", asks=[(asks[0][0], 10.0)]), NOW + 10)              # убрали стену до порога устойчивости
        fb.detect(NOW + 12)
        fb.handle(delta("X", asks=[(asks[0][0], 400.0)]), NOW + 40)             # вернули: отсчёт заново
        self.assertEqual([e for e in fb.detect(NOW + 41) if e["type"] == "book_wall"], [])

    def test_loud_cap_and_xex_is_always_quiet(self):
        fb = F.FlowBook({"max_loud_per_hour": 1, "min_wall_usd": 20000, "persist_s": 0, "wall_range_pct": 2.0, "xex_gap_bps": 10, "xex_confirm": 2})
        for s in ("A", "B"):
            bids, asks = ladder(100.0, size=10.0, tick=0.1)
            asks[0] = (asks[0][0], 3000.0)
            fb.handle(snap(s, bids, asks), NOW)
        ev = fb.detect(NOW)                                                     # persist_s=0: стена устойчива сразу
        self.assertEqual(sorted(e["wake"] for e in ev if e["type"] == "book_wall"), [False, True])
        for k in range(2):
            fb.set_xex("A", 99.5, NOW + k)                                      # Bybit 100 против Binance 99.5: +50 б.п. два раза подряд
        x = [e for e in fb.detect(NOW + 3) if e["type"] == "xex_gap"]
        self.assertEqual((len(x), x[0]["wake"], x[0]["xex_bps"] > 0), (1, False, True))

    def test_xex_count_resets_on_small_gap_or_sign_flip(self):
        fb = F.FlowBook({"xex_gap_bps": 10})
        bids, asks = ladder(100.0)
        fb.handle(snap("A", bids, asks), NOW)
        fb.set_xex("A", 99.5, NOW)
        fb.set_xex("A", 99.5, NOW + 1)
        self.assertEqual(fb.xex["A"]["n"], 2)
        fb.set_xex("A", 100.5, NOW + 2)                                         # знак сменился
        self.assertEqual(fb.xex["A"]["n"], 1)
        fb.set_xex("A", 100.0, NOW + 3)
        self.assertEqual(fb.xex["A"]["n"], 0)


class Files(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbflow_")
        os.makedirs(os.path.join(self.root, "runtime"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_snapshot_shape_and_publish_writes_flow_json(self):
        r = F.Runner(self.root, {"min_flow_usd": 1000})
        bids, asks = ladder(100.0, size=10.0)
        r.book.handle(snap("X", bids, asks), NOW)
        r.book.handle(trades("X", [(0, "Buy", 50, 100.0), (1, "Sell", 10, 100.1)]), NOW)
        r.publish(NOW + 2)
        with open(paths.runtime("flow.json", root=self.root), encoding="utf-8") as f:
            doc = json.load(f)
        row = doc["syms"][0]
        self.assertEqual(row["sym"], "X")
        self.assertGreater(row["cvd_5m_usd"], 0)
        self.assertIn("imb_pct", row)
        self.assertIn("spread_bps", row)

    def test_read_symbols_always_plus_hot(self):
        with open(paths.runtime("hot.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"top": [{"sym": "INIT", "tradable": True}, {"sym": "BAD", "tradable": False}, {"sym": "BTC", "tradable": True}, {"sym": "AKE"}]}, f)
        self.assertEqual(F.read_symbols(self.root, F.DEFAULTS), ["BTC", "ETH", "SOL", "INIT", "AKE"])
        self.assertEqual(F.read_symbols(self.root, dict(F.DEFAULTS, n_syms=4)), ["BTC", "ETH", "SOL", "INIT"])

    def test_events_reach_publisher_and_bus_with_new_fields(self):
        pub = scansvc.Publisher(self.root, {}, os.path.join(self.root, "runtime", "hot_events.jsonl"), emit=lambda *a: None)
        ev = F.make_event("book_wall", "SOL", 2.0, {"wall_usd": 900000, "wall_dist_pct": 0.12, "wall_side": "bid", "wall_px": 100.0}, 100.0, NOW, True)
        d, sym = pub.convert(dict(ev, type="book_wall"))
        self.assertEqual((d["kind"], d["wall_usd"], d["wall_dist_pct"], d["wall_side"], sym), ("book_wall", 900000, 0.12, "bid", "SOL"))
        seq = bus.emit("scanner_signal", d, sym=sym, root=self.root, now=NOW)
        e = [x for x in bus.read(0, root=self.root) if x["seq"] == seq][0]
        self.assertEqual((e["d"]["kind"], e["d"]["wall_side"]), ("book_wall", "bid"))
        cv = F.make_event("cvd_divergence", "SOL", 1.8, {"cvd_pct": -22.5, "ret_5m_pct": 0.7, "flow_usd": 500000, "direction": "up"}, 100.0, NOW, True)
        d2, _ = pub.convert(dict(cv, type="cvd_divergence"))
        self.assertEqual((d2["kind"], d2["cvd_pct"], d2["dir"], d2["ret_5m_pct"]), ("cvd_divergence", -22.5, "up", 0.7))
        xe = F.make_event("xex_gap", "SOL", 1.2, {"xex_bps": -18.4}, 100.0, NOW, False)
        d3, _ = pub.convert(dict(xe, type="xex_gap"))
        self.assertEqual(d3["xex_bps"], -18.4)


class BriefAndRequests(Files):
    def write_flow(self, ts, syms):
        with open(paths.runtime("flow.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"ts_epoch": ts, "xex_ok": True, "syms": syms}, f)

    def test_flow_for_compact_fresh_and_only_stable_walls(self):
        from tb.core import flowdoc
        self.write_flow(NOW, [{"sym": "SOL", "cvd_5m_pct": -22.5, "cvd_1m_usd": -50000, "big_5m_usd": -120000, "imb_pct": 14.0, "spread_bps": 0.9, "xex_bps": 1.5, "warm_s": 240,
                               "walls": [{"side": "bid", "usd": 900000, "dist_pct": 0.1, "stable": True}, {"side": "ask", "usd": 700000, "dist_pct": 0.2, "stable": False},
                                         {"side": "ask", "usd": 650000, "dist_pct": 0.3, "stable": True}, {"side": "ask", "usd": 600000, "dist_pct": 0.4, "stable": True}]}])
        d = flowdoc.flow_for(self.root, "SOL", NOW + 5)
        self.assertEqual((d["cvd_5m_pct"], d["imb_pct"], d["warm_s"], d["age_s"]), (-22.5, 14.0, 240, 5))
        self.assertEqual([(w["side"], w["usd"]) for w in d["walls"]], [("bid", 900000), ("ask", 650000)])      # мигающая не попала, не больше двух
        self.assertIsNone(flowdoc.flow_for(self.root, "SOL", NOW + 60))                                         # старше 30 с: данных нет
        self.assertIsNone(flowdoc.flow_for(self.root, "ETH", NOW + 5))                                          # монеты нет в потоке
        self.assertIsNone(flowdoc.flow_for(os.path.join(self.root, "none"), "SOL", NOW))

    def test_brief_flow_block_and_market_block_use_single_source(self):
        import types
        from tb.brain import brief
        self.write_flow(NOW, [{"sym": "SOL", "cvd_5m_pct": 31.0, "imb_pct": -8.0, "warm_s": 300}, {"sym": "BTC", "cvd_5m_pct": -12.0, "imb_pct": 5.0}])
        c = types.SimpleNamespace(root=self.root, now=NOW + 3)
        self.assertEqual(brief._b_flow(c, "SOL")["cvd_5m_pct"], 31.0)
        self.assertIsNone(brief._b_flow(c, None))
        self.assertIsNone(brief._b_flow(types.SimpleNamespace(root=None, now=NOW), "SOL"))

    def test_request_extends_symbol_set_and_expires(self):
        F.request(self.root, ["PEPE", "bad sym", "WIF"], NOW)
        got = F.read_symbols(self.root, dict(F.DEFAULTS, n_syms=6), NOW + 10)
        self.assertEqual(got[:3], ["BTC", "ETH", "SOL"])
        self.assertEqual(sorted(got[3:]), ["PEPE", "WIF"])                         # мусорное имя отброшено, запрошенные идут перед горячими
        self.assertEqual(F.read_symbols(self.root, dict(F.DEFAULTS), NOW + F.REQ_TTL_S + 5), ["BTC", "ETH", "SOL"])     # запрос живёт 15 минут
        F.request(self.root, ["NEW"], NOW + F.REQ_TTL_S + 6)
        with open(paths.runtime("flow_req.json", root=self.root), encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["NEW"])                         # просроченные записи вычищены

    def test_requested_come_before_hot_and_respect_cap(self):
        with open(paths.runtime("hot.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"top": [{"sym": "H%d" % i, "tradable": True} for i in range(5)]}, f)
        F.request(self.root, ["REQ"], NOW)
        self.assertEqual(F.read_symbols(self.root, dict(F.DEFAULTS, n_syms=5), NOW + 1), ["BTC", "ETH", "SOL", "REQ", "H0"])

    def test_scanner_signal_requests_flow_for_its_coin(self):
        pub = scansvc.Publisher(self.root, {"flow": {"enabled": True}, "structure": {"enabled": False}}, os.path.join(self.root, "runtime", "hot_events.jsonl"), emit=lambda *a: None)
        pub.publish({"kind": "range_5m", "px": 1.0, "wake": True}, "INIT", NOW)
        self.assertIn("INIT", F.read_symbols(self.root, F.DEFAULTS, NOW + 1))
        off = scansvc.Publisher(self.root + "_x", {"flow": {"enabled": False}, "structure": {"enabled": False}}, os.path.join(self.root, "runtime", "hot_events2.jsonl"), emit=lambda *a: None)
        off.publish({"kind": "range_5m", "px": 1.0, "wake": True}, "WLD", NOW)
        self.assertNotIn("WLD", F.read_symbols(self.root, F.DEFAULTS, NOW + 1))

    def test_snapshot_reports_warm_seconds(self):
        fb = F.FlowBook()
        fb.handle(trades("X", [(0, "Buy", 1, 100.0)]), NOW)
        fb.handle(trades("X", [(90, "Sell", 1, 100.0)]), NOW + 90)
        row = fb.snapshot(NOW + 100)["syms"][0]
        self.assertEqual(row["warm_s"], 100)


class FakeClient(object):
    def __init__(self, url, msgs):
        self.url, self.msgs, self.sent, self.connected, self.subscriptions, self.closed = url, msgs, [], True, [], False

    async def messages(self):
        for m in self.msgs:
            yield json.dumps(m)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class Loop(Files):
    def test_main_subscribes_trades_and_book_and_applies_xex(self):
        holder = {}
        bids, asks = ladder(100.0, size=10.0)

        def factory(url):
            holder["c"] = FakeClient(url, [snap("BTC", bids, asks), trades("BTC", [(0, "Buy", 5, 100.0)])])
            return holder["c"]
        stop = {"v": False}
        r = F.Runner(self.root, {"publish_s": 0.1, "xex_s": 0.1, "always": ["BTC"], "depth": 200}, client_factory=factory, stop=lambda: stop["v"], binance=lambda: {"BTC": 99.5})

        async def go():
            t = asyncio.ensure_future(r.main())
            await asyncio.sleep(1.6)
            stop["v"] = True
            await asyncio.wait_for(t, 5)
        asyncio.run(go())
        r.publish(r.clock())                                                    # финальная запись после остановки: в ней уже есть разрыв
        args = [a for m in holder["c"].sent if m.get("op") == "subscribe" for a in m["args"]]
        self.assertEqual(sorted(args), ["orderbook.200.BTCUSDT", "publicTrade.BTCUSDT"])
        self.assertTrue(r.book.xex_ok)
        self.assertGreater(r.book.xex["BTC"]["bps"], 0)
        with open(paths.runtime("flow.json", root=self.root), encoding="utf-8") as f:
            row = json.load(f)["syms"][0]
        self.assertIn("xex_bps", row)
        self.assertTrue(holder["c"].closed)

    def test_binance_failure_disables_gap_for_a_minute_and_keeps_flow(self):
        def boom():
            raise OSError("451")
        r = F.Runner(self.root, {"always": ["BTC"]}, binance=boom)
        r.subscribed = {"BTC"}
        r.xex_step(NOW)
        self.assertFalse(r.book.xex_ok)
        calls = {"n": 0}
        r.binance = lambda: calls.__setitem__("n", calls["n"] + 1) or {}
        r.xex_step(NOW + 10)
        self.assertEqual(calls["n"], 0)                                          # пауза 60 с
        r.xex_step(NOW + 61)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
