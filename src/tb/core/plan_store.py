"""Хранилище планов `state/plans/` с одним писателем (06 п. 6.6, формат runtime.json 6.7). Владелец: WP4.

Раскладка (пути: tb.paths.plans*):
  state/plans/PL-*/spec.json, runtime.json, revs.jsonl      спецификация, состояние исполнителя, история ревизий
  state/plans/_inbox/C-<ts>-<rand>.json                     команды от CLI мозга (единственное, что мозг пишет)
  state/plans/_outbox/<cmd_id>.json                         результаты команд (пишет исполнитель)
  state/plans/_archive/YYYY-MM/                             терминальные планы старше 7 суток
  state/plans/_counters.json                                счётчики (plan_n и др.)
Старые файлы вида state/plans/YYYY-MM-DD.md (планы дня в Markdown) лежат рядом и не трогаются: PlanStore работает только с PL-*.
"""

import copy
import json
import os
import re
import time
import uuid

from tb import fsutil
from tb import paths
from tb.core import plan_schema
from tb.core.filelock import FileLock, atomic_write_json

ARCHIVE_AFTER_DAYS = 7
ID_PREFIX = "PL-"
CMD_ID_PREFIX = "C-"
SEEN_CMD_KEEP = 500

# значения полей runtime.json (06 п. 6.7); дашборд и brief читают именно их
ENTRY_STATES = ("waiting", "placing", "resting", "filled", "blocked")
TARGET_STATES = ("pending", "resting", "filled", "cancelled", "touch")
RULE_STATES = ("waiting", "done", "skipped")
ENTRY_KINDS = ("entry", "add")
RUNTIME_TOP_KEYS = (
    "plan_id", "plan_n", "status", "rev_applied", "hold", "hold_reason", "ts", "entry", "pos", "targets", "rules", "adds",
    "realized", "close", "actions", "counters", "paper",
)
# поля, которые читают дашборд и brief
RUNTIME_PUBLIC_KEYS = ("status", "hold", "entry", "pos", "targets", "rules", "realized", "close")


_PLAN_RE = re.compile(r"^PL-[A-Za-z0-9_-]+$")
_CMD_RE = re.compile(r"^C-[A-Za-z0-9_-]+$")
LOCK_WAIT_S = 10.0


def _now():
    return time.time()


def _read_json(path):
    """-> объект или None (нет файла, битый JSON)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _check_plan_id(plan_id):
    if not isinstance(plan_id, str) or not _PLAN_RE.match(plan_id):
        raise ValueError("plan_store: недопустимый plan_id %r" % (plan_id,))


def _check_cmd_id(cmd_id):
    if not isinstance(cmd_id, str) or not _CMD_RE.match(cmd_id):
        raise ValueError("plan_store: недопустимый cmd_id %r" % (cmd_id,))


def _utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class PlanStore(object):
    """Все методы принимают корень через конструктор (`root=` для тестов). Писатель планов один (исполнитель),
    но все записи атомарны и идут под блокировками: `_locks/_store.lock` (create/archive), `_locks/<plan>.lock` (spec/rt),
    `_locks/_counters.lock`, `_locks/_inbox.lock`. Порядок захвата: store -> plan -> counters (иначе взаимоблокировок нет)."""

    def __init__(self, root=None):
        self.root = root
        self.broken = 0          # сколько битых папок пропустил последний list()

    # ---- пути ----
    def _p(self, *parts):
        return paths.plans(*parts, root=self.root)

    def _lock(self, name, wait=LOCK_WAIT_S):
        return FileLock(self._p("_locks", name + ".lock"), wait=wait)

    def _dir(self, plan_id):
        return self._p(plan_id)

    # ---- чтение ----
    def list(self, status=None, mode=None):
        """-> [{"spec":..,"rt":..}]; битые папки пропускаются и считаются (self.broken)."""
        out = []
        broken = 0
        try:
            names = sorted(n for n in os.listdir(self._p()) if n.startswith(ID_PREFIX))
        except OSError:
            names = []
        for n in names:
            if not os.path.isdir(self._dir(n)):
                continue
            spec, rt = self.load(n)
            if spec is None:
                broken += 1
                continue
            if status is not None:
                want = (status,) if isinstance(status, str) else tuple(status)
                if rt.get("status") not in want:
                    continue
            if mode is not None and spec.get("mode", "live") != mode:
                continue
            out.append({"spec": spec, "rt": rt})
        self.broken = broken
        return out

    def load(self, plan_id):
        """-> (spec, rt) или (None, None) (нет плана или файлы битые)."""
        if not isinstance(plan_id, str) or not _PLAN_RE.match(plan_id):
            return None, None
        spec = _read_json(self._p(plan_id, "spec.json"))
        rt = _read_json(self._p(plan_id, "runtime.json"))
        if not isinstance(spec, dict) or not isinstance(rt, dict):
            return None, None
        return spec, rt

    def revs(self, plan_id):
        """-> [запись ревизии] из revs.jsonl (битые строки пропускаются)."""
        _check_plan_id(plan_id)
        out = []
        try:
            with open(self._p(plan_id, "revs.jsonl"), encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        out.append(r)
        except OSError:
            pass
        return out

    # ---- счётчики ----
    def counters_next(self, name):
        """Атомарный счётчик: 1, 2, 3... без повторов при параллельных процессах. Битый файл: восстановление plan_n по существующим планам."""
        if not isinstance(name, str) or not name:
            raise ValueError("counters_next: имя счётчика должно быть непустой строкой")
        path = paths.plans_counters(root=self.root)
        with self._lock("_counters"):
            d = _read_json(path)
            if not isinstance(d, dict):
                if os.path.exists(path):
                    try:
                        fsutil.replace(path, "%s.corrupt-%d" % (path, int(_now())))
                    except OSError:
                        pass
                d = {"plan_n": self._max_plan_n()}
            if name.startswith("day:"):    # счётчики прошлых дней не копим
                for k in [k for k in d if k.startswith("day:") and k.split("-")[0] != name.split("-")[0]]:
                    del d[k]
            v = d.get(name, 0)
            n = (v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0) + 1
            d[name] = n
            atomic_write_json(path, d, sync=True)
            return n

    def _max_plan_n(self):
        best = 0
        roots = [self._p()]
        try:
            for m in os.listdir(self._p("_archive")):
                roots.append(self._p("_archive", m))
        except OSError:
            pass
        for r in roots:
            try:
                names = os.listdir(r)
            except OSError:
                continue
            for n in names:
                if n.startswith(ID_PREFIX):
                    rt = _read_json(os.path.join(r, n, "runtime.json"))
                    if isinstance(rt, dict) and isinstance(rt.get("plan_n"), int):
                        best = max(best, rt["plan_n"])
        return best

    # ---- запись планов ----
    def _exists(self, plan_id):
        if os.path.exists(self._dir(plan_id)):
            return True
        try:
            for m in os.listdir(self._p("_archive")):
                if os.path.exists(self._p("_archive", m, plan_id)):
                    return True
        except OSError:
            pass
        return False

    def _new_id(self, spec):
        sym = re.sub(r"[^A-Z0-9]", "", str(spec.get("symbol", "")).upper())[:20] or "X"
        day = time.strftime("%m%d", time.localtime(_now()))
        while True:
            nn = self.counters_next("day:%s-%s" % (day, sym))
            pid = "PL-%s-%s-%02d" % (day, sym, nn)
            if not self._exists(pid):
                return pid

    def create(self, spec, rt):
        """Новая папка PL-..., revs.jsonl с rev 1; назначает plan_n из _counters.json. -> plan_id.

        spec["id"] (если задан) используется как есть, занятый id = ValueError; иначе PL-MMDD-SYM-NN. Папка создаётся целиком
        во временном имени и переименовывается: плана либо нет, либо он полный."""
        spec = copy.deepcopy(spec)
        rt = copy.deepcopy(rt) if rt else {}
        now = _now()
        with self._lock("_store"):
            plan_id = spec.get("id")
            if plan_id:
                _check_plan_id(plan_id)
                if self._exists(plan_id):
                    raise ValueError("plan_store: план %s уже существует" % plan_id)
            else:
                plan_id = self._new_id(spec)
            spec["id"] = plan_id
            spec["rev"] = 1
            rt["plan_id"] = plan_id
            rt["plan_n"] = self.counters_next("plan_n")
            rt.setdefault("status", "draft")
            rt.setdefault("rev_applied", 0)
            if not isinstance(rt.get("ts"), dict):
                rt["ts"] = {}
            rt["ts"].setdefault("created", round(now, 3))
            tmp = self._p("_tmp-%s-%s" % (plan_id, uuid.uuid4().hex[:6]))
            os.makedirs(tmp)
            try:
                atomic_write_json(os.path.join(tmp, "spec.json"), spec, sync=True)
                atomic_write_json(os.path.join(tmp, "runtime.json"), rt, sync=True)
                with open(os.path.join(tmp, "revs.jsonl"), "w", encoding="utf-8") as f:
                    f.write(json.dumps({"rev": 1, "ts": _utc(now), "t": round(now, 3), "why": "create", "spec": spec},
                                       ensure_ascii=False, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                self._rename_dir(tmp, self._dir(plan_id))
            except BaseException:
                self._rmtree(tmp)
                raise
        return plan_id

    @staticmethod
    def _rename_dir(src, dst):
        for i in range(20):
            try:
                os.rename(src, dst)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def save_spec(self, spec, why):
        """rev+1, атомарно, дописывает в revs.jsonl. spec["rev"] должен совпадать с текущей ревизией на диске
        (оптимистичная блокировка: иначе ValueError). -> новый spec (с rev+1)."""
        plan_id = spec.get("id")
        _check_plan_id(plan_id)
        with self._lock(plan_id):
            cur = _read_json(self._p(plan_id, "spec.json"))
            if not isinstance(cur, dict):
                raise KeyError("plan_store: плана %s нет" % plan_id)
            if spec.get("rev") != cur.get("rev"):
                raise ValueError("plan_store: конфликт ревизий %s: у вас rev=%r, на диске rev=%r"
                                 % (plan_id, spec.get("rev"), cur.get("rev")))
            new = copy.deepcopy(spec)
            new["rev"] = cur["rev"] + 1
            now = _now()
            atomic_write_json(self._p(plan_id, "spec.json"), new, sync=True)
            line = json.dumps({"rev": new["rev"], "ts": _utc(now), "t": round(now, 3), "why": str(why)[:200], "spec": new},
                              ensure_ascii=False, sort_keys=True) + "\n"
            with open(self._p(plan_id, "revs.jsonl"), "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            spec["rev"] = new["rev"]
            return new

    def save_rt(self, plan_id, rt, sync=False):
        """Атомарная замена runtime.json. Смена status только допустимая (plan_schema.can_transition), иначе ValueError."""
        _check_plan_id(plan_id)
        if not isinstance(rt, dict):
            raise ValueError("plan_store: rt должен быть dict")
        if rt.get("plan_id") not in (None, plan_id):
            raise ValueError("plan_store: rt.plan_id=%r не совпадает с %s" % (rt.get("plan_id"), plan_id))
        with self._lock(plan_id):
            old = _read_json(self._p(plan_id, "runtime.json"))
            if not isinstance(old, dict):
                raise KeyError("plan_store: плана %s нет" % plan_id)
            a, b = old.get("status"), rt.get("status")
            if b != a and not plan_schema.can_transition(a, b):
                raise ValueError("plan_store: переход статуса %s -> %s недопустим (%s)" % (a, b, plan_id))
            rt = dict(rt)
            rt["plan_id"] = plan_id
            if "plan_n" in old:
                rt["plan_n"] = old["plan_n"]       # plan_n неизменен
            atomic_write_json(self._p(plan_id, "runtime.json"), rt, sync=sync)

    def archive(self, plan_id, now=None, force=False):
        """Перенос терминального плана старше 7 суток (от rt.ts.closed, иначе от mtime runtime.json) в _archive/YYYY-MM/.
        -> путь в архиве или None (не терминален/слишком свеж/нет)."""
        _check_plan_id(plan_id)
        now = _now() if now is None else now
        with self._lock("_store"):
            with self._lock(plan_id):
                rt = _read_json(self._p(plan_id, "runtime.json"))
                if not isinstance(rt, dict) or rt.get("status") not in plan_schema.TERMINAL:
                    return None
                closed = (rt.get("ts") or {}).get("closed")
                if not isinstance(closed, (int, float)):
                    try:
                        closed = os.path.getmtime(self._p(plan_id, "runtime.json"))
                    except OSError:
                        return None
                if not force and now - closed < ARCHIVE_AFTER_DAYS * 86400:
                    return None
                month = time.strftime("%Y-%m", time.localtime(closed))
                dst = self._p("_archive", month, plan_id)
                if os.path.exists(dst):
                    dst = "%s.%d" % (dst, int(now))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                self._rename_dir(self._dir(plan_id), dst)
                return dst

    def archive_all(self, now=None):
        """Архивирует все подходящие планы. -> [plan_id]."""
        done = []
        for it in self.list():
            pid = it["spec"].get("id") or it["rt"].get("plan_id")
            if pid and self.archive(pid, now=now):
                done.append(pid)
        return done

    # ---- очереди ----
    def _seen_path(self):
        return self._p("_seen_cmds.json")

    def _seen_list(self):
        d = _read_json(self._seen_path())
        return [x for x in d if isinstance(x, str)] if isinstance(d, list) else []

    def seen_cmd(self, cmd_id):
        """Идемпотентность: True, если команда уже обработана (помечается inbox_done / mark_seen). Хранит последние 500 cmd_id.
        Чистый запрос: падение исполнителя между приёмом и inbox_done не теряет команду."""
        return cmd_id in self._seen_list() or os.path.exists(self._p("_outbox", "%s.json" % cmd_id))

    def mark_seen(self, cmd_id):
        _check_cmd_id(cmd_id)
        with self._lock("_seen"):
            lst = self._seen_list()
            if cmd_id in lst:
                return
            lst.append(cmd_id)
            atomic_write_json(self._seen_path(), lst[-SEEN_CMD_KEEP:])

    def inbox_put(self, cmd):
        """-> cmd_id; tmp + replace; имя файла C-<ts>-<rand>.json. ЕДИНСТВЕННАЯ запись CLI мозга.

        cmd["cmd_id"] задан и уже есть в очереди/обработан: повторная доставка, файл не перезаписывается (идемпотентно)."""
        cmd = copy.deepcopy(cmd)
        cmd_id = cmd.get("cmd_id")
        if not cmd_id:
            cmd_id = "C-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(_now())), uuid.uuid4().hex[:6])
        _check_cmd_id(cmd_id)
        cmd["cmd_id"] = cmd_id
        cmd.setdefault("ts", _utc(_now()))
        path = self._p("_inbox", cmd_id + ".json")
        with self._lock("_inbox"):
            if os.path.exists(path) or self.seen_cmd(cmd_id):
                return cmd_id
            atomic_write_json(path, cmd, sync=True)
        return cmd_id

    def inbox_list(self):
        """-> [cmd] по возрастанию имени файла. Нечитаемый файл возвращается как {"cmd_id":..,"_broken":True} (исполнитель отвергнет его)."""
        try:
            names = sorted(n for n in os.listdir(self._p("_inbox")) if n.startswith(CMD_ID_PREFIX) and n.endswith(".json"))
        except OSError:
            return []
        out = []
        for n in names:
            d = _read_json(self._p("_inbox", n))
            cid = n[:-5]
            if isinstance(d, dict):
                d.setdefault("cmd_id", cid)
                out.append(d)
            elif os.path.exists(self._p("_inbox", n)):
                out.append({"cmd_id": cid, "_broken": True})
        return out

    def inbox_done(self, cmd_id, result):
        """Пишет _outbox/<cmd_id>.json, помечает команду обработанной, удаляет её из _inbox (в таком порядке: сбой между шагами = повторная
        доставка, которую seen_cmd/outbox отсекают)."""
        _check_cmd_id(cmd_id)
        res = dict(result) if isinstance(result, dict) else {"result": result}
        res.setdefault("cmd_id", cmd_id)
        atomic_write_json(self._p("_outbox", cmd_id + ".json"), res, sync=True)
        self.mark_seen(cmd_id)
        try:
            os.remove(self._p("_inbox", cmd_id + ".json"))
        except OSError:
            pass

    def outbox_get(self, cmd_id):
        """-> результат команды (dict) или None."""
        if not isinstance(cmd_id, str) or not _CMD_RE.match(cmd_id):
            return None
        d = _read_json(self._p("_outbox", cmd_id + ".json"))
        return d if isinstance(d, dict) else None

    def outbox_gc(self, max_age_s=3 * 86400, now=None):
        """Удаляет старые результаты из _outbox. -> сколько удалено."""
        now = _now() if now is None else now
        n = 0
        d = self._p("_outbox")
        try:
            names = os.listdir(d)
        except OSError:
            return 0
        for f in names:
            fp = os.path.join(d, f)
            try:
                if now - os.path.getmtime(fp) > max_age_s:
                    os.remove(fp)
                    n += 1
            except OSError:
                pass
        return n
