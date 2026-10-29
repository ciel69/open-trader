"""Хранилище дня и машина состояний (07 п. 2.1, 2.8): `state/days/<D-id>/day.json`, шаги с записью ДО и ПОСЛЕ, торговый флаг, события шины. WP14.

Состояния: stopped / starting / running / closing / halted. Параллельный день невозможен. Шаги идемпотентны: при возобновлении выполняются те,
у которых нет ok=true. Запись day.json атомарная (core.filelock.atomic_write_json) и под процессной блокировкой.
"""
import os
import re
import threading
import time

from tb import paths
from tb.control import ctlstate
from tb.core import bus

SCHEMA = "tb.day/1"
STATES = ("stopped", "starting", "running", "closing", "halted")
TRANSITIONS = {"stopped": ("starting",), "starting": ("running", "stopped", "halted"), "running": ("closing", "halted"),
               "closing": ("stopped", "halted"), "halted": ("stopped",)}
RE_DAY = re.compile(r"^D-\d{8}-\d{1,4}$")
_LOCK = threading.RLock()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if isinstance(ts, (int, float)) else None


def days_dir(root):
    return paths.state("days", root=root)


def day_dir(root, day_id):
    return paths.state("days", day_id, root=root)


def day_file(root, day_id):
    return os.path.join(day_dir(root, day_id), "day.json")


def valid_id(day_id):
    return isinstance(day_id, str) and bool(RE_DAY.match(day_id))


def new_day_id(root, now):
    stamp = ctlstate.utc_day(now).replace("-", "")
    n = 0
    try:
        for name in os.listdir(days_dir(root)):
            m = re.match(r"^D-%s-(\d+)$" % stamp, name)
            if m:
                n = max(n, int(m.group(1)))
    except OSError:
        pass
    return "D-%s-%d" % (stamp, n + 1)


def load(root, day_id):
    if not valid_id(day_id):
        return None
    d = ctlstate.read_json(day_file(root, day_id))
    return d if isinstance(d, dict) else None


def save(root, d):
    with _LOCK:
        ctlstate.write_json(day_file(root, d["day_id"]), d)


def list_ids(root):
    try:
        return sorted(n for n in os.listdir(days_dir(root)) if valid_id(n))
    except OSError:
        return []


def _key(day_id):
    m = RE_DAY.match(day_id)
    return (day_id[2:10], int(day_id.split("-")[2])) if m else ("", 0)


def current(root):
    """Незавершённый день (state != stopped) среди трёх последних по номеру либо None."""
    for i in sorted(list_ids(root), key=_key, reverse=True)[:3]:
        d = load(root, i)
        if d and d.get("state") != "stopped":
            return d
    return None


def last_day(root, exclude=None):
    for i in sorted(list_ids(root), key=_key, reverse=True):
        if i != exclude:
            d = load(root, i)
            if d:
                return d
    return None


def create(root, args, now, agent=None):
    """Новый день в состоянии starting. -> d."""
    with _LOCK:
        day_id = new_day_id(root, now)
        d = {"schema": SCHEMA, "day_id": day_id, "state": "starting", "phase": "start", "mode": args.get("mode"), "agent": agent, "team": args.get("team"),
             "session": {"type": args.get("session"), "id": None}, "live_waivers": list(args.get("waivers") or []),
             "args": {k: args.get(k) for k in ("mode", "session", "team", "allow_amber", "accepted") if k in args},
             "started": round(now, 1), "stopped": None, "stop_source": None, "abort": None, "steps": [], "runner": {"pid": os.getpid(), "hb": round(now, 1)},
             "brief_id": None, "carry_in": [], "owner": {"questions": [], "deadline_ts": None}, "closing": {}, "report": {"ready": False, "acked": False},
             "needs_resume": False}
        save(root, d)
        return d


def touch(root, d, now):
    d["runner"] = {"pid": os.getpid(), "hb": round(now, 1)}
    save(root, d)


def runner_stale(d, now, stale_s):
    hb = (d.get("runner") or {}).get("hb")
    return not isinstance(hb, (int, float)) or now - hb > stale_s


def set_state(root, d, state, now, code=None, step=None):
    """Переход по таблице (недопустимый игнорируется -> False) + запись + событие шины day_state."""
    if state == d.get("state"):
        return True
    if state not in TRANSITIONS.get(d.get("state"), ()):
        return False
    d["state"] = state
    if state == "stopped":
        d["stopped"] = round(now, 1)
    save(root, d)
    emit("day_state", {"state": state, "step": step, "code": code}, root, now)
    return True


# ------------------------------------------------------------------ шаги
def get_step(d, name, phase):
    for s in d.get("steps") or []:
        if s.get("name") == name and s.get("phase") == phase:
            return s
    return None


def step_begin(root, d, name, phase, now):
    """Запись ДО выполнения (ok=None). Повторный вызов обновляет ту же запись."""
    s = get_step(d, name, phase)
    if s is None:
        s = {"name": name, "phase": phase, "ok": None, "ts": round(now, 1), "dur_s": None, "detail": None, "code": None, "tries": 0}
        d.setdefault("steps", []).append(s)
    s["tries"] = int(s.get("tries") or 0) + 1
    s["ok"], s["ts"] = None, round(now, 1)
    save(root, d)
    return s


def step_end(root, d, name, phase, ok, now, detail=None, code=None):
    s = get_step(d, name, phase)
    if s is None:
        s = step_begin(root, d, name, phase, now)
    s["ok"] = ok
    s["dur_s"] = round(max(0.0, now - (s.get("ts") or now)), 2)
    s["detail"] = detail if isinstance(detail, (str, dict, list, int, float)) or detail is None else str(detail)
    s["code"] = code
    save(root, d)
    return s


# ------------------------------------------------------------------ торговый флаг, шина, баннер
def set_trading(root, enabled, d, until_ts=None):
    ctlstate.write_json(ctlstate.f_trading(root), {"day_id": d.get("day_id"), "enabled": bool(enabled), "mode": d.get("mode"), "agent": d.get("agent"), "until_ts": until_ts})


def emit(type_, data, root, now, pri=None, plan=None, sym=None):
    """Событие шины; сбой шины не ломает день."""
    try:
        clean = {k: v for k, v in (data or {}).items() if v is not None}
        bus.emit(type_, clean, plan=plan, sym=sym, pri=pri, root=root, now=now)
    except Exception:
        pass


def banner_file(root):
    return paths.control_dir("day_banner.json", root=root)


def set_banner(root, level, text, day_id=None, report=False):
    """Баннер дашборда (читает ctl status и вкладка «День»); report=True: «итог дня готов» до report.ack."""
    ctlstate.write_json(banner_file(root), {"level": level, "text": str(text)[:200], "day_report": day_id if report else None, "day_id": day_id})


def clear_banner(root):
    try:
        os.remove(banner_file(root))
    except OSError:
        pass


def banner(root):
    b = ctlstate.read_json(banner_file(root))
    return b if isinstance(b, dict) and b.get("text") else None
