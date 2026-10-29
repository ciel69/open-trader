"""Диспетчер подписок: шина событий -> подписчики по реестру `config/subscriptions.json`. Владелец: WP11-B. CLI: `./tb dispatch ...`.

Читает шину СВОИМ курсором на каждого подписчика (`state/dispatch/subs/<id>.cursor.json`: что обработано; обрыв и перезапуск = повторная доставка с
последнего обработанного, доставки идемпотентны по seq). Шинный потребитель `dispatch` подтверждается минимумом по подписчикам.

Способы доставки:
  queue      очередь подписчика `state/dispatch/queues/<id>.jsonl` (+ `<id>.ack.json`). Агент забирает: `./tb dispatch queue <id> [--ack N]` или библиотекой
             `Queue(root, id).read()/.ack(qseq)/.stats()` (MCP get_events/get_status). Счётчики непрочитанного: `runtime/platform/unread.json`. Чат Claude Code
             НЕ будится (это невозможно): события ждут в очереди, непрочитанное видно в `./tb status [--line]`.
  api_agent  задание для API-мозга (WP11-C) файлом `state/brain/jobs/J-*.json` со status "new": коалесцирование серии событий по одному плану/символу
             в окне coalesce_s в одно задание, приоритет = лучший класс, потолок заданий в час (critical_per_hour для класса 0). Мозг берёт задание
             (status new -> taken -> done) и не раньше `not_before`.
  webhook    POST JSON {"event": {...}} на локальный URL (только localhost/127.0.0.1/::1, по умолчанию подписчик выключен); сбой = повторы с паузой, затем отказ.
  exec_cmd   НЕ поддерживается (запись в журнале «unsupported»).
Классы приоритета (0 высший): 0 связь/kill/лимиты/ошибки/service_down; 1 исполнение; 2 вехи позиции; 3 сигналы сканера; 4 идеи (config/platform.json dispatch.classes).
Журнал доставок `state/dispatch/deliveries.jsonl`: каждая доставка и каждый отказ (ok, dup, coalesced, rate, cap, failed, unsupported, rejected_config).
Только белые списки: в очередь и задания попадают поля событий шины (они уже очищены `bus.sanitize`), текста третьих лиц нет.
"""
import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request
from urllib.parse import urlparse

from tb import fsutil, paths
from tb.core import bus, localcfg
from tb.core.filelock import FileLock, atomic_write_json
from tb.platform import services as S

DELIVERIES = ("queue", "api_agent", "webhook", "exec_cmd")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
RE_SUB_ID = re.compile(r"^[a-z0-9_-]{1,20}$")
CONSUMER = "dispatch"
READ_BATCH = 200
LOG_MAX_BYTES = 1024 * 1024
RETRY = "retry"

DEFAULT_SUBSCRIBERS = [
    {"id": "chat", "enabled": True, "delivery": "queue", "filter": {"pri": "wake"}, "rate": {"max_per_hour": 120}},
    {"id": "api", "enabled": False, "delivery": "api_agent", "filter": {"pri": "wake", "max_class": 3}},
]


# ------------------------------------------------------------------ пути
def ddir(root=None, *parts):
    return paths.state("dispatch", *parts, root=root)


def subs_file(root=None):
    return paths.config("subscriptions.json", root=root)


def jobs_dir(root=None):
    return paths.state("brain", "jobs", root=root)


# ------------------------------------------------------------------ реестр подписчиков
def load_subscribers(root=None):
    """-> (подписчики, замечания). Нет файла -> умолчания; битая запись пропускается с замечанием; веб-хук не на localhost отвергается."""
    problems = []
    raw = None
    try:
        raw = localcfg.read("subscriptions", root)      # поставляемый файл + config/local/subscriptions.json (слияние по id)
    except FileNotFoundError:
        raw = {"subscribers": DEFAULT_SUBSCRIBERS}
    except (OSError, ValueError) as e:
        return [], ["subscriptions.json не прочитан: %s" % type(e).__name__]
    items = raw.get("subscribers") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], ["subscriptions.json: нужен объект {\"subscribers\": [...]}"]
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict) or not RE_SUB_ID.match(str(it.get("id") or "")):
            problems.append("подписчик без корректного id (a-z0-9_-, до 20)")
            continue
        sid = it["id"]
        if sid in seen:
            problems.append("%s: повторный id" % sid)
            continue
        seen.add(sid)
        dl = it.get("delivery")
        if dl not in DELIVERIES:
            problems.append("%s: неизвестный способ доставки %r" % (sid, dl))
            continue
        if dl == "webhook":
            u = urlparse(str(it.get("url") or ""))
            if u.scheme != "http" or (u.hostname or "").lower() not in LOCAL_HOSTS:
                problems.append("%s: webhook только http://localhost|127.0.0.1|[::1]; подписчик отвергнут" % sid)
                continue
        if dl == "exec_cmd":
            problems.append("%s: exec_cmd не поддерживается" % sid)
        sub = {"id": sid, "enabled": it.get("enabled", True) is True, "delivery": dl, "filter": it.get("filter") if isinstance(it.get("filter"), dict) else {},
               "rate": it.get("rate") if isinstance(it.get("rate"), dict) else {}, "url": it.get("url"), "start_at": it.get("start_at", "end")}
        out.append(sub)
    return out, problems


def event_class(ev, classes):
    """Класс приоритета события (0 высший)."""
    t = ev.get("type")
    if t == "scanner_signal":
        k = str((ev.get("d") or {}).get("kind") or "")
        return 4 if k.startswith("idea_") else int(classes.get("scanner_signal", 3))
    return int(classes.get(t, 3))


def matches(sub, ev, cls):
    f = sub.get("filter") or {}
    types = f.get("types")
    if types and "*" not in types and ev.get("type") not in types:
        return False
    if ev.get("type") in (f.get("exclude_types") or ()):
        return False
    kinds = f.get("kinds")
    if kinds and ev.get("type") == "scanner_signal" and (ev.get("d") or {}).get("kind") not in kinds:
        return False
    if f.get("pri") == "wake" and ev.get("pri") != bus.PRI_WAKE:
        return False
    mc = f.get("max_class")
    if isinstance(mc, int) and not isinstance(mc, bool) and cls > mc:
        return False
    syms = f.get("syms")
    if syms and ev.get("sym") not in syms:
        return False
    return True


# ------------------------------------------------------------------ журнал доставок
class DeliveryLog(object):
    def __init__(self, root=None):
        self.path = ddir(root, "deliveries.jsonl")

    def log(self, sub, ev, result, now, **extra):
        rec = {"ts": round(now, 3), "sub": sub, "seq": ev.get("seq"), "type": ev.get("type"), "res": result}
        rec.update(extra)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                fsutil.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def tail(self, n=50):
        from tb.platform import logs
        out = []
        for ln in logs.tail(self.path, n):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


# ------------------------------------------------------------------ очередь подписчика
class Queue(object):
    """state/dispatch/queues/<id>.jsonl: {"q":N,"seq","ts","type","pri","cls","plan"?,"sym"?,"d"}; подтверждение: <id>.ack.json {"acked":N}."""

    def __init__(self, root, sub_id, keep=2000):
        self.root, self.id, self.keep = root, sub_id, keep
        self.path = ddir(root, "queues", "%s.jsonl" % sub_id)
        self.ack_path = ddir(root, "queues", "%s.ack.json" % sub_id)
        self._last = None

    def _lines(self):
        out = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and isinstance(r.get("q"), int):
                        out.append(r)
        except OSError:
            pass
        return out

    def last(self):
        """-> (последний q, последний seq шины) в очереди."""
        if self._last is None:
            rows = self._lines()
            self._last = (rows[-1]["q"], rows[-1].get("seq", 0)) if rows else (0, 0)
        return self._last

    def append(self, ev, cls):
        """-> q новой записи или None, если событие уже в очереди (идемпотентность при повторной доставке)."""
        q, last_seq = self.last()
        if ev["seq"] <= last_seq:
            return None
        rec = {"q": q + 1, "seq": ev["seq"], "ts": ev.get("ts"), "type": ev.get("type"), "pri": ev.get("pri"), "cls": cls}
        for k in ("plan", "sym"):
            if ev.get(k):
                rec[k] = ev[k]
        rec["d"] = ev.get("d") or {}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._last = (q + 1, ev["seq"])
        self._trim()
        return q + 1

    def _trim(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < 64 * 1024:
            return
        rows = self._lines()
        if len(rows) <= int(self.keep * 1.25):
            return
        keep = rows[-self.keep:]
        tmp = self.path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        fsutil.replace(tmp, self.path)

    def acked(self):
        d = S.read_json(self.ack_path)
        return int(d.get("acked") or 0) if isinstance(d, dict) else 0

    def read(self, after=None, limit=50, wake_only=False):
        """Записи с q > after (по умолчанию после подтверждённого)."""
        a = self.acked() if after is None else int(after)
        rows = [r for r in self._lines() if r["q"] > a]
        if wake_only:
            rows = [r for r in rows if r.get("pri") == bus.PRI_WAKE]
        return rows[:limit] if limit else rows

    def ack(self, q):
        """Подтвердить обработку до q включительно (не больше последнего)."""
        last = self.last()[0]
        cur = max(self.acked(), min(int(q), last))
        os.makedirs(os.path.dirname(self.ack_path), exist_ok=True)
        atomic_write_json(self.ack_path, {"acked": cur, "ts": round(time.time(), 3)}, indent=None)
        return cur

    def stats(self, now=None):
        now = time.time() if now is None else now
        a = self.acked()
        rows = [r for r in self._lines() if r["q"] > a]
        wake = [r for r in rows if r.get("pri") == bus.PRI_WAKE]
        old = bus.parse_iso(wake[0].get("ts")) if wake else None
        return {"unread": len(rows), "wake_unread": len(wake), "last_q": rows[-1]["q"] if rows else a, "acked": a,
                "oldest_wake_age_s": None if old is None else round(max(0.0, now - old), 1), "ts": round(now, 3)}


# ------------------------------------------------------------------ задания для API-мозга
class Jobs(object):
    """state/brain/jobs/J-*.json. Коалесцирование и потолок; контракт для WP11-C в docstring модуля и docs/RUNBOOK.md."""

    def __init__(self, root, acfg):
        self.root, self.cfg = root, acfg
        self.dir = jobs_dir(root)
        self.rate_path = ddir(root, "rate.json")
        self.dropped = {}
        self.n = 0
        self._open = {}                      # (sub, key) -> job_id
        self._rebuild()

    def _lock(self):
        os.makedirs(self.dir, exist_ok=True)
        return FileLock(os.path.join(self.dir, ".lock"), wait=5.0)

    def _path(self, jid):
        return os.path.join(self.dir, "%s.json" % jid)

    def _read(self, jid):
        d = S.read_json(self._path(jid))
        return d if isinstance(d, dict) else None

    def _rebuild(self):
        try:
            names = [n for n in os.listdir(self.dir) if n.startswith("J-") and n.endswith(".json")]
        except OSError:
            return
        for n in sorted(names)[-200:]:
            d = S.read_json(os.path.join(self.dir, n))
            if isinstance(d, dict) and d.get("status") == "new" and d.get("sub") and d.get("key"):
                self._open[(d["sub"], d["key"])] = d["id"]

    def _rates(self):
        d = S.read_json(self.rate_path)
        return d if isinstance(d, dict) else {}

    @staticmethod
    def _kind(crit):
        return crit if isinstance(crit, str) else ("crit" if crit else "norm")

    def _count(self, sub, crit, now):
        r = self._rates().get(sub) or {}
        lst = r.get(self._kind(crit)) or []
        return [t for t in lst if isinstance(t, (int, float)) and now - t < 3600.0]

    def _bump(self, sub, crit, now):
        r = self._rates()
        e = r.setdefault(sub, {})
        for k in ["norm", "crit"] + [x for x in e if x not in ("norm", "crit")] + ([crit] if isinstance(crit, str) else []):
            e[k] = self._count(sub, k, now)
        e[self._kind(crit)].append(round(now, 1))
        atomic_write_json(self.rate_path, r, indent=None)

    @staticmethod
    def key_of(ev):
        if ev.get("plan"):
            return "plan:%s" % ev["plan"]
        if ev.get("sym"):
            return "sym:%s" % ev["sym"]
        return "type:%s" % ev.get("type")

    def add(self, sub, ev, cls, now, limits=None):
        """-> (результат, job_id|None): created | coalesced | dup | cap."""
        c = self.cfg
        lim = dict(c, **(limits or {}))
        key = self.key_of(ev)
        with self._lock():
            jid = self._open.get((sub, key))
            job = self._read(jid) if jid else None
            if job and (job.get("status") != "new" or now - job.get("ts_first", 0) > float(lim["coalesce_s"])):
                job = None
                self._open.pop((sub, key), None)
            if job:
                if ev["seq"] in job["seqs"]:
                    return "dup", job["id"]
                job["seqs"] = (job["seqs"] + [ev["seq"]])[-int(lim["max_seqs"]):]
                job["count"] += 1
                job["ts_last"] = round(now, 1)
                job["prio"] = min(job["prio"], cls)
                if ev.get("type") not in job["types"] and len(job["types"]) < 8:
                    job["types"].append(ev["type"])
                k = (ev.get("d") or {}).get("kind")
                if k and k not in job["kinds"] and len(job["kinds"]) < 8:
                    job["kinds"].append(k)
                if cls == 0:
                    job["not_before"] = job["ts_first"]
                atomic_write_json(self._path(job["id"]), job, indent=None)
                return "coalesced", job["id"]
            crit = cls == 0
            ccap = lim.get("class_jobs_per_hour") if isinstance(lim.get("class_jobs_per_hour"), dict) else {}
            bucket = "cls%d" % cls if (not crit and isinstance(ccap.get(str(cls)), int) and not isinstance(ccap.get(str(cls)), bool)) else crit
            cap = int(lim["critical_per_hour"] if crit else (ccap[str(cls)] if isinstance(bucket, str) else lim["jobs_per_hour"]))
            if len(self._count(sub, bucket, now)) >= cap:
                self.dropped[sub] = self.dropped.get(sub, 0) + 1
                return "cap", None
            self.n += 1
            jid = "J-%s-%s%03d" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)), os.getpid() % 1000, self.n)
            kinds = [(ev.get("d") or {}).get("kind")] if (ev.get("d") or {}).get("kind") else []
            job = {"id": jid, "sub": sub, "status": "new", "prio": cls, "key": key, "plan": ev.get("plan"), "sym": ev.get("sym"),
                   "ts_first": round(now, 1), "ts_last": round(now, 1), "not_before": round(now + (0.0 if crit else float(lim["settle_s"])), 1),
                   "count": 1, "seqs": [ev["seq"]], "types": [ev["type"]], "kinds": kinds, "dropped_before": self.dropped.pop(sub, 0)}
            os.makedirs(self.dir, exist_ok=True)
            atomic_write_json(self._path(jid), job, indent=None)
            self._open[(sub, key)] = jid
            self._bump(sub, bucket, now)
            return "created", jid

    def pending(self):
        out = []
        try:
            for n in sorted(os.listdir(self.dir)):
                if n.startswith("J-") and n.endswith(".json"):
                    d = S.read_json(os.path.join(self.dir, n))
                    if isinstance(d, dict) and d.get("status") == "new":
                        out.append(d)
        except OSError:
            pass
        return out


# ------------------------------------------------------------------ доставка
def post_webhook(url, ev, timeout=3.0):
    """POST {"event": ev}; без прокси. -> True при 2xx, иначе исключение."""
    body = json.dumps({"event": ev}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return 200 <= r.status < 300


class Dispatcher(object):
    def __init__(self, root=None, cfg=None, clock=time.time, poster=post_webhook):
        self.root = root
        self.cfg = cfg or S.load_cfg(root)
        self.dc = self.cfg["dispatch"]
        self.clock, self.poster = clock, poster
        self.dlog = DeliveryLog(root)
        self.jobs = Jobs(root, self.dc["api"])
        self.queues = {}
        self.attempts = {}                  # sub -> (число сбоев на текущем событии, seq)
        self.retry_at = {}
        self.stamp = None
        self.subs, self.problems = [], []
        self.counts = {"delivered": 0, "dup": 0, "coalesced": 0, "rate": 0, "cap": 0, "failed": 0, "unsupported": 0}
        self._extra = {}
        self._unread_sig = None
        self._unread_ts = 0.0
        self.reload(force=True)

    # ---- реестр
    def reload(self, force=False):
        try:
            st = os.stat(subs_file(self.root))
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = None
        lp = localcfg.local_path("subscriptions", self.root)      # локальный слой config/local/subscriptions.json тоже перечитывается на лету
        try:
            ls = os.stat(lp) if lp else None
            stamp = (stamp, (ls.st_mtime, ls.st_size) if ls else None)
        except OSError:
            stamp = (stamp, None)
        if not force and stamp == self.stamp:
            return
        self.stamp = stamp
        self.subs, new_problems = load_subscribers(self.root)
        if new_problems != self.problems:
            for p in new_problems:
                self.dlog.log("-", {}, "rejected_config", self.clock(), why=p[:120])
        self.problems = new_problems

    def queue(self, sid):
        q = self.queues.get(sid)
        if q is None:
            q = self.queues[sid] = Queue(self.root, sid, self.dc.get("queue_keep", 2000))
        return q

    # ---- курсоры
    def _cpath(self, sid):
        return ddir(self.root, "subs", "%s.cursor.json" % sid)

    def cursor(self, sid, sub):
        d = S.read_json(self._cpath(sid))
        last = bus.last_seq(root=self.root)
        if isinstance(d, dict) and isinstance(d.get("seq"), int):
            return min(d["seq"], last)                  # курсор впереди конца (шину чистили): назад к концу
        start = 0 if sub.get("start_at") == "begin" else last
        self.set_cursor(sid, start)
        return start

    def set_cursor(self, sid, seq):
        os.makedirs(os.path.dirname(self._cpath(sid)), exist_ok=True)
        atomic_write_json(self._cpath(sid), {"seq": seq, "ts": round(self.clock(), 3)}, indent=None)

    # ---- доставка одного события
    def deliver(self, sub, ev, cls, now):
        """-> результат: ok|dup|coalesced|rate|cap|failed|unsupported|retry."""
        sid, dl = sub["id"], sub["delivery"]
        if dl == "exec_cmd":
            return "unsupported"
        if dl == "queue":
            if cls > 0 and self._rate_hit(sub, now):
                return "rate"
            return "ok" if self.queue(sid).append(ev, cls) else "dup"
        if dl == "api_agent":
            res, jid = self.jobs.add(sid, ev, cls, now, sub.get("rate") or {})
            self._extra = {"job": jid} if jid else {}
            return {"created": "ok"}.get(res, res)
        if dl == "webhook":
            if cls > 0 and self._rate_hit(sub, now):
                return "rate"
            try:
                self.poster(sub["url"], ev)
                return "ok"
            except Exception as e:
                n, seq = self.attempts.get(sid, (0, None))
                n = n + 1 if seq == ev["seq"] else 1
                self.attempts[sid] = (n, ev["seq"])
                if n >= int(self.dc["max_attempts"]):
                    self.attempts.pop(sid, None)
                    self._extra = {"err": type(e).__name__, "attempts": n}
                    return "failed"
                delay = min(float(self.dc["retry_max_s"]), float(self.dc["retry_base_s"]) * 2 ** (n - 1))
                self.retry_at[sid] = now + delay
                self._extra = {"err": type(e).__name__, "attempt": n}
                return RETRY
        return "unsupported"

    def _rate_hit(self, sub, now):
        """Ограничение частоты подписчика: rate.max_per_hour (кроме класса 0). Считает фактические доставки за час."""
        lim = (sub.get("rate") or {}).get("max_per_hour")
        gap = (sub.get("rate") or {}).get("min_gap_s")
        hist = self.__dict__.setdefault("_hist", {}).setdefault(sub["id"], [])
        hist[:] = [t for t in hist if now - t < 3600.0]
        if (isinstance(lim, (int, float)) and len(hist) >= lim) or (isinstance(gap, (int, float)) and hist and now - hist[-1] < gap):
            return True
        hist.append(now)
        return False

    # ---- цикл
    def step(self, now=None):
        """Один проход по всем подписчикам. -> число обработанных (доставленных или отброшенных) событий."""
        now = self.clock() if now is None else now
        self.reload()
        total = 0
        cursors = []
        for sub in self.subs:
            if not sub["enabled"]:
                continue
            sid = sub["id"]
            if self.retry_at.get(sid, 0) > now:
                cursors.append(self.cursor(sid, sub))
                continue
            cur = start = self.cursor(sid, sub)
            for raw in bus.read(after_seq=cur, limit=READ_BATCH, root=self.root):
                ev = bus.clean_out(raw)
                if ev is None:
                    cur = raw["seq"]
                    continue
                cls = event_class(ev, self.dc["classes"])
                if matches(sub, ev, cls):
                    self._extra = {}
                    res = self.deliver(sub, ev, cls, now)
                    if res == RETRY:
                        self.dlog.log(sid, ev, res, now, **self._extra)
                        break
                    self.dlog.log(sid, ev, res, now, cls=cls, **self._extra)
                    key = {"ok": "delivered"}.get(res, res)
                    self.counts[key] = self.counts.get(key, 0) + 1
                    total += 1
                    cur = ev["seq"]
                    self.set_cursor(sid, cur)
                else:
                    cur = ev["seq"]
            if cur != start:
                self.set_cursor(sid, cur)
            cursors.append(cur)
        if cursors:
            try:
                bus.ack(CONSUMER, min(cursors), root=self.root)
            except ValueError:
                pass
        self.write_unread(now)
        return total

    def write_unread(self, now, force=False):
        """runtime/platform/unread.json: счётчики очередей (для status и MCP get_status). Пересчёт при изменении файлов или раз в 10 с."""
        sig = []
        for sub in self.subs:
            if sub["delivery"] == "queue" and sub["enabled"]:
                q = self.queue(sub["id"])
                try:
                    sig.append((sub["id"], os.stat(q.path).st_mtime_ns, os.stat(q.path).st_size))
                except OSError:
                    sig.append((sub["id"], 0, 0))
                try:
                    sig.append(os.stat(q.ack_path).st_mtime_ns)
                except OSError:
                    sig.append(0)
        if not force and sig == self._unread_sig and now - self._unread_ts < 10.0:
            return
        self._unread_sig, self._unread_ts = sig, now
        out = {}
        for sub in self.subs:
            if sub["delivery"] == "queue" and sub["enabled"]:
                out[sub["id"]] = self.queue(sub["id"]).stats(now)
        try:
            S.write_json(S.unread_file(self.root), out)
        except OSError:
            pass

    def heartbeat(self, now=None):
        S.write_hb("dispatch", {"subs": len([s for s in self.subs if s["enabled"]]), "counts": self.counts, "problems": len(self.problems)}, root=self.root, now=now)

    def run(self, max_seconds=None, sleep=time.sleep, stop=lambda: False):
        t0 = self.clock()
        while not stop():
            try:
                self.step()
            except Exception as e:                       # цикл диспетчера не должен падать от одного странного события
                self.dlog.log("-", {}, "error", self.clock(), err=type(e).__name__)
            self.heartbeat()
            if max_seconds is not None and self.clock() - t0 >= max_seconds:
                break
            sleep(float(self.dc["poll_s"]))
        return 0


# ------------------------------------------------------------------ CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="tb dispatch", description="диспетчер подписок на шину событий (WP11-B)")
    sp = ap.add_subparsers(dest="cmd")
    r = sp.add_parser("run", help="демон диспетчера (запускается супервизором)")
    r.add_argument("--max-seconds", type=float, default=None, dest="max_seconds")
    sp.add_parser("once", help="один проход")
    sp.add_parser("check", help="проверить config/subscriptions.json")
    q = sp.add_parser("queue", help="очередь подписчика: прочитать / подтвердить")
    q.add_argument("id")
    q.add_argument("--after", type=int, default=None)
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--ack", type=int, default=None, help="подтвердить обработку до номера q включительно")
    q.add_argument("--wake-only", action="store_true", dest="wake_only")
    sp.add_parser("stats", help="счётчики очередей, заданий и журнала")
    j = sp.add_parser("log", help="хвост журнала доставок")
    j.add_argument("-n", type=int, default=30)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not a.cmd:
        ap.print_usage(sys.stderr)
        return 2
    root = paths.root()
    if a.cmd == "check":
        subs, problems = load_subscribers(root)
        print(json.dumps({"ok": not problems, "subscribers": [{"id": s["id"], "delivery": s["delivery"], "enabled": s["enabled"]} for s in subs],
                          "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1
    if a.cmd == "queue":
        qu = Queue(root, a.id)
        if a.ack is not None:
            print(json.dumps({"ok": True, "acked": qu.ack(a.ack)}))
            return 0
        for rec in qu.read(after=a.after, limit=a.limit, wake_only=a.wake_only):
            print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        return 0
    if a.cmd == "log":
        for rec in DeliveryLog(root).tail(a.n):
            print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        return 0
    d = Dispatcher(root)
    if a.cmd == "stats":
        out = {"unread": {s["id"]: d.queue(s["id"]).stats() for s in d.subs if s["delivery"] == "queue"},
               "jobs_new": len(d.jobs.pending()), "problems": d.problems}
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if a.cmd == "once":
        n = d.step()
        d.heartbeat()
        print(json.dumps({"processed": n, "counts": d.counts, "problems": d.problems}, ensure_ascii=False))
        return 0
    flag = {"stop": False}

    def _h(*_):
        flag["stop"] = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _h)
            except (ValueError, OSError):
                pass
    print("dispatch: подписчиков %d (включено %d)" % (len(d.subs), len([s for s in d.subs if s["enabled"]])), flush=True)
    for p in d.problems:
        print("замечание: " + p, flush=True)
    return d.run(max_seconds=a.max_seconds, stop=lambda: flag["stop"])


if __name__ == "__main__":
    sys.exit(main())
