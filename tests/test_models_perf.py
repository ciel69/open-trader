"""Вкладка «Модели»: результат по моделям (сделки с тегом model.*) вместе с учётом вызовов LLM; исключённые сбоем платформы сделки не считаются."""
import json
import os
import shutil
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.llm import dashapi, usage as U

NOW = 1793340000.0


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class ModelsPerf(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbmp_")
        os.makedirs(os.path.join(self.root, "state"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def trade(self, tid, model, pnl, risk=1.0, ago=3600):
        rows = [{"ev": "open", "id": tid, "ts": iso(NOW - ago - 600), "sym": "SOL", "side": "long", "mode": "paper", "tags": ["paper", "model." + model], "risk_usdt": risk},
                {"ev": "close", "id": tid, "ts_close": iso(NOW - ago), "pnl_usdt": pnl, "r": pnl / risk, "reason": "tp"}]
        with open(paths.state("trades.jsonl", root=self.root), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def calls(self, profile, n, usd=0.0, err=0):
        with open(U.calls_file(self.root), "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"t": NOW - 100 - i, "profile": profile, "role": "entry_scout", "in": 1000, "out": 100, "usd": usd, "ok": i >= err, "lat_ms": 900}) + "\n")

    def test_net_efficiency_and_reliability(self):
        self.trade("A1", "ds-flash", 1.0)
        self.trade("A2", "ds-flash", -0.4)
        self.trade("B1", "lm-oss", 0.6)
        self.calls("ds-flash", 50, usd=0.01)
        self.calls("lm-oss", 200, usd=0.0, err=20)
        r = dashapi.models_perf(self.root, NOW, 7, "paper")
        rows = {x["id"]: x for x in r["rows"]}
        a, b = rows["ds-flash"], rows["lm-oss"]
        self.assertEqual((a["n"], a["wins"], a["calls"]), (2, 1, 50))
        self.assertAlmostEqual(a["pnl"], 0.6)
        self.assertAlmostEqual(a["usd"], 0.5)
        self.assertAlmostEqual(a["net"], 0.1)                          # PnL минус расход на ИИ
        self.assertAlmostEqual(a["pnl_per_100_calls"], 1.2)
        self.assertAlmostEqual(a["pnl_per_usd"], 1.2)
        self.assertEqual((b["n"], b["usd"], b["pnl_per_usd"]), (1, 0.0, None))     # бесплатная модель: PnL/$ не считается
        self.assertAlmostEqual(b["err_rate"], 0.1)
        self.assertFalse(a["reliable"])
        self.assertEqual(r["min_n"], 30)
        self.assertEqual(r["rows"][0]["id"], "lm-oss")                 # сортировка по чистому результату: 0.6 против 0.1

    def test_excluded_trade_and_old_trade_are_ignored(self):
        self.trade("X1", "ds-flash", 6.3)
        self.trade("Y1", "ds-flash", 1.0, ago=20 * 86400)
        self.trade("Z1", "ds-flash", 0.5)
        with open(paths.state("exclude_trades.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"ids": ["X1"]}, f)
        r = dashapi.models_perf(self.root, NOW, 7, "paper")
        row = [x for x in r["rows"] if x["id"] == "ds-flash"][0]
        self.assertEqual(row["n"], 1)
        self.assertAlmostEqual(row["pnl"], 0.5)

    def test_api_route_and_empty(self):
        code, obj = dashapi.api("/api/settings/models_perf", {"days": ["1"], "mode": ["paper"]}, self.root, NOW)
        self.assertEqual(code, 200)
        self.assertEqual(obj["rows"], [])


if __name__ == "__main__":
    unittest.main()
