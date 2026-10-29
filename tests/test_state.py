import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from tb.legacy_engine import state as S  # noqa: E402


class TestState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "state.json")

    def test_missing_file_default_inactive(self):
        st = S.load_state(self.p)
        self.assertFalse(st["session_active"])
        self.assertFalse(st["halted"])

    def test_roundtrip_and_no_tmp_left(self):
        st = S.default_state()
        st["trades_today"] = 3
        S.save_state(self.p, st)
        self.assertEqual(S.load_state(self.p)["trades_today"], 3)
        self.assertEqual([f for f in os.listdir(self.d) if f != "state.json"], [])

    def test_atomic_write_failure_keeps_old_file(self):
        st = S.default_state()
        st["trades_today"] = 1
        S.save_state(self.p, st)
        st["trades_today"] = 2
        with mock.patch("tb.legacy_engine.state.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                S.save_state(self.p, st)
        self.assertEqual(S.load_state(self.p)["trades_today"], 1)
        self.assertEqual([f for f in os.listdir(self.d) if f != "state.json"], [])

    def test_unserializable_nan_does_not_corrupt(self):
        S.save_state(self.p, S.default_state())
        bad = S.default_state()
        bad["realized_pnl_today"] = float("nan")
        with self.assertRaises(ValueError):
            S.save_state(self.p, bad)
        self.assertFalse(S.load_state(self.p)["halted"])

    def test_corrupt_file_halts(self):
        with open(self.p, "w") as f:
            f.write("{not json")
        st = S.load_state(self.p)
        self.assertTrue(st["halted"])
        self.assertIn("state_corrupt", st["halt_reason"])

    def test_non_object_and_bad_types(self):
        with open(self.p, "w") as f:
            f.write("[1,2]")
        self.assertTrue(S.load_state(self.p)["halted"])
        with open(self.p, "w") as f:
            json.dump({"trades_today": "x", "positions": 5, "day_start_equity": "abc",
                       "session_active": "yes"}, f)
        st = S.load_state(self.p)
        self.assertEqual(st["trades_today"], 0)
        self.assertEqual(st["positions"], {})
        self.assertIsNone(st["day_start_equity"])
        self.assertFalse(st["session_active"])

    def test_journal_adds_ts_and_appends(self):
        jp = os.path.join(self.d, "j.jsonl")
        S.journal(jp, {"type": "a"})
        S.journal(jp, {"type": "b", "ts": "2026-01-01T00:00:00Z"})
        with open(jp, "a") as f:
            f.write("garbage\n")
        ev = S.read_journal(jp)
        self.assertEqual([e["type"] for e in ev], ["a", "b"])
        self.assertTrue(ev[0]["ts"])

    def test_new_day_reset(self):
        st = S.default_state()
        st.update(trades_today=5, realized_pnl_today=-2.0, halted=True, halt_reason="x")
        st["last_stop_ts"]["ETH"] = 123.0
        S.new_day_reset(st, "2026-10-28T00:00:01Z", 30.5)
        self.assertEqual(st["day"], "2026-10-28")
        self.assertEqual(st["day_start_equity"], 30.5)
        self.assertEqual(st["trades_today"], 0)
        self.assertEqual(st["realized_pnl_today"], 0.0)
        self.assertFalse(st["halted"])
        self.assertEqual(st["last_stop_ts"]["ETH"], 123.0)

    def test_parse_ts(self):
        self.assertEqual(S.parse_ts("2026-10-27T06:30:00Z"), S.parse_ts("2026-10-27T06:30:00+00:00"))
        self.assertIsNotNone(S.parse_ts("2026-10-27T06:30:00.5Z"))
        self.assertIsNone(S.parse_ts("garbage"))
        self.assertIsNone(S.parse_ts(None))
        self.assertIsNone(S.parse_ts(float("nan")))


if __name__ == "__main__":
    unittest.main()
