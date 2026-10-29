"""Парная торговля ETH/BTC: возврат к среднему логарифма отношения цен. Правила зафиксированы ДО запуска.

Сигнал: z = (ln R - среднее(ln R, W)) / ст.откл(ln R, W), R = ETH/BTC, часовые закрытия.
  z > +E: шорт ETH / лонг BTC (шорт отношения); z < -E: лонг ETH / шорт BTC.  Выход: |z| < 0.5, либо |z| > 4 (стоп), либо 240 ч.
Сетка (4 конфигурации): W in (168, 336), E in (2.0, 2.5). Выбор по train (первые 60%), >= 8 сделок. Test открывается один раз.
Издержки: 4 исполнения * (taker 0.09% + проскальзывание 0.05%) = 0.56% от номинала ноги за круг. Funding длинной и короткой ноги
взаимно гасятся (одинаковые ставки), не учитывается. Доходность — % от номинала одной ноги.
Вердикт «есть edge» только если test: net > 0, PF >= 1.3, сделок >= 15.
  ./tb research pairs data/ticks_ETH_hist_1h.csv data/ticks_BTC_hist_1h.csv
"""
import math
import statistics
import sys

from tb.research import research_pool as rp
from tb.research import research_hourly as rh

COST = 4 * (rh.FEE + rh.SLIP)
GRID = [(w, e) for w in (168, 336) for e in (2.0, 2.5)]
EXIT_Z, STOP_Z, MAX_HOLD, TRAIN_FRAC, MIN_TRAIN, MIN_TEST, MIN_PF = 0.5, 4.0, 240, 0.6, 8, 15, 1.3


def ratio_series(eth_path, btc_path):
    te, pe = rp.series(eth_path)
    tb, pb = rp.series(btc_path)
    be = dict(zip(te, pe))
    bb = dict(zip(tb, pb))
    hours = sorted(set(be) & set(bb))
    return hours, [math.log(be[h] / bb[h]) for h in hours]


def simulate(lr, w, e, a, b):
    """Сделки на индексах [a, b). Возвращает список net-доходностей (доли номинала ноги)."""
    trades, side, entry, ent_i = [], 0, 0.0, 0
    for i in range(max(a, w), b):
        win = lr[i - w:i]
        sd = statistics.pstdev(win)
        if sd == 0:
            continue
        z = (lr[i] - statistics.fmean(win)) / sd
        if side == 0:
            if z > e:
                side, entry, ent_i = -1, lr[i], i  # шорт отношения
            elif z < -e:
                side, entry, ent_i = 1, lr[i], i
        else:
            if abs(z) < EXIT_Z or abs(z) > STOP_Z or i - ent_i >= MAX_HOLD or i == b - 1:
                trades.append(side * (lr[i] - entry) - COST)
                side = 0
    return trades


def main(argv):
    hours, lr = ratio_series(argv[0], argv[1])
    cut = int(len(lr) * TRAIN_FRAC)
    print("Часов: %d, train=%d, test=%d, ratio ETH/BTC: начало %.5f, граница %.5f, конец %.5f" % (
        len(lr), cut, len(lr) - cut, math.exp(lr[0]), math.exp(lr[cut]), math.exp(lr[-1])))
    best, best_s = None, None
    for w, e in GRID:
        s = rh.stats(simulate(lr, w, e, 0, cut))
        if s["n"] >= MIN_TRAIN and (best_s is None or s["net"] > best_s["net"]):
            best, best_s = (w, e), s
    if best is None:
        print("На train < %d сделок у всех конфигураций: вердикт не выносится." % MIN_TRAIN)
        return
    print("Выбрано на train: W=%d E=%.1f -> %s" % (best[0], best[1], rh.fmt(best_s)))
    te = rh.stats(simulate(lr, best[0], best[1], cut, len(lr)))
    print("TEST: %s" % rh.fmt(te))
    ok = te["net"] > 0 and te["pf"] >= MIN_PF and te["n"] >= MIN_TEST
    print("ВЕРДИКТ: %s (конфигураций на train: %d; test открыт один раз)" % (
        "edge подтверждён" if ok else "edge НЕ подтверждён", len(GRID)))


if __name__ == "__main__":
    main(sys.argv[1:])
