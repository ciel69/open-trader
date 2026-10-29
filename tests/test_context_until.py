"""Конец макроокна risk_off (until_epoch) и текст «до HH:MM UTC (ещё N мин)» на карточке состояния: замечание про окно новостей не выглядит вечным."""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.dash import model
from tb.marketdata import context


class MacroUntil(unittest.TestCase):
    def test_compute_macro_until(self):
        now = 1_800_000_000.0
        ev = [{"name": "FOMC", "ts_iso": context.epoch_to_iso(now - 600), "impact": "high"}, {"name": "NFP", "ts_iso": context.epoch_to_iso(now + 86400), "impact": "high"}]
        m = context.compute_macro(ev, now, 30, ["high"])
        self.assertTrue(m["active"])
        self.assertAlmostEqual(m["active_events"][0]["until_epoch"], now - 600 + 1800, places=1)
        self.assertEqual(m["next_event"]["name"], "NFP")

    def test_intel_union_until(self):
        root = tempfile.mkdtemp(prefix="tbctx_")
        try:
            os.makedirs(os.path.join(root, "runtime", "intel"))
            now = 1_800_000_000.0
            with open(os.path.join(root, "runtime", "intel", "blackouts.json"), "w", encoding="utf-8") as f:
                json.dump({"windows": [{"name": "FOMC", "from": context.epoch_to_iso(now - 3600), "to": context.epoch_to_iso(now + 240), "v": True}]}, f)
            m = context.intel_union({"active": False, "active_events": []}, root, now)
            self.assertTrue(m["active"])
            self.assertAlmostEqual(m["active_events"][0]["until_epoch"], now + 240, places=1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_model_until_text(self):
        now = 1_800_000_000.0
        ctx = {"sources": {"macro": {"ok": True, "active": True, "active_events": [{"name": "FOMC", "until_epoch": now + 250}, {"name": "CPI", "until_epoch": now + 90}]}}}
        self.assertAlmostEqual(model.macro_until_s(ctx, now), 250.0)
        self.assertIsNone(model.macro_until_s({"sources": {"macro": {"active": False}}}, now))
        self.assertIsNone(model.macro_until_s({"sources": {"macro": {"active": True, "active_events": [{"name": "x"}]}}}, now))
        t = model.macro_until_text(250.0, now)
        self.assertIn("ещё 5 мин", t)
        self.assertIn("UTC", t)
        self.assertEqual(model.macro_until_text(None, now), "")
