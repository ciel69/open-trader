import argparse
import contextlib
import io
import json
import os

import _bootstrap  # noqa: F401
from _wp9 import TmpState
from tb.core import journal
from tb.dash import model

FEE = journal.FEE


def ns(**kw):
    return argparse.Namespace(**kw)


def open_trade(tid="T1", side="long", entry=100.0, margin=10.0, lev=5, sl=98.0, tp=110.0, **extra):
    d = dict(id=tid, sym="SOL", side=side, entry=entry, lev=lev, margin=margin, sl=sl, tp=tp, thesis="t", news="", regime="range", tags="bybit,plan",
             mode="live", ctx={}, ts_open="2026-10-28T01:00:00Z")
    d.update(extra)
    with contextlib.redirect_stdout(io.StringIO()):
        journal.cmd_open(ns(**d))


def close(tid, **kw):
    d = dict(id=tid, exit=0.0, reason="manual", funding=None, note="", path={})
    d.update(kw)
    with contextlib.redirect_stdout(io.StringIO()):
        journal.cmd_close(ns(**d))


def quiet(fn, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        fn(ns(**kw))


def last(tid="T1"):
    return journal._load()[0][tid]


class JournalPlanTest(TmpState):
    def test_old_records_read_identically(self):
        open_trade()
        close("T1", exit=104.0, reason="tp")
        t = last()
        notional = 50.0
        fees = 2 * FEE * notional
        self.assertAlmostEqual(t["pnl_usdt"], round((104 / 100 - 1) * notional - fees, 4), places=4)
        self.assertNotIn("qty", t)
        self.assertNotIn("adds", t)

    def test_plan_fields_and_tags(self):
        open_trade(plan_id="p1", plan_rev=2, exp="sweep-v1", var="A", setup="reject", hyp="H3", plan_ctx={"a": 1})
        t = last()
        for k, v in (("plan_id", "p1"), ("plan_rev", 2), ("exp", "sweep-v1"), ("var", "A"), ("setup", "reject"), ("hyp", "H3")):
            self.assertEqual(t[k], v)
        for tg in ("bybit", "plan", "plan:p1", "exp:sweep-v1", "var:A", "setup:reject"):
            self.assertIn(tg, t["tags"])
        open_trade("T2", tags="exp:x1,bybit", exp="x1")
        self.assertEqual(last("T2")["tags"].count("exp:x1"), 1)

    def test_part_long_then_close(self):
        open_trade()                                  # notional 50, qty 0.5, R0 = 2/100*50 = 1.0
        quiet(journal.cmd_part, id="T1", px=104.0, qty=0.2)
        t = last()
        self.assertAlmostEqual(t["qty"], 0.3)
        self.assertAlmostEqual(t["realized_parts"], 0.8)
        self.assertAlmostEqual(t["fees_parts"], FEE * 104.0 * 0.2)
        self.assertAlmostEqual(t["notional"], 30.0)
        self.assertEqual(t["risk_usdt"], 1.0)
        close("T1", exit=106.0, reason="tp")
        t = last()
        gross = 0.8 + 0.3 * 6.0
        fees = FEE * 50.0 + FEE * 104.0 * 0.2 + FEE * 0.3 * 106.0
        self.assertAlmostEqual(t["pnl_usdt"], round(gross - fees, 4), places=4)
        self.assertAlmostEqual(t["fees_usdt"], round(fees, 4), places=4)
        self.assertAlmostEqual(t["r"], round((gross - fees) / 1.0, 2), places=2)

    def test_part_short(self):
        open_trade("S1", side="short", sl=102.0, tp=90.0)
        quiet(journal.cmd_part, id="S1", px=96.0, qty=0.25)
        t = last("S1")
        self.assertAlmostEqual(t["realized_parts"], 1.0)
        close("S1", exit=94.0, reason="tp")
        t = last("S1")
        gross = 1.0 + 0.25 * 6.0
        fees = FEE * 50.0 + FEE * 96.0 * 0.25 + FEE * 0.25 * 94.0
        self.assertAlmostEqual(t["pnl_usdt"], round(gross - fees, 4), places=4)

    def test_add_recomputes_average_and_keeps_r0(self):
        open_trade()                                   # qty 0.5 @100
        quiet(journal.cmd_add, id="T1", px=106.0, qty=0.5)
        t = last()
        self.assertAlmostEqual(t["entry"], 103.0)
        self.assertAlmostEqual(t["qty"], 1.0)
        self.assertAlmostEqual(t["notional"], 103.0)
        self.assertAlmostEqual(t["margin"], 10.0 + 0.5 * 106.0 / 5)
        self.assertEqual(t["risk_usdt"], 1.0)
        self.assertEqual(t["entry0"], 100.0)
        self.assertEqual(t["adds"], 1)
        close("T1", exit=110.0, reason="tp")
        t = last()
        gross = (110.0 - 103.0) * 1.0
        fees = FEE * 50.0 + FEE * 106.0 * 0.5 + FEE * 1.0 * 110.0
        self.assertAlmostEqual(t["pnl_usdt"], round(gross - fees, 4), places=4)

    def test_add_then_part_then_close_chain(self):
        open_trade()
        quiet(journal.cmd_add, id="T1", px=104.0, qty=0.5)          # avg 102, qty 1.0
        quiet(journal.cmd_part, id="T1", px=108.0, qty=0.4)          # +2.4
        quiet(journal.cmd_adjust, id="T1", sl=102.5, by="rule:be")
        t = last()
        self.assertAlmostEqual(t["qty"], 0.6)
        self.assertAlmostEqual(t["entry"], 102.0)
        self.assertEqual((t["sl"], t["sl0"]), (102.5, 98.0))
        self.assertAlmostEqual(t["realized_parts"], 2.4)
        close("T1", exit=107.0, reason="tp")
        t = last()
        gross = 2.4 + 0.6 * 5.0
        fees = FEE * 50.0 + FEE * 104.0 * 0.5 + FEE * 108.0 * 0.4 + FEE * 0.6 * 107.0
        self.assertAlmostEqual(t["pnl_usdt"], round(gross - fees, 4), places=4)

    def test_pnl_override(self):
        open_trade()
        quiet(journal.cmd_part, id="T1", px=104.0, qty=0.2)
        close("T1", exit=105.0, reason="tp", pnl_override=1.234, fees_usdt=0.2)
        t = last()
        self.assertEqual(t["pnl_usdt"], 1.234)
        self.assertEqual(t["fees_usdt"], 0.2)
        self.assertEqual(t["r"], 1.23)
        open_trade("T3")                                             # без частей override тоже применим, прежнее поведение без него не меняется
        close("T3", exit=101.0, pnl_override=0.5)
        self.assertEqual(last("T3")["pnl_usdt"], 0.5)

    def test_events_after_close_and_unknown_ignored(self):
        open_trade()
        close("T1", exit=101.0)
        before = dict(last())
        journal._append({"ev": "add", "id": "T1", "px": 105.0, "qty": 1.0, "fee": 0.0})
        journal._append({"ev": "part", "id": "ZZZ", "px": 105.0, "qty": 1.0})
        journal._append({"ev": "mystery", "id": "T1"})
        self.assertEqual(last(), before)

    def test_part_qty_validation(self):
        open_trade()
        with self.assertRaises(SystemExit):
            quiet(journal.cmd_part, id="T1", px=104.0, qty=5.0)
        with self.assertRaises(SystemExit):
            quiet(journal.cmd_part, id="NOPE", px=104.0, qty=0.1)

    def test_round_trip_file_lines_valid_json(self):
        open_trade(exp="e1")
        quiet(journal.cmd_add, id="T1", px=101.0, qty=0.1)
        quiet(journal.cmd_part, id="T1", px=103.0, qty=0.1)
        quiet(journal.cmd_adjust, id="T1", sl=99.0)
        close("T1", exit=104.0)
        with open(journal.PATH, encoding="utf-8") as f:
            evs = [json.loads(l)["ev"] for l in f]
        self.assertEqual(evs, ["open", "add", "part", "adjust", "close"])

    def test_dash_model_survives_new_events(self):
        open_trade(exp="e1")
        quiet(journal.cmd_add, id="T1", px=101.0, qty=0.1)
        quiet(journal.cmd_part, id="T1", px=103.0, qty=0.1)
        quiet(journal.cmd_adjust, id="T1", sl=99.0)
        close("T1", exit=104.0, pnl_override=0.7, fees_usdt=0.1)
        os.makedirs(os.path.join(self.dir, "state"))
        with open(journal.PATH, encoding="utf-8") as src, open(os.path.join(self.dir, "state", "trades.jsonl"), "w", encoding="utf-8") as dst:
            dst.write(src.read())
        trades, warn = model.load_trades(root=self.dir)       # прежний код дашборда: новые события не должны его ронять
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "closed")
        self.assertAlmostEqual(trades[0]["pnl_usdt"], 0.7)
        self.assertEqual(warn["orphan_close"], 0)

    def test_analyze_by_and_review(self):
        for i, (e, ex) in enumerate((("e1", 104.0), ("e1", 99.0), ("e2", 103.0))):
            open_trade("A%d" % i, exp=e, setup="s")
            close("A%d" % i, exit=ex)
        open_trade("OLD")                              # без exp
        close("OLD", exit=101.0)
        trades, order = journal._load()
        rows = [trades[i] for i in order if trades[i].get("ts_close")]
        rep = journal.analyze(rows, min_bucket=2, by="exp")
        g = {x["value"]: x for x in rep["by"]["groups"]}
        self.assertEqual(g["e1"]["n"], 2)
        self.assertIn("note", g["e2"])
        self.assertEqual(g["нет"]["n"], 1)
        self.assertIn("по exp", journal.format_analyze(rep))
        self.assertNotIn("by", journal.analyze(rows))
        tg = journal.analyze_by(rows, "tag", 1)
        self.assertTrue(any(x["value"] == "exp:e1" for x in tg))
        with self.assertRaises(ValueError):
            journal.analyze_by(rows, "bogus")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            journal.cmd_review(ns(mode="live"))
            journal.cmd_analyze(ns(mode="live", min_bucket=2, json=True, by="setup"))
        self.assertIn("exp=e1", buf.getvalue())
