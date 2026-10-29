import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import exec_cfg


class ExecCfgTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbcfg_")

    def put(self, obj, name="exec_limits.json"):
        p = paths.config(name, root=self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def test_defaults_without_file(self):
        self.assertEqual(exec_cfg.load(root=self.root), exec_cfg.DEFAULTS)
        self.assertEqual(exec_cfg.warnings(root=self.root), [])

    def test_defaults_deepcopy(self):
        d = exec_cfg.load(root=self.root)
        d["pyramid"]["enabled"] = True
        self.assertFalse(exec_cfg.DEFAULTS["pyramid"]["enabled"])
        self.assertFalse(exec_cfg.defaults()["pyramid"]["enabled"])

    def test_partial_file_merges(self):
        self.put({"entry": {"max_chase_bps": 10}, "watch_symbols": ["SOL"]})
        c = exec_cfg.load(root=self.root)
        self.assertEqual(c["entry"]["max_chase_bps"], 10)
        self.assertEqual(c["entry"]["max_price_age_s"], exec_cfg.DEFAULTS["entry"]["max_price_age_s"])
        self.assertEqual(c["watch_symbols"], ["SOL"])
        self.assertEqual(exec_cfg.warnings(root=self.root), [])      # сужение не флагуется

    def test_real_config_file_valid(self):
        real = os.path.join(paths.DEFAULT_ROOT, "config", "exec_limits.json")
        if not os.path.exists(real):
            self.skipTest("нет config/exec_limits.json")
        with open(real, encoding="utf-8") as f:
            self.put(json.load(f))
        c = exec_cfg.load(root=self.root)
        self.assertFalse(c["pyramid"]["enabled"])
        self.assertEqual(exec_cfg.warnings(root=self.root), [])

    def test_int_from_integral_float(self):
        self.put({"protections": {"max_entries_per_day": 6.0}})
        self.assertEqual(exec_cfg.load(root=self.root)["protections"]["max_entries_per_day"], 6)

    def test_invalid_values(self):
        cases = [
            ("{oops", "JSON"),
            ("[1]", "объектом"),
            ({"entry": 5}, "entry"),
            ({"entry": {"max_chase_bps": "много"}}, "entry.max_chase_bps"),
            ({"entry": {"max_chase_bps": True}}, "entry.max_chase_bps"),
            ({"entry": {"max_chase_bps": -1}}, "отрицательное"),
            ({"entry": {"blocked_attempts_max": 1.5}}, "целое"),
            ({"pyramid": {"enabled": "yes"}}, "pyramid.enabled"),
            ({"watch_symbols": "SOL"}, "watch_symbols"),
            ({"events": {"wake_r": [1.0]}}, "wake_r"),
            ({"rate": {"rest_write_rps": 0}}, "больше 0"),
            ({"equity_sync": {"max_dev_frac": 2}}, "доля"),
            ({"entry": {"no_confirm_max_size_mult": 2}}, "множитель"),
            ({"version": 2}, "version"),
            ({"entry": {"max_chase_bps": float("nan")}}, "entry.max_chase_bps"),
        ]
        for obj, frag in cases:
            self.put(obj if isinstance(obj, str) else json.dumps(obj).replace("NaN", "NaN"))
            with self.assertRaises(ValueError) as cm:
                exec_cfg.load(root=self.root)
            self.assertIn(frag, str(cm.exception), obj)

    def test_all_errors_listed(self):
        self.put({"entry": {"max_chase_bps": "a"}, "rate": {"rest_read_rps": -2}})
        with self.assertRaises(ValueError) as cm:
            exec_cfg.load(root=self.root)
        self.assertIn("max_chase_bps", str(cm.exception))
        self.assertIn("rest_read_rps", str(cm.exception))

    def test_unknown_keys_ignored_with_warning(self):
        self.put({"leverage": 50, "entry": {"foo": 1}})
        c = exec_cfg.load(root=self.root)
        self.assertNotIn("leverage", c)
        self.assertNotIn("foo", c["entry"])
        w = exec_cfg.warnings(root=self.root)
        self.assertTrue(any("leverage" in x for x in w))
        self.assertTrue(any("entry.foo" in x for x in w))

    def test_weakening_flagged_not_forbidden(self):
        self.put({"entry": {"max_chase_bps": 40}, "protections": {"max_entries_per_day": 30, "symbol_cooldown_after_stop_min": 5},
                  "pyramid": {"enabled": True}})
        c = exec_cfg.load(root=self.root)
        self.assertEqual(c["entry"]["max_chase_bps"], 40)
        w = " | ".join(exec_cfg.warnings(root=self.root))
        for k in ("entry.max_chase_bps", "protections.max_entries_per_day", "symbol_cooldown_after_stop_min", "pyramid.enabled"):
            self.assertIn(k, w)

    def test_require_confirm_false_flagged(self):
        self.put({"protections": {"pause_after_consecutive_stops": {"require_confirm": False}}})
        self.assertTrue(any("require_confirm" in x for x in exec_cfg.warnings(root=self.root)))

    def test_weaker_than_risk_profile(self):
        self.put({"max_consecutive_stops": 3}, name="risk_profile.json")
        self.put({"protections": {"pause_after_consecutive_stops": {"n": 5}}})
        w = exec_cfg.warnings(root=self.root)
        self.assertTrue(any("max_consecutive_stops" in x for x in w))

    def test_warnings_on_invalid_file_does_not_raise(self):
        self.put("{oops")
        w = exec_cfg.warnings(root=self.root)
        self.assertEqual(len(w), 1)

    def test_no_risk_keys_in_schema(self):
        flat = json.dumps(exec_cfg.DEFAULTS)
        for k in ("leverage", "margin", "risk_per_trade"):
            self.assertNotIn(k, flat)

    # --- KILL ---
    def test_kill_cycle(self):
        self.assertIsNone(exec_cfg.kill_state(root=self.root))
        rec = exec_cfg.set_kill(flatten=False, reason="тест", root=self.root)
        k = exec_cfg.kill_state(root=self.root)
        self.assertEqual((k["flatten"], k["reason"]), (False, "тест"))
        self.assertTrue(k["ts"])
        self.assertEqual(rec["reason"], "тест")
        self.assertTrue(exec_cfg.clear_kill(root=self.root))
        self.assertIsNone(exec_cfg.kill_state(root=self.root))
        self.assertFalse(exec_cfg.clear_kill(root=self.root))

    def test_kill_read_every_time(self):
        self.assertIsNone(exec_cfg.kill_state(root=self.root))
        p = paths.kill_file(self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump({"flatten": True, "reason": "руками", "ts": "x"}, f)
        self.assertTrue(exec_cfg.kill_state(root=self.root)["flatten"])
        os.remove(p)
        self.assertIsNone(exec_cfg.kill_state(root=self.root))

    def test_kill_flatten_not_weakened(self):
        exec_cfg.set_kill(flatten=True, reason="a", root=self.root)
        exec_cfg.set_kill(flatten=False, reason="b", root=self.root)
        k = exec_cfg.kill_state(root=self.root)
        self.assertTrue(k["flatten"])
        self.assertEqual(k["reason"], "b")

    def test_kill_garbage_still_kill(self):
        p = paths.kill_file(self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        for content in ("", "остановка", "{обрыв"):
            with open(p, "w") as f:
                f.write(content)
            k = exec_cfg.kill_state(root=self.root)
            self.assertIsNotNone(k)
            self.assertFalse(k["flatten"])

    def test_kill_no_tmp_leftovers(self):
        exec_cfg.set_kill(root=self.root)
        self.assertEqual(os.listdir(os.path.dirname(paths.kill_file(self.root))), ["KILL"])


if __name__ == "__main__":
    unittest.main()
