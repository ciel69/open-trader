"""Инверсия «самых убыточных» сигналов: если сигнал стабильно теряет, не станет ли обратная сделка прибыльной? Правила заданы ДО запуска.

Идея пользователя: места с максимальным минусом превратить в максимальный плюс. Проверка без самообмана:
  * Инвертируем направление у гипотез H1..H9 типа «по рынку» (kind mkt), выход и издержки те же (стоп 1.5 ATR, тейк 2R, удержание до 48 баров).
    Гипотезы типа «пробойный стоп-ордер» (brk) пропускаем: у них нет однозначной обратной сделки.
  * Три независимых набора: A) пул SOL/HYPE/ZEC, train (до границы 60%), в основном падающий рынок; B) тот же пул, test (последние 40%), в основном растущий;
    C) ETH и BTC за весь период (эти активы в отборе гипотез не участвовали).
  * Нулевые модели на тех же наборах: всегда лонг и всегда шорт на каждом 6-м баре, и случайное направление. Показывают, сколько даёт один только рыночный дрейф.
  * Вердикт «инверсия даёт edge» только если у ОДНОЙ и той же гипотезы одновременно на A и на C: net>0 после издержек платформы И Bybit, PF>=1.3, n>=40,
    и результат лучше нулевой модели того же направления. Если плюс есть лишь на B (растущий рынок) или совпадает с «всегда лонг», это дрейф, а не edge.
  * Это выборка худших из уже просмотренных результатов: число гипотез и инверсий учитывается как множественная проверка (K = число проверенных).
"""
import random
import sys

from tb.marketdata import patterns as pt
from tb.research import research_patterns as rp

SIDES = ("platform", "bybit")


def flip(sigs):
    return {i: (k, -a, b) for i, (k, a, b) in sigs.items() if k == "mkt"}


def run(assets, syms, sigs_by_sym, lo_ts, hi_ts):
    raw = {}
    for s in syms:
        bars = assets[s]
        raw[s] = rp.simulate(bars, pt.atr_series(bars, 14), sigs_by_sym[s], rp.idx_of(bars, lo_ts), rp.idx_of(bars, hi_ts))
    return {v: rp.stats(rp.rows_for(raw, assets, v)) for v in SIDES}


def fixed_dir(assets, syms, direction, step=6):
    return {s: {i: ("mkt", direction, None) for i in range(rp.WARM, len(assets[s]) - 1, step)} for s in syms}


def rand_dir(assets, syms, step=6):
    rnd = random.Random(rp.SEED)
    return {s: {i: ("mkt", rnd.choice((1, -1)), None) for i in range(rp.WARM, len(assets[s]) - 1, step)} for s in syms}


def main(argv=None):
    assets = rp.load_assets(rp.POOL + rp.REF)
    start, end = rp.window(assets, rp.POOL)
    cut = start + int((end - start) * rp.TRAIN_FRAC) // 3600 * 3600
    rstart, rend = rp.window(assets, rp.REF)
    sets = [("A pул train", rp.POOL, start, cut), ("B пул test", rp.POOL, cut, end), ("C ETH+BTC всё", rp.REF, rstart, rend)]
    print("Окна: пул train %s..%s, test ..%s; ETH+BTC весь период\n" % (start, cut, end))
    print("Нулевые модели (net%% / PF) платформа | Bybit:")
    for name, syms, lo, hi in sets:
        for lab, fn in (("всегда лонг", lambda s: fixed_dir(assets, s, 1)), ("всегда шорт", lambda s: fixed_dir(assets, s, -1)), ("случайно", lambda s: rand_dir(assets, s))):
            r = run(assets, syms, fn(syms), lo, hi)
            print("  %-14s %-12s %s | %s" % (name, lab, rp.fmt_stats(r["platform"]), rp.fmt_stats(r["bybit"])))
    print("\nИнверсии (K = %d гипотез):" % len(rp.HYPS))
    verdicts = {}
    for hid, hname, fn, base, _nb in rp.HYPS:
        row, ok_sets = [], {}
        for name, syms, lo, hi in sets:
            sigs = {}
            for s in syms:
                raw, _an = rp.make_signals(assets[s], fn, base)
                sigs[s] = flip(raw)
            r = run(assets, syms, sigs, lo, hi)
            ok_sets[name] = all(r[v]["net"] > 0 and r[v]["pf"] >= rp.MIN_PF and r[v]["n"] >= rp.MIN_TRADES for v in SIDES)
            row.append("%s: %s | %s" % (name, rp.fmt_stats(r["platform"]), rp.fmt_stats(r["bybit"])))
        print("\n%s %s (инверсия)" % (hid, hname))
        for x in row:
            print("   " + x)
        verdicts[hid] = ok_sets["A pул train"] and ok_sets["C ETH+BTC всё"]
    print("\nВЕРДИКТ по критерию (A и C одновременно): %s" % ({k: ("edge" if v else "нет") for k, v in verdicts.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
