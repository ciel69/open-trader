"""Сторож позиции: STALLED (боковик с плюсом) и FLOW_TURN (ордерфлоу против), лимиты пробуждений."""
import doctest
import unittest

import _bootstrap  # noqa: F401
from tb.logic import watch as W

CFG = {"stalled_min": 15, "stalled_min_r": 0.5, "flow_turn_s": 30, "max_wakes_per_hour": 3}
AGAINST_LONG = {"cvd_1m_usd": -5000.0, "imb_pct": -20.0}
WITH_LONG = {"cvd_1m_usd": 5000.0, "imb_pct": 20.0}


class WatchTest(unittest.TestCase):
    def test_doctests(self):
        self.assertEqual(doctest.testmod(W).failed, 0)

    def test_stalled_only_with_profit(self):
        st = {}
        W.evaluate(st, 1, 0.3, None, 0.0, CFG)
        self.assertIsNone(W.evaluate(st, 1, 0.3, None, 2000.0, CFG))        # прибыли мало: будить нечего

    def test_new_peak_resets_timer(self):
        st = {}
        W.evaluate(st, 1, 0.8, None, 0.0, CFG)
        W.evaluate(st, 1, 1.2, None, 800.0, CFG)
        self.assertIsNone(W.evaluate(st, 1, 1.1, None, 1500.0, CFG))        # 700 с после нового пика
        self.assertEqual(W.evaluate(st, 1, 1.1, None, 800.0 + 901.0, CFG), "STALLED")

    def test_flow_turn_needs_duration_and_both_signals(self):
        st = {}
        self.assertIsNone(W.evaluate(st, 1, 1.0, AGAINST_LONG, 0.0, CFG))
        self.assertIsNone(W.evaluate(st, 1, 1.0, AGAINST_LONG, 20.0, CFG))
        self.assertEqual(W.evaluate(st, 1, 1.0, AGAINST_LONG, 31.0, CFG), "FLOW_TURN")
        st = {}
        W.evaluate(st, 1, 1.0, AGAINST_LONG, 0.0, CFG)
        W.evaluate(st, 1, 1.0, WITH_LONG, 20.0, CFG)                         # поток вернулся: отсчёт сброшен
        self.assertIsNone(W.evaluate(st, 1, 1.0, AGAINST_LONG, 40.0, CFG))
        self.assertIsNone(W.evaluate(st, 1, 1.0, {"cvd_1m_usd": -1.0, "imb_pct": 5.0}, 100.0, CFG))   # перекос стакана за позицию

    def test_short_side(self):
        st = {}
        W.evaluate(st, -1, 1.0, WITH_LONG, 0.0, CFG)                         # для short рост покупок = против
        self.assertEqual(W.evaluate(st, -1, 1.0, WITH_LONG, 31.0, CFG), "FLOW_TURN")

    def test_cooldown_and_hourly_cap(self):
        st = {}
        W.evaluate(st, 1, 1.0, AGAINST_LONG, 0.0, CFG)
        self.assertEqual(W.evaluate(st, 1, 1.0, AGAINST_LONG, 31.0, CFG), "FLOW_TURN")
        self.assertIsNone(W.evaluate(st, 1, 1.0, AGAINST_LONG, 200.0, CFG))  # пауза между повторами 10 мин
        self.assertEqual(W.evaluate(st, 1, 1.0, AGAINST_LONG, 700.0, CFG), "FLOW_TURN")
        self.assertEqual(W.evaluate(st, 1, 1.0, AGAINST_LONG, 1400.0, CFG), "FLOW_TURN")
        self.assertIsNone(W.evaluate(st, 1, 1.0, AGAINST_LONG, 2100.0, CFG))  # четвёртое за час: лимит 3


if __name__ == "__main__":
    unittest.main()
