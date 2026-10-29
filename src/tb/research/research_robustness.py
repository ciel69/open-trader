"""Устойчивость дневного моментума: все соседние окна N, активы по отдельности, разные начальные даты. Ничего не подбирается.

Позиция по закрытию дня t-1 применяется к доходности дня t. Издержки: 0.14% за смену позиции (на сторону), funding по измерению:
0.0288%/сутки (лонг платит, шорт получает). Доходность — сложный процент на один актив без плеча.
Режимы: lf (лонг/флэт), ls (лонг/шорт). Начальные даты: весь период и смещения +180, +365, +545 суток (конец общий).
  ./tb research robustness
"""
import statistics
import sys

from tb.research import research_daily as rd

NS = (10, 20, 30, 40, 60, 80, 100, 120, 150, 200)
OFFSETS = (0, 180, 365, 545)
MODES = ("lf", "ls")


def stream(px, n, mode):
    pos, out = [0] * len(px), []
    for i in range(n, len(px)):
        s = 1 if px[i] > px[i - n] else -1
        pos[i] = s if mode == "ls" else max(s, 0)
    ret = [0.0] * len(px)
    for i in range(1, len(px)):
        p = pos[i - 1]
        ret[i] = p * (px[i] / px[i - 1] - 1.0) - p * rd.FUND_DAY - (abs(pos[i - 1] - pos[i - 2]) * rd.SIDE_COST if i >= 2 else 0.0)
    return ret, pos


def perf(rets):
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        dd = max(dd, 1.0 - eq / peak)
    return 100 * (eq - 1.0), 100 * dd


def main():
    daily = {s: rd.load_daily(s) for s in rd.SYMS}
    total_rows = []
    for mode in MODES:
        print("\n=== Режим %s ===" % mode)
        print("%-4s | %s" % ("N", " | ".join("%-21s" % ("%s: итог / б&х / просадка" % s) for s in rd.SYMS)))
        for n in NS:
            cells = []
            for s in rd.SYMS:
                px = daily[s][1]
                r, _ = stream(px, n, mode)
                tot, dd = perf(r[1:])
                bh, _ = perf([px[i] / px[i - 1] - 1.0 for i in range(1, len(px))])
                cells.append("%+6.0f%% /%+6.0f%% /%3.0f%%" % (tot, bh, dd))
            print("%-4d | %s" % (n, " | ".join(cells)))
        # разброс по началам
        res = []
        for s in rd.SYMS:
            px = daily[s][1]
            for n in NS:
                r, _ = stream(px, n, mode)
                for off in OFFSETS:
                    a = 1 + off
                    tot, dd = perf(r[a:])
                    bh, bdd = perf([px[i] / px[i - 1] - 1.0 for i in range(a, len(px))])
                    res.append((s, n, off, tot, bh, dd, bdd))
        tots = [x[3] for x in res]
        exc = [x[3] - x[4] for x in res]
        print("\nРазброс по %d комбинациям (актив x N x начало):" % len(res))
        print("  итог > 0: %.0f%% комбинаций; медиана %+.0f%%, худший %+.0f%%, лучший %+.0f%%" % (
            100.0 * sum(1 for t in tots if t > 0) / len(tots), statistics.median(tots), min(tots), max(tots)))
        print("  лучше buy&hold по доходности: %.0f%%; медиана превосходства %+.0f п.п." % (
            100.0 * sum(1 for e in exc if e > 0) / len(exc), statistics.median(exc)))
        print("  просадка меньше, чем у buy&hold: %.0f%%" % (100.0 * sum(1 for x in res if x[5] < x[6]) / len(res)))
        for s in rd.SYMS:
            sub = [x for x in res if x[0] == s]
            print("  %s: итог>0 в %.0f%%, лучше б&х в %.0f%%, медиана %+.0f%%" % (
                s, 100.0 * sum(1 for x in sub if x[3] > 0) / len(sub),
                100.0 * sum(1 for x in sub if x[3] > x[4]) / len(sub), statistics.median(x[3] for x in sub)))
        for off in OFFSETS:
            sub = [x for x in res if x[2] == off]
            print("  старт +%3d сут: итог>0 в %.0f%%, медиана %+.0f%%, лучше б&х в %.0f%%" % (
                off, 100.0 * sum(1 for x in sub if x[3] > 0) / len(sub), statistics.median(x[3] for x in sub),
                100.0 * sum(1 for x in sub if x[3] > x[4]) / len(sub)))


if __name__ == "__main__":
    sys.exit(main())
