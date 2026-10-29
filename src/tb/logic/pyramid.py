"""Пирамидинг: добор в прибыльную позицию (SPEC_V2 п. 1.7; 06 п. 6.9). Владелец: WP3.

Выключен по умолчанию (`exec_limits.pyramid.enabled=false`, решение владельца Q1). Добор в убыток невозможен: условия P1-P9 из SPEC_V2 п. 1.7.
Чистая логика: Gate исполнителя вызывает `check_add` ПОСЛЕ `guard.evaluate_add` (его ответ приходит аргументом `guard_add`).

Единицы: комиссии и проскальзывание в ПРОЦЕНТАХ (taker 0.09 = 0.09%); `f = (fee_pct + slip_pct) / 100`. Время в секундах epoch.
"""
import math

from tb.logic import conds as C
from tb.logic.rules import _f, _pos, _side, is_tighter

# коды отказа check_add (P1..P9 из SPEC_V2 п. 1.7); первые десять заданы WP0, добавлены WP3: NOT_MANAGING, STALE, PENDING, STOP_SIDE, NO_DATA
ADD_CODES = ("ADD_DISABLED", "NOT_PROFIT", "STOP_NOT_BE", "MAX_ADDS", "MIN_GAP", "RISK_CAP", "GUARD", "HOLD", "KILL", "MIN_SIZE",
             "NOT_MANAGING", "STALE", "PENDING", "STOP_SIDE", "NO_DATA")
DEFAULT_FEE_PCT = 0.09      # taker Bybit, одна сторона
DEFAULT_SLIP_PCT = 0.02
_EPS = 1e-9


def _fplus(fee_pct, slip_pct):
    return (float(fee_pct) + float(slip_pct)) / 100.0


def worst_case_pnl(rt, stop, fee_pct, slip_pct, side):
    """-> W: худший PnL плана при срабатывании стопа `stop` (SPEC_V2 п. 1.7):
    W(S) = realized_net + qty * s * (S - avg) - f * S * qty,  realized_net = realized.pnl - realized.fees (все уплаченные комиссии),
    qty/avg берутся из rt.pos (текущий остаток; сумма по частям эквивалентна). Без позиции -> realized_net.

    side: "long"/"short" или +1/-1.

    >>> rt = {"pos": {"qty": 2.0, "avg": 100.0}, "realized": {"pnl": 0.0, "fees": 0.2}}
    >>> round(worst_case_pnl(rt, 99.0, 0.09, 0.0, "long"), 6)
    -2.3782
    """
    s = side if side in (1, -1) else (1 if side == "long" else -1)
    real = (rt or {}).get("realized") or {}
    net = (_f(real.get("pnl")) or 0.0) - (_f(real.get("fees")) or 0.0)
    p = _pos(rt)
    if not p:
        return net
    q, avg, S = _f(p.get("qty")), _f(p.get("avg")), _f(stop)
    f = _fplus(fee_pct, slip_pct)
    return net + q * s * (S - avg) - f * S * q


def _walk_min_r(cond):
    """Наибольший порог pnl_r gte/gt среди конъюнктов условия (leaf или all)."""
    if not isinstance(cond, dict):
        return 0.0
    best = 0.0
    if cond.get("kind") == "pnl_r":
        for k in ("gte", "gt"):
            v = _f(cond.get(k))
            if v is not None:
                best = max(best, v)
    for ch in cond.get("all") or []:
        best = max(best, _walk_min_r(ch))
    return best


def _floor_step(q, step):
    s = _f(step)
    if not s or s <= 0:
        return q
    return round(math.floor(q / s + 1e-9) * s, 12)


def check_add(plan, rt, view, limits, add_rule, price, guard_add):
    """-> {"ok":bool,"codes":[...],"qty_max":float,"risk_add":float}.

    Проверки P1-P9 (SPEC_V2 п. 1.7), все коды собираются (кроме ADD_DISABLED: он возвращается один):
      ADD_DISABLED (pyramid.enabled=false), NOT_MANAGING (план не в managing), HOLD (hold плана), KILL (limits["kill"] или guard_add["kill"]),
      MAX_ADDS (adds.n >= max_adds), NOT_PROFIT (pnl_r по свежей цене < max(X правила, pyramid.min_r)), STOP_NOT_BE (текущий стоп хуже
      безубытка: s*(S-avg) < 2*f*avg), STOP_SIDE (цена добора не по ту сторону стопа), MIN_GAP (с прошлого добора < min_gap_s),
      PENDING (у плана есть незавершённые действия), STALE (цена старше connectivity.public_price_stale_s), GUARD (guard_add нет или ok=false),
      RISK_CAP (после добора W < -R0: лимит нулевой), MIN_SIZE (размер ниже min_qty/шага).
    Размер: q = floor_step(min(A, B, guard.qty_max)), A = risk_frac*R0/loss_unit, B = (W(S)+R0)/loss_unit,
    loss_unit = |px-S| + f*(px+S). Стоп не двигается: добор вешается под тот же S. `risk_add` = q*loss_unit (в деньгах).
    R0: guard_add["plan_risk0"] | rt.pos.risk0 | расчёт по первому входу q0*(|e0-S0| + f*(e0+S0)).
    Необязательные ключи limits: `costs` {"fee_pct","slip_pct"}, `qty_step`, `min_qty`, `kill`; те же `qty_step`/`min_qty` берутся из guard_add.

    >>> rt = {"status": "managing", "pos": {"qty": 1.0, "avg": 100.0, "initial_stop": 99.0, "cur_stop": 100.5, "r_unit": 1.0,
    ...       "risk0": 1.2}, "realized": {"pnl": 0.0, "fees": 0.09}, "adds": {"n": 0}}
    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("S", 102.0, 101.9, 102.1, 102.0, 10.0, None, 10.0)
    >>> lim = {"pyramid": {"enabled": True, "min_r": 1.0, "max_adds": 1, "max_add_risk_frac": 1.0, "min_gap_s": 60}}
    >>> r = check_add({"side": "long"}, rt, v, lim, {"do": {"risk_frac": 0.5}}, 102.0, {"ok": True, "qty_max": 5.0})
    >>> r["ok"], r["codes"]
    (True, [])
    """
    out = {"ok": False, "codes": [], "qty_max": 0.0, "risk_add": 0.0}
    codes = out["codes"]
    lim = limits or {}
    pyr = lim.get("pyramid") or {}
    if not pyr.get("enabled"):
        codes.append("ADD_DISABLED")
        return out
    s, pos = _side(plan), _pos(rt)
    if s is None or not pos or (rt or {}).get("status") != "managing":
        codes.append("NOT_MANAGING")
        return out
    if (rt or {}).get("hold"):
        codes.append("HOLD")
    if lim.get("kill") or (guard_add or {}).get("kill"):
        codes.append("KILL")
    if int(((rt or {}).get("adds") or {}).get("n", 0)) >= int(pyr.get("max_adds", 1)):
        codes.append("MAX_ADDS")
    costs = lim.get("costs") or {}
    fee_pct = costs.get("fee_pct", DEFAULT_FEE_PCT)
    slip_pct = costs.get("slip_pct", DEFAULT_SLIP_PCT)
    f = _fplus(fee_pct, slip_pct)
    avg = _f(pos.get("avg"))
    S = _f(pos.get("cur_stop"))
    px = _f(price)
    last = C.last_price(view)
    stale = float((lim.get("connectivity") or {}).get("public_price_stale_s", C.DEFAULT_STALE_S))
    if last is None or px is None or px <= 0 or view.price_age() > stale:
        codes.append("STALE")
    if S is None or S <= 0:
        codes.append("STOP_NOT_BE")
    # P2: прибыль не менее X R по свежей цене
    need = max(_walk_min_r((add_rule or {}).get("when")), _f(pyr.get("min_r")) or 0.0)
    r = C.r_multiple(pos, last, s) if last is not None else None
    if r is None or r < need - _EPS:
        codes.append("NOT_PROFIT")
    # P3: стоп в безубытке или лучше (цена входа плюс комиссии круга)
    if S is not None and S > 0 and s * (S - avg) < 2.0 * f * avg - _EPS:
        codes.append("STOP_NOT_BE")
    # P6
    ra = (rt or {}).get("adds") or {}
    gap = _f(pyr.get("min_gap_s")) or 0.0
    if ra.get("last_ts") is not None and (view.now or 0) - ra["last_ts"] < gap:
        codes.append("MIN_GAP")
    if ((rt or {}).get("actions") or {}).get("pending"):
        codes.append("PENDING")
    # P5
    if not guard_add or not guard_add.get("ok"):
        codes.append("GUARD")
    # P4: размер
    if S is not None and S > 0 and px is not None and px > 0:
        if s * (px - S) <= 0:
            codes.append("STOP_SIDE")
        else:
            loss_unit = abs(px - S) + f * (px + S)
            r0 = _f((guard_add or {}).get("plan_risk0")) or _f(pos.get("risk0"))
            if r0 is None or r0 <= 0:
                ents = [e for e in (pos.get("entries") or []) if e.get("kind", "entry") == "entry"]
                e0 = ents[0] if ents else None
                s0 = _f(pos.get("initial_stop"))
                if e0 and s0:
                    r0 = _f(e0.get("qty")) * (abs(_f(e0.get("px")) - s0) + f * (_f(e0.get("px")) + s0))
            if r0 is None or r0 <= 0:
                codes.append("NO_DATA")
            else:
                rf = min(_f((add_rule or {}).get("do", {}).get("risk_frac")) or 0.0, 1.0, _f(pyr.get("max_add_risk_frac")) or 1.0)
                A = rf * r0 / loss_unit
                W = worst_case_pnl(rt, S, fee_pct, slip_pct, s)
                B = (W + r0) / loss_unit
                if B <= _EPS or rf <= 0:
                    codes.append("RISK_CAP")
                else:
                    q = min(A, B)
                    gq = _f((guard_add or {}).get("qty_max"))
                    if gq is not None:
                        q = min(q, gq)
                    step = lim.get("qty_step") or (guard_add or {}).get("qty_step")
                    q = _floor_step(q, step)
                    mq = _f(lim.get("min_qty") or (guard_add or {}).get("min_qty")) or 0.0
                    if q <= 0 or q < mq - _EPS:
                        codes.append("MIN_SIZE")
                    else:
                        out["qty_max"] = q
                        out["risk_add"] = q * loss_unit
    out["codes"] = [c for i, c in enumerate(codes) if c not in codes[:i]]
    out["ok"] = not out["codes"]
    if not out["ok"]:
        out["qty_max"] = out["risk_add"] = 0.0
    return out
