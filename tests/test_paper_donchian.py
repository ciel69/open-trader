import io
import json
import os
import random
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.core import journal
from tb.research import paper_donchian as pd
from tb.research import research_hourly as rh

H = 3600


class EquivalenceTest(unittest.TestCase):
    def test_decide_matches_research_positions(self):
        random.seed(7)
        for trial in range(5):
            px, p = [], 100.0
            for _ in range(400):
                p *= 1 + random.gauss(0, 0.01)
                px.append(p)
            oracle = rh.positions(px, ("donchian", pd.LOOK))
            pos = 0
            for i in range(pd.LOOK, len(px)):
                pos = pd.decide(px[:i], pos, px[i])
                self.assertEqual(pos, oracle[i], "trial %d bar %d" % (trial, i))

    def test_needs_history(self):
        self.assertEqual(pd.decide([1.0] * 5, 0, 100.0), 0)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "runtime"))
        self.old = journal.PATH
        journal.PATH = os.path.join(self.root, "trades.jsonl")
        self.out = io.StringIO()

    def tearDown(self):
        journal.PATH = self.old

    def runner(self, base):
        r = pd.Runner(self.root, self.out)
        r.st["SOL"]["closes"] = list(base)
        return r

    def test_open_then_close_with_costs_in_journal(self):
        base = [100.0 + (i % 3) for i in range(40)]  # макс 102, мин 100
        r = self.runner(base)
        t = 5000 * H
        # час 0: рост до 105 -> закрытие часа 105 > 102 -> лонг
        r.on_price("SOL", t, 101)
        r.on_price("SOL", t + 60, 105)
        r.on_price("SOL", t + H, 106)       # переход часа: закрытие 105 -> вход
        self.assertEqual(r.st["SOL"]["pos"], 1)
        # падение ниже минимума последних 12 закрытий (100) -> выход
        r.on_price("SOL", t + H + 60, 99)
        r.on_price("SOL", t + 2 * H, 98)    # закрытие часа 99 < 100 -> выход
        self.assertEqual(r.st["SOL"]["pos"], 0)
        trades, order = journal._load()
        self.assertEqual(len(order), 1)
        tr = trades[order[0]]
        self.assertEqual(tr["mode"], "paper")
        self.assertIn("paper", tr["tags"])
        self.assertEqual(tr["reason"], "channel-exit")
        self.assertAlmostEqual(tr["fees_usdt"], 2 * pd.FEE * pd.NOTIONAL, places=4)
        self.assertLess(tr["pnl_usdt"], 0)   # вход ~105.02, выход ~98.98

    def test_state_persisted(self):
        r = self.runner([100.0 + (i % 3) for i in range(40)])
        r.on_price("SOL", 5000 * H, 101)
        r.save()
        r2 = pd.Runner(self.root, self.out)
        self.assertEqual(r2.st["SOL"]["cur_h"], 5000)

    def test_stale_feed_ignored(self):
        r = self.runner([100.0] * 40)
        with open(os.path.join(self.root, "runtime", "market.json"), "w") as f:
            json.dump({"feed_ok": True, "ts_epoch": 1000, "symbols": {"SOL": {"price": 100}}}, f)
        r.step(now=5000)
        self.assertIsNone(r.st["SOL"]["cur_h"])


if __name__ == "__main__":
    unittest.main()
