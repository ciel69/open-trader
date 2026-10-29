"""Деривативы из тикеров скана: события oi_spike / oi_flush / funding_extreme, антиспам, тепловая карта heat.json, проход через публикатор шины. Без сети."""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import bus
from tb.marketdata import hotscan as H
from tb.platform import scansvc

NOW = 1793270000.0


def row(sym="SOL", **kw):
    r = {"sym": sym, "symbol": sym + "USDT", "price": 100.0, "score": 0.5, "tradable": True, "corr_group": False, "chg_24h_pct": 2.0, "ret_5m_pct": 0.2, "ret_15m_pct": 0.4,
         "range_5m_pct": 0.3, "turn_surge_5m": 1.0, "oi_chg_15m_pct": 0.5, "oi_chg_1h_pct": 1.0, "oi_musd": 50.0, "funding_pct": 0.01, "turn_24h_musd": 300.0, "spread_bps": 1.0}
    r.update(kw)
    return r


class Gate(unittest.TestCase):
    def setUp(self):
        self.g = H.DerivGate({})

    def test_oi_spike_with_price_direction(self):
        ev = self.g.pick([row(oi_chg_15m_pct=9.0, ret_15m_pct=1.4)], NOW)
        self.assertEqual([(e["type"], e["sym"], e["direction"], e["wake"]) for e in ev], [("oi_spike", "SOL", "up", True)])
        self.assertEqual(ev[0]["oi_chg_15m_pct"], 9.0)
        flat = H.DerivGate({}).pick([row(oi_chg_15m_pct=9.0, ret_15m_pct=0.1)], NOW)
        self.assertIsNone(flat[0]["direction"])                       # цена стоит: направления нет, это набор позиций

    def test_oi_flush_needs_big_price_move(self):
        self.assertEqual(self.g.pick([row(oi_chg_15m_pct=-6.0, ret_15m_pct=0.2)], NOW), [])
        ev = H.DerivGate({}).pick([row(oi_chg_15m_pct=-6.0, ret_15m_pct=-2.5)], NOW)
        self.assertEqual((ev[0]["type"], ev[0]["direction"]), ("oi_flush", "down"))

    def test_funding_extreme_both_signs_no_direction(self):
        for fr in (0.12, -0.12):
            ev = H.DerivGate({}).pick([row(funding_pct=fr)], NOW)
            self.assertEqual((ev[0]["type"], ev[0]["funding_pct"], ev[0]["direction"]), ("funding_extreme", fr, None))

    def test_small_open_interest_is_ignored(self):
        self.assertEqual(self.g.pick([row(oi_musd=0.3, oi_chg_15m_pct=20.0)], NOW), [])

    def test_cooldown_and_loud_cap_make_rest_quiet(self):
        self.assertEqual(len(self.g.pick([row(oi_chg_15m_pct=9.0)], NOW)), 1)
        self.assertEqual(self.g.pick([row(oi_chg_15m_pct=9.0)], NOW + 60), [])                       # cooldown на (вид, монета)
        g = H.DerivGate({"deriv_max_loud_per_hour": 2, "deriv_max_per_scan": 10})
        evs = g.pick([row("S%d" % i, oi_chg_15m_pct=9.0) for i in range(5)], NOW)
        self.assertEqual([e["wake"] for e in evs], [True, True, False, False, False])                  # сверх потолка громких: тихие (лог и статистика)

    def test_weak_signal_is_quiet(self):
        ev = self.g.pick([row(oi_chg_15m_pct=4.5)], NOW)                                              # чуть выше порога: score < deriv_wake_score
        self.assertEqual((ev[0]["type"], ev[0]["wake"]), ("oi_spike", False))


class HeatAndBus(unittest.TestCase):
    def test_heat_table_sorted_by_turnover_and_limited(self):
        rows = [row("A", turn_24h_musd=10.0), row("B", turn_24h_musd=500.0), row("C", turn_24h_musd=50.0, tradable=False)]
        h = H.build_heat(rows, "mainnet", NOW, n=2)
        self.assertEqual([r["sym"] for r in h["rows"]], ["B", "C"])
        self.assertEqual(h["cols"], list(H.HEAT_COLS))
        self.assertIn("oi_chg_15m_pct", h["rows"][0])
        self.assertFalse(h["rows"][1]["tradable"])

    def test_event_passes_publisher_to_bus_with_new_fields(self):
        root = tempfile.mkdtemp(prefix="tbderiv_")
        try:
            os.makedirs(os.path.join(root, "runtime"))
            got = []
            pub = scansvc.Publisher(root, {}, os.path.join(root, "runtime", "hot_events.jsonl"), emit=lambda t, d, sym: got.append((t, d, sym)))
            ev = H.make_deriv_event("oi_spike", row(oi_chg_15m_pct=9.0, ret_15m_pct=1.4), 2.25, NOW, True)
            ev.update(type="oi_spike")
            conv = pub.convert(ev)
            self.assertIsNotNone(conv)
            d, sym = conv
            self.assertEqual((d["kind"], d["oi_chg_15m_pct"], d["dir"]), ("oi_spike", 9.0, "up"))
            seq = bus.emit("scanner_signal", d, sym=sym, root=root, now=NOW)
            e = [x for x in bus.read(0, root=root) if x["seq"] == seq][0]
            self.assertEqual((e["d"]["kind"], e["d"]["oi_chg_15m_pct"]), ("oi_spike", 9.0))
            liq = bus.emit("scanner_signal", {"kind": "liq_cascade", "liq_usd": 250000, "liq_side": "long", "dir": "down", "px": 100.0}, sym="SOL", root=root, now=NOW + 1)
            e2 = [x for x in bus.read(0, root=root) if x["seq"] == liq][0]
            self.assertEqual((e2["d"]["kind"], e2["d"]["liq_side"], e2["d"]["liq_usd"]), ("liq_cascade", "long", 250000))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class DashModel(unittest.TestCase):
    def test_deriv_info_reads_heat_and_liq_and_survives_garbage(self):
        from tb.dash import model
        root = tempfile.mkdtemp(prefix="tbheat_")
        try:
            os.makedirs(os.path.join(root, "runtime"))
            self.assertEqual(model.deriv_info(root, NOW), {"heat": {"available": False}, "liq": {"available": False}, "flow": {"available": False}})
            with open(os.path.join(root, "runtime", "flow.json"), "w", encoding="utf-8") as f:
                json.dump({"ts_epoch": NOW - 2, "xex_ok": True, "syms": [{"sym": "BTC", "cvd_5m_pct": -20.0}, "junk"]}, f)
            fl = model.deriv_info(root, NOW)["flow"]
            self.assertEqual((fl["available"], fl["xex_ok"], [r["sym"] for r in fl["syms"]]), (True, True, ["BTC"]))
            os.remove(os.path.join(root, "runtime", "flow.json"))
            with open(os.path.join(root, "runtime", "heat.json"), "w", encoding="utf-8") as f:
                json.dump(H.build_heat([row("A", turn_24h_musd=5.0), row("B", turn_24h_musd=9.0)], "mainnet", NOW), f)
            with open(os.path.join(root, "runtime", "liq.json"), "w", encoding="utf-8") as f:
                json.dump({"ts_epoch": NOW - 4, "totals_1h": {"long": 10, "short": 20}, "n_total": 3, "syms": [{"sym": "A", "long_1h": 10, "short_1h": 20}, "junk", {"x": 1}]}, f)
            d = model.deriv_info(root, NOW)
            self.assertTrue(d["heat"]["available"])
            self.assertEqual([r["sym"] for r in d["heat"]["rows"]], ["B", "A"])
            self.assertEqual(d["liq"]["totals"], {"long": 10, "short": 20})
            self.assertEqual([r["sym"] for r in d["liq"]["syms"]], ["A"])               # мусорные элементы отброшены
            with open(os.path.join(root, "runtime", "heat.json"), "w", encoding="utf-8") as f:
                f.write("{битый")
            self.assertFalse(model.deriv_info(root, NOW)["heat"]["available"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_scanner_rows_carry_oi_fields(self):
        """Строки скана получают oi_chg_1h_pct и oi_musd (для карты и событий); окно часа накапливается через keep_s."""
        sc = H.HotScanner({"min_turnover": 0, "max_spread_bps": 1000, "max_lot": 1e9})
        tick = lambda oi: {"SOLUSDT": {"price": 100.0, "mark": 100.0, "bid": 99.99, "ask": 100.01, "turn24": 5e8, "chg24": 1.0, "oi": oi, "funding_pct": 0.01}}
        for k in range(130):                                                           # 65 минут по 30 с
            sc.step(tick(1000.0 if k < 120 else 1100.0), {"SOLUSDT": {"status": "Trading", "min_qty": 0.1, "qty_step": "0.1", "tick": "0.01", "min_notional": 5.0, "max_leverage": 25}}, NOW + k * 30)
        top, trad, blocked, stats = sc.step(tick(1100.0), {"SOLUSDT": {"status": "Trading", "min_qty": 0.1, "qty_step": "0.1", "tick": "0.01", "min_notional": 5.0, "max_leverage": 25}}, NOW + 130 * 30)
        r = (trad + blocked)[0]
        self.assertAlmostEqual(r["oi_musd"], 1100.0 * 100.0 / 1e6, places=3)
        self.assertIsNotNone(r["oi_chg_1h_pct"])
        self.assertGreater(r["oi_chg_1h_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
