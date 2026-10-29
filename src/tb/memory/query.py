"""Сервисный слой чтения для CLI и будущих инструментов шлюза (WP15b): похожие случаи + агрегат + применимые уроки + предупреждение.

Без записи. find_similar() возвращает компактный словарь (случаи в сжатом виде, why <= 100), как ждёт get_similar_cases шлюза.
Память ничего не разрешает: результат только данные для контекста.
"""
import time

from tb.memory import agg, cases, cfg as memcfg, features, lessons, similar

FEATURE_TYPES = dict((k, "num") for k in cases.NUM_FEATURES)
FEATURE_TYPES.update({"utc_hour": "int", "dow": "int", "fng": "int", "confirmed": "bool", "no_confirm_tag": "bool", "blackout": "bool", "regime": "enum",
                      "utc_session": "enum", "coin_class": "enum", "setup": "enum", "confirm_kind": "enum", "stop_dist_vs_range5": "num"})
QUERY_FEATURES = tuple(sorted(FEATURE_TYPES))


def parse_feature(k, v):
    """'range_5m_pct','1.8' -> число/bool/enum по белому списку; ValueError при неверном имени или значении."""
    t = FEATURE_TYPES.get(k)
    if t is None:
        raise ValueError("признак %r вне белого списка: %s" % (k, ", ".join(QUERY_FEATURES)))
    if t == "bool":
        if str(v).lower() not in ("true", "false", "1", "0"):
            raise ValueError("%s: true|false" % k)
        return str(v).lower() in ("true", "1")
    if t == "enum":
        tbl = cases.SITUATION.get(k)
        if v not in tbl[1]:
            raise ValueError("%s: %r не из %s" % (k, v, list(tbl[1])))
        return v
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s: нужно число" % k)
    return int(x) if t == "int" else x


def build_query(sym=None, side=None, setup=None, feats=None, mode=None, cfg=None, now=None):
    """Ситуация запроса -> {"features","side","mode"}. sym даёт coin_class; now (epoch) даёт utc_session/hour/dow, если они не заданы явно."""
    f = dict(feats or {})
    if setup:
        f["setup"] = parse_feature("setup", setup)
    if sym and "coin_class" not in f:
        cc = features.classify_coin(sym, cfg or {})
        if cc != "unknown":
            f["coin_class"] = cc
    if now is not None and "utc_session" not in f:
        t = features.time_feats(cases.iso(now))
        f.update(dict((k, v) for k, v in t.items() if v is not None and k not in f))
    return {"features": f, "side": side if side in cases.SIDES else None, "mode": mode or "live", "sym": sym}


def find_similar(root, query, k=None, scope=None, rules=None, as_of=None, by=None, now=None, types=None, cfg=None, all_cases=None, lessons_list=None):
    cfg = cfg or memcfg.load(root)
    now = time.time() if now is None else now
    allc = cases.load(root) if all_cases is None else all_cases
    sel = similar.select(allc, query, cfg, now, k=k, scope=scope, rules=rules, as_of=as_of, mode=query.get("mode"), types=types)
    rep = agg.build_report(sel, allc, cfg, by=by)
    ls = list(lessons.read_all(root).values()) if lessons_list is None else lessons_list
    feats = dict(query.get("features") or {})
    if query.get("side"):
        feats.setdefault("side", query["side"])
    other = None
    if sel.get("scope") == "same_mode":
        alt = [c for c in allc if (c.get("src") or {}).get("mode", "live") != sel["mode"] and agg.closed_r(c) is not None and not c.get("exclude_from_stats")]
        other = {"n_closed": len(alt), "mean_r": agg.summarize(alt, None, cfg)["mean_r"]} if alt else None
    return {"similar": rep, "cases": [similar.compact(h) for h in sel["hits"]], "lessons": lessons.applicable(ls, feats, now, cfg),
            "filtered": sel["filtered"], "other_mode": other, "warn": rep["warn"]}


def stats(root, by=None, mode=None, since=None, min_n=5, cfg=None):
    """Сводка по базе: общий агрегат режима + разрез by (группы меньше min_n помечены, но показываются)."""
    cfg = cfg or memcfg.load(root)
    allc = cases.load(root)
    ex = tuple(cfg.get("exclude_roles") or ())
    # разрез mem сравнивает случаи С памятью и БЕЗ неё: mem=off здесь не исключается (в уроки и общую статистику он по-прежнему не идёт)
    ok = (lambda c: not c.get("exclude_from_stats") and "inj_suspect" not in (c.get("flags") or []) and (c.get("src") or {}).get("role") not in ex) if by == "mem" \
        else (lambda c: cases.eligible(c, cfg, ex))
    sel = [c for c in allc if ok(c) and (mode is None or (c.get("src") or {}).get("mode", "live") == mode)
           and (since is None or (c.get("ts") or "") >= since)]
    base = agg.base_rate(sel, None)
    out = {"mode": mode, "total": agg.summarize(sel, base["base_r"], cfg), "types": {}}
    for c in sel:
        out["types"][c["type"]] = out["types"].get(c["type"], 0) + 1
    if by:
        rows = agg.breakdown([c for c in sel if c.get("type") == "trade"], by, base["base_r"], cfg)
        for r in rows:
            r["small"] = r["n"] < min_n
        out["by"] = {by: rows}
    return out
