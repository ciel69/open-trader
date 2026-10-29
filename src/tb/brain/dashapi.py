"""Чтение для дашборда (EXT_API): `/api/brain/watch` = «что видит мозг и чего ждёт». Только чтение, без секретов, без текстов третьих лиц (заголовки новостей, названия, заметки).

Источник: свежая сборка досье scope=start БЕЗ сохранения (brief.build; файл в state/briefs не пишется), плюс health мозга, задания и журнал решений, план дня, конфигурация подписки.
Ответ (контракт для вёрстки: docs/spec_v2/BRAIN_WATCH.md): {"ts","brief":{"id","ts","age_s"},"state":{"code","text","detail"},"blocks":{market,hot,struct,intel,news_digest,ideas,recent},
"last_job":{"role","ts","outcome","code"}|null,"next_wake":{"roles":{роль:[типы]},"jobs_per_hour","scan_jobs_per_hour","role_jobs_per_hour","subscriber_enabled"}}.
"""
import time

from tb import paths
from tb.brain import common

PATH = "/api/brain/watch"
STATES = ("working", "waiting_targets", "blocked_by_dayplan", "blackout", "protect_only", "brain_down")
TEXTS = {"working": "работает: есть планы, позиции или задание в обработке", "waiting_targets": "ждёт целей: активных идей нет, горячих монет тут %(hot)d",
         "blocked_by_dayplan": "вход закрыт планом дня: max_new_plans=0", "blackout": "окно новостей: вход закрыт, до события %(in_min)d мин",
         "protect_only": "режим только защита: новые планы не создаются", "brain_down": "мозг недоступен: нет рабочей модели"}


def _age(ts, now):
    v = common.num(ts)
    return None if v is None else max(0, int(now - v))


def _blocks(b, root, now):
    mk = b.get("market") if isinstance(b.get("market"), dict) else {}
    hot = b.get("hot") if isinstance(b.get("hot"), dict) else {}
    st = b.get("struct") if isinstance(b.get("struct"), dict) else {}
    srows = st.get("s") if isinstance(st.get("s"), dict) else {}
    regs = {}
    for r in srows.values():
        k = r.get("reg") if isinstance(r, dict) and isinstance(r.get("reg"), str) else "unknown"
        regs[k] = regs.get(k, 0) + 1
    facts = common.read_json(paths.runtime("intel", "facts.json", root=root))
    digest = common.read_json(paths.runtime("intel", "digest.json", root=root))
    nd = b.get("news_digest") if isinstance(b.get("news_digest"), dict) else {}
    intel = b.get("intel") if isinstance(b.get("intel"), dict) else {}
    ideas = b.get("ideas") if isinstance(b.get("ideas"), dict) else {}
    bo = mk.get("blackout") if isinstance(mk.get("blackout"), dict) else None
    return {
        "market": {"present": bool(mk), "risk_off": mk.get("risk_off") if isinstance(mk.get("risk_off"), bool) else None, "session": common.ident(mk.get("utc_session")),
                   "blackout_in_min": common.num(bo.get("in_min")) if bo else None, "blackout_mode": common.ident(bo.get("mode")) if bo else None},
        "hot": {"present": bool(hot), "n": len(hot.get("top") or []), "stale_s": hot.get("stale_s")},
        "struct": dict({"present": bool(st), "n": len(srows), "stale_s": st.get("stale_s"), "regimes": regs}, **_struct_file(root, now)),
        "intel": {"present": bool(intel), "age_s": _age((facts or {}).get("ts") if isinstance(facts, dict) else None, now), "stale_s": intel.get("stale_s"),
                  "blackout": bool(intel.get("blackout"))},
        "news_digest": {"present": bool(nd), "n": len(nd.get("events") or []), "age_s": _age((digest or {}).get("ts") if isinstance(digest, dict) else None, now),
                        "stale": nd.get("stale") is True},
        "ideas": {"present": bool(ideas), "active": len(ideas.get("active") or [])},
        "recent": {"n": len(b.get("recent") or [])},
    }


def _struct_file(root, now):
    """Структура рынка целиком (runtime/structure.json, а не урезанный блок досье): число монет в файле, возраст, монеты по запросу, ждущие загрузки. Только числа и тикеры."""
    d = common.read_json(paths.runtime("structure.json", root=root))
    syms = d.get("symbols") if isinstance(d, dict) and isinstance(d.get("symbols"), dict) else {}
    names = [k for k, v in syms.items() if isinstance(v, dict) and common.RE_SYMBOL.fullmatch(str(k))]
    try:
        from tb.marketdata import structure
        req = structure.read_requests(root, now)
    except Exception:
        req = []
    return {"file_n": len(names), "age_s": _age((d or {}).get("ts_epoch") if isinstance(d, dict) else None, now), "syms": sorted(names)[:24],
            "requested": req, "requested_wait": [x for x in req if x not in names]}


def _dayplan(root):
    """-> {"day_id","max_new_plans","regime","risks":[коды],"source"} из плана дня (только числа и коды-идентификаторы) или None."""
    plat = common.read_json(paths.control_dir("platform.json", root=root))
    did = (plat or {}).get("day_id") if isinstance(plat, dict) else None
    if not isinstance(did, str):
        return None
    try:
        from tb.lifecycle import days
        d = common.read_json(days.day_dir(root, did) + "/dayplan.json")
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    lim = d.get("limits") if isinstance(d.get("limits"), dict) else {}
    return {"day_id": common.ident(did), "max_new_plans": common.num(lim.get("max_new_plans")), "regime": common.ident(d.get("regime")),
            "risks": [x for x in (d.get("risks") or []) if isinstance(x, str) and common.RE_IDENT.match(x)][:5],
            "source": common.ident((d.get("meta") or {}).get("source")) if isinstance(d.get("meta"), dict) else None}


def _last_job(root):
    from tb.brain import jobs
    rows = [j for j in jobs.list_jobs(root) if j.get("status") in ("done", "skipped", "failed")]
    if not rows:
        return None
    j = max(rows, key=lambda x: common.num(x.get("done_ts"), common.num(x.get("ts_last"), 0)) or 0)
    res = j.get("result") if isinstance(j.get("result"), dict) else {}
    roles = [r for r in (res.get("roles") or []) if isinstance(r, dict)]
    r0 = roles[0] if roles else {}
    return {"role": common.ident(r0.get("role")), "ts": common.num(j.get("done_ts"), common.num(j.get("ts_last"))), "status": common.ident(j.get("status")),
            "outcome": common.ident(res.get("outcome")), "code": common.ident(res.get("code") or r0.get("code")), "types": [t for t in (j.get("types") or []) if isinstance(t, str)][:4]}


def _next_wake(root):
    """Карта «роль -> типы» и лимиты из подписки api и brain.json (то, что реально действует), без правки и без записи."""
    out = {"roles": {}, "jobs_per_hour": None, "class_jobs_per_hour": None, "role_jobs_per_hour": None, "subscriber_enabled": None, "kinds": None}
    try:
        from tb.core import localcfg
        for it in (localcfg.read("subscriptions", root).get("subscribers") or []):
            if isinstance(it, dict) and it.get("id") == "api":
                rate = it.get("rate") if isinstance(it.get("rate"), dict) else {}
                out["subscriber_enabled"] = it.get("enabled") is True
                out["roles"] = {r: [t for t in ts if isinstance(t, str)] for r, ts in (it.get("roles") or {}).items() if isinstance(ts, list)}
                out["jobs_per_hour"] = rate.get("jobs_per_hour")
                out["class_jobs_per_hour"] = rate.get("class_jobs_per_hour")
                out["kinds"] = (it.get("filter") or {}).get("kinds")
    except Exception:
        pass
    try:
        from tb.brain import conf as conf_mod
        c = conf_mod.load(root)
        out["role_jobs_per_hour"] = (c.runtime.get("guards") or {}).get("role_jobs_per_hour")
    except Exception:
        pass
    return out


def watch(root, now):
    from tb.brain import brief as brief_mod, fallback, jobs
    try:
        b = brief_mod.build("start", root=root, now=now)
    except Exception:
        b = None
    b = b or {}
    blocks = _blocks(b, root, now)
    hp = fallback.Health(root, lambda: now)
    protect = hp.protect if hp.protect_on else None
    dp = _dayplan(root)
    positions, plans = len(b.get("positions") or []), len(b.get("plans") or [])
    bl = blocks["market"]["blackout_in_min"]
    busy = positions or plans or jobs.count_open(root)
    if protect and protect.get("code") == "brain_down":
        code, detail = "brain_down", {"since": protect.get("since")}
    elif protect:
        code, detail = "protect_only", {"code": common.ident(protect.get("code"))}
    elif bl is not None and bl <= 60:
        code, detail = "blackout", {"in_min": int(bl)}
    elif dp and dp.get("max_new_plans") == 0:
        code, detail = "blocked_by_dayplan", {"regime": dp.get("regime"), "risks": dp.get("risks"), "source": dp.get("source")}
    elif busy:
        code, detail = "working", {"positions": positions, "plans": plans, "jobs_open": jobs.count_open(root)}
    elif not blocks["ideas"]["active"]:
        code, detail = "waiting_targets", {"hot": blocks["hot"]["n"]}
    else:
        code, detail = "working", {"ideas": blocks["ideas"]["active"]}
    text = TEXTS[code] % {"hot": blocks["hot"]["n"], "in_min": int(bl or 0)}
    return {"ts": round(now, 1), "brief": {"id": common.ident(b.get("brief_id")), "ts": b.get("ts") if isinstance(b.get("ts"), str) else None, "built": bool(b),
                                          "trunc": bool(b.get("trunc"))},
            "state": {"code": code, "text": text, "detail": detail}, "blocks": blocks, "dayplan": dp, "last_job": _last_job(root), "next_wake": _next_wake(root)}


def api(path, query, root, now):
    """Контракт реестра EXT_API (dash/server.py): None, если путь не наш."""
    if path != PATH:
        return None
    now = time.time() if now is None else now
    try:
        return 200, watch(root, now)
    except Exception as e:            # карточка не должна ронять дашборд; без путей и текста ошибки
        return 500, {"error": "watch_failed", "kind": type(e).__name__}
