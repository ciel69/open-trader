import io
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import ideas
from tb.marketdata import hotscan as hs
from tb.marketdata import ideawatch as iw


def tk(sym, price, turn=50e6, chg=0.01, bid=None, ask=None, oi=1000.0, fr=0.0001):
    bid = bid if bid is not None else price * 0.9998
    ask = ask if ask is not None else price * 1.0002
    return {"symbol": sym, "lastPrice": str(price), "bid1Price": str(bid), "ask1Price": str(ask), "turnover24h": str(turn),
            "price24hPcnt": str(chg), "openInterest": str(oi), "fundingRate": str(fr)}


def ins(sym, min_qty="1", step="1", tick="0.001", status="Trading", min_notional="5", ctype="LinearPerpetual"):
    return {"symbol": sym, "status": status, "quoteCoin": "USDT", "contractType": ctype,
            "lotSizeFilter": {"minOrderQty": min_qty, "qtyStep": step, "minNotionalValue": min_notional},
            "priceFilter": {"tickSize": tick}, "leverageFilter": {"maxLeverage": "50"}}


INS = hs.parse_instruments([ins("AAAUSDT"), ins("BBBUSDT"), ins("BTCUSDT", min_qty="0.001", step="0.001", tick="0.1"),
                            ins("CCCUSDT", status="PreLaunch"), ins("ETHUSDT", min_qty="0.01", step="0.01", tick="0.01"),
                            ins("BTC-27MAR26", ctype="LinearFutures")])


class ParseTest(unittest.TestCase):
    def test_tickers(self):
        r = hs.parse_tickers({"list": [tk("AAAUSDT", 2.0, chg=0.05), tk("BTCPERP", 1), tk("BTC-27MAR26USDT", 5),
                                       {"symbol": "ZZZUSDT", "lastPrice": ""}, tk("USDCUSDT", 1.0)]})
        self.assertIn("AAAUSDT", r)
        self.assertNotIn("BTCPERP", r)
        self.assertNotIn("ZZZUSDT", r)
        self.assertNotIn("BTC-27MAR26USDT", r)
        self.assertAlmostEqual(r["AAAUSDT"]["chg24"], 5.0)
        self.assertAlmostEqual(r["AAAUSDT"]["funding_pct"], 0.01)

    def test_instruments_only_usdt_perp(self):
        self.assertNotIn("BTC-27MAR26", INS)
        self.assertEqual(INS["BTCUSDT"]["min_qty"], 0.001)
        self.assertEqual(INS["AAAUSDT"]["tick"], "0.001")

    def test_spread(self):
        self.assertAlmostEqual(hs.spread_bps(99.99, 100.01), 2.0, places=2)
        self.assertIsNone(hs.spread_bps(0, 100))
        self.assertIsNone(hs.spread_bps(101, 100))


class WindowTest(unittest.TestCase):
    def buf(self, prices, dt=30.0, turns=None):
        return [(i * dt, p, 100.0, (turns[i] if turns else None)) for i, p in enumerate(prices)]

    def test_range_needs_coverage(self):
        b = self.buf([100.0, 101.0, 100.5])          # 60 с накоплено из 300
        self.assertIsNone(hs.window_range(b, 60.0, 300, 0.75))
        self.assertIsNone(hs.window_ret(b, 60.0, 300, 0.75))

    def test_range_and_ret_5m(self):
        prices = [100.0] * 5 + [100.8] * 6                       # 11 замеров по 30 с = 300 с
        b = self.buf(prices)
        self.assertAlmostEqual(hs.window_range(b, 300.0, 300, 0.75), 100 * 0.8 / 100.8, places=4)
        self.assertAlmostEqual(hs.window_ret(b, 300.0, 300, 0.75), 0.8, places=4)

    def test_window_drops_old_samples(self):
        prices = [120.0] + [100.0] * 20                          # старый выброс вне 5-минутного окна
        b = self.buf(prices)
        self.assertAlmostEqual(hs.window_range(b, 600.0, 300, 0.75), 0.0)

    def test_ref_gap_rejected(self):
        b = [(0.0, 100.0, 1.0, None), (2000.0, 101.0, 1.0, None)]
        self.assertIsNone(hs.ref_sample(b, 2000.0, 300, 0.75))

    def test_turnover_surge(self):
        turns = [100e6 + 0.1e6 * i for i in range(11)]           # норма 5 мин = 100e6/288 = 0.347e6, за 300 с набежало 1.0e6
        b = self.buf([1.0] * 11, turns=turns)
        self.assertAlmostEqual(hs.turnover_surge(b, 300.0, turns[-1], 300, 0.75), 1.0e6 / (turns[-1] * 300 / 86400.0), places=3)
        self.assertGreater(hs.turnover_surge(b, 300.0, turns[-1], 300, 0.75), 2.5)
        # оборот не растёт (выпадение старых сделок) -> 0, не отрицательное
        b2 = self.buf([1.0] * 11, turns=[100e6 - 1e5 * i for i in range(11)])
        self.assertEqual(hs.turnover_surge(b2, 300.0, 99e6, 300, 0.75), 0.0)


class ScoreTest(unittest.TestCase):
    P = dict(hs.DEFAULTS)

    def test_score_terms(self):
        s, why = hs.score_row({"range_5m_pct": 0.8, "range_15m_pct": 1.5, "turn_surge_5m": 3.0, "chg_24h_pct": -10.0}, self.P)
        self.assertAlmostEqual(s, 1.0 + 1.0 + 2.0 + 1.0)
        self.assertEqual(len(why), 4)

    def test_none_and_caps(self):
        self.assertEqual(hs.score_row({"range_5m_pct": None, "range_15m_pct": None, "turn_surge_5m": None, "chg_24h_pct": 0.0}, self.P)[0], 0.0)
        s, _ = hs.score_row({"range_5m_pct": 50.0, "range_15m_pct": 50.0, "turn_surge_5m": 99.0, "chg_24h_pct": 90.0}, self.P)
        self.assertAlmostEqual(s, 10.0)


def scan(sc, tickers, now, ins=INS):
    return sc.step(hs.parse_tickers({"list": tickers}), ins, now)


class FilterTest(unittest.TestCase):
    def test_filters_and_flags(self):
        sc = hs.HotScanner({"corr_dummy": 1}, corr={"BTC", "ETH", "SOL"})
        tickers = [tk("AAAUSDT", 2.0, turn=50e6),
                   tk("LOWUSDT", 2.0, turn=1e6), tk("WIDEUSDT", 2.0, turn=50e6, bid=1.9, ask=2.1),
                   tk("CCCUSDT", 2.0, turn=50e6), tk("BTCUSDT", 100000.0, turn=9e9), tk("ETHUSDT", 3000.0, turn=5e9),
                   tk("NOINSUSDT", 2.0, turn=50e6)]
        top, trad, blocked, stats = scan(sc, tickers, 1000.0)
        syms = {r["sym"] for r in trad}
        self.assertEqual(syms, {"AAA"})
        by = {r["sym"]: r for r in blocked}
        self.assertIn("BTC", by)                                  # лот 0.001*100000 = 100 > 15
        self.assertFalse(by["BTC"]["tradable"])
        self.assertTrue(by["BTC"]["corr_group"])
        self.assertTrue(by["ETH"]["corr_group"])                  # лот 0.01*3000 = 30 > 15
        self.assertIn("NOINS", by)                                # нет instruments-info -> не tradable
        for gone in ("LOW", "WIDE", "CCC"):                       # оборот, спред, статус PreLaunch
            self.assertNotIn(gone, syms | set(by))
        self.assertEqual(stats["symbols"], 7)

    def test_row_fields(self):
        sc = hs.HotScanner({}, corr=())
        top, trad, blocked, _ = scan(sc, [tk("AAAUSDT", 2.0)], 1000.0)
        r = top[0]
        for k in ("price", "tick", "qty_step", "min_qty", "min_notional", "spread_bps", "funding_pct", "turn_24h_musd", "chg_24h_pct", "corr_group"):
            self.assertIn(k, r)
        self.assertTrue(r["tradable"])
        self.assertEqual(r["tick"], "0.001")
        self.assertEqual(r["lot_usdt"], 5.0)                      # max(1*2, 5)

    def test_top_limit_and_order(self):
        sc = hs.HotScanner({"top": 2})
        tickers = [tk("AAAUSDT", 2.0, chg=0.02), tk("BBBUSDT", 2.0, chg=0.09), tk("DDDUSDT", 2.0, chg=0.05)]
        ins_ = dict(INS, DDDUSDT=hs.parse_instruments([ins("DDDUSDT")])["DDDUSDT"])
        top, trad, _, _ = scan(sc, tickers, 1000.0, ins_)
        self.assertEqual([r["sym"] for r in top], ["BBB", "DDD"])
        self.assertEqual(len(trad), 3)


def history_scan(sc, steps, ins_=INS):
    """steps: [(now, цена AAA, turnover24h)] -> результат последнего скана."""
    res = None
    for now, px, turn in steps:
        res = scan(sc, [tk("AAAUSDT", px, turn=turn)], now, ins_)
    return res


class WindowIntegrationTest(unittest.TestCase):
    def test_5m_15m_windows_in_scanner(self):
        sc = hs.HotScanner({})
        steps = [(30.0 * i, 2.0 if i < 20 else 2.0 * 1.012, 50e6) for i in range(31)]   # 15 минут, скачок +1.2% на 10-й минуте
        top, trad, _, _ = history_scan(sc, steps)
        r = top[0]
        self.assertIsNone(history_scan(hs.HotScanner({}), steps[:5])[0][0]["range_5m_pct"])
        self.assertEqual(r["range_5m_pct"], 0.0)                  # скачок уже старше 5 минут
        self.assertGreater(r["range_15m_pct"], 1.1)
        self.assertGreater(r["ret_15m_pct"], 1.1)


class GateTest(unittest.TestCase):
    def mk(self, **kw):
        return hs.EventGate(dict(hs.DEFAULTS, **dict({"wake_score": 0.0}, **kw)))

    def row(self, sym, score=3.0, r5=None, surge=None):
        return {"sym": sym, "price": 1.0, "score": score, "reasons": [], "range_5m_pct": r5, "ret_5m_pct": 0.9, "chg_24h_pct": 1.0,
                "turn_24h_musd": 50.0, "turn_surge_5m": surge, "spread_bps": 2.0, "funding_pct": 0.01, "corr_group": False, "tradable": True}

    def test_first_scan_silent_then_enter(self):
        g = self.mk()
        self.assertEqual(g.pick([self.row("A")], [self.row("A")], 0.0), [])
        evs = g.pick([self.row("A"), self.row("B")], [self.row("A"), self.row("B")], 100.0)
        self.assertEqual([(e["type"], e["sym"]) for e in evs], [("hot_enter", "B")])

    def test_enter_needs_score(self):
        g = self.mk()
        g.pick([self.row("A")], [self.row("A")], 0.0)
        self.assertEqual(g.pick([self.row("A"), self.row("B", score=0.5)], [self.row("B", score=0.5)], 100.0), [])

    def test_range_event_and_cooldown(self):
        g = self.mk()
        g.pick([], [], 0.0)
        r = self.row("A", r5=0.9)
        e1 = g.pick([r], [r], 100.0)
        self.assertEqual((e1[0]["type"], e1[0]["direction"]), ("range_5m", "up"))
        self.assertEqual(g.pick([r], [r], 160.0), [])                   # cooldown 1200 с
        self.assertEqual(g.pick([r], [r], 1299.0), [])
        self.assertEqual(len(g.pick([r], [r], 1301.0)), 1)

    def test_quiet_events_below_wake_score(self):
        g = self.mk(wake_score=4.0, global_gap=0.0, max_per_hour=1)
        g.pick([], [], 0.0)
        lo, hi = self.row("A", score=2.5, r5=1.0), self.row("B", score=5.0, r5=1.0)
        evs = g.pick([lo, hi], [lo, hi], 100.0)
        self.assertEqual([(e["sym"], e["wake"]) for e in evs], [("B", True), ("A", False)])    # тихое не тратит лимит будильника
        self.assertTrue(evs[0]["id"].endswith("-B"))
        c = self.row("C", score=6.0, r5=1.0)
        self.assertEqual(g.pick([c], [c], 200.0), [])                                         # часовой лимит громких исчерпан
        q = self.row("D", score=1.0, r5=1.0)
        self.assertEqual([e["sym"] for e in g.pick([q], [q], 200.0)], ["D"])                  # тихие идут дальше
        self.assertEqual(g.pick([q], [q], 300.0), [])                                         # но с cooldown на символ

    def test_range_below_threshold(self):
        g = self.mk()
        g.pick([], [], 0.0)
        r = self.row("A", r5=0.5)
        self.assertEqual([e for e in g.pick([r], [r], 100.0) if e["type"] == "range_5m"], [])

    def test_surge_event(self):
        g = self.mk()
        g.pick([], [], 0.0)
        r = self.row("A", surge=5.0, r5=0.5)
        self.assertEqual(g.pick([r], [r], 100.0)[0]["type"], "turnover_surge")
        flat = self.row("B", surge=9.0, r5=0.05)                       # оборот есть, цена стоит: не событие
        self.assertEqual(g.pick([r], [flat], 400.0), [])

    def test_hourly_cap(self):
        g = self.mk(global_gap=0.0, max_per_scan=5, max_per_hour=3, cooldown=10.0)
        g.pick([], [], 0.0)
        rows = [self.row(c, r5=1.0) for c in "ABCDE"]
        self.assertEqual(len(g.pick(rows, rows, 100.0)), 3)
        self.assertEqual(g.pick(rows, rows, 200.0), [])
        self.assertEqual(len(g.pick(rows, rows, 3800.0)), 3)

    def test_per_scan_limit_and_drop_not_stamped(self):
        g = self.mk(max_per_scan=2, global_gap=0.0)
        g.pick([], [], 0.0)
        rows = [self.row("A", score=5, r5=1.0), self.row("B", score=4, r5=1.0), self.row("C", score=3, r5=1.0)]
        self.assertEqual([e["sym"] for e in g.pick(rows, rows, 100.0)], ["A", "B"])
        self.assertEqual([e["sym"] for e in g.pick(rows, rows, 130.0)], ["C"])   # отброшенное сработало на следующем скане

    def test_global_gap(self):
        g = self.mk(global_gap=60.0)
        g.pick([], [], 0.0)
        a, b = self.row("A", r5=1.0), self.row("B", r5=1.0, score=2.0)
        self.assertEqual(len(g.pick([a, b], [a, b], 100.0)), 1)
        self.assertEqual(g.pick([a, b], [a, b], 130.0), [])
        self.assertEqual([e["sym"] for e in g.pick([a, b], [a, b], 170.0)], ["B"])


class FakeClient:
    env = "mainnet"

    def __init__(self, ticker_pages):
        self.pages, self.calls = ticker_pages, 0

    def call(self, method, path, params=None, auth=True):
        assert auth is False and method == "GET"
        if path == "/v5/market/instruments-info":
            return {"list": [ins("AAAUSDT"), ins("BBBUSDT")]}
        self.calls += 1
        return {"list": self.pages[min(self.calls - 1, len(self.pages) - 1)]}


class PricesFileTest(unittest.TestCase):
    def test_build_prices_all_symbols_compact_atomic(self):
        t = hs.parse_tickers({"list": [dict(tk("DOODUSDT", 0.002035), markPrice="0.002036"), tk("AAAUSDT", 2.0), tk("BTC-27MAR26USDT", 5)]})
        self.assertAlmostEqual(t["DOODUSDT"]["mark"], 0.002036)
        pl = hs.build_prices(t, "mainnet", 1000.0)
        self.assertEqual(pl["prices"], {"DOOD": 0.002035, "AAA": 2.0})
        self.assertEqual(pl["marks"], {"DOOD": 0.002036})
        self.assertEqual((pl["n"], pl["ts_epoch"], pl["env"]), (2, 1000.0, "mainnet"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "runtime", "prices.json")
            hs.write_prices(path, pl)
            raw = open(path, encoding="utf-8").read()
            self.assertNotIn("\n", raw)
            self.assertNotIn(": ", raw)
            self.assertEqual(json.loads(raw)["prices"]["DOOD"], 0.002035)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["prices.json"])

    def test_run_writes_prices_next_to_hot(self):
        with tempfile.TemporaryDirectory() as d:
            hot, log = os.path.join(d, "runtime", "hot.json"), os.path.join(d, "runtime", "ev.jsonl")
            c = FakeClient([[tk("AAAUSDT", 2.0), tk("ZZZUSDT", 0.00123, turn=10.0)]])
            self.assertEqual(hs.run(c, {"interval": 30.0}, False, out=io.StringIO(), hot_path=hot, log_path=log), 0)
            pr = json.load(open(os.path.join(d, "runtime", "prices.json"), encoding="utf-8"))
            self.assertEqual(pr["prices"], {"AAA": 2.0, "ZZZ": 0.00123})      # и монета, не попавшая в hot.json


class RunTest(unittest.TestCase):
    def test_oneshot_and_daemon(self):
        with tempfile.TemporaryDirectory() as d:
            hot, log = os.path.join(d, "runtime", "hot.json"), os.path.join(d, "runtime", "ev.jsonl")
            out = io.StringIO()
            c = FakeClient([[tk("AAAUSDT", 2.0, chg=0.03), tk("BBBUSDT", 1.0)]])
            self.assertEqual(hs.run(c, {}, False, out=out, hot_path=hot, log_path=log), 0)
            self.assertEqual(json.load(open(hot))["top"][0]["sym"] in ("AAA", "BBB"), True)
            self.assertFalse(os.path.exists(hot + ".tmp.%d" % os.getpid()))
            # демон: рынок тихий 6 минут, потом скачок: ровно одно событие range_5m, служебные scan_start/scan_end
            pages = [[tk("AAAUSDT", 2.0), tk("BBBUSDT", 1.0)]] * 12 + [[tk("AAAUSDT", 2.0 * 1.012), tk("BBBUSDT", 1.0)]] * 10
            c = FakeClient(pages)
            clock = {"t": 1000.0}
            out = io.StringIO()
            hs.run(c, {"interval": 30.0, "max_seconds": 600.0, "wake_score": 0.0}, True, out=out, hot_path=hot, log_path=log,
                   clock=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
            evs = [json.loads(x) for x in out.getvalue().splitlines()]
            types = [e["type"] for e in evs]
            self.assertEqual(types[0], "scan_start")
            self.assertEqual(types[-1], "scan_end")
            self.assertEqual(types.count("range_5m"), 1, types)
            self.assertLessEqual(len(evs), 4, types)
            self.assertEqual(len(open(log).read().splitlines()), len(evs))

    def test_quiet_not_printed_but_logged_and_tracked(self):
        with tempfile.TemporaryDirectory() as d:
            hot, log = os.path.join(d, "hot.json"), os.path.join(d, "ev.jsonl")
            pages = [[tk("AAAUSDT", 2.0), tk("BBBUSDT", 1.0)]] * 12 + [[tk("AAAUSDT", 2.0 * 1.012), tk("BBBUSDT", 1.0)]] * 80
            clock, out = {"t": 1000.0}, io.StringIO()
            hs.run(FakeClient(pages), {"interval": 30.0, "max_seconds": 2400.0, "wake_score": 99.0, "outcome_min": "15"}, True, out=out, hot_path=hot,
                   log_path=log, clock=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
            printed = [json.loads(x)["type"] for x in out.getvalue().splitlines()]
            self.assertNotIn("range_5m", printed)
            logged = [json.loads(x) for x in open(log).read().splitlines()]
            q = [e for e in logged if e["type"] == "range_5m"]
            self.assertEqual(len(q), 1)
            self.assertFalse(q[0]["wake"])
            rows = [json.loads(x) for x in open(os.path.join(d, "hot_outcomes.jsonl")).read().splitlines()]
            self.assertEqual([(r["id"], r["h"], r["kind"]) for r in rows], [(q[0]["id"], 15, "event")])
            self.assertEqual(rows[0]["dir"], "up")

    def test_idea_events_in_daemon(self):
        with tempfile.TemporaryDirectory() as d:
            hot, log = os.path.join(d, "hot.json"), os.path.join(d, "ev.jsonl")
            clock, out = {"t": 1000.0}, io.StringIO()
            ideas.append(os.path.join(d, "ideas.jsonl"), {"ev": "idea", "id": "I1", "ts": ideas.iso(1000.0), "ts_epoch": 1000.0, "sym": "AAA", "side": "long", "note": "тест",
                                                          "level": 1.95, "stop": 1.8, "tp": None, "status": "idea", "price": 2.0})
            pages = [[tk("AAAUSDT", 2.0), tk("BBBUSDT", 1.0)]] * 3 + [[tk("AAAUSDT", 1.955), tk("BBBUSDT", 1.0)]] * 3 + [[tk("AAAUSDT", 1.94), tk("BBBUSDT", 1.0)]] * 10
            # wake_score 99: сканерные события тихие, будильник идей это не должно останавливать
            hs.run(FakeClient(pages), {"interval": 30.0, "max_seconds": 400.0, "wake_score": 99.0, "max_per_hour": 0}, True, out=out, hot_path=hot, log_path=log,
                   clock=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
            evs = [json.loads(x) for x in out.getvalue().splitlines()]
            self.assertEqual([e["type"] for e in evs if e["type"].startswith("idea_")], ["idea_near", "idea_level"])
            self.assertEqual(evs[1]["id"], "I1")
            self.assertTrue(all(e["wake"] for e in evs if e["type"].startswith("idea_")))
            logged = [json.loads(x) for x in open(log).read().splitlines()]
            self.assertEqual([e["type"] for e in logged if e["type"].startswith("idea_")], ["idea_near", "idea_level"])
            # статистика пропущенных не видит idea_*: исходов kind=event для них нет
            op = os.path.join(d, "hot_outcomes.jsonl")
            rows = [json.loads(x) for x in open(op).read().splitlines()] if os.path.exists(op) else []
            self.assertFalse([r for r in rows if r.get("kind") == "event" and str(r.get("type", "")).startswith("idea_")])

    def test_error_dedup(self):
        class Bad(FakeClient):
            def call(self, method, path, params=None, auth=True):
                raise OSError("boom")
        with tempfile.TemporaryDirectory() as d:
            clock = {"t": 0.0}
            out = io.StringIO()
            hs.run(Bad([]), {"interval": 30.0, "max_seconds": 300.0}, True, out=out, hot_path=os.path.join(d, "h.json"),
                   log_path=os.path.join(d, "e.jsonl"), clock=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
            types = [json.loads(x)["type"] for x in out.getvalue().splitlines()]
            self.assertEqual(types.count("scan_error"), 1)


T0 = 1793106000.0


class IdeaWatchTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tbiw_")
        self.path = os.path.join(self.d, "ideas.jsonl")
        self.w = iw.IdeaWatcher(self.path, os.path.join(self.d, "idea_watch.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def add(self, price, **kw):
        """идея с ценой записи price (прямая запись, без price_book)."""
        n = len(ideas.read_rows(self.path)) + 1
        rec = {"ev": "idea", "id": "I%d" % n, "ts": ideas.iso(T0), "ts_epoch": T0, "sym": "AAA", "side": "long", "note": "тест", "level": None, "stop": None,
               "tp": None, "tag": None, "status": "idea", "price": price, "src": "agent"}
        rec.update(kw)
        ideas.append(self.path, rec)
        return rec["id"]

    def chk(self, price, dt=0.0, sym="AAAUSDT"):
        return self.w.check({sym: {"price": price}}, T0 + dt)

    def status(self, iid):
        return {i["id"]: i for i in ideas.load_ideas(path=self.path)}[iid]

    def test_near_once_with_hysteresis_and_cooldown(self):
        iid = self.add(110.0, level=100.0, stop=95.0)
        self.assertEqual(self.chk(105.0, 10), [])
        ev = self.chk(100.2, 40)                       # 0.2% от уровня: в зоне 0.3%
        self.assertEqual([(e["type"], e["id"], e["sym"], e["wake"]) for e in ev], [("idea_near", iid, "AAA", True)])
        self.assertAlmostEqual(ev[0]["dist_pct"], 0.2, places=3)
        self.assertEqual(ev[0]["level"], 100.0)
        self.assertEqual(self.chk(100.25, 70), [])      # всё ещё в зоне: тихо
        self.assertEqual(self.chk(100.4, 100), [])      # между зоной и 1.5*зона: ещё «в зоне» (гистерезис)
        self.assertEqual(self.chk(101.0, 130), [])      # вышла из зоны
        self.assertEqual(self.chk(100.1, 160), [])      # вернулась, но cooldown 1800 с не прошёл
        self.chk(101.0, 190)
        self.assertEqual([e["type"] for e in self.chk(100.1, 2200)], ["idea_near"])    # после cooldown снова можно

    def test_level_long_waits_pullback_down(self):
        self.add(110.0, level=100.0, stop=95.0)         # цена записи выше уровня: ждём откат вниз
        self.chk(103.0, 10)
        ev = self.chk(99.9, 40)
        self.assertEqual([e["type"] for e in ev], ["idea_level"])
        self.assertAlmostEqual(ev[0]["dist_pct"], -0.1, places=3)
        self.assertEqual(self.chk(99.0, 70), [])        # один раз
        self.assertEqual(self.chk(100.1, 100), [])      # и near после касания не повторяется

    def test_level_short_waits_up_and_side_from_price_not_side(self):
        self.add(90.0, level=100.0, side="short", stop=105.0)      # цена записи ниже: ждём подъём
        self.assertEqual(self.chk(99.8, 10)[0]["type"], "idea_near")
        self.assertEqual([e["type"] for e in self.chk(100.3, 40)], ["idea_level"])
        # long-идея с ценой записи НИЖЕ уровня (вход на пробое): ждём подъём вверх, а не падение
        w2 = iw.IdeaWatcher(self.path, os.path.join(self.d, "w2.json"))
        self.add(90.0, level=120.0, sym="BBB", stop=80.0)
        self.assertEqual(w2.check({"BBBUSDT": {"price": 85.0}}, T0 + 5), [])
        self.assertEqual([e["type"] for e in w2.check({"BBBUSDT": {"price": 120.5}}, T0 + 35) if e["sym"] == "BBB"], ["idea_level"])

    def test_no_price_falls_back_to_side(self):
        self.add(None, level=100.0, side="short")
        self.assertEqual(self.w.st, {})
        self.chk(97.0, 5)
        self.assertEqual(self.w.st["I1"]["s0"], 1 if 97.0 > 100.0 else -1)      # short без цены записи: ждём подъём (s0=-1)
        self.assertEqual([e["type"] for e in self.chk(100.0, 35)], ["idea_level"])

    def test_invalidated_marks_status_atomically(self):
        iid = self.add(110.0, level=100.0, stop=95.0)
        self.chk(101.0, 10)
        ev = self.chk(94.5, 40)
        self.assertEqual([e["type"] for e in ev], ["idea_invalidated"])
        st = self.status(iid)
        self.assertEqual(st["status"], "invalidated")
        self.assertIn("stop", st["mark_notes"][-1])
        n = len(ideas.read_rows(self.path))
        self.assertEqual(self.chk(94.0, 70), [])        # идея неактивна: больше событий нет
        self.assertEqual(len(ideas.read_rows(self.path)), n)

    def test_stop_after_level_touch_is_not_invalidation(self):
        iid = self.add(110.0, level=100.0, stop=95.0)
        self.assertEqual([e["type"] for e in self.chk(99.9, 10)], ["idea_level"])
        self.assertEqual(self.chk(94.0, 40), [])
        self.assertEqual(self.status(iid)["status"], "idea")

    def test_stop_for_short_above_level(self):
        iid = self.add(90.0, level=100.0, side="short", stop=105.0)
        self.assertEqual([e["type"] for e in self.chk(105.5, 10)], ["idea_invalidated"])
        self.assertEqual(self.status(iid)["status"], "invalidated")

    def test_tp_without_entry_expires(self):
        iid = self.add(110.0, level=100.0, stop=95.0, tp=130.0, status="watching")
        ev = self.chk(131.0, 10)                        # улетела вверх без отката к уровню
        self.assertEqual([e["type"] for e in ev], ["idea_tp"])
        st = self.status(iid)
        self.assertEqual(st["status"], "expired")
        self.assertIn("tp", st["mark_notes"][-1])

    def test_no_level_ignored_and_taken_ignored(self):
        self.add(110.0, stop=95.0, tp=130.0)            # только заметка со stop/tp
        t = self.add(110.0, level=100.0, status="taken")
        self.assertEqual(self.chk(100.0, 10), [])
        self.assertEqual(self.chk(50.0, 40), [])
        self.assertEqual(self.status(t)["status"], "taken")
        self.assertEqual(self.status("I1")["status"], "idea")

    def test_ttl_silent_expiry_default_and_custom(self):
        a = self.add(110.0, level=100.0)                                  # без ttl_h: по умолчанию 24 ч
        b = self.add(110.0, level=100.0, ttl_h=1.0)
        c = self.add(110.0, level=100.0, ttl_h=0)                         # без срока
        d = self.add(110.0)                                               # без level тоже чистится по сроку
        self.assertEqual(self.chk(105.0, 3000), [])
        self.assertEqual([self.status(x)["status"] for x in (a, b, c, d)], ["idea", "idea", "idea", "idea"])
        self.assertEqual(self.chk(105.0, 3601), [])                       # ttl 1 ч истёк, события нет
        self.assertEqual([self.status(x)["status"] for x in (a, b, c, d)], ["idea", "expired", "idea", "idea"])
        self.assertEqual(self.chk(105.0, 24 * 3600 + 1), [])
        self.assertEqual([self.status(x)["status"] for x in (a, b, c, d)], ["expired", "expired", "idea", "expired"])

    def test_mark_if_active_does_not_override_agent(self):
        iid = self.add(110.0, level=100.0, stop=95.0)
        self.chk(101.0, 10)
        ideas.append(self.path, {"ev": "mark", "id": iid, "ts": ideas.iso(T0), "status": "taken", "note": "вошли", "trade": "T1"})
        self.assertEqual(self.chk(94.0, 40), [])                          # агент уже взял идею: не инвалидируем
        self.assertEqual(self.status(iid)["status"], "taken")
        self.assertIsNone(ideas.mark_if_active(iid, "expired", path=self.path))
        self.assertIsNone(ideas.mark_if_active("I99", "expired", path=self.path))

    def test_state_persists_across_restart(self):
        self.add(110.0, level=100.0)
        self.assertEqual([e["type"] for e in self.chk(99.9, 10)], ["idea_level"])
        w2 = iw.IdeaWatcher(self.path, os.path.join(self.d, "idea_watch.json"))
        self.assertEqual(w2.check({"AAAUSDT": {"price": 99.0}}, T0 + 40), [])

    def test_cache_by_mtime(self):
        self.add(110.0, level=100.0)
        self.chk(105.0, 1)
        calls = []
        orig = ideas.load_ideas
        ideas.load_ideas = lambda *a, **k: calls.append(1) or orig(*a, **k)
        try:
            self.chk(105.0, 31)
            self.chk(105.0, 61)
            self.assertEqual(calls, [])
            self.add(110.0, level=50.0, sym="BBB")
            self.chk(105.0, 91)
            self.assertEqual(len(calls), 1)
        finally:
            ideas.load_ideas = orig

    def test_missing_ticker_and_file_are_quiet(self):
        w = iw.IdeaWatcher(os.path.join(self.d, "none.jsonl"), os.path.join(self.d, "s.json"))
        self.assertEqual(w.check({}, T0), [])
        self.add(110.0, level=100.0, sym="ZZZ")
        self.assertEqual(self.chk(100.0, 5), [])


if __name__ == "__main__":
    unittest.main()
