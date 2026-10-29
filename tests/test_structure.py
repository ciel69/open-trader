"""Структура рынка для досье мозга: расчёт по свечам (marketdata/structure.py) и блок `struct` в brief (WP «данные мозга»). Сеть не используется: свечи синтетические."""
import json
import math
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.brain import brief
from tb.logic.candles import CandleBook
from tb.marketdata import structure as S
from tb.dash import model

import test_brief as TB

NOW = TB.NOW                      # 2026-10-28 09:55 UTC
DAY_MS = 86400000
D0 = int(NOW // 86400) * 86400


def price_at(t_s, base=100.0):
    """Плавная цена: синус суточного периода + медленный дрейф вверх."""
    return base + 3.0 * math.sin(t_s / 86400.0 * 2 * math.pi * 1.7) + (t_s - D0) / 86400.0 * 0.8


def rows(tf_min, n, now=NOW, base=100.0, spike=None, turn_mult=None):
    """n свечей tf_min, последняя ФОРМИРУЮЩАЯСЯ (содержит now). spike {start_ms: high} - явный прокол high."""
    step = tf_min * 60
    last = int(now // step) * step
    out = []
    for i in range(n):
        st = last - (n - 1 - i) * step
        o, c = price_at(st, base), price_at(st + step, base)
        h, l = max(o, c) + 0.2, min(o, c) - 0.2
        if spike and st * 1000 in spike:
            h = spike[st * 1000]
        tm = 1.0 if turn_mult is None else turn_mult(st)
        out.append((st * 1000, o, h, l, c, 10.0, 1000.0 * tm))
    return out


def book(spike=None, turn_mult=None, n5=300, n1h=200):
    b = CandleBook()
    b.seed(60, rows(60, n1h, spike=spike), now=NOW)
    b.seed(5, rows(5, n5, turn_mult=turn_mult), now=NOW)
    return b


class TestAnalyze(unittest.TestCase):
    def test_empty_book_is_none(self):
        self.assertIsNone(S.analyze(CandleBook(), NOW))

    def test_day_prev_week_match_bruteforce(self):
        r5, r1h = rows(5, 300), rows(60, 200)
        b = CandleBook()
        b.seed(60, r1h, now=NOW)
        b.seed(5, r5, now=NOW)
        a = S.analyze(b, NOW)
        day = [x for x in r5 if x[0] >= D0 * 1000]
        self.assertEqual(a["day"], [S.sig(max(x[2] for x in day)), S.sig(min(x[3] for x in day))])
        prev = [x for x in r1h if (D0 - 86400) * 1000 <= x[0] < D0 * 1000]
        self.assertEqual(len(prev), 24)
        self.assertEqual(a["pd"], [S.sig(max(x[2] for x in prev)), S.sig(min(x[3] for x in prev)), S.sig(prev[-1][4])])
        week0 = D0 - ((D0 // 86400 + 3) % 7) * 86400
        wk = [x for x in r1h if x[0] >= week0 * 1000]
        self.assertEqual(a["wk"], [S.sig(max(x[2] for x in wk)), S.sig(min(x[3] for x in wk))])
        self.assertEqual(a["px"], S.sig(r5[-1][4]))
        self.assertTrue(0.0 <= a["dpos"] <= 1.0)

    def test_levels_distances_and_order(self):
        a = S.analyze(book(), NOW)
        px = a["px"]
        self.assertTrue(a.get("res") and a.get("sup"))
        a1h = a["a1h_pct"] * px / 100.0
        for side, sign in (("res", 1), ("sup", -1)):
            ds = [x["d_pct"] for x in a[side]]
            self.assertEqual(ds, sorted(ds))                                  # ближайший первым
            for x in a[side]:
                self.assertGreater(sign * (x["px"] - px), 0)
                self.assertAlmostEqual(x["d_pct"], abs(x["px"] - px) / px * 100.0, delta=0.01)
                self.assertAlmostEqual(x["d_atr"], abs(x["px"] - px) / a1h, delta=0.05 + 0.02 * x["d_atr"])
                self.assertIn(x["t"], S.TAGS)

    def test_spike_becomes_nearest_resistance(self):
        def flat(tf, n, spike_at=None):
            step = tf * 60
            last = int(NOW // step) * step
            out = []
            for i in range(n):
                st = last - (n - 1 - i) * step
                out.append((st * 1000, 100.0, 101.0 if st == spike_at else 100.1, 99.9, 100.0, 10.0, 1000.0))
            return out
        b = CandleBook()
        b.seed(60, flat(60, 200, spike_at=int(NOW // 3600) * 3600 - 10 * 3600), now=NOW)
        b.seed(5, flat(5, 300), now=NOW)
        a = S.analyze(b, NOW)
        self.assertEqual(a["px"], 100.0)
        hit = [x for x in a["res"] if x["px"] == 101.0]
        self.assertTrue(hit, a["res"])
        self.assertIn(hit[0]["t"], ("wh", "pdh"))
        self.assertEqual(hit[0]["d_pct"], 1.0)
        self.assertEqual(a["res"][0]["px"], 100.1)                           # ближайший: максимум дня
        self.assertEqual(a["wk"][0], 101.0)

    def test_swing_pivots_feed_levels(self):
        b = book()
        piv = b.swing_levels(60, 3, 3, 8)
        self.assertTrue(piv)
        a = S.analyze(b, NOW)
        allpx = [x["px"] for x in a["res"] + a["sup"]]
        self.assertTrue(set(allpx) & set(S.sig(x) for x in piv + b.swing_levels(5, 3, 3, 8) + [a["wk"][0], a["wk"][1]]))

    def test_close_levels_merge_to_one(self):
        cands = [(101.0, "wh"), (101.05, "s1h"), (103.0, "dh"), (99.0, "wl"), (98.99, "s5m")]
        res, sup = S._levels_around(100.0, cands, 1.0)
        self.assertEqual([x["px"] for x in res], [101.0, 103.0])
        self.assertEqual([x["t"] for x in res], ["wh", "dh"])                  # более приоритетный тег остаётся
        self.assertEqual([x["px"] for x in sup], [99.0])
        self.assertEqual(res[0]["d_atr"], 1.0)

    def test_regime_trend_and_range(self):
        up = CandleBook()
        r = [(st, o, h, l, c, v, t) for (st, o, h, l, c, v, t) in rows(60, 200, base=100.0)]
        # монотонный рост 1ч: +0.5 за час -> тренд вверх
        grow = []
        for i, x in enumerate(r):
            p = 100.0 + 0.5 * i
            grow.append((x[0], p, p + 0.3, p - 0.3, p + 0.5, 10.0, 1000.0))
        up.seed(60, grow, now=NOW)
        up.seed(5, rows(5, 300, base=200.0), now=NOW)
        a = S.analyze(up, NOW)
        self.assertEqual(a["reg"], "trend_up")
        self.assertGreater(a["te"], 1.0)
        down = CandleBook()
        down.seed(60, [(x[0], 200 - (g[1] - 100), 200 - (g[1] - 100) + 0.3, 200 - (g[1] - 100) - 0.3, 200 - (g[4] - 100), 10.0, 1000.0) for x, g in zip(r, grow)], now=NOW)
        down.seed(5, rows(5, 300, base=100.0), now=NOW)
        self.assertEqual(S.analyze(down, NOW)["reg"], "trend_down")
        flat = CandleBook()
        flat.seed(60, [(x[0], 100.0, 100.3, 99.7, 100.0 + (0.1 if i % 2 else -0.1), 10.0, 1000.0) for i, x in enumerate(r)], now=NOW)
        flat.seed(5, rows(5, 300, base=100.0), now=NOW)
        self.assertEqual(S.analyze(flat, NOW)["reg"], "range")

    def test_storm_when_last_hour_is_wild(self):
        r5 = rows(5, 300)
        wild = []
        for i, x in enumerate(r5):
            if i >= len(r5) - 13 and i < len(r5) - 1:
                wild.append((x[0], x[1], x[2] + 4.0, x[3] - 4.0, x[4], x[5], x[6]))
            else:
                wild.append(x)
        b = CandleBook()
        b.seed(60, rows(60, 200), now=NOW)
        b.seed(5, wild, now=NOW)
        a = S.analyze(b, NOW)
        self.assertGreaterEqual(a["vx"], S.STORM_VX)
        self.assertEqual(a["reg"], "storm")

    def test_turnover_x_vs_norm(self):
        a = S.analyze(book(turn_mult=lambda st: 3.0 if st >= int(NOW // 300) * 300 - 12 * 300 else 1.0), NOW)
        self.assertGreater(a["tx"], 2.5)
        self.assertAlmostEqual(S.analyze(book(), NOW)["tx"], 1.0, delta=0.05)

    def test_short_history_degrades_without_error(self):
        a = S.analyze(book(n5=20, n1h=10), NOW)
        self.assertEqual(a["reg"], "unknown")
        self.assertNotIn("wk", a)
        self.assertNotIn("tx", a)
        self.assertIn("px", a)

    def test_only_numbers_and_enums(self):
        a = S.analyze(book(), NOW)
        for k, v in a.items():
            self.assertTrue(isinstance(v, (int, float, list, str)), k)
            if isinstance(v, str):
                self.assertIn(v, S.REGIMES)


class FakeClient(object):
    """Минимальный клиент: kline по символам; BAD падает."""
    def __init__(self, bad=()):
        self.calls, self.bad = [], set(bad)

    def call(self, method, path, params=None, auth=True):
        self.calls.append((path, params["symbol"], params["interval"]))
        if params["symbol"] in self.bad:
            raise RuntimeError("boom")
        n = int(params["limit"])
        iv = int(params["interval"])
        rs = rows(iv, n)
        lst = [[str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), str(r[6])] for r in reversed(rs)]      # Bybit: от новых к старым
        return {"list": lst}


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbstruct_")
        self.t = [NOW]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def hot(self, syms):
        TB.w(paths.runtime("hot.json", root=self.root), {"ts_epoch": NOW, "top": [{"sym": s, "symbol": s + "USDT", "tradable": True} for s in syms]})

    def test_wanted_symbols_base_hot_positions_limit(self):
        self.hot(["PEPE", "KORU", "BAD SYM", "ETH", "A1", "A2", "A3", "A4", "A5"])
        TB.w(paths.exec_snapshot_file(root=self.root), {"positions": [{"sym": "HYPE"}]})
        syms = S.wanted_symbols(self.root)
        self.assertEqual([x[0] for x in syms], ["BTC", "ETH", "SOL", "HYPE", "PEPE", "KORU", "A1", "A2", "A3", "A4", "A5"])     # BAD SYM и дубль ETH пропущены
        self.assertEqual([x[0] for x in S.wanted_symbols(self.root, hot_n=3)], ["BTC", "ETH", "SOL", "HYPE", "PEPE", "KORU", "A1"])
        self.assertEqual(len(S.wanted_symbols(self.root, n=5)), 5)
        self.assertLessEqual(len(S.wanted_symbols(self.root, n=999)), S.HARD_MAX_SYMS)

    def test_requests_go_before_hot_and_expire(self):
        self.hot(["H%d" % i for i in range(10)])
        S.request(self.root, ["CBRS", "XAN", "BTC", "bad sym"], now=NOW)
        self.assertEqual(sorted(S.read_requests(self.root, NOW)), ["CBRS", "XAN"])        # BTC и мусор не пишутся
        syms = [x[0] for x in S.wanted_symbols(self.root, hot_n=2, now=NOW)]
        self.assertEqual(syms[:3], ["BTC", "ETH", "SOL"])
        self.assertEqual(sorted(syms[3:5]), ["CBRS", "XAN"])
        self.assertEqual(syms[5:], ["H0", "H1"])
        self.assertEqual(S.read_requests(self.root, NOW + S.REQ_TTL_S + 1), [])
        self.assertEqual(S.request(self.root, ["CBRS"], now=NOW + 10), 0)                 # свежий запрос не переписывается
        for i in range(10):
            S.request(self.root, ["R%d" % i], now=NOW + i)
        self.assertEqual(len(S.read_requests(self.root, NOW + 20)), S.MAX_REQ)

    def test_request_coin_gets_structure_out_of_turn_and_leaving_coins_are_dropped(self):
        self.hot(["PEPE"])
        cl = FakeClient()
        col = S.Collector(cl, self.root, clock=lambda: self.t[0], sleep=lambda s: None)
        col.step()
        self.assertNotIn("CBRS", col.last["symbols"])
        S.request(self.root, ["CBRS"], now=self.t[0])
        self.assertEqual(col.pending_requests(), ["CBRS"])
        self.t[0] += 6
        n = len(cl.calls)
        doc = col.step()
        self.assertIn("CBRS", doc["symbols"])
        self.assertEqual([c[1] for c in cl.calls[n:]], ["CBRSUSDT", "CBRSUSDT"])             # только новая монета: 1ч и 5м, остальные в кэше
        self.assertEqual(col.pending_requests(), [])
        self.assertIn("ts", doc["symbols"]["CBRS"])
        self.hot([])                                                                       # PEPE ушла из hot, запрос протух
        self.t[0] += S.REQ_TTL_S + 10
        doc = col.step()
        self.assertEqual(sorted(doc["symbols"]), ["BTC", "ETH", "SOL"])
        self.assertEqual(sorted(col.books), ["BTC", "ETH", "SOL"])
        with open(paths.runtime("structure.json", root=self.root), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)["symbols"]), ["BTC", "ETH", "SOL"])

    def test_rate_limits_pause_and_hot_ttl(self):
        self.hot(["PEPE"])
        pauses = []
        cl = FakeClient()
        col = S.Collector(cl, self.root, clock=lambda: self.t[0], sleep=pauses.append)
        col.step()
        self.assertEqual(len(pauses), len(cl.calls))
        self.assertTrue(all(x == S.PAUSE_S for x in pauses))
        n = len(cl.calls)
        self.t[0] += 61                                                                    # базовые обновились, горячая монета ещё нет (hot_every_s=180)
        col.step()
        self.assertEqual({c[1] for c in cl.calls[n:]}, {"BTCUSDT", "ETHUSDT", "SOLUSDT"})
        n = len(cl.calls)
        self.t[0] += 130
        col.step()
        self.assertIn("PEPEUSDT", {c[1] for c in cl.calls[n:]})
        self.assertLessEqual(len(json.dumps(col.last)), 700 * S.MAX_SYMS)

    def test_run_loop_wakes_on_request(self):
        self.hot([])
        t = [NOW]
        steps = []

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s
            if len(steps) == 1 and t[0] > NOW + 3 and not S.read_requests(self.root, t[0]):
                S.request(self.root, ["XAN"], now=t[0])

        orig = S.Collector.step

        def step(self_, *a, **k):
            r = orig(self_, *a, **k)
            steps.append(self_.clock())
            return r
        S.Collector.step = step
        try:
            S.run_loop(FakeClient(), self.root, {"every_s": 600.0, "pause_s": 0.0}, clock=clock, sleep=sleep, stop=lambda: len(steps) >= 2)
        finally:
            S.Collector.step = orig
        self.assertEqual(len(steps), 2)
        self.assertLess(steps[1] - steps[0], 60)                                           # не дожидаясь every_s=600

    def test_step_writes_file_and_survives_bad_symbol(self):
        self.hot(["PEPE", "KORU"])
        cl = FakeClient(bad=("KORUUSDT",))
        col = S.Collector(cl, self.root, clock=lambda: self.t[0], sleep=lambda s: None)
        doc = col.step()
        self.assertEqual(sorted(doc["symbols"]), ["BTC", "ETH", "PEPE", "SOL"])
        self.assertEqual(doc["err"], ["KORU"])
        with open(paths.runtime("structure.json", root=self.root), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["schema"], S.SCHEMA)
        self.assertIn("reg", d["symbols"]["BTC"])
        n = len(cl.calls)
        col.step()                                                           # сразу повторно: кэш по расписанию, новых запросов к успешным символам нет
        self.assertEqual(len([c for c in cl.calls[n:] if c[1] != "KORUUSDT"]), 0)
        self.t[0] += 61
        col.step()
        self.assertTrue(any(c[2] == "5" for c in cl.calls[n:]))
        self.assertFalse(any(c[2] == "60" and c[1] == "BTCUSDT" for c in cl.calls[n:]))   # 1ч реже (h1_every_s)


class TestService(unittest.TestCase):
    def test_run_loop_writes_and_stops(self):
        root = tempfile.mkdtemp(prefix="tbstruct_")
        try:
            n = {"i": 0}

            def stop():
                n["i"] += 1
                return n["i"] > 2

            S.run_loop(FakeClient(), root, {"every_s": 0.0, "h1_every_s": 0.0}, clock=lambda: NOW, sleep=lambda s: None, stop=stop)
            self.assertTrue(os.path.exists(paths.runtime("structure.json", root=root)))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_scansvc_thread_can_be_disabled(self):
        from tb.platform import scansvc
        self.assertIsNone(scansvc._start_structure("/nonexistent", {"structure": {"enabled": False}}, lambda: True))

    def test_defaults_enable_structure(self):
        from tb.platform import services
        self.assertTrue(services.DEFAULTS["scan"]["structure"]["enabled"])


class TestPublisherRequests(unittest.TestCase):
    def test_wake_signal_requests_structure(self):
        from tb.platform import scansvc as SC
        root = tempfile.mkdtemp(prefix="tbstruct_")
        try:
            pub = SC.Publisher(root, {}, os.path.join(root, "ev.jsonl"), clock=lambda: NOW, emit=lambda t, d, sym: None)
            self.assertTrue(pub.publish({"kind": "hot_enter", "wake": True}, "CBRS", now=NOW))
            self.assertTrue(pub.publish({"kind": "idea_tp", "idea": "I1", "wake": True}, "XAN", now=NOW))
            self.assertEqual(S.read_requests(root, NOW), ["CBRS"])
            off = SC.Publisher(root, {"structure": {"enabled": False}}, os.path.join(root, "ev.jsonl"), clock=lambda: NOW, emit=lambda t, d, sym: None)
            off.publish({"kind": "hot_enter", "wake": True}, "BLUR", now=NOW)
            self.assertEqual(S.read_requests(root, NOW), ["CBRS"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestBriefStruct(TB.Base):
    def struct_file(self, now=NOW, **kw):
        col = S.Collector(FakeClient(), self.root, clock=lambda: now, sleep=lambda s: None)
        col.step([(s, s + "USDT") for s in ("BTC", "ETH", "SOL", "DOOD", "BEAT", "SOL")][:5] if not kw.get("syms") else kw["syms"])
        return col

    def test_start_has_struct_and_market_regime_from_candles(self):
        self.struct_file()
        b = self.build()
        st = b["struct"]["s"]
        self.assertEqual([k for k in st][:3], ["BTC", "ETH", "SOL"])
        self.assertIn("DOOD", st)
        row = st["BTC"]
        for k in ("px", "reg", "a5_pct", "a1h_pct", "day", "pd", "wk", "res", "sup", "vx", "tx"):
            self.assertIn(k, row)
        self.assertEqual(len(row["res"][0]), 4)                               # [px, d%, dATR, tag]
        self.assertEqual(row["sp"], 0.04)                                     # спред из market.json
        self.assertEqual(b["market"]["btc"]["regime"], "range")               # фид сказал range: не перетирается
        self.assertNotIn("regime_src", b["market"]["btc"])

    def test_unknown_feed_regime_filled_from_struct(self):
        m = paths.runtime("market.json", root=self.root)
        with open(m, encoding="utf-8") as f:
            d = json.load(f)
        d["symbols"]["BTC"]["regime"] = "unknown"
        TB.w(m, d)
        self.struct_file()
        b = self.build()
        self.assertEqual(b["market"]["btc"]["regime_src"], "1h")
        self.assertIn(b["market"]["btc"]["regime"], ("trend_up", "trend_down", "range", "storm"))

    def test_missing_file_marks_missing_and_does_not_crash(self):
        b = self.build()
        self.assertNotIn("struct", b)
        self.assertIn("struct", b["missing"])
        self.assertIn("market", b)

    def test_broken_file_marks_missing(self):
        TB.w(paths.runtime("structure.json", root=self.root), "{not json")
        self.assertIn("struct", self.build()["missing"])
        TB.w(paths.runtime("structure.json", root=self.root), {"schema": "other", "symbols": {}})
        self.assertIn("struct", self.build()["missing"])

    def test_stale_flag(self):
        self.struct_file(now=NOW - 900)
        b = self.build()
        self.assertEqual(b["struct"]["stale_s"], 900)

    def test_only_whitelisted_values_survive(self):
        self.struct_file()
        p = paths.runtime("structure.json", root=self.root)
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["symbols"]["BTC"]["reg"] = TB.INJ
        d["symbols"]["BTC"]["note"] = TB.INJ
        d["symbols"]["BTC"]["res"][0]["t"] = TB.INJ
        d["symbols"]["BTC"]["day"] = [TB.INJ, 1]
        d["symbols"][TB.INJ.upper()] = dict(d["symbols"]["BTC"])
        TB.w(p, d)
        text = json.dumps(self.build(), ensure_ascii=False)
        self.assertNotIn(TB.INJ, text)
        self.assertNotIn(TB.INJ.upper(), text)

    def test_token_budget_of_block(self):
        self.struct_file(syms=[(s, s + "USDT") for s in ("BTC", "ETH", "SOL", "DOOD", "BEAT", "A1", "A2", "A3")])
        b = self.build()
        self.assertLessEqual(brief.tokens(b["struct"]), brief.STRUCT_BUDGET)
        self.assertLessEqual(brief.tokens(b), brief.DEFAULT_BUDGET + brief.STRUCT_BUDGET)

    def test_block_is_trimmed_by_budget(self):
        self.struct_file(syms=[(s, s + "USDT") for s in ("BTC", "ETH", "SOL", "DOOD", "BEAT", "A1", "A2", "A3")])
        blk = brief._b_struct(brief._Ctx(self.root, NOW), [], budget=300)
        self.assertLessEqual(brief.tokens(blk), 300)
        self.assertIn("BTC", blk["s"])

    def test_event_scope_has_compact_struct(self):
        self.struct_file()
        b = self.build("event")
        self.assertIn("struct", b)
        self.assertLessEqual(len(b["struct"]["s"]), 3)
        self.assertLessEqual(brief.tokens(b["struct"]), brief.STRUCT_EVENT_BUDGET)

    def _signal(self, sym):
        from tb.core import bus
        bus.emit("scanner_signal", {"kind": "hot_enter", "px": 1.0, "wake": True}, sym=sym, root=self.root, now=NOW - 5)

    def test_event_struct_has_event_coin_and_btc_not_alphabet(self):
        self.struct_file(syms=[(s, s + "USDT") for s in ("AKE", "BEAT", "BLUR", "BTC", "ETH", "SOL", "CBRS")])
        self._signal("CBRS")
        b = self.build("event", since=0)
        self.assertEqual(list(b["struct"]["s"])[0], "CBRS")
        self.assertIn("BTC", b["struct"]["s"])
        self.assertNotIn("struct_missing", b)

    def test_event_coin_without_struct_is_flagged_and_requested(self):
        self.struct_file(syms=[(s, s + "USDT") for s in ("BTC", "ETH", "SOL")])
        self._signal("XAN")
        b = self.build("event", since=0)
        self.assertEqual(b["struct_missing"], ["XAN"])
        self.assertNotIn("XAN", b["struct"]["s"])
        self.assertIn("BTC", b["struct"]["s"])
        self.assertEqual(S.read_requests(self.root, NOW), ["XAN"])

    def test_event_without_structure_file_flags_missing(self):
        self._signal("XAN")
        b = self.build("event", since=0)
        self.assertEqual(b["struct_missing"], ["XAN"])
        self.assertIn("struct", b["missing"])

    def test_watch_block_shows_file_size_age_and_waiting(self):
        from tb.brain import dashapi
        self.struct_file(syms=[(s, s + "USDT") for s in ("BTC", "ETH", "SOL", "DOOD")])
        S.request(self.root, ["XAN", "DOOD"], now=NOW - 3)
        st = dashapi.watch(self.root, NOW + 40)["blocks"]["struct"]
        self.assertEqual((st["file_n"], st["age_s"]), (4, 40))
        self.assertEqual(st["requested_wait"], ["XAN"])
        self.assertIn("DOOD", st["syms"])

    def test_levels_block_reads_executor_key_names(self):
        TB.w(paths.levels_file(root=self.root), {"ts_epoch": NOW - 5, "symbols": {"SOL": {"price": 146.9, "atr_5m_14": 0.31, "atr_1h_14": 1.2, "vol_med_5m_20": 55.0,
                                                                                   "day": {"hi": 148.0, "lo": 144.0}, "swing_5m": [145.0], "swing_1h": [143.0, 149.0]}}})
        lv = self.build()["levels"]["SOL"]
        self.assertEqual((lv["px"], lv["atr_5m"], lv["atr_1h"]), (146.9, 0.31, 1.2))
        self.assertEqual(lv["swing_1h"], [143.0, 149.0])


# ---- шлюз и раннер: блоки досье доходят до модели
from _brain import BrainBase       # noqa: E402
from tb.gateway.rpc import Gateway  # noqa: E402

NEW_BLOCKS = ("struct", "limits", "risk_level", "paper_history", "intel", "news_digest", "memory")


class TestGatewayPassesBlocks(BrainBase):
    def setUp(self):
        super(TestGatewayPassesBlocks, self).setUp()
        model._CACHE.clear()
        self.real = Gateway(self.root)
        for name in ("brief_checklist.json",):
            shutil.copy(os.path.join(TB.PROJECT, "config", name), self.p("config", name))
        n = NOW
        TB.w(self.p("runtime", "market.json"), {"ts_epoch": n - 1, "feed_ok": True, "symbols": {"BTC": {"price": 100.0, "book": {"spread_bps": 0.1}}}})
        TB.w(self.p("runtime", "hot.json"), {"ts_epoch": n - 5, "top": [TB.hot_row("DOOD")]})
        TB.w(self.p("runtime", "exec_health.json"), {"ts_epoch": n - 1, "env": "paper", "mode": "paper", "observe": False})
        TB.w(self.p("state", "portfolio.json"), {"venues": {"bybit": {"equity": 39.2, "free": 31.0, "ts": TB.iso(n)}}, "hwm": 41.0})
        S.Collector(FakeClient(), self.root, clock=lambda: n, sleep=lambda s: None).step([("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")])
        facts = {"schema": "tb.facts/1", "ts": n - 10, "sentiment": {"fng": {"v": 51, "cls": "neutral"}}, "events": []}
        TB.w(self.p("runtime", "intel", "facts.json"), facts)
        TB.w(self.p("runtime", "intel", "digest.json"), {"schema": "tb.digest/1", "ts": n - 10, "status": "ok", "stale": False, "items": {
            "N-0123abcd": {"its": n - 600, "ttl_min": 120, "imp": 3, "st": "ok", "type": "listing", "syms": ["DOOD"], "dir": "up", "conf": 0.8}}})

    def test_every_brief_block_reaches_gateway_answer(self):
        direct = brief.build("start", root=self.root, now=NOW)
        for k in ("struct", "limits", "risk_level", "paper_history", "intel", "news_digest"):
            self.assertIn(k, direct, k)
        got = self.real.call("api", "get_brief", {"scope": "start"})
        self.assertTrue(got["ok"], got)
        for k in NEW_BLOCKS:
            if k in direct:
                self.assertIn(k, got, "блок %s не прошёл фильтр выхода шлюза" % k)
        self.assertIn("BTC", got["struct"]["s"])
        self.assertLess(len(json.dumps(got, ensure_ascii=False)), 20000)          # runner.MAX_BRIEF_CHARS

    def test_struct_missing_reaches_gateway_answer(self):
        from tb.core import bus
        bus.emit("scanner_signal", {"kind": "hot_enter", "px": 1.0, "wake": True}, sym="XAN", root=self.root, now=NOW - 5)
        got = self.real.call("api", "get_brief", {"scope": "event", "since": 0})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got.get("struct_missing"), ["XAN"])

    def test_runner_dossier_of_morning_analyst_contains_blocks(self):
        r = self.runner({"ds-pro": []}, gw=self.real)
        role = r.roles.get("morning_analyst")
        self.assertIsNotNone(role)
        meta = {"agent": "api", "profile": "ds-pro", "role": "morning_analyst", "job": "J-1"}
        text, _ctx = r._input(role, {"id": "J-1", "seqs": []}, "api", meta)
        b = json.loads(text.split("<brief>")[1].split("</brief>")[0])
        for k in ("struct", "limits", "risk_level", "intel"):
            self.assertIn(k, b, k)
        self.assertIn("reg", b["struct"]["s"]["BTC"])


if __name__ == "__main__":
    unittest.main()
