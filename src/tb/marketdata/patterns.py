"""Свечные фигуры, уровни, сжатие волатильности, двойное дно/вершина, мультитаймфрейм. ЧИСТЫЕ функции, без I/O.

Бар = кортеж (ts_epoch, o, h, l, c, v), ts = начало бара. Все функции берут `bars` и индекс `i` ПОСЛЕДНЕГО ЗАКРЫТОГО бара и читают только bars[:i+1]
(нет заглядывания в будущее; это проверяется тестом на усечении ряда). Пивот j подтверждён только на баре j+k, поэтому на баре i используются пивоты j <= i-k.
Все пороги в cfg (DEFAULTS ниже; для сканера их можно переопределить в config/patterns_config.json). Исследование research_patterns берёт ТОЛЬКО DEFAULTS.

Формальные определения (a = ATR(atr_n) на баре i, rng = h-l, body = |c-o|):
  pin_bar     : rng >= pin_min_range_atr*a; нижняя тень/rng >= pin_wick_frac и верхняя/rng <= pin_opp_frac -> +1 (молот); зеркально -> -1 (падающая звезда)
  engulfing   : бычье: бар i-1 медвежий, бар i бычий, o_i < c_{i-1}, c_i > o_{i-1}, body_i >= engulf_min_body_atr*a; медвежье зеркально
  inside_bar  : h_i < h_{i-1} и l_i > l_{i-1}
  doji        : body <= doji_body_frac*rng и rng >= doji_min_range_atr*a
  morning_star: бар i-2 медвежий с body >= star_body_atr*a; бар i-1 body <= star_small_frac*body_{i-2}; бар i бычий и c_i > середины тела бара i-2 (вечерняя зеркально)
  nr_n        : rng_i <= rng всех предыдущих nr_n-1 баров (NR7 при nr_n=7)
  squeeze     : ранг ширины Bollinger (bb_n, bb_k сигм) среди последних squeeze_lookback значений <= squeeze_pct (доля, 0.1 = нижние 10%)
  уровень     : кластер пивотов (фракталы k=pivot_k) за level_lookback баров; пивоты в пределах level_tol_atr*ATR склеиваются; сила = сумма
                0.5^(возраст/level_half_life) по пивотам, touches = число пивотов
  двойное дно : два последних подтверждённых пивота-минимума на расстоянии dbl_min_sep..dbl_max_sep баров, |p2-p1| <= dbl_tol_atr*a, глубина
                (максимум между ними - min) >= dbl_depth_atr*a; сигнал на первом закрытии выше неклайна (макс. между дном), не позже dbl_max_age баров после
                второго дна, без нового минимума ниже; двойная вершина зеркально
  объём       : v_i >= vol_mult * среднее v предыдущих vol_n баров
  мультитф    : aggregate(bars,k) собирает k-часовые бары; тренд старшего ТФ = EMA(fast) vs EMA(slow) по ЗАКРЫТЫМ старшим барам, в 1h-индекс i попадает последний
                старший бар с last_idx <= i.
Ничего из этого НЕ является доказанным сигналом (см. docs/RESEARCH.md): это описание рынка и «подтверждение» для решения агента.
"""
import math

O, H, L, C, V = 1, 2, 3, 4, 5

DEFAULTS = {
    "atr_n": 14,
    "pin_wick_frac": 0.6, "pin_opp_frac": 0.2, "pin_min_range_atr": 0.5,
    "engulf_min_body_atr": 0.5,
    "doji_body_frac": 0.1, "doji_min_range_atr": 0.3,
    "star_body_atr": 0.6, "star_small_frac": 0.3,
    "nr_n": 7,
    "bb_n": 20, "bb_k": 2.0, "squeeze_lookback": 168, "squeeze_pct": 0.1,
    "pivot_k": 3, "level_lookback": 500, "level_tol_atr": 0.5, "level_half_life": 240, "level_min_touches": 2,
    "dbl_min_sep": 10, "dbl_max_sep": 120, "dbl_tol_atr": 0.6, "dbl_depth_atr": 1.5, "dbl_max_age": 48,
    "vol_n": 20, "vol_mult": 1.5,
    "htf_k": 4, "htf_fast": 20, "htf_slow": 50,
    "range_n": 168,
}


def merge_cfg(user=None):
    cfg = dict(DEFAULTS)
    for k, v in (user or {}).items():
        if k in DEFAULTS and isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = v
    return cfg


# ------------------------------------------------------------------ базовые ряды
def atr_series(bars, n):
    """ATR(n) = простое среднее TR последних n баров, на каждом индексе; None пока не хватает баров."""
    out, trs = [None] * len(bars), []
    for i, b in enumerate(bars):
        tr = b[H] - b[L] if i == 0 else max(b[H] - b[L], abs(b[H] - bars[i - 1][C]), abs(b[L] - bars[i - 1][C]))
        trs.append(tr)
        if i >= n:  # TR[0] без prev close не используем
            out[i] = sum(trs[i - n + 1:i + 1]) / n
    return out


def ema_series(xs, n):
    k, e, out = 2.0 / (n + 1), None, []
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def bb_width_series(bars, n, k):
    """Ширина Bollinger (верх-низ)/середина по закрытиям; None пока не хватает баров."""
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = [bars[j][C] for j in range(i - n + 1, i + 1)]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        out[i] = (2 * k * sd) / m if m > 0 else None
    return out


def rank_pct(series, i, lookback):
    """Доля значений в series[i-lookback+1..i] (без None), не превышающих series[i]. None если данных мало (< lookback/2)."""
    cur = series[i]
    if cur is None:
        return None
    w = [x for x in series[max(0, i - lookback + 1):i + 1] if x is not None]
    if len(w) < lookback // 2:
        return None
    return sum(1 for x in w if x <= cur) / float(len(w))


# ------------------------------------------------------------------ свечные фигуры (+1 бычья, -1 медвежья, 0 нет)
def pin_bar(bars, i, cfg, a):
    b = bars[i]
    rng = b[H] - b[L]
    if a is None or rng <= 0 or rng < cfg["pin_min_range_atr"] * a:
        return 0
    lo_w = (min(b[O], b[C]) - b[L]) / rng
    up_w = (b[H] - max(b[O], b[C])) / rng
    if lo_w >= cfg["pin_wick_frac"] and up_w <= cfg["pin_opp_frac"]:
        return 1
    if up_w >= cfg["pin_wick_frac"] and lo_w <= cfg["pin_opp_frac"]:
        return -1
    return 0


def engulfing(bars, i, cfg, a):
    if i < 1 or a is None:
        return 0
    p, b = bars[i - 1], bars[i]
    if abs(b[C] - b[O]) < cfg["engulf_min_body_atr"] * a:
        return 0
    if p[C] < p[O] and b[C] > b[O] and b[O] < p[C] and b[C] > p[O]:
        return 1
    if p[C] > p[O] and b[C] < b[O] and b[O] > p[C] and b[C] < p[O]:
        return -1
    return 0


def inside_bar(bars, i):
    return i >= 1 and bars[i][H] < bars[i - 1][H] and bars[i][L] > bars[i - 1][L]


def doji(bars, i, cfg, a):
    b = bars[i]
    rng = b[H] - b[L]
    return a is not None and rng > 0 and rng >= cfg["doji_min_range_atr"] * a and abs(b[C] - b[O]) <= cfg["doji_body_frac"] * rng


def star(bars, i, cfg, a):
    """+1 утренняя звезда, -1 вечерняя."""
    if i < 2 or a is None:
        return 0
    b2, b1, b0 = bars[i - 2], bars[i - 1], bars[i]
    body2, body1 = abs(b2[C] - b2[O]), abs(b1[C] - b1[O])
    if body2 < cfg["star_body_atr"] * a or body1 > cfg["star_small_frac"] * body2:
        return 0
    mid = (b2[O] + b2[C]) / 2.0
    if b2[C] < b2[O] and b0[C] > b0[O] and b0[C] > mid:
        return 1
    if b2[C] > b2[O] and b0[C] < b0[O] and b0[C] < mid:
        return -1
    return 0


def is_nr(bars, i, n):
    if i < n - 1:
        return False
    r = bars[i][H] - bars[i][L]
    return all(r <= bars[j][H] - bars[j][L] for j in range(i - n + 1, i))


def volume_confirm(bars, i, cfg):
    n = int(cfg["vol_n"])
    if i < n:
        return False
    m = sum(bars[j][V] for j in range(i - n, i)) / n
    return m > 0 and bars[i][V] >= cfg["vol_mult"] * m


# ------------------------------------------------------------------ пивоты, уровни, двойное дно/вершина
def pivot_flags(bars, k):
    """[(is_high, is_low)] по всему ряду. Пивот j виден торговле только с бара j+k (фильтруй j <= i-k)."""
    n, out = len(bars), []
    for j in range(n):
        if j < k or j + k >= n:
            out.append((False, False))
            continue
        hi = bars[j][H]
        lo = bars[j][L]
        ih = all(hi > bars[x][H] for x in range(j - k, j)) and all(hi >= bars[x][H] for x in range(j + 1, j + k + 1))
        il = all(lo < bars[x][L] for x in range(j - k, j)) and all(lo <= bars[x][L] for x in range(j + 1, j + k + 1))
        out.append((ih, il))
    return out


def levels(bars, i, cfg, flags, a):
    """Уровни на баре i: список dict(price, touches, last_age, strength), по убыванию силы. Использует пивоты j <= i-k."""
    k = int(cfg["pivot_k"])
    if a is None:
        return []
    lo_j = max(k, i - int(cfg["level_lookback"]))
    pts = []
    for j in range(lo_j, i - k + 1):
        ih, il = flags[j]
        if ih:
            pts.append((bars[j][H], j))
        if il:
            pts.append((bars[j][L], j))
    pts.sort()
    tol = cfg["level_tol_atr"] * a
    clusters, cur = [], []
    for p, j in pts:
        if cur and p - (sum(x[0] for x in cur) / len(cur)) > tol:
            clusters.append(cur)
            cur = []
        cur.append((p, j))
    if cur:
        clusters.append(cur)
    out = []
    for cl in clusters:
        out.append({"price": sum(x[0] for x in cl) / len(cl), "touches": len(cl), "last_age": i - max(x[1] for x in cl),
                    "strength": sum(0.5 ** ((i - x[1]) / float(cfg["level_half_life"])) for x in cl)})
    out.sort(key=lambda d: -d["strength"])
    return out


def near_level(lvls, price, tol, min_touches):
    """Сильнейший уровень с touches >= min_touches в пределах tol от price или None."""
    best = None
    for lv in lvls:
        if lv["touches"] >= min_touches and abs(lv["price"] - price) <= tol and (best is None or lv["strength"] > best["strength"]):
            best = lv
    return best


def nearest_levels(lvls, price, min_touches=1):
    """(ближайший уровень ниже price, ближайший выше) среди уровней с touches >= min_touches."""
    below = [x for x in lvls if x["touches"] >= min_touches and x["price"] <= price]
    above = [x for x in lvls if x["touches"] >= min_touches and x["price"] > price]
    return (max(below, key=lambda d: d["price"]) if below else None, min(above, key=lambda d: d["price"]) if above else None)


def double_extreme(bars, i, cfg, flags, a):
    """(+1|-1|0, info). +1 двойное дно: закрытие i впервые выше неклайна. info: neckline, p1, p2, j1, j2."""
    k = int(cfg["pivot_k"])
    if a is None or i < 2 * k + 2:
        return 0, None
    for side in (1, -1):
        idx = 1 if side == 1 else 0  # flags[j] = (is_high, is_low)
        piv = [j for j in range(max(k, i - int(cfg["dbl_max_sep"]) - int(cfg["dbl_max_age"]) - k), i - k + 1) if flags[j][idx]]
        if len(piv) < 2:
            continue
        j1, j2 = piv[-2], piv[-1]
        if not (cfg["dbl_min_sep"] <= j2 - j1 <= cfg["dbl_max_sep"]) or i - j2 > cfg["dbl_max_age"]:
            continue
        if side == 1:
            p1, p2 = bars[j1][L], bars[j2][L]
            neck = max(bars[x][H] for x in range(j1, j2 + 1))
            depth = neck - min(p1, p2)
            broke_before = max(bars[x][C] for x in range(j2, i)) > neck
            invalid = min(bars[x][L] for x in range(j2, i + 1)) < min(p1, p2) - cfg["dbl_tol_atr"] * a
            hit = bars[i][C] > neck
        else:
            p1, p2 = bars[j1][H], bars[j2][H]
            neck = min(bars[x][L] for x in range(j1, j2 + 1))
            depth = max(p1, p2) - neck
            broke_before = min(bars[x][C] for x in range(j2, i)) < neck
            invalid = max(bars[x][H] for x in range(j2, i + 1)) > max(p1, p2) + cfg["dbl_tol_atr"] * a
            hit = bars[i][C] < neck
        if abs(p2 - p1) <= cfg["dbl_tol_atr"] * a and depth >= cfg["dbl_depth_atr"] * a and hit and not broke_before and not invalid:
            return side, {"neckline": neck, "p1": p1, "p2": p2, "j1": j1, "j2": j2}
    return 0, None


# ------------------------------------------------------------------ мультитаймфрейм
def aggregate(bars, k, unit=3600):
    """k-часовые бары из часовых. -> (agg_bars, last_idx). Бакет = ts // (k*unit); последний бакет берётся только полным (k баров);
    last_idx[m] = индекс последнего часового бара m-го старшего бара (с этого индекса он закрыт)."""
    agg, last = [], []
    cur, cnt, cur_key = None, 0, None
    for i, b in enumerate(bars):
        key = int(b[0] // (k * unit))
        if key != cur_key:
            if cur is not None:
                agg.append(tuple(cur))
                last.append(i - 1)
            cur, cnt, cur_key = [key * k * unit, b[O], b[H], b[L], b[C], b[V]], 1, key
        else:
            cur[H], cur[L], cur[C], cur[V] = max(cur[H], b[H]), min(cur[L], b[L]), b[C], cur[V] + b[V]
            cnt += 1
    if cur is not None and cnt >= k:
        agg.append(tuple(cur))
        last.append(len(bars) - 1)
    return agg, last


def htf_trend_map(bars, k, fast, slow):
    """По каждому индексу i: +1/-1/0 — тренд старшего ТФ по последнему ЗАКРЫТОМУ старшему бару (EMA fast vs slow и закрытие относительно slow)."""
    agg, last = aggregate(bars, k)
    closes = [b[C] for b in agg]
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    tr = []
    for m in range(len(agg)):
        if m < slow:
            tr.append(0)
        elif ef[m] > es[m] and closes[m] > es[m]:
            tr.append(1)
        elif ef[m] < es[m] and closes[m] < es[m]:
            tr.append(-1)
        else:
            tr.append(0)
    out, m = [], -1
    for i in range(len(bars)):
        while m + 1 < len(agg) and last[m + 1] <= i:
            m += 1
        out.append(tr[m] if m >= 0 else 0)
    return out


# ------------------------------------------------------------------ анализатор с кэшем рядов
class Analyzer:
    """Кэширует ряды для одного набора баров. at(i)/confluence(i) читают только bars[:i+1] (кроме проверки фракталов, которые фильтруются по j <= i-k)."""

    def __init__(self, bars, cfg=None):
        self.bars, self.cfg = bars, merge_cfg(cfg)
        self._c = {}

    def _get(self, key, fn):
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    def atr(self, i, n=None):
        n = int(n or self.cfg["atr_n"])
        return self._get(("atr", n), lambda: atr_series(self.bars, n))[i]

    def flags(self, k=None):
        k = int(k or self.cfg["pivot_k"])
        return self._get(("piv", k), lambda: pivot_flags(self.bars, k))

    def bbw(self):
        return self._get(("bbw", self.cfg["bb_n"], self.cfg["bb_k"]), lambda: bb_width_series(self.bars, int(self.cfg["bb_n"]), self.cfg["bb_k"]))

    def bb_rank(self, i):
        return rank_pct(self.bbw(), i, int(self.cfg["squeeze_lookback"]))

    def htf(self, i):
        c = self.cfg
        if not c["htf_k"]:
            return 0
        return self._get(("htf", c["htf_k"], c["htf_fast"], c["htf_slow"]),
                         lambda: htf_trend_map(self.bars, int(c["htf_k"]), int(c["htf_fast"]), int(c["htf_slow"])))[i]

    def ema(self, n, i):
        return self._get(("ema", n), lambda: ema_series([b[C] for b in self.bars], n))[i]

    def levels(self, i):
        return levels(self.bars, i, self.cfg, self.flags(), self.atr(i))

    def candles(self, i):
        """Свечные фигуры бара i -> dict имя -> направление (+1/-1) либо True (inside/doji/nr)."""
        c, a, out = self.cfg, self.atr(i), {}
        for name, d in (("pin_bar", pin_bar(self.bars, i, c, a)), ("engulfing", engulfing(self.bars, i, c, a)), ("star", star(self.bars, i, c, a))):
            if d:
                out[name] = d
        if inside_bar(self.bars, i):
            out["inside_bar"] = True
        if doji(self.bars, i, c, a):
            out["doji"] = True
        if is_nr(self.bars, i, int(c["nr_n"])):
            out["nr"] = True
        return out

    def confluence(self, i):
        """Описательный счёт подтверждений для long и short: {side: {"score", "reasons"}}. НЕ валидирован как сигнал.
        +1 за каждую свечную фигуру по направлению (максимум 2), +1 у сильного уровня нужного типа (touches >= level_min_touches),
        +1 подтверждение объёмом, +1 тренд старшего ТФ по направлению, +1 сжатие волатильности (оба направления),
        +1 двойное дно/вершина по направлению; -1 за встречную фигуру, -1 если вплотную (1 ATR) встречный уровень."""
        c, a = self.cfg, self.atr(i)
        res = {"long": {"score": 0, "reasons": []}, "short": {"score": 0, "reasons": []}}
        if a is None:
            return res
        cd = self.candles(i)
        b = self.bars[i]
        lv = self.levels(i)
        tol = c["level_tol_atr"] * a
        vol = volume_confirm(self.bars, i, c)
        rk = self.bb_rank(i)
        squeeze = rk is not None and rk <= c["squeeze_pct"]
        ht = self.htf(i)
        dbl, _ = double_extreme(self.bars, i, c, self.flags(), a)
        for side, sgn in (("long", 1), ("short", -1)):
            r = res[side]
            pats = [n for n in ("pin_bar", "engulfing", "star") if cd.get(n) == sgn]
            opp = [n for n in ("pin_bar", "engulfing", "star") if cd.get(n) == -sgn]
            if pats:
                r["score"] += min(2, len(pats))
                r["reasons"].append("свеча:" + "+".join(pats))
            if opp:
                r["score"] -= 1
                r["reasons"].append("встречная свеча:" + "+".join(opp))
            lvl = near_level(lv, b[L] if sgn == 1 else b[H], tol, int(c["level_min_touches"]))
            if lvl:
                r["score"] += 1
                r["reasons"].append("у %s %.6g (касаний %d)" % ("поддержки" if sgn == 1 else "сопротивления", lvl["price"], lvl["touches"]))
            if vol:
                r["score"] += 1
                r["reasons"].append("объём x%.1f" % (b[V] / max(1e-12, sum(self.bars[j][V] for j in range(i - int(c["vol_n"]), i)) / c["vol_n"])))
            if ht == sgn:
                r["score"] += 1
                r["reasons"].append("тренд %dh по направлению" % c["htf_k"])
            if squeeze:
                r["score"] += 1
                r["reasons"].append("сжатие Bollinger")
            if dbl == sgn:
                r["score"] += 1
                r["reasons"].append("двойное %s" % ("дно" if sgn == 1 else "вершина"))
            if sgn == 1:
                wall = [x for x in lv if x["touches"] >= c["level_min_touches"] and 0 < x["price"] - b[C] <= a]
            else:
                wall = [x for x in lv if x["touches"] >= c["level_min_touches"] and 0 < b[C] - x["price"] <= a]
            if wall:
                r["score"] -= 1
                r["reasons"].append("встречный уровень в 1 ATR")
        return res
