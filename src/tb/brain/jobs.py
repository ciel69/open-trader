"""Очередь заданий API-мозга: `state/brain/jobs/J-*.json` (формат диспетчера, WP11-B; WP11-C2 берёт, ведёт и закрывает задания).

Поля диспетчера: id, sub, status, prio (0 высший), key, plan, sym, ts_first, ts_last, not_before, count, seqs, types, kinds, dropped_before.
Добавляет мозг: taken_ts, taken_by (pid), attempts, requeued, rev (ревизия назначений при взятии), retry_at, result {outcome, code, role, profile, usd, ...}, done_ts.
Статусы: new -> taken -> done | failed | expired | skipped (терминальные); aborted/новый перезапуск возвращают taken -> new (событие не подтверждено, задание повторит новая модель).
Запись файлов под тем же замком `jobs/.lock`, что у диспетчера; коалесцирование диспетчера идёт только в status=new, поэтому гонок с мозгом нет.
Подтверждение событий шины (`ack_through`): только до минимального seq среди незавершённых заданий, чтобы приоритетный порядок не «перепрыгнул» необработанное.
"""
import json
import os
import time

from tb import paths
from tb.brain import common
from tb.core.filelock import FileLock, atomic_write_json, pid_alive

TERMINAL = ("done", "failed", "expired", "skipped")
OPEN = ("new", "taken")
MAX_SEQS = 40


def jobs_dir(root=None):
    return paths.state("brain", "jobs", root=root)


def _lock(root):
    os.makedirs(jobs_dir(root), exist_ok=True)
    return FileLock(os.path.join(jobs_dir(root), ".lock"), wait=10.0)


def _path(root, jid):
    return os.path.join(jobs_dir(root), "%s.json" % jid)


def _read(root, name):
    d = common.read_json(os.path.join(jobs_dir(root), name))
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) else None


def _write(root, job):
    atomic_write_json(_path(root, job["id"]), job, indent=None)


def list_jobs(root=None, status=None):
    """Все задания (по возрастанию id), при status — только с таким статусом (строка или кортеж)."""
    try:
        names = sorted(n for n in os.listdir(jobs_dir(root)) if n.startswith("J-") and n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        j = _read(root, n)
        if j is None:
            continue
        if status is None or j.get("status") in ((status,) if isinstance(status, str) else status):
            out.append(j)
    return out


def count_open(root=None):
    return len(list_jobs(root, OPEN))


def _valid_seqs(j):
    s = j.get("seqs")
    return [x for x in s if isinstance(x, int) and not isinstance(x, bool)] if isinstance(s, list) else []


def _order(j):
    return (j.get("prio") if isinstance(j.get("prio"), int) else 9, j.get("ts_first") or 0, j["id"])


def pick(root, now, owner=None, busy_keys=(), max_attempts=3, ttl_s=900.0, rev=None):
    """Берёт ОДНО готовое задание (status new, not_before <= now, retry_at <= now, ключ не занят) -> job (уже taken) или None.
    Просроченные (старше ttl_s с последнего события) закрываются как expired. Дубли того же (sub,key) в статусе new сливаются в выбранное."""
    with _lock(root):
        rows = list_jobs(root, "new")
        ready = []
        for j in rows:
            last = j.get("ts_last") or j.get("ts_first") or 0
            if ttl_s and now - last > ttl_s:
                j.update(status="expired", done_ts=round(now, 1), result={"outcome": "expired", "code": "TTL"})
                _write(root, j)
                continue
            if (j.get("not_before") or 0) > now or (j.get("retry_at") or 0) > now or j.get("key") in busy_keys:
                continue
            if int(j.get("attempts") or 0) >= max_attempts:
                j.update(status="failed", done_ts=round(now, 1), result={"outcome": "failed", "code": "ATTEMPTS"})
                _write(root, j)
                continue
            ready.append(j)
        if not ready:
            return None
        ready.sort(key=_order)
        job = ready[0]
        for o in rows:           # слияние дублей (одна подписка и ключ, ещё не взятые)
            if o is job or o.get("status") != "new" or o.get("sub") != job.get("sub") or o.get("key") != job.get("key"):
                continue
            job["seqs"] = sorted(set(_valid_seqs(job) + _valid_seqs(o)))[-MAX_SEQS:]
            job["count"] = int(job.get("count") or 1) + int(o.get("count") or 1)
            job["types"] = list(job.get("types") or [])
            for t in o.get("types") or []:
                if t not in job["types"] and len(job["types"]) < 8:
                    job["types"].append(t)
            job["kinds"] = list(job.get("kinds") or [])
            for k in o.get("kinds") or []:
                if k not in job["kinds"] and len(job["kinds"]) < 8:
                    job["kinds"].append(k)
            job["prio"] = min(_order(job)[0], _order(o)[0])
            o.update(status="done", done_ts=round(now, 1), result={"outcome": "merged", "into": job["id"]})
            _write(root, o)
        job.update(status="taken", taken_ts=round(now, 1), taken_by=os.getpid(), owner=owner, attempts=int(job.get("attempts") or 0) + 1)
        if rev is not None:
            job["rev"] = rev
        _write(root, job)
        return job


def finish(root, job, status, result, now):
    """Закрывает взятое задание (done|failed|skipped|expired) или возвращает в очередь (status="new", result["retry_in_s"])."""
    with _lock(root):
        cur = _read(root, "%s.json" % job["id"]) or dict(job)
        cur["status"] = status
        cur["result"] = result
        if (result or {}).get("defer"):          # отложенное (нет профиля/бюджета/защита) не тратит попытки
            cur["attempts"] = max(0, int(cur.get("attempts") or 1) - 1)
        if status == "new":
            cur["retry_at"] = round(now + float((result or {}).get("retry_in_s") or 30.0), 1)
            cur["taken_by"] = None
        else:
            cur["done_ts"] = round(now, 1)
        _write(root, cur)
        return cur


def requeue_stale(root, now, stale_s=300.0, max_attempts=3, inflight=(), own_dead=True):
    """Рестарт демона: задания taken, чей владелец мёртв или давно молчит, возвращаются в new (или failed после max_attempts). -> число возвращённых.
    own_dead=True (старт демона): задания с нашим pid остались от прошлого запуска; в работе демона (own_dead=False) наши задания трогает только таймаут stale_s."""
    n = 0
    with _lock(root):
        for j in list_jobs(root, "taken"):
            if j["id"] in inflight:
                continue
            by = j.get("taken_by")
            dead = not (isinstance(by, int) and pid_alive(by)) or (own_dead and by == os.getpid())
            old = now - (j.get("taken_ts") or 0) > stale_s
            if not (dead or old):
                continue
            if int(j.get("attempts") or 0) >= max_attempts:
                j.update(status="failed", done_ts=round(now, 1), result={"outcome": "failed", "code": "ATTEMPTS"})
            else:
                j.update(status="new", taken_by=None, requeued=int(j.get("requeued") or 0) + 1, retry_at=None)
                n += 1
            _write(root, j)
    return n


def prune(root, now, keep_s=259200.0):
    """Удаляет терминальные задания старше keep_s. -> число."""
    n = 0
    for j in list_jobs(root, TERMINAL):
        if now - (j.get("done_ts") or j.get("ts_last") or 0) > keep_s:
            try:
                os.remove(_path(root, j["id"]))
                n += 1
            except OSError:
                pass
    return n


def ack_through(root, consumer, now=None):
    """Подтверждает в шине события завершённых заданий, но не дальше минимального seq незавершённых. -> новый acked или None."""
    from tb.core import bus
    rows = list_jobs(root)
    open_min = None
    done_max = 0
    for j in rows:
        sq = _valid_seqs(j)
        if not sq:
            continue
        if j.get("status") in OPEN:
            open_min = min(sq) if open_min is None else min(open_min, min(sq))
        elif j.get("status") in TERMINAL:
            done_max = max(done_max, max(sq))
    target = done_max if open_min is None else min(done_max, open_min - 1)
    target = min(target, bus.last_seq(root))
    if target <= 0 or target <= bus.cursor_get(consumer, root=root)["acked"]:
        return None
    try:
        return bus.ack(consumer, target, root=root, now=now)["acked"]
    except ValueError:
        return None


def stats(root=None):
    c = {}
    for j in list_jobs(root):
        c[j.get("status")] = c.get(j.get("status"), 0) + 1
    return c
