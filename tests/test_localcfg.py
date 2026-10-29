"""Слой локальных переопределений config/local/<имя>.json (tb.core.localcfg)."""
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import localcfg


class LocalCfgTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tb_tmp_lc_")
        os.makedirs(os.path.join(self.root, "config", "local"))
        self._env = os.environ.pop("TB_ROOT", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["TB_ROOT"] = self._env

    def w(self, rel, obj, raw=None):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(obj))

    def test_no_local_file(self):
        self.w("config/a.json", {"x": 1})
        self.assertEqual(localcfg.read("a", self.root), {"x": 1})

    def test_deep_merge_and_replace(self):
        self.w("config/a.json", {"x": {"y": 1, "z": 2}, "l": [1, 2], "k": "s"})
        self.w("config/local/a.json", {"x": {"y": 9}, "l": [3], "n": True})
        self.assertEqual(localcfg.read("a", self.root), {"x": {"y": 9, "z": 2}, "l": [3], "k": "s", "n": True})

    def test_list_by_id(self):
        self.w("config/a.json", {"subscribers": [{"id": "a", "enabled": False, "r": {"u": 1}}, {"id": "b", "enabled": False}]})
        self.w("config/local/a.json", {"subscribers": [{"id": "a", "enabled": True}, {"id": "c", "enabled": True}]})
        got = localcfg.read("a", self.root)["subscribers"]
        self.assertEqual([x["id"] for x in got], ["a", "b", "c"])
        self.assertEqual(got[0], {"id": "a", "enabled": True, "r": {"u": 1}})
        self.assertFalse(got[1]["enabled"])

    def test_broken_local_ignored(self):
        self.w("config/a.json", {"x": 1})
        self.w("config/local/a.json", None, raw="{не json")
        self.assertEqual(localcfg.read("a", self.root), {"x": 1})
        self.w("config/local/a.json", None, raw="[1,2]")
        self.assertEqual(localcfg.read("a", self.root), {"x": 1})

    def test_base_errors_propagate_load_safe(self):
        with self.assertRaises(FileNotFoundError):
            localcfg.read("nope", self.root)
        self.assertEqual(localcfg.load("nope", self.root), {})
        self.w("config/local/nope.json", {"q": 1})
        self.assertEqual(localcfg.load("nope", self.root), {"q": 1})

    def test_no_escape(self):
        for bad in ("../a", "..", "a/../../x", "/etc/passwd", "a\\b", ""):
            self.assertIsNone(localcfg.local_path(bad, self.root), bad)
        self.w("config/secret.json", {"s": 1})
        self.assertIsNone(localcfg.read_local("../secret", self.root))

    def test_symlink_out_of_dir_rejected(self):
        self.w("config/a.json", {"x": 1})
        self.w("outside.json", {"x": 2})
        try:
            os.symlink(os.path.join(self.root, "outside.json"), os.path.join(self.root, "config", "local", "a.json"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlink недоступен")
        self.assertEqual(localcfg.read("a", self.root), {"x": 1})

    def test_env_override_only_without_root(self):
        d = tempfile.mkdtemp(prefix="tb_tmp_lc_")
        old = os.environ.get("TB_CONFIG_LOCAL")
        os.environ["TB_CONFIG_LOCAL"] = d
        try:
            self.assertEqual(localcfg.local_dir(None), os.path.abspath(d))
            self.assertEqual(localcfg.local_dir(self.root), os.path.join(self.root, "config", "local"))
        finally:
            os.environ["TB_CONFIG_LOCAL"] = old


class ShippedDefaultsTest(unittest.TestCase):
    def test_shipped_defaults_not_personal(self):
        from tb.platform import dispatcher as D
        from tb.intel import store
        subs, _p = D.load_subscribers(None)
        self.assertFalse([s for s in subs if s["id"] == "api"][0]["enabled"])
        self.assertFalse([k for k, v in store.load_cfg(None)["sources"].items() if v.get("verified")])


if __name__ == "__main__":
    unittest.main()
