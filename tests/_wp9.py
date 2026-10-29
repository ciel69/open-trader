"""Общая оснастка тестов WP9: временные state-файлы (журнал, портфель, сессии, learning) вместо рабочего state/."""
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import journal, learning, portfolio, session


class TmpState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tbwp9_")
        self._old = (journal.PATH, portfolio.PATH, session.PATH, learning.PATH)
        journal.PATH = os.path.join(self.dir, "trades.jsonl")
        portfolio.PATH = os.path.join(self.dir, "portfolio.json")
        session.PATH = os.path.join(self.dir, "sessions.jsonl")
        learning.PATH = os.path.join(self.dir, "learning.json")

    def tearDown(self):
        journal.PATH, portfolio.PATH, session.PATH, learning.PATH = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
