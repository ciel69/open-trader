"""Свечи и снимок рынка для вычисления условий (06 п. 6.9). Владелец: WP3.

Чистый модуль: время и данные приходят аргументами. Нет данных или дыра в свечах = «условие не выполнено» и причина, а не исключение.

Единицы: `Candle.start` в миллисекундах биржи; `tf` в минутах (int) либо строкой «1m/5m/15m/1h»; `now`/`ts` во ВСЕХ вызовах
в секундах epoch (как `FakeClock.time()`), `*_ms` в миллисекундах.
"""
import math
from collections import namedtuple

# start в миллисекундах; confirm = свеча закрыта (подтверждена биржей по WS или по времени)
Candle = namedtuple("Candle", "start o h l c vol turn confirm")

MAX_KEEP = 1500          # закрытых свечей на таймфрейм (хватает на 1ч x 60 суток и 1м x сутки)
_TF_WORDS = {"m": 1, "h": 60, "d": 1440}


def tf_min(tf):
    """-> минуты (int) | None. Принимает 5, "5", "5m", "1h", "60".

    >>> tf_min("1h"), tf_min(5), tf_min("15m"), tf_min("x")
    (60, 5, 15, None)
    """
    try:
        if isinstance(tf, bool):
            return None
        if isinstance(tf, (int, float)):
            v = int(tf)
        else:
            s = str(tf).strip().lower()
            v = int(s) if s.isdigit() else int(s[:-1]) * _TF_WORDS[s[-1]]
    except (ValueError, KeyError, IndexError):
        return None
    return v if v > 0 else None


def _num(x):
    """-> float | None (конечное число)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _row_to_candle(row, confirm):
    """(start,o,h,l,c,vol,turn) -> Candle | None (мусор отбрасывается без исключения)."""
    try:
        start = int(row[0])
    except (TypeError, ValueError, IndexError):
        return None
    vals = [_num(x) for x in list(row)[1:7]]
    if len(vals) < 5 or any(v is None for v in vals[:5]):
        return None
    turn = vals[5] if len(vals) > 5 and vals[5] is not None else 0.0
    o, h, l, c, vol = vals[:5]
    if min(o, h, l, c) <= 0 or h < l:
        return None
    return Candle(start, o, h, l, c, max(vol, 0.0), max(turn, 0.0), bool(confirm))


class CandleBook(object):
    """Свечи по таймфреймам одного символа. gaps[tf] = (from_ms, to_ms) при пропуске свечи (start первой и последней недостающих)."""

    def __init__(self):
        self.gaps = {}
        self._closed = {}    # tf -> {start: Candle}
        self._forming = {}   # tf -> Candle

    # ---- служебное ----
    def _tf(self, tf):
        return tf_min(tf)

    def _sorted(self, tf):
        d = self._closed.get(tf)
        return [d[k] for k in sorted(d)] if d else []

    def _trim(self, tf):
        d = self._closed.get(tf)
        if d and len(d) > MAX_KEEP:
            for k in sorted(d)[:len(d) - MAX_KEEP]:
                del d[k]

    def _recheck_gaps(self, tf):
        """Самая свежая дыра в закрытых свечах (между соседями шаг больше tf) -> gaps[tf]; нет дыры -> запись убирается."""
        step = tf * 60000
        cs = self._sorted(tf)
        hole = None
        for a, b in zip(cs, cs[1:]):
            if b.start - a.start > step:
                hole = (a.start + step, b.start - step)
        # формирующаяся свеча далеко впереди закрытых тоже означает пропуск
        f = self._forming.get(tf)
        if f is not None and cs and f.start - cs[-1].start > step:
            hole = (cs[-1].start + step, f.start - step)
        if hole:
            self.gaps[tf] = hole
        else:
            self.gaps.pop(tf, None)

    # ---- загрузка ----
    def seed(self, tf, rows, now=None):
        """rows [(start_ms,o,h,l,c,vol,turn)]; последняя считается формирующейся, если start+tf*60000 > now*1000 (now в секундах).

        Подкачка идемпотентна: повтор тех же строк ничего не меняет; закрытые свечи заменяют имевшиеся с тем же start
        (REST точнее WS), пропуски, закрытые подкачкой, снимаются с gaps. Без `now` последняя строка считается формирующейся.

        >>> b = CandleBook(); b.seed(5, [(0,1,2,1,1.5,10,15),(300000,1.5,2,1,1.8,5,9)], now=700)
        >>> len(b.last_closed(5, 5)), b.forming(5) is None
        (2, True)
        """
        t = self._tf(tf)
        if t is None:
            return
        step = t * 60000
        cs = [c for c in (_row_to_candle(r, True) for r in (rows or [])) if c is not None]
        cs.sort(key=lambda c: c.start)
        d = self._closed.setdefault(t, {})
        for i, c in enumerate(cs):
            last = i == len(cs) - 1
            is_forming = (c.start + step > now * 1000) if now is not None else last
            if is_forming:
                self._forming[t] = c._replace(confirm=False)
            else:
                d[c.start] = c
                f = self._forming.get(t)
                if f is not None and f.start <= c.start:
                    del self._forming[t]
        self._trim(t)
        self._recheck_gaps(t)

    def on_kline(self, tf, row, confirm):
        """-> Candle закрытая | None; дозаполнение и дедупликация по start.

        confirm=False обновляет формирующуюся свечу (None). confirm=True новой свечи = её закрытие (возвращается Candle);
        повторное подтверждение той же свечи -> None (условие не должно сработать дважды). Запоздавшая старая свеча дозаполняет
        историю и возвращается. Пропуск свечи -> self.gaps[tf] = (from_ms, to_ms).

        >>> b = CandleBook(); b.on_kline(5, (0,1,2,1,1.5,3,4), False) is None
        True
        >>> b.on_kline(5, (0,1,2,1,1.6,3,4), True).c
        1.6
        >>> b.on_kline(5, (0,1,2,1,1.6,3,4), True) is None
        True
        """
        t = self._tf(tf)
        c = _row_to_candle(row, confirm)
        if t is None or c is None:
            return None
        d = self._closed.setdefault(t, {})
        f = self._forming.get(t)
        if not confirm:
            if c.start in d:                       # поздний пуш по уже закрытой свече: игнор
                return None
            if f is not None and c.start < f.start:
                return None                        # устаревший пуш
            self._forming[t] = c
            self._recheck_gaps(t)
            return None
        isnew = c.start not in d
        d[c.start] = c
        if f is not None and f.start <= c.start:
            del self._forming[t]
        self._trim(t)
        self._recheck_gaps(t)
        return c if isnew else None

    # ---- чтение ----
    def last_closed(self, tf, n=1):
        """-> [Candle] старые -> новые (меньше n, если истории не хватает).

        >>> b = CandleBook(); b.last_closed(5, 3)
        []
        """
        t = self._tf(tf)
        cs = self._sorted(t) if t else []
        return cs[-n:] if n > 0 else []

    def forming(self, tf):
        """-> Candle | None (формирующаяся, confirm=False)."""
        t = self._tf(tf)
        return self._forming.get(t) if t else None

    def closed_since(self, tf, ts_ms):
        """-> [Candle] закрытые свечи, начавшиеся >= ts_ms (старые -> новые)."""
        t = self._tf(tf)
        return [c for c in (self._sorted(t) if t else []) if c.start >= ts_ms]

    def candles(self, tf, since_ms=None, include_forming=False):
        """-> [Candle] закрытые (+ формирующаяся) с start >= since_ms, по возрастанию. Для levels и вычислений по диапазону."""
        t = self._tf(tf)
        if t is None:
            return []
        cs = self._sorted(t)
        f = self._forming.get(t)
        if include_forming and f is not None and (not cs or f.start > cs[-1].start):
            cs = cs + [f]
        if since_ms is not None:
            cs = [c for c in cs if c.start >= since_ms]
        return cs

    def tfs(self):
        """-> отсортированный список таймфреймов (мин), по которым есть хоть какие-то данные."""
        return sorted(set(k for k, v in self._closed.items() if v) | set(self._forming))

    def atr(self, tf, n=14):
        """-> float | None (Уайлдер по закрытым; нужно n+1 свечей).

        >>> b = CandleBook(); b.atr(5) is None
        True
        """
        t = self._tf(tf)
        cs = self._sorted(t) if t else []
        if n < 1 or len(cs) < n + 1:
            return None
        trs = []
        for p, c in zip(cs, cs[1:]):
            trs.append(max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c)))
        a = sum(trs[:n]) / n
        for tr in trs[n:]:
            a = (a * (n - 1) + tr) / n
        return a

    def vol_median(self, tf, lookback=20, before_ms=None):
        """-> float | None: медиана объёма последних `lookback` закрытых свечей (с start < before_ms, если задан).

        Нужна ровно lookback свечей, иначе None (мало истории = условие не выполнено).
        """
        t = self._tf(tf)
        cs = self._sorted(t) if t else []
        if before_ms is not None:
            cs = [c for c in cs if c.start < before_ms]
        if lookback < 1 or len(cs) < lookback:
            return None
        v = sorted(c.vol for c in cs[-lookback:])
        m = len(v) // 2
        return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0

    def swing_levels(self, tf, left=3, right=3, n=4):
        """-> [float] до n последних локальных экстремумов (максимумы и минимумы вместе), по времени: старые -> новые.

        Максимум: high выше всех `left` предыдущих и не ниже `right` следующих (плато считается по первой свече); минимум зеркально.
        Свеча без `right` закрытых соседей справа экстремумом не считается.
        """
        t = self._tf(tf)
        cs = self._sorted(t) if t else []
        left, right = max(1, int(left)), max(1, int(right))
        piv = []
        for i in range(left, len(cs) - right):
            w_l, w_r = cs[i - left:i], cs[i + 1:i + 1 + right]
            if cs[i].h > max(c.h for c in w_l) and cs[i].h >= max(c.h for c in w_r):
                piv.append((cs[i].start, cs[i].h))
            if cs[i].l < min(c.l for c in w_l) and cs[i].l <= min(c.l for c in w_r):
                piv.append((cs[i].start, cs[i].l))
        piv.sort(key=lambda x: x[0])
        return [p for _, p in piv[-n:]] if n > 0 else []


class MarketView(object):
    """Снимок для вычислений: цены символа, время, книга свечей. ts (время последнего тика) и now в секундах epoch."""

    def __init__(self, sym, last, bid, ask, mark, ts, book, now):
        self.sym = sym
        self.last = last
        self.bid = bid
        self.ask = ask
        self.mark = mark
        self.ts = ts
        self.book = book
        self.now = now

    def price_age(self):
        """-> секунды с момента последнего тика (now - ts); нет времени = inf (данные «бесконечно» устарели).

        >>> MarketView("SOL", 1, 1, 1, 1, 100.0, None, 102.5).price_age()
        2.5
        """
        if self.ts is None or self.now is None:
            return float("inf")
        return max(0.0, float(self.now) - float(self.ts))
