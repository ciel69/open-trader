import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import klines as kl
from tb.research import research_volume as rv

T0 = 1760000000 // 300 * 300


def make(n, base=100.0, vol=10.0, sym="AAAUSDT"):
    """Плоская серия: open=close=base, узкий диапазон, постоянный объём."""
    return rv.Series(sym, "crypto", T0, [base] * n, [base * 1.0005] * n, [base * 0.9995] * n, [base] * n, [vol] * n)


def bar(s, i, o, h, l, c, v):
    s.o[i], s.h[i], s.l[i], s.c[i], s.v[i] = o, h, l, c, v


class FeatureTest(unittest.TestCase):
    def test_close_pos(self):
        self.assertAlmostEqual(rv.close_pos(100, 110, 100, 110, 1), 1.0)
        self.assertAlmostEqual(rv.close_pos(100, 110, 99, 100, 1), (100 - 99) / 11.0)
        self.assertAlmostEqual(rv.close_pos(100, 100, 90, 90, -1), 1.0)       # вниз: закрытие у low = у края импульса
        self.assertAlmostEqual(rv.close_pos(100, 101, 90, 100.5, -1), (101 - 100.5) / 11.0)
        self.assertEqual(rv.close_pos(5, 5, 5, 5, 1), 0.5)

    def test_impulse_and_vm(self):
        s = make(400)
        bar(s, 350, 100, 102.5, 99.9, 102.0, 45.0)      # +2%, объём 4.5x медианы 10
        ev = rv.impulses(s)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["j"], e["d"]), (350, 1))
        self.assertAlmostEqual(e["vm"], 4.5)
        self.assertAlmostEqual(e["cp"], (102.0 - 99.9) / (102.5 - 99.9))

    def test_vm_excludes_current_bar_and_needs_history(self):
        s = make(400)
        bar(s, 100, 100, 103, 99.9, 102.0, 50.0)         # j < LOOK: признака нет
        self.assertEqual(rv.impulses(s), [])
        s2 = make(400)
        for i in range(300, 340):                          # дыра: в окне слишком мало баров
            s2.v[i] = None
        for i in range(250, 300):
            s2.v[i] = None
        bar(s2, 340, 100, 103, 99.9, 102.0, 50.0)
        self.assertEqual(rv.impulses(s2), [])

    def test_down_impulse(self):
        s = make(400)
        bar(s, 330, 100, 100.1, 97.0, 98.0, 10.0)
        e = rv.impulses(s)[0]
        self.assertEqual(e["d"], -1)
        self.assertAlmostEqual(e["vm"], 1.0)

    def test_atr_before_impulse(self):
        s = make(400)
        bar(s, 330, 100, 105, 95, 104, 10)
        a = rv.atr_pct(s, 330)
        self.assertAlmostEqual(a, 0.001, places=4)         # диапазон 0.1% плоских баров, импульс не входит
        s.h[320] = None
        self.assertIsNone(rv.atr_pct(s, 330))


class HypTest(unittest.TestCase):
    def ev(self, vm, cp, d=1):
        return {"j": 10, "d": d, "vm": vm, "cp": cp}

    def test_rules(self):
        self.assertEqual(rv._v1(self.ev(3, 0.8), None), 1)
        self.assertIsNone(rv._v1(self.ev(2.9, 0.8), None))
        self.assertEqual(rv._v2(self.ev(3, 0.2, -1), None), 1)    # против импульса вниз = лонг
        self.assertIsNone(rv._v2(self.ev(3, 0.5), None))
        self.assertEqual(rv._v3(self.ev(0.5, 0.9), None), -1)
        self.assertIsNone(rv._v3(self.ev(1.0, 0.9), None))
        self.assertEqual(rv._v5a(self.ev(10, 0.5), None), -1)
        self.assertEqual(rv._v5b(self.ev(10, 0.5), None), 1)
        self.assertIsNone(rv._v5b(self.ev(9.9, 0.5), None))

    def test_v4_decay(self):
        s = make(20)
        s.v[10], s.v[11], s.v[12], s.v[13] = 40, 30, 20, 10
        self.assertEqual(rv._v4(self.ev(4, 1), s), -1)
        s.v[13] = 25
        self.assertIsNone(rv._v4(self.ev(4, 1), s))
        s.v[13] = None
        self.assertIsNone(rv._v4(self.ev(4, 1), s))


class SimTest(unittest.TestCase):
    def test_fixed_horizon_return(self):
        s = make(400)
        s.o[300], s.c[302] = 100.0, 101.0
        tr = rv.simulate(s, [(300, 1, None)], "H3", 0, 10 ** 12)
        self.assertEqual(len(tr), 1)
        self.assertAlmostEqual(tr[0][2], 0.01)
        sh = rv.simulate(s, [(300, -1, None)], "H3", 0, 10 ** 12)
        self.assertAlmostEqual(sh[0][2], -0.01)

    def test_no_overlap(self):
        s = make(400)
        tr = rv.simulate(s, [(300, 1, None), (301, 1, None), (302, 1, None), (303, 1, None)], "H3", 0, 10 ** 12)
        self.assertEqual([t[1] for t in tr], [s.ts(300), s.ts(303)])

    def test_stop_first_when_both_touched(self):
        s = make(400)
        atr = 0.01
        # лонг: стоп 1.5%, тейк 3% от входа 100; бар 301 касается и 96, и 104
        bar(s, 301, 100, 104, 96, 100, 10)
        tr = rv.simulate(s, [(300, 1, atr)], "SL", 0, 10 ** 12)
        self.assertAlmostEqual(tr[0][2], -0.015)

    def test_take_profit_and_gap_stop(self):
        s = make(400)
        bar(s, 302, 100, 103.5, 100, 103, 10)
        tr = rv.simulate(s, [(300, 1, 0.01)], "SL", 0, 10 ** 12)
        self.assertAlmostEqual(tr[0][2], 0.03)
        s2 = make(400)
        bar(s2, 301, 97, 97.5, 96.5, 97, 10)               # гэп вниз через стоп 98.5: исполнение по open 97
        tr = rv.simulate(s2, [(300, 1, 0.01)], "SL", 0, 10 ** 12)
        self.assertAlmostEqual(tr[0][2], -0.03)
        s3 = make(400)
        bar(s3, 301, 100, 100, 98, 99, 10)                 # шорт: стоп выше 101.5, тейк 97
        bar(s3, 302, 99, 99, 96.5, 97, 10)
        tr = rv.simulate(s3, [(300, -1, 0.01)], "SL", 0, 10 ** 12)
        self.assertAlmostEqual(tr[0][2], 0.03)

    def test_time_exit_sl_mode_and_hole(self):
        s = make(400)
        tr = rv.simulate(s, [(300, 1, 0.05)], "SL", 0, 10 ** 12)
        self.assertAlmostEqual(tr[0][2], 0.0)
        s.l[305] = None
        self.assertEqual(rv.simulate(s, [(300, 1, 0.05)], "SL", 0, 10 ** 12), [])

    def test_window_bounds_no_leak(self):
        s = make(400)
        cut = s.ts(320)
        a = rv.simulate(s, [(318, 1, None)], "H6", 0, cut)             # вход до границы, выход после -> отброшена
        self.assertEqual(a, [])
        b = rv.simulate(s, [(318, 1, None), (314, 1, None)], "H3", 0, cut)
        self.assertEqual([t[1] for t in b], [s.ts(314)])        # 318 + 3 бара выходит за границу
        c = rv.simulate(s, [(319, 1, None), (320, 1, None)], "H3", cut, 10 ** 12)    # test: только входы с cut
        self.assertEqual([t[1] for t in c], [s.ts(320)])


class StatsTest(unittest.TestCase):
    def test_basic(self):
        trades = [("A", 86400 * 1, 0.02), ("A", 86400 * 2, -0.01), ("B", 86400 * 3, 0.02), ("B", 86400 * 4, -0.01)]
        st = rv.stats(trades, 0.0)
        self.assertEqual(st["n"], 4)
        self.assertAlmostEqual(st["net"], 0.005)
        self.assertAlmostEqual(st["pf"], 2.0)
        self.assertAlmostEqual(st["win"], 0.5)
        self.assertEqual(st["days"], 4)
        net = rv.stats(trades, 0.01)
        self.assertAlmostEqual(net["net"], -0.005)
        self.assertLess(net["pf"], 1.0)

    def test_cluster_se_larger_than_iid_when_correlated(self):
        same_day = [("A%d" % i, 86400, 0.01) for i in range(10)] + [("B%d" % i, 86400 * 2, -0.01) for i in range(10)]
        st = rv.stats(same_day, 0.0)
        # два кластера с суммами +-0.1 вокруг среднего 0: SE = sqrt(0.1^2+0.1^2)/20
        self.assertAlmostEqual(st["se"], (0.02 ** 0.5) / 20 * 1.0, places=6)

    def test_empty_and_verdict(self):
        self.assertEqual(rv.stats([], 0.0)["n"], 0)
        good = {"net": 0.004, "pf": 1.5, "n": 150, "p": 0.0005}
        v = rv.verdict(good, [0.0, -0.002, -0.0024], [0.1, 0.2, -0.1])
        self.assertTrue(v["edge"])
        self.assertTrue(v["strict"])
        self.assertAlmostEqual(v["p_adj"], 0.0005 * rv.K)
        self.assertFalse(rv.verdict(dict(good, n=99), [0.0], [1, 1, 1])["edge"])
        self.assertFalse(rv.verdict(good, [0.005], [1, 1, 1])["edge"])
        self.assertFalse(rv.verdict(good, [0.0], [1, -1, -1])["edge"])
        self.assertFalse(rv.verdict(dict(good, pf=1.2), [0.0], [1, 1, 1])["edge"])


class SplitTest(unittest.TestCase):
    def test_common_boundaries_and_parts(self):
        a, b = make(1000, sym="A"), make(800, sym="B")
        start, cut, end, edges = rv.split_times([a, b])
        self.assertEqual(start, T0 + rv.LOOK * 300)
        self.assertEqual(end, a.ts(1000))
        self.assertAlmostEqual((cut - start) / float(end - start), 0.6, places=2)
        self.assertEqual(edges[0], cut)
        self.assertEqual(edges[-1], end)
        self.assertEqual(len(edges), rv.PARTS + 1)
        trades = [("A", t, 0.0) for t in (cut - 300, cut, edges[1], edges[2] + 300, end - 300)]
        parts = rv.split_parts([t for t in trades if t[1] >= cut], edges)
        self.assertEqual([len(p) for p in parts], [1, 1, 2])

    def test_impulse_found_in_test_only_when_after_cut(self):
        s = make(1000)
        start, cut, end, edges = rv.split_times([s])
        j = int((cut - T0) // 300) + 5
        bar(s, j, 100, 103, 99.9, 102, 60)
        ev = {s.sym: rv.impulses(s)}
        tr_train = rv.run_variant([s], ev, 1, rv._v1, "H3", (start, cut))
        tr_test = rv.run_variant([s], ev, 1, rv._v1, "H3", (cut, end))
        self.assertEqual((len(tr_train), len(tr_test)), (0, 1))


class FromRowsTest(unittest.TestCase):
    def test_grid_with_holes(self):
        rows = {T0: (T0, "1", "2", "0.5", "1.5", "7", "9"), T0 + 900: (T0 + 900, "1", "2", "0.5", "1.5", "8", "9")}
        s = rv.from_rows("X", "crypto", rows)
        self.assertEqual(len(s), 4)
        self.assertIsNone(s.o[1])
        self.assertEqual(s.v[3], 8.0)


class FakeClient(object):
    """Имитация /v5/market/kline: свечи с шагом 300 с, ответ новые -> старые, лимит 1000, поле end включительно."""

    def __init__(self, t_first, t_last, step=300):
        self.all = list(range(t_first, t_last + 1, step))
        self.calls = []

    def call(self, method, path, params=None, auth=True):
        self.calls.append((path, dict(params or {})))
        if path == "/v5/market/tickers":
            return {"list": [{"symbol": "BTCUSDT", "turnover24h": "9e9"}, {"symbol": "AAAUSDT", "turnover24h": "5e8"},
                             {"symbol": "BBBUSDT", "turnover24h": "9e8"}, {"symbol": "BTC-27MAR26", "turnover24h": "1e9"},
                             {"symbol": "USDCUSDT", "turnover24h": "1e9"}, {"symbol": "ZZZUSDT", "turnover24h": "0"}]}
        end = int(params["end"]) // 1000
        sel = [t for t in self.all if t <= end][-int(params["limit"]):][::-1]
        return {"list": [[str(t * 1000), "1", "2", "0.5", "1.5", "10", "15"] for t in sel]}


class KlinesTest(unittest.TestCase):
    def test_pick_symbols(self):
        res = FakeClient(0, 0).call("GET", "/v5/market/tickers")
        self.assertEqual([s for s, _ in kl.pick_symbols(res, 10)], ["BBBUSDT", "AAAUSDT"])
        self.assertEqual(len(kl.pick_symbols(res, 1)), 1)

    def test_group(self):
        self.assertEqual(kl.group_of("TQQQUSDT"), "stock")
        self.assertEqual(kl.group_of("DOGEUSDT"), "crypto")

    def test_pagination_dedup_and_resume(self):
        now = T0 + 300 * 3000 + 123
        c = FakeClient(T0 - 300 * 5000, now + 300 * 200)             # на «бирже» есть и будущая/открытая свеча
        with tempfile.TemporaryDirectory() as d:
            r = kl.sync_symbol(c, "AAAUSDT", 5, 2.0, d, now=now, pause=0, sleep=lambda x: None)   # 2 суток = 576 свечей
            self.assertEqual(r["rows"], 576)
            self.assertGreaterEqual(r["pages"], 1)
            until = now // 300 * 300 - 300
            self.assertEqual(r["last"], kl.epoch_to_iso(until))        # открытая свеча не сохранена
            rows = kl.load_csv(os.path.join(d, "k5_AAAUSDT.csv"))
            ts = sorted(rows)
            self.assertEqual(len(ts), len(set(ts)))
            self.assertTrue(all(b - a == 300 for a, b in zip(ts, ts[1:])))
            # дозагрузка: прошло ещё 100 свечей, окно шире (4 суток) -> дозагружаются и хвост, и голова
            now2 = now + 300 * 100
            r2 = kl.sync_symbol(c, "AAAUSDT", 5, 4.0, d, now=now2, pause=0, sleep=lambda x: None)
            self.assertEqual(r2["rows"], 1152)
            ts2 = sorted(kl.load_csv(os.path.join(d, "k5_AAAUSDT.csv")))
            self.assertTrue(all(b - a == 300 for a, b in zip(ts2, ts2[1:])))
            self.assertEqual(ts2[-1], now2 // 300 * 300 - 300)

    def test_multi_page(self):
        now = T0 + 300 * 100
        c = FakeClient(T0 - 300 * 4000, now)
        with tempfile.TemporaryDirectory() as d:
            r = kl.sync_symbol(c, "AAAUSDT", 5, 14.0, d, now=now, pause=0, sleep=lambda x: None)   # 4032 свечи -> 5 страниц
            self.assertEqual(r["rows"], 4032)
            self.assertGreaterEqual(r["pages"], 4)

    def test_call_retry(self):
        class Flaky(object):
            n = 0

            def call(self, *a, **k):
                Flaky.n += 1
                if Flaky.n < 3:
                    raise OSError("timeout")
                return {"list": []}
        self.assertEqual(kl.call_retry(Flaky(), {}, sleep=lambda x: None), {"list": []})

    def test_allowlist_has_kline(self):
        from tb.venues import bybit
        self.assertIn(("GET", "/v5/market/kline"), bybit.ALLOWED)


if __name__ == "__main__":
    unittest.main()
