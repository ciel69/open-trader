"""Детерминированный чек-лист закрытия дня A0-A9 и жёсткие условия переноса K1-K8 (07 п. 2.5-2.6). Чистые функции от `inp` (словарь audit_input). WP14.

Работает без LLM. K-условия не переопределяются ни LLM, ни политикой уровня. Запись чек-листа:
{"item","kind","plan","sym","side","stype","rule","decision":"close|carry|cancel|keep|stop","soft","candidate","codes":[...],"facts":{...числа...}}.
`candidate` = агент (day_auditor) может предложить перенос; `soft` = решение умолчания, которое агент вправе заменить на carry (никогда не наоборот в сторону риска).
"""
import math

CARRY_STYPES = ("intraday", "swing", "position")
OPEN_STATUS = ("entered", "managing")
WAIT_STATUS = ("draft", "armed")
K_CODES = ("K1_STOP", "K2_RISK", "K3_TOTAL", "K4_EVENT", "K5_FUNDING", "K6_LIQ", "K7_RECONCILE", "K8_LINK")
# событие считаем близким для символа, если impact >= этого порога
EVENT_IMPACT = 3


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _f(v, default=0.0):
    return float(v) if _num(v) else default


def _round(v, n=4):
    return round(v, n) if _num(v) else None


# ------------------------------------------------------------------ числа позиции
def pos_numbers(inp, pos, plan):
    """-> dict чисел позиции: size, avg, mark, sl, risk0, risk_now, stop_pct, be_ok, notional."""
    size, avg = _f(pos.get("size")), _f(pos.get("avg"))
    mark = _f(pos.get("mark"), avg)
    sl = pos.get("sl")
    sl = float(sl) if _num(sl) else 0.0
    long_ = pos.get("side") == "long"
    fee = _f((inp.get("profile") or {}).get("taker_fee_pct"), 0.0009)
    rtp = ((plan or {}).get("rt") or {}).get("pos") or {}
    init = rtp.get("initial_stop")
    init = float(init) if _num(init) else sl
    dist = (mark - sl) if long_ else (sl - mark)
    risk_now = size * max(dist, 0.0) + fee * size * (mark + sl) if sl > 0 else None
    risk0 = rtp.get("risk0")
    risk0 = float(risk0) if _num(risk0) and risk0 > 0 else (size * abs(avg - init) if init > 0 else None)
    be_ok = bool(sl > 0 and ((long_ and sl >= avg) or (not long_ and sl <= avg)))
    return {"size": size, "avg": avg, "mark": mark, "sl": sl, "init_stop": init, "risk0": risk0, "risk_now": risk_now, "be_ok": be_ok,
            "stop_pct": (abs(avg - init) / avg * 100.0) if avg and init else None, "notional": size * mark, "long": long_}


# ------------------------------------------------------------------ K1-K8
def k1(inp, pos, plan, n):
    prof = (inp.get("profile") or {}).get("session_types") or {}
    stype = (plan or {}).get("stype") or "intraday"
    mx = _f((prof.get(stype) or {}).get("max_stop_pct"), 0.0)
    ra = (inp.get("health") or {}).get("reconcile_age_s")
    facts = {"sl": _round(n["sl"], 6), "stop_pct": _round(n["stop_pct"], 3), "max_stop_pct": mx or None, "reconcile_age_s": ra}
    if not n["sl"] > 0:
        return "K1_STOP", dict(facts, why="нет стопа на бирже")
    side_ok = (n["sl"] < n["mark"]) if n["long"] else (n["sl"] > n["mark"])
    if not side_ok:
        return "K1_STOP", dict(facts, why="стоп не на защитной стороне цены")
    if not _num(ra) or ra >= float(inp["cfg"]["preflight"]["reconcile_max_s"]):
        return "K1_STOP", dict(facts, why="стоп не подтверждён сверкой (%s с)" % ra)
    if mx and n["stop_pct"] is not None and n["stop_pct"] > mx + 1e-9:
        return "K1_STOP", dict(facts, why="стоп шире %.2f%% типа %s" % (mx, stype))
    return None, facts


def k2(inp, pos, plan, n):
    pol = inp["policy"]
    frac = _f(pol.get("max_risk_frac_of_trade"), 0.5)
    facts = {"risk_now": _round(n["risk_now"]), "risk0": _round(n["risk0"]), "max_frac": frac, "be_ok": n["be_ok"]}
    if n["be_ok"]:
        return None, facts
    if n["risk_now"] is None or n["risk0"] is None or n["risk0"] <= 0:
        return "K2_RISK", dict(facts, why="риск до стопа не вычислен")
    if n["risk_now"] > frac * n["risk0"] + 1e-12:
        return "K2_RISK", dict(facts, why="риск до стопа %.3f > %.2f x R0 %.3f" % (n["risk_now"], frac, n["risk0"]))
    return None, facts


def k3_cap(inp):
    pol = inp["policy"]
    return _f(pol.get("max_total_risk_frac")) * _f(inp.get("daily_loss_usdt")), int(min(_f(pol.get("max_positions")), _f(inp.get("plan_max_positions"), 99)))


def k3_check(inp, chosen, n):
    """chosen = [risk_now уже выбранных на перенос]; n = числа кандидата -> (код|None, facts). Кумулятивно: число и суммарный риск."""
    cap_usd, cap_n = k3_cap(inp)
    r = n["risk_now"] if n["risk_now"] is not None else 0.0
    total = sum(chosen) + r
    facts = {"chosen_n": len(chosen), "max_positions": cap_n, "total_risk": _round(total), "cap_usd": _round(cap_usd)}
    if len(chosen) + 1 > cap_n:
        return "K3_TOTAL", dict(facts, why="переносов уже %d из %d" % (len(chosen), cap_n))
    if total > cap_usd + 1e-12:
        return "K3_TOTAL", dict(facts, why="суммарный риск %.3f > %.3f (доля дневного лимита)" % (total, cap_usd))
    return None, facts


def _events_near(inp, sym, hours):
    """-> список близких событий [{"what","in_min"}] по символу и макро в пределах hours."""
    fx = inp.get("facts")
    out = []
    if not isinstance(fx, dict):
        return None
    now, lim = inp["now"], hours * 60.0
    for e in ((fx.get("calendar") or {}).get("next")) or []:
        if not isinstance(e, dict) or not _num(e.get("impact")) or e["impact"] < EVENT_IMPACT:
            continue
        ts = e.get("ts")
        m = (ts - now) / 60.0 if _num(ts) else e.get("in_min")
        if _num(m) and -30.0 <= m <= lim:
            out.append({"what": str(e.get("type") or e.get("id") or "macro")[:20], "in_min": int(m)})
    bl = (fx.get("calendar") or {}).get("blackout")
    if isinstance(bl, dict) and _num(bl.get("in_min")) and bl["in_min"] <= lim:
        out.append({"what": "blackout:%s" % str(bl.get("name") or "")[:20], "in_min": int(bl["in_min"])})
    for e in fx.get("events") or []:
        if isinstance(e, dict) and sym in (e.get("syms") or []) and _num(e.get("imp")) and e["imp"] >= EVENT_IMPACT:
            out.append({"what": str(e.get("type") or "event")[:20], "in_min": 0})
    ex = fx.get("exchange") or {}
    if sym in (ex.get("delist_syms") or []):
        out.append({"what": "delist", "in_min": 0})
    mm = ex.get("maint_in_min")
    if _num(mm) and mm <= lim:
        out.append({"what": "maintenance", "in_min": int(mm)})
    return out


def k4(inp, pos, plan, n):
    hours = _f(inp["policy"].get("event_hours"), 12.0)
    ev = _events_near(inp, pos.get("sym"), hours)
    if ev is None:
        return "K4_EVENT", {"why": "календарь недоступен (facts нет)", "event_hours": hours}
    if ev:
        return "K4_EVENT", {"events": ev[:3], "event_hours": hours, "why": "близкое событие: %s через %d мин" % (ev[0]["what"], ev[0]["in_min"])}
    return None, {"events": [], "event_hours": hours}


def k5(inp, pos, plan, n):
    fx = inp.get("facts")
    pct = ((fx.get("deriv") or {}).get(pos.get("sym")) or {}).get("funding_pct") if isinstance(fx, dict) else None
    hrs = max(0.0, (inp["next_day_start"] - inp["now"]) / 3600.0)
    frac = _f(inp["policy"].get("max_funding_frac_of_risk"), 0.25)
    facts = {"funding_pct": pct, "hours": round(hrs, 2), "max_frac": frac}
    if not _num(pct):
        return "K5_FUNDING", dict(facts, why="funding неизвестен")
    payments = hrs / 8.0
    sign = 1.0 if n["long"] else -1.0
    cost = max(0.0, sign * pct / 100.0 * n["notional"] * payments)          # платим только если funding против позиции
    facts["cost"] = _round(cost, 5)
    base = n["risk0"] or n["risk_now"] or 0.0
    if cost > frac * base + 1e-12:
        return "K5_FUNDING", dict(facts, why="funding до старта %.4f > %.2f x риска %.3f" % (cost, frac, base))
    return None, facts


def k6(inp, pos, plan, n):
    lf = _f((inp.get("profile") or {}).get("liq_factor"), 2.2)
    liq = pos.get("liq")
    facts = {"liq": liq, "liq_factor": lf}
    if not _num(liq) or liq <= 0:
        return "K6_LIQ", dict(facts, why="цена ликвидации неизвестна")
    liq_dist = abs(n["avg"] - float(liq))
    stop_dist = abs(n["avg"] - n["init_stop"]) if n["init_stop"] else 0.0
    facts.update({"liq_dist": _round(liq_dist, 6), "stop_dist": _round(stop_dist, 6)})
    if stop_dist <= 0 or liq_dist < lf * stop_dist - 1e-12:
        return "K6_LIQ", dict(facts, why="до ликвидации %.4g < %.2f x стоп %.4g" % (liq_dist, lf, stop_dist))
    return None, facts


def k7(inp, pos, plan, n):
    sym = pos.get("sym")
    h = inp.get("health") or {}
    facts = {"diffs": h.get("diffs"), "owner": pos.get("owner")}
    if plan is None:
        return "K7_RECONCILE", dict(facts, why="у позиции нет плана (orphan)")
    if plan.get("hold") or (plan.get("status") not in OPEN_STATUS):
        return "K7_RECONCILE", dict(facts, why="план в hold/blocked: %s" % (plan.get("hold") or plan.get("status")))
    if h.get("diffs"):
        return "K7_RECONCILE", dict(facts, why="расхождения с биржей: %s" % h.get("diffs"))
    if sym in (inp.get("alerts") or {}):
        return "K7_RECONCILE", dict(facts, why="reconcile_alert по %s" % sym)
    return None, facts


def k8(inp, pos, plan, n):
    h = inp.get("health") or {}
    ws = (h.get("ws") or {}).get("private") or {}
    facts = {"hold_reasons": h.get("hold_reasons") or [], "private_ws": ws.get("up"), "brain": inp.get("brain_ok"), "intel_age_s": inp.get("facts_age_s"),
             "provider_recent": inp.get("provider_recent_ok")}
    why = []
    if h.get("hold_reasons"):
        why.append("hold: %s" % ",".join(str(x) for x in h["hold_reasons"][:2]))
    if not ws.get("up"):
        why.append("приватный WS не работает")
    if not inp.get("brain_ok"):
        why.append("мозг недоступен")
    age = inp.get("facts_age_s")
    if not _num(age) or age > 1800.0:
        why.append("intel не отвечал последние 30 минут")
    if not inp.get("provider_recent_ok"):
        why.append("провайдер LLM не отвечал последние 30 минут")
    if why:
        return "K8_LINK", dict(facts, why="; ".join(why))
    return None, facts


K_FUNCS = (("K1", k1), ("K2", k2), ("K4", k4), ("K5", k5), ("K6", k6), ("K7", k7), ("K8", k8))


def k_checks(inp, pos, plan):
    """K1, K2, K4-K8 (K3 кумулятивно отдельно) -> (codes, facts{K1:...}, nums)."""
    n = pos_numbers(inp, pos, plan)
    codes, facts = [], {}
    for name, fn in K_FUNCS:
        try:
            code, fx = fn(inp, pos, plan, n)
        except Exception:
            code, fx = name + "_ERR", {"why": "проверка не выполнилась"}
        facts[name] = fx
        if code:
            codes.append(code)
    return codes, facts, n


# ------------------------------------------------------------------ правила A0-A9
def _stype(plan):
    return (plan or {}).get("stype") or "intraday"


def has_exit_rules(plan):
    spec = (plan or {}).get("spec") or {}
    if _num((spec.get("lifecycle") or {}).get("max_hold_min")):
        return True
    for r in spec.get("manage") or []:
        if isinstance(r, dict) and (r.get("do") or {}).get("action") == "trail":
            return True
    return False


def _max_hold_expired(inp, plan):
    spec = (plan or {}).get("spec") or {}
    mh = (spec.get("lifecycle") or {}).get("max_hold_min")
    ents = (((plan or {}).get("rt") or {}).get("pos") or {}).get("entries") or []
    if _num(mh) and ents and _num(ents[0].get("ts")):
        return inp["now"] - ents[0]["ts"] > mh * 60.0
    return False


def eval_position(inp, pos, plan):
    sym = pos.get("sym")
    pid = (plan or {}).get("id") or pos.get("owner")
    stype = _stype(plan)
    row = {"item": "pos:%s" % (pid or "ORPHAN-%s" % sym), "kind": "pos", "plan": pid, "sym": sym, "side": pos.get("side"), "stype": stype, "rule": None,
           "decision": "close", "soft": False, "candidate": False, "codes": [], "facts": {}}
    codes, kf, n = k_checks(inp, pos, plan)
    row["facts"] = {"size": _round(n["size"], 6), "avg": _round(n["avg"], 6), "mark": _round(n["mark"], 6), "sl": _round(n["sl"], 6),
                    "risk_now": _round(n["risk_now"]), "risk0": _round(n["risk0"]), "be_ok": n["be_ok"], "K": kf}
    row["_n"] = n
    if plan is None:
        row.update(rule="A0", codes=["A0_ORPHAN"])
        return row
    if plan.get("hold") or plan.get("status") not in OPEN_STATUS or (inp.get("health") or {}).get("diffs"):
        row.update(rule="A0", codes=["A0_DRIFT"])
        return row
    if stype == "scalp":                                              # скальп не переносится ни на каком уровне
        row.update(rule="A2", codes=["A2_SCALP"])
        return row
    if _max_hold_expired(inp, plan):
        row.update(rule="A6", codes=["A6_MAX_HOLD"])
        return row
    pol = inp["policy"]["policy"]
    if pol == "never" or not inp["cfg"]["carry"].get("enabled", True):
        row.update(rule="A3" if stype == "intraday" else "A4", codes=["POLICY_NEVER"])
        return row
    if codes:
        row.update(rule="A7", codes=codes)
        return row
    row["candidate"] = True
    row["rule"] = "A3" if stype == "intraday" else "A4"
    if stype == "intraday":
        row["decision"], row["soft"] = "close", True                     # решение агента или закрытие
    else:                                                               # swing/position
        need = bool(inp["cfg"]["carry"].get("need_trail_or_max_hold", True))
        if pol == "agent" and (has_exit_rules(plan) or not need):
            row["decision"], row["soft"] = "carry", True
        else:
            row["decision"], row["soft"] = "close", True
            if pol == "agent":
                row["codes"] = ["NO_EXIT_RULES"]
    return row


def eval_waiting(inp, plan):
    """A1: ждущий вход (план без позиции) -> cancel; keep только swing/position при ttl дольше времени до следующего дня и без K4."""
    stype = _stype(plan)
    row = {"item": "plan:%s" % plan.get("id"), "kind": "entry", "plan": plan.get("id"), "sym": plan.get("sym"), "side": plan.get("side"), "stype": stype, "rule": "A1",
           "decision": "cancel", "soft": False, "candidate": False, "codes": [], "facts": {}}
    spec, rt = plan.get("spec") or {}, plan.get("rt") or {}
    ttl = (spec.get("lifecycle") or {}).get("ttl_min")
    created = rt.get("ts")
    exp = (created + ttl * 60.0) if _num(created) and _num(ttl) else None
    row["facts"] = {"expires_ts": exp, "next_day_start": inp["next_day_start"]}
    if stype in ("swing", "position") and inp["policy"]["policy"] != "never" and exp is not None and exp > inp["next_day_start"]:
        ev = _events_near(inp, plan.get("sym"), _f(inp["policy"].get("event_hours"), 12.0))
        if ev == []:
            row.update(decision="keep", codes=["A1_KEEP_LONG_TTL"])
    return row


def evaluate(inp):
    """-> {"rows":[...], "carry_default":[item...], "orphans":[...], "llm_candidates":[item...]}.
    K3 применяется кумулятивно к умолчаниям carry (A4+agent): в порядке возрастания риска."""
    rows = []
    plans = inp.get("plans") or {}
    seen = set()
    for pos in inp.get("positions") or []:
        plan = plans.get(pos.get("owner")) if pos.get("owner") else None
        rows.append(eval_position(inp, pos, plan))
        if plan:
            seen.add(plan["id"])
    for pid, plan in sorted(plans.items()):
        if pid in seen:
            continue
        if plan.get("status") in WAIT_STATUS:
            rows.append(eval_waiting(inp, plan))
        elif plan.get("status") in OPEN_STATUS:                       # план в позиции, но позиции нет в снимке: расхождение, закрывать нечем -> A0 (оценит C6)
            rows.append({"item": "plan:%s" % pid, "kind": "entry", "plan": pid, "sym": plan.get("sym"), "side": plan.get("side"), "stype": _stype(plan), "rule": "A0",
                         "decision": "close", "soft": False, "candidate": False, "codes": ["A0_NO_POSITION"], "facts": {}})
    # K3 для умолчаний carry: кумулятивно, самые малые риски первыми
    chosen = []
    defaults = sorted((r for r in rows if r["kind"] == "pos" and r["candidate"] and r["decision"] == "carry"), key=lambda r: r["_n"]["risk_now"] or 0.0)
    for r in defaults:
        code, fx = k3_check(inp, chosen, r["_n"])
        r["facts"]["K"]["K3"] = fx
        if code:
            r.update(decision="close", soft=False, candidate=False, rule="A7", codes=[code])
        else:
            chosen.append(r["_n"]["risk_now"] or 0.0)
    for r in rows:
        if r["kind"] == "pos" and r["candidate"] and r["decision"] != "carry":
            code, fx = k3_check(inp, [], r["_n"])                       # кандидат в одиночку должен помещаться в лимиты
            r["facts"]["K"]["K3"] = fx
            if code:
                r.update(decision="close", soft=False, candidate=False, rule="A7", codes=[code])
    for s in inp.get("sessions") or []:
        rows.append({"item": "sess:%s" % s.get("id"), "kind": "session", "plan": None, "sym": None, "side": None, "stype": s.get("type"), "rule": "A8", "decision": "stop",
                     "soft": False, "candidate": False, "codes": [], "facts": {}})
    if inp.get("ideas_n"):
        rows.append({"item": "ideas", "kind": "ideas", "plan": None, "sym": None, "side": None, "stype": None, "rule": "A9", "decision": "keep", "soft": False,
                     "candidate": False, "codes": [], "facts": {"count": inp["ideas_n"]}})
    return {"rows": rows}


def public_rows(rows):
    """Строки без служебных полей (_n)."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
