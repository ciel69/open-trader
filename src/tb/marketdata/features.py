"""Чистые функции признаков (без I/O). Единицы: доли, не проценты (кроме *_bps).

Ряд цен: список (ts_epoch, price), старые -> новые. Сделки: (ts, size, side), side 'buy'|'sell'
(сторона агрессора/тейкера). Стакан: списки (price, size), bids по убыванию цены, asks по возрастанию.
"""
import bisect
import math


def ema(values, period):
    """Последнее значение EMA (альфа=2/(n+1), инициализация первым значением). None если пусто."""
    if not values or period < 1:
        return None
    a = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = a * v + (1 - a) * e
    return e


def price_at(series, t):
    """Цена последней точки с ts <= t; None если такой нет."""
    if not series:
        return None
    ts = [p[0] for p in series]
    i = bisect.bisect_right(ts, t) - 1
    return series[i][1] if i >= 0 else None


def window_return(series, now, window_s, tol_frac=0.2):
    """Доходность за окно: p(now)/p(now-window)-1. None, если истории не хватает
    (самая старая точка позже now-window более чем на tol_frac*window)."""
    if not series:
        return None
    target = now - window_s
    ts = [p[0] for p in series]
    i = bisect.bisect_right(ts, target) - 1
    if i < 0:
        if ts[0] - target > tol_frac * window_s:
            return None
        i = 0
    p0 = series[i][1]
    p1 = series[-1][1]
    if p0 <= 0 or p1 <= 0:
        return None
    return p1 / p0 - 1.0


def realized_vol(series, now, window_s, step_s, min_points=5):
    """sqrt(сумма квадратов лог-доходностей) на сетке step_s внутри окна. Доля. None при нехватке данных."""
    if not series or step_s <= 0:
        return None
    start = now - window_s
    ts = [p[0] for p in series]
    if ts[0] - start > 0.2 * window_s or now - ts[-1] > 2 * step_s:
        return None
    prices = []
    t = start + step_s
    while t <= now + 1e-9:
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0:
            prices.append(series[i][1])
        t += step_s
    if len(prices) < min_points:
        return None
    ss = 0.0
    for a, b in zip(prices, prices[1:]):
        if a > 0 and b > 0:
            ss += math.log(b / a) ** 2
    return math.sqrt(ss)


def book_imbalance(bids, asks, n=5):
    """(sumBid - sumAsk)/(sumBid + sumAsk) по top-n уровням, в [-1, 1]. None если стакан пуст."""
    sb = sum(s for _, s in bids[:n])
    sa = sum(s for _, s in asks[:n])
    tot = sb + sa
    if tot <= 0:
        return None
    return (sb - sa) / tot


def spread_bps(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 1e4


def cvd(trades, now, window_s):
    """Покупки-агрессоры минус продажи за окно (в единицах size)."""
    lo = now - window_s
    tot = 0.0
    for ts, size, side in trades:
        if ts >= lo:
            tot += size if side == "buy" else -size if side == "sell" else 0.0
    return tot


def volume(trades, now, window_s):
    lo = now - window_s
    return sum(size for ts, size, _ in trades if ts >= lo)


def oi_change(oi_series, now, window_s):
    """Относительное изменение OI за окно (доля) по ряду (ts, oi)."""
    return window_return(oi_series, now, window_s, tol_frac=0.5)


def bars_from_series(series, bar_s):
    """Бары o,h,l,c из ряда (ts, price) по сетке bar_s."""
    bars = []
    cur = None
    for ts, p in series:
        k = int(ts // bar_s)
        if cur is None or cur["k"] != k:
            if cur is not None:
                bars.append(cur)
            cur = {"k": k, "o": p, "h": p, "l": p, "c": p}
        else:
            cur["h"] = max(cur["h"], p)
            cur["l"] = min(cur["l"], p)
            cur["c"] = p
    if cur is not None:
        bars.append(cur)
    return bars


def atr(bars, n=14):
    """Средний true range за последние n баров (в единицах цены). None при <2 баров."""
    if len(bars) < 2:
        return None
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"])))
    trs = trs[-n:]
    return sum(trs) / len(trs)


def classify_regime(rv_5m, ema_fast, ema_slow, atr_val, ret_15m, cfg):
    """storm: rv_5m >= cfg['storm_rv_5m'].
    trend_up/down: |ema_fast-ema_slow|/atr >= cfg['trend_atr_mult'] и знак ret_15m совпадает со знаком (fast-slow).
    иначе range; при нехватке данных unknown."""
    if rv_5m is not None and rv_5m >= cfg["storm_rv_5m"]:
        return "storm"
    if ema_fast is None or ema_slow is None or atr_val is None or ret_15m is None:
        return "unknown"
    if atr_val <= 0:
        return "unknown"
    d = ema_fast - ema_slow
    if abs(d) / atr_val >= cfg["trend_atr_mult"]:
        if d > 0 and ret_15m > 0:
            return "trend_up"
        if d < 0 and ret_15m < 0:
            return "trend_down"
    return "range"
