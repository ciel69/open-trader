"""Динамический размер позиции и плечо: от стопа, уверенности сигнала и режима рынка.

Чистая функция. Жёсткие потолки (risk.leverage, risk.margin_per_trade, резерв) остаются в risk.check_open:
здесь мы только выбираем значения внутри них. Риск на сделку = доля equity; убыток по стопу ~ risk_usdt.
"""
import math

REGIME_MULT = {"trend_aligned": 1.0, "trend_against": 0.4, "range": 0.6, "unknown": 0.8, "storm": 0.0}


def _num(x, default=None):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else default
    except (TypeError, ValueError):
        return default


def regime_multiplier(side, ctx):
    ctx = ctx or {}
    if ctx.get("risk_off"):
        return 0.0, "risk_off"
    reg = ctx.get("regime") or "unknown"
    if reg in ("trend_up", "trend_down"):
        aligned = (reg == "trend_up") == (side == "long")
        key = "trend_aligned" if aligned else "trend_against"
    else:
        key = reg if reg in REGIME_MULT else "unknown"
    return REGIME_MULT[key], key


def dynamic_size(sig, side, equity, avail, contract, cfg, ctx=None):
    """Возвращает {"ok", "leverage", "margin", "notional", "risk_usdt", "reasons", "mult"}.

    equity: текущий капитал; avail: свободные средства сверх резерва и уже запланированных входов.
    """
    r, s = cfg["risk"], cfg.get("sizing", {})
    stop_pct = _num(sig.get("stop_pct"))
    eq, av = _num(equity), _num(avail)
    out = {"ok": False, "leverage": 1, "margin": 0.0, "notional": 0.0, "risk_usdt": 0.0, "reasons": [], "mult": 0.0}
    if not stop_pct or stop_pct <= 0 or not eq or eq <= 0 or av is None or av <= 0:
        out["reasons"].append("sizing_inputs_invalid")
        return out
    rm, rname = regime_multiplier(side, ctx)
    if rm <= 0:
        out["reasons"].append("regime_blocks: %s" % rname)
        return out
    conf = 0.5 + 0.5 * max(0.0, min(1.0, _num(sig.get("score"), 0.0)))
    mult = rm * conf
    risk_usdt = eq * _num(s.get("risk_per_trade_frac"), 0.03) * mult
    notional_target = risk_usdt / (stop_pct / 100.0)

    maint = _num(r.get("maintenance_margin_pct"), 1.0)
    lev_cap = _num(r.get("leverage"), 1.0)
    cmax = _num((contract or {}).get("max_leverage"))
    if cmax:
        lev_cap = min(lev_cap, cmax)
    # запас до ликвидации: стоп должен быть заметно ближе неё (risk.check_open требует stop < liq/2)
    lev_cap = min(lev_cap, 100.0 / (_num(s.get("liq_factor"), 2.2) * stop_pct + maint))
    lev_cap = math.floor(lev_cap)
    if lev_cap < 1:
        out["reasons"].append("leverage_below_1_for_stop")
        return out

    fee = _num((contract or {}).get("taker_fee_pct"), _num((cfg.get("costs") or {}).get("taker_fee_pct"), 0.0009))
    margin_cap = min(_num(r.get("margin_per_trade"), 0.0), av / (1.0 + lev_cap * fee))
    if margin_cap <= 0:
        out["reasons"].append("no_margin_available")
        return out
    lev = int(min(lev_cap, max(1, math.ceil(notional_target / margin_cap))))
    margin = min(margin_cap, notional_target / lev)
    margin = math.floor(margin * 100.0) / 100.0
    min_size = _num((contract or {}).get("min_size_quote"), 0.0)
    if margin < min_size or margin <= 0:
        out["reasons"].append("margin %.2f < min_size_quote %.2f" % (margin, min_size))
        return out
    out.update({"ok": True, "leverage": lev, "margin": margin, "notional": round(margin * lev, 2),
                "risk_usdt": round(margin * lev * stop_pct / 100.0, 3), "mult": round(mult, 3)})
    return out
