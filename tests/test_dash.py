"""Дашборд: разбор журнала на кривых данных, PnL/R/метрики, ответы API, сервер, отсутствие секретов. Без сети, во временном TB_ROOT."""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401
from tb import paths
from tb.dash import model, server

NOW = 1793106000.0  # 2026-10-27T12:20:00Z


def iso(ts):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def op(i, **kw):
    r = {"ev": "open", "id": i, "ts_open": iso(NOW - 600), "sym": "SOL", "side": "long", "entry": 100.0, "lev": 10.0, "margin": 2.0,
         "notional": 20.0, "sl": 99.0, "tp": 103.0, "risk_usdt": 0.2, "thesis": "тезис <b>x</b>", "tags": ["t1"], "mode": "live",
         "venue": "bybit", "session": "intraday-1", "stype": "intraday"}
    r.update(kw)
    return r


def cl(i, pnl, **kw):
    r = {"ev": "close", "id": i, "ts_close": iso(NOW - 300), "exit": 101.0, "reason": "tp", "pnl_usdt": pnl, "fees_usdt": 0.04, "funding_usdt": 0.0, "r": None}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbdash_")
        for d in ("state", "runtime", "config"):
            os.makedirs(os.path.join(self.root, d))
        model._CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def w(self, sub, name, obj, raw=False):
        with open(os.path.join(self.root, sub, name), "w", encoding="utf-8") as f:
            f.write(obj if raw else json.dumps(obj, ensure_ascii=False))
        model._CACHE.clear()

    def jl(self, name, rows, extra=""):
        self.w("state", name, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n" + extra, raw=True)

    def market(self, price=101.0, age=1.0, sym="SOL"):
        self.w("runtime", "market.json", {"ts": iso(NOW - age), "ts_epoch": NOW - age, "feed_ok": True, "symbols": {
            sym: {"price": price, "src": "okx", "prices_by_src": {"okx": price, "bybit": price + 0.5}, "regime": "range", "ret_1m": 0.001,
                  "book": {"spread_bps": 1.5, "imbalance": 0.1}}}})


class TestJournalParsing(Base):
    def test_old_record_and_defaults(self):
        self.jl("trades.jsonl", [{"ev": "open", "id": "A", "sym": "ETH", "side": "short", "entry": 2000, "lev": 3, "margin": 10, "sl": 2100, "tp": 1800},
                                 {"ev": "close", "id": "A", "exit": 1900, "reason": "tp", "pnl_usdt": 1.4, "fees_usdt": 0.05}])
        tr, w = model.load_trades(self.root)
        self.assertEqual(len(tr), 1)
        t = tr[0]
        self.assertEqual((t["mode"], t["venue"], t["tags"], t["status"]), ("live", "platform", [], "closed"))
        self.assertAlmostEqual(t["notional"], 30.0)
        self.assertAlmostEqual(t["risk_usdt"], 1.5)  # |2000-2100|/2000*30
        self.assertAlmostEqual(t["r"], 1.4 / 1.5)
        self.assertEqual(sum(w.values()), 0)

    def test_open_without_close_stays_open(self):
        self.jl("trades.jsonl", [op("A")])
        tr, _ = model.load_trades(self.root)
        self.assertEqual([t["status"] for t in tr], ["open"])

    def test_duplicate_open_replaces_and_reopen_after_close_is_new(self):
        self.jl("trades.jsonl", [op("A", entry=100.0), op("A", entry=101.0), op("B"), cl("B", 0.1), op("B", entry=105.0)])
        tr, w = model.load_trades(self.root)
        self.assertEqual([(t["id"], t["entry"], t["status"]) for t in tr], [("A", 101.0, "open"), ("B", 100.0, "closed"), ("B", 105.0, "open")])
        self.assertEqual(w["dup_open"], 1)

    def test_second_close_is_a_correction(self):
        self.jl("trades.jsonl", [op("A"), cl("A", -0.02, fees_usdt=0.024), cl("A", -0.04, fees_usdt=0.04, risk_usdt=0.4, r=-0.1)])
        tr, _ = model.load_trades(self.root)
        self.assertEqual(len(tr), 1)
        self.assertEqual((tr[0]["pnl_usdt"], tr[0]["fees_usdt"], tr[0]["risk_usdt"], tr[0]["r"]), (-0.04, 0.04, 0.4, -0.1))

    def test_garbage_lines_and_orphan_close(self):
        self.jl("trades.jsonl", [op("A"), cl("ZZZ", 1.0), {"ev": "weird", "id": "Q"}, {"no": "id"}], extra='{"ev": "open", "id": "cut", "sy\n[1,2]\n\n')
        tr, w = model.load_trades(self.root)
        self.assertEqual([t["id"] for t in tr], ["A"])
        self.assertEqual((w["orphan_close"], w["bad_rows"]), (1, 2))

    def test_missing_files_are_empty(self):
        self.assertEqual(model.load_trades(self.root)[0], [])
        st = model.build_state(self.root, now=NOW)
        self.assertEqual((st["open_trades"], st["history"], st["session"]), ([], [], None))
        self.assertFalse(st["hot"]["available"])
        self.assertFalse(st["market"]["ok"])


class TestCalc(unittest.TestCase):
    T = {"side": "long", "entry": 100.0, "notional": 20.0, "margin": 2.0, "lev": 10.0, "sl": 99.0, "tp": 103.0, "risk_usdt": 0.2, "fee_open": 0.007}

    def test_long(self):
        c = model.live_calc(dict(self.T), 101.0, 1.0)
        self.assertAlmostEqual(c["pnl_gross"], 0.2)
        self.assertAlmostEqual(c["pnl"], 0.2 - 0.007)
        self.assertAlmostEqual(c["r_now"], (0.2 - 0.007) / 0.2)
        self.assertAlmostEqual(c["pnl_pct_margin"], (0.2 - 0.007) / 2.0 * 100)
        self.assertAlmostEqual(c["to_sl_pct"], 2.0 / 101.0 * 100)
        self.assertAlmostEqual(c["to_tp_pct"], 2.0 / 101.0 * 100)
        self.assertAlmostEqual(c["to_sl_usdt"], 0.4)
        self.assertAlmostEqual(c["pnl_at_sl"], -0.2 - 0.007)
        self.assertAlmostEqual(c["pnl_at_tp"], 0.6 - 0.007)
        self.assertAlmostEqual(c["gauge"]["price"], 0.5)
        self.assertAlmostEqual(c["gauge"]["entry"], 0.25)
        self.assertAlmostEqual(c["liq_est"], 100.0 * (1 - (0.1 - 0.01)))

    def test_short_signs_and_missing_fee(self):
        t = dict(self.T, side="short", sl=101.0, tp=97.0, fee_open=None)
        c = model.live_calc(t, 99.0)
        self.assertAlmostEqual(c["pnl_gross"], 0.2)
        self.assertFalse(c["fee_open_known"])
        self.assertAlmostEqual(c["fee_open_usdt"], 0.0009 * 20.0, places=5)
        self.assertAlmostEqual(c["to_sl_pct"], 2.0 / 99.0 * 100)  # цена пошла в нашу сторону: до стопа дальше
        self.assertAlmostEqual(c["to_tp_pct"], 2.0 / 99.0 * 100)
        self.assertAlmostEqual(c["gauge"]["price"], 0.5)
        c2 = model.live_calc(t, 102.0)  # за стопом
        self.assertLess(c2["pnl"], 0)
        self.assertLess(c2["to_sl_pct"], 0)
        self.assertEqual(c2["gauge"]["price"], 0.0)

    def test_no_price(self):
        c = model.live_calc(dict(self.T), None)
        self.assertFalse(c["has_price"])
        self.assertNotIn("pnl", c)

    def test_summarize(self):
        rows = [{"pnl_usdt": p, "r": r, "fees_usdt": 0.1} for p, r in ((1.0, 1.0), (-0.5, -0.5), (2.0, 2.0), (-1.0, None))]
        m = model.summarize(rows)
        self.assertEqual((m["n"], m["wins"], m["losses"]), (4, 2, 2))
        self.assertAlmostEqual(m["winrate"], 50.0)
        self.assertAlmostEqual(m["pf"], 2.0)
        self.assertAlmostEqual(m["pnl"], 1.5)
        self.assertAlmostEqual(m["avg_r"], (1 - 0.5 + 2) / 3)
        self.assertAlmostEqual(m["max_dd"], 1.0)
        self.assertAlmostEqual(m["fees"], 0.4)
        e = model.summarize([])
        self.assertEqual((e["n"], e["winrate"], e["pf"], e["avg_r"]), (0, None, None, None))
        self.assertTrue(model.summarize([{"pnl_usdt": 1.0}])["pf_inf"])


class TestPcts(Base):
    def test_trade_pcts(self):
        mv, mp = model.trade_pcts({"side": "long", "entry": 0.002035, "exit": 0.00199, "margin": 6.32885, "pnl_usdt": -0.5})
        self.assertAlmostEqual(mv, -2.2113, places=3)
        self.assertAlmostEqual(mp, -0.5 / 6.32885 * 100)
        mv, _ = model.trade_pcts({"side": "short", "entry": 0.0908, "exit": 0.09254})
        self.assertAlmostEqual(mv, (1 - 0.09254 / 0.0908) * 100)
        self.assertLess(mv, 0)
        mv, mp = model.trade_pcts({"side": "long", "entry": 100.0, "margin": None, "pnl_usdt": 1.0})
        self.assertEqual((mv, mp), (None, None))  # нет выхода и маржи: не падаем

    def test_live_est_exit(self):
        c = model.live_calc(dict(TestCalc.T), 101.0)
        self.assertAlmostEqual(c["move_pct"], 1.0)
        self.assertAlmostEqual(c["pnl_est_exit"], c["pnl"] - 0.0009 * 20.0 * 1.01)
        self.assertAlmostEqual(c["pnl_pct_margin_est"], c["pnl_est_exit"] / 2.0 * 100)

    def test_history_and_summary(self):
        self.jl("trades.jsonl", [
            op("A", side="long", entry=100.0, margin=2.0), cl("A", 0.1, exit=102.0),
            op("B", side="short", entry=100.0, margin=2.0), cl("B", -0.2, exit=101.0),
            op("C", entry=100.0, margin=None), cl("C", 0.0, exit=None)])
        st = model.build_state(self.root, now=NOW)
        h = {t["id"]: t for t in st["history"]}
        self.assertAlmostEqual(h["A"]["move_pct"], 2.0)
        self.assertAlmostEqual(h["A"]["pnl_pct_margin"], 5.0)
        self.assertAlmostEqual(h["B"]["move_pct"], -1.0)
        self.assertAlmostEqual(h["B"]["pnl_pct_margin"], -10.0)
        self.assertIsNone(h["C"]["move_pct"])
        self.assertIsNone(h["C"]["pnl_pct_margin"])
        m = st["metrics"]
        self.assertAlmostEqual(m["avg_move_pct"], 0.5)
        self.assertAlmostEqual(m["avg_margin_pct"], -2.5)
        self.assertAlmostEqual(m["best_margin_pct"], 5.0)
        self.assertAlmostEqual(m["worst_margin_pct"], -10.0)
        self.assertIsNone(model.summarize([])["avg_move_pct"])


class TestState(Base):
    def prep(self):
        self.jl("trades.jsonl", [
            op("L1"), cl("L1", 0.5, r=2.5),
            op("T1", mode="testnet"), cl("T1", -0.1, r=-0.5),
            op("P1", mode="paper", venue="platform", tags=["paper"]), cl("P1", 0.3, r=1.5),
            op("OPEN1", ts_open=iso(NOW - 120), fee_open=0.007)])
        self.jl("sessions.jsonl", [{"ev": "start", "id": "intraday-1", "type": "intraday", "venue": "bybit", "thesis": "x", "ts": iso(NOW - 900),
                                    "start": NOW - 900, "expires": NOW + 1800, "budget_usdt": 1.2, "equity_at_start": 40.0}])
        self.w("state", "portfolio.json", {"venues": {"bybit": {"equity": 40.0, "free": 38.0, "ts": iso(NOW - 30)}}, "hwm": 41.0})
        self.w("config", "risk_profile.json", {"maintenance_margin_pct": 1.0, "drawdown_ladder": [[0.05, 1.0], [0.1, 0.5]], "drawdown_halt": 0.2,
                                               "probation_trades": 30, "perf_mult": {"probation": 0.5, "good": 1, "ok": 0.75, "bad": 0.25},
                                               "session_types": {"intraday": {"max_trades": 6}}})
        self.market(101.0)

    def test_default_is_live_only_and_switch(self):
        self.prep()
        st = model.build_state(self.root, now=NOW)
        self.assertEqual([t["id"] for t in st["history"]], ["L1"])
        self.assertEqual(st["filter"]["modes"], ["live", "paper", "testnet"])
        self.assertEqual(model.build_state(self.root, "testnet", now=NOW)["history"][0]["id"], "T1")
        self.assertEqual(model.build_state(self.root, "all", now=NOW)["metrics"]["n"], 3)
        self.assertEqual(model.build_state(self.root, "live", "platform", now=NOW)["metrics"]["n"], 0)

    def test_active_trade_uses_bybit_price_and_fee(self):
        self.prep()
        st = model.build_state(self.root, now=NOW)
        t = st["open_trades"][0]
        c = t["calc"]
        self.assertEqual(c["price_src"], "bybit")
        self.assertAlmostEqual(c["price"], 101.5)
        self.assertAlmostEqual(c["pnl"], 0.3 - 0.007)
        self.assertAlmostEqual(t["held_s"], 120.0)
        self.assertAlmostEqual(st["open_pnl"], 0.3 - 0.007)

    def test_no_price_is_reported_not_invented(self):
        self.prep()
        self.market(5.0, sym="ETH")  # у SOL цены нет
        st = model.build_state(self.root, now=NOW)
        self.assertFalse(st["open_trades"][0]["calc"]["has_price"])
        self.assertIsNone(st["open_pnl"])
        self.assertTrue(any("Нет цены" in w["text"] for w in st["warnings"]))

    def test_account_and_session(self):
        self.prep()
        st = model.build_state(self.root, now=NOW)
        a = st["account"]
        self.assertAlmostEqual(a["equity"], 40.0)
        self.assertAlmostEqual(a["drawdown"], 1 - 40.0 / 41.0)
        self.assertEqual(a["ladder_mult"], 1.0)
        s = st["session"]
        self.assertEqual((s["id"], s["trades"], s["open"], s["max_trades"]), ("intraday-1", 4, 1, 6))
        self.assertIsNone(s["left_s"])                                              # срока у сессии нет
        self.assertAlmostEqual(s["realized"], 0.5 - 0.1 + 0.3)
        self.assertTrue(s["entries_allowed"])
        self.assertEqual(s["perf_mult"], 0.5)

    def test_session_blocks(self):
        self.prep()
        st = model.build_state(self.root, now=NOW + 5000)
        self.assertTrue(st["session"]["entries_allowed"])                  # срока у сессии нет: время её не закрывает
        self.assertEqual(st["session"]["block_reasons"], [])
        self.jl("sessions.jsonl", [{"ev": "start", "id": "s1", "type": "intraday", "start": NOW, "expires": NOW + 10, "budget_usdt": 1.0}, {"ev": "stop", "id": "s1", "ts": iso(NOW)}])
        self.assertIsNone(model.build_state(self.root, now=NOW)["session"])

    def test_health_warnings(self):
        self.prep()
        self.w("runtime", "health.json", {"ts": iso(NOW - 400), "ts_epoch": NOW - 400, "running": True, "feed_ok": True, "sources": {"okx": {"state": "ok", "connected": True}}})
        self.w("runtime", "context.json", {"ts": iso(NOW - 4000), "ts_epoch": NOW - 4000, "risk_off": True, "risk_off_reasons": ["fng"], "degraded_sources": ["dvol_BTC"], "sources": {}})
        st = model.build_state(self.root, now=NOW)
        txt = " | ".join(w["text"] for w in st["warnings"])
        self.assertIn("Фид не пишет", txt)
        self.assertIn("risk_off: fng", txt)
        self.assertIn("dvol_BTC", txt)

    def test_hot_present(self):
        self.prep()
        self.w("runtime", "hot.json", {"ts": iso(NOW - 10), "ts_epoch": NOW - 10, "env": "mainnet", "stats": {"eligible": 3}, "top": [{"sym": "AAA", "price": 1.0, "score": 2.0, "rank": 1, "tradable": True, "reasons": ["r"], "junk": 1}], "blocked": []})
        h = model.build_state(self.root, now=NOW)["hot"]
        self.assertTrue(h["available"])
        self.assertEqual(h["top"][0]["sym"], "AAA")
        self.assertNotIn("junk", h["top"][0])

    def test_curve_and_breakdown(self):
        self.jl("trades.jsonl", [op("A", tags=["x", "y"]), cl("A", 1.0, ts_close=iso(NOW - 100)), op("B", stype="scalp", tags=[]), cl("B", -0.4, ts_close=iso(NOW - 200))])
        st = model.build_state(self.root, now=NOW)
        self.assertEqual([p["id"] for p in st["curve"]], ["B", "A"])
        self.assertAlmostEqual(st["curve"][-1]["cum"], 0.6)
        tags = {g["name"]: g["n"] for g in st["breakdown"]["tag"]}
        self.assertEqual(tags, {"x": 1, "y": 1, "(без тегов)": 1})
        self.assertEqual({g["name"] for g in st["breakdown"]["stype"]}, {"intraday", "scalp"})


class TestSummaryAndStatus(Base):
    """Агрегаты обзора: PnL за сегодня/всего, теги ошибок, единый индикатор состояния."""

    def test_today_and_total(self):
        self.market(price=101.0)
        self.jl("trades.jsonl", [op("A"), cl("A", 1.0, ts_close=iso(NOW - 300)),
                                 op("B"), cl("B", -0.4, ts_close=iso(NOW - 3 * 86400)),
                                 op("C", margin=2.0, notional=20.0)])
        sm = model.build_state(self.root, now=NOW)["summary"]
        self.assertEqual((sm["today"]["n"], sm["today"]["wins"]), (1, 1))
        self.assertAlmostEqual(sm["today"]["pnl"], 1.0)
        self.assertAlmostEqual(sm["total"]["realized"], 0.6)
        self.assertIsNotNone(sm["total"]["open"])
        self.assertAlmostEqual(sm["total"]["pnl"], 0.6 + sm["total"]["open"])
        self.assertLessEqual(sm["today"]["start"], NOW)

    def test_empty_summary(self):
        sm = model.build_state(self.root, now=NOW)["summary"]
        self.assertEqual((sm["today"]["n"], sm["today"]["pnl"], sm["total"]["pnl"], sm["total"]["open"]), (0, 0, 0, None))

    def test_err_breakdown(self):
        self.jl("trades.jsonl", [op("A"), cl("A", -1.0, err=["rule-break", "no-confirm"]), op("B"), cl("B", 0.5)])
        bd = {g["name"]: g for g in model.build_state(self.root, now=NOW)["breakdown"]["err"]}
        self.assertEqual((bd["rule-break"]["n"], bd["no-confirm"]["n"], bd["(без разбора)"]["n"]), (1, 1, 1))

    def test_status_levels(self):
        st = model.build_state(self.root, now=NOW)
        self.assertEqual(st["status"]["level"], "bad")            # нет health.json: фид не запущен
        self.assertEqual({i["id"] for i in st["status"]["items"]}, {"scanner", "feed", "context", "session"})
        self.w("runtime", "health.json", {"ts_epoch": NOW - 2, "running": True, "feed_ok": True, "sources": {"okx": {"state": "ok"}}})
        self.w("runtime", "context.json", {"ts_epoch": NOW - 2, "risk_off": False})
        self.w("runtime", "prices.json", {"ts_epoch": NOW - 5, "prices": {"SOL": 100.0}})
        st = model.build_state(self.root, now=NOW)
        self.assertEqual(st["status"]["level"], "ok")             # торговли нет, сессии нет: это норма
        self.assertEqual({i["id"]: i["level"] for i in st["status"]["items"]}["session"], "off")
        self.w("runtime", "context.json", {"ts_epoch": NOW - 2, "risk_off": True, "risk_off_reasons": ["btc"]})
        self.assertEqual(model.build_state(self.root, now=NOW)["status"]["level"], "bad")

    def test_status_macro_risk_off_is_warn_in_paper_only(self):
        self.w("runtime", "health.json", {"ts_epoch": NOW - 2, "running": True, "feed_ok": True, "sources": {"okx": {"state": "ok"}}})
        self.w("runtime", "prices.json", {"ts_epoch": NOW - 5, "prices": {"SOL": 100.0}})
        self.w("runtime", "context.json", {"ts_epoch": NOW - 2, "risk_off": True, "risk_off_reasons": ["macro_window: FOMC"]})
        self.w("runtime", "exec_health.json", {"ts_epoch": NOW - 1, "env": "paper", "profile": "paper"})
        ctx = lambda: {i["id"]: i["level"] for i in model.build_state(self.root, now=NOW)["status"]["items"]}["context"]
        self.assertEqual(ctx(), "warn")
        self.w("runtime", "exec_health.json", {"ts_epoch": NOW - 1, "env": "mainnet", "profile": "trade"})
        self.assertEqual(ctx(), "bad")
        self.w("runtime", "exec_health.json", {"ts_epoch": NOW - 1, "env": "paper", "profile": "paper"})
        self.w("runtime", "context.json", {"ts_epoch": NOW - 2, "risk_off": True, "risk_off_reasons": ["fng"]})
        self.assertEqual(ctx(), "bad")

    def test_status_scanner_needed_for_open_trade(self):
        self.w("runtime", "health.json", {"ts_epoch": NOW - 2, "running": True, "feed_ok": True, "sources": {}})
        self.w("runtime", "context.json", {"ts_epoch": NOW - 2})
        self.jl("trades.jsonl", [op("A")])
        sc = {i["id"]: i["level"] for i in model.build_state(self.root, now=NOW)["status"]["items"]}["scanner"]
        self.assertEqual(sc, "bad")


class TestRiskRoom(Base):
    """«Запас до лимитов»: числа должны совпадать с тем, что считают guard/session/portfolio."""
    PROF = {"risk_per_trade_frac": 0.03, "max_open_positions": 2, "daily_loss_limit_usdt": 3.0, "daily_loss_frac": 0.1, "max_consecutive_stops": 3,
            "drawdown_ladder": [[0.05, 1.0], [0.1, 0.5], [0.2, 0.25]], "drawdown_halt": 0.2, "probation_trades": 30,
            "perf_mult": {"probation": 0.5, "good": 1.0, "ok": 0.75, "bad": 0.25},
            "session_types": {"intraday": {"max_trades": 6, "max_minutes": 720}}}

    def setup_state(self, trades, session=True, pending=None):
        self.w("config", "risk_profile.json", self.PROF)
        self.w("state", "portfolio.json", {"venues": {"bybit": {"equity": 40.0, "free": 30.0, "ts": iso(NOW)}}, "hwm": 40.0})
        self.jl("trades.jsonl", trades)
        if session:
            self.jl("sessions.jsonl", [{"ev": "start", "id": "intraday-1", "type": "intraday", "venue": "bybit", "ts": iso(NOW - 900), "start": NOW - 900,
                                        "expires": NOW + 3600, "budget_usdt": 1.2, "equity_at_start": 40.0}])
        if pending:
            self.w("state", "pending_orders.json", {"orders": pending})

    def rr(self):
        return model.build_state(self.root, "live", "all", NOW)["risk_room"]

    def by(self, r):
        return {x["id"]: x for x in r["rows"]}

    def test_numbers_match_guard_formulas(self):
        # 2 стопа подряд сегодня (-0.5, -0.4), открыта одна сделка с риском 0.2, сессия intraday-1 содержит все три
        self.setup_state([op("A"), cl("A", -0.5, reason="sl", ts_close=iso(NOW - 700)), op("B"), cl("B", -0.4, reason="sl", ts_close=iso(NOW - 400)), op("C", risk_usdt=0.2)])
        r = self.rr()
        rows = self.by(r)
        self.assertTrue(r["allowed"])
        self.assertAlmostEqual(rows["daily"]["limit"], 4.0)                        # 10% капитала 40
        self.assertAlmostEqual(rows["daily"]["room"], 4.0 - 0.9 - 0.2)             # лимит + реализованный - риск открытых
        self.assertAlmostEqual(rows["session"]["room"], 1.2 - 0.9 - 0.2)
        self.assertEqual((rows["slots"]["used"], rows["slots"]["limit"]), (1, 2))
        self.assertEqual((rows["stops"]["used"], rows["stops"]["limit"], rows["stops"]["level"]), (2, 3, "warn"))
        self.assertEqual((rows["trades"]["used"], rows["trades"]["limit"]), (3, 6))
        nr = r["next_risk"]
        self.assertAlmostEqual(nr["full"], 40.0 * 0.03 * 0.5)                      # перф-множитель 0.5 до 30 сделок
        self.assertAlmostEqual(nr["usdt"], 0.1)                                    # ограничен остатком бюджета сессии (1.2-0.9-0.2)
        self.assertEqual(nr["limited_by"], "бюджет сессии")

    def test_paper_day_counts_open_paper_positions_from_snapshot(self):
        """Paper-день: открытые бумажные позиции есть только в снимке исполнителя (в журнал попадают при закрытии). Слоты, риск и число сделок обязаны их учитывать,
        иначе блок показывает полный запас при занятых слотах (ошибка с 3 позициями SOL/HAEDAL/BLUR: «Слоты 3 из 3», «Под риском 0.00»)."""
        self.setup_state([])
        pos = lambda sym, size, avg, sl: {"sym": sym, "side": "long", "size": size, "avg": avg, "sl": sl, "owner": "plan:PL-1-%s-01" % sym}
        self.w("runtime", "exec_snapshot.json", {"ts_epoch": NOW - 1, "env": "paper", "account": {"equity": 40.0, "free": 30.0},
                                                 "positions": [pos("SOL", 0.3, 100.0, 99.0), pos("ETH", 2.0, 50.0, 49.5)]})
        rows = self.by(self.rr())
        self.assertEqual((rows["slots"]["used"], rows["slots"]["limit"], rows["slots"]["open"]), (2, 2, 2))
        self.assertAlmostEqual(rows["daily"]["open_risk"], 0.3 * 1.0 + 2.0 * 0.5)                   # риск до стопа по снимку
        self.assertEqual(rows["trades"]["used"], 2)
        r = self.rr()
        self.assertTrue(r["paper"])                                                                 # блок как в live; страница пишет, что в paper лимиты не действуют
        self.assertFalse(r["allowed"])                                                              # слоты заняты
        self.w("runtime", "exec_snapshot.json", {"ts_epoch": NOW - 1, "env": "mainnet", "positions": [pos("SOL", 0.3, 100.0, 99.0)]})
        rows = self.by(self.rr())
        self.assertEqual((rows["slots"]["used"], rows["daily"]["open_risk"]), (0, 0.0))             # live-день: снимок не учитывается (позиции в журнале)

    def test_next_risk_uses_default_type_multiplier_without_session(self):
        """Недочёт 4б: без активной сессии множитель по результатам берётся как у guard/portfolio для типа по умолчанию (intraday, испытательный срок ×0.5), а не ×1.00."""
        from tb.core import portfolio
        self.setup_state([], session=False)
        nr = self.rr()["next_risk"]
        pm, n = portfolio.perf_mult("intraday", self.PROF, [])
        self.assertEqual(pm, 0.5)
        self.assertEqual((nr["perf_mult"], nr["session_type"], nr["assumed_type"], nr["perf_n"]), (pm, "intraday", True, n))
        self.assertAlmostEqual(nr["full"], 40.0 * 0.03 * portfolio.dd_mult({"venues": {"b": {"equity": 40.0}}, "hwm": 40.0}, self.PROF) * pm)
        self.assertIn("intraday", nr["perf_by_type"])

    def test_next_risk_session_type_wins_over_default(self):
        self.setup_state([], session=False)
        self.jl("sessions.jsonl", [{"ev": "start", "id": "scalp-1", "type": "scalp", "venue": "bybit", "ts": iso(NOW - 900), "start": NOW - 900,
                                    "expires": NOW + 600, "budget_usdt": 0.6, "equity_at_start": 40.0}])
        prof = dict(self.PROF, session_types={"intraday": {"max_trades": 6, "max_minutes": 720}, "scalp": {"max_trades": 6, "max_minutes": 15}})
        self.w("config", "risk_profile.json", prof)
        nr = self.rr()["next_risk"]
        self.assertEqual((nr["session_type"], nr["assumed_type"]), ("scalp", False))

    def test_daily_row_exposes_realized_for_label(self):
        """Недочёт 4а: строка дневного лимита отдаёт лимит, результат дня и запас раздельно (запас может быть больше лимита при прибыли)."""
        self.setup_state([op("A"), cl("A", 0.45, ts_close=iso(NOW - 400))])
        d = self.by(self.rr())["daily"]
        self.assertAlmostEqual(d["limit"], 4.0)
        self.assertAlmostEqual(d["realized_today"], 0.45)
        self.assertGreater(d["room"], d["limit"])
        js = open(os.path.join(os.path.dirname(__file__), "..", "src", "tb", "dash", "static", "v-overview.js"), encoding="utf-8").read()
        self.assertIn('"лимит " + fx(r.limit, 2) + " · сегодня "', js)

    def test_blocks(self):
        trades = []
        for i in "ABC":
            trades += [op(i), cl(i, -0.1, reason="sl", ts_close=iso(NOW - 600 + ord(i)))]
        self.setup_state(trades)
        r = self.rr()
        self.assertFalse(r["allowed"])
        self.assertTrue(any("стопов подряд" in x for x in r["reasons"]))
        self.assertEqual(self.by(r)["stops"]["level"], "bad")

    def test_paper_day_block_looks_like_live_with_paper_flag(self):
        """Paper: блок считается как в live (строки и уровни те же), но отдаёт paper=True: страница пишет, что лимиты на сделки не влияют (guard для paper их не применяет)."""
        trades = []
        for i in "ABC":
            trades += [op(i, mode="paper"), cl(i, -0.1, reason="sl", ts_close=iso(NOW - 600 + ord(i)))]
        self.setup_state(trades)
        self.w("runtime", "exec_snapshot.json", {"ts_epoch": NOW - 1, "env": "paper", "account": {"equity": 40.0, "free": 30.0}, "positions": []})
        r = self.rr()
        row = self.by(r)["stops"]
        self.assertTrue(r["paper"])
        self.assertEqual((row["used"], row["limit"], row["label"]), (3, 3, "Стопов подряд сегодня"))
        self.assertTrue(any("стопов подряд" in x for x in r["reasons"]))

    def test_pending_takes_slot_and_risk(self):
        pend = {"p": {"id": "P1", "mode": "live", "sym": "ETH", "side": "long", "price": 1.0, "risk_usdt": 0.3, "session": "intraday-1", "ts": NOW - 5}}
        self.setup_state([op("A", risk_usdt=0.2)], pending=pend)
        r = self.rr()
        rows = self.by(r)
        self.assertEqual(rows["slots"]["used"], 2)
        self.assertFalse(r["allowed"])
        self.assertAlmostEqual(rows["daily"]["room"], 4.0 - 0.5)

    def test_no_session_and_empty_profile(self):
        self.setup_state([], session=False)
        r = self.rr()
        self.assertFalse(r["allowed"])
        self.assertIn("нет активной сессии", r["reasons"])
        self.assertNotIn("session", self.by(r))
        os.remove(os.path.join(self.root, "config", "risk_profile.json"))     # нет профиля: блока нет, страница покажет пустое состояние
        model._CACHE.clear()
        self.assertIsNone(model.build_state(self.root, now=NOW)["risk_room"])

    def test_drawdown_halt(self):
        self.setup_state([])
        self.w("state", "portfolio.json", {"venues": {"bybit": {"equity": 30.0, "free": 20.0, "ts": iso(NOW)}}, "hwm": 40.0})
        r = self.rr()
        self.assertFalse(r["allowed"])
        self.assertTrue(any("просадк" in x for x in r["reasons"]))


class TestIdeasBlock(Base):
    def test_ideas_block(self):
        self.market(price=110.0, sym="SOL")
        self.jl("trades.jsonl", [op("T-1"), cl("T-1", 0.5, r=1.2)])
        self.jl("ideas.jsonl", [
            {"ev": "idea", "id": "I1", "ts": iso(NOW - 900), "ts_epoch": NOW - 900, "sym": "SOL", "side": "long", "note": "ждём <b>пробой</b>", "level": 105.0, "status": "idea", "price": 100.0},
            {"ev": "idea", "id": "I2", "ts": iso(NOW - 800), "ts_epoch": NOW - 800, "sym": "SOL", "side": "short", "note": "слабо", "status": "skipped", "price": 100.0},
            {"ev": "mark", "id": "I1", "ts": iso(NOW - 100), "status": "taken", "trade": "T-1", "note": "вошли"}])
        self.jl("hot_outcomes.jsonl", [
            {"id": "I2", "kind": "idea", "sym": "SOL", "side": "short", "dir": "down", "h": 15, "dir_pct": -1.0, "ret_pct": 1.0, "cost_pct": 0.24},
            {"id": "E1", "kind": "event", "type": "range_5m", "sym": "AAA", "dir": "up", "h": 15, "dir_pct": 0.5, "ret_pct": 0.5, "cost_pct": 0.24, "ts0_epoch": NOW - 3000, "range_5m_pct": 1.6}])
        I = model.build_state(self.root, now=NOW)["ideas"]
        self.assertTrue(I["available"])
        self.assertEqual((I["counts"]["taken"], I["counts"]["skipped"], I["counts"]["auto_skipped"]), (1, 1, 1))
        by = {i["id"]: i for i in I["items"]}
        self.assertEqual((by["I1"]["status"], by["I1"]["trade"]["status"], by["I1"]["trade"]["pnl"], by["I1"]["trade"]["r"]), ("taken", "closed", 0.5, 1.2))
        self.assertAlmostEqual(by["I1"]["shift_pct"], 10.0)
        self.assertAlmostEqual(by["I2"]["shift_dir_pct"], -10.0)
        self.assertAlmostEqual(by["I2"]["outcomes"]["15"]["net_pct"], -1.24)
        self.assertEqual(I["auto_recent"][0]["id"], "E1")
        self.assertAlmostEqual(I["auto_recent"][0]["h"]["15"]["net_pct"], 0.26)
        code, obj = server.api_payload("/api/ideas", {}, self.root, NOW)
        self.assertEqual((code, len(obj["items"])), (200, 2))

    def test_empty(self):
        I = model.build_state(self.root, now=NOW)["ideas"]
        self.assertFalse(I["available"])
        self.assertEqual(I["items"], [])


class TestPendingBlock(Base):
    def rec(self, **kw):
        r = {"id": "PM-SOL-1027-120000", "env": "mainnet", "mode": "live", "sym": "SOL", "side": "long", "status": "open", "price": 95.0, "qty": "0.5", "sl": 93.0, "tp": 101.0,
             "lev": 5, "margin": 2.0, "risk_usdt": 0.2, "ts": NOW - 600, "expires": NOW + 3000, "ttl_min": 60, "cancel_above": 102.0, "cancel_below": None, "idea": "I7"}
        r.update(kw)
        return r

    def test_pending_rows_with_distance_age_and_ttl(self):
        self.market(price=100.0)
        self.w("state", "pending_orders.json", {"orders": {"a": self.rec(), "b": self.rec(id="PM-ETH-1027-120001", sym="ETH", side="short", price=2000.0, ts=NOW - 60)}})
        st = server.api_payload("/api/state", {"mode": ["live"]}, self.root, NOW)[1]
        rows = {r["sym"]: r for r in st["pending"]}
        sol = rows["SOL"]
        self.assertEqual((sol["side"], sol["price"], sol["sl"], sol["tp"], sol["cancel_above"], sol["idea"]), ("long", 95.0, 93.0, 101.0, 102.0, "I7"))
        self.assertAlmostEqual(sol["dist_pct"], -100.0 * 5.5 / 100.5, places=3)       # цена Bybit из фида 100.5
        self.assertEqual((sol["age_s"], sol["left_s"], sol["ttl_min"]), (600.0, 3000.0, 60.0))
        self.assertIsNone(rows["ETH"]["cur"])                                         # цены нет в фиде: не выдумываем
        self.assertIsNone(rows["ETH"]["dist_pct"])
        self.assertEqual([r["sym"] for r in st["pending"]], ["SOL", "ETH"])               # по времени постановки

    def test_mode_venue_filter_and_garbage(self):
        self.market()
        self.w("state", "pending_orders.json", {"orders": {"a": self.rec(), "t": self.rec(id="PT-SOL-1027-120000", mode="testnet"), "bad": "x", "n": {"id": "z"},
                                                          "np": self.rec(id="PM-X", price="abc")}})
        live = model.build_state(self.root, "live", "all", NOW)["pending"]
        self.assertEqual([r["id"] for r in live], ["PM-SOL-1027-120000"])
        self.assertEqual([r["id"] for r in model.build_state(self.root, "testnet", "all", NOW)["pending"]], ["PT-SOL-1027-120000"])
        self.assertEqual(len(model.build_state(self.root, "all", "bybit", NOW)["pending"]), 2)
        self.assertEqual(model.build_state(self.root, "live", "platform", NOW)["pending"], [])

    def test_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(model.build_state(self.root, "live", "all", NOW)["pending"], [])
        self.w("state", "pending_orders.json", "{битый", raw=True)
        self.assertEqual(model.build_state(self.root, "live", "all", NOW)["pending"], [])

    def test_page_has_pending_block(self):
        base = os.path.dirname(server.__file__)
        html = open(os.path.join(base, "static", "index.html"), encoding="utf-8").read()
        js = "".join(open(os.path.join(base, "static", n), encoding="utf-8").read() for n in server.SCRIPTS)
        self.assertIn('id="pending"', html)
        self.assertIn("function renderPending", js)
        self.assertNotIn("innerHTML", js)


class TestPriceFallback(Base):
    """Цена сделок по монетам вне фида: market.json -> prices.json -> hot.json, свежесть, отсутствие файлов, форматтер."""
    DOOD = dict(id="D1", sym="DOOD", entry=0.002035, notional=19.0, margin=6.3, lev=3.0, sl=0.00199, tp=0.00218, risk_usdt=0.42, fee_open=0.0076)

    def prices(self, px, age=10.0, name="prices.json"):
        self.w("runtime", name, {"ts": iso(NOW - age), "ts_epoch": NOW - age, "env": "mainnet", "n": len(px), "prices": px, "marks": {}})

    def state(self):
        return server.api_payload("/api/state", {"mode": ["live"]}, self.root, NOW)[1]

    def test_prices_json_gives_pnl_for_non_feed_coin(self):
        self.jl("trades.jsonl", [op("D1", **self.DOOD)])
        self.market()                                             # SOL есть в фиде, DOOD нет
        self.prices({"DOOD": 0.00210})
        t = self.state()["open_trades"][0]["calc"]
        self.assertTrue(t["has_price"])
        self.assertEqual((t["price"], t["price_src"], t["price_stale"]), (0.0021, "prices.json", False))
        self.assertAlmostEqual(t["price_age_s"], 10.0)
        self.assertGreater(t["pnl"], 0)
        self.assertIn("to_sl_pct", t)
        st = self.state()
        self.assertTrue(st["price_feed"]["running"])
        self.assertFalse([w for w in st["warnings"] if "scan" in w["text"]])

    def test_market_bybit_beats_prices_and_hot_is_last(self):
        self.jl("trades.jsonl", [op("S1"), op("D1", **self.DOOD)])
        self.market(price=101.0)
        self.prices({"SOL": 90.0})
        c = {t["id"]: t["calc"] for t in self.state()["open_trades"]}
        self.assertEqual((c["S1"]["price"], c["S1"]["price_src"]), (101.5, "bybit"))
        self.w("runtime", "hot.json", {"ts_epoch": NOW - 20, "top": [{"sym": "DOOD", "price": 0.0022}], "blocked": []})
        self.assertEqual(self.state()["open_trades"][1]["calc"]["price_src"], "hot.json")       # в prices.json DOOD нет
        self.prices({"DOOD": 0.0021})
        self.assertEqual(self.state()["open_trades"][1]["calc"]["price_src"], "prices.json")    # prices.json приоритетнее hot.json

    def test_stale_price_is_marked_but_pnl_counted(self):
        self.jl("trades.jsonl", [op("D1", **self.DOOD)])
        self.prices({"DOOD": 0.0021}, age=400.0)
        st = self.state()
        c = st["open_trades"][0]["calc"]
        self.assertTrue(c["has_price"] and c["price_stale"])
        self.assertAlmostEqual(c["price_age_s"], 400.0)
        self.assertTrue(any("сканер" in w["text"] for w in st["warnings"]))
        self.assertFalse(st["price_feed"]["running"])

    def test_fresh_beats_stale_in_priority(self):
        self.jl("trades.jsonl", [op("D1", **self.DOOD)])
        self.prices({"DOOD": 0.0021}, age=400.0)
        self.w("runtime", "hot.json", {"ts_epoch": NOW - 5, "top": [{"sym": "DOOD", "price": 0.0023}], "blocked": []})
        c = self.state()["open_trades"][0]["calc"]
        self.assertEqual((c["price_src"], c["price_stale"]), ("hot.json", False))

    def test_no_prices_file_no_crash_and_hint(self):
        self.jl("trades.jsonl", [op("D1", **self.DOOD)])
        self.w("state", "pending_orders.json", {"orders": {"a": {"id": "P1", "sym": "DOOD", "side": "long", "price": 0.002, "ts": NOW - 5, "expires": NOW + 99}}})
        st = self.state()
        c = st["open_trades"][0]["calc"]
        self.assertFalse(c["has_price"])
        self.assertIsNone(c["price_src"])
        self.assertFalse(st["price_feed"]["ok"])
        self.assertTrue(any("scan --watch" in w["text"] and "DOOD" in w["text"] for w in st["warnings"]))
        self.assertIsNone(st["pending"][0]["cur"])
        self.w("runtime", "prices.json", "{битый", raw=True)
        self.assertFalse(self.state()["price_feed"]["ok"])

    def test_pending_and_ideas_use_scan_prices(self):
        self.prices({"DOOD": 0.0021, "PEPE": 0.0000123})
        self.w("state", "pending_orders.json", {"orders": {"a": {"id": "P1", "sym": "DOOD", "side": "long", "price": 0.002, "ts": NOW - 5, "expires": NOW + 99}}})
        self.jl("ideas.jsonl", [{"ev": "idea", "id": "I1", "ts": iso(NOW - 60), "ts_epoch": NOW - 60, "sym": "PEPE", "side": "long", "price": 0.0000120, "status": "idea"}])
        st = self.state()
        p = st["pending"][0]
        self.assertEqual((p["cur"], p["price_src"], p["price_stale"]), (0.0021, "prices.json", False))
        self.assertAlmostEqual(p["dist_pct"], 100.0 * (0.002 / 0.0021 - 1.0), places=3)
        i = st["ideas"]["items"][0]
        self.assertEqual((i["now_price"], i["now_src"]), (0.0000123, "prices.json"))

    def test_platform_venue_ignores_bybit_scan_prices(self):
        self.jl("trades.jsonl", [op("P1", venue="platform", sym="DOOD")])
        self.prices({"DOOD": 0.0021})
        self.assertFalse(self.state()["open_trades"][0]["calc"]["has_price"])

    def test_fmt_price_significant_digits(self):
        f = model.fmt_price
        self.assertEqual(f(0.002035), "0.002035")
        self.assertEqual(f(0.00199), "0.001990")
        self.assertEqual(f(0.00218), "0.002180")
        self.assertEqual(f(0.0000123), "0.00001230")
        self.assertEqual(f(0.0125), "0.012500")
        self.assertEqual(f(0.5), "0.50000")
        self.assertEqual(f(1.2345678), "1.2346")
        self.assertEqual(f(25.0), "25.000")
        self.assertEqual(f(150.126), "150.13")
        self.assertEqual(f(67123.456), "67123.5")
        self.assertEqual(f(None), "–")
        self.assertEqual(f(float("nan")), "–")


class TestApiAndServer(Base):
    def test_api_payload_endpoints(self):
        self.market()
        for p in ("/api/state", "/api/market", "/api/hot", "/api/health", "/api/trades", "/api/ping"):
            code, obj = server.api_payload(p, {}, self.root, NOW)
            self.assertEqual(code, 200, p)
            json.dumps(obj)
        self.assertEqual(server.api_payload("/api/nope", {}, self.root, NOW)[0], 404)
        _, st = server.api_payload("/api/state", {"mode": ["../etc"], "venue": ["x"]}, self.root, NOW)
        self.assertEqual((st["filter"]["mode"], st["filter"]["venue"]), ("live", "all"))

    def get(self, srv, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (srv.server_address[1], path), headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_server_live(self):
        self.jl("trades.jsonl", [op("A")])
        self.market()
        srv = server.make_server(0, self.root)
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        try:
            self.assertEqual(srv.server_address[0], "127.0.0.1")
            code, h, body = self.get(srv, "/api/state")
            self.assertEqual(code, 200)
            self.assertIn("application/json", h["Content-Type"])
            self.assertEqual(json.loads(body.decode("utf-8"))["open_trades"][0]["id"], "A")
            for path, ctype in [("/", "text/html"), ("/app.css", "text/css"), ("/favicon.svg", "image/svg")] + [("/" + n, "application/javascript") for n in server.SCRIPTS]:
                code, h, body = self.get(srv, path)
                self.assertEqual(code, 200, path)
                self.assertIn(ctype, h["Content-Type"])
                self.assertTrue(len(body) > 100)
            self.assertEqual(self.get(srv, "/../../etc/passwd")[0], 404)
            self.assertEqual(self.get(srv, "/state/trades.jsonl")[0], 404)  # файлы проекта напрямую не отдаются
            self.assertEqual(self.get(srv, "/api/state", {"Host": "evil.example.com"})[0], 403)
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                req = urllib.request.Request("http://127.0.0.1:%d/api/state" % srv.server_address[1], data=b"{}", method=method)
                try:
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
                    self.fail("%s должен быть запрещён" % method)
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 405)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_static_page_is_self_contained_and_safe(self):
        d = server.STATIC
        for name in ("index.html", "app.css") + tuple(server.SCRIPTS):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                txt = f.read()
            self.assertIsNone(re.search(r"https?://", txt.replace("http://www.w3.org/2000/svg", "")), name)  # без CDN и внешних ресурсов
            if name.endswith(".js"):
                self.assertNotIn("innerHTML", txt, name)
                self.assertNotIn("insertAdjacentHTML", txt, name)
                self.assertNotIn("document.write", txt, name)
                if name == "v-control.js":              # WP12: единственный скрипт с POST, и только под /api/ctl/ с CSRF-заголовком
                    self.assertIn('method: "POST"', txt)
                    self.assertIn("X-TB-CSRF", txt)
                    self.assertEqual(sorted(set(re.findall(r'ctlPost\("(/api/ctl/[a-z]+)"', txt))), ["/api/ctl/challenge", "/api/ctl/command", "/api/ctl/llmkey", "/api/ctl/login", "/api/ctl/logout"])
                    self.assertNotIn("method:", txt.replace('method: "POST"', ""))
                else:
                    self.assertNotIn("method:", txt.replace("method: \"GET\"", ""), name)  # нет запросов с телом/методами записи
        html = open(os.path.join(d, "index.html"), encoding="utf-8").read()
        for n in server.SCRIPTS:                      # каждый скрипт подключён и раздаётся сервером
            self.assertIn('src="/%s"' % n, html)
        self.assertIn(server.EXT_MARK, html)          # сюда сервер вставляет скрипты расширений (WP12)
        self.assertNotIn("<script>", html)            # CSP не пускает инлайн-скрипты


class TestNoSecrets(Base):
    def test_scrub_and_sentinel(self):
        os.environ["BYBIT_API_KEY"] = "SENTINEL_KEY_123"
        os.environ["BYBIT_API_SECRET"] = "SENTINEL_SECRET_456"
        try:
            self.w("state", "portfolio.json", {"venues": {"bybit": {"equity": 10.0, "free": 5.0, "api_key": "SENTINEL_LEAK", "ts": iso(NOW)}}, "hwm": 10.0})
            self.w("state", "learning.json", {"active": True, "secret": "SENTINEL_LEAK2", "note": "ok"})
            self.jl("trades.jsonl", [op("A", api_secret="SENTINEL_LEAK3")])
            self.market()
            for p in ("/api/state", "/api/market", "/api/hot", "/api/health", "/api/trades"):
                _, obj = server.api_payload(p, {"mode": ["all"]}, self.root, NOW)
                text = json.dumps(obj, ensure_ascii=False)
                self.assertNotIn("SENTINEL", text, p)
        finally:
            del os.environ["BYBIT_API_KEY"]
            del os.environ["BYBIT_API_SECRET"]

    def test_dash_sources_do_not_touch_keys(self):
        base = os.path.dirname(server.__file__)
        for name in ("model.py", "server.py"):
            src = open(os.path.join(base, name), encoding="utf-8").read()
            for bad in ("/.config", "launcher", "BYBIT_API", ".env\"", "urlopen", "tb.venues", "os.environ", "expanduser", "APPDATA"):
                self.assertFalse(bad in src, "%s: %s" % (name, bad))


if __name__ == "__main__":
    unittest.main()
