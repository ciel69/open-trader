"""Память для entry_scout: блок `memory` на сигнале сканера без плана (похожие ситуации по монете события), размер, режимы, сторона не фиксируется."""
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import NOW, TmpRoot, mk, synthetic
from tb.memory import blocks


class ScoutBlock(TmpRoot):
    def test_scout_block_has_similar_cases_without_side(self):
        self.put([mk(i, sym="DOOD", side="long" if i % 2 else "short", r=0.5 if i % 2 else -1.0, mode="paper") for i in range(6)])
        b = blocks.brief_block(self.root, NOW, "event", mode="paper", cfg=self.cfg, scout_sym="DOOD", scout_feats={"range_5m_pct": 1.8})
        self.assertIsNotNone(b)
        self.assertEqual((b["scope"], b["sym"]), ("event", "DOOD"))
        self.assertNotIn("side", b)                                         # сторона не задана: не фильтрует и не показывается
        self.assertGreaterEqual(b["similar"]["n"], 3)
        self.assertLessEqual(blocks.tokens(b), self.cfg["brief"]["max_tokens"])

    def test_scout_block_none_without_data_or_sym(self):
        self.assertIsNone(blocks.brief_block(self.root, NOW, "event", mode="paper", cfg=self.cfg, scout_sym="DOOD", scout_feats={}))
        self.put(synthetic(10))
        self.assertIsNone(blocks.brief_block(self.root, NOW, "event", mode="paper", cfg=self.cfg))             # без монеты и плана: блока нет

    def test_scout_block_hidden_in_live(self):
        self.put([mk(i, mode="paper", r=0.5) for i in range(6)])
        self.assertIsNone(blocks.brief_block(self.root, NOW, "event", mode="live", cfg=self.cfg, scout_sym="DOOD"))


class IdeaCaseMode(TmpRoot):
    def test_idea_case_takes_mode_and_setup_from_record(self):
        """Идея из paper (watch у entry_scout) попадает в память как paper-случай с сетапом, а не как live."""
        from tb.memory import migrate
        i = {"id": "I1", "ts": "2026-10-28T10:00:00Z", "sym": "SOL", "side": "long", "note": "x", "level": 144.0, "stop": 142.8, "tp": 147.0, "tag": "level_reclaim",
             "status": "watching", "mode": "paper", "src": "entry_scout"}
        c = migrate.build_idea_case(i, {}, self.cfg, set())
        self.assertEqual((c["src"]["mode"], c["situation"]["setup"]), ("paper", "level_reclaim"))
        self.assertEqual(migrate.build_idea_case(dict(i, mode=None), {}, self.cfg, set())["src"]["mode"], "live")        # старые идеи без режима как раньше


if __name__ == "__main__":
    unittest.main()
