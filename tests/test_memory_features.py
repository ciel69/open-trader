"""WP15a: извлечение признаков (полные и неполные данные, старые записи без ctx), результат, whatif."""
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import NOW  # noqa: F401
from tb.memory import cases, cfg as memcfg, features

CFG = memcfg.load()
OPEN = {"ev": "open", "id": "BB-M-DOOD-1027-143314", "ts_open": "2026-10-27T14:33:11Z", "sym": "DOOD", "side": "long", "entry": 0.002035, "sl": 0.00199, "tp": 0.00218,
        "risk_usdt": 0.42, "thesis": "I6: ретест зоны", "regime": "unknown", "tags": ["bybit", "mainnet", "limit_entry"], "mode": "live", "idea": "I6"}
CTX = {"range_5m_pct": 1.8, "range_15m_pct": 3.1, "ret_5m_pct": 1.2, "turn_surge_5m": 5.4, "chg_24h_pct": -12.3, "spread_bps": 4.9, "funding_pct": -0.39, "regime": "range", "price": 1.0}


class TestSituation(unittest.TestCase):
    def test_old_trade_without_ctx(self):
        s = features.situation_from_trade(OPEN, None, CFG)
        self.assertEqual((s["sym"], s["side"]), ("DOOD", "long"))
        self.assertEqual(s["stop_pct"], 2.21)
        self.assertEqual(s["rr_planned"], 3.22)
        self.assertEqual((s["utc_hour"], s["dow"], s["utc_session"]), (14, 1, "overlap"))
        self.assertIsNone(s["range_5m_pct"])
        self.assertIn("range_5m_pct", s["missing"])
        self.assertNotIn("sym", s["missing"])
        self.assertEqual(s["confirm_kind"], "unknown")
        self.assertIsNone(s["confirmed"])

    def test_ctx_numbers_taken(self):
        s = features.situation_from_trade(dict(OPEN, ctx=CTX), None, CFG)
        self.assertEqual(s["range_5m_pct"], 1.8)
        self.assertEqual(s["funding_pct"], -0.39)
        self.assertEqual(s["regime"], "range")
        self.assertNotIn("range_5m_pct", s["missing"])

    def test_empty_ctx_falls_back_to_plan_ctx(self):
        s = features.situation_from_trade(dict(OPEN, ctx={}, plan_ctx=CTX), None, CFG)
        self.assertEqual(s["spread_bps"], 4.9)

    def test_no_confirm_tag_is_not_retro(self):
        s = features.situation_from_trade(dict(OPEN, tags=["no-confirm"]), None, CFG)
        self.assertIs(s["confirmed"], False)
        self.assertEqual(s["retro"], [])

    def test_no_confirm_from_post_is_retro(self):
        s = features.situation_from_trade(OPEN, {"err": ["no-confirm"]}, CFG)
        self.assertIs(s["confirmed"], False)
        self.assertEqual(s["confirm_kind"], "none")
        self.assertEqual(s["retro"], ["confirm_kind", "confirmed", "no_confirm_tag"])

    def test_plan_tags_parsed(self):
        t = features.parse_tags(["setup:sweep_reject", "exp:sweep-reject-v1", "var:A", "agent.api", "model.ds-pro", "mem.on", "no-confirm", "plan:PL-1"])
        self.assertEqual((t["setup"], t["exp"], t["var"], t["agent_id"], t["model"], t["mem"], t["plan"]), ("sweep_reject", "sweep-reject-v1", "A", "api", "ds-pro", "on", "PL-1"))
        self.assertTrue(t["no_confirm"])

    def test_bad_setup_tag_ignored(self):
        self.assertIsNone(features.parse_tags(["setup:zzz"])["setup"])

    def test_manual_pick_setup(self):
        s = features.situation_from_trade(dict(OPEN, tags=["manual-pick"]), None, CFG)
        self.assertEqual(s["setup"], "manual")

    def test_idea_ignores_note_text(self):
        i = {"id": "I1", "ts": "2026-10-27T13:39:43Z", "sym": "DOOD", "side": "long", "note": "диапазон 5м 6.2%, спред 4.9 bps, funding -0.39%", "price": 0.002, "level": None, "stop": None}
        s = features.situation_from_idea(i, CFG)
        self.assertIsNone(s["range_5m_pct"])
        self.assertIsNone(s["spread_bps"])
        self.assertIsNone(s["stop_pct"])

    def test_idea_stop_from_numeric_fields(self):
        s = features.situation_from_idea({"id": "I", "ts": "2026-10-27T13:00:00Z", "sym": "BEAT", "side": "short", "level": 0.1, "stop": 0.103, "tp": 0.09}, CFG)
        self.assertEqual(s["stop_pct"], 3.0)
        self.assertEqual(s["rr_planned"], 3.33)

    def test_signal_from_outcome(self):
        s = features.situation_from_outcome({"sym": "SOXS", "dir": "down", "ts": "2026-10-27T13:57:29Z", "range_5m_pct": 2.1, "turn_surge_5m": 6.1}, CFG)
        self.assertEqual((s["side"], s["range_5m_pct"], s["turn_surge_5m"]), ("short", 2.1, 6.1))
        self.assertEqual(s["coin_class"], "stock_token")

    def test_apply_brief_fills_without_overwriting(self):
        s = features.situation_from_trade(dict(OPEN, ctx=CTX), None, CFG)
        b = {"sentiment": {"fng": {"v": 23}, "dvol": {"DOOD": 55.0}}, "market": {"regime": "trend", "blackout": {"event": "FOMC", "in_min": 30}}}
        s2 = features.apply_brief(s, b)
        self.assertEqual(s2["fng"], 23)
        self.assertEqual(s2["regime"], "range")           # из ctx, не затирается
        self.assertIs(s2["blackout"], True)
        self.assertNotIn("fng", s2["missing"])
        self.assertEqual(features.apply_brief(s, None), s)

    def test_coin_class(self):
        self.assertEqual(features.classify_coin("BTCUSDT", CFG), "major")
        self.assertEqual(features.classify_coin("DOGE", CFG), "meme")
        self.assertEqual(features.classify_coin("XYZ", CFG), "unknown")
        self.assertEqual(features.classify_coin("XYZ", CFG, turnover_usd=5e8), "alt_mid")
        self.assertEqual(features.classify_coin("XYZ", CFG, turnover_usd=5e7), "alt_small")
        self.assertEqual(features.classify_coin("XYZ", CFG, turnover_usd=1e6), "meme")

    def test_sessions(self):
        self.assertEqual([features.utc_session(h) for h in (0, 6, 7, 12, 13, 15, 16, 21, 22, 23)],
                         ["asia", "asia", "europe", "europe", "overlap", "overlap", "us", "us", "asia", "asia"])
        self.assertEqual(features.time_feats("bad"), {"utc_hour": None, "dow": None, "utc_session": None})

    def test_built_situation_is_valid(self):
        s = features.situation_from_trade(dict(OPEN, ctx=CTX), {"err": ["no-confirm"]}, CFG)
        _, errs = cases.validate({"case_id": "C-20261027-DOOD-abc123", "type": "trade", "ts": OPEN["ts_open"], "situation": s, "decision": {"action": "enter"}})
        self.assertEqual(errs, [])


class TestResult(unittest.TestCase):
    CL = {"ts_close": "2026-10-27T14:51:03Z", "reason": "sl", "r": -1.07, "pnl_usdt": -0.4479, "fees_usdt": 0.028,
          "path": {"mae_r": -1.2, "mfe_r": 0.1}}

    def test_result_fields(self):
        r = features.result_from_close(OPEN, self.CL)
        self.assertEqual((r["status"], r["r"], r["exit_reason"]), ("closed", -1.07, "sl"))
        self.assertEqual(r["hold_min"], 17.9)
        self.assertEqual(r["fees_r"], 0.067)
        self.assertEqual((r["mae_r"], r["mfe_r"]), (-1.2, 0.1))

    def test_r_from_pnl_when_missing(self):
        r = features.result_from_close(OPEN, {"ts_close": "2026-10-27T14:51:03Z", "pnl_usdt": -0.21, "reason": "weird"})
        self.assertEqual(r["r"], -0.5)
        self.assertIsNone(r["exit_reason"])

    def test_no_r_no_risk(self):
        r = features.result_from_close({"ts_open": "x"}, {"pnl_usdt": 1})
        self.assertIsNone(r["r"])
        self.assertIsNone(r["hold_min"])


class TestWhatif(unittest.TestCase):
    ROWS = [{"id": "I2", "h": 15, "dir_pct": 10.53, "ret_pct": -10.53, "cost_pct": 0.24}, {"id": "I2", "h": 60, "dir_pct": 6.15, "ret_pct": -6.15, "cost_pct": 0.24}]

    def test_picks_horizon(self):
        w = features.whatif_from_outcomes(self.ROWS, 15)
        self.assertEqual((w["h_min"], w["net_dir_pct"]), (15, 10.29))
        self.assertEqual(features.whatif_from_outcomes(self.ROWS, 60)["h_min"], 60)
        self.assertEqual(features.whatif_from_outcomes(self.ROWS, 240)["h_min"], 15)

    def test_watch_has_no_direction(self):
        w = features.whatif_from_outcomes([{"id": "I3", "h": 15, "dir_pct": None, "ret_pct": -2.9, "cost_pct": 0.24}])
        self.assertIsNone(w["net_dir_pct"])

    def test_empty(self):
        self.assertIsNone(features.whatif_from_outcomes([]))

    def test_mechanics_detection(self):
        self.assertTrue(features.is_mechanics({"thesis": "testnet: проверка механики"}))
        self.assertTrue(features.is_mechanics({"thesis": "mainnet mechanics check"}))
        self.assertTrue(features.is_mechanics({"thesis": "x", "tags": ["mechanics"]}))
        self.assertFalse(features.is_mechanics({"thesis": "ретест зоны"}))


if __name__ == "__main__":
    unittest.main()
