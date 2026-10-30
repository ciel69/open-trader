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

from tb import fsutil, paths
from tb.core.filelock import FileLock, atomic_write_json

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


# ---------------------------------------------------------------- сегменты (ротация)
# Активный файл state/decisions.jsonl растёт до MAX_ACTIVE_BYTES, затем под той же блокировкой переименовывается в закрытый сегмент
# state/decisions.d/seg-<seq первой записи>.jsonl, а запись продолжается в новом пустом активном файле. Цепочка хешей общая: первая запись нового
# файла ссылается (prev) на последний хеш сегмента, seq не прерывается. Закрытые сегменты не меняются и не удаляются (аудит).
# decisions.d/manifest.json: имя сегмента -> first_seq/last_seq/first_prev/last_h/n/size/mtime_ns (ускоряет verify(fast=True)).
MAX_ACTIVE_BYTES = 8 * 1024 * 1024
SEG_PREFIX, SEG_SUFFIX = "seg-", ".jsonl"


def segdir(root=None):
    return os.path.join(os.path.dirname(paths.decisions_file(root)), "decisions.d")


def segments(root=None):
    """-> [путь закрытого сегмента] по возрастанию seq первой записи."""
    d = segdir(root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith(SEG_PREFIX) and n.endswith(SEG_SUFFIX))
    except OSError:
        return []
    return [os.path.join(d, n) for n in names]


def _manifest_path(root=None):
    return os.path.join(segdir(root), "manifest.json")


def _manifest(root=None):
    try:
        with open(_manifest_path(root), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _first_record(path):
    try:
        with open(path, "rb") as f:
            for ln in f:
                r = _parse(ln)
                if r is not None:
                    return r
    except OSError:
        pass
    return None


def _scan_segment(path):
    """-> {first_seq, last_seq, first_prev, last_h, n} закрытого сегмента (один проход) или None, если в нём нет целых записей."""
    first = last = None
    n = 0
    for ln in open(path, "rb"):
        r = _parse(ln)
        if r is None:
            continue
        if first is None:
            first = r
        last = r
        n += 1
    if first is None:
        return None
    return {"first_seq": first["seq"], "last_seq": last["seq"], "first_prev": first.get("prev"), "last_h": last["h"], "n": n}


def _rotate(root, path):
    """Закрыть активный файл в сегмент. Под блокировкой записи. Best effort: на Windows файл, открытый читателем, переименовать нельзя, тогда попробуем при следующей записи."""
    first = _first_record(path)
    if first is None:
        return False
    d = segdir(root)
    os.makedirs(d, exist_ok=True)
    name = "%s%012d%s" % (SEG_PREFIX, first["seq"], SEG_SUFFIX)
    dst = os.path.join(d, name)
    if os.path.exists(dst):
        return False
    try:
        fsutil.replace(path, dst)
    except OSError:
        return False
    try:
        info = _scan_segment(dst)
        if info is not None:
            st = os.stat(dst)
            info.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            m = _manifest(root)
            m[name] = info
            atomic_write_json(_manifest_path(root), m, indent=None)
    except OSError:
        pass            # манифест вторичен: verify без него проверит сегмент полностью
    return True


def _head(root, path):
    """-> (last_seq, last_h, ends_with_newline) всей цепочки: хвост активного файла, а если он пуст (только что ротирован), хвост последнего сегмента."""
    seq, h, nl = _tail(path)
    if seq == 0:
        for p in reversed(segments(root)):
            s, h2, _nl = _tail(p)
            if s > 0:
                return s, h2, nl
    return seq, h, nl


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
        try:
            if os.path.getsize(path) >= MAX_ACTIVE_BYTES:
                _rotate(root, path)
        except OSError:
            pass
        last_seq, last_h, nl = _head(root, path)
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
    """Строки всей цепочки: закрытые сегменты по порядку, затем активный файл. Список сегментов и дескриптор активного файла берутся одновременно под
    блокировкой записи: ротация между ними не потеряет и не продублирует записи (на POSIX дескриптор указывает на тот же файл, на Windows ротация откладывается)."""
    try:
        with FileLock(paths.decisions_lock(root), wait=LOCK_WAIT_S):
            segs = segments(root)
            try:
                f = open(paths.decisions_file(root), "rb")
            except OSError:
                f = None
    except TimeoutError:
        segs, f = segments(root), None
        try:
            f = open(paths.decisions_file(root), "rb")
        except OSError:
            pass
    try:
        for p in segs:
            try:
                sf = open(p, "rb")
            except OSError:
                continue
            with sf:
                for ln in sf:
                    yield ln
        if f is not None:
            for ln in f:
                yield ln
    finally:
        if f is not None:
            f.close()


def _rev_lines(path, block=65536):
    """Строки файла с конца (от последней к первой), чтение блоками: для «последних N записей» не нужно читать весь файл."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        pos = f.seek(0, 2)
        buf = b""
        while pos > 0:
            start = max(0, pos - block)
            f.seek(start)
            buf = f.read(pos - start) + buf
            pos = start
            parts = buf.split(b"\n")
            buf = parts[0]
            for ln in reversed(parts[1:]):
                if ln.strip():
                    yield ln
        if buf.strip():
            yield buf


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
    if n <= 0:
        return res
    for p in [paths.decisions_file(root)] + list(reversed(segments(root))):      # с конца: от нового к старому, пока не наберётся n
        for ln in _rev_lines(p):
            r = _parse(ln)
            if r is None or (plan is not None and r.get("plan") != plan):
                continue
            res.append(r)
            if len(res) >= n:
                break
        if len(res) >= n:
            break
    res.reverse()
    return res


def verify(root=None, fast=False):
    """-> {"ok":bool,"n":int,"bad_seq":int|None,"broken":int}. Проверяет prev/h/seq всех целых записей.

    Оборванные строки (не JSON) пропускаются и считаются в `broken`; подмена содержимого, удаление или перестановка записей
    видны как несовпадение h/prev/seq у первой затронутой записи.

    fast=True: закрытые сегменты с записью в manifest.json и совпавшими размером и временем изменения не перечитываются, проверяется только стык
    цепочки (first_prev/first_seq/last_h); активный файл проверяется полностью. Сегмент без манифеста или изменившийся проверяется целиком."""
    if fast:
        return _verify_fast(root)
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


def _verify_lines(lines, prev_h, prev_seq, n, broken):
    """Полная проверка потока строк от заданного звена цепочки -> (результат|None, prev_h, prev_seq, n, broken)."""
    for ln in lines:
        if not ln.strip():
            continue
        r = _parse(ln)
        if r is None:
            broken += 1
            continue
        if not (r.get("prev") == prev_h and r["seq"] == prev_seq + 1 and r["h"] == _hash(prev_h, r)):
            return {"ok": False, "n": n, "bad_seq": r["seq"], "broken": broken}, prev_h, prev_seq, n, broken
        n += 1
        prev_h, prev_seq = r["h"], r["seq"]
    return None, prev_h, prev_seq, n, broken


def _verify_fast(root=None):
    man = _manifest(root)
    n = broken = 0
    prev_h, prev_seq = GENESIS, 0
    with FileLock(paths.decisions_lock(root), wait=LOCK_WAIT_S):
        segs = segments(root)
        try:
            af = open(paths.decisions_file(root), "rb")
        except OSError:
            af = None
        try:
            for p in segs:
                name = os.path.basename(p)
                m = man.get(name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if (isinstance(m, dict) and m.get("size") == st.st_size and m.get("mtime_ns") == st.st_mtime_ns and m.get("first_prev") == prev_h
                        and m.get("first_seq") == prev_seq + 1 and isinstance(m.get("n"), int)):
                    n += m["n"]
                    prev_h, prev_seq = m["last_h"], m["last_seq"]
                    continue
                with open(p, "rb") as sf:
                    bad, prev_h, prev_seq, n, broken = _verify_lines(sf, prev_h, prev_seq, n, broken)
                if bad is not None:
                    return bad
            if af is not None:
                bad, prev_h, prev_seq, n, broken = _verify_lines(af, prev_h, prev_seq, n, broken)
                if bad is not None:
                    return bad
        finally:
            if af is not None:
                af.close()
    return {"ok": True, "n": n, "bad_seq": None, "broken": broken}
