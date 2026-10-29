"""Риск-менеджер (Agent B). Принцип: любая неопределённость = не открывать.

snapshot (собирает tick.py):
  now: epoch входного тика; wall_now: реальные часы (None -> сравнение не делается, нужен price_ts)
  prices {sym: float}, price_ts {sym: epoch} (по умолчанию = now), funding_hourly,
  balance (свободный USDT, БЕЗ маржи позиций), positions (сырые элементы perps_list_positions),
  contracts {sym: {...}}, planned [{symbol, margin}] (открытия, решённые в этом же тике).
equity = balance + sum(margin_quote + unrealized_pnl); pnl = margin*leverage*(price/entry-1)*dir.
"""
import math

from tb.legacy_engine.state import parse_ts

CLOSED_STATUSES = {"closed", "liquidated", "cancelled", "canceled", "settled"}


def fnum(x, default=None):
    try:
        if x is None or isinstance(x, bool):
            return default
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _pos_price(v):
    f = fnum(v)
    return f if (f is not None and f > 0) else None


def parse_position(p):
    """Сырая позиция -> нормализованный dict либо None (если не dict)."""
    if not isinstance(p, dict):
        return None
    ac = p.get("auto_close")
    ac = ac if isinstance(ac, dict) else {}
    tp = ac.get("take_profit")
    sl = ac.get("stop_loss")
    side = str(p.get("side") or "").strip().lower()
    side = {"buy": "long", "sell": "short"}.get(side, side)
    return {
        "contract_id": p.get("contract_id"),
        "cid": str(p.get("contract_id")),
        "side": side if side in ("long", "short") else None,
        "status": str(p.get("status") or "").strip().lower(),
        "leverage": fnum(p.get("leverage")),
        "margin": fnum(p.get("margin_quote")),
        "entry": _pos_price(p.get("avg_entry_price")),
        "liq": _pos_price(p.get("liquidation_price")),
        "stop": _pos_price(sl.get("trigger_price_quote")) if isinstance(sl, dict) else None,
        "tp": _pos_price(tp.get("trigger_price_quote")) if isinstance(tp, dict) else None,
        "created": parse_ts(p.get("created_at")),
        "raw": p,
    }


def open_positions(snapshot):
    out = []
    for p in snapshot.get("positions") or []:
        pp = parse_position(p)
        if pp is not None and pp["status"] not in CLOSED_STATUSES:
            out.append(pp)
    return out


def symbol_for(snapshot, cid):
    for sym, c in (snapshot.get("contracts") or {}).items():
        if isinstance(c, dict) and str(c.get("contract_id")) == str(cid):
            return sym
    return None


def position_pnl(pos, price):
    if not pos or pos["side"] is None or price is None:
        return None
    m, lev, e = pos["margin"], pos["leverage"], pos["entry"]
    if m is None or lev is None or e is None or e <= 0 or price <= 0:
        return None
    d = 1.0 if pos["side"] == "long" else -1.0
    return m * lev * (price / e - 1.0) * d


def compute_equity(snapshot):
    """-> (equity|None, info). uncertain=True, если не всё вычислимо."""
    info = {"unrealized": 0.0, "uncertain": False}
    bal = fnum(snapshot.get("balance"))
    prices = snapshot.get("prices") or {}
    for pos in open_positions(snapshot):
        sym = symbol_for(snapshot, pos["contract_id"])
        pnl = position_pnl(pos, _pos_price(prices.get(sym)) if sym else None)
        if pnl is None:
            info["uncertain"] = True
            pnl = 0.0
        info["unrealized"] += pnl
        if pos["margin"] is None:
            info["uncertain"] = True
        elif bal is not None:
            bal += pos["margin"]
    if bal is None:
        return None, info
    return bal + info["unrealized"], info


def price_status(snapshot, sym, cfg):
    """-> (price|None, stale_reason|None)."""
    price = _pos_price((snapshot.get("prices") or {}).get(sym))
    if price is None:
        return None, "no_valid_price"
    limit = cfg["risk"]["stale_seconds"]
    ts = fnum((snapshot.get("price_ts") or {}).get(sym))
    if ts is None:
        ts = fnum(snapshot.get("now"))
    if ts is None:
        return price, "no_price_timestamp"
    wall = fnum(snapshot.get("wall_now"))
    ref = wall if wall is not None else fnum(snapshot.get("now"))
    if ref is None:
        return price, "no_clock"
    age = ref - ts
    if abs(age) > limit:
        return price, "stale_price(age=%.0fs>%ss)" % (age, limit)
    return price, None


def compute_levels(side, ref_price, stop_pct, tp_pct):
    """Лонг: стоп ниже, TP выше. Шорт: стоп выше, TP ниже. Проценты цены."""
    if side == "long":
        stop = ref_price * (1.0 - stop_pct / 100.0)
        tp = ref_price * (1.0 + tp_pct / 100.0)
    elif side == "short":
        stop = ref_price * (1.0 + stop_pct / 100.0)
        tp = ref_price * (1.0 - tp_pct / 100.0)
    else:
        raise ValueError("bad side: %r" % (side,))
    return round(stop, 6), round(tp, 6)


def est_liq_distance_pct(leverage, cfg):
    """Грубая оценка дистанции до ликвидации: 100/плечо минус поддерживающая маржа."""
    maint = fnum(cfg["risk"].get("maintenance_margin_pct"), 1.0)
    return 100.0 / leverage - maint


def kill_switch(state, snapshot, cfg):
    if state.get("halted"):
        return {"halt": True, "reason": state.get("halt_reason") or "halted"}
    limit = cfg["risk"]["daily_loss_limit_usdt"]
    eq, info = compute_equity(snapshot)
    dse = fnum(state.get("day_start_equity"))
    realized = fnum(state.get("realized_pnl_today"), 0.0)
    if eq is not None and dse is not None:
        loss = dse - eq
        basis = "equity %.4f vs day_start %.4f" % (eq, dse)
    else:
        loss = max(-(realized + info["unrealized"]), -info["unrealized"])
        basis = "no equity baseline; realized %.4f unrealized %.4f" % (realized, info["unrealized"])
    if loss >= limit:
        return {"halt": True, "reason": "daily_loss_limit: loss %.4f >= %.4f (%s)" % (loss, limit, basis)}
    return {"halt": False, "reason": ""}


def flatten_decisions(snapshot, why, code):
    out = []
    for pos in open_positions(snapshot):
        sym = symbol_for(snapshot, pos["contract_id"]) or str(pos["contract_id"])
        out.append({"op": "close", "symbol": sym, "contract_id": pos["contract_id"],
                    "why": list(why), "code": code})
    return out


def check_open(state, snapshot, proposal, cfg):
    r = cfg["risk"]
    reasons = []
    sym = proposal.get("symbol")
    side = str(proposal.get("side") or "").lower()
    cid = proposal.get("contract_id")
    now = fnum(snapshot.get("now"))

    if state.get("session_active") is not True:
        reasons.append("session_inactive")
    ks = kill_switch(state, snapshot, cfg)
    if ks["halt"]:
        reasons.append("halted: %s" % ks["reason"])
    if side not in ("long", "short"):
        reasons.append("bad_side")
    if now is None:
        reasons.append("no_now")

    price, stale = price_status(snapshot, sym, cfg)
    if stale:
        reasons.append(stale)
    ref = fnum(proposal.get("ref_price"))
    if ref is None or ref <= 0:
        reasons.append("bad_ref_price")
    elif price is not None and abs(ref / price - 1.0) > 0.01:
        reasons.append("ref_price_mismatch")

    contract = (snapshot.get("contracts") or {}).get(sym)
    if not isinstance(contract, dict):
        reasons.append("no_contract_info")
        contract = {}
    elif cid is None or str(contract.get("contract_id")) != str(cid):
        reasons.append("contract_id_mismatch")

    # плечо и маржа
    lev = fnum(proposal.get("leverage"), fnum(r.get("leverage")))
    lev = min(lev if lev else 0.0, fnum(r.get("leverage"), 0.0))
    max_lev = fnum(contract.get("max_leverage"))
    if max_lev is not None and max_lev > 0 and lev > max_lev:
        lev = max_lev
    if lev < 1:
        reasons.append("bad_leverage")
        lev = 1.0
    margin = fnum(proposal.get("margin"), r["margin_per_trade"])
    margin = min(margin, r["margin_per_trade"])
    if margin <= 0:
        reasons.append("bad_margin")
    min_size = fnum(contract.get("min_size_quote"))
    if min_size is None:
        reasons.append("min_size_quote_unknown")
    elif margin < min_size:
        reasons.append("margin %.4f < min_size_quote %.4f" % (margin, min_size))

    # резерв
    fee_pct = fnum(contract.get("taker_fee_pct"), fnum((cfg.get("costs") or {}).get("taker_fee_pct"), 0.0009))
    fee = margin * lev * fee_pct
    bal = fnum(snapshot.get("balance"))
    planned = [x for x in (snapshot.get("planned") or []) if isinstance(x, dict)]
    planned_margin = sum(fnum(x.get("margin"), 0.0) for x in planned)
    if bal is None:
        reasons.append("balance_unknown")
    elif bal - planned_margin - margin - fee < r["min_free_reserve"]:
        reasons.append("reserve: free after open %.4f < min_free_reserve %.4f"
                       % (bal - planned_margin - margin - fee, r["min_free_reserve"]))

    # позиции
    opens = open_positions(snapshot)
    if any(p["cid"] == str(cid) or (sym and symbol_for(snapshot, p["contract_id"]) == sym) for p in opens):
        reasons.append("position_exists_on_contract")
    if len(opens) + len(planned) >= r["max_open_positions"]:
        reasons.append("max_open_positions")
    if any(x.get("symbol") == sym for x in planned):
        reasons.append("already_planned")

    # дневные лимиты
    if int(state.get("trades_today") or 0) >= r["max_trades_per_day"]:
        reasons.append("max_trades_per_day")
    last_stop = parse_ts((state.get("last_stop_ts") or {}).get(sym))
    cd = r["cooldown_after_stop_minutes"] * 60.0
    if last_stop is not None and now is not None and now - last_stop < cd:
        reasons.append("cooldown_after_stop(%.0fs left)" % (cd - (now - last_stop)))
    guard = 3.0 * fnum(cfg.get("tick_seconds"), 60.0)
    last_close = parse_ts((state.get("last_close_ts") or {}).get(sym))
    if last_close is not None and now is not None and now - last_close < guard:
        reasons.append("recently_closed")
    po = (state.get("pending_open") or {}).get(sym)
    if isinstance(po, dict):
        pts = parse_ts(po.get("ts"))
        if pts is None or now is None or now - pts < 2.0 * r["stale_seconds"]:
            reasons.append("pending_open")

    eq, info = compute_equity(snapshot)
    if eq is None:
        reasons.append("equity_unknown")
    elif info["uncertain"]:
        reasons.append("equity_uncertain")
    if fnum(state.get("day_start_equity")) is None:
        reasons.append("day_start_equity_unknown")

    # ликвидация и уровни
    liq = est_liq_distance_pct(lev, cfg)
    if liq < r["min_liq_distance_pct"]:
        reasons.append("liq_distance %.1f%% < %.1f%%" % (liq, r["min_liq_distance_pct"]))
    stop_pct = fnum(proposal.get("stop_pct"))
    tp_pct = fnum(proposal.get("tp_pct"))
    if stop_pct is None or stop_pct <= 0 or tp_pct is None or tp_pct <= 0:
        reasons.append("bad_stop_tp_pct")
    elif stop_pct >= liq * 0.5:
        reasons.append("stop_too_close_to_liquidation")

    return {"ok": not reasons, "reasons": reasons, "margin": margin, "leverage": int(lev) if float(lev).is_integer() else lev}


def manage_positions(state, snapshot, cfg):
    r = cfg["risk"]
    st_cfg = cfg.get("strategy") or {}
    now = fnum(snapshot.get("now"))
    prices_cache = {}
    decisions = []
    tracked_all = state.get("positions") or {}
    for pos in open_positions(snapshot):
        cid = pos["cid"]
        sym = symbol_for(snapshot, pos["contract_id"])
        label = sym or cid
        tr = tracked_all.get(cid) if isinstance(tracked_all.get(cid), dict) else {}
        if sym not in prices_cache:
            price, stale = price_status(snapshot, sym, cfg) if sym else (None, "unknown_contract")
            prices_cache[sym] = None if stale else price
        price = prices_cache[sym]

        def close(code, why):
            decisions.append({"op": "close", "symbol": label, "contract_id": pos["contract_id"],
                              "why": [why], "code": code})

        # max_hold
        opened = pos["created"] if pos["created"] is not None else parse_ts(tr.get("first_seen"))
        if opened is not None and now is not None and (now - opened) / 60.0 >= r["max_hold_minutes"]:
            close("max_hold", "max_hold %.0f min reached" % ((now - opened) / 60.0))
            continue
        if pos["side"] is None:
            continue  # непонятная позиция: не трогаем, но она считается открытой
        stop = pos["stop"] if pos["stop"] is not None else fnum(tr.get("stop"))
        tp = pos["tp"] if pos["tp"] is not None else fnum(tr.get("tp"))
        long_ = pos["side"] == "long"
        if price is not None:
            if stop and ((long_ and price <= stop) or (not long_ and price >= stop)):
                close("stop", "price %.6f beyond stop %.6f (auto_close did not fire)" % (price, stop))
                continue
            if tp and ((long_ and price >= tp) or (not long_ and price <= tp)):
                close("tp", "price %.6f beyond tp %.6f" % (price, tp))
                continue
            if pos["liq"]:
                d = abs(price - pos["liq"]) / price * 100.0
                if d < r["min_liq_distance_pct"] / 3.0:
                    close("liq_risk", "liquidation only %.2f%% away" % d)
                    continue
        if pos["stop"] is None:
            ref = pos["entry"] or price
            if ref is None:
                continue
            if stop is None or (price is not None and ((long_ and stop >= price) or (not long_ and stop <= price))):
                sp = st_cfg.get("stop_pct_max", 2.0)
                new_stop, new_tp = compute_levels(pos["side"], ref, sp, sp * st_cfg.get("tp_rr", 2.0))
                if tp:
                    new_tp = tp
            else:
                new_stop = stop
                new_tp = tp if tp else compute_levels(pos["side"], ref, abs(ref - stop) / ref * 100.0,
                                                      abs(ref - stop) / ref * 100.0 * st_cfg.get("tp_rr", 2.0))[1]
            if price is not None and ((long_ and new_stop >= price) or (not long_ and new_stop <= price)):
                close("no_stop_beyond", "no stop set and price already beyond default stop %.6f" % new_stop)
                continue
            decisions.append({"op": "set_auto_close", "symbol": label, "contract_id": pos["contract_id"],
                              "stop_price": new_stop, "tp_price": new_tp,
                              "why": ["position has no stop_loss auto_close"], "code": "set_stop"})
    return decisions
