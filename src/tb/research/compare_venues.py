"""Сравнение площадок на истории: те же сигналы, разные издержки. Разница в прибыли = число сделок x разница издержек.

Фиксированные конфигурации, ничего не подбирается:
  часовые: donchian 24 и 72, ema 24/96 на пулах (SOL, HYPE, ZEC) и (ETH, BTC); дневные: моментум 120 лонг/флэт и лонг/шорт на BTC, ETH, SOL.
Сценарии издержек на сторону (комиссия + проскальзывание): платформа 0.09%+0.05%; Bybit/OKX taker 0.055%+0.02% (VIP0, проскальзывание — допущение
для ликвидных пар); лимитные 0.02%+0 (ВЕРХНЯЯ ГРАНИЦА: не учитывает пропущенные и неблагоприятные исполнения). Funding для всех: измеренное
0.0012%/ч платформы (у Bybit базовая 0.01% за 8 ч, то есть близко). Валовая доходность = при нулевых издержках.
Доходность — сумма % от номинала по сделкам (без плеча), период test = последние 40% общего периода; полный период тоже.
  ./tb research compare
"""
import io
import contextlib
import sys

from tb import paths
from tb.research import research_daily as rd
from tb.research import research_hourly as rh
from tb.research import research_pool as rp

SCEN = (("gross (0 издержек)", 0.0, 0.0), ("платформа", 0.0009, 0.0005),
        ("Bybit/OKX taker", 0.00055, 0.0002), ("лимитные (верхняя гр.)", 0.0002, 0.0))
HOURLY_CFGS = (("donchian", 24), ("donchian", 72), ("ema", 24, 96))
POOLS = (("SOL+HYPE+ZEC", ["SOL", "HYPE", "ZEC"]), ("ETH+BTC", ["ETH", "BTC"]))


def hourly():
    print("=== Часовые (год), сумма %% номинала: полный период | test (последние 40%%) ===")
    for pname, syms in POOLS:
        assets = {s: rp.series(paths.data("ticks_%s_hist_1h.csv" % s)) for s in syms}
        start = max(v[0][0] for v in assets.values())
        end = min(v[0][-1] for v in assets.values())
        cut = start + int((end - start) * 0.6)
        for cfg in HOURLY_CFGS:
            row = []
            n_all = n_te = None
            for name, fee, slip in SCEN:
                rh.FEE, rh.SLIP = fee, slip
                full = rp.pooled(rp.run_cfg(assets, cfg, start, end))
                te = rp.pooled(rp.run_cfg(assets, cfg, cut, end))
                row.append("%-22s %+7.1f%% | %+7.1f%% (PF %.2f)" % (name, full["net"], te["net"], te["pf"]))
                n_all, n_te = full["n"], te["n"]
            print("\n%s, %s: сделок %d всего / %d на test" % (pname, cfg, n_all, n_te))
            for r in row:
                print("   " + r)
    rh.FEE, rh.SLIP = 0.0009, 0.0005


def daily():
    print("\n=== Дневной моментум N=120 (4 года, BTC+ETH+SOL), сумма %% номинала: полный | test ===")
    daily_ = {s: rd.load_daily(s) for s in rd.SYMS}
    start = max(d[0][0] for d in daily_.values())
    end = min(d[0][-1] for d in daily_.values())
    cut = start + int((end - start) * 0.6)

    def idx(s, d):
        for k, x in enumerate(daily_[s][0]):
            if x >= d:
                return k
        return len(daily_[s][0])
    for mode in ("lf", "ls"):
        print("\nрежим %s:" % mode)
        for name, fee, slip in SCEN:
            rd.SIDE_COST = fee + slip
            full = {s: rd.momentum_trades(daily_[s][1], 120, mode, idx(s, start), idx(s, end)) for s in rd.SYMS}
            te = {s: rd.momentum_trades(daily_[s][1], 120, mode, idx(s, cut), idx(s, end)) for s in rd.SYMS}
            f, t = rd.pooled(full), rd.pooled(te)
            print("   %-22s %+7.1f%% | %+7.1f%% (PF %.2f, сделок %d/%d)" % (name, f["net"], t["net"], t["pf"], f["n"], t["n"]))
    rd.SIDE_COST = rd.rh.FEE + rd.rh.SLIP


if __name__ == "__main__":
    hourly()
    daily()
