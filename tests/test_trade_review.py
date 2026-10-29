"""Разбор сделок: снимок рынка при входе (ctx), путь цены MAE/MFE, теги ошибок и journal post, отчёт analyze, дашборд, hotscan-трекер.
Без сети, во временном TB_ROOT, фейковые runtime-файлы prices/hot/market/context."""
import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import journal, tradectx, tradepath
from tb.dash import model
from tb.marketdata import hotscan as hs

import test_bybit_limit as TL

T0 = 1793100000.0


def iso(ts):
    return journal.time.strftime("%Y-%m-%dT%H:%M:%SZ", journal.time.gmtime(ts))


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def jl(path):
    return json.loads(rd(path))


def wjson(root, sub, name, obj):
    os.makedirs(os.path.join(root, sub), exist_ok=True)
    with open(os.path.join(root, sub, name), "w", encoding="utf-8") as f:
        json.dump(obj, f)


def fake_runtime(root, now=T0, rng5=1.4, spread=3.5, with_market=True, with_hot=True):
    if with_hot:
        wjson(root, "runtime", "hot.json", {"ts_epoch": now - 10, "top": [
            {"sym": "AAA", "price": 2.0, "rank": 2, "score": 4.2, "range_5m_pct": rng5, "range_15m_pct": 2.0, "ret_5m_pct": -0.7, "ret_15m_pct": 0.1,
             "turn_surge_5m": 1.2, "turn_x_median": 70.0, "chg_24h_pct": -25.4, "spread_bps": spread, "funding_pct": 0.0268, "oi_chg_15m_pct": -1.7, "tradable": True}],
            "blocked": []})
    wjson(root, "runtime", "prices.json", {"ts_epoch": now - 5, "prices": {"AAA": 2.01, "BBB": 5.0}})
    if with_market:
        wjson(root, "runtime", "market.json", {"ts_epoch": now - 2, "symbols": {"SOL": {
            "price": 100.5, "age_s": 0.3, "regime": "range", "ret_5m": 0.001, "ret_15m": -0.002, "funding_hourly": 1e-5, "book": {"spread_bps": 0.9}}}})
        wjson(root, "runtime", "context.json", {"ts_epoch": now - 300, "risk_off": True, "risk_off_reasons": ["btc dump"]})


class Env(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbrev_")
        for d in ("state", "runtime", "config"):
            os.makedirs(os.path.join(self.root, d))
        self.old = (journal.PATH, os.environ.get("TB_ROOT"), journal._now)
        os.environ["TB_ROOT"] = self.root
        journal.PATH = os.path.join(self.root, "state", "trades.jsonl")
        model._CACHE.clear()

    def tearDown(self):
        journal.PATH, root, journal._now = self.old
        if root is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = root
        shutil.rmtree(self.root, ignore_errors=True)

    def open(self, tid="T1", sym="AAA", side="long", entry=2.0, sl=1.9, tp=2.3, ts_open=None, **kw):
        a = argparse.Namespace(sym=sym, side=side, entry=entry, lev=3.0, margin=6.0, sl=sl, tp=tp, thesis="т", news="", regime="range", tags="", mode="live",
                               id=tid, session=None, stype="intraday", venue="bybit", ts_open=ts_open or iso(T0), **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            journal.cmd_open(a)

    def close(self, tid="T1", exit=2.1, now=T0 + 600, **kw):
        journal._now = lambda: iso(now)
        a = argparse.Namespace(id=tid, exit=exit, reason="manual", funding=0.0, fee=0.0009, note="", **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            journal.cmd_close(a)

    def trade(self, tid="T1"):
        return journal._load()[0][tid]


class CtxTest(Env):
    def test_full_snapshot(self):
        fake_runtime(self.root)
        c = tradectx.snapshot("AAA", now=T0)
        self.assertEqual((c["range_5m_pct"], c["range_15m_pct"], c["ret_5m_pct"], c["turn_surge_5m"], c["turn_x_median"]), (1.4, 2.0, -0.7, 1.2, 70.0))
        self.assertEqual((c["chg_24h_pct"], c["spread_bps"], c["funding_pct"], c["rank"], c["tradable"]), (-25.4, 3.5, 0.0268, 2, True))
        self.assertEqual(c["price"], 2.01)                       # самая свежая из источников: prices.json (5 с), а не hot (10 с)
        self.assertEqual(c["age_s"]["hot"], 10.0)
        self.assertEqual(c["src"], ["hot", "prices", "context"])
        self.assertNotIn("late_s", c)

    def test_market_coin_outside_hot_and_context(self):
        fake_runtime(self.root)
        c = tradectx.snapshot("SOLUSDT", now=T0)
        self.assertEqual((c["regime"], c["ret_5m_pct"], c["ret_15m_pct"], c["spread_bps"], c["risk_off"]), ("range", 0.1, -0.2, 0.9, True))
        self.assertEqual(c["price"], 100.5)
        self.assertNotIn("range_5m_pct", c)                      # монеты нет в hot.json: диапазона нет, и это нормально

    def test_partial_and_empty_never_raise(self):
        self.assertEqual(tradectx.snapshot("AAA", now=T0), {})
        fake_runtime(self.root, with_hot=False, with_market=False)
        c = tradectx.snapshot("AAA", now=T0)
        self.assertEqual((c["price"], c["src"]), (2.01, ["prices"]))
        with open(os.path.join(self.root, "runtime", "prices.json"), "w") as f:
            f.write("{битый")
        self.assertEqual(tradectx.snapshot("AAA", now=T0), {})

    def test_late_marker(self):
        fake_runtime(self.root)
        self.assertEqual(tradectx.snapshot("AAA", now=T0, ts_open_epoch=T0 - 900)["late_s"], 900)

    def test_open_writes_ctx(self):
        fake_runtime(self.root, now=journal.time.time())
        self.open(ctx_place={"price": 1.9, "ts": "x"})
        t = self.trade()
        self.assertEqual(t["ctx"]["range_5m_pct"], 1.4)
        self.assertEqual(t["ctx_place"]["price"], 1.9)

    def test_open_without_runtime_files_has_empty_ctx(self):
        self.open()
        self.assertEqual(self.trade()["ctx"], {})
        self.assertNotIn("ctx_place", self.trade())

    def test_limit_place_and_fill_snapshots(self):
        """Лимит: ctx_place кладётся в pending при постановке, ctx снимается при заполнении (через sync) и оба оказываются в журнале."""
        class T(TL.Base):
            def runTest(self):
                f = TL.LimitFake()
                fake_runtime(self.dir, now=journal.time.time(), rng5=0.6, spread=2.0)
                self.place(f, sym="XRP", limit_price=1.99)
                rec = self.rec()
                self.assertEqual(rec["ctx_place"], {})           # у XRP в фейк-hot нет строки: пусто, но ключ есть и ничего не упало
                wjson(self.dir, "runtime", "prices.json", {"ts_epoch": journal.time.time(), "prices": {"XRP": 1.99}})
                f.fill(avg="1.9895")
                self.sync(f)
                t = list(journal._load()[0].values())[0]
                self.assertEqual(t["ctx"]["price"], 1.99)
                self.assertNotIn("ctx_place", t)          # пустой снимок при постановке в журнал не пишется
        with contextlib.redirect_stdout(io.StringIO()):
            r = unittest.TextTestRunner(stream=io.StringIO()).run(T())
        self.assertTrue(r.wasSuccessful(), [str(x[1]) for x in r.failures + r.errors])


class PathTest(Env):
    def tracker(self, **kw):
        return tradepath.PathTracker(journal.PATH, paths.runtime("trade_paths.json"), **kw)

    @staticmethod
    def tk(px):
        return {"AAAUSDT": {"price": px}}

    def run_path(self, tr, prices, t0=T0, step=30.0):
        for i, px in enumerate(prices):
            tr.update(self.tk(px), t0 + step * (i + 1))

    def test_long_mae_mfe_and_close_path(self):
        self.open(entry=2.0, sl=1.9, tp=2.3)
        tr = self.tracker()
        self.run_path(tr, [2.02, 1.96, 2.10, 2.20, 2.05])           # MAE 2% (t=60), MFE 10% (t=120)
        self.close(exit=2.05, now=T0 + 160)
        p = self.trade()["path"]
        self.assertEqual((p["mae_pct"], p["mfe_pct"], p["t_mae_s"], p["t_mfe_s"]), (2.0, 10.0, 60, 120))
        self.assertEqual((p["mae_r"], p["mfe_r"], p["mfe_to_tp"]), (0.4, 2.0, 0.667))     # sl 5% -> 2%/5%, 10%/5%; tp 15%
        self.assertEqual((p["n"], p["src"]), (5, "hotscan"))
        self.assertGreaterEqual(p["path_coverage"], 0.99)

    def test_short_signs(self):
        self.open(side="short", entry=2.0, sl=2.1, tp=1.8)
        tr = self.tracker()
        self.run_path(tr, [2.04, 1.90, 2.06])                        # против: вверх 3%, за: вниз 5%
        self.close(exit=2.06, now=T0 + 100)
        p = self.trade()["path"]
        self.assertEqual((p["mae_pct"], p["mfe_pct"]), (3.0, 5.0))
        self.assertEqual((p["t_mae_s"], p["t_mfe_s"]), (90, 60))
        self.assertEqual((p["mae_r"], p["mfe_to_tp"]), (0.6, 0.5))

    def test_exit_price_counts_as_extreme(self):
        self.open(entry=2.0, sl=1.9, tp=2.3)
        self.run_path(self.tracker(), [1.99])
        self.close(exit=1.88, now=T0 + 45)                            # проскальзывание за стоп видно в MAE
        self.assertEqual(self.trade()["path"]["mae_pct"], 6.0)
        self.assertEqual(self.trade()["path"]["mae_r"], 1.2)

    def test_restart_keeps_state(self):
        self.open()
        tr = self.tracker()
        self.run_path(tr, [1.95, 2.0])
        tr2 = self.tracker()                                          # перезапуск сканера
        self.assertEqual(tr2.state[list(tr2.state)[0]]["n"], 2)
        tr2.update(self.tk(2.15), T0 + 90)
        st = list(tr2.state.values())[0]
        self.assertEqual((st["min"], st["max"], st["n"]), (1.95, 2.15, 3))
        raw = jl(paths.runtime("trade_paths.json"))
        self.assertEqual(raw["trades"]["T1"]["n"], 3)
        self.assertEqual([x for x in os.listdir(paths.runtime()) if "tmp" in x], [])

    def test_coverage_with_gap(self):
        self.open()
        tr = self.tracker()
        tr.update(self.tk(2.0), T0 + 30)
        tr.update(self.tk(2.0), T0 + 60)
        tr.update(self.tk(1.97), T0 + 600)                            # сканер молчал 9 минут
        self.close(exit=2.0, now=T0 + 630)
        p = self.trade()["path"]
        self.assertLess(p["path_coverage"], 0.2)
        self.assertEqual(p["n"], 3)

    def test_no_state_no_path_and_old_trades_unaffected(self):
        self.open()
        self.close()
        self.assertNotIn("path", self.trade())

    def test_closed_trade_is_pruned_and_other_modes_ignored(self):
        self.open()
        tr = self.tracker()
        self.run_path(tr, [2.0])
        self.close(exit=2.0, now=T0 + 40)
        self.assertEqual(self.tracker().update(self.tk(2.1), T0 + 70), 0)
        self.assertEqual(jl(paths.runtime("trade_paths.json"))["trades"], {})

    def test_garbage_journal_does_not_raise(self):
        with open(journal.PATH, "w") as f:
            f.write('{"ev": "open", "id": "X", "sym"\n[1]\n\n')
        self.assertEqual(self.tracker().update(self.tk(2.0), T0 + 30), 0)

    def test_hotscan_updates_tracker_each_scan(self):
        self.open(sym="AAA", entry=2.0)
        hot = os.path.join(self.root, "runtime", "hot.json")
        log = os.path.join(self.root, "runtime", "hot_events.jsonl")
        shutil.copy(journal.PATH, os.path.join(self.root, "runtime", "trades.jsonl"))
        c = TL_hs_client([[hs_tk("AAAUSDT", 2.0)], [hs_tk("AAAUSDT", 1.9)], [hs_tk("AAAUSDT", 2.2)]])
        clock = {"t": T0 + 10}
        hs.run(c, {"interval": 30.0, "max_seconds": 100.0}, True, out=io.StringIO(), hot_path=hot, log_path=log,
               clock=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
        st = jl(os.path.join(self.root, "runtime", "trade_paths.json"))["trades"]["T1"]
        self.assertEqual((st["min"], st["max"]), (1.9, 2.2))


def hs_tk(sym, price):
    return {"symbol": sym, "lastPrice": str(price), "markPrice": str(price), "bid1Price": str(price), "ask1Price": str(price), "turnover24h": "9000000",
            "price24hPcnt": "0.01", "openInterest": "1000", "fundingRate": "0.0001"}


class TL_hs_client:
    env = "mainnet"

    def __init__(self, pages):
        self.pages, self.n = pages, 0

    def call(self, method, path, params=None, auth=True):
        if path == "/v5/market/instruments-info":
            return {"list": []}
        self.n += 1
        return {"list": self.pages[min(self.n - 1, len(self.pages) - 1)]}


class PostTest(Env):
    def test_close_with_tags_and_lesson(self):
        self.open()
        self.close(err=["no-confirm", "chase"], rule_followed="partial", lesson="ждать свечу")
        t = self.trade()
        self.assertEqual((t["err"], t["rule_followed"], t["lesson"]), (["no-confirm", "chase"], "partial", "ждать свечу"))

    def test_post_later_appends_event_and_keeps_log_append_only(self):
        self.open()
        self.close()
        before = rd(journal.PATH)
        journal.main(["post", "T1", "--err", "tight-stop,good-process-bad-luck", "--rule-followed", "yes", "--lesson", "шум"])
        after = rd(journal.PATH)
        self.assertTrue(after.startswith(before))
        self.assertEqual(json.loads(after[len(before):].strip())["ev"], "post")
        t = self.trade()
        self.assertEqual((t["err"], t["rule_followed"], t["lesson"], t["pnl_usdt"] is not None, "ts_close" in t), (["tight-stop", "good-process-bad-luck"], "yes", "шум", True, True))
        journal.main(["post", "T1", "--lesson", "уточнение"])             # дополнение: непереданные поля остаются
        t = self.trade()
        self.assertEqual((t["lesson"], t["err"]), ("уточнение", ["tight-stop", "good-process-bad-luck"]))

    def test_bad_tag_rejected_and_open_trade_refused(self):
        self.open()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            journal.main(["post", "T1", "--err", "nonsense"])
        with self.assertRaises(SystemExit) as cm:
            journal.main(["post", "T1", "--err", "good"])               # сделка ещё открыта
        self.assertIn("открыта", str(cm.exception))
        with self.assertRaises(SystemExit):
            journal.main(["post", "NOPE", "--err", "good"])
        self.close()
        with self.assertRaises(SystemExit):
            journal.main(["post", "T1"])                                  # нечего писать

    def test_tags_constant_and_parse(self):
        self.assertEqual(set(tradectx.ERR_TAGS), {"no-confirm", "chase", "tight-stop", "wide-stop", "wrong-direction", "news-miss", "size", "fee-drag",
                                                   "rule-break", "good-process-bad-luck", "good"})
        self.assertEqual(tradectx.parse_err("good, size;good"), ["good", "size"])

    def test_bybit_close_parser_validates_tag_before_any_order(self):
        from tb.venues import bybit
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bybit.main(["close", "--sym", "SOL", "--err", "xx"])        # argparse падает до make_client и ордеров

    def test_sync_style_close_has_no_review_fields(self):
        self.open()
        self.close()                                                      # как bybit sync: без err/rule/lesson
        t = self.trade()
        for k in ("err", "rule_followed", "lesson"):
            self.assertNotIn(k, t)


class AnalyzeTest(Env):
    def run_report(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            journal.main(["analyze"] + list(args))
        return buf.getvalue()

    def test_empty_and_small_sample_warning(self):
        out = self.run_report()
        self.assertIn("выборка мала", out)
        self.open()
        self.close()
        out = self.run_report()
        self.assertIn("СВОДКА: n=1", out)
        self.assertIn("мало данных", out)

    def test_report_with_mixed_old_and_new_records(self):
        # старая запись без новых полей + новая с ctx/path/err
        self.open(tid="OLD")
        self.close(tid="OLD", exit=1.9, now=T0 + 100)
        fake_runtime(self.root, now=journal.time.time())
        self.open(tid="NEW", sym="AAA", ts_open=iso(T0))
        tr = self.make_tracker()
        for i, px in enumerate([1.9, 2.1]):
            tr.update({"AAAUSDT": {"price": px}}, T0 + 30 * (i + 1))
        self.close(tid="NEW", exit=2.1, now=T0 + 70, err=["good"], rule_followed="yes", lesson="ок")
        rep = journal.analyze([self.trade("OLD"), self.trade("NEW")])
        self.assertEqual((rep["n"], rep["with_path"], rep["with_ctx"], rep["untagged"]), (2, 1, 1, 1))
        self.assertEqual(rep["err_tags"], {"good": 1})
        self.assertEqual(rep["mae_mfe"]["win"]["n"], 1)
        self.assertEqual(rep["mae_mfe"]["loss"]["n"], 0)
        self.assertIsNone(rep["mae_mfe"]["loss"]["mae_r"])
        out = self.run_report()
        self.assertIn("NEW", out)
        self.assertIn("good", out)
        js = json.loads(self.run_report("--json"))
        self.assertEqual(js["n"], 2)

    def make_tracker(self):
        return tradepath.PathTracker(journal.PATH, paths.runtime("trade_paths.json"))

    def test_buckets_need_min_n(self):
        fake_runtime(self.root, now=journal.time.time(), rng5=1.4)
        for i in range(10):
            self.open(tid="B%d" % i)
            self.close(tid="B%d" % i, exit=2.1 if i % 2 else 1.95, now=T0 + 60)
        rep = journal.analyze(list(journal._load()[0].values()))
        b = {x["bucket"]: x for x in rep["ctx_range_5m"]}
        self.assertEqual(b["1-2%"]["n"], 10)
        self.assertEqual(b["1-2%"]["winrate"], 0.5)
        self.assertEqual(b["<0.5%"]["note"], "мало данных")
        rep9 = journal.analyze(list(journal._load()[0].values())[:9])
        self.assertIn("note", {x["bucket"]: x for x in rep9["ctx_range_5m"]}["1-2%"])

    def test_pf_fees_share(self):
        self.open(tid="W")
        self.close(tid="W", exit=2.2)
        self.open(tid="L", sym="AAA")
        self.close(tid="L", exit=1.9)
        s = journal.analyze(list(journal._load()[0].values()))["summary"]
        self.assertEqual((s["n"], s["wins"]), (2, 1))
        self.assertGreater(s["fees"], 0)
        self.assertGreater(s["pf"], 0)
        self.assertGreater(s["fees_share_abs_pnl"], 0)

    def test_real_journal_copy_does_not_break(self):
        """Копия реального журнала (старые записи без ctx/path) не ломает analyze и review."""
        real = os.path.join(_bootstrap.ROOT, "state", "trades.jsonl")
        if not os.path.exists(real):
            self.skipTest("нет реального журнала")
        shutil.copy(real, journal.PATH)
        out = self.run_report()
        self.assertIn("СВОДКА", out)
        self.run_report("--mode", "all")
        with contextlib.redirect_stdout(io.StringIO()):
            journal.main(["review"])


class DashTest(Env):
    def test_history_and_active_fields(self):
        self.open(tid="C1")
        self.close(tid="C1", err=["no-confirm"], rule_followed="no", lesson="урок <b>")
        self.open(tid="O1", sym="AAA")
        wjson(self.root, "runtime", "trade_paths.json", {"trades": {"O1": {"sym": "AAA", "side": "long", "entry": 2.0, "sl": 1.9, "tp": 2.3, "ts_open": T0,
                                                                         "min": 1.96, "max": 2.1, "t_min": T0 + 30, "t_max": T0 + 60, "n": 3, "last": 2.05, "last_ts": T0 + 90,
                                                                         "prev_ts": T0 + 90, "covered_s": 90.0}}})
        journal.main(["post", "C1", "--err", "no-confirm,chase"])
        st = model.build_state(self.root, "live", "all", now=T0 + 100)
        h = st["history"][0]
        self.assertEqual((h["err"], h["rule_followed"], h["lesson"]), (["no-confirm", "chase"], "no", "урок <b>"))
        self.assertIsNone(h["path"])
        o = st["open_trades"][0]
        self.assertEqual(o["path_live"]["mae_pct"], 2.0)
        self.assertEqual(o["path_live"]["mfe_pct"], 5.0)
        self.assertEqual(o["path_live"]["path_coverage"], 1.0)
        self.assertEqual(st["metrics"]["n"], 1)

    def test_old_records_get_empty_review_fields(self):
        self.open(tid="C2")
        self.close(tid="C2")
        h = model.build_state(self.root, "live", "all", now=T0 + 100)["history"][0]
        self.assertEqual((h["err"], h["rule_followed"], h["lesson"], h["path"]), ([], None, None, None))


if __name__ == "__main__":
    unittest.main()
