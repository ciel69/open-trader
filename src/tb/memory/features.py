"""Извлечение признаков ситуации из записей журналов (08 п. 2.1). Чистые функции над dict, файлов не читают.

Правила: недоступное = None (не 0) и имя в situation.missing; признак, восстановленный ПОСЛЕ решения (тег no-confirm из разбора сделки),
помечается в situation.retro и не принимается валидатором ревью «known_at_decision». Текст заметок/тезисов НЕ парсится для числовых признаков:
берутся только числовые поля записи, ctx, теги. why = собственный текст системы, очищенный и обрезанный до 200.
"""
import re
import time

from tb.memory import cases, privacy

CTX_NUM = ("range_5m_pct", "range_15m_pct", "ret_5m_pct", "turn_surge_5m", "chg_24h_pct", "spread_bps", "funding_pct")
NON_FEATURES = ("sym", "side", "hyp", "missing", "retro")
RE_MECH = re.compile(r"(?i)механик|mechanic")


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def base_sym(sym):
    s = str(sym or "").upper().strip()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def utc_session(hour):
    """asia 22-07, europe 07-13, overlap 13-16 (Европа+США), us 16-22."""
    if hour is None:
        return None
    if hour >= 22 or hour < 7:
        return "asia"
    if hour < 13:
        return "europe"
    return "overlap" if hour < 16 else "us"


def time_feats(ts):
    t = cases.parse_ts(ts)
    if t is None:
        return {"utc_hour": None, "dow": None, "utc_session": None}
    g = time.gmtime(t)
    return {"utc_hour": g.tm_hour, "dow": g.tm_wday, "utc_session": utc_session(g.tm_hour)}


def classify_coin(sym, cfg, turnover_usd=None):
    """Справочник из config/memory.json; для неизвестных монет по обороту (если он дан), иначе unknown."""
    cc = cfg.get("coin_class") or {}
    b = base_sym(sym)
    for cls in ("major", "stock_token", "meme", "alt_mid"):
        if b in (cc.get(cls) or []):
            return cls
    t = num(turnover_usd)
    if t is None:
        return "unknown"
    return "alt_mid" if t >= (cc.get("turnover_mid_usd") or 2e8) else ("alt_small" if t >= (cc.get("turnover_small_usd") or 2e7) else "meme")


def stop_pct(entry, sl):
    e, s = num(entry), num(sl)
    return round(abs(e - s) / e * 100.0, 2) if e and s else None


def rr_planned(entry, sl, tp):
    e, s, t = num(entry), num(sl), num(tp)
    return round(abs(t - e) / abs(e - s), 2) if e and s and t and e != s else None


def parse_tags(tags):
    """Список тегов журнала/плана -> {setup, exp, var, hyp, plan, model, agent_id, mem, no_confirm, manual, testnet, flags:[...]}. Разделитель ':' или '.'."""
    out = {"setup": None, "exp": None, "var": None, "hyp": None, "plan": None, "model": None, "agent_id": None, "mem": None,
           "no_confirm": False, "manual": False, "testnet": False, "mechanics": False, "blackout": False}
    keys = {"setup": "setup", "exp": "exp", "var": "var", "hyp": "hyp", "plan": "plan", "model": "model", "agent": "agent_id", "mem": "mem"}
    for tg in tags or []:
        if not isinstance(tg, str):
            continue
        t = tg.strip()
        m = re.match(r"^([a-z_]+)[:.](.+)$", t)
        if m and m.group(1) in keys:
            out[keys[m.group(1)]] = m.group(2)
        elif t == "no-confirm":
            out["no_confirm"] = True
        elif t == "manual-pick":
            out["manual"] = True
        elif t == "testnet":
            out["testnet"] = True
        elif t == "blackout":
            out["blackout"] = True          # вход в макро-окне (paper, blackout_mode=warn), ставит исполнитель
        elif t in ("mechanics", "mechanic"):
            out["mechanics"] = True
    if out["setup"] not in cases.SETUPS:
        out["setup"] = None
    if out["mem"] not in cases.MEM:
        out["mem"] = None
    return out


def finalize(sit, retro=()):
    """Дополняет situation: незаполненные признаки = None, missing = их имена, retro = ретроспективные поля."""
    sit = dict(sit)
    for k in cases.SITUATION:
        sit.setdefault(k, None)
    sit["missing"] = sorted(k for k in cases.SITUATION if sit.get(k) is None and k not in NON_FEATURES)
    sit["retro"] = sorted(set(retro))
    return sit


def _ctx_part(ctx):
    ctx = ctx if isinstance(ctx, dict) else {}
    out = {k: num(ctx.get(k)) for k in CTX_NUM}
    reg = ctx.get("regime")
    out["regime"] = reg if reg in cases.REGIMES else None
    return out


def _setup(tags, op):
    s = tags["setup"] or op.get("setup")
    return s if s in cases.SETUPS else ("manual" if tags["manual"] else None)


def situation_from_trade(op, post, cfg):
    """op = open-запись журнала (со слитыми close/post полями или без), post = поля разбора (err/rule_followed) или None -> situation (finalize)."""
    tags = parse_tags(op.get("tags"))
    ctx = op.get("ctx") if isinstance(op.get("ctx"), dict) and op.get("ctx") else op.get("plan_ctx")
    sit = _ctx_part(ctx)
    reg = op.get("regime")
    if sit.get("regime") is None and reg in cases.REGIMES:
        sit["regime"] = reg
    t = time_feats(op.get("ts_open"))
    sit.update(t)
    sit.update({"sym": base_sym(op.get("sym")), "side": op.get("side") if op.get("side") in cases.SIDES else None,
                "coin_class": classify_coin(op.get("sym"), cfg), "stop_pct": stop_pct(op.get("entry"), op.get("sl")),
                "rr_planned": rr_planned(op.get("entry"), op.get("sl"), op.get("tp")),
                "setup": _setup(tags, op),
                "hyp": (op.get("hyp") or tags["hyp"] or None), "size_mult": num(op.get("size_mult")), "blackout": (True if tags["blackout"] else None)})
    retro = []
    errs = (post or {}).get("err") or []
    if tags["no_confirm"] or "no-confirm" in errs:
        sit.update({"confirmed": False, "confirm_kind": "none", "no_confirm_tag": True})
        if not tags["no_confirm"]:
            retro += ["confirmed", "confirm_kind", "no_confirm_tag"]     # знание получено из разбора после сделки
    else:
        sit.update({"confirmed": None, "confirm_kind": "unknown", "no_confirm_tag": None})
    return finalize(sit, retro)


def stop_vs_range5(sit):
    s, r = sit.get("stop_pct"), sit.get("range_5m_pct")
    return round(s / r, 2) if s and r else None


def situation_from_idea(idea, cfg):
    """Идея -> situation только из числовых полей записи (текст note не парсится)."""
    ref = num(idea.get("level")) or num(idea.get("price"))
    sit = {"sym": base_sym(idea.get("sym")), "side": idea.get("side") if idea.get("side") in cases.SIDES else None,
           "coin_class": classify_coin(idea.get("sym"), cfg), "stop_pct": stop_pct(ref, idea.get("stop")),
           "rr_planned": rr_planned(ref, idea.get("stop"), idea.get("tp")), "confirm_kind": "unknown", "setup": None}
    sit.update(time_feats(idea.get("ts")))
    return finalize(sit)


def situation_from_outcome(row, cfg):
    """Строка hot_outcomes (событие сканера) -> situation сигнала: только числовые поля строки."""
    sit = {"sym": base_sym(row.get("sym")), "side": "long" if row.get("dir") == "up" else ("short" if row.get("dir") == "down" else "watch"),
           "coin_class": classify_coin(row.get("sym"), cfg), "range_5m_pct": num(row.get("range_5m_pct")), "turn_surge_5m": num(row.get("turn_surge_5m")),
           "confirm_kind": "unknown", "setup": None}
    sit.update(time_feats(row.get("ts")))
    return finalize(sit)


def apply_brief(sit, brief):
    """Пополняет situation полями досье (если есть): fng, dvol (по символу), blackout, regime. Ничего не затирает."""
    if not isinstance(brief, dict):
        return sit
    sit = dict(sit)
    sent = brief.get("sentiment") if isinstance(brief.get("sentiment"), dict) else {}
    fng = sent.get("fng") if isinstance(sent.get("fng"), dict) else {}
    mk = brief.get("market") if isinstance(brief.get("market"), dict) else {}
    cand = {"fng": num(fng.get("v")), "regime": mk.get("regime") if mk.get("regime") in cases.REGIMES else None,
            "blackout": (bool(mk.get("blackout")) if "blackout" in mk else None),
            "dvol": num((sent.get("dvol") or {}).get(sit.get("sym"))) if isinstance(sent.get("dvol"), dict) else None,
            "btc_ret_1h_pct": num(mk.get("btc_ret_1h_pct"))}
    for k, v in cand.items():
        if sit.get(k) is None and v is not None:
            sit[k] = int(v) if k == "fng" else v
    return finalize({k: v for k, v in sit.items() if k not in ("missing", "retro")}, sit.get("retro") or [])


def why_text(s):
    """-> (очищенный why <= 200, подозрение на инъекцию)."""
    return privacy.safe_text(s, 200)


EXIT_MAP = {"sl": "sl", "tp": "tp", "manual": "manual", "timeout": "timeout", "invalidation": "invalidation", "rule": "rule", "kill": "kill",
            "session_end": "session_end"}


def result_from_close(op, cl):
    """open + close (слитые поля) -> result. r берётся из журнала; без r и без risk -> r=None."""
    r = num(cl.get("r"))
    risk = num(cl.get("risk_usdt")) or num(op.get("risk_usdt"))
    if r is None and num(cl.get("pnl_usdt")) is not None and risk:
        r = num(cl["pnl_usdt"]) / risk
    path = cl.get("path") if isinstance(cl.get("path"), dict) else {}
    t0, t1 = cases.parse_ts(op.get("ts_open")), cases.parse_ts(cl.get("ts_close"))
    fees = num(cl.get("fees_usdt"))
    return {"status": "closed", "r": r, "pnl_r_net": r, "mae_r": num(path.get("mae_r")), "mfe_r": num(path.get("mfe_r")),
            "hold_min": round((t1 - t0) / 60.0, 1) if t0 is not None and t1 is not None and t1 >= t0 else None,
            "exit_reason": EXIT_MAP.get(cl.get("reason")), "fees_r": round(fees / risk, 3) if fees is not None and risk else None}


def whatif_from_outcomes(rows, h_pref=15):
    """Строки hot_outcomes одной идеи/события -> whatif (горизонт h_pref, иначе минимальный) или None."""
    rows = [r for r in rows if isinstance(r, dict) and num(r.get("h"))]
    if not rows:
        return None
    pick = [r for r in rows if int(r["h"]) == int(h_pref)] or [min(rows, key=lambda r: r["h"])]
    r = pick[-1]
    d, c = num(r.get("dir_pct")), num(r.get("cost_pct"))
    return {"h_min": int(r["h"]), "dir_pct": d, "ret_pct": num(r.get("ret_pct")), "cost_pct": c,
            "net_dir_pct": round(d - c, 4) if d is not None and c is not None else None, "src": "hot_outcomes", "outcome_id": str(r.get("id"))[:48]}


def is_mechanics(op):
    tags = parse_tags(op.get("tags"))
    return bool(tags["mechanics"] or RE_MECH.search(str(op.get("thesis") or "")))
