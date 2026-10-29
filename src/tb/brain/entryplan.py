"""План tb.plan/1 из ответа entry/1 (роль entry_scout, только paper). Чистый модуль: без файлов и сети.

Размер, плечо и маржу модель не задаёт и здесь не появляются: их считает guard исполнителя по риску плана. Режим плана ВСЕГДА paper (жёстко, не из ответа модели).
Подтверждение входа: confirm=candle -> условие candle_close 5м за уровнем; confirm=none -> price_in_zone по узкой зоне, risk.confirm_waiver и size_mult <= 0.5 (проверено в validate).
"""
MODE = "paper"
EXPERIMENT = "entry-scout-v1"
MAX_HOLD_MIN = {"scalp": 60, "intraday": 240, "swing": 1440, "position": 4320}
FLATTEN_TYPES = ("scalp", "intraday")


def _fracs(n):
    base = round(1.0 / n, 6)
    out = [base] * (n - 1)
    out.append(round(1.0 - sum(out), 6))
    return out


def build(obj, ctx):
    """obj: проверенный ответ entry/1 (decision=propose); ctx: контекст раннера (session_type, px, event_seq). -> словарь плана tb.plan/1 (без полей исполнителя).
    Горизонт плана (session_type плана: стоп/RR/плечо у guard, срок удержания, закрытие в конце дня) = obj["horizon"], выбор модели; проверен в validate
    (в сессии-дне любой из risk_profile.session_types, в сессии одного горизонта только её тип)."""
    side, lvl, stop, tps = obj["side"], float(obj["level"]), float(obj["stop"]), [float(x) for x in obj["tp"]]
    ttl = int(obj["ttl_min"])
    stype = obj.get("horizon") or ctx.get("session_type")
    px = ctx.get("px") or lvl
    if obj["confirm"] == "candle":
        when = {"kind": "candle_close", "tf": "5m", "dir": "above" if side == "long" else "below", "level": lvl}
    else:
        z = obj["zone"]
        when = {"kind": "price_in_zone", "low": float(z["low"]), "high": float(z["high"])}
    valid = {"ttl_min": ttl}
    if side == "long":
        valid.update(cancel_below=stop, cancel_above=tps[0])
    else:
        valid.update(cancel_above=stop, cancel_below=tps[0])
    tags = {"experiment": EXPERIMENT, "variant": "A", "hypothesis": obj["hypothesis"]}
    if obj["setup"] != "none":
        tags["setup"] = obj["setup"]
    if obj.get("tags"):
        tags["extra"] = list(obj["tags"])
    fr = _fracs(len(tps))
    risk = {"score": round(float(obj["conf"]), 2), "size_mult": float(obj.get("size_mult") or 1.0)}
    if obj["confirm"] == "none":
        risk["confirm_waiver"] = True
    plan = {"schema": "tb.plan/1", "symbol": obj["sym"], "side": side, "mode": MODE, "session_type": stype,
            "source": {"kind": "scanner", "event_seq": int(ctx.get("event_seq") or 0)},
            "thesis": obj["thesis"], "tags": tags,
            "entry": {"when": when, "how": {"type": "market"}, "ref_price": float(px), "valid": valid},
            "stop": {"price": stop, "trigger_by": "last"},
            "targets": [{"id": "t%d" % (i + 1), "price": p, "frac": fr[i], "exec": "resting"} for i, p in enumerate(tps)],
            "risk": risk,
            "lifecycle": {"ttl_min": ttl, "max_hold_min": MAX_HOLD_MIN.get(stype, 240), "flatten_at_session_end": stype in FLATTEN_TYPES}}
    assert plan["mode"] == MODE
    return plan
