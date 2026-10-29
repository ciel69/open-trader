"""Журнал решений `state/decisions.jsonl` (03_brain.md п. 3.2; контракт 06 п. 6.6). Владелец: WP4.

Append-only, одна JSON-строка на запись, запись под блокировкой `state/decisions.lock`, `seq` монотонный, хеш-цепочка `prev`/`h`
(h = sha256(prev + канонический JSON записи без h)[:16]; первая запись: prev = GENESIS). Свободный текст только в `why` у мозга
(<= 200 символов, очищается от управляющих символов).

Устойчивость к обрыву: если процесс умер посреди строки, в файле остаётся хвост без перевода строки. Следующая запись начинается с
"\n" (битая строка остаётся как есть и пропускается при чтении), цепочка продолжается от последней ЦЕЛОЙ записи.
"""
import hashlib
import json
import os
import re
import time

from tb import paths
from tb.core.filelock import FileLock

ACTORS = ("brain:chat", "brain:api", "exec", "owner", "system")
RE_BRAIN_ACTOR = re.compile(r"^brain:[a-z][a-z0-9_-]{0,9}$")   # WP11-A: агент шлюза = brain:<id агента> (дашборд группирует по префиксу brain)


def valid_actor(actor):
    """actor из ACTORS либо brain:<id агента шлюза> (до 16 символов целиком, как показывает дашборд)."""
    return actor in ACTORS or (isinstance(actor, str) and bool(RE_BRAIN_ACTOR.fullmatch(actor)))
KINDS = ("cmd", "accept", "reject", "transition", "intent", "result", "rule", "skip", "protect", "reconcile", "event", "note")
# обязательные ключи записи (остальные необязательны и опускаются, если пусты)
REQUIRED_KEYS = ("seq", "ts", "t", "actor", "kind", "what", "prev", "h")
OPTIONAL_KEYS = ("plan", "rev", "args", "why", "basis", "res", "action_id", "profile_hash")
RES_CODES_EXAMPLES = ("OK", "DENY_GUARD", "STOP_NOT_TIGHTER", "NOT_TIGHTER", "ADD_DISABLED")
DEFAULT_LIMIT = 500
GENESIS = "0" * 16
WHY_MAX = 200
LOCK_WAIT_S = 10.0


def _canon(rec):
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev, rec):
    body = dict((k, v) for k, v in rec.items() if k != "h")
    return hashlib.sha256((prev + _canon(body)).encode("utf-8")).hexdigest()[:16]


def _clean_why(s):
    s = "".join(c if (c >= " " and c != "\x7f") else " " for c in str(s))
    return " ".join(s.split())[:WHY_MAX]


def _parse(line):
    """-> запись (dict с int seq и str h) или None (мусор, оборванная строка)."""
    line = line.strip()
    if not line:
        return None
    try:
        r = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
    except ValueError:
        return None
    if isinstance(r, dict) and isinstance(r.get("seq"), int) and isinstance(r.get("h"), str):
        return r
    return None


def _tail(path):
    """-> (last_seq, last_h, ends_with_newline). Читает файл с конца блоками, пока не найдёт целую запись."""
    try:
        f = open(path, "rb")
    except OSError:
        return 0, GENESIS, True
    with f:
        size = f.seek(0, 2)
        if size == 0:
            return 0, GENESIS, True
        f.seek(size - 1)
        nl = f.read(1) == b"\n"
        chunk = 16384
        while True:
            start = max(0, size - chunk)
            f.seek(start)
            data = f.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]       # первая строка блока может быть неполной
            for ln in reversed(lines):
                r = _parse(ln)
                if r is not None:
                    return r["seq"], r["h"], nl
            if start == 0:
                return 0, GENESIS, nl
            chunk *= 4


def append(rec, root=None, fsync=False):
    """-> seq (под блокировкой; считает prev/h). ValueError при неверном actor/kind/what или неизвестных ключах."""
    if not isinstance(rec, dict):
        raise ValueError("запись журнала решений должна быть dict")
    if not valid_actor(rec.get("actor")):
        raise ValueError("decisions: actor %r не из %s" % (rec.get("actor"), list(ACTORS)))
    if rec.get("kind") not in KINDS:
        raise ValueError("decisions: kind %r не из %s" % (rec.get("kind"), list(KINDS)))
    if not rec.get("what") or not isinstance(rec["what"], str):
        raise ValueError("decisions: what обязателен (строка)")
    allowed = set(REQUIRED_KEYS) | set(OPTIONAL_KEYS)
    bad = [k for k in rec if k not in allowed]
    if bad:
        raise ValueError("decisions: неизвестные ключи %s (разрешены %s)" % (sorted(bad), sorted(allowed)))
    out = {}
    for k in ("actor", "kind", "what") + OPTIONAL_KEYS:
        v = rec.get(k)
        if k in OPTIONAL_KEYS and (v is None or v == "" or v == {} or v == []):
            continue
        out[k] = v
    if "why" in out:
        out["why"] = _clean_why(out["why"])
        if not out["why"]:
            del out["why"]
    t = rec.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        t = time.time()
    out["t"] = round(float(t), 3)
    out["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    path = paths.decisions_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(paths.decisions_lock(root), wait=LOCK_WAIT_S):
        last_seq, last_h, nl = _tail(path)
        out["seq"] = last_seq + 1
        out["prev"] = last_h
        out["h"] = _hash(last_h, out)
        data = ("" if nl else "\n") + _canon(out) + "\n"
        with open(path, "ab") as f:
            f.write(data.encode("utf-8"))
            f.flush()
            if fsync:
                os.fsync(f.fileno())
    return out["seq"]


def _iter(root=None):
    try:
        f = open(paths.decisions_file(root), "rb")
    except OSError:
        return
    with f:
        for ln in f:
            yield ln


def read(after_seq=0, plan=None, limit=DEFAULT_LIMIT, root=None):
    """-> [запись] по возрастанию seq (первые `limit` после after_seq); битые строки пропускаются."""
    res = []
    for ln in _iter(root):
        r = _parse(ln)
        if r is None or r["seq"] <= after_seq:
            continue
        if plan is not None and r.get("plan") != plan:
            continue
        res.append(r)
        if limit and len(res) >= limit:
            break
    return res


def last(n=20, plan=None, root=None):
    """-> последние n записей (по возрастанию seq), при необходимости только плана."""
    res = []
    for ln in _iter(root):
        r = _parse(ln)
        if r is None or (plan is not None and r.get("plan") != plan):
            continue
        res.append(r)
        if len(res) > n:
            res.pop(0)
    return res


def verify(root=None):
    """-> {"ok":bool,"n":int,"bad_seq":int|None,"broken":int}. Проверяет prev/h/seq всех целых записей.

    Оборванные строки (не JSON) пропускаются и считаются в `broken`; подмена содержимого, удаление или перестановка записей
    видны как несовпадение h/prev/seq у первой затронутой записи."""
    n = 0
    broken = 0
    prev_h = GENESIS
    prev_seq = 0
    for ln in _iter(root):
        if not ln.strip():
            continue
        r = _parse(ln)
        if r is None:
            broken += 1
            continue
        ok = (r.get("prev") == prev_h and r["seq"] == prev_seq + 1 and r["h"] == _hash(prev_h, r))
        if not ok:
            return {"ok": False, "n": n, "bad_seq": r["seq"], "broken": broken}
        n += 1
        prev_h = r["h"]
        prev_seq = r["seq"]
    return {"ok": True, "n": n, "bad_seq": None, "broken": broken}
