"""WP15b: ретро-проверка памяти (`memory backtest`), сравнение периодов (`memory compare`), разрез mem в `memory stats` и `exp stats`. Только числа, предупреждения о малой выборке."""
import contextlib
import io
import json
import os
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import T0, TmpRoot, mk, ts_at
from test_memory_cli import Cli
from tb.core import expstats
from tb.memory import agg, backtest, cases, similar


def signal_pool(n=40, mem_from=None):
    """Подтверждённые входы выигрывают, неподтверждённые проигрывают: у памяти есть настоящий сигнал."""
    out = []
    for i in range(n):
        ok = i % 2 == 0
        out.append(mk(i, sym="S%d" % (i % 5), r=0.8 if ok else -1.0, confirmed=ok, mode="paper", ts=ts_at(i), err=[] if ok else ["no-confirm"],
                      mem=("on" if (mem_from is not None and i >= mem_from) else ("off" if mem_from is not None else None))))
    return out


def noise_pool(n=40):
    """Результат не зависит от признаков (чередование независимо от confirmed): честная память не должна выглядеть умной."""
    out = []
    for i in range(n):
        out.append(mk(i, sym="N%d" % (i % 5), r=0.5 if (i * 7) % 5 < 2 else -0.5, confirmed=(i % 3 == 0), mode="paper", ts=ts_at(i)))
    return out


def fold(lst):
    from test_memory_similar import fold as f
    return f(lst)


class TestBacktest(unittest.TestCase):
    def setUp(self):
        from tb.memory import cfg
        self.cfg = cfg.load()

    def run_bt(self, lst, **kw):
        return backtest.run(fold(lst), self.cfg, mode="paper", **kw)

    def test_signal_beats_baseline_walk_forward(self):
        w = self.run_bt(signal_pool(60))["walk_forward"]
        self.assertGreaterEqual(w["n_eval"], 30)
        self.assertGreater(w["acc_mem"], w["acc_base"])
        self.assertLess(w["brier_mem"], w["brier_base"])
        self.assertGreater(w["d_brier_mean"], 0)
        self.assertIsNotNone(w["d_brier_se"])
        self.assertEqual(w["warn"], "OK_NO_MULTIPLE")

    def test_noise_gives_no_edge(self):
        w = self.run_bt(noise_pool(60))["walk_forward"]
        self.assertLessEqual(w["acc_mem"] - w["acc_base"], 0.2)
        self.assertLessEqual(w["d_brier_mean"], 0.02)

    def test_split_old_to_new_saves_r_when_signal(self):
        s = self.run_bt(signal_pool(60))["split"]
        self.assertEqual(s["n_old"] + s["n_new"], 60)
        self.assertGreater(s["n_avoid"], 0)
        self.assertEqual(s["avoid_lost_share"], 1.0)             # все избегаемые действительно проигрышные
        self.assertGreater(s["r_saved_if_avoided"], 0)
        self.assertGreater(s["mean_r_kept"], s["mean_r_all"])
        self.assertLess(s["mean_r_avoided"], 0)

    def test_split_ts_explicit(self):
        s = self.run_bt(signal_pool(60), split_ts=ts_at(40))["split"]
        self.assertEqual((s["n_old"], s["n_new"]), (40, 20))
        self.assertEqual(s["split_ts"], ts_at(40))

    def test_no_future_leak_changing_later_case_does_not_change_earlier_advice(self):
        lst = signal_pool(40)
        tr = backtest.closed_trades(fold(lst), self.cfg, "paper")
        a1 = backtest.advice(tr[:20], tr[20], self.cfg)
        lst2 = signal_pool(40)
        lst2[30]["result"]["r"] = 99.0             # изменили СЛЕДУЮЩИЙ по времени случай
        tr2 = backtest.closed_trades(fold(lst2), self.cfg, "paper")
        self.assertEqual(backtest.advice(tr2[:20], tr2[20], self.cfg), a1)
        # и поиск как таковой не видит случаи позже самого случая, даже если они переданы в train
        a_all = backtest.advice(tr2, tr2[20], self.cfg)
        self.assertEqual(a_all, backtest.advice(tr2[:20], tr2[20], self.cfg))

    def test_small_sample_warnings_and_empty(self):
        r = self.run_bt(signal_pool(6))
        self.assertEqual(r["walk_forward"]["warn"], "INSUFFICIENT")
        self.assertEqual(r["split"]["warn"], "INSUFFICIENT")
        r = self.run_bt(signal_pool(0))
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["walk_forward"]["n_eval"], 0)
        r = self.run_bt(signal_pool(14))
        self.assertEqual(r["walk_forward"]["warn"], "INSUFFICIENT" if r["walk_forward"]["n_eval"] < 5 else "LOW_N")

    def test_excluded_and_other_mode_ignored_mem_off_kept(self):
        lst = signal_pool(20)
        lst += [mk(100 + i, r=5.0, mode="live", ts=ts_at(100 + i)) for i in range(5)]
        lst += [mk(200 + i, r=5.0, mode="paper", exclude=True, ts=ts_at(200 + i)) for i in range(5)]
        lst += [mk(300 + i, r=5.0, mode="paper", mem="off", ts=ts_at(300 + i)) for i in range(5)]
        tr = backtest.closed_trades(fold(lst), self.cfg, "paper")
        self.assertEqual(len(tr), 25)               # 20 + 5 с mem=off (в поиске похожих они участвуют); live и исключённые нет

    def test_only_numbers_no_advice_words(self):
        text = json.dumps(self.run_bt(signal_pool(60)), ensure_ascii=False).lower()
        for w in ("рекоменду", "надо ", "следует", "recommend", "should"):
            self.assertNotIn(w, text)

    def test_deterministic_under_row_order(self):
        lst = signal_pool(50)
        a = self.run_bt(lst)
        b = self.run_bt(list(reversed(lst)))
        self.assertEqual(a, b)


class TestCompare(unittest.TestCase):
    def setUp(self):
        from tb.memory import cfg
        self.cfg = cfg.load()

    def test_before_after_with_mem_label(self):
        lst = signal_pool(60, mem_from=30)
        rep = backtest.compare_periods(fold(lst), self.cfg, mode="paper", split_ts=ts_at(30))
        self.assertEqual((rep["a"]["n_closed"], rep["b"]["n_closed"]), (30, 30))
        self.assertEqual(rep["a"]["by_mem"]["off"]["n"], 30)
        self.assertEqual(rep["b"]["by_mem"]["on"]["n"], 30)
        self.assertIn("не A/B", rep["note"])
        self.assertEqual(rep["warn"], "OK_NO_MULTIPLE")
        self.assertIsNotNone(rep["diff_mean_r"])

    def test_small_periods_warn_and_mem_off_included(self):
        lst = signal_pool(8, mem_from=4)
        rep = backtest.compare_periods(fold(lst), self.cfg, mode="paper", split_ts=ts_at(4))
        self.assertEqual(rep["warn"], "INSUFFICIENT")
        self.assertEqual(rep["a"]["n_closed"], 4)              # mem=off участвует в сравнении периодов
        self.assertEqual(rep["a"]["by_mem"]["off"]["n"], 4)

    def test_explicit_bounds(self):
        rep = backtest.compare_periods(fold(signal_pool(20)), self.cfg, a_from=ts_at(0), a_to=ts_at(5), b_from=ts_at(5), b_to=ts_at(10), mode="paper")
        self.assertEqual((rep["a"]["n_closed"], rep["b"]["n_closed"]), (5, 5))


class TestCliAndStats(Cli):
    def test_cli_backtest_compare_stats_by_mem(self):
        self.put(signal_pool(40, mem_from=20))
        code, out, _ = self.run_cli("backtest", "--mode", "paper")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["n_trades"], 40)
        self.assertEqual(len(out.strip().splitlines()), 1)
        code, out, _ = self.run_cli("compare", "--mode", "paper", "--split-ts", ts_at(20), "--text")
        self.assertEqual(code, 0)
        self.assertIn("до/после, не A/B", out)
        code, out, _ = self.run_cli("stats", "--by", "mem", "--mode", "paper")
        rows = dict((r["value"], r) for r in json.loads(out)["by"]["mem"])
        self.assertEqual((rows["on"]["n"], rows["off"]["n"]), (20, 20))            # mem=off в разрезе mem виден
        self.assertEqual(json.loads(self.run_cli("stats", "--mode", "paper")[1])["total"]["n_closed"], 20)         # в общую статистику mem=off не идёт

    def test_cli_empty_base(self):
        for argv in (("backtest",), ("compare",), ("stats", "--by", "mem")):
            self.assertEqual(self.run_cli(*argv)[0], 0, argv)
        self.assertEqual(self.run_cli("compare", "--split-ts", "bad")[0], 0)

    def test_sync_final_only_flag(self):
        self.copy_fixtures()
        self.assertEqual(json.loads(self.run_cli("sync", "--final-only", "--dry-run")[1])["would_write"], 13)

    def test_agg_by_mem_key(self):
        c = fold([mk(0, mem="on"), mk(1), mk(2, mem="off")])
        self.assertEqual([agg.group_key(x, "mem")[0] for x in c], ["on", "unknown", "off"])


class TestExpStatsMem(TmpRoot):
    def test_group_by_mem_from_gateway_tags(self):
        def tr(i, r, tags):
            return [{"ev": "open", "id": "T%d" % i, "ts": "2026-10-28T10:0%d:00Z" % i, "sym": "SOL", "side": "long", "mode": "paper", "tags": tags, "risk_usdt": 1.0},
                    {"ev": "close", "id": "T%d" % i, "ts_close": "2026-10-28T11:0%d:00Z" % i, "pnl_usdt": r, "r": r, "reason": "tp"}]
        rows = tr(1, 1.0, ["agent.api", "mem.on"]) + tr(2, -1.0, ["agent.api", "mem.off"]) + tr(3, 0.5, ["mem.on"]) + tr(4, 0.2, [])
        with open(os.path.join(self.root, "state", "trades.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        ts = expstats.load_trades("all", self.root)
        g = dict((x["key"], x["n"]) for x in expstats.group_stats(ts, "mem", 1))
        self.assertEqual(g, {"on": 2, "off": 1, "(нет)": 1})
        self.assertIn("mem", expstats.GROUP_BY)

    def test_exp_cli_accepts_mem(self):
        from tb.brain import exp_cli
        a = exp_cli._parser().parse_args(["stats", "--by", "mem"])
        self.assertEqual(a.by, "mem")


if __name__ == "__main__":
    unittest.main()
