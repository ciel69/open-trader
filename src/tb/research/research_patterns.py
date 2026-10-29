"""Честная проверка свечных фигур/уровней на часовых барах. ПРАВИЛА ЗАФИКСИРОВАНЫ ДО ПЕРВОГО ЗАПУСКА (2026-10-27) и не меняются после просмотра результата.

Данные: data/candles_<SYM>_1h.csv (OHLCV). Пул вердикта: SOL, HYPE, ZEC. Справка (в вердикт не входят): ETH, BTC.
Период: [max(начал)+200 ч разгона, конец]; train = первые 60%, test = последние 40%. Параметры НЕ подбираются: у каждой гипотезы один набор,
train нужен только как проверка знака (правила на нём не обучаются). Определения фигур: marketdata/patterns.py, только patterns.DEFAULTS
(config/patterns_config.json исследование не читает, чтобы прогон был воспроизводим).

Сделка: сигнал по ЗАКРЫТОМУ бару i, вход по открытию бара i+1 (для H3 стоп-вход по пробою материнского бара внутри бара i+1; если пробиты обе стороны - пропуск).
Стоп = 1.5*ATR14(i), тейк = 2.0*стоп (RR 2), выход по времени через 48 баров по закрытию. В одном баре стоп и тейк - считаем стоп (консервативно);
гэп через стоп исполняется по открытию. Одна позиция на актив, следующий сигнал не раньше бара выхода (сделки на одном активе не перекрываются).
Издержки на сторону: платформа 0.09% + 0.05% проскальзывание; Bybit 0.10% комиссия (измерено у пользователя) + 0.02% проскальзывание (допущение).
Funding: 0.0012%/ч платформы (лонг платит, шорт получает), для Bybit то же приближение (0.01% за 8 ч = 0.00125%/ч). R = net / расстояние до стопа.

ГИПОТЕЗЫ (K=9, каждая один раз, обе стороны если не указано):
  H1 pin_at_range_extreme  : пин-бар (молот) с минимумом <= минимума предыдущих 168 баров -> лонг; падающая звезда с максимумом >= максимума 168 -> шорт
  H2 engulf_level_volume   : поглощение, чей экстремум (low для лонга, high для шорта) в пределах 0.5 ATR от уровня с >= 2 касаниями, и объём >= 1.5 x средний(20)
  H3 nr7_inside_breakout   : бар i - внутренний и NR7; пробой экстремума материнского бара (i-1) на баре i+1
  H4 star_at_extreme       : утренняя звезда, чей минимум <= минимума предыдущих 48 баров -> лонг; вечерняя у максимума 48 -> шорт
  H5 double_bottom_top     : двойное дно (вершина) по определению patterns.double_extreme, вход на первом закрытии за неклайном
  H6 pullback_pin_htf      : пин-бар по тренду 4h (EMA20>EMA50 и закрытие>EMA50), экстремум бара коснулся EMA24 часового ряда и закрытие по ту сторону
  H7 reclaim_long          : (сигнал наблюдателя long_reclaim) low < min закрытий предыдущих 168 и close выше него -> лонг
  H8 breakdown_short       : (сигнал наблюдателя short_breakdown) close < min закрытий предыдущих 168 -> шорт
  H9 squeeze_breakout_vol  : ширина Bollinger на баре i-1 в нижних 10% за 168 ч, закрытие i за границей полосы (20, 2 сигмы), объём >= 1.5 x среднего
Соседи (устойчивость): 4 по выходу (стоп 1.0 и 2.0 ATR при RR 2; RR 1.5 и 3.0 при стопе 1.5) и 2 по главному параметру фигуры (см. HYPS) = 6 на гипотезу.
Всего конфигураций 9 x 7 = 63; гипотез с поправкой K = 9.

КРИТЕРИЙ «edge есть» (для каждой площадки отдельно; все пункты сразу):
  1 test пула: net > 0 после издержек;  2 PF >= 1.3;  3 сделок >= 40;  4 плюс минимум у 2 из 3 активов пула;
  5 соседи: минимум 5 из 6 с test пула net > 0;
  6 bootstrap (по кластерам «день входа», 5000 повторов, seed 12345) нижняя граница среднего R на уровне 5%/K (поправка Бонферрони, одностороннее) > 0;
  7 не в одном периоде: train пула net > 0 и test net > 0 при начале test на 50% и на 70% периода (доли test 0.5 и 0.3).
Если не выполнен хотя бы один пункт - «edge не подтверждён». Это валидный итог.
Использование: ./tb research patterns [--quick]   (--quick: без bootstrap и соседей; для отладки, вердикта не даёт)
"""
import bisect
import random
import sys

from tb import paths
from tb.marketdata import data
from tb.marketdata import patterns as pt

POOL, REF = ["SOL", "HYPE", "ZEC"], ["ETH", "BTC"]
WARM, TRAIN_FRAC, TEST_FRACS = 200, 0.6, (0.5, 0.3)
STOP_ATR, RR, MAX_HOLD, FUND_H = 1.5, 2.0, 48, 0.000012
VENUES = {"platform": 0.0009 + 0.0005, "bybit": 0.0010 + 0.0002}
MIN_PF, MIN_TRADES, MIN_NEIGH, ALPHA, BOOT_N, SEED = 1.3, 40, 5, 0.05, 5000, 12345


# ------------------------------------------------------------------ сигналы: (kind, a, b); kind "mkt": a=направление; "brk": a=цена вверх, b=цена вниз
def _hi(bars, lo, hi):
    return max(bars[j][pt.H] for j in range(lo, hi))


def _lo(bars, lo, hi):
    return min(bars[j][pt.L] for j in range(lo, hi))


def h1(an, i, p):
    n, bars = int(p["range_n"]), an.bars
    if i < n:
        return None
    d = pt.pin_bar(bars, i, an.cfg, an.atr(i))
    if d == 1 and bars[i][pt.L] <= _lo(bars, i - n, i):
        return ("mkt", 1, None)
    if d == -1 and bars[i][pt.H] >= _hi(bars, i - n, i):
        return ("mkt", -1, None)
    return None


def h2(an, i, p):
    bars, c = an.bars, an.cfg
    d = pt.engulfing(bars, i, c, an.atr(i))
    if not d or not pt.volume_confirm(bars, i, c):
        return None
    lv = an.levels(i)
    tol = c["level_tol_atr"] * an.atr(i)
    px = bars[i][pt.L] if d == 1 else bars[i][pt.H]
    return ("mkt", d, None) if pt.near_level(lv, px, tol, int(c["level_min_touches"])) else None


def h3(an, i, p):
    bars = an.bars
    if i >= 1 and pt.inside_bar(bars, i) and pt.is_nr(bars, i, int(p["nr_n"])):
        return ("brk", bars[i - 1][pt.H], bars[i - 1][pt.L])
    return None


def h4(an, i, p):
    n, bars = int(p["range_n"]), an.bars
    if i < n + 3:
        return None
    d = pt.star(bars, i, an.cfg, an.atr(i))
    if d == 1 and _lo(bars, i - 2, i + 1) <= _lo(bars, i - 2 - n, i - 2):
        return ("mkt", 1, None)
    if d == -1 and _hi(bars, i - 2, i + 1) >= _hi(bars, i - 2 - n, i - 2):
        return ("mkt", -1, None)
    return None


def h5(an, i, p):
    d, _ = pt.double_extreme(an.bars, i, an.cfg, an.flags(), an.atr(i))
    return ("mkt", d, None) if d else None


def h6(an, i, p):
    bars, ht = an.bars, an.htf(i)
    if not ht:
        return None
    d = pt.pin_bar(bars, i, an.cfg, an.atr(i))
    if d != ht:
        return None
    e = an.ema(int(p["ema_n"]), i)
    b = bars[i]
    if d == 1 and b[pt.L] <= e <= b[pt.C]:
        return ("mkt", 1, None)
    if d == -1 and b[pt.C] <= e <= b[pt.H]:
        return ("mkt", -1, None)
    return None


def h7(an, i, p):
    n, bars = int(p["lookback"]), an.bars
    if i < n:
        return None
    m = min(bars[j][pt.C] for j in range(i - n, i))
    return ("mkt", 1, None) if bars[i][pt.L] < m < bars[i][pt.C] else None


def h8(an, i, p):
    n, bars = int(p["lookback"]), an.bars
    if i < n:
        return None
    return ("mkt", -1, None) if bars[i][pt.C] < min(bars[j][pt.C] for j in range(i - n, i)) else None


def h9(an, i, p):
    bars, c = an.bars, an.cfg
    n = int(c["bb_n"])
    if i < n or not pt.volume_confirm(bars, i, c):
        return None
    rk = pt.rank_pct(an.bbw(), i - 1, int(c["squeeze_lookback"]))
    if rk is None or rk > p["squeeze_pct"]:
        return None
    w = [bars[j][pt.C] for j in range(i - n + 1, i + 1)]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    if bars[i][pt.C] > m + c["bb_k"] * sd:
        return ("mkt", 1, None)
    if bars[i][pt.C] < m - c["bb_k"] * sd:
        return ("mkt", -1, None)
    return None


# id, имя, функция, параметры по умолчанию, два соседа по главному параметру (переопределения)
HYPS = [
    ("H1", "pin_at_range_extreme", h1, {"range_n": 168}, [{"range_n": 120}, {"range_n": 240}]),
    ("H2", "engulf_level_volume", h2, {"level_tol_atr": 0.5, "vol_mult": 1.5}, [{"vol_mult": 1.2}, {"vol_mult": 2.0}]),
    ("H3", "nr7_inside_breakout", h3, {"nr_n": 7}, [{"nr_n": 5}, {"nr_n": 9}]),
    ("H4", "star_at_extreme", h4, {"range_n": 48}, [{"range_n": 24}, {"range_n": 96}]),
    ("H5", "double_bottom_top", h5, {"dbl_tol_atr": 0.6}, [{"dbl_tol_atr": 0.4}, {"dbl_tol_atr": 0.8}]),
    ("H6", "pullback_pin_htf", h6, {"htf_fast": 20, "htf_slow": 50, "ema_n": 24}, [{"htf_fast": 15, "htf_slow": 40}, {"htf_fast": 25, "htf_slow": 60}]),
    ("H7", "reclaim_long", h7, {"lookback": 168}, [{"lookback": 120}, {"lookback": 240}]),
    ("H8", "breakdown_short", h8, {"lookback": 168}, [{"lookback": 120}, {"lookback": 240}]),
    ("H9", "squeeze_breakout_vol", h9, {"squeeze_pct": 0.1, "vol_mult": 1.5}, [{"squeeze_pct": 0.05}, {"squeeze_pct": 0.2}]),
]
EXIT_NEIGH = [{"stop": 1.0}, {"stop": 2.0}, {"rr": 1.5}, {"rr": 3.0}]


def make_signals(bars, fn, params, n_start=WARM):
    an = pt.Analyzer(bars, {k: v for k, v in params.items() if k in pt.DEFAULTS})
    sigs = {}
    for i in range(n_start, len(bars) - 1):
        s = fn(an, i, params)
        if s is not None:
            sigs[i] = s
    return sigs, an


# ------------------------------------------------------------------ симуляция
def simulate(bars, atr, sigs, start, end, stop_mult=STOP_ATR, rr=RR, max_hold=MAX_HOLD):
    """Сделки с сигналами на барах [start, end-1). Возвращает [(entry_idx, dir, gross, stop_pct, hold_bars)], gross в долях от входа без издержек."""
    trades, free = [], start
    for i in sorted(k for k in sigs if start <= k < end - 1):
        if i < free or atr[i] is None:
            continue
        kind, a, b = sigs[i]
        e = i + 1
        if kind == "mkt":
            d, entry = a, bars[e][pt.O]
        else:
            up, dn = bars[e][pt.H] >= a, bars[e][pt.L] <= b
            if up == dn:
                continue
            d = 1 if up else -1
            entry = max(a, bars[e][pt.O]) if d == 1 else min(b, bars[e][pt.O])
        dist = stop_mult * atr[i]
        stop, tp = entry - d * dist, entry + d * rr * dist
        exit_px, j = None, e
        last = min(e + max_hold - 1, end - 1)
        for j in range(e, last + 1):
            bj = bars[j]
            if d == 1:
                if bj[pt.L] <= stop:
                    exit_px = min(stop, bj[pt.O]) if j > e else stop
                    break
                if bj[pt.H] >= tp:
                    exit_px = tp
                    break
            else:
                if bj[pt.H] >= stop:
                    exit_px = max(stop, bj[pt.O]) if j > e else stop
                    break
                if bj[pt.L] <= tp:
                    exit_px = tp
                    break
        if exit_px is None:
            exit_px = bars[last][pt.C]
            j = last
        trades.append((e, d, d * (exit_px / entry - 1.0), dist / entry, j - e + 1))
        free = j
    return trades


def to_net(tr, side_cost):
    """-> (net в долях номинала, R)"""
    _, d, gross, stop_pct, hold = tr
    net = gross - 2 * side_cost - d * FUND_H * hold
    return net, net / stop_pct


def stats(rows):
    """rows: [(net, R, ...)] -> dict n, net%, pf, meanR, win."""
    n = len(rows)
    if not n:
        return {"n": 0, "net": 0.0, "pf": 0.0, "r": 0.0, "win": 0.0}
    nets = [r[0] for r in rows]
    w, l = sum(x for x in nets if x > 0), -sum(x for x in nets if x <= 0)
    return {"n": n, "net": 100 * sum(nets), "pf": (w / l) if l > 0 else float("inf"),
            "r": sum(r[1] for r in rows) / n, "win": 100.0 * sum(1 for x in nets if x > 0) / n}


def bootstrap_lb(rows, q, n_boot=BOOT_N, seed=SEED):
    """Нижняя граница среднего R (квантиль q) и p = доля повторов со средним <= 0; кластеры = день входа (сделки разных активов в один день зависимы)."""
    cl = {}
    for _, r, day in rows:
        s = cl.setdefault(day, [0.0, 0])
        s[0] += r
        s[1] += 1
    cs = list(cl.values())
    if not cs:
        return float("nan"), 1.0
    rng = random.Random(seed)
    m, means = len(cs), []
    for _ in range(n_boot):
        sr = sn = 0
        for _k in range(m):
            c = cs[int(rng.random() * m)]
            sr += c[0]
            sn += c[1]
        means.append(sr / sn)
    means.sort()
    return means[max(0, int(q * n_boot) - 1)], sum(1 for x in means if x <= 0) / float(n_boot)


# ------------------------------------------------------------------ прогон
def load_assets(syms):
    out = {}
    for s in syms:
        bars = data.load_candles(paths.data("candles_%s_1h.csv" % s))
        if bars:
            out[s] = bars
    return out


def window(assets, pool):
    start = max(assets[s][0][0] for s in pool) + WARM * 3600
    end = min(assets[s][-1][0] for s in pool) + 3600
    return start, end


def idx_of(bars, ts):
    return bisect.bisect_left([b[0] for b in bars], ts)


def run_config(assets, syms, hyp, overrides, exit_over, lo_ts, hi_ts, cache):
    """-> {sym: [(net_platform...)]} в виде сырых сделок по активам на отрезке [lo_ts, hi_ts)."""
    hid, _name, fn, base, _nb = hyp
    params = dict(base, **overrides)
    out = {}
    for s in syms:
        key = (hid, s, tuple(sorted(params.items())))
        if key not in cache:
            cache[key] = make_signals(assets[s], fn, params) + (pt.atr_series(assets[s], 14),)
        sigs, _an, atr = cache[key]
        bars = assets[s]
        a, b = idx_of(bars, lo_ts), idx_of(bars, hi_ts)
        out[s] = simulate(bars, atr, sigs, a, b, exit_over.get("stop", STOP_ATR), exit_over.get("rr", RR))
    return out


def rows_for(raw, assets, venue):
    """Плоский список (net, R, день) по пулу для площадки."""
    cost, rows = VENUES[venue], []
    for s, trs in raw.items():
        for tr in trs:
            net, r = to_net(tr, cost)
            rows.append((net, r, int(assets[s][tr[0]][0] // 86400)))
    return rows


def evaluate(assets, hyp, quick=False, cache=None):
    cache = {} if cache is None else cache
    start, end = window(assets, POOL)
    cut = start + int((end - start) * TRAIN_FRAC) // 3600 * 3600
    res = {"id": hyp[0], "name": hyp[1], "venues": {}}
    base_raw_test = run_config(assets, POOL, hyp, {}, {}, cut, end, cache)
    base_raw_train = run_config(assets, POOL, hyp, {}, {}, start, cut, cache)
    starts = [start + int((end - start) * (1 - f)) // 3600 * 3600 for f in TEST_FRACS]
    start_raws = [run_config(assets, POOL, hyp, {}, {}, s0, end, cache) for s0 in starts]
    neigh_raws = []
    if not quick:
        for ov in hyp[4]:
            neigh_raws.append(run_config(assets, POOL, hyp, ov, {}, cut, end, cache))
        for ex in EXIT_NEIGH:
            neigh_raws.append(run_config(assets, POOL, hyp, {}, ex, cut, end, cache))
    ref_raw = run_config(assets, [s for s in REF if s in assets], hyp, {}, {}, cut, end, cache)
    K = len(HYPS)
    for v in VENUES:
        rt = rows_for(base_raw_test, assets, v)
        st, tr_st = stats(rt), stats(rows_for(base_raw_train, assets, v))
        per_asset = {s: stats(rows_for({s: base_raw_test[s]}, assets, v)) for s in POOL}
        pos_assets = sum(1 for x in per_asset.values() if x["net"] > 0)
        start_ok = [stats(rows_for(r, assets, v))["net"] > 0 for r in start_raws]
        neigh_pos = sum(1 for r in neigh_raws if stats(rows_for(r, assets, v))["net"] > 0)
        lb, p = (float("nan"), 1.0) if quick or not rt else bootstrap_lb(rt, ALPHA / K)
        crit = {
            "net>0": st["net"] > 0, "PF>=%.1f" % MIN_PF: st["pf"] >= MIN_PF, "n>=%d" % MIN_TRADES: st["n"] >= MIN_TRADES,
            "активов+>=2": pos_assets >= 2, "соседи>=%d/6" % MIN_NEIGH: neigh_pos >= MIN_NEIGH,
            "bootLB>0": lb == lb and lb > 0, "train>0": tr_st["net"] > 0, "старт test": all(start_ok),
        }
        ref = {s: stats(rows_for({s: ref_raw[s]}, assets, v)) for s in ref_raw}
        res["venues"][v] = {"test": st, "train": tr_st, "assets": per_asset, "neigh_pos": neigh_pos, "neigh_n": len(neigh_raws),
                            "start_net": [stats(rows_for(r, assets, v))["net"] for r in start_raws], "lb": lb, "p": p, "crit": crit,
                            "edge": all(crit.values()) and not quick, "ref": ref}
    return res


def baseline(assets, cut, end, step=6):
    """Нулевая модель (справка, не гипотеза): случайное направление на каждом step-м баре, те же выход и издержки. Показывает, как выглядит «нет edge»."""
    rnd, raw = random.Random(SEED), {}
    for s in POOL:
        bars = assets[s]
        sigs = {i: ("mkt", rnd.choice((1, -1)), None) for i in range(WARM, len(bars) - 1, step)}
        raw[s] = simulate(bars, pt.atr_series(bars, 14), sigs, idx_of(bars, cut), idx_of(bars, end))
    return {v: stats(rows_for(raw, assets, v)) for v in VENUES}


def fmt_stats(s):
    return "n=%d net=%+.1f%% PF=%s R=%+.2f" % (s["n"], s["net"], "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"], s["r"])


def main(argv):
    quick = "--quick" in argv
    assets = load_assets(POOL + REF)
    missing = [s for s in POOL if s not in assets]
    if missing:
        print("нет данных data/candles_<SYM>_1h.csv для: %s" % ", ".join(missing))
        return 1
    start, end = window(assets, POOL)
    cut = start + int((end - start) * TRAIN_FRAC) // 3600 * 3600
    d = data.epoch_to_iso
    print("Период %s .. %s (разгон %d ч), train до %s, test после. Пул %s; гипотез K=%d, конфигураций %d.%s" % (
        d(start), d(end), WARM, d(cut), "+".join(POOL), len(HYPS), len(HYPS) * 7, "  [--quick: вердикта нет]" if quick else ""))
    print("Издержки на сторону: " + ", ".join("%s %.2f%%" % (k, 100 * v) for k, v in VENUES.items()) + "; стоп %.1f ATR, RR %.1f, выход по времени %d баров" % (STOP_ATR, RR, MAX_HOLD))
    for v, st in baseline(assets, cut, end).items():
        print("Нулевая модель (случайный вход каждые 6 ч, test пула) %-8s %s" % (v, fmt_stats(st)))
    cache, results = {}, []
    for hyp in HYPS:
        r = evaluate(assets, hyp, quick, cache)
        results.append(r)
        print("\n%s %s" % (r["id"], r["name"]))
        for v, x in r["venues"].items():
            print("  %-8s TEST %s | train %s | активы %s" % (
                v, fmt_stats(x["test"]), fmt_stats(x["train"]),
                " ".join("%s:%+.0f%%/%d" % (s, a["net"], a["n"]) for s, a in x["assets"].items())))
            print("           соседи+ %d/%d | старт test(50%%,30%%) net %s | bootLB(R) %s p=%.3f | ref ETH/BTC %s" % (
                x["neigh_pos"], x["neigh_n"], "/".join("%+.0f%%" % n for n in x["start_net"]),
                "n/a" if x["lb"] != x["lb"] else "%+.2f" % x["lb"], x["p"],
                " ".join("%s:%+.0f%%/%d" % (s, a["net"], a["n"]) for s, a in x["ref"].items())))
            print("           критерии: " + " ".join("%s=%s" % (k, "да" if ok else "НЕТ") for k, ok in x["crit"].items()) +
                  "  => %s" % ("EDGE ЕСТЬ" if x["edge"] else "edge не подтверждён"))
    print("\nИТОГ (K=%d гипотез, %d конфигураций, test открыт один раз на конфигурацию):" % (len(HYPS), len(HYPS) * 7))
    for v in VENUES:
        ok = [r["id"] for r in results if r["venues"][v]["edge"]]
        print("  %-8s: edge подтверждён у %s" % (v, ", ".join(ok) if ok else "НИ ОДНОЙ гипотезы"))
    if quick:
        print("(--quick: итог не является вердиктом)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
