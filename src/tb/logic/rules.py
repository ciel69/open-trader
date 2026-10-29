"""Правила входа, жизненного цикла и сопровождения плана (01 п. 1.5; 06 п. 6.9). Владелец: WP3.

Action (словарь): {"type":"fire_entry|cancel_entry|transition|move_stop|partial_close|close|move_target|add|notify","rule":str|None,
"why":"RULE:id|LIFECYCLE:TTL|...", ...поля}. move_stop: {"to":float,"mode":"breakeven|price|lock_r|trail"} (to уже абсолютная цена, строго
строже cur_stop, иначе действие НЕ создаётся); partial_close: {"frac_initial":float}|{"frac_remaining":float}; close: {"frac","reason"};
move_target: {"target","price"}; add: {"risk_frac"}; notify: {"code"}; transition: {"to":"expired|invalidated|cancelled","reason"}.

Соглашения (чистые функции, без часов и файлов):
  * `now`, `view.now`, `view.ts`, `rt.ts.*` в секундах epoch; `*_ms` (в ctx условий и у свечей) в миллисекундах.
  * `plan` = spec плана; `rt` = runtime плана (06 п. 6.7); `limits` = `exec_limits` (06 п. 6.8). Необязательные ключи, которые исполнитель
    может подложить в КОПИЮ limits (файл не меняется): `tick` (шаг цены инструмента), `qty_step`, `min_qty`, `session_end_ts`
    (epoch окончания сессии), `session_end_lead_s` (за сколько секунд до конца сбрасывать, умолч. 30), `round_trip_fee_bps`.
  * `rt_patch` = частичная структура для глубокого слияния в rt (словари сливаются рекурсивно, остальное заменяется).
  * Стоп двигается ТОЛЬКО в сторону уменьшения риска: итоговый фильтр `enforce_tighter` стоит последним в evaluate_manage и
    отбрасывает любой move_stop, не строго более жёсткий, чем текущий стоп (защита от ошибок в кандидатах).
"""
import math

from tb.logic import conds as C
from tb.logic.candles import tf_min

ACTION_TYPES = ("fire_entry", "cancel_entry", "transition", "move_stop", "partial_close", "close", "move_target", "add", "notify")
MOVE_STOP_MODES = ("breakeven", "price", "lock_r", "trail")
ENTRY_BLOCKS = ("CHASE", "SPREAD", "STALE", "GUARD", "COOLDOWN", "MIN_SIZE", "HOLD")   # HOLD добавлен WP3 (hold плана запрещает вход)
SKIP_CODES = ("NOT_TIGHTER", "TOO_CLOSE", "WRONG_SIDE", "ADD_DISABLED", "MAX_ADDS", "TARGET_FARTHER_NOT_RISKFREE", "NO_DATA")
DEFAULT_BE_ROUND_TRIP_BPS = 18.0     # 2 x taker 0.09%, если фактических комиссий нет
BE_EXTRA_BPS = 2.0
TRAIL_CANDLE_MODES = ("candle_extreme", "atr")
_EPS = 1e-12


# ---------------------------------------------------------------- мелочи
def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _lim(limits, section, key, default):
    v = ((limits or {}).get(section) or {}).get(key)
    return default if v is None else v


def _side(plan):
    s = (plan or {}).get("side")
    return 1 if s == "long" else (-1 if s == "short" else None)


def is_tighter(side, new, cur):
    """True, если стоп `new` строго строже `cur` (long: выше, short: ниже). Нечисловое = False.

    >>> is_tighter(1, 101.0, 100.0), is_tighter(1, 100.0, 100.0), is_tighter(-1, 99.0, 100.0), is_tighter(-1, 101.0, 100.0)
    (True, False, True, False)
    """
    n, c = _f(new), _f(cur)
    if n is None or c is None or n <= 0 or side not in (1, -1):
        return False
    return side * (n - c) > 0


def enforce_tighter(actions, side, cur_stop):
    """-> [Action]: действия без move_stop, не строже cur_stop (и без нечисловых/неположительных цен). Остальные действия не трогает.

    >>> enforce_tighter([{"type": "move_stop", "to": 99.0}, {"type": "close"}], 1, 100.0)
    [{'type': 'close'}]
    """
    out = []
    for a in actions:
        if a.get("type") == "move_stop" and not is_tighter(side, a.get("to"), cur_stop):
            continue
        out.append(a)
    return out


def _round_stop(px, tick, s):
    """Округление стопа к тику в сторону ослабления (long вниз, short вверх): никогда не ближе к цене, чем посчитано."""
    t = _f(tick)
    if not t or t <= 0:
        return px
    q = px / t
    k = math.floor(q + 1e-9) if s == 1 else math.ceil(q - 1e-9)
    return round(k * t, 10)


def _pos(rt):
    p = (rt or {}).get("pos") or {}
    q = _f(p.get("qty"))
    return p if q is not None and q > 0 else None


def build_ctx(plan, rt, limits=None, stateless=False):
    """-> ctx для conds.eval_cond из spec/runtime. Секунды ts переводятся в миллисекунды.

    >>> build_ctx({"side": "long"}, {"ts": {"armed": 100.0}})["armed_ms"]
    100000.0
    """
    ts = (rt or {}).get("ts") or {}
    a, e = _f(ts.get("armed")), _f(ts.get("entered"))
    ctx = {"armed_ms": a * 1000.0 if a else None, "entered_ms": e * 1000.0 if e else None,
           "pos": _pos(rt), "targets": (rt or {}).get("targets") or {}, "side": (plan or {}).get("side"),
           "stale_s": float(_lim(limits, "connectivity", "public_price_stale_s", C.DEFAULT_STALE_S))}
    if stateless:
        ctx["stateless"] = True
    return ctx


def r_now(rt, price):
    """-> float | None: нереализованный результат в R = side*(price-avg)/r_unit по rt.pos (сторона из начального стопа). Нет позиции/данных = None.

    >>> r_now({"pos": {"qty": 1, "avg": 100.0, "initial_stop": 99.0, "r_unit": 1.0}}, 101.5)
    1.5
    >>> r_now({"pos": {"qty": 1, "avg": 100.0, "initial_stop": 101.0, "r_unit": 1.0}}, 99.0)
    1.0
    """
    p = _pos(rt)
    return C.r_multiple(p, price) if p else None


def _first_level(cond):
    """Первый встретившийся уровень в дереве условия (опорная цена для chase)."""
    if not isinstance(cond, dict):
        return None
    if cond.get("level") is not None:
        return _f(cond["level"])
    for k in ("all", "any", "steps"):
        for ch in cond.get(k) or []:
            v = _first_level(ch)
            if v is not None:
                return v
    return _first_level(cond["not"]) if "not" in cond else None


# ---------------------------------------------------------------- вход
def evaluate_entry(plan, rt, view, now, limits):
    """-> {"fire":bool,"blocked":None|"CHASE|SPREAD|STALE|HOLD",...,"rt_patch":{..},"cond":{...}}.

    Вычисляет `entry.when` (память seq/already в rt.entry.cond_state), затем проверки свежести цены, спреда и погони.
    GUARD, COOLDOWN, MIN_SIZE проверяет Gate исполнителя (здесь их нет). `fire=True` только если условие выполнено И блокировок нет.
    Нет `when` = условие выполнено сразу. До `lifecycle.not_before_ts` вход не стреляет (blocked=None, в cond причина).

    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("SOL", 10.0, 9.99, 10.01, 10.0, 5.0, None, 5.0)
    >>> p = {"side": "long", "entry": {"how": {"type": "market"}, "ref_price": 10.0}}
    >>> evaluate_entry(p, {"status": "armed"}, v, 5.0, {})["fire"]
    True
    """
    entry = plan.get("entry") or {}
    ent = (rt or {}).get("entry") or {}
    how = (entry.get("how") or {}).get("type", "market")
    when = entry.get("when")
    res = {"fire": False, "blocked": None, "rt_patch": {}, "cond": {"met": False, "text": "", "dist_pct": None, "eta_s": None}}
    if (rt or {}).get("status") not in (None, "armed"):
        res["cond"]["text"] = "план не в статусе armed"
        return res
    if ent.get("state") not in (None, "waiting", "blocked"):
        res["cond"]["text"] = "вход уже в работе (%s)" % ent.get("state")
        return res
    ctx = build_ctx(plan, rt, limits)
    st = ent.get("cond_state") or {}
    if when:
        met, st2 = C.eval_cond(when, view, st, ctx)
        res["cond"] = C.describe(when, view, st, ctx)
    else:
        met, st2 = True, st
        res["cond"] = {"met": True, "text": "без условия", "dist_pct": 0.0, "eta_s": None}
    if st2 != st:
        res["rt_patch"] = {"entry": {"cond_state": st2}}
    nb = _f((plan.get("lifecycle") or {}).get("not_before_ts"))
    if nb is not None and now < nb:
        res["cond"]["met"] = False
        res["cond"]["text"] += " [ждём not_before_ts]"
        return res
    if not met:
        return res
    px = C.last_price(view)
    blocked = None
    if (rt or {}).get("hold"):
        blocked = "HOLD"
    elif px is None or view.price_age() > float(_lim(limits, "entry", "max_price_age_s", 3)):
        blocked = "STALE"
    elif how == "market":
        bid, ask = _f(view.bid), _f(view.ask)
        if bid is None or ask is None or bid <= 0 or ask < bid:
            blocked = "SPREAD"
        else:
            ref = _f(entry.get("ref_price")) or _f(ent.get("ref_px")) or _first_level(when)
            cap = float(_lim(limits, "entry", "max_chase_bps", 15))
            mine = _f((entry.get("valid") or {}).get("max_chase_bps"))
            bps = min(cap, mine) if mine is not None else cap
            side = _side(plan)
            if ref and ref > 0 and side is not None and side * (px - ref) / ref * 1e4 > bps + _EPS:
                blocked = "CHASE"
    res["blocked"] = blocked
    res["fire"] = blocked is None
    patch = res["rt_patch"].setdefault("entry", {})
    patch["blocked_why"] = blocked
    if res["fire"]:
        patch["fired_px"] = px
    return res


# ---------------------------------------------------------------- жизненный цикл
def _act(typ, why, **kw):
    a = {"type": typ, "rule": None, "why": why}
    a.update(kw)
    return a


def evaluate_lifecycle(plan, rt, view, now, limits):
    """-> [Action]: ttl / invalidation / cancel_above|below / max_hold / session_end. Не более одного завершающего действия.

    До входа (armed, позиции нет): cancel_above/below -> invalidated, invalidation -> invalidated, ttl -> expired, конец сессии
    (при flatten_at_session_end) -> cancelled; если входной ордер уже стоит (placing/resting), перед transition идёт cancel_entry.
    В позиции (entered/managing): invalidation -> close(invalidation), конец сессии (flatten) -> close(session_end),
    max_hold -> close(timeout). Первое сработавшее по приоритету. Условия вычисляются без памяти (price_beyond как allow_already=true).

    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("SOL", 10.0, 9.99, 10.01, 10.0, 100.0, None, 100.0)
    >>> p = {"side": "long", "entry": {"valid": {"ttl_min": 1}}}
    >>> evaluate_lifecycle(p, {"status": "armed", "ts": {"armed": 0.0}}, v, 100.0, {})[0]["to"]
    'expired'
    """
    status = (rt or {}).get("status")
    lc = plan.get("lifecycle") or {}
    entry = plan.get("entry") or {}
    valid = entry.get("valid") or {}
    ctx = build_ctx(plan, rt, limits, stateless=True)
    px = C._fresh_price(view, ctx)
    inv = [c for c in (plan.get("invalidation") or []) if isinstance(c, dict)]
    session_end = _f((limits or {}).get("session_end_ts"))
    lead = float((limits or {}).get("session_end_lead_s", 30))

    def inv_hit():
        for i, c in enumerate(inv):
            if C.eval_cond(c, view, {}, ctx)[0]:
                return i
        return None

    if status == "armed" and not _pos(rt):
        why, to, reason = None, None, None
        ca, cb = _f(valid.get("cancel_above")), _f(valid.get("cancel_below"))
        if px is not None and ca is not None and px >= ca:
            why, to, reason = "LIFECYCLE:CANCEL_ABOVE", "invalidated", "CANCEL_ABOVE"
        elif px is not None and cb is not None and px <= cb:
            why, to, reason = "LIFECYCLE:CANCEL_BELOW", "invalidated", "CANCEL_BELOW"
        else:
            i = inv_hit()
            ttl = _f(valid.get("ttl_min")) or _f(lc.get("ttl_min"))
            armed = _f(((rt or {}).get("ts") or {}).get("armed"))
            if i is not None:
                why, to, reason = "LIFECYCLE:INVALIDATION", "invalidated", "INVALIDATION:%d" % i
            elif ttl and armed is not None and now - armed >= ttl * 60.0 - _EPS:
                why, to, reason = "LIFECYCLE:TTL", "expired", "TTL"
            elif lc.get("flatten_at_session_end") and session_end is not None and now >= session_end:
                why, to, reason = "LIFECYCLE:SESSION_END", "cancelled", "SESSION_END"
        if not why:
            return []
        out = []
        if ((rt or {}).get("entry") or {}).get("state") in ("placing", "resting"):
            out.append(_act("cancel_entry", why))
        out.append(_act("transition", why, to=to, reason=reason))
        return out

    if status in ("entered", "managing") and _pos(rt):
        if inv_hit() is not None:
            return [_act("close", "LIFECYCLE:INVALIDATION", frac=1.0, reason="invalidation")]
        if lc.get("flatten_at_session_end") and session_end is not None and now >= session_end - lead:
            return [_act("close", "LIFECYCLE:SESSION_END", frac=1.0, reason="session_end")]
        mh = _f(lc.get("max_hold_min"))
        ent_ts = _f(((rt or {}).get("ts") or {}).get("entered"))
        if mh and ent_ts is not None and now - ent_ts >= mh * 60.0 - _EPS:
            return [_act("close", "LIFECYCLE:MAX_HOLD", frac=1.0, reason="timeout")]
    return []


# ---------------------------------------------------------------- сопровождение
def _be_offset_bps(rt, limits):
    """Умолчание смещения безубытка: комиссии круга по факту (2 x уплаченные комиссии входа) плюс 2 бп; без фактов 2 x taker."""
    p = _pos(rt) or {}
    fee = notional = 0.0
    for e in p.get("entries") or []:
        q, px, fe = _f(e.get("qty")), _f(e.get("px")), _f(e.get("fee"))
        if q and px and fe is not None:
            fee += fe
            notional += q * px
    base = 2.0 * fee / notional * 1e4 if notional > 0 and fee > 0 else float((limits or {}).get("round_trip_fee_bps", DEFAULT_BE_ROUND_TRIP_BPS))
    return base + BE_EXTRA_BPS


def trail_candidate(rule, plan, rt, view):
    """-> float | None: абсолютная цена стопа-кандидата (монотонность проверяет вызывающий).

    `rule` = правило manage или его `do`. Режимы: candle_extreme (tf, n: за экстремумом n последних закрытых свечей tf, начавшихся
    после входа), atr (tf, n, mult: от лучшей цены на mult x ATR), percent (pct от лучшей цены), step_r (step_r: лесенка, при
    достижении k*step_r стоп на (k-1)*step_r R от входа). Везде `buffer_bps` отодвигает стоп от экстремума. Нет данных = None.

    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("SOL", 110.0, 110, 110, 110, 1.0, None, 1.0)
    >>> rt = {"pos": {"qty": 1, "avg": 100.0, "initial_stop": 95.0, "r_unit": 5.0, "best_px": 112.0}}
    >>> round(trail_candidate({"do": {"action": "trail", "mode": "percent", "pct": 5.0}}, {"side": "long"}, rt, v), 6)
    106.4
    """
    do = (rule or {}).get("do", rule) or {}
    s, pos = _side(plan), _pos(rt)
    if s is None or not pos:
        return None
    mode = do.get("mode")
    buf = (_f(do.get("buffer_bps")) or 0.0) / 1e4
    avg, last = _f(pos.get("avg")), C.last_price(view)
    if avg is None or last is None:
        return None
    best = _f(pos.get("best_px"))
    best = max(best, last) if (best is not None and s == 1) else (min(best, last) if best is not None else last)
    if mode == "candle_extreme":
        book, tf = view.book, tf_min(do.get("tf"))
        n = int(_f(do.get("n")) or 0)
        ent_ms = _f(((rt or {}).get("ts") or {}).get("entered"))
        if book is None or not tf or n < 1 or ent_ms is None:
            return None
        cs = book.closed_since(tf, ent_ms * 1000.0)
        if len(cs) < n:
            return None
        cs = cs[-n:]
        if any(b.start - a.start != tf * 60000 for a, b in zip(cs, cs[1:])):
            return None
        return min(c.l for c in cs) * (1 - buf) if s == 1 else max(c.h for c in cs) * (1 + buf)
    if mode == "atr":
        book, tf = view.book, tf_min(do.get("tf"))
        mult = _f(do.get("mult"))
        if book is None or not tf or mult is None or mult <= 0:
            return None
        a = book.atr(tf, int(_f(do.get("n")) or 14))
        if a is None or a <= 0:
            return None
        return (best - mult * a) * (1 - buf) if s == 1 else (best + mult * a) * (1 + buf)
    if mode == "percent":
        pct = _f(do.get("pct"))
        if pct is None or pct <= 0:
            return None
        return best * (1 - pct / 100.0) * (1 - buf) if s == 1 else best * (1 + pct / 100.0) * (1 + buf)
    if mode == "step_r":
        step = _f(do.get("step_r"))
        ru = _f(pos.get("r_unit")) or abs(avg - (_f(pos.get("initial_stop")) or avg))
        if step is None or step <= 0 or not ru or ru <= 0:
            return None
        k = math.floor(s * (best - avg) / ru / step + 1e-9)
        if k < 1:
            return None
        lvl = avg + s * (k - 1) * step * ru
        return lvl * (1 - buf) if s == 1 else lvl * (1 + buf)
    return None


def _stop_target(do, rt, limits, s):
    """Абсолютная цена для move_stop (до округления и проверок) | None."""
    pos = _pos(rt)
    avg = _f(pos.get("avg"))
    to = do.get("to")
    if to == "breakeven":
        bps = _f(do.get("offset_bps"))
        bps = _be_offset_bps(rt, limits) if bps is None else bps
        return avg * (1 + s * bps / 1e4)
    if to == "price":
        return _f(do.get("price"))
    if to == "lock_r":
        r, ru = _f(do.get("r")), _f(pos.get("r_unit")) or abs(avg - (_f(pos.get("initial_stop")) or avg))
        return None if r is None or not ru else avg + s * r * ru
    if to == "entry_pct":
        pct = _f(do.get("pct"))
        return None if pct is None else avg * (1 + s * pct / 100.0)
    return None


def _mode_of(do):
    return {"breakeven": "breakeven", "lock_r": "lock_r"}.get(do.get("to"), "price")


def evaluate_manage(plan, rt, view, now, limits):
    """-> {"actions":[Action],"rt_patch":{..},"events":[{"name":"r_plus_1",..}],"skipped":[{"rule","code"}]}.

    Правила вычисляются только в статусе managing при открытой позиции, в порядке массива. Для каждого: once/max_times/cooldown_s,
    условие `when` (без памяти), затем действие. Несколько move_stop за один вызов схлопываются в самый жёсткий (остальные правила
    считаются сработавшими, на бирже они ничего не меняют): набор действий не зависит от порядка правил, кроме порядка в списке.
    Стоп применяется только строго жёстче текущего (иначе NOT_TIGHTER, действие не создаётся), не ближе `min_gap_bps_from_price` к цене
    (trail прижимается к границе зазора, остальные правила ждут: TOO_CLOSE) и не по ту сторону цены. При hold и устаревшей цене
    стоп не двигается (hold: add тоже запрещён). `events`: веха r_plus_1 (и r_minus_0_5 из events.wake_r) один раз, near_target один раз на цель.

    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("SOL", 102.0, 101.9, 102.1, 102.0, 5.0, None, 5.0)
    >>> p = {"side": "long", "manage": [{"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True}]}
    >>> rt = {"status": "managing", "pos": {"qty": 1, "avg": 100.0, "initial_stop": 99.0, "cur_stop": 99.0, "r_unit": 1.0}}
    >>> [a["type"] for a in evaluate_manage(p, rt, v, 5.0, {})["actions"]]
    ['move_stop']
    """
    out = {"actions": [], "rt_patch": {}, "events": [], "skipped": []}
    pos, s = _pos(rt), _side(plan)
    if (rt or {}).get("status") != "managing" or not pos or s is None:
        return out
    last = C.last_price(view)
    fresh = C._fresh_price(view, {"stale_s": float(_lim(limits, "connectivity", "public_price_stale_s", C.DEFAULT_STALE_S))}) is not None
    cur_stop = _f(pos.get("cur_stop")) or _f(pos.get("initial_stop"))
    hold = bool((rt or {}).get("hold"))
    ctx = build_ctx(plan, rt, limits, stateless=True)
    tick = (limits or {}).get("tick")
    gap = float(_lim(limits, "stop_moves", "min_gap_bps_from_price", 8)) / 1e4
    step_ticks = float(_lim(limits, "stop_moves", "min_step_ticks", 2))
    min_int = float(_lim(limits, "stop_moves", "min_interval_s", 5))
    rules_rt = (rt or {}).get("rules") or {}
    patch_rules = {}
    stops = []      # (to, rid, mode, patch)
    others = []     # (index, action)

    def mark(rid, fired_inc=1, **extra):
        rs = rules_rt.get(rid) or {}
        cur = patch_rules.get(rid) or {"fired": rs.get("fired", 0), "last_ts": rs.get("last_ts"), "state": rs.get("state", "waiting")}
        cur["fired"] += fired_inc
        if fired_inc:
            cur["last_ts"] = now
        cur.update(extra)
        patch_rules[rid] = cur
        return cur

    def skip(rid, code, once_state=False, rule=None):
        out["skipped"].append({"rule": rid, "code": code})
        if once_state and rule is not None and rule.get("once"):
            mark(rid, 0, state="skipped")

    for idx, rule in enumerate(plan.get("manage") or []):
        if not isinstance(rule, dict) or not isinstance(rule.get("do") or {}, dict):
            continue
        rid, do = rule.get("id"), rule.get("do") or {}
        act = do.get("action")
        rs = rules_rt.get(rid) or {}
        fired = int(rs.get("fired", 0) or 0)
        mt = rule.get("max_times")
        mt = 1 if (rule.get("once") and mt is None) else mt
        if (mt is not None and fired >= mt) or (rule.get("once") and rs.get("state") == "skipped"):
            continue
        cd = _f(rule.get("cooldown_s")) or 0.0
        if cd > 0 and rs.get("last_ts") is not None and now - rs["last_ts"] < cd:
            continue
        when = rule.get("when")
        if when and not C.eval_cond(when, view, {}, ctx)[0]:
            continue
        done_state = "done" if (mt is not None and fired + 1 >= mt) else "waiting"

        if act in ("move_stop", "trail"):
            if hold or not fresh or last is None or cur_stop is None:
                skip(rid, "NO_DATA")
                continue
            if act == "move_stop":
                to = _stop_target(do, rt, limits, s)
                mode = _mode_of(do)
            else:
                book_tf = tf_min(do.get("tf")) if do.get("mode") in TRAIL_CANDLE_MODES else None
                extra = {}
                if book_tf:
                    lc = view.book.last_closed(book_tf, 1) if view.book is not None else []
                    key = lc[-1].start if lc else None
                    if key is None or key == rs.get("last_candle"):
                        continue          # новой подтверждённой свечи нет: пересчёта нет
                    extra["last_candle"] = key
                elif rs.get("last_ts") is not None and now - rs["last_ts"] < min_int:
                    continue
                to = trail_candidate(rule, plan, rt, view)
                mode = "trail"
                if extra:
                    mark(rid, 0, **extra)
            to = _f(to)
            if to is None or to <= 0:
                skip(rid, "NO_DATA")
                continue
            if act == "trail":
                to = min(to, last * (1 - gap)) if s == 1 else max(to, last * (1 + gap))   # не ближе зазора к цене
            elif s * (last - to) < last * gap:
                skip(rid, "WRONG_SIDE" if s * (last - to) <= 0 else "TOO_CLOSE")
                continue
            to = _round_stop(to, tick, s)
            tk = _f(tick)
            need = (step_ticks * tk) if (act == "trail" and tk) else 0.0
            if not is_tighter(s, to, cur_stop) or s * (to - cur_stop) < need - _EPS:
                skip(rid, "NOT_TIGHTER", True, rule)
                continue
            stops.append((to, rid, mode, done_state, idx))
            continue

        a = None
        if act == "partial_close":
            fr, fi = _f(do.get("frac_of_remaining")), _f(do.get("frac_of_initial"))
            if fr is not None and 0 < fr <= 1:
                a = _act("partial_close", "RULE:%s" % rid, rule=rid, frac_remaining=fr)
            elif fi is not None and 0 < fi <= 1:
                a = _act("partial_close", "RULE:%s" % rid, rule=rid, frac_initial=fi)
        elif act == "close":
            fr = _f(do.get("frac"))
            fr = 1.0 if fr is None else fr
            if 0 < fr <= 1:
                a = _act("close", "RULE:%s" % rid, rule=rid, frac=fr, reason=do.get("reason", "rule"))
        elif act == "move_target":
            a = _move_target(rid, do, plan, rt, s, pos, cur_stop, out)
            if a is None:
                continue
        elif act == "add":
            rf = _f(do.get("risk_frac"))
            if not (limits or {}).get("pyramid", {}).get("enabled"):
                skip(rid, "ADD_DISABLED")
                continue
            if hold or rf is None or not 0 < rf <= 1:
                skip(rid, "NO_DATA")
                continue
            if int(((rt or {}).get("adds") or {}).get("n", 0)) >= int(_lim(limits, "pyramid", "max_adds", 1)):
                skip(rid, "MAX_ADDS")
                continue
            a = _act("add", "RULE:%s" % rid, rule=rid, risk_frac=rf, how=do.get("how", "market"))
        elif act == "notify" and do.get("code"):
            a = _act("notify", "RULE:%s" % rid, rule=rid, code=do["code"])
        if a is not None:
            others.append((idx, a))
            mark(rid, 1, state=done_state)

    # схлопывание стопов: самый жёсткий; при равенстве выигрывает меньший id (детерминизм при перестановке правил)
    acts = []
    if stops:
        stops.sort(key=lambda t: (-s * t[0], str(t[1])))
        to, rid, mode, _, idx = stops[0]
        acts.append((idx, _act("move_stop", "RULE:%s" % rid, rule=rid, to=to, mode=mode)))
        for to2, rid2, mode2, st2, idx2 in stops:
            mark(rid2, 1, state=st2)
    acts.extend(others)
    acts.sort(key=lambda t: t[0])
    actions = enforce_tighter([a for _, a in acts], s, cur_stop)
    out["actions"] = actions

    patch = {}
    if patch_rules:
        patch["rules"] = patch_rules
    ev, ms = _events(plan, rt, view, now, limits, pos)
    out["events"] = ev
    if ms:
        patch["counters"] = {"milestones": ms}
    out["rt_patch"] = patch
    return out


def _move_target(rid, do, plan, rt, s, pos, cur_stop, out):
    tid, newp = do.get("target"), _f(do.get("price"))
    t = ((rt or {}).get("targets") or {}).get(tid) or {}
    plan_t = next((x for x in plan.get("targets") or [] if x.get("id") == tid), {})
    curp = _f(t.get("price")) or _f(plan_t.get("price"))
    if newp is None or newp <= 0 or curp is None or t.get("state") == "filled" or t.get("state") == "cancelled":
        out["skipped"].append({"rule": rid, "code": "NO_DATA"})
        return None
    avg = _f(pos.get("avg"))
    if s * (newp - curp) > 0:                      # отодвигание цели допустимо только при стопе в безубытке или лучше
        if cur_stop is None or avg is None or s * (cur_stop - avg) < 0:
            out["skipped"].append({"rule": rid, "code": "TARGET_FARTHER_NOT_RISKFREE"})
            return None
    if newp == curp:
        return None
    return _act("move_target", "RULE:%s" % rid, rule=rid, target=tid, price=newp)


def _fmt_r(x):
    return ("%g" % abs(x)).replace(".", "_")


def _events(plan, rt, view, now, limits, pos):
    """-> (events, milestones_patch). Вехи один раз: r_plus_N / r_minus_N (events.wake_r), near_target:<id> (events.near_target_frac)."""
    done = (((rt or {}).get("counters") or {}).get("milestones")) or {}
    events, ms = [], {}
    last = C.last_price(view)
    r = r_now(rt, last) if last else None
    if r is not None:
        for thr in _lim(limits, "events", "wake_r", [1.0, -0.5]):
            thr = _f(thr)
            if thr is None or thr == 0:
                continue
            name = ("r_plus_" if thr > 0 else "r_minus_") + _fmt_r(thr)
            if name not in done and ((thr > 0 and r >= thr - _EPS) or (thr < 0 and r <= thr + _EPS)):
                ms[name] = now
                events.append({"name": name, "r": round(r, 4), "px": last, "ts": now})
    frac = _f(_lim(limits, "events", "near_target_frac", 0.8))
    avg = _f(pos.get("avg"))
    if last and avg and frac:
        for tid, t in sorted(((rt or {}).get("targets") or {}).items()):
            tp = _f((t or {}).get("price"))
            key = "near_target:%s" % tid
            if key in done or not tp or tp == avg or (t or {}).get("state") in ("filled", "cancelled"):
                continue
            if (last - avg) / (tp - avg) >= frac - _EPS:
                ms[key] = now
                events.append({"name": "near_target", "target": tid, "px": last, "ts": now})
    return events, ms
