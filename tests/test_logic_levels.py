"""WP3: key_levels (уровни, ATR, swing, VWAP по UTC-суткам)."""
import copy
import unittest

import _bootstrap  # noqa: F401
from tb.logic import levels as L
from tb.logic.candles import CandleBook

DAY = 86400
D0 = 1699920000                     # вторник 2023-11-14 00:00 UTC
WEEK0 = D0 - DAY                    # понедельник 2023-11-13
NOW_S = D0 + 6 * 3600 + 120         # 06:02 UTC
NOW_MS = NOW_S * 1000


def put(b, tf, start_s, o, h, l, c, vol=10.0):
    b.on_kline(tf, (start_s * 1000, o, h, l, c, vol, vol * (h + l) / 2.0), True)


def full_book():
    b = CandleBook()
    # 1ч с воскресенья 00:00 до 06:00 вторника: цена растёт по 1 за час, hi = o+2, lo = o-1
    t = WEEK0 - DAY
    i = 0
    while t < D0 + 6 * 3600:
        o = 100.0 + i
        put(b, 60, t, o, o + 2, o - 1, o + 0.5, vol=100.0)
        t += 3600
        i += 1
    # 5м за вторник с 00:00 до 06:00: очень ровные
    t = D0
    j = 0
    while t < D0 + 6 * 3600:
        put(b, 5, t, 200.0, 201.0 + (j % 5) * 0.1, 199.0 - (j % 7) * 0.1, 200.0 + (j % 3) * 0.1, vol=10.0 + (j % 4))
        t += 300
        j += 1
    return b


class KeyLevelsTest(unittest.TestCase):
    def test_keys_always_present(self):
        out = L.key_levels(CandleBook(), NOW_MS)
        self.assertEqual(set(out), set(L.LEVELS_KEYS))
        self.assertTrue(all(v is None for v in out.values()))
        self.assertEqual(set(L.key_levels(None, NOW_MS)), set(L.LEVELS_KEYS))

    def test_day_from_finest_covering_tf(self):
        out = L.key_levels(full_book(), NOW_MS)
        self.assertEqual(out["day"]["open"], 200.0)
        self.assertAlmostEqual(out["day"]["hi"], 201.4)               # j%5 = 4 -> 201.4
        self.assertAlmostEqual(out["day"]["lo"], 199.0 - 0.6)
        self.assertEqual(out["price"], full_book().last_closed(5)[-1].c)

    def test_prev_day_and_week_from_hourly(self):
        out = L.key_levels(full_book(), NOW_MS)
        # понедельник 00:00..23:00: часовые индексы 24..47, o = 124..147
        self.assertEqual(out["prev_day"], {"hi": 147.0 + 2, "lo": 124.0 - 1, "close": 147.5})
        # неделя: понедельник 00:00 .. вторник 06:00 по часовым свечам (i = 24..53, последняя o = 153)
        self.assertEqual(out["week"], {"hi": 155.0, "lo": 123.0})

    def test_no_coverage_gives_none(self):
        b = CandleBook()
        for i in range(10):                                           # 5м начинаются только в 05:00: сутки не покрыты
            put(b, 5, D0 + 5 * 3600 + i * 300, 100, 101, 99, 100)
        out = L.key_levels(b, NOW_MS)
        self.assertIsNone(out["day"])
        self.assertIsNone(out["vwap_day"])
        self.assertIsNone(out["prev_day"])
        self.assertIsNone(out["week"])
        self.assertEqual(out["price"], 100.0)

    def test_vwap_day(self):
        b = CandleBook()
        put(b, 5, D0, 100, 100, 100, 100, vol=10)           # turn = 10*100
        put(b, 5, D0 + 300, 110, 110, 110, 110, vol=30)     # turn = 30*110
        out = L.key_levels(b, D0 * 1000 + 700000)
        self.assertAlmostEqual(out["vwap_day"], (1000 + 3300) / 40.0)

    def test_forming_candle_included_in_day_hi(self):
        b = CandleBook()
        put(b, 5, D0, 100, 101, 99, 100)
        b.on_kline(5, ((D0 + 300) * 1000, 100, 150, 99, 140, 5, 5), False)
        out = L.key_levels(b, (D0 + 400) * 1000)
        self.assertEqual(out["day"]["hi"], 150.0)
        self.assertEqual(out["price"], 140.0)

    def test_atr_swing_volmed(self):
        out = L.key_levels(full_book(), NOW_MS)
        self.assertIsNotNone(out["atr_5m_14"])
        self.assertIsNotNone(out["atr_1h_14"])
        self.assertIsNotNone(out["vol_med_5m_20"])
        self.assertIsInstance(out["swing_5m"], list)
        self.assertLessEqual(len(out["swing_5m"]), 4)

    def test_week_start_monday_boundaries(self):
        # воскресенье 2023-11-12 23:00 не входит в неделю с понедельника 13-го
        b = CandleBook()
        put(b, 60, WEEK0 - 3600, 500, 900, 400, 500)        # воскресенье
        put(b, 60, WEEK0, 100, 110, 90, 105)
        put(b, 60, D0, 105, 120, 100, 110)
        out = L.key_levels(b, (D0 + 1800) * 1000)
        self.assertEqual(out["week"], {"hi": 120.0, "lo": 90.0})
        # на воскресенье now: неделя начинается в предыдущий понедельник
        sunday_now = (WEEK0 - 3600 + 600) * 1000
        out = L.key_levels(b, sunday_now)
        self.assertIsNone(out["week"])                       # история не покрывает прошлый понедельник

    def test_pure(self):
        b = full_book()
        before = copy.deepcopy(b.last_closed(5, 1000))
        a, c = L.key_levels(b, NOW_MS), L.key_levels(b, NOW_MS)
        self.assertEqual(a, c)
        self.assertEqual(b.last_closed(5, 1000), before)


if __name__ == "__main__":
    unittest.main()
