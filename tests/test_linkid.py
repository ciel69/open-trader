"""Тесты core/linkid.py (WP2): формат tbx-<plan_n>-<action_n>, разбор, отличие от прежних ссылок."""
import unittest

import _bootstrap  # noqa: F401
from tb.core import linkid, pending


class TestLinkId(unittest.TestCase):
    def test_make(self):
        self.assertEqual(linkid.make(12, 3), "tbx-12-3")
        self.assertEqual(linkid.make(0, 0), "tbx-0-0")

    def test_make_rejects_garbage(self):
        for a in ((-1, 1), (1, -1), (1.5, 1), ("1", 1), (True, 1), (None, 1)):
            with self.assertRaises(ValueError, msg=repr(a)):
                linkid.make(*a)

    def test_make_length_limit(self):
        self.assertLessEqual(len(linkid.make(10 ** 12, 10 ** 12)), linkid.MAX_LEN)
        with self.assertRaises(ValueError):
            linkid.make(10 ** 20, 10 ** 20)

    def test_roundtrip(self):
        for pn, an in ((1, 1), (12, 345), (99999, 0)):
            self.assertEqual(linkid.parse(linkid.make(pn, an)), (pn, an))

    def test_parse_rejects(self):
        for s in ("", "tbx-1", "tbx-1-", "tbx--1", "tbx-1-2-3", "TBX-1-2", "tbx-a-2", "tbx-1-2\n", " tbx-1-2", "tbx-١-2",
                  "BB-M-SOL-1028-101502", None, 5, ["tbx-1-1"], "tbx-" + "9" * 40 + "-1"):
            self.assertIsNone(linkid.parse(s), repr(s))

    def test_is_ours(self):
        self.assertTrue(linkid.is_ours("tbx-1-1"))
        self.assertTrue(linkid.is_ours("tbx-"))
        self.assertFalse(linkid.is_ours("xtbx-1-1"))
        self.assertFalse(linkid.is_ours(None))
        self.assertFalse(linkid.is_ours(5))

    def test_not_overlapping_with_pending(self):
        self.assertFalse(pending.LINK_RE.match(linkid.make(7, 7)))


if __name__ == "__main__":
    unittest.main()
