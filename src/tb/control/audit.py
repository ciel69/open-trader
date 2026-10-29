"""Журнал аудита плоскости управления `state/control/audit.jsonl`: append-only, хеш-цепочка (как decisions), запись под блокировкой. Владелец: WP12.

Запись: {"seq","ts","t","id","src","sess","action","args_digest","result","code","actor":"owner","ip":"127.0.0.1","prev","h"} (+ необязательно "warnings_accepted", "note").
Пишут и дашборд (отказы на входе, KILL), и сервис ctl; в журнал попадает КАЖДАЯ команда, включая отклонённые и с неверной подписью. Секретов в записи нет.
"""
import hashlib
import json
import os
import time

from tb import paths
from tb.control import protocol
from tb.core.filelock import FileLock

GENESIS = "0" * 16
OPTIONAL = ("warnings_accepted", "note", "mode")
RESULTS = ("ok", "rejected", "failed", "expired", "running", "info")


def _hash(prev, rec):
    body = dict((k, v) for k, v in rec.items() if k != "h")
    return hashlib.sha256((prev + protocol.canon(body)).encode("utf-8")).hexdigest()[:16]


def _parse(line):
    try:
        r = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
    except ValueError:
        return None
    return r if isinstance(r, dict) and isinstance(r.get("seq"), int) and isinstance(r.get("h"), str) else None


def _tail(path):
    last_seq, last_h, nl = 0, GENESIS, True
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return last_seq, last_h, nl
    if not data:
        return last_seq, last_h, nl
    nl = data.endswith(b"\n")
    for ln in reversed(data.split(b"\n")):
        r = _parse(ln)
        if r is not None:
            return r["seq"], r["h"], nl
    return last_seq, last_h, nl


def append(root, cid, src, sess, action, digest, result, code, now=None, **extra):
    """-> seq. Не бросает исключений наружу (аудит не должен ронять сервис): при сбое записи -> 0."""
    now = time.time() if now is None else now
    rec = {"t": round(now, 3), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "id": str(cid or "-")[:40], "src": str(src or "-")[:8],
           "sess": str(sess or "-")[:16], "action": str(action or "-")[:32], "args_digest": str(digest or "-")[:64],
           "result": result if result in RESULTS else "info", "code": str(code or "-")[:20], "actor": "owner", "ip": "127.0.0.1"}
    for k in OPTIONAL:
        if extra.get(k):
            rec[k] = extra[k]
    path = paths.control_audit_file(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with FileLock(paths.control_dir("audit.lock", root=root), wait=5.0):
            seq, prev, nl = _tail(path)
            rec["seq"], rec["prev"] = seq + 1, prev
            rec["h"] = _hash(prev, rec)
            with open(path, "ab") as f:
                f.write((("" if nl else "\n") + protocol.canon(rec) + "\n").encode("utf-8"))
        return rec["seq"]
    except (OSError, TimeoutError):
        return 0


def _iter(root):
    try:
        with open(paths.control_audit_file(root), "rb") as f:
            for ln in f:
                yield ln
    except OSError:
        return


def tail(root, n=20):
    res = []
    for ln in _iter(root):
        r = _parse(ln)
        if r is not None:
            res.append(r)
            if len(res) > n:
                res.pop(0)
    return res


def verify(root):
    """-> {"ok","n","bad_seq","broken"} (подмена, удаление или перестановка видны как несовпадение h/prev/seq)."""
    n, broken, prev_h, prev_seq = 0, 0, GENESIS, 0
    for ln in _iter(root):
        if not ln.strip():
            continue
        r = _parse(ln)
        if r is None:
            broken += 1
            continue
        if not (r.get("prev") == prev_h and r["seq"] == prev_seq + 1 and r["h"] == _hash(prev_h, r)):
            return {"ok": False, "n": n, "bad_seq": r["seq"], "broken": broken}
        n, prev_h, prev_seq = n + 1, r["h"], r["seq"]
    return {"ok": True, "n": n, "bad_seq": None, "broken": broken}
