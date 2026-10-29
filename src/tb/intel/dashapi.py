"""API вкладки «Новости» дашборда (WP13b): `/api/intel/news?limit=N`. Только чтение. Владелец: WP13.

Единственное место (вместе с самим сервисом), где очищенные заголовки отдаются наружу, и только в браузер владельца: дашборд вставляет их через textContent.
Мозг, шлюз, mcp и память этот модуль не импортируют (тест слоёв). Рядом с заголовком: структурная классификация читателя (digest.json), источник, возраст,
пометка подозрения, статус источников и календаря (facts.json: verified/unverified/degraded), расход читателя.
Ссылки не кликабельны: отдаётся только домен.
"""
import time

from tb.intel import digest as dg
from tb.intel import items as iitems
from tb.intel import store

MAX_LIMIT = 100


def _cls(e):
    if not isinstance(e, dict):
        return None
    return {"st": e.get("st"), "type": e.get("type"), "imp": e.get("imp"), "ir": e.get("ir"), "dir": e.get("dir"), "syms": e.get("syms") or [],
            "ttl_min": e.get("ttl_min"), "conf": e.get("conf")}


def news(root, query, now):
    try:
        limit = max(1, min(MAX_LIMIT, int((query.get("limit") or ["50"])[0])))
    except (ValueError, TypeError):
        limit = 50
    cfg = store.load_cfg(root)
    rc = dg.reader_cfg(cfg)
    dig = dg.read_digest(root) or {}
    ents = dig.get("items") if isinstance(dig.get("items"), dict) else {}
    rows = iitems.ItemStore(root).load(limit=limit)
    out = []
    for r in rows:
        ts = r.get("ts") if isinstance(r.get("ts"), (int, float)) else None
        out.append({"ref": r.get("ref"), "src": r.get("src"), "ts": ts, "age_s": None if ts is None else max(0, int(now - ts)),
                    "title": r.get("title"), "snip": r.get("snip") or "", "domain": iitems.domain_of(r.get("url", "")),
                    "susp": "inj_suspect" in (r.get("flags") or []), "seen": len(r.get("seen") or []), "cls": _cls(ents.get(r.get("ref"))),
                    "struct": r.get("src") in rc["skip_srcs"]})
    facts = store.read_facts(root) or {}
    srcs = []
    for sid, s in sorted((facts.get("sources") or {}).items()):
        if isinstance(s, dict):
            deg = sid in (facts.get("degraded") or [])
            srcs.append({"id": sid, "ok": bool(s.get("ok")), "age_s": s.get("age_s"), "fails": s.get("fails"), "verified": bool(s.get("v")), "degraded": deg})
    cal = facts.get("calendar") if isinstance(facts.get("calendar"), dict) else {}
    events = [{"id": e.get("id"), "type": e.get("type"), "in_min": e.get("in_min"), "impact": e.get("impact"), "verified": bool(e.get("v"))}
              for e in (cal.get("next") or []) if isinstance(e, dict)][:6]
    rd = dig.get("reader") if isinstance(dig.get("reader"), dict) else {}
    ts = dig.get("ts") if isinstance(dig.get("ts"), (int, float)) else None
    return {"now": int(now), "items": out, "total": len(out),
            "sources": srcs, "facts_age_s": None if not isinstance(facts.get("ts"), (int, float)) else max(0, int(now - facts["ts"])),
            "degraded": facts.get("degraded") or [],
            "calendar": {"next": events, "blackout": cal.get("blackout") if isinstance(cal.get("blackout"), dict) else None},
            "reader": {"enabled": bool(rc["enabled"]), "present": bool(dig), "status": dig.get("status"), "stale": bool(dig.get("stale")),
                       "age_s": None if ts is None else max(0, int(now - ts)), "profile": "%s.%s" % (rc["provider"], rc["model"]),
                       "limits": {"calls_h": rc["max_calls_hour"], "usd_day": rc["max_usd_day"], "batch": rc["batch"]},
                       "stats": rd}}


def api(path, query, root, now):
    """Контракт реестра EXT_API (dash/server.py): None, если путь не наш."""
    if path != "/api/intel/news":
        return None
    return 200, news(root, query, time.time() if now is None else now)
