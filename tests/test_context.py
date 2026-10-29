import copy
import json
import os
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import context  # noqa: E402
from tb.marketdata import history  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1793000100.0


def iso(t):
    from tb.marketdata.data import epoch_to_iso
    return epoch_to_iso(t)


class Base(unittest.TestCase):
    def setUp(self):
        self._orig = context.http_get_json
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.cfg = context.load_config(os.path.join(ROOT, "config", "context_config.json"))
        self.set_events([])
        self.overrides = {}
        context.http_get_json = self.router

    def tearDown(self):
        context.http_get_json = self._orig
        self.tmp.cleanup()

    def set_events(self, ev):
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        with open(os.path.join(self.root, "config", self.cfg["macro_events_file"]), "w") as f:
            json.dump(ev, f)

    def router(self, url, params, timeout=10):
        for key, val in self.overrides.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        return context.fixture_router(url, params, timeout)

    def cycle(self, now=NOW, cache=None):
        return context.poll_cycle(self.cfg, self.root, {} if cache is None else cache, now)


class TestRisk(Base):
    def test_baseline_not_risk_off(self):
        ctx = self.cycle()
        self.assertFalse(ctx["risk_off"])
        self.assertEqual(ctx["risk_off_reasons"], [])
        self.assertEqual(ctx["degraded_sources"], [])
        for k in ("dvol_ETH", "dvol_BTC", "fear_greed", "global", "derivs_ETH", "derivs_BTC", "macro"):
            self.assertTrue(ctx["sources"][k]["ok"], k)
        self.assertAlmostEqual(ctx["sources"]["derivs_ETH"]["funding_hourly"], 0.0001 / 8.0)
        self.assertEqual(ctx["sources"]["global"]["btc_dominance_pct"], 52.3)
        self.assertEqual(ctx["sources"]["derivs_ETH"]["long_short_ratio"], 1.35)

    def test_dvol_high(self):
        self.overrides["deribit"] = {"result": {"data": [[1.7e12, 1, 1, 1, 95.0]]}}
        ctx = self.cycle()
        self.assertTrue(ctx["risk_off"])
        self.assertTrue(any(r.startswith("dvol_high") for r in ctx["risk_off_reasons"]))

    def test_dvol_threshold_per_symbol(self):
        self.cfg["thresholds"]["dvol_max"] = {"ETH": 1000, "BTC": 50}
        ctx = self.cycle()  # фикстура DVOL = 51.25 -> только BTC
        self.assertEqual(len(ctx["risk_off_reasons"]), 1)
        self.assertIn("BTC", ctx["risk_off_reasons"][0])

    def test_fng_extreme_fear_and_greed(self):
        for val, key in (("10", "fear"), ("90", "greed")):
            self.overrides["alternative.me"] = {"data": [{"value": val, "value_classification": "x"}]}
            ctx = self.cycle()
            self.assertTrue(ctx["risk_off"], val)
            self.assertIn(key, ctx["risk_off_reasons"][0])

    def test_fng_boundaries(self):
        self.overrides["alternative.me"] = {"data": [{"value": "16"}]}
        self.assertFalse(self.cycle()["risk_off"])
        self.overrides["alternative.me"] = {"data": [{"value": "15"}]}
        self.assertTrue(self.cycle()["risk_off"])

    def test_macro_window_by_time(self):
        ev_ts = NOW + 20 * 60
        self.set_events([{"ts_iso": iso(ev_ts), "name": "FOMC", "impact": "high"}])
        ctx = self.cycle(NOW)  # за 20 мин до события, окно 30
        self.assertTrue(ctx["risk_off"])
        self.assertIn("FOMC", ctx["risk_off_reasons"][0])
        self.assertTrue(self.cycle(ev_ts + 29 * 60)["risk_off"])   # после события
        self.assertFalse(self.cycle(ev_ts + 31 * 60)["risk_off"])  # окно закрылось
        self.assertFalse(self.cycle(ev_ts - 31 * 60)["risk_off"])  # ещё не началось

    def test_macro_low_impact_ignored_and_next_event(self):
        self.set_events([{"ts_iso": iso(NOW + 60), "name": "minor", "impact": "low"},
                         {"ts_iso": iso(NOW + 3 * 3600), "name": "CPI", "impact": "high"}])
        ctx = self.cycle()
        self.assertFalse(ctx["risk_off"])
        self.assertEqual(ctx["sources"]["macro"]["next_event"]["name"], "CPI")

    def test_macro_bad_file_degraded_not_risk_off(self):
        with open(os.path.join(self.root, "config", self.cfg["macro_events_file"]), "w") as f:
            f.write("{not json")
        ctx = self.cycle()
        self.assertFalse(ctx["risk_off"])
        self.assertIn("macro", ctx["degraded_sources"])

    def test_multiple_reasons(self):
        self.overrides["deribit"] = {"result": {"data": [[1.7e12, 1, 1, 1, 95.0]]}}
        self.overrides["alternative.me"] = {"data": [{"value": "5"}]}
        self.set_events([{"ts_iso": iso(NOW), "name": "X", "impact": "high"}])
        ctx = self.cycle()
        self.assertGreaterEqual(len(ctx["risk_off_reasons"]), 4)


class TestDegradation(Base):
    def test_unavailable_source_is_degraded_without_risk_off(self):
        self.overrides["deribit"] = history.HttpError(503, "down")
        self.overrides["alternative.me"] = OSError("timeout")
        ctx = self.cycle()
        self.assertFalse(ctx["risk_off"])
        self.assertEqual(sorted(ctx["degraded_sources"]), ["dvol_BTC", "dvol_ETH", "fear_greed"])
        self.assertFalse(ctx["sources"]["fear_greed"]["ok"])
        self.assertIn("error", ctx["sources"]["fear_greed"])
        self.assertTrue(ctx["sources"]["global"]["ok"])

    def test_garbage_response_is_not_a_crash(self):
        self.overrides["coingecko"] = {"unexpected": 1}
        ctx = self.cycle()
        self.assertIn("global", ctx["degraded_sources"])

    def test_derivs_fallback_to_bybit(self):
        self.overrides["okx.com"] = history.HttpError(403, "geo")
        ctx = self.cycle()
        self.assertTrue(ctx["sources"]["derivs_ETH"]["ok"])
        self.assertEqual(ctx["sources"]["derivs_ETH"]["exchange"], "bybit")
        self.assertAlmostEqual(ctx["sources"]["derivs_ETH"]["long_short_ratio"], 0.55 / 0.45)

    def test_derivs_partial(self):
        self.overrides["long-short"] = history.HttpError(500, "")
        ctx = self.cycle()
        d = ctx["sources"]["derivs_ETH"]
        self.assertTrue(d["ok"])
        self.assertEqual(d["exchange"], "okx")
        self.assertIn("partial", d)
        self.assertNotIn("long_short_ratio", d)

    def test_derivs_all_down(self):
        self.overrides["okx.com"] = OSError("x")
        self.overrides["bybit"] = OSError("y")
        ctx = self.cycle()
        self.assertIn("derivs_ETH", ctx["degraded_sources"])
        self.assertFalse(ctx["risk_off"])

    def test_interval_cache_and_last_ok(self):
        cache = {}
        calls = []
        orig = self.router

        def counting(url, params, timeout=10):
            calls.append(url)
            return orig(url, params, timeout)
        context.http_get_json = counting
        self.cycle(NOW, cache)
        n1 = len(calls)
        self.cycle(NOW + 5, cache)  # ничего не должно быть "просрочено"
        self.assertEqual(len(calls), n1)
        self.overrides["alternative.me"] = OSError("down")
        ctx = self.cycle(NOW + 4000, cache)  # fear_greed (3600 с) просрочен
        self.assertFalse(ctx["sources"]["fear_greed"]["ok"])
        self.assertEqual(ctx["sources"]["fear_greed"]["last_ok_ts"], iso(NOW))


class TestWriteAndCli(Base):
    def test_atomic_write_no_tmp_left_and_valid_json(self):
        self.cycle()
        out = os.path.join(self.root, self.cfg["output"])
        with open(out) as f:
            d = json.load(f)
        self.assertIn("risk_off", d)
        self.assertEqual([x for x in os.listdir(os.path.dirname(out)) if x.endswith(".tmp")], [])

    def test_atomic_write_failure_keeps_old_file(self):
        p = os.path.join(self.root, "runtime", "context.json")
        context.atomic_write_json(p, {"v": 1})

        class Bad(object):
            pass
        with self.assertRaises(TypeError):
            context.atomic_write_json(p, {"v": Bad()})
        with open(p) as f:
            self.assertEqual(json.load(f), {"v": 1})
        self.assertEqual([x for x in os.listdir(os.path.dirname(p)) if x.endswith(".tmp")], [])

    def test_selftest_and_once(self):
        cfgp = os.path.join(ROOT, "config", "context_config.json")
        self.assertEqual(context.main(["--selftest", "--config", cfgp]), 0)
        self.assertEqual(context.http_get_json, self.router)  # selftest вернул подмену теста
