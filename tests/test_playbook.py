"""Карточки сетапов: валидность поставляемого config/playbook.json, поиск по виду сигнала и режиму, блок brief.playbook. Без сети."""
import json
import os
import shutil
import tempfile
import time
import types
import unittest

import _bootstrap  # noqa: F401
from tb.brain import brief, playbook, validate
from tb.core import bus

PROJECT = _bootstrap.ROOT


def card(**kw):
    c = {"id": "c1", "setup": "level_reclaim", "side": "both", "kinds": ["range_5m"], "regimes": [], "when": "w", "stop": "s", "target": "t", "inval": "i", "avoid": "a", "rr": 2.0, "hyp": "h1"}
    c.update(kw)
    return c


class Shipped(unittest.TestCase):
    def test_shipped_playbook_is_valid_and_consistent(self):
        cards, errs = playbook.load(PROJECT)
        self.assertEqual(errs, [])
        self.assertGreaterEqual(len(cards), 10)
        self.assertEqual(len({c["id"] for c in cards}), len(cards))
        self.assertEqual(len({c["hyp"] for c in cards}), len(cards))                       # у каждой карточки своя гипотеза для статистики
        for c in cards:
            self.assertIn(c["setup"], validate.SETUPS)
            self.assertLessEqual(len(c["when"]), 210)
        used = {k for c in cards for k in c["kinds"]}
        self.assertTrue({"oi_flush", "oi_spike", "funding_extreme", "liq_cascade", "news:hack", "news:listing"} <= used)      # новые каналы покрыты карточками

    def test_every_scanner_and_news_kind_that_wakes_scout_has_a_card(self):
        import json as _j
        with open(os.path.join(PROJECT, "config", "roles", "entry_scout.json"), encoding="utf-8") as f:
            wake = [k for k in _j.load(f)["wake_kinds"] if not k.startswith("idea_") or k == "idea_level"]
        cards, _ = playbook.load(PROJECT)
        used = {k for c in cards for k in c["kinds"]}
        self.assertEqual([k for k in wake if k not in used], [])                            # любой сигнал, будящий скаута, находит хотя бы одну карточку

    def test_day_setups_constant_matches(self):
        self.assertEqual(tuple(brief._DAY_SETUPS), tuple(validate.SETUPS))


class CheckCard(unittest.TestCase):
    def test_validation_rejects_bad_cards_individually(self):
        root = tempfile.mkdtemp(prefix="tbpb_")
        try:
            os.makedirs(os.path.join(root, "config"))
            with open(os.path.join(root, "config", "playbook.json"), "w", encoding="utf-8") as f:
                json.dump({"cards": [card(), card(id="dup"), card(id="dup", hyp="h3"), card(id="b1", setup="nope"), card(id="b2", kinds=["wat"]), card(id="b3", regimes=["moon"]),
                                     card(id="b4", when="x" * 300), card(id="b5", hyp="has space"), "junk"]}, f)
            cards, errs = playbook.load(root)
            self.assertEqual([c["id"] for c in cards], ["c1", "dup"])
            self.assertEqual(len(errs), 7, errs)
            self.assertEqual(playbook.load(os.path.join(root, "none"))[0], [])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class Match(unittest.TestCase):
    def setUp(self):
        self.cards = playbook.load(PROJECT)[0]

    def ids(self, kinds, reg=None, exp=None):
        return [r["id"] for r in playbook.match(self.cards, kinds, reg, exp)]

    def test_narrow_card_beats_general_one(self):
        self.assertEqual(self.ids(["oi_flush"], "range")[0], "oi-flush-reclaim")
        self.assertEqual(self.ids(["liq_cascade"], "trend_up")[0], "liq-cascade-reclaim")
        self.assertEqual(self.ids(["funding_extreme"], "range"), ["funding-crowded-reversal"])

    def test_regime_filters_cards(self):
        self.assertIn("range-fade", self.ids(["patrol"], "range"))
        self.assertNotIn("range-fade", self.ids(["patrol"], "trend_up"))
        self.assertIn("trend-pullback", self.ids(["patrol"], "trend_down"))
        self.assertEqual(self.ids(["range_5m"], "storm"), [])                               # в шторм общие карточки молчат, остаются только событийные
        self.assertEqual(self.ids(["funding_extreme"], "storm"), [])
        self.assertEqual(self.ids(["oi_flush"], "storm")[0], "oi-flush-reclaim")

    def test_news_kinds(self):
        self.assertEqual(self.ids(["news:hack"]), ["news-negative-short"])
        self.assertEqual(self.ids(["news:listing"]), ["news-listing-retest"])
        self.assertEqual(self.ids(["news:macro"]), ["macro-wait"])
        self.assertEqual(self.ids([]), [])

    def test_confirmed_hypothesis_ranks_higher_and_is_reported(self):
        base = self.ids(["range_5m", "hot_enter"], "range")
        exp = {"sweep-reject": {"n": 34, "avg_r": 0.4, "confirmed": True}}
        rows = playbook.match(self.cards, ["range_5m", "hot_enter"], "range", exp)
        self.assertEqual(rows[0]["id"], "sweep-reject")
        self.assertEqual(rows[0]["exp"], {"n": 34, "avg_r": 0.4})
        self.assertNotEqual(base[0], "sweep-reject")

    def test_output_is_compact_and_second_card_shorter(self):
        rows = playbook.match(self.cards, ["range_5m", "turnover_surge"], "trend_up")
        self.assertEqual(len(rows), 2)
        self.assertIn("target", rows[0])
        self.assertNotIn("target", rows[1])
        self.assertLess(len(json.dumps(rows, ensure_ascii=False)), 1300)                  # около 300 токенов на два
        self.assertTrue(all(w.startswith(("kind:", "reg:")) for r in rows for w in r["why"]))

    def test_exp_index_from_experiments_block(self):
        idx = playbook.exp_index({"confirmed": [{"hyp": "a", "n": 31, "avg_r": 0.3}], "open": [{"hyp": "b", "n": 4, "avg_r": -0.1}]})
        self.assertEqual(idx, {"a": {"n": 31, "avg_r": 0.3, "confirmed": True}, "b": {"n": 4, "avg_r": -0.1, "confirmed": False}})


class BriefBlock(unittest.TestCase):
    def ctx(self):
        return types.SimpleNamespace(root=PROJECT, now=time.time())

    def test_block_from_recent_signal_flash_and_struct(self):
        b = {"recent": [{"seq": 1, "type": "scanner_signal", "sym": "SOL", "d": {"kind": "oi_flush"}}, {"seq": 2, "type": "scanner_signal", "sym": "ETH", "d": {"kind": "patrol"}}],
             "flash": [{"cls": "hack", "dir": "down", "imp": 5}], "struct": {"s": {"SOL": {"reg": "range"}}}, "experiments": None}
        rows = brief._b_playbook(self.ctx(), b, "SOL")
        ids = [r["id"] for r in rows]
        self.assertEqual(ids[0], "oi-flush-reclaim")
        only_news = brief._b_playbook(self.ctx(), dict(b, recent=[]), "SOL")                 # без сигнала скана работает вид news:hack из brief.flash
        self.assertEqual([r["id"] for r in only_news], ["news-negative-short"])
        self.assertIsNone(brief._b_playbook(self.ctx(), {"recent": [], "flash": None}, "SOL"))      # нет сигнала: нет карточек
        self.assertIsNone(brief._b_playbook(types.SimpleNamespace(root=None, now=0), b, "SOL"))
        self.assertIsNone(brief._b_playbook(self.ctx(), b, None))

    def test_scout_event_kinds_in_bus_are_known_to_playbook(self):
        self.assertTrue(set(bus.SCANNER_KINDS) >= {k for c in playbook.load(PROJECT)[0] for k in c["kinds"] if not k.startswith("news:")})


if __name__ == "__main__":
    unittest.main()
