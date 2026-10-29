"""Запись настроечных изменений из терминала (CLI провайдеров, профилей и ролей) в общий журнал аудита `state/control/audit.jsonl`.

Формат строки и хеш-цепочка те же, что у tb.control.audit (проверка `./tb control audit verify` видит эти записи): здесь нет импорта плоскости управления, поэтому слои
llm и brain могут писать аудит CLI, не зная ctl. Значения (ключи, тексты) в запись не попадают: только действие, хеш аргументов и результат.
"""
import hashlib
import json
import os
import time

from tb import paths
from tb.core.filelock import FileLock

GENESIS = "0" * 16


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(action, args):
    return hashlib.sha256((action + "\n" + canon(dict(args or {}))).encode("utf-8")).hexdigest()


def _hash(prev, rec):
    body = dict((k, v) for k, v in rec.items() if k != "h")
    return hashlib.sha256((prev + canon(body)).encode("utf-8")).hexdigest()[:16]


def _tail(path):
    seq, h, nl = 0, GENESIS, True
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return seq, h, nl
    nl = (not data) or data.endswith(b"\n")
    for ln in reversed(data.split(b"\n")):
        try:
            r = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("seq"), int) and isinstance(r.get("h"), str):
            return r["seq"], r["h"], nl
    return seq, h, nl


def append(root, action, args, ok, code="OK", src="cli", sess=None, now=None):
    """-> seq (0 при сбое записи: аудит не должен ронять операцию)."""
    now = time.time() if now is None else now
    rec = {"t": round(now, 3), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "id": "-", "src": str(src)[:8], "sess": str(sess or "-")[:16],
           "action": str(action)[:32], "args_digest": digest(action, args)[:64], "result": "ok" if ok else "rejected", "code": str(code or "-")[:20], "actor": "owner", "ip": "127.0.0.1"}
    path = paths.control_audit_file(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with FileLock(paths.control_dir("audit.lock", root=root), wait=5.0):
            seq, prev, nl = _tail(path)
            rec["seq"], rec["prev"] = seq + 1, prev
            rec["h"] = _hash(prev, rec)
            with open(path, "ab") as f:
                f.write((("" if nl else "\n") + canon(rec) + "\n").encode("utf-8"))
        return rec["seq"]
    except (OSError, TimeoutError):
        return 0
