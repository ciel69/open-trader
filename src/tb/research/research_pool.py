"""Пул активов, один заранее зафиксированный train/test. ПРАВИЛА ЗАДАНЫ ДО ПОЛУЧЕНИЯ ДАННЫХ и не меняются.

Регистрация (зафиксировано 2026-10-27 до загрузки SOL/HYPE/ZEC):
  * Основной пул: SOL, HYPE, ZEC. ETH и BTC печатаются отдельно как справка (их тест уже виден ранее).
  * Часовые закрытия. Сетка: 10 конфигураций из research_hourly.GRID, без изменений.
  * Общая граница по времени: общий период = [max(начал), min(концов)], train = первые 60%, test = последние 40%.
    Данные до начала общего периода используются только как разгон индикаторов.
  * Выбор конфигурации: максимальная суммарная net-доходность пула на train при >= 20 сделок на train.
  * Test открывается один раз. Результат читается по ПУЛУ целиком; отдельные активы — только справка.
  * Вердикт «есть edge» только если test пула: net > 0, PF >= 1.2, сделок >= 40 и плюс не менее чем у половины активов.
    Иначе вердикт «edge не подтверждён». Правила вердикта не меняются после просмотра результата.
Использование: ./tb research pool data/ticks_SOL_hist_1h.csv data/ticks_HYPE_hist_1h.csv data/ticks_ZEC_hist_1h.csv \
                                          [--ref data/ticks_ETH_hist_1h.csv data/ticks_BTC_hist_1h.csv]
"""
import sys

from tb.marketdata import data
from tb.research import research_hourly as rh

TRAIN_FRAC, MIN_TRAIN_TRADES = 0.6, 20
VERDICT_MIN_PF, VERDICT_MIN_TRADES = 1.2, 40


def series(path):
    last, ts_out, px = None, [], []
    for ts, p in data.load_ticks(path):
        h = int(ts // 3600)
        if h != last:
            ts_out.append(h)
            px.append(p)
            last = h
        else:
            px[-1] = p
    return ts_out, px


def idx_from(ts, h):
    for i, t in enumerate(ts):
        if t >= h:
            return i
    return len(ts)


def run_cfg(assets, cfg, lo, hi):
    """Сделки по каждому активу на отрезке часов [lo, hi) (в часах от эпохи)."""
    res = {}
    for name, (ts, px) in assets.items():
        pos = rh.positions(px, cfg)
        a, b = idx_from(ts, lo), idx_from(ts, hi)
        res[name] = rh.simulate(px, pos, a, b) if b - a > 1 else []
    return res


def pooled(res):
    allt = [t for v in res.values() for t in v]
    return rh.stats(allt)


def main(argv):
    ref = []
    if "--ref" in argv:
        k = argv.index("--ref")
        ref, argv = argv[k + 1:], argv[:k]
    pool = {p: series(p) for p in argv}
    refs = {p: series(p) for p in ref}
    start = max(v[0][0] for v in pool.values())
    end = min(v[0][-1] for v in pool.values())
    cut = start + int((end - start) * TRAIN_FRAC)
    print("Общий период: %d часов, граница train/test после %d часов" % (end - start, cut - start))
    for name, (ts, px) in pool.items():
        a, b, c = idx_from(ts, start), idx_from(ts, cut), idx_from(ts, end)
        print("  %s: баров=%d, buy&hold train=%+.1f%% test=%+.1f%%" % (
            name, len(px), 100 * (px[b - 1] / px[a] - 1), 100 * (px[min(c, len(px) - 1)] / px[b] - 1)))
    best, best_s = None, None
    for cfg in rh.GRID:
        s = pooled(run_cfg(pool, cfg, start, cut))
        if s["n"] >= MIN_TRAIN_TRADES and (best_s is None or s["net"] > best_s["net"]):
            best, best_s = cfg, s
    if best is None:
        print("На train недостаточно сделок (< %d) у всех конфигураций: вердикт не выносится." % MIN_TRAIN_TRADES)
        return
    print("\nВыбрано на train: %s -> %s" % (best, rh.fmt(best_s)))
    te = run_cfg(pool, best, cut, end)
    for name, tr in te.items():
        print("TEST %s: %s" % (name, rh.fmt(rh.stats(tr))))
    ps = pooled(te)
    pos_assets = sum(1 for tr in te.values() if rh.stats(tr)["net"] > 0)
    print("TEST ПУЛ: %s, активов в плюсе: %d из %d" % (rh.fmt(ps), pos_assets, len(te)))
    ok = ps["net"] > 0 and ps["pf"] >= VERDICT_MIN_PF and ps["n"] >= VERDICT_MIN_TRADES and pos_assets * 2 >= len(te)
    print("ВЕРДИКТ: %s" % ("edge подтверждён по заранее заданным критериям" if ok else "edge НЕ подтверждён"))
    if refs:
        print("\nСправка (ETH/BTC, та же конфигурация, тот же период):")
        for name, (ts, px) in refs.items():
            lo = max(start, ts[0])
            r = run_cfg({name: (ts, px)}, best, cut, min(end, ts[-1]))[name]
            print("  %s test: %s" % (name, rh.fmt(rh.stats(r))))
    print("(конфигураций на train: %d; test открыт один раз)" % len(rh.GRID))


if __name__ == "__main__":
    main(sys.argv[1:])
