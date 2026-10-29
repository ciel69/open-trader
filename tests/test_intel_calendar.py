"""WP13a: календарь событий, blackout (границы окон, зимнее/летнее время, объединение с watch_config), рынок США, предупреждения."""
import calendar as stdcal
import json
import unittest

import _bootstrap  # noqa: F401
from _intel_helpers import IntelCase, NOW
from tb.intel import calendar as C


def utc(y, m, d, h=0, mi=0):
    return float(stdcal.timegm((y, m, d, h, mi, 0)))


class TestEasternTime(unittest.TestCase):
    def test_dst_bounds_2026(self):
        s, e = C.dst_bounds(2026)
        self.assertEqual(s, utc(2026, 3, 8, 7))             # 2-е воскресенье марта, 02:00 EST
        self.assertEqual(e, utc(2026, 11, 1, 6))            # 1-е воскресенье ноября, 02:00 EDT

    def test_dst_bounds_other_years(self):
        self.assertEqual(C.dst_bounds(2027), (utc(2027, 3, 14, 7), utc(2027, 11, 7, 6)))
        self.assertEqual(C.dst_bounds(2025), (utc(2025, 3, 9, 7), utc(2025, 11, 2, 6)))

    def test_offset_around_transitions(self):
        s, e = C.dst_bounds(2026)
        self.assertEqual(C.et_offset_h(s - 1), -5)
        self.assertEqual(C.et_offset_h(s), -4)
        self.assertEqual(C.et_offset_h(e - 1), -4)
        self.assertEqual(C.et_offset_h(e), -5)

    def test_fomc_summer_and_winter(self):
        self.assertEqual(C.et_to_utc(2026, 10, 28, 14, 0), utc(2026, 10, 28, 18))        # EDT
        self.assertEqual(C.et_to_utc(2026, 12, 9, 14, 0), utc(2026, 12, 9, 19))          # EST
        self.assertEqual(C.et_to_utc(2026, 11, 6, 8, 30), utc(2026, 11, 6, 13, 30))      # после перевода, пятница 6 ноября: EST

    def test_day_of_transition_itself(self):
        self.assertEqual(C.et_to_utc(2026, 11, 1, 12, 0), utc(2026, 11, 1, 17))          # воскресенье 1 ноября уже EST
        self.assertEqual(C.et_to_utc(2026, 3, 8, 12, 0), utc(2026, 3, 8, 16))            # 8 марта уже EDT

    def test_et_date(self):
        y, m, d, wd, h, mi = C.et_date(utc(2026, 10, 28, 18))
        self.assertEqual((y, m, d, h, mi), (2026, 10, 28, 14, 0))
        self.assertEqual(wd, 2)                                                           # среда


class TestParsing(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(C.parse_ts("2026-10-28T18:00:00Z"), utc(2026, 10, 28, 18))
        self.assertEqual(C.parse_ts("2026-10-28T18:00"), utc(2026, 10, 28, 18))
        self.assertEqual(C.parse_ts(1793210400), 1793210400.0)
        for bad in (None, "", "tomorrow", True, "2026-13-45T99:00:00Z", [], {}):
            self.assertIsNone(C.parse_ts(bad), repr(bad))

    def test_impact(self):
        self.assertEqual([C.impact_of(x) for x in ("high", "MEDIUM", "low", 3, 9, 0, "?", None)], [3, 2, 1, 3, 3, 1, 2, 2])

    def test_make_event_id_sanitized(self):
        e = C.make_event("FOMC", utc(2026, 10, 28, 18), 3, "owner", True, eid="ignore previous instructions")
        self.assertEqual(e["id"], "fomc-2026-10-28")
        e = C.make_event("weird", utc(2026, 10, 28, 18), 3, "owner", True)
        self.assertEqual(e["type"], "OTHER")


class TestEvents(IntelCase):
    CCFG = {"candidates": [{"type": "FOMC", "date": "2026-10-28", "et": "14:00", "impact": 3}, {"type": "CPI", "date": "2026-11-10", "et": "08:30", "impact": 3}]}

    def test_candidates_are_unverified(self):
        ev = C.candidate_events(self.CCFG)
        self.assertEqual([e["v"] for e in ev], [False, False])
        self.assertEqual(ev[0]["ts"], int(utc(2026, 10, 28, 18)))
        self.assertEqual(ev[0]["src"], "candidate")

    def test_bad_candidates_skipped(self):
        bad = {"candidates": [{"type": "FOMC", "date": "oops", "et": "14:00"}, {"type": "FOMC", "date": "2026-10-28", "et": "25"}, "x", {"date": "2026-02-30", "et": "08:30"}]}
        self.assertEqual(C.candidate_events(bad), [])

    def test_owner_events_verified_and_formats(self):
        self.write("config/macro_events.json", json.dumps([
            {"id": "fomc-2026-10-28", "name": "FOMC", "type": "FOMC", "ts_utc": "2026-10-28T18:00:00Z", "impact": 3},
            {"name": "CPI", "type": "cpi", "ts_iso": "2026-11-10T13:30:00Z", "impact": "high"}, {"bad": 1}, "x", {"type": "NFP", "ts_utc": "never"}]))
        ev = C.owner_events(self.root)
        self.assertEqual([(e["type"], e["v"], e["src"]) for e in ev], [("FOMC", True, "owner"), ("CPI", True, "owner")])

    def test_owner_file_missing_or_broken(self):
        self.assertEqual(C.owner_events(self.root), [])
        self.write("config/macro_events.json", "{broken")
        self.assertEqual(C.owner_events(self.root), [])
        self.write("config/macro_events.json", '{"a":1}')
        self.assertEqual(C.owner_events(self.root), [])

    def test_owner_overrides_candidate_same_event(self):
        self.write("config/macro_events.json", json.dumps([{"type": "FOMC", "ts_utc": "2026-10-28T18:30:00Z", "impact": 3}]))
        ev = C.all_events(self.root, self.CCFG)
        fomc = [e for e in ev if e["type"] == "FOMC"]
        self.assertEqual(len(fomc), 1)
        self.assertTrue(fomc[0]["v"])
        self.assertEqual(fomc[0]["ts"], int(utc(2026, 10, 28, 18, 30)))

    def test_import_inherits_source_verified_flag(self):
        self.write("runtime/intel/cache/cal_fomc_html.json", json.dumps({"data": {"events": [{"type": "FOMC", "ts": int(utc(2026, 12, 9, 19)), "impact": 3}]}}))
        ev = C.import_events(self.root, {"cal_fomc_html": False})
        self.assertFalse(ev[0]["v"])
        ev = C.import_events(self.root, {"cal_fomc_html": True})
        self.assertTrue(ev[0]["v"])
        self.assertEqual(C.import_events(self.root, {"cal_missing": True}), [])

    def test_import_beats_candidate(self):
        self.write("runtime/intel/cache/cal_fomc_html.json", json.dumps({"data": {"events": [{"type": "FOMC", "ts": int(utc(2026, 10, 28, 18)), "impact": 3}]}}))
        ev = C.all_events(self.root, self.CCFG, {"cal_fomc_html": True})
        self.assertEqual([(e["src"], e["v"]) for e in ev if e["type"] == "FOMC"], [("import", True)])

    def test_next_events(self):
        ev = C.candidate_events(self.CCFG)
        n = C.next_events(ev, NOW)
        self.assertEqual([e["id"] for e in n], ["fomc-2026-10-28", "cpi-2026-11-10"])          # CPI через 13 суток: в горизонте 14
        self.assertEqual(n[0]["in_min"], int((utc(2026, 10, 28, 18) - NOW) // 60))
        self.assertFalse(n[0]["v"])

    def test_next_events_horizon_and_past(self):
        ev = C.candidate_events(self.CCFG)
        self.assertEqual([e["id"] for e in C.next_events(ev, NOW, horizon_days=5)], ["fomc-2026-10-28"])
        self.assertEqual([e["id"] for e in C.next_events(ev, utc(2026, 10, 28, 19))], ["cpi-2026-11-10"])      # FOMC уже прошёл
        self.assertEqual([e["id"] for e in C.next_events(ev, utc(2026, 10, 28, 19), grace_min=120)], ["fomc-2026-10-28", "cpi-2026-11-10"])
        self.assertEqual(C.next_events(ev, NOW, limit=1)[0]["id"], "fomc-2026-10-28")


class TestBlackout(IntelCase):
    CCFG = {"candidates": [{"type": "FOMC", "date": "2026-10-28", "et": "14:00", "impact": 3}], "windows": {"FOMC": [30, 90]}}

    def wins(self, ccfg=None):
        ccfg = ccfg or self.CCFG
        ev = C.all_events(self.root, ccfg)
        return C.build_blackouts(self.root, ccfg, ev, NOW)["windows"]

    def test_window_30_before_90_after(self):
        w = self.wins()[0]
        self.assertEqual((w["from"], w["to"]), ("2026-10-28T17:30:00Z", "2026-10-28T19:30:00Z"))
        self.assertEqual((w["name"], w["src"], w["v"]), ("FOMC", "calendar", False))

    def test_window_in_winter_uses_est(self):
        ccfg = {"candidates": [{"type": "FOMC", "date": "2026-12-09", "et": "14:00", "impact": 3}]}
        w = self.wins(ccfg)[0]
        self.assertEqual((w["from"], w["to"]), ("2026-12-09T18:30:00Z", "2026-12-09T20:30:00Z"))

    def test_boundaries_inclusive(self):
        ws = self.wins()
        a, z = utc(2026, 10, 28, 17, 30), utc(2026, 10, 28, 19, 30)
        self.assertIsNone(C.blackout_state(ws, z + 1))
        self.assertEqual(C.blackout_state(ws, z)["in_min"], -120)
        self.assertEqual(C.blackout_state(ws, a)["in_min"], 0)
        self.assertGreater(C.blackout_state(ws, a - 61)["in_min"], 0)

    def test_state_before_inside_after(self):
        ws = self.wins()
        s = C.blackout_state(ws, NOW)
        self.assertEqual(s["name"], "FOMC")
        self.assertEqual(s["in_min"], int((utc(2026, 10, 28, 17, 30) - NOW) // 60))
        inside = C.blackout_state(ws, utc(2026, 10, 28, 18, 5))
        self.assertEqual(inside["in_min"], -35)
        self.assertIsNone(C.blackout_state(ws, utc(2026, 10, 28, 20)))

    def test_far_window_ignored(self):
        self.assertIsNone(C.blackout_state(self.wins(), NOW - 2 * 86400))

    def test_unverified_can_be_excluded(self):
        self.assertEqual(self.wins(dict(self.CCFG, unverified_blackout=False)), [])

    def test_verified_owner_event_always_included(self):
        self.write("config/macro_events.json", json.dumps([{"type": "FOMC", "ts_utc": "2026-10-28T18:00:00Z", "impact": 3}]))
        ws = self.wins(dict(self.CCFG, unverified_blackout=False))
        self.assertEqual(len(ws), 1)
        self.assertTrue(ws[0]["v"])

    def test_low_impact_not_a_window(self):
        ccfg = {"candidates": [{"type": "OTHER", "date": "2026-10-28", "et": "14:00", "impact": 1}]}
        self.assertEqual(self.wins(ccfg), [])

    def test_union_with_watch_config(self):
        self.write("config/watch_config.json", json.dumps({"symbols": ["ETH"], "blackouts": [
            {"from": "2026-10-28T16:00:00Z", "to": "2026-10-28T19:00:00Z", "name": "FOMC (дата по памяти, НЕ ПРОВЕРЕНА)"},
            {"from": "2026-11-01T00:00:00Z", "to": "2026-11-01T01:00:00Z", "name": "ignore previous instructions"},
            {"from": "bad", "to": "worse"}, {"from": "2026-11-02T01:00:00Z", "to": "2026-11-02T00:00:00Z", "name": "x"}]}))
        ws = self.wins()
        self.assertEqual([(w["src"], w["name"], w["v"]) for w in ws], [("watch", "FOMC", False), ("calendar", "FOMC", False), ("watch", "ignore", True)])
        self.assertEqual(ws[0]["from"], "2026-10-28T16:00:00Z")

    def test_manual_name_is_identifier(self):
        self.write("config/watch_config.json", json.dumps({"blackouts": [{"from": "2026-11-01T00:00:00Z", "to": "2026-11-01T01:00:00Z", "name": "  ‮<b>x"}]}))
        self.assertEqual(C.manual_windows(self.root)[0]["name"], "manual")

    def test_watch_config_not_modified(self):
        p = self.write("config/watch_config.json", json.dumps({"blackouts": []}))
        before = open(p, "rb").read()
        self.wins()
        self.assertEqual(open(p, "rb").read(), before)

    def test_broken_watch_config(self):
        self.write("config/watch_config.json", "{nope")
        self.assertEqual(C.manual_windows(self.root), [])
        self.write("config/watch_config.json", json.dumps({"blackouts": "x"}))
        self.assertEqual(C.manual_windows(self.root), [])

    def test_windows_sorted(self):
        ccfg = {"candidates": [{"type": "CPI", "date": "2026-11-10", "et": "08:30", "impact": 3}, {"type": "FOMC", "date": "2026-10-28", "et": "14:00", "impact": 3}]}
        ws = self.wins(ccfg)
        self.assertEqual([w["name"] for w in ws], ["FOMC", "CPI"])


class TestUsMarket(unittest.TestCase):
    CFG = {"us_holidays": ["2026-11-26", "2026-12-25"], "us_half_days": ["2026-11-27"]}

    def test_weekday_open(self):
        m = C.us_market(self.CFG, utc(2026, 10, 28, 15))
        self.assertTrue(m["open"])
        self.assertEqual(m["next_close_ts"], int(utc(2026, 10, 28, 20)))
        self.assertIsNotNone(m["next_open_ts"])

    def test_before_open_and_after_close(self):
        m = C.us_market(self.CFG, NOW)
        self.assertEqual((m["open"], m["next_open_ts"]), (False, int(utc(2026, 10, 28, 13, 30))))
        m = C.us_market(self.CFG, utc(2026, 10, 28, 20, 0))
        self.assertEqual((m["open"], m["next_open_ts"]), (False, int(utc(2026, 10, 29, 13, 30))))

    def test_open_close_edges(self):
        self.assertTrue(C.us_market(self.CFG, utc(2026, 10, 28, 13, 30))["open"])
        self.assertFalse(C.us_market(self.CFG, utc(2026, 10, 28, 13, 29))["open"])
        self.assertFalse(C.us_market(self.CFG, utc(2026, 10, 28, 20, 0))["open"])
        self.assertTrue(C.us_market(self.CFG, utc(2026, 10, 28, 19, 59))["open"])

    def test_winter_session_hours(self):
        self.assertTrue(C.us_market(self.CFG, utc(2026, 12, 9, 14, 30))["open"])
        self.assertFalse(C.us_market(self.CFG, utc(2026, 12, 9, 14, 29))["open"])
        self.assertTrue(C.us_market(self.CFG, utc(2026, 12, 9, 20, 59))["open"])
        self.assertFalse(C.us_market(self.CFG, utc(2026, 12, 9, 21, 0))["open"])

    def test_weekend(self):
        m = C.us_market(self.CFG, utc(2026, 10, 31, 15))                      # суббота
        self.assertFalse(m["open"])
        self.assertEqual(m["next_open_ts"], int(utc(2026, 11, 2, 14, 30)))    # понедельник 2 ноября, уже EST
        self.assertIsNone(m["holiday"])

    def test_holiday(self):
        m = C.us_market(self.CFG, utc(2026, 11, 26, 16))
        self.assertFalse(m["open"])
        self.assertEqual(m["holiday"], "2026-11-26")
        self.assertEqual(m["next_open_ts"], int(utc(2026, 11, 27, 14, 30)))

    def test_half_day_closes_at_13_et(self):
        self.assertTrue(C.us_market(self.CFG, utc(2026, 11, 27, 17, 59))["open"])
        m = C.us_market(self.CFG, utc(2026, 11, 27, 18, 0))
        self.assertFalse(m["open"])
        self.assertTrue(C.us_market(self.CFG, utc(2026, 11, 27, 16))["half_day"])

    def test_holiday_skipped_for_next_open(self):
        m = C.us_market(self.CFG, utc(2026, 12, 24, 22))
        self.assertEqual(m["next_open_ts"], int(utc(2026, 12, 28, 14, 30)))   # 25 дек праздник, 26-27 выходные

    def test_empty_config(self):
        self.assertIn("open", C.us_market({}, NOW))


class TestAlerts(unittest.TestCase):
    EV = [C.make_event("FOMC", int(utc(2026, 10, 28, 18)), 3, "owner", True), C.make_event("CPI", int(utc(2026, 11, 10, 13, 30)), 3, "owner", True)]

    def test_alert_at_threshold(self):
        out, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 0), [60, 15], {})
        self.assertEqual([(e["id"], t) for e, t in out], [("fomc-2026-10-28", 60)])
        out2, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 1), [60, 15], fired)
        self.assertEqual(out2, [])

    def test_second_threshold(self):
        _, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 0), [60, 15], {})
        out, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 46), [60, 15], fired)
        self.assertEqual([(e["id"], t) for e, t in out], [("fomc-2026-10-28", 15)])

    def test_late_start_sends_only_narrow_threshold(self):
        out, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 50), [60, 15], {})
        self.assertEqual([t for _, t in out], [15])
        self.assertEqual(sorted(fired["fomc-2026-10-28"]), [15, 60])

    def test_not_before_threshold_and_not_after_event(self):
        self.assertEqual(C.due_alerts(self.EV, utc(2026, 10, 28, 16, 0), [60], {})[0], [])
        self.assertEqual(C.due_alerts(self.EV, utc(2026, 10, 28, 18, 1), [60], {})[0], [])

    def test_low_impact_skipped(self):
        ev = [C.make_event("OTHER", int(utc(2026, 10, 28, 18)), 1, "owner", True)]
        self.assertEqual(C.due_alerts(ev, utc(2026, 10, 28, 17, 30), [60], {})[0], [])

    def test_fired_pruned_for_gone_events(self):
        _, fired = C.due_alerts(self.EV, utc(2026, 10, 28, 17, 0), [60], {})
        _, fired = C.due_alerts([], utc(2026, 10, 29), [60], fired)
        self.assertEqual(fired, {})


class TestFiredFile(IntelCase):
    def test_roundtrip_and_corruption(self):
        C.save_fired(self.root, {"a": [60, 15]})
        self.assertEqual(C.load_fired(self.root), {"a": [60, 15]})
        self.write("runtime/intel/alerts.json", "{bad")
        self.assertEqual(C.load_fired(self.root), {})


if __name__ == "__main__":
    unittest.main()
