"""Часовые бары, редкие входы, честная проверка: выбор параметров на train (первые 60%), оценка на test (последние 40%) один раз.

Сетка зафиксирована заранее (10 конфигураций), параметры не подбираются под test.
Доходность в % от номинала позиции, без плеча. Издержки: taker 0.09% и проскальзывание 0.05% на сторону, funding 0.01%/ч.
Использование: ./tb research hourly data/ticks_ETH_hist.csv [data/ticks_BTC_hist.csv ...]
"""
import sys

from tb.marketdata import data

# FUND_H ИСПРАВЛЕН 2026-10-27 по измерению начисления на платформе: 0.0012%/ч (раньше было 0.01%/ч, завышено в 8 раз).
FEE, SLIP, FUND_H = 0.0009, 0.0005, 0.000012
TRAIN_FRAC, MIN_TRAIN_TRADES = 0.6, 4
GRID = ([("donchian", n) for n in (24, 48, 72)] +
        [("ema", f, s) for f in (12, 24) for s in (48, 96)] +
        [("donchian_long_only", n) for n in (24, 48, 72)])


def hourly_closes(path):
    ticks = data.load_ticks(path)
    last, out = None, []
    for ts, p in ticks:
        h = int(ts // 3600)
        if h != last:
            out.append(p)
            last = h
        else:
            out[-1] = p
    return out


def ema(xs, n):
    k, e, out = 2.0 / (n + 1), None, []
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def positions(closes, cfg):
    """Целевая позиция на каждый бар (по закрытым данным до него включительно): +1/-1/0."""
    n = len(closes)
    pos = [0] * n
    kind = cfg[0]
    if kind in ("donchian", "donchian_long_only"):
        look = cfg[1]
        cur = 0
        for i in range(look, n):
            hi, lo = max(closes[i - look:i]), min(closes[i - look:i])
            mid_exit_hi = max(closes[i - look // 2:i])
            mid_exit_lo = min(closes[i - look // 2:i])
            c = closes[i]
            if cur == 0:
                if c > hi:
                    cur = 1
                elif c < lo and kind == "donchian":
                    cur = -1
            elif cur == 1 and c < mid_exit_lo:
                cur = 0
            elif cur == -1 and c > mid_exit_hi:
                cur = 0
            pos[i] = cur
    else:
        ef, es = ema(closes, cfg[1]), ema(closes, cfg[2])
        for i in range(cfg[2], n):
            pos[i] = 1 if ef[i] > es[i] else -1
    return pos


def simulate(closes, pos, start, end):
    """Сделки на отрезке [start, end): вход по close бара сигнала, выход при смене позиции."""
    trades, cur, entry, ent_i = [], 0, None, None
    for i in range(start, end):
        p = pos[i]
        if p != cur:
            if cur != 0:
                gross = cur * (closes[i] / entry - 1.0)
                cost = 2 * (FEE + SLIP) + cur * FUND_H * (i - ent_i)
                trades.append(gross - cost)
            cur, entry, ent_i = p, closes[i], i
    if cur != 0:
        gross = cur * (closes[end - 1] / entry - 1.0)
        trades.append(gross - 2 * (FEE + SLIP) - cur * FUND_H * (end - 1 - ent_i))
    return trades


def stats(tr):
    if not tr:
        return {"n": 0, "net": 0.0, "win": 0.0, "pf": 0.0}
    w = [t for t in tr if t > 0]
    l = [-t for t in tr if t <= 0]
    return {"n": len(tr), "net": 100 * sum(tr), "win": 100.0 * len(w) / len(tr),
            "pf": (sum(w) / sum(l)) if l else float("inf")}


def fmt(s):
    return "сделок=%d net=%+.2f%% win=%.0f%% PF=%.2f" % (s["n"], s["net"], s["win"], s["pf"])


def main(paths):
    sets = []
    for p in paths:
        c = hourly_closes(p)
        cut = int(len(c) * TRAIN_FRAC)
        sets.append((p, c, cut))
        print("%s: %d часовых баров, train=%d, test=%d, buy&hold train=%+.2f%% test=%+.2f%%" % (
            p, len(c), cut, len(c) - cut, 100 * (c[cut - 1] / c[0] - 1), 100 * (c[-1] / c[cut] - 1)))
    best, best_score = None, None
    for cfg in GRID:
        tr = []
        for _, c, cut in sets:
            tr += simulate(c, positions(c, cfg), 0, cut)
        s = stats(tr)
        if s["n"] >= MIN_TRAIN_TRADES and (best_score is None or s["net"] > best_score):
            best, best_score, best_tr = cfg, s["net"], s
    if best is None:
        print("На train недостаточно сделок ни у одной конфигурации (нужно >= %d)." % MIN_TRAIN_TRADES)
        return
    print("\nВыбрано на train: %s -> %s" % (best, fmt(best_tr)))
    te = []
    for p, c, cut in sets:
        t = simulate(c, positions(c, best), cut, len(c))
        te += t
        print("TEST %s: %s" % (p, fmt(stats(t))))
    print("TEST всего: %s" % fmt(stats(te)))
    print("(проверено %d конфигураций на train; test открыт один раз)" % len(GRID))


if __name__ == "__main__":
    main(sys.argv[1:])
