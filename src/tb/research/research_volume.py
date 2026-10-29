"""Предсказывает ли ОБЪЁМ (в связке со свечой) отскок или продолжение после импульса? ПРАВИЛА ЗАФИКСИРОВАНЫ ДО ПЕРВОГО ПРОГОНА (2026-10-27).

Данные: data/klines/k5_<SYMBOL>.csv (5м свечи Bybit USDT-перпов с volume, ./tb bybit read klines --top 100 --days 40). Вердикт даёт группа crypto
(без BTC/ETH/SOL, они в прошлых тестах); токенизированные акции/ETF/металлы/нефть (klines.STOCK_LIKE) идут справочно и в вердикт не входят.
Сетка 5м, пропущенные свечи = дыры (признаки с дырами в нужных окнах не считаются, сделка с дырой в пути пропускается).

Определения (бар j закрыт; все пороги заданы один раз, не подбираются):
  ret_j  = close/open - 1;  импульс: |ret_j| >= X = 1.0%;  d = знак ret_j
  vm_j   = volume_j / медиана volume за предыдущие 288 баров (24 ч, без j; нужно >= 250 присутствующих баров)
  cp_j   = положение закрытия в диапазоне в сторону импульса: (close-low)/(high-low) при d>0, иначе (high-close)/(high-low)
Гипотезы (K_H = 6), сделка по направлению гипотезы, вход по открытию бара entry:
  V1  vm>=3 и cp>=2/3 (закрытие у края в сторону импульса)       -> по импульсу (+d), вход open j+1
  V2  vm>=3 и cp<=1/3 (длинная тень против импульса)             -> против (-d),     вход open j+1
  V3  vm<1 (импульс на малом объёме)                              -> против (-d),     вход open j+1
  V4  vm>=3 и volume_j > v_{j+1} > v_{j+2} > v_{j+3} (затухание) -> против (-d),     вход open j+4
  V5a vm>=10 (экстремальный объём)                                -> против (-d),     вход open j+3
  V5b vm>=10                                                      -> по импульсу (+d), вход open j+3
Выходы (4 варианта на гипотезу): время через H = 3, 6, 12 баров (15/30/60 мин; цена = close последнего бара удержания) и «SL»: стоп 1.5*ATR14,
тейк 3.0*ATR14 (ATR по 14 барам ДО импульса, в % от close бара j-1), максимум 12 баров. В одном баре касание обоих = стоп (консервативно);
гэп через стоп исполняется по open бара; тейк исполняется ровно по уровню. Одна позиция на монету, сделки одной монеты не перекрываются.
K = 6 гипотез x 4 выхода = 24 проверки (поправка Бонферрони: p_adj = min(1, 24 * p), p односторонний по t с кластерами «день входа»).
Издержки (config/risk_profile.json costs.bybit; запасные значения совпадают): taker вход+выход = 2*(0.10% + 0.02%) = 0.24% круга;
оптимистичный maker-вход: 0.036% (без проскальзывания, заполнение гарантировано, адверс-селекции нет) + taker выход 0.12% = 0.156%. Funding за <=60 мин не учтён.
Нулевые модели (на ТЕХ ЖЕ импульсах |ret|>=X без фильтров объёма/свечи, тот же сдвиг входа и выход): по импульсу, против импульса, случайное направление (seed 12345).
Разбиение по времени одинаково для всех монет: окно [первая свеча самой ранней монеты + 24 ч, конец], train = первые 60%, test = последние 40% (три равные части по времени).
Сделка train должна и войти, и выйти до границы; сделка test входит после границы. Никакого обучения: train нужен как проверка знака, test смотрится один раз.
ВЕРДИКТ «edge есть» для варианта (все пункты на test, группа crypto, taker-издержки): net > 0; PF >= 1.3; n >= 100; средний net выше ВСЕХ трёх нулевых моделей;
плюс минимум в 2 из 3 частей test. «Строго» дополнительно: p_adj < 0.05. Иначе «нет».
Оговорки: коррелированные события (одни и те же рыночные дни, монеты ходят вместе; SE считается по кластерам-дням, но дней мало), выживание (берём ТЕКУЩИЕ ликвидные монеты),
короткая история (~40 дней, один рыночный режим), 5м-свеча не показывает порядок high/low внутри бара (поэтому стоп первым), проскальзывание на малых монетах выше принятого.

Использование: ./tb research volume [--counts] [--dir data/klines] [--group crypto|stock|all]
  --counts: только число импульсов на train при разных X (без доходностей), для проверки, что выборки хватит.
"""
import argparse
import math
import os
import random
import sys
from statistics import NormalDist, median

from tb import paths
from tb.marketdata import klines

STEP = 300
X, M_BIG, M_EXT, M_LOW = 0.01, 3.0, 10.0, 1.0
LOOK, MIN_LOOK, ATR_N = 288, 250, 14
HORIZONS = (3, 6, 12)
SL_ATR, TP_ATR, MAX_HOLD = 1.5, 3.0, 12
TRAIN_FRAC, PARTS = 0.6, 3
MIN_PF, MIN_N, ALPHA, SEED = 1.3, 100, 0.05, 12345
FEE, SLIP, MAKER_FEE = 0.001, 0.0002, 0.00036
EXCLUDE_REF = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# (id, описание, сдвиг входа от бара импульса, функция(ev, s) -> направление или None)
def _v1(ev, s):
    return ev["d"] if ev["vm"] >= M_BIG and ev["cp"] >= 2 / 3.0 else None


def _v2(ev, s):
    return -ev["d"] if ev["vm"] >= M_BIG and ev["cp"] <= 1 / 3.0 else None


def _v3(ev, s):
    return -ev["d"] if ev["vm"] < M_LOW else None


def _v4(ev, s):
    j = ev["j"]
    if ev["vm"] < M_BIG or j + 3 >= len(s.v):
        return None
    a, b, c, d = s.v[j], s.v[j + 1], s.v[j + 2], s.v[j + 3]
    if None in (a, b, c, d):
        return None
    return -ev["d"] if a > b > c > d else None


def _v5a(ev, s):
    return -ev["d"] if ev["vm"] >= M_EXT else None


def _v5b(ev, s):
    return ev["d"] if ev["vm"] >= M_EXT else None


HYPS = [("V1", "объём+закрытие по импульсу -> продолжение", 1, _v1),
        ("V2", "объём+тень против -> откат", 1, _v2),
        ("V3", "малый объём -> откат", 1, _v3),
        ("V4", "затухание объёма 3 бара -> разворот", 4, _v4),
        ("V5a", "объём>=10x, против через 3 бара", 3, _v5a),
        ("V5b", "объём>=10x, по импульсу через 3 бара", 3, _v5b)]
EXITS = [("H%d" % h, h) for h in HORIZONS] + [("SL", 0)]
K = len(HYPS) * len(EXITS)


# ------------------------------------------------------------------ данные
class Series(object):
    """Ряд монеты на сетке 5м: t0 = open первой свечи, массивы o,h,l,c,v (None = дыра)."""

    def __init__(self, sym, group, t0, o, h, l, c, v):
        self.sym, self.group, self.t0 = sym, group, t0
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v

    def ts(self, i):
        return self.t0 + i * STEP

    def __len__(self):
        return len(self.o)


def from_rows(sym, group, rows):
    """rows: {ts: (ts, o, h, l, c, volume, turnover)} -> Series на сетке."""
    if not rows:
        return None
    ts = sorted(rows)
    t0 = ts[0] // STEP * STEP
    n = (ts[-1] - t0) // STEP + 1
    o, h, l, c, v = ([None] * n for _ in range(5))
    for t in ts:
        i = (t - t0) // STEP
        r = rows[t]
        try:
            o[i], h[i], l[i], c[i], v[i] = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            pass
    return Series(sym, group, t0, o, h, l, c, v)


def load_all(directory, group="crypto"):
    out = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith("k5_") and name.endswith(".csv")):
            continue
        sym = name[3:-4]
        g = klines.group_of(sym)
        if (group != "all" and g != group) or sym in EXCLUDE_REF:
            continue
        s = from_rows(sym, g, klines.load_csv(os.path.join(directory, name)))
        if s is not None and len(s) > LOOK + 50:
            out.append(s)
    return out


# ------------------------------------------------------------------ признаки
def _full(s, j):
    return None not in (s.o[j], s.h[j], s.l[j], s.c[j], s.v[j])


def vol_median(s, j):
    """Медиана volume предыдущих LOOK баров (без j); None, если присутствует меньше MIN_LOOK."""
    if j < LOOK:
        return None
    vs = [x for x in s.v[j - LOOK:j] if x is not None]
    if len(vs) < MIN_LOOK:
        return None
    m = median(vs)
    return m if m > 0 else None


def close_pos(o, h, l, c, d):
    """Положение закрытия в диапазоне в сторону импульса d (+1 вверх/-1 вниз): 1 = закрытие у края импульса, 0 = у противоположного."""
    if h <= l:
        return 0.5
    return (c - l) / (h - l) if d > 0 else (h - c) / (h - l)


def impulses(s, x=X, lo=0, hi=None):
    """События-импульсы: [{'j', 'd', 'ret', 'vm', 'cp'}] для баров j с индексом в [lo, hi). Медиана считается только на импульсах."""
    hi = len(s) if hi is None else hi
    out = []
    for j in range(max(lo, LOOK), hi):
        if not _full(s, j) or s.o[j] <= 0:
            continue
        ret = s.c[j] / s.o[j] - 1.0
        if abs(ret) < x:
            continue
        m = vol_median(s, j)
        if m is None:
            continue
        d = 1 if ret > 0 else -1
        out.append({"j": j, "d": d, "ret": ret, "vm": s.v[j] / m, "cp": close_pos(s.o[j], s.h[j], s.l[j], s.c[j], d)})
    return out


def atr_pct(s, j):
    """Средний True Range 14 баров ДО бара j, в долях close бара j-1; None при дырах."""
    if j < ATR_N + 1 or s.c[j - 1] in (None, 0):
        return None
    trs = []
    for k in range(j - ATR_N, j):
        if None in (s.h[k], s.l[k], s.c[k - 1]):
            return None
        trs.append(max(s.h[k] - s.l[k], abs(s.h[k] - s.c[k - 1]), abs(s.l[k] - s.c[k - 1])))
    return (sum(trs) / len(trs)) / s.c[j - 1]


# ------------------------------------------------------------------ симуляция
def cost_taker():
    return 2 * (FEE + SLIP)


def cost_maker():
    return MAKER_FEE + FEE + SLIP


def exit_trade(s, e, d, mode, h, ref_atr):
    """Одна сделка от open бара e. -> (gross, индекс бара после выхода) или None (дыра/нет данных).
    mode 'H': выход по close бара e+h-1. mode 'SL': стоп/тейк по ATR, максимум MAX_HOLD баров."""
    if e < 1 or e >= len(s) or s.o[e] is None or s.o[e] <= 0:
        return None
    entry = s.o[e]
    if mode == "H":
        last = e + h - 1
        if last >= len(s) or s.c[last] is None:
            return None
        return d * (s.c[last] / entry - 1.0), e + h
    if ref_atr is None or e + MAX_HOLD - 1 >= len(s):
        return None
    sl, tp = SL_ATR * ref_atr, TP_ATR * ref_atr
    stop, take = entry * (1 - d * sl), entry * (1 + d * tp)
    for k in range(e, e + MAX_HOLD):
        if None in (s.o[k], s.h[k], s.l[k], s.c[k]):
            return None
        if d > 0:
            hit_stop, hit_tp = s.l[k] <= stop, s.h[k] >= take
            gap = k > e and s.o[k] <= stop
        else:
            hit_stop, hit_tp = s.h[k] >= stop, s.l[k] <= take
            gap = k > e and s.o[k] >= stop
        if hit_stop:  # при касании обоих уровней считаем стоп (порядок внутри бара неизвестен)
            px = s.o[k] if gap else stop
            return d * (px / entry - 1.0), k + 1
        if hit_tp:
            return d * (take / entry - 1.0), k + 1
    return d * (s.c[e + MAX_HOLD - 1] / entry - 1.0), e + MAX_HOLD


def simulate(s, signals, exit_name, t_lo, t_hi):
    """signals: [(entry_idx, direction, ref_atr)] -> сделки [(sym, entry_ts, gross)] с входом в [t_lo, t_hi) и выходом не позже t_hi.
    Одна позиция на монету: сигнал раньше индекса выхода предыдущей сделки пропускается."""
    mode, h = ("SL", 0) if exit_name == "SL" else ("H", int(exit_name[1:]))
    out, busy = [], -1
    for e, d, ref in sorted(signals, key=lambda x: x[0]):
        if e < busy or not (t_lo <= s.ts(e) < t_hi):
            continue
        r = exit_trade(s, e, d, mode, h, ref)
        if r is None:
            continue
        gross, nxt = r
        if s.ts(nxt) > t_hi:
            continue
        busy = nxt
        out.append((s.sym, s.ts(e), gross))
    return out


def hyp_signals(s, events, delay, fn):
    sigs = []
    for ev in events:
        d = fn(ev, s)
        if d is None:
            continue
        sigs.append((ev["j"] + delay, d, atr_pct(s, ev["j"])))
    return sigs


def null_signals(s, events, delay, kind, rng):
    sigs = []
    for ev in events:
        d = ev["d"] if kind == "with" else (-ev["d"] if kind == "against" else rng.choice((1, -1)))
        sigs.append((ev["j"] + delay, d, atr_pct(s, ev["j"])))
    return sigs


# ------------------------------------------------------------------ статистика
def stats(trades, cost):
    """trades [(sym, ts, gross)] -> показатели net = gross - cost. SE по кластерам «день входа»."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "gross": 0.0, "net": 0.0, "win": 0.0, "pf": 0.0, "se": 0.0, "t": 0.0, "p": 1.0, "coins": 0, "coins_pos": 0, "days": 0, "best_share": 0.0}
    nets = [g - cost for _, _, g in trades]
    mean = sum(nets) / n
    win = sum(1 for x in nets if x > 0) / float(n)
    gp = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    clus = {}
    for (_, ts, _), x in zip(trades, nets):
        clus[int(ts // 86400)] = clus.get(int(ts // 86400), 0.0) + (x - mean)
    se = math.sqrt(sum(v * v for v in clus.values())) / n
    t = mean / se if se > 0 else 0.0
    p = 1.0 - NormalDist().cdf(t)            # односторонний: H1 «среднее > 0»
    by = {}
    for (sym, _, _), x in zip(trades, nets):
        by[sym] = by.get(sym, 0.0) + x
    tot_pos = sum(v for v in by.values() if v > 0)
    return {"n": n, "gross": sum(g for _, _, g in trades) / n, "net": mean, "win": win, "pf": pf, "se": se, "t": t, "p": p,
            "coins": len(by), "coins_pos": sum(1 for v in by.values() if v > 0), "days": len(clus),
            "best_share": (max(by.values()) / tot_pos) if tot_pos > 0 and max(by.values()) > 0 else 0.0}


def split_times(series, train_frac=TRAIN_FRAC, parts=PARTS):
    """Общие для всех монет границы: (start, cut, end, [границы частей test]). start = самая ранняя свеча + 24 ч прогрева."""
    first = min(s.t0 for s in series)
    end = max(s.ts(len(s)) for s in series)
    start = first + LOOK * STEP
    cut = start + int((end - start) * train_frac) // STEP * STEP
    edges = [cut + int((end - cut) * k / float(parts)) // STEP * STEP for k in range(parts)] + [end]
    return start, cut, end, edges


def split_parts(trades, edges):
    return [[t for t in trades if edges[k] <= t[1] < edges[k + 1]] for k in range(len(edges) - 1)]


def verdict(test_taker, nulls_net, thirds_net, k=K):
    """Критерии пользователя на test (taker): net>0, PF>=1.3, n>=100, лучше всех нулевых, плюс в >=2 из 3 частей; строго: p_adj<0.05."""
    p_adj = min(1.0, k * test_taker["p"])
    c = {"net>0": test_taker["net"] > 0, "pf>=1.3": test_taker["pf"] >= MIN_PF, "n>=100": test_taker["n"] >= MIN_N,
         "лучше нулей": all(test_taker["net"] > x for x in nulls_net), "2из3 частей": sum(1 for x in thirds_net if x > 0) >= 2}
    return {"criteria": c, "edge": all(c.values()), "p_adj": p_adj, "strict": all(c.values()) and p_adj < ALPHA}


# ------------------------------------------------------------------ прогон
def run_variant(series, events_by, delay, fn, exit_name, window, null_kind=None):
    """Сделки по всем монетам в окне [lo, hi). fn=None + null_kind -> нулевая модель."""
    rng = random.Random(SEED)
    out = []
    for s in series:
        ev = events_by[s.sym]
        sigs = null_signals(s, ev, delay, null_kind, rng) if null_kind else hyp_signals(s, ev, delay, fn)
        out.extend(simulate(s, sigs, exit_name, window[0], window[1]))
    return out


def fmt(st):
    pf = "inf" if st["pf"] == float("inf") else "%.2f" % st["pf"]
    return "n=%-5d gross %+.3f%% net %+.3f%% win %4.1f%% PF %s" % (st["n"], 100 * st["gross"], 100 * st["net"], 100 * st["win"], pf)


def counts(series):
    start, cut, end, _ = split_times(series)
    print("Число импульсов на TRAIN [%s .. %s) по X (без доходностей):" % (klines.epoch_to_iso(start), klines.epoch_to_iso(cut)))
    for x in (0.006, 0.008, 0.01, 0.015, 0.02):
        tot = big = ext = 0
        for s in series:
            for ev in impulses(s, x):
                if start <= s.ts(ev["j"]) < cut:
                    tot += 1
                    big += ev["vm"] >= M_BIG
                    ext += ev["vm"] >= M_EXT
        print("  X=%.1f%%: импульсов %d, с vm>=3: %d, vm>=10: %d" % (100 * x, tot, big, ext))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--dir", default="")
    ap.add_argument("--group", default="crypto", choices=["crypto", "stock", "all"])
    a = ap.parse_args(argv)
    series = load_all(a.dir or paths.data("klines"), a.group)
    if not series:
        print("нет данных: ./tb bybit read klines --top 100 --days 40", file=sys.stderr)
        return 2
    if a.counts:
        return counts(series)
    start, cut, end, edges = split_times(series)
    print("Группа %s: монет %d, свечей %d. Окно %s .. %s; train до %s, test %s .. %s (3 части)" % (
        a.group, len(series), sum(sum(1 for x in s.o if x is not None) for s in series), klines.epoch_to_iso(start), klines.epoch_to_iso(end),
        klines.epoch_to_iso(cut), klines.epoch_to_iso(cut), klines.epoch_to_iso(end)))
    print("Издержки круга: taker %.3f%%, maker-вход %.3f%%. K = %d. Нули: на всех импульсах |ret|>=%.1f%%.\n" % (100 * cost_taker(), 100 * cost_maker(), K, 100 * X))
    events_by = {s.sym: impulses(s) for s in series}
    n_ev = sum(len(v) for v in events_by.values())
    print("Импульсов всего: %d\n" % n_ev)
    train_w, test_w = (start, cut), (cut, end)
    results = []
    for hid, desc, delay, fn in HYPS:
        print("== %s %s (вход +%d бар от импульса)" % (hid, desc, delay))
        for ename, _ in EXITS:
            tr = stats(run_variant(series, events_by, delay, fn, ename, train_w), cost_taker())
            te_tr = run_variant(series, events_by, delay, fn, ename, test_w)
            te = stats(te_tr, cost_taker())
            te_m = stats(te_tr, cost_maker())
            nulls = {k: stats(run_variant(series, events_by, delay, None, ename, test_w, k), cost_taker()) for k in ("with", "against", "random")}
            thirds = [stats(p, cost_taker())["net"] if p else 0.0 for p in split_parts(te_tr, edges)]
            thirds_n = [len(p) for p in split_parts(te_tr, edges)]
            v = verdict(te, [x["net"] for x in nulls.values()], thirds)
            results.append((hid, ename, v))
            print("  %-3s train: %s" % (ename, fmt(tr)))
            print("      TEST : %s | maker net %+.3f%% | t_cl %.2f p_adj %.3f | монет %d (плюс %d), дней %d, доля лучшей %.0f%%" % (
                fmt(te), 100 * te_m["net"], te["t"], v["p_adj"], te["coins"], te["coins_pos"], te["days"], 100 * te["best_share"]))
            print("      нули : по импульсу %+.3f%% | против %+.3f%% | случайно %+.3f%%  (n=%d)  части test net: %s (n %s)" % (
                100 * nulls["with"]["net"], 100 * nulls["against"]["net"], 100 * nulls["random"]["net"], nulls["with"]["n"],
                " / ".join("%+.3f%%" % (100 * x) for x in thirds), "/".join(str(x) for x in thirds_n)))
            print("      вердикт: %s%s  %s" % ("EDGE" if v["edge"] else "нет", " (строго)" if v["strict"] else "",
                                              ",".join(k for k, ok in v["criteria"].items() if not ok) and "не прошли: " + ",".join(k for k, ok in v["criteria"].items() if not ok)))
        print("")
    edges_found = [(h, e) for h, e, v in results if v["edge"]]
    print("ИТОГ (%s): вариантов с EDGE по критерию пользователя: %d из %d; строго (с поправкой K): %d. %s" % (
        a.group, len(edges_found), K, sum(1 for _, _, v in results if v["strict"]), edges_found or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
