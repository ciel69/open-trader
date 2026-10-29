"""Решение о переносе позиции через сутки: схема tb.carry/1, политика уровня риска, K-проверки поверх (07 п. 2.5-2.6, 07b п. 5.6-5.7). WP14.

Правило: агент может ЗАКРЫТЬ любую запись; `carry` принимается только если K1-K8 пройдены (K3 кумулятивно), политика уровня разрешает, `basis` не пуст.
Иначе решение превращается в close с кодом `K_FAILED:<код>` / `POLICY_NEVER` / ... Отклонённые предложения попадают в audit.json.
"""
import re

from tb.lifecycle import checklist as CL

SCHEMA = "tb.carry/1"
BASIS = ("trend_intact", "level_held", "volume_supportive", "news_neutral", "event_far", "funding_ok", "stop_at_be", "thesis_valid")
AGAINST = ("news_risk", "volume_fading", "level_lost", "event_near", "funding_high", "range_expanding")
RISKS = ("low", "mid", "high")
POLICIES = ("never", "owner_confirm", "agent")
WHY_MAX = 200
_RE_ITEM = re.compile(r"^(pos|plan):[A-Za-z0-9_-]{1,40}$")


def clean_text(s, n=WHY_MAX):
    """Очистка текста агента: управляющие символы, переводы строк, кавычки и обратные слэши -> пробел, длина."""
    s = "".join(c if (c >= " " and c not in "\x7f\"'\\`<>") else " " for c in str(s or ""))
    return " ".join(s.split())[:n]


def validate_one(raw, items):
    """Схема tb.carry/1 для одной записи -> (запись|None, [коды ошибок]). `items` = множество допустимых item."""
    errs = []
    if not isinstance(raw, dict):
        return None, ["BAD_TYPE"]
    extra = set(raw) - {"schema", "item", "decision", "basis", "against", "view", "why", "conf"}
    if extra:
        errs.append("EXTRA_KEY:%s" % sorted(extra)[0])
    if raw.get("schema") not in (None, SCHEMA):
        errs.append("BAD_SCHEMA")
    item = raw.get("item")
    if not isinstance(item, str) or not _RE_ITEM.match(item):
        errs.append("BAD_ITEM")
    elif item not in items:
        errs.append("UNKNOWN_ITEM")
    if raw.get("decision") not in ("carry", "close"):
        errs.append("BAD_ENUM:decision")
    out = {"schema": SCHEMA, "item": item, "decision": raw.get("decision")}
    for key, enum in (("basis", BASIS), ("against", AGAINST)):
        v = raw.get(key, [])
        if not isinstance(v, list) or any(not isinstance(x, str) or x not in enum for x in v):
            errs.append("BAD_ENUM:%s" % key)
            v = []
        out[key] = sorted(set(v), key=v.index)
    view = raw.get("view", {})
    if not isinstance(view, dict) or set(view) - {"risk", "horizon_h", "recheck_min"}:
        errs.append("BAD_VIEW")
        view = {}
    risk = view.get("risk", "mid")
    hz, rc = view.get("horizon_h", 12), view.get("recheck_min", 240)
    if risk not in RISKS:
        errs.append("BAD_ENUM:view.risk")
        risk = "high"
    if not (isinstance(hz, (int, float)) and not isinstance(hz, bool) and 0 < hz <= 168):
        errs.append("BAD_RANGE:horizon_h")
        hz = 12
    if not (isinstance(rc, (int, float)) and not isinstance(rc, bool) and 5 <= rc <= 1440):
        errs.append("BAD_RANGE:recheck_min")
        rc = 240
    out["view"] = {"risk": risk, "horizon_h": hz, "recheck_min": rc}
    conf = raw.get("conf", 0.5)
    if not (isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0.0 <= conf <= 1.0):
        errs.append("BAD_RANGE:conf")
        conf = 0.0
    out["conf"] = float(conf)
    out["why"] = clean_text(raw.get("why"))
    return (None if errs else out), errs


def apply_decisions(inp, rows, output, cfg):
    """Применяет ответ day_auditor к строкам-кандидатам. -> {"decisions":[...], "rejected":[...], "questions":[...], "narrative":str}.
    Строки rows изменяются на месте (decision/codes/decided_by). Порядок обработки: как в ответе агента (K3 кумулятивно по принятым)."""
    by_item = {r["item"]: r for r in rows}
    cands = {r["item"] for r in rows if r.get("candidate")}
    out = {"decisions": [], "rejected": [], "questions": [], "narrative": ""}
    if not isinstance(output, dict):
        out["rejected"].append({"item": None, "codes": ["BAD_OUTPUT"]})
        return out
    out["narrative"] = clean_text(output.get("narrative"), 600)
    pol = inp["policy"]["policy"]
    dp = inp.get("dayplan") or {}
    plan_allows = (dp.get("carry") or {}).get("allow", True) is not False
    raw_list = output.get("carry")
    if not isinstance(raw_list, list):
        out["rejected"].append({"item": None, "codes": ["BAD_CARRY_LIST"]})
        return out
    chosen = dict((r["item"], r["_n"]["risk_now"] or 0.0) for r in rows if r.get("decided_by") == "default" and r["decision"] == "carry")     # item -> риск
    seen = set()
    for raw in raw_list[:30]:
        rec, errs = validate_one(raw, cands | {r["item"] for r in rows if r["kind"] == "pos"})
        item = raw.get("item") if isinstance(raw, dict) else None
        if rec is None:
            out["rejected"].append({"item": item if isinstance(item, str) else None, "codes": errs[:4]})
            continue
        if rec["item"] in seen:
            out["rejected"].append({"item": rec["item"], "codes": ["DUP_ITEM"]})
            continue
        seen.add(rec["item"])
        row = by_item.get(rec["item"])
        if row is None or row["kind"] != "pos":
            out["rejected"].append({"item": rec["item"], "codes": ["NOT_A_POSITION"]})
            continue
        if rec["decision"] == "close":
            chosen.pop(row["item"], None)
            if row["decision"] != "close":
                _set(row, "close", rec, ["AGENT_CLOSE"])
            else:
                row["agent"] = _public(rec)
            out["decisions"].append(_public(rec))
            continue
        # decision == carry
        why = []
        if row["item"] not in cands:
            why.append("NOT_CANDIDATE:" + ",".join(row["codes"][:2] or [row["rule"]]))
        if not rec["basis"]:
            why.append("EMPTY_BASIS")
        if not plan_allows:
            why.append("DAYPLAN_NO_CARRY")
        if pol == "never":
            why.append("POLICY_NEVER")
        if not why:
            # у кандидата K-проверки уже пройдены (иначе он не кандидат); K3 кумулятивно по принятым
            code, fx = CL.k3_check(inp, [v for k, v in chosen.items() if k != row["item"]], row["_n"])
            row["facts"]["K"]["K3"] = fx
            if code:
                why.append("K_FAILED:" + code)
        if why:
            out["rejected"].append({"item": rec["item"], "codes": why[:4], "agent": _public(rec)})
            chosen.pop(row["item"], None)
            row["decision"], row["soft"] = "close", False
            row["agent_rejected"] = _public(rec)
            for c in [w for w in why if w.startswith(("K_FAILED", "POLICY", "DAYPLAN"))][:2] or ["CARRY_REJECTED"]:
                if c not in row["codes"]:
                    row["codes"].append(c)
            continue
        chosen[row["item"]] = row["_n"]["risk_now"] or 0.0
        if pol == "owner_confirm":
            _set(row, "ask_owner", rec, ["OWNER_CONFIRM"])
            out["questions"].append({"item": row["item"], "plan": row["plan"], "text": clean_text(rec["why"], 160) or "Перенести позицию %s через сутки?" % row["sym"],
                                     "options": ["carry", "close"]})
        else:
            _set(row, "carry", rec, ["AGENT_CARRY"])
        out["decisions"].append(_public(rec))
    return out


def _public(rec):
    return {k: rec[k] for k in ("item", "decision", "basis", "against", "view", "why", "conf")}


def _set(row, decision, rec, codes):
    row["decision"], row["soft"], row["decided_by"] = decision, False, "agent"
    row["agent"] = _public(rec)
    for c in codes:
        if c not in row["codes"]:
            row["codes"].append(c)


def finalize_defaults(rows):
    """Строки, которых агент не касался, остаются с умолчанием; помечаем кто решил."""
    for r in rows:
        r.setdefault("decided_by", "default" if r["kind"] in ("pos", "entry") else "system")
    return rows


def recheck_for_execution(inp, row):
    """Перед исполнением (C6): перенос подтверждаем повторной проверкой K1-K8 (K3 за вызывающим). -> список кодов провала."""
    plan = (inp.get("plans") or {}).get(row.get("plan"))
    pos = next((p for p in inp.get("positions") or [] if p.get("owner") == row.get("plan")), None)
    if pos is None:
        return ["K7_RECONCILE"]
    codes, _f, _n = CL.k_checks(inp, pos, plan)
    return codes
