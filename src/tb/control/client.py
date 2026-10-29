"""Сторона дашборда и CLI: положить подписанную команду в очередь и прочитать результат. Владелец: WP12.

Единственная запись на диск, которую делает этот модуль: файл команды в `paths.control_inbox()` (функция `submit`). Статус, результаты и аудит только читаются.
Биржевого кода, процессов и сети здесь нет (тест `test_layering`).
"""
import json
import os
import time

from tb import fsutil, paths
from tb.control import audit, auth, ctlstate, protocol

# то, что нужно дашборду от протокола (дашборду разрешены только client и auth)
ACTIONS = protocol.ACTIONS
CODES = protocol.CODES
RE_ID = protocol.RE_ID
canon = protocol.canon
args_digest = protocol.args_digest
norm_phrase = protocol.norm_phrase
validate_args = protocol.validate_args
load_cfg = ctlstate.load_cfg
audit_append = audit.append


def build(action, args, src="dash", sess=None, nonce=None, phrases=None, now=None, environ=None):
    """-> подписанная команда (словарь) или None, если ключа подписи нет."""
    now = time.time() if now is None else now
    cmd = {"schema": protocol.SCHEMA, "id": protocol.new_id(now), "ts": round(now, 3), "src": src, "action": action, "args": args,
           "nonce": nonce, "phrase_hash": protocol.phrase_hash(phrases) if phrases else None, "sess": sess}
    return auth.sign(cmd, environ)


def submit(cmd, root=None):
    """Кладёт команду в state/control/_inbox/<id>.json (tmp + замена). -> id."""
    d = paths.control_inbox(root=root)
    os.makedirs(d, exist_ok=True)
    final = os.path.join(d, cmd["id"] + ".json")
    tmp = os.path.join(d, ".%s.tmp" % cmd["id"])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False, separators=(",", ":"))
    fsutil.replace(tmp, final)
    return cmd["id"]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def read_status(root=None, now=None, stale_s=15.0):
    """status.json сервиса ctl + поля `age_s`, `stale` (ctl не отвечает: кнопки блокируются, кроме KILL). Нет файла: stale=True."""
    now = time.time() if now is None else now
    d = _read_json(paths.control_status_file(root))
    if d is None:
        return {"schema": "tb.ctlstatus/1", "ctl": {"alive": False}, "stale": True, "age_s": None, "platform": {"state": "unknown"}, "checks": [],
                "pending": [], "last": [], "banner": None}
    ts = d.get("ts")
    age = max(0.0, now - ts) if isinstance(ts, (int, float)) else None
    d = dict(d)
    d["age_s"] = None if age is None else round(age, 1)
    d["stale"] = age is None or age > stale_s
    if d["stale"]:
        d.setdefault("ctl", {})["alive"] = False
    return d


def read_result(cid, root=None):
    if not isinstance(cid, str) or not protocol.RE_ID.match(cid):
        return None
    return _read_json(paths.control_outbox(cid + ".json", root=root))


def wait_result(cid, root=None, timeout=4.0, poll=0.1, final=True, sleep=time.sleep, clock=time.time):
    """Ждёт результат команды (для challenge). final=True: до статуса done/rejected/failed/expired. -> результат или None по таймауту."""
    end = clock() + timeout
    while True:
        r = read_result(cid, root)
        if r is not None and (not final or r.get("status") in ("done", "rejected", "failed", "expired")):
            return r
        if clock() >= end:
            return r
        sleep(poll)


def audit_tail(root=None, n=10):
    return audit.tail(root, n)
