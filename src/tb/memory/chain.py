"""Журнал jsonl с хеш-цепочкой (как decisions.jsonl): seq монотонный, prev/h = sha256(prev + канонический JSON без h)[:16].

Запись под FileLock; оборванный хвост (процесс умер посреди строки): следующая запись начинается с перевода строки, битая строка пропускается
при чтении и считается в verify (broken); цепочка продолжается от последней ЦЕЛОЙ записи. Подмена, удаление, перестановка видны в verify.
"""
import hashlib
import json
import os

from tb.core.filelock import FileLock

GENESIS = "0" * 16
LOCK_WAIT_S = 10.0


def canon(rec):
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_hash(prev, rec):
    return hashlib.sha256((prev + canon(dict((k, v) for k, v in rec.items() if k != "h"))).encode("utf-8")).hexdigest()[:16]


def parse(line):
    line = line.strip()
    if not line:
        return None
    try:
        r = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
    except ValueError:
        return None
    return r if isinstance(r, dict) and isinstance(r.get("seq"), int) and isinstance(r.get("h"), str) else None


def tail(path):
    """-> (last_seq, last_h, ends_with_newline)."""
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
            lines = f.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]
            for ln in reversed(lines):
                r = parse(ln)
                if r is not None:
                    return r["seq"], r["h"], nl
            if start == 0:
                return 0, GENESIS, nl
            chunk *= 4


def read(path):
    """-> (записи по порядку файла, число битых строк)."""
    rows, broken = [], 0
    try:
        f = open(path, "rb")
    except OSError:
        return rows, broken
    with f:
        for ln in f:
            if not ln.strip():
                continue
            r = parse(ln)
            if r is None:
                broken += 1
            else:
                rows.append(r)
    return rows, broken


def append(path, lock_path, rec, check=None, max_bytes=None, fsync=False):
    """Дописывает rec (без seq/prev/h) под блокировкой -> готовая запись. check() вызывается под блокировкой (может бросить исключение)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(lock_path, wait=LOCK_WAIT_S):
        if check is not None:
            check()
        seq, last_h, nl = tail(path)
        out = dict(rec)
        out["seq"], out["prev"] = seq + 1, last_h
        out["h"] = make_hash(last_h, out)
        line = canon(out)
        if max_bytes and len(line.encode("utf-8")) > max_bytes:
            raise ValueError("строка %d байт > %d" % (len(line.encode("utf-8")), max_bytes))
        with open(path, "ab") as f:
            f.write((("" if nl else "\n") + line + "\n").encode("utf-8"))
            f.flush()
            if fsync:
                os.fsync(f.fileno())
    return out


def verify(path):
    """-> {"ok","n","bad_seq","broken"}."""
    n = broken = 0
    prev_h, prev_seq = GENESIS, 0
    try:
        f = open(path, "rb")
    except OSError:
        return {"ok": True, "n": 0, "bad_seq": None, "broken": 0}
    with f:
        for ln in f:
            if not ln.strip():
                continue
            r = parse(ln)
            if r is None:
                broken += 1
                continue
            if not (r.get("prev") == prev_h and r["seq"] == prev_seq + 1 and r["h"] == make_hash(prev_h, r)):
                return {"ok": False, "n": n, "bad_seq": r["seq"], "broken": broken}
            n += 1
            prev_h, prev_seq = r["h"], r["seq"]
    return {"ok": True, "n": n, "bad_seq": None, "broken": broken}
