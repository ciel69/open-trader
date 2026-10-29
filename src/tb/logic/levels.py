"""Ключевые уровни и ATR по символу для `runtime/levels.json` (02 п. 2.11; 06 п. 6.9). Владелец: WP3.

Чистая функция по свечам `CandleBook`; сутки и неделя по UTC (неделя с понедельника 00:00 UTC). Метрика считается по самому мелкому
таймфрейму, чья история доходит до начала периода; нет покрытия = None (лучше пусто, чем неверное «дневное» значение).
"""
import math

# ключи записи по символу (числа, без текста)
LEVELS_KEYS = ("price", "day", "prev_day", "week", "swing_5m", "swing_1h", "atr_5m_14", "atr_1h_14", "vol_med_5m_20", "vwap_day")

_DAY_MS = 86400000
_TFS = (1, 5, 15, 60)


def _cover(book, since_ms, tfs=_TFS, include_forming=True, until_ms=None):
    """-> [Candle] самого мелкого tf, чья история начинается не позже since_ms (иначе None). until_ms: исключая start >= until_ms."""
    for tf in tfs:
        cs = book.candles(tf, include_forming=include_forming)
        if cs and cs[0].start <= since_ms:
            cs = [c for c in cs if c.start >= since_ms and (until_ms is None or c.start < until_ms)]
            if cs:
                return cs
    return None


def _num(x):
    return x if x is not None and math.isfinite(x) else None


def key_levels(book, now_ms):
    """-> dict для runtime/levels.json по одному символу (все LEVELS_KEYS присутствуют; нет данных = None).

    day {hi,lo,open}, prev_day {hi,lo,close}, week {hi,lo}, swing_5m/swing_1h [до 4 уровней], atr_5m_14, atr_1h_14, vol_med_5m_20,
    vwap_day (оборот / объём свечей текущих суток UTC), price (close последней свечи самого мелкого tf).

    >>> from tb.logic.candles import CandleBook
    >>> key_levels(CandleBook(), 1700000000000)["price"] is None
    True
    """
    out = dict((k, None) for k in LEVELS_KEYS)
    if book is None:
        return out
    day0 = (int(now_ms) // _DAY_MS) * _DAY_MS
    prev0 = day0 - _DAY_MS
    week0 = day0 - ((day0 // _DAY_MS + 3) % 7) * _DAY_MS      # 1970-01-01 четверг -> понедельник = (дни + 3) % 7 == 0
    for tf in _TFS:
        cs = book.candles(tf, include_forming=True)
        if cs:
            out["price"] = cs[-1].c
            break
    cs = _cover(book, day0)
    if cs:
        out["day"] = {"hi": max(c.h for c in cs), "lo": min(c.l for c in cs), "open": cs[0].o}
        vol = sum(c.vol for c in cs)
        out["vwap_day"] = (sum(c.turn for c in cs) / vol) if vol > 0 and sum(c.turn for c in cs) > 0 else None
    cs = _cover(book, prev0, include_forming=False, until_ms=day0)
    if cs:
        out["prev_day"] = {"hi": max(c.h for c in cs), "lo": min(c.l for c in cs), "close": cs[-1].c}
    cs = _cover(book, week0, tfs=(15, 60))
    if cs:
        out["week"] = {"hi": max(c.h for c in cs), "lo": min(c.l for c in cs)}
    out["swing_5m"] = book.swing_levels(5, 3, 3, 4) or None
    out["swing_1h"] = book.swing_levels(60, 3, 3, 4) or None
    out["atr_5m_14"] = _num(book.atr(5, 14))
    out["atr_1h_14"] = _num(book.atr(60, 14))
    out["vol_med_5m_20"] = _num(book.vol_median(5, 20))
    return out
