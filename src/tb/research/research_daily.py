"""Дневные стратегии: (A) низкооборотный моментум, (B) контрарная ставка по funding. ПРАВИЛА ЗАДАНЫ ДО ПОЛУЧЕНИЯ ДАННЫХ.

Данные: data/ticks_<SYM>_hist_1d.csv (дневные закрытия UTC), data/funding_<SYM>.csv (Bybit, начисления). Пул: BTC, ETH, SOL.
Общая граница по времени: общий период [max(начал), min(концов)], train = первые 60%, test = последние 40%; данные до начала — только разгон.
Издержки: taker 0.09% + проскальзывание 0.05% на сторону. Funding платформы (ДОПУЩЕНИЕ: константа, как сейчас: 0.0096%/ч = 0.2304%/сутки):
лонг платит, шорт получает. История funding платформы неизвестна, это главная неопределённость; в печать идёт вариант без funding для справки,
в вердикт идёт вариант с funding.

(A) Моментум: сигнал = знак доходности за N дней (N in 20, 60, 120); режимы: ls (лонг/шорт), lf (лонг/флэт). Смена позиции по дневному закрытию.
    Сетка 6 конфигураций. Выбор по суммарному net пула на train при >= 8 сделок.
(B) Funding-контрариан: суточная сумма funding Bybit; процентиль за предыдущие 180 дней (нужно >= 90). Процентиль >= P: шорт, <= 100-P: лонг;
    удержание H суток, без наложений. P in (90, 95), H in (1, 3): 4 конфигурации. Выбор по суммарному net пула на train при >= 8 сделок.
Вердикт для каждой стратегии отдельно: test пула net > 0, PF >= 1.3, сделок >= 15, плюс не менее чем у 2 из 3 активов. Правила не менять после просмотра.
Проверено 2 стратегии (10 конфигураций): при пограничном результате считать «не подтверждено».
  ./tb research daily
"""
import csv
import os
import sys

from tb import paths
from tb.research import research_hourly as rh
from tb.research import research_pool as rp

SYMS = ("BTC", "ETH", "SOL")
SIDE_COST = rh.FEE + rh.SLIP
# ИСПРАВЛЕНО по измерению (2026-10-27): реальное начисление по позиции ETH шорт = 0.000357 USDC за час на номинал ~29.7 = 0.0012%,
# т.е. поле funding_rate_hourly (0.0096%) на деле ставка за 8 ч, начисляется по 1/8 в час. Шорт ПОЛУЧАЕТ (маржа выросла).
# Первый прогон шёл с завышенной в 8 раз константой 0.0096%/ч и давал ложную «подтверждённость» моментума.
FUND_DAY = 0.0096 / 100 / 8 * 24
TRAIN_FRAC, MIN_TRAIN, MIN_TEST, MIN_PF = 0.6, 8, 15, 1.3


def load_daily(sym):
    ts, px = rp.series(paths.data("ticks_%s_hist_1d.csv" % sym))
    return [t // 24 for t in ts], px


def load_funding(sym):
    day = {}
    with open(paths.data("funding_%s.csv" % sym), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            import calendar
            import time as _t
            d = calendar.timegm(_t.strptime(row["ts_iso"], "%Y-%m-%dT%H:%M:%SZ")) // 86400
            day[d] = day.get(d, 0.0) + float(row["rate"])
    return day


def trade_ret(px, i, j, side, fund=True):
    r = side * (px[j] / px[i] - 1.0) - 2 * SIDE_COST
    if fund:
        r -= side * FUND_DAY * (j - i)
    return r


def momentum_trades(px, n, mode, a, b, fund=True):
    out, pos, ent = [], 0, 0
    for i in range(max(a, n), b):
        s = 1 if px[i] > px[i - n] else -1
        want = s if mode == "ls" else max(s, 0)
        if want != pos:
            if pos != 0:
                out.append(trade_ret(px, ent, i, pos, fund))
            pos, ent = want, i
    if pos != 0:
        out.append(trade_ret(px, ent, b - 1, pos, fund))
    return out


def funding_trades(px, days, fd, pct, hold, a, b, fund=True):
    out, i = [], max(a, 91)
    vals = [fd.get(d, None) for d in days]
    while i < b - hold:
        hist = [v for v in vals[max(0, i - 180):i] if v is not None]
        cur = vals[i]
        if cur is not None and len(hist) >= 90:
            rank = 100.0 * sum(1 for v in hist if v <= cur) / len(hist)
            side = -1 if rank >= pct else (1 if rank <= 100 - pct else 0)
            if side:
                out.append(trade_ret(px, i, i + hold, side, fund))
                i += hold
                continue
        i += 1
    return out


def pooled(res):
    return rh.stats([t for v in res.values() for t in v])


def verdict(name, ps, per):
    wins = sum(1 for v in per.values() if rh.stats(v)["net"] > 0)
    ok = ps["net"] > 0 and ps["pf"] >= MIN_PF and ps["n"] >= MIN_TEST and wins >= 2
    print("ВЕРДИКТ %s: %s (активов в плюсе: %d из %d)" % (name, "edge подтверждён" if ok else "edge НЕ подтверждён", wins, len(per)))


def main():
    daily = {s: load_daily(s) for s in SYMS}
    start = max(d[0][0] for d in daily.values())
    end = min(d[0][-1] for d in daily.values())
    cut = start + int((end - start) * TRAIN_FRAC)
    print("Общий период: %d суток (train %d, test %d)" % (end - start, cut - start, end - cut))

    def idx(sym, d):
        days = daily[sym][0]
        for k, x in enumerate(days):
            if x >= d:
                return k
        return len(days)

    for s in SYMS:
        days, px = daily[s]
        a, b, c = idx(s, start), idx(s, cut), min(idx(s, end), len(px) - 1)
        print("  %s: баров=%d, buy&hold train=%+.0f%% test=%+.0f%%" % (s, len(px), 100 * (px[b - 1] / px[a] - 1), 100 * (px[c] / px[b] - 1)))

    # --- A: моментум ---
    print("\n=== A. Моментум ===")
    grid = [(n, m) for n in (20, 60, 120) for m in ("ls", "lf")]

    def run_a(cfg, lo, hi, fund=True):
        return {s: momentum_trades(daily[s][1], cfg[0], cfg[1], idx(s, lo), idx(s, hi), fund) for s in SYMS}
    best, bs = None, None
    for cfg in grid:
        s_ = pooled(run_a(cfg, start, cut))
        if s_["n"] >= MIN_TRAIN and (bs is None or s_["net"] > bs["net"]):
            best, bs = cfg, s_
    if best is None:
        print("Недостаточно сделок на train.")
    else:
        print("Выбрано на train: N=%d режим=%s -> %s" % (best[0], best[1], rh.fmt(bs)))
        te = run_a(best, cut, end)
        for s in SYMS:
            print("  TEST %s: %s" % (s, rh.fmt(rh.stats(te[s]))))
        ps = pooled(te)
        print("TEST ПУЛ (с funding): %s" % rh.fmt(ps))
        print("TEST ПУЛ (без funding, справка): %s" % rh.fmt(pooled(run_a(best, cut, end, fund=False))))
        verdict("A", ps, te)

    # --- B: funding ---
    print("\n=== B. Funding-контрариан ===")
    try:
        fund = {s: load_funding(s) for s in SYMS}
    except OSError as e:
        print("Нет файлов funding (%s): часть B пропущена." % e)
        return
    grid_b = [(p, h) for p in (90, 95) for h in (1, 3)]

    def run_b(cfg, lo, hi, fnd=True):
        return {s: funding_trades(daily[s][1], daily[s][0], fund[s], cfg[0], cfg[1], idx(s, lo), idx(s, hi), fnd) for s in SYMS}
    best, bs = None, None
    for cfg in grid_b:
        s_ = pooled(run_b(cfg, start, cut))
        if s_["n"] >= MIN_TRAIN and (bs is None or s_["net"] > bs["net"]):
            best, bs = cfg, s_
    if best is None:
        print("Недостаточно сделок на train.")
        return
    print("Выбрано на train: P=%d удержание=%d сут -> %s" % (best[0], best[1], rh.fmt(bs)))
    te = run_b(best, cut, end)
    for s in SYMS:
        print("  TEST %s: %s" % (s, rh.fmt(rh.stats(te[s]))))
    ps = pooled(te)
    print("TEST ПУЛ (с funding): %s" % rh.fmt(ps))
    print("TEST ПУЛ (без funding, справка): %s" % rh.fmt(pooled(run_b(best, cut, end, fnd=False))))
    verdict("B", ps, te)


if __name__ == "__main__":
    sys.exit(main())
