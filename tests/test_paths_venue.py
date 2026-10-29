import os
import subprocess
import sys
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.venues import bybit
from tb.venues.base import Venue


class TestPaths(unittest.TestCase):
    def test_default_root_is_project(self):
        self.assertTrue(os.path.isdir(os.path.join(paths.DEFAULT_ROOT, "src", "tb")))
        self.assertEqual(paths.config("x.json", root="/r"), os.path.join("/r", "config", "x.json"))
        self.assertEqual(paths.plans("p.md", root="/r"), os.path.join("/r", "state", "plans", "p.md"))

    def test_tb_root_override(self):
        old = os.environ.get("TB_ROOT")
        base = os.path.abspath(os.path.join(os.sep, "tmp", "zzz"))  # на Windows получится C:\tmp\zzz
        os.environ["TB_ROOT"] = base
        try:
            self.assertEqual(paths.state("trades.jsonl"), os.path.join(base, "state", "trades.jsonl"))
            self.assertEqual(paths.runtime("a"), os.path.join(base, "runtime", "a"))
            self.assertEqual(paths.data("b.csv"), os.path.join(base, "data", "b.csv"))
        finally:
            if old is None:
                del os.environ["TB_ROOT"]
            else:
                os.environ["TB_ROOT"] = old

    def test_tb_root_changes_module_paths_in_subprocess(self):
        base = os.path.abspath(os.path.join(os.sep, "tmp", "zzz"))
        env = dict(os.environ, TB_ROOT=base, PYTHONPATH=_bootstrap.SRC)
        out = subprocess.check_output([sys.executable, "-c", "from tb.core import journal; print(journal.PATH)"], env=env)
        self.assertEqual(out.decode().strip(), os.path.join(base, "state", "trades.jsonl"))


class TestVenue(unittest.TestCase):
    def test_bybit_implements_interface(self):
        self.assertTrue(issubclass(bybit.BybitVenue, Venue))
        for m in ("open", "close", "positions", "balance"):
            self.assertIsNot(getattr(bybit.BybitVenue, m), getattr(Venue, m))

    def test_base_is_abstract_in_effect(self):
        with self.assertRaises(NotImplementedError):
            Venue().balance()


if __name__ == "__main__":
    unittest.main()
