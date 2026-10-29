"""Чтение для дашборда (EXT_API WP12): /api/day/status, /api/day/list, /api/day/report?day=ID. Только чтение файлов `state/days/*`; кнопки идут через WP12 (ctl). WP14."""
import os

from tb.control import ctlstate
from tb.lifecycle import days

MAX_LIST = 30


def _brief(d):
    rep = d.get("report") or {}
    return {"day_id": d.get("day_id"), "state": d.get("state"), "mode": d.get("mode"), "agent": d.get("agent"), "started": days.iso(d.get("started")),
            "stopped": days.iso(d.get("stopped")), "stop_source": d.get("stop_source"), "verdict": rep.get("verdict"), "net": rep.get("net"), "carried_n": rep.get("carried_n"),
            "questions_n": rep.get("questions_n"), "acked": bool(rep.get("acked")), "report_ready": bool(rep.get("ready")), "needs_resume": bool(d.get("needs_resume")),
            "abort": d.get("abort")}


def _read(root, day_id, name):
    return ctlstate.read_json(os.path.join(days.day_dir(root, day_id), name))


def status(root, now):
    cur = days.current(root)
    last = None
    for i in sorted(days.list_ids(root), key=days._key, reverse=True)[:1]:
        last = days.load(root, i)
    out = {"now": now, "banner": days.banner(root), "current": None, "last": _brief(last) if last else None}
    d = cur or last
    if d:
        did = d["day_id"]
        out["current"] = dict(_brief(d), steps=[{k: s.get(k) for k in ("name", "phase", "ok", "dur_s", "code", "detail")} for s in d.get("steps") or []],
                              questions=[{k: q.get(k) for k in ("id", "plan", "text", "options", "answer", "answered_by")} for q in (d.get("owner") or {}).get("questions") or []],
                              deadline_ts=(d.get("owner") or {}).get("deadline_ts"), closing=dict((k, v) for k, v in (d.get("closing") or {}).items()
                                                                                            if k in ("source", "auto_reason", "failed", "manual", "emergency")),
                              dayplan=d.get("dayplan"), facts=d.get("facts"), preflight=d.get("preflight"),
                              checklist=(_read(root, did, "checklist.json") or {}).get("rows") or [], carry=(_read(root, did, "carry.json") or {}).get("items") or [])
    return out


def listing(root):
    out = []
    for i in sorted(days.list_ids(root), key=days._key, reverse=True)[:MAX_LIST]:
        d = days.load(root, i)
        if d:
            out.append(_brief(d))
    return {"days": out}


def report(root, day_id):
    if not days.valid_id(day_id):
        return None
    rep = _read(root, day_id, "report.json")
    if not isinstance(rep, dict):
        return None
    try:
        with open(os.path.join(days.day_dir(root, day_id), "report.md"), encoding="utf-8") as f:
            md = f.read(200000)
    except OSError:
        md = ""
    return {"report": rep, "md": md}


def api(path, query, root, now):
    """-> None (не мой путь) | (код, объект)."""
    if path == "/api/notify/status":
        from tb.lifecycle import notifycfg
        return 200, notifycfg.view(root, now)
    if not path.startswith("/api/day/"):
        return None
    if path == "/api/day/status":
        return 200, status(root, now)
    if path == "/api/day/list":
        return 200, listing(root)
    if path == "/api/day/report":
        r = report(root, ((query or {}).get("day") or [""])[0])
        return (200, r) if r else (404, {"error": "нет отчёта"})
    return 404, {"error": "unknown endpoint"}
