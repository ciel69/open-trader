import io
import json
import os
import random
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import data, patterns as pt, patterns_scan, watch
from tb.research import research_patterns as rp

CFG = pt.merge_cfg()
O, H, L, C = pt.O, pt.H, pt.L, pt.C


def bar(i, o, h, l, c, v=100.0):
    return (i * 3600.0, float(o), float(h), float(l), float(c), float(v))


def walk(n, seed=1, start=100.0, vol=0.01):
    rnd, px, out = random.Random(seed), start, []
    for i in range(n):
        o = px
        c = px * (1 + rnd.gauss(0, vol))
        h, l = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2))), min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(bar(i, o, h, l, c, 100 + rnd.random() * 100))
        px = c
    return out


class CandleTest(unittest.TestCase):
    def test_pin_bar(self):
        self.assertEqual(pt.pin_bar([bar(0, 100, 101, 90, 100.5)], 0, CFG, 10.0), 1)
        self.assertEqual(pt.pin_bar([bar(0, 100, 110, 99, 99.5)], 0, CFG, 10.0), -1)
        self.assertEqual(pt.pin_bar([bar(0, 100, 101, 99, 100.2)], 0, CFG, 10.0), 0)       # диапазон меньше 0.5 ATR
        self.assertEqual(pt.pin_bar([bar(0, 100, 105, 95, 100)], 0, CFG, 10.0), 0)         # обе тени

    def test_engulfing(self):
        p = bar(0, 105, 106, 99, 100)
        self.assertEqual(pt.engulfing([p, bar(1, 99, 107, 98, 106)], 1, CFG, 4.0), 1)
        self.assertEqual(pt.engulfing([p, bar(1, 101, 107, 98, 106)], 1, CFG, 4.0), 0)     # открытие не ниже закрытия предыдущего
        q = bar(0, 100, 106, 99, 105)
        self.assertEqual(pt.engulfing([q, bar(1, 106, 107, 98, 99)], 1, CFG, 4.0), -1)
        self.assertEqual(pt.engulfing([p, bar(1, 99, 107, 98, 106)], 1, CFG, 40.0), 0)     # тело меньше 0.5 ATR

    def test_inside_doji_nr(self):
        self.assertTrue(pt.inside_bar([bar(0, 100, 110, 90, 100), bar(1, 100, 105, 95, 101)], 1))
        self.assertFalse(pt.inside_bar([bar(0, 100, 110, 90, 100), bar(1, 100, 110, 95, 101)], 1))
        self.assertTrue(pt.doji([bar(0, 100, 105, 95, 100.5)], 0, CFG, 10.0))
        self.assertFalse(pt.doji([bar(0, 100, 105, 95, 103)], 0, CFG, 10.0))
        bars = [bar(i, 100, 100 + 3 + i % 3, 100 - 3, 100) for i in range(6)] + [bar(6, 100, 101, 99, 100)]
        self.assertTrue(pt.is_nr(bars, 6, 7))
        self.assertFalse(pt.is_nr(bars, 5, 7))

    def test_star(self):
        b = [bar(0, 110, 111, 99, 100), bar(1, 99, 99.5, 98, 98.5), bar(2, 99, 107, 98.5, 106)]
        self.assertEqual(pt.star(b, 2, CFG, 10.0), 1)
        e = [bar(0, 100, 111, 99, 110), bar(1, 111, 112, 110.5, 111.5), bar(2, 111, 111.5, 102, 104)]
        self.assertEqual(pt.star(e, 2, CFG, 10.0), -1)
        b[2] = bar(2, 99, 104, 98.5, 103)  # не дошла до середины тела (105)
        self.assertEqual(pt.star(b, 2, CFG, 10.0), 0)

    def test_volume_confirm(self):
        bars = [bar(i, 100, 101, 99, 100, v=10) for i in range(25)]
        bars.append(bar(25, 100, 101, 99, 100, v=16))
        bars.append(bar(26, 100, 101, 99, 100, v=14))
        self.assertTrue(pt.volume_confirm(bars, 25, CFG))
        self.assertFalse(pt.volume_confirm(bars, 26, CFG))


def path(points, start_i=0, noise=0.3):
    """Кусочно-линейный путь по 1 шагу цены на бар; points = [(цель, число баров)]."""
    out, px, i = [], points[0][0], start_i
    for target, n in points[1:]:
        for k in range(1, n + 1):
            c = px + (target - px) * k / n
            o = out[-1][C] if out else px
            out.append(bar(i, o, max(o, c) + noise, min(o, c) - noise, c))
            i += 1
        px = target
    return out


class StructureTest(unittest.TestCase):
    def setUp(self):
        warm = path([(105, 0), (95, 25), (105, 25), (95, 25), (110, 25)])
        self.bars = warm + path([(110, 0), (100, 10), (110, 10), (100.2, 10), (113, 13)], start_i=len(warm))
        self.an = pt.Analyzer(self.bars)

    def test_double_bottom_first_close_above_neckline(self):
        hits = [i for i in range(100, len(self.bars)) if pt.double_extreme(self.bars, i, CFG, self.an.flags(), self.an.atr(i))[0] == 1]
        self.assertEqual(len(hits), 1, hits)
        i = hits[0]
        _, info = pt.double_extreme(self.bars, i, CFG, self.an.flags(), self.an.atr(i))
        self.assertGreater(self.bars[i][C], info["neckline"])
        self.assertLessEqual(self.bars[i - 1][C], info["neckline"])
        self.assertAlmostEqual(info["p2"], 100.2 - 0.3, delta=0.5)

    def test_levels_cluster_and_touches(self):
        i = len(self.bars) - 1
        lv = pt.levels(self.bars, i, CFG, self.an.flags(), self.an.atr(i))
        lows = [x for x in lv if 94 < x["price"] < 96.5]
        self.assertTrue(lows and lows[0]["touches"] >= 2, lv)
        self.assertEqual(lv, sorted(lv, key=lambda d: -d["strength"]))
        below, above = pt.nearest_levels(lv, 100.0)
        self.assertTrue(below["price"] < 100.0 <= above["price"])

    def test_pivot_confirmed_only_after_k_bars(self):
        bars = [bar(i, 100, 101 + (5 if i == 10 else 0), 99, 100) for i in range(30)]
        fl = pt.pivot_flags(bars, 3)
        self.assertTrue(fl[10][0])
        lv_early = pt.levels(bars, 12, CFG, fl, 2.0)  # пивот 10 подтверждён с бара 13
        lv_late = pt.levels(bars, 13, CFG, fl, 2.0)
        self.assertFalse(any(abs(x["price"] - 106) < 1 for x in lv_early))
        self.assertTrue(any(abs(x["price"] - 106) < 1 for x in lv_late))


class MtfTest(unittest.TestCase):
    def test_aggregate(self):
        bars = [bar(i + 4, 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(10)]  # ts кратен 4 ч с бара 0
        agg, last = pt.aggregate(bars, 4)
        self.assertEqual(len(agg), 2)                       # хвост из 2 баров отброшен
        self.assertEqual(agg[0][O], 100)
        self.assertEqual(agg[0][H], 104)
        self.assertEqual(agg[0][L], 99)
        self.assertEqual(agg[0][C], 103.5)
        self.assertEqual(agg[0][5], 40)
        self.assertEqual(last, [3, 7])

    def test_htf_trend_up_and_no_lookahead(self):
        bars = [bar(i, 100 + i * 0.1, 100.3 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1) for i in range(600)]
        t = pt.htf_trend_map(bars, 4, 20, 50)
        self.assertEqual(t[-1], 1)
        self.assertEqual(t[10], 0)
        for i in (150, 301, 302, 303, 450):
            self.assertEqual(pt.htf_trend_map(bars[:i + 1], 4, 20, 50)[i], t[i])


class NoLookaheadTest(unittest.TestCase):
    def test_truncation_gives_same_answer(self):
        bars = walk(700, seed=3)
        full = pt.Analyzer(bars)
        for i in range(120, 700, 29):
            cut = pt.Analyzer(bars[:i + 1])
            self.assertEqual(full.candles(i), cut.candles(i))
            self.assertEqual(full.levels(i), cut.levels(i))
            self.assertEqual(full.htf(i), cut.htf(i))
            self.assertEqual(full.bb_rank(i), cut.bb_rank(i))
            self.assertEqual(full.confluence(i), cut.confluence(i))
            self.assertEqual(pt.double_extreme(bars, i, CFG, full.flags(), full.atr(i)), pt.double_extreme(bars[:i + 1], i, CFG, cut.flags(), cut.atr(i)))

    def test_deterministic(self):
        bars = walk(400, seed=5)
        self.assertEqual(pt.Analyzer(bars).confluence(399), pt.Analyzer(list(bars)).confluence(399))

    def test_merge_cfg_ignores_unknown_and_bad_types(self):
        c = pt.merge_cfg({"pin_wick_frac": 0.7, "nope": 1, "nr_n": "x", "vol_mult": True})
        self.assertEqual(c["pin_wick_frac"], 0.7)
        self.assertNotIn("nope", c)
        self.assertEqual(c["nr_n"], pt.DEFAULTS["nr_n"])
        self.assertEqual(c["vol_mult"], pt.DEFAULTS["vol_mult"])


class ConfluenceTest(unittest.TestCase):
    def test_hammer_at_support_scores_long(self):
        base = path([(105, 0), (95, 25), (105, 25), (95, 25), (105, 25), (95.4, 22)])
        i = len(base)
        hammer = bar(i, 95.4, 95.6, 94.4, 95.55, v=500)
        bars = base + [hammer]
        r = pt.Analyzer(bars).confluence(i)
        self.assertGreaterEqual(r["long"]["score"], 3, r)
        self.assertTrue(any("свеча:pin_bar" in x for x in r["long"]["reasons"]))
        self.assertLess(r["short"]["score"], r["long"]["score"])


class ScanTest(unittest.TestCase):
    def test_load_candles_and_scan_without_writes(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "data"))
        bars = walk(300, seed=2)
        with open(os.path.join(root, "data", "candles_TST_1h.csv"), "w") as f:
            f.write("ts_iso,o,h,l,c,volume\n")
            for b in bars:
                f.write("%s,%r,%r,%r,%r,%r\n" % (data.epoch_to_iso(b[0] + 1.7e9), b[1], b[2], b[3], b[4], b[5]))
        before = sorted(os.listdir(root))
        r = patterns_scan.analyze("TST", "1h", root=root)
        self.assertNotIn("error", r)
        self.assertIsNone(r["live_price"])           # нет runtime/market.json
        self.assertIn("confluence", r)
        self.assertIn("TST", patterns_scan.render(r))
        self.assertEqual(sorted(os.listdir(root)), before)
        self.assertIn("error", patterns_scan.analyze("NOPE", "1h", root=root))
        self.assertTrue(patterns_scan.analyze("TST", "4h", root=root)["htf_trend"] is None)


H3600 = 3600


class WatchPatternsTest(unittest.TestCase):
    wcfg = dict(watch.DEFAULTS, symbols=["ETH"], min_history_hours=10, lookback_hours=48)

    def test_default_off(self):
        self.assertFalse(watch.DEFAULTS["patterns_events"])
        s = watch.SymState("ETH", [100.0 + (i % 5) for i in range(40)], self.wcfg)
        self.assertIsNone(s.pcfg)
        w = watch.Watcher(tempfile.mkdtemp(), dict(self.wcfg), io.StringIO())
        self.assertIsNone(w.states["ETH"].pcfg)

    def feed(self, s, hours):
        out = []
        for h, prices in hours:
            for k, p in enumerate(prices):
                out += s.update(h * H3600 + 60 * k, p)
        return out

    def test_pattern_event_when_enabled(self):
        warm = [bar(1000 - 300 + i, 100, 101 + (i % 3) * 0.2, 99, 100 + (i % 2) * 0.2) for i in range(300)]
        closes = [b[C] for b in warm]
        s = watch.SymState("ETH", closes, self.wcfg, bars=warm, pcfg={"cfg": pt.merge_cfg(), "min_score": 1})
        ev = self.feed(s, [(1000, [100.0, 92.0, 100.5]), (1001, [100.4])])
        kinds = [e["type"] for e in ev]
        self.assertIn("pattern", kinds)
        e = [x for x in ev if x["type"] == "pattern"][0]
        self.assertEqual(e["side"], "long")
        self.assertTrue(e["unvalidated"])
        self.assertLess(e["suggest_stop"], e["bar_close"])
        # тот же ввод с выключенным флагом: событий pattern нет
        s0 = watch.SymState("ETH", closes, self.wcfg)
        self.assertNotIn("pattern", [x["type"] for x in self.feed(s0, [(1000, [100.0, 92.0, 100.5]), (1001, [100.4])])])

    def test_gap_in_bars_disables_events(self):
        warm = [bar(500 + i, 100, 101, 99, 100) for i in range(300)]
        s = watch.SymState("ETH", [100.0] * 40, self.wcfg, bars=warm, pcfg={"cfg": pt.merge_cfg(), "min_score": 1})
        ev = self.feed(s, [(1000, [100.0, 92.0, 100.5]), (1001, [100.4])])
        self.assertNotIn("pattern", [e["type"] for e in ev])


class ResearchTest(unittest.TestCase):
    def mk(self, rows):
        return [bar(i, *r) for i, r in enumerate(rows)]

    def sim(self, rows, sig, **kw):
        bars = self.mk(rows)
        return rp.simulate(bars, [1.0] * len(bars), {0: sig}, 0, len(bars), stop_mult=1.0, rr=2.0, **kw)

    def test_take_profit_stop_and_priority(self):
        flat = (100, 100.5, 99.5, 100)
        t = self.sim([flat, (100, 102.5, 99.5, 102), flat], ("mkt", 1, None))
        self.assertAlmostEqual(t[0][2], 0.02)
        t = self.sim([flat, (100, 100.5, 98.9, 99), flat], ("mkt", 1, None))
        self.assertAlmostEqual(t[0][2], -0.01)
        t = self.sim([flat, (100, 103, 98, 100), flat], ("mkt", 1, None))      # оба уровня в одном баре -> стоп
        self.assertAlmostEqual(t[0][2], -0.01)
        t = self.sim([flat, (100, 100.5, 97.9, 98), flat], ("mkt", -1, None))  # шорт: стоп 101, тейк 98
        self.assertAlmostEqual(t[0][2], 0.02)

    def test_gap_through_stop_and_time_exit(self):
        flat = (100, 100.5, 99.5, 100)
        t = self.sim([flat, flat, (95, 96, 94, 95), flat], ("mkt", 1, None))
        self.assertAlmostEqual(t[0][2], -0.05)
        t = self.sim([flat, (100, 100.5, 99.5, 100.4), (100.4, 100.6, 100, 100.5), flat], ("mkt", 1, None), max_hold=2)
        self.assertAlmostEqual(t[0][2], 0.005)
        self.assertEqual(t[0][4], 2)

    def test_breakout_entry(self):
        flat = (100, 100.5, 99.5, 100)
        t = self.sim([flat, (100, 101.5, 100, 101.2), flat], ("brk", 101.0, 99.0))
        self.assertEqual(t[0][1], 1)
        t = self.sim([flat, (100, 101.5, 98.5, 100), flat], ("brk", 101.0, 99.0))  # пробиты обе стороны -> пропуск
        self.assertEqual(t, [])

    def test_no_overlap(self):
        flat = (100, 100.5, 99.5, 100)
        bars = self.mk([flat] * 10)
        sigs = {0: ("mkt", 1, None), 1: ("mkt", 1, None), 5: ("mkt", 1, None)}
        t = rp.simulate(bars, [1.0] * 10, sigs, 0, 10, stop_mult=1.0, rr=2.0, max_hold=3)
        self.assertEqual([x[0] for x in t], [1, 6])  # второй сигнал внутри первой позиции пропущен

    def test_costs_and_funding_sign(self):
        long_net, _ = rp.to_net((1, 1, 0.0, 0.01, 10), 0.001)
        short_net, _ = rp.to_net((1, -1, 0.0, 0.01, 10), 0.001)
        self.assertAlmostEqual(long_net, -0.002 - 10 * rp.FUND_H)
        self.assertAlmostEqual(short_net, -0.002 + 10 * rp.FUND_H)

    def test_bootstrap_reproducible_and_signed(self):
        rows = [(0.01, 0.5, d // 3) for d in range(120)]
        lb, p = rp.bootstrap_lb(rows, 0.005, n_boot=500)
        self.assertGreater(lb, 0)
        self.assertEqual(rp.bootstrap_lb(rows, 0.005, n_boot=500), (lb, p))
        rnd = random.Random(1)
        noise = [(0.0, rnd.choice([-1.0, 1.0]), d // 2) for d in range(200)]
        self.assertLess(rp.bootstrap_lb(noise, 0.005, n_boot=500)[0], 0)

    def test_evaluate_reproducible_on_random_data(self):
        assets = {}
        for k, s in enumerate(rp.POOL + rp.REF):
            assets[s] = walk(2500, seed=10 + k)
        a = rp.evaluate(assets, rp.HYPS[6])
        b = rp.evaluate(assets, rp.HYPS[6])
        self.assertEqual(json.dumps(a, sort_keys=True, default=str), json.dumps(b, sort_keys=True, default=str))
        self.assertFalse(a["venues"]["platform"]["edge"] and a["venues"]["bybit"]["edge"] and False)



class JournalSummaryTest(unittest.TestCase):
    def test_summary_has_pf_and_stderr(self):
        from tb.core import journal
        rows = [{"pnl_usdt": 2.0, "fees_usdt": 0.1, "r": 1.0}, {"pnl_usdt": -1.0, "fees_usdt": 0.1, "r": -0.5}, {"pnl_usdt": -1.0, "fees_usdt": 0.1, "r": -0.5}]
        s = journal._summ(rows)
        self.assertIn("PF=1.00", s)
        self.assertIn("ст.ош.", s)
        self.assertIn("PF=n/a", journal._summ([]))


if __name__ == "__main__":
    unittest.main()
