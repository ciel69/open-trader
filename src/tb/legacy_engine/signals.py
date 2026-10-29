"""Сигнал: EMA-тренд + моментум + фильтры ATR и боковика + проверка edge vs издержки.
Все *_pct на выходе — ПРОЦЕНТЫ цены (1.2 = 1.2%). Конфиг: taker_fee_pct и slippage_buffer_pct — ДОЛИ (0.0009 = 0.09%)."""


def ema_series(values, n):
    if not values:
        return []
    n = max(1, int(n))
    a = 2.0 / (n + 1.0)
    k = min(n, len(values))
    e = sum(values[:k]) / k
    out = [e] * k  # до накопления n значений — SMA-затравка
    for v in values[k:]:
        e = a * v + (1.0 - a) * e
        out.append(e)
    return out


def atr_abs(bars, n):
    """Простое среднее true range за последние n баров (в единицах цены)."""
    trs = []
    start = max(1, len(bars) - n)
    for i in range(start, len(bars)):
        pc = bars[i - 1]["c"]
        h, l = bars[i]["h"], bars[i]["l"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def round_trip_cost_pct(cfg, funding_hourly, side=None):
    """Издержки круга в % цены: 2*taker + slippage-буфер + funding за max_hold.
    Funding консервативно: по знаку стороны, но получение funding не учитывается (минимум 0)."""
    c = cfg["costs"]
    hold_h = cfg["risk"]["max_hold_minutes"] / 60.0
    fr = funding_hourly
    if side == "short":
        fr = -fr
    elif side is None:
        fr = abs(fr)
    funding = max(0.0, fr) * 100.0 * hold_h
    return 2.0 * c["taker_fee_pct"] * 100.0 + c["slippage_buffer_pct"] * 100.0 + funding


def _clip01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def compute_signal(bars, funding_hourly, cfg):
    s = cfg["strategy"]
    fast, slow = s["ema_fast"], s["ema_slow"]
    mb, atr_n = s["momentum_bars"], s["atr_bars"]
    need = max(s["min_bars_warmup"], slow + 1, mb + 2, atr_n + 1)
    res = {"action": "flat", "score": 0.0, "stop_pct": 0.0, "tp_pct": 0.0,
           "atr_pct": 0.0, "reasons": [], "warmup": False}
    if funding_hourly is None:
        funding_hourly = cfg["costs"].get("funding_hourly_assumed", 0.0)
    if len(bars) < need:
        res["warmup"] = True
        res["reasons"].append("warmup: %d/%d bars" % (len(bars), need))
        return res

    closes = [b["c"] for b in bars]
    last = closes[-1]
    a_abs = atr_abs(bars, atr_n)
    if last <= 0 or a_abs <= 0:
        res["reasons"].append("zero ATR / bad price")
        return res
    atr_pct = a_abs / last * 100.0
    res["atr_pct"] = atr_pct

    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    spread = (ef[-1] - es[-1]) / a_abs
    slope = (es[-1] - es[-1 - mb]) / a_abs
    mom = (last - closes[-1 - mb]) / a_abs
    ext = (last - es[-1]) / a_abs

    erb = int(s.get("er_bars", 12))
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - erb, len(closes)))
    eff = abs(last - closes[-1 - erb]) / path if path > 0 else 0.0  # Kaufman ER: 1 = прямая, 0 = пила

    # стороны: тренд по EMA
    side = "long" if spread > 0 else "short"
    sgn = 1.0 if side == "long" else -1.0
    blockers = []

    if atr_pct < s["min_atr_pct"]:
        blockers.append("ATR%% %.3f < min %.3f (мало волатильности)" % (atr_pct, s["min_atr_pct"]))
    if atr_pct > s["max_atr_pct"]:
        blockers.append("ATR%% %.3f > max %.3f (слишком волатильно)" % (atr_pct, s["max_atr_pct"]))
    if sgn * spread < s.get("min_spread_atr", 0.25):
        blockers.append("боковик: разнос EMA %.2f ATR < %.2f" % (abs(spread), s.get("min_spread_atr", 0.25)))
    if sgn * slope < s.get("min_slope_atr", 0.25):
        blockers.append("боковик: наклон EMA%d %.2f ATR (по направлению) < %.2f" % (slow, sgn * slope, s.get("min_slope_atr", 0.25)))
    if sgn * mom < s.get("min_momentum_atr", 0.6):
        blockers.append("нет моментума: %.2f ATR за %d баров < %.2f" % (sgn * mom, mb, s.get("min_momentum_atr", 0.6)))
    if eff < s.get("min_efficiency", 0.3):
        blockers.append("боковик: efficiency ratio %.2f за %d баров < %.2f" % (eff, erb, s.get("min_efficiency", 0.3)))
    if sgn * (last - ef[-1]) < 0:
        blockers.append("цена по другую сторону EMA%d" % fast)
    if sgn * ext > s.get("max_extension_atr", 3.5):
        blockers.append("перегрет: цена %.1f ATR от EMA%d" % (sgn * ext, slow))

    stop_pct = min(max(s["stop_atr_mult"] * atr_pct, s["stop_pct_min"]), s["stop_pct_max"])
    tp_pct = stop_pct * s["tp_rr"]
    res["stop_pct"], res["tp_pct"] = stop_pct, tp_pct

    cost = round_trip_cost_pct(cfg, funding_hourly, side)
    need_edge = s["min_edge_mult"] * cost
    if tp_pct < need_edge:
        blockers.append("edge: TP %.3f%% < %.1f x издержки %.3f%% = %.3f%%" % (tp_pct, s["min_edge_mult"], cost, need_edge))

    if blockers:
        res["reasons"] = blockers
        return res

    score = (_clip01(sgn * spread / 1.5) + _clip01(sgn * slope / 2.0) + _clip01(sgn * mom / 3.0)) / 3.0
    res["action"] = side
    res["score"] = round(score, 4)
    res["reasons"] = [
        "%s: EMA%d/%d разнос %.2f ATR, наклон %.2f, моментум %.2f ATR, ATR%% %.3f" % (side, fast, slow, abs(spread), sgn * slope, sgn * mom, atr_pct),
        "stop %.2f%% tp %.2f%% (edge ok: tp >= %.2f%%)" % (stop_pct, tp_pct, need_edge),
    ]
    return res
