"""Поиск похожих случаев: взвешенное сходство Gower (08 п. 3.1-3.2). ЧИСТАЯ логика: без файлов, сети и текущего времени (now передаётся).

S(a,b) = sum(w_i * s_i) / sum(w_i) по признакам, известным у обоих (неизвестное пропускается и снижает coverage). coverage = доля веса признаков запроса,
которые удалось сравнить. Категориальные: равенство (setup: частичное сходство семейств из config); булевые: равенство; числовые: 1 - |x-y|/scale
(шкалы из конфига, не из данных; логарифмические признаки считаются в log1p); utc_hour: циклическое расстояние.
Жёсткие фильтры: тип, режим (live/paper/testnet раздельно), версия правил (текущая и предыдущая), сторона, возраст, as_of (случаи только ДО решения),
exclude_from_stats, dup_of_trade, inj_suspect. Порядок результата детерминирован: (-sim, свежее, case_id), не зависит от порядка строк файла.
"""
import calendar
import math
import time

CAT = ("regime", "side", "utc_session", "coin_class", "setup", "confirm_kind")
BOOL = ("confirmed", "no_confirm_tag", "blackout")
CYC = {"utc_hour": 24.0}
DAY = 86400.0


def ts_epoch(s):
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def vector(case):
    """Случай -> {признак: значение} из situation и decision.stop_dist_vs_range5 (None не включается)."""
    v = dict((k, x) for k, x in (case.get("situation") or {}).items() if x is not None and k not in ("sym", "hyp", "missing", "retro"))
    d = (case.get("decision") or {}).get("stop_dist_vs_range5")
    if d is not None:
        v["stop_dist_vs_range5"] = d
    return v


def _num_sim(name, a, b, cfg):
    scale = (cfg.get("scales") or {}).get(name) or 1.0
    if name in (cfg.get("log_features") or ()):
        a, b = math.log1p(max(a, 0.0)), math.log1p(max(b, 0.0))
        scale = math.log1p(scale)
    return max(0.0, 1.0 - abs(a - b) / scale)


def feature_sim(name, a, b, cfg):
    if name in CYC:
        d = abs(a - b) % CYC[name]
        return 1.0 - min(d, CYC[name] - d) / (CYC[name] / 2.0)
    if name in CAT:
        if a == b:
            return 1.0
        if name == "setup":
            fam = cfg.get("setup_family") or {}
            return float(fam.get("%s~%s" % (a, b), fam.get("%s~%s" % (b, a), 0.0)))
        return 0.0
    if name in BOOL:
        return 1.0 if a == b else 0.0
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return 1.0 if a == b else 0.0
    return _num_sim(name, float(a), float(b), cfg)


def similarity(q, v, cfg):
    """q, v: векторы -> (S, coverage, n_used)."""
    w = cfg.get("weights") or {}
    tot_q = sum(w.get(k, 0.0) for k in q)
    used = [k for k in q if k in v and w.get(k, 0.0) > 0]
    sw = sum(w[k] for k in used)
    if not used or sw <= 0:
        return 0.0, 0.0, 0
    s = sum(w[k] * feature_sim(k, q[k], v[k], cfg) for k in used) / sw
    return round(s, 4), round(sw / tot_q, 4) if tot_q > 0 else 0.0, len(used)


def allowed_rules(cfg):
    hist = list(cfg.get("rules_history") or [])
    cur = cfg.get("rules_ver")
    out = set(hist[-2:])
    if cur:
        out.add(cur)
    return out


def prefilter(c, query, cfg, now, opts):
    """-> None (подходит) или код причины отказа жёсткого фильтра."""
    if c.get("type") not in opts["types"]:
        return "type"
    fl = c.get("flags") or []
    if "inj_suspect" in fl or "dup_of_trade" in fl:
        return "flags"
    if c.get("exclude_from_stats") and not opts.get("include_excluded"):
        return "excluded"
    src = c.get("src") or {}
    if opts["scope"] == "same_mode" and (src.get("mode") or "live") != opts["mode"]:
        return "mode"
    if opts["rules"] != "any" and src.get("rules_ver") not in opts["rules_ok"]:
        return "rules_ver"
    side, cs = query.get("side"), (c.get("situation") or {}).get("side")
    if side in ("long", "short") and cs != side:
        return "side"
    t = ts_epoch(c.get("ts"))
    if t is None:
        return "ts"
    if opts.get("as_of") is not None and t >= opts["as_of"]:
        return "as_of"
    if opts["max_age_days"] and now - t > opts["max_age_days"] * DAY:
        return "age"
    return None


def select(cases, query, cfg, now, k=None, min_sim=None, min_coverage=None, scope=None, rules=None, types=None, mode=None,
           max_age_days=None, as_of=None, include_excluded=False):
    """cases: свёрнутые случаи; query: {"features": {...}, "side": "long|short|watch|None", "mode": "live"}.

    -> {"hits": [{"case","sim","coverage","n_used"}], "considered", "filtered": {причина: n}, "k", "min_sim_used", "avg_sim", "coverage"}"""
    sc = cfg.get("similar") or {}
    k = max(1, min(int(k or sc.get("k_default", 8)), int(sc.get("k_max", 20))))
    ms = sc.get("min_sim", 0.55) if min_sim is None else min_sim
    mc = sc.get("min_coverage", 0.6) if min_coverage is None else min_coverage
    opts = {"types": tuple(types or ("trade", "skip", "idea")), "scope": scope or sc.get("scope", "same_mode"), "rules": rules or sc.get("rules", "cur"),
            "mode": mode or query.get("mode") or "live", "rules_ok": allowed_rules(cfg), "as_of": as_of, "include_excluded": include_excluded,
            "max_age_days": sc.get("max_age_days", 90) if max_age_days is None else max_age_days}
    feats = dict((k_, v) for k_, v in (query.get("features") or {}).items() if v is not None)
    if query.get("side") in ("long", "short"):         # watch = направление неизвестно, в сходстве не участвует
        feats.setdefault("side", query["side"])
    filtered, scored = {}, []
    for c in cases:
        why = prefilter(c, query, cfg, now, opts)
        if why:
            filtered[why] = filtered.get(why, 0) + 1
            continue
        s, cov, n_used = similarity(feats, vector(c), cfg)
        if n_used == 0 or cov < mc:
            filtered["coverage"] = filtered.get("coverage", 0) + 1
            continue
        if s < ms:
            filtered["min_sim"] = filtered.get("min_sim", 0) + 1
            continue
        scored.append({"case": c, "sim": s, "coverage": cov, "n_used": n_used})
    scored.sort(key=lambda h: (-h["sim"], -(ts_epoch(h["case"].get("ts")) or 0.0), h["case"]["case_id"]))
    hits = scored[:k]
    return {"hits": hits, "considered": len(cases), "filtered": filtered, "k": k, "min_sim_used": ms, "scope": opts["scope"], "mode": opts["mode"],
            "avg_sim": round(sum(h["sim"] for h in hits) / len(hits), 3) if hits else None,
            "coverage": round(sum(h["coverage"] for h in hits) / len(hits), 3) if hits else None}


def compact(hit, why_max=100):
    """Сжатый вид случая для инструмента шлюза и brief: {case_id, sim, ts, side, setup, confirmed, r, err[], exit, why}."""
    c = hit["case"]
    s, r = c.get("situation") or {}, c.get("result") or {}
    return {"case_id": c["case_id"], "sim": hit["sim"], "ts": c.get("ts"), "side": s.get("side"), "setup": s.get("setup"), "confirmed": s.get("confirmed"),
            "r": r.get("r"), "err": list(((c.get("tags") or {}).get("err")) or []), "exit": r.get("exit_reason"),
            "why": str((c.get("decision") or {}).get("why") or "")[:why_max]}
