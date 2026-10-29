"""Порты жизненного цикла: мозг (задания ролей) и исполнитель (очередь команд плана + снимок). WP14.

Слой lifecycle не знает ни биржи, ни исполнителя: команды идут ТОЛЬКО через очередь `state/plans/_inbox` (core.plan_store), состояние читается из
`runtime/exec_snapshot.json` и файлов планов. Мозг: абстрактный `LifecycleBrain`; реальный рантайм WP11-C2 подключается реализацией порта,
пока есть `FileJobBrain` (контракт заданий-файлов, docs/LIFECYCLE.md п. 7) и `NullBrain` (мозга нет).
"""
import hashlib
import json
import os
import re
import time

from tb import paths
from tb.core import plan_store
from tb.core.filelock import atomic_write_json

JOB_SCHEMA = "tb.brainjob/1"
RE_JOB = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
ACTIVE_PLAN = ("draft", "armed", "entered", "managing")
OPEN_POS = ("entered", "managing")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ мозг
class LifecycleBrain(object):
    """Порт «мозга» для ролей morning_analyst и day_auditor. Реализации не блокируют: submit -> id, poll -> None пока нет ответа."""

    def available(self, root, now):
        """-> (bool, текст)."""
        raise NotImplementedError

    def submit(self, role, payload, timeout_s, now, meta=None):
        """Ставит задание -> job_id (строка)."""
        raise NotImplementedError

    def poll(self, job_id, now):
        """-> None (ещё нет) | {"ok": bool, "output": dict|None, "err": str|None}."""
        raise NotImplementedError

    def cancel(self, job_id):
        return None


class NullBrain(LifecycleBrain):
    def available(self, root, now):
        return False, "мозг не подключён"

    def submit(self, role, payload, timeout_s, now, meta=None):
        raise RuntimeError("мозг не подключён")

    def poll(self, job_id, now):
        return {"ok": False, "output": None, "err": "NO_BRAIN"}


class FileJobBrain(LifecycleBrain):
    """Задание жизненного цикла = файл `state/brain/jobs/L-<время>-<хеш>.json` (префикс L-, чтобы не пересекаться с заданиями диспетчера J-*.json формата WP11-B),
    ответ раннера = `L-....result.json`: {"id","ok","output","err","usd"} (писать атомарно). Раннер C2/C3 берёт `kind=lifecycle`, вызывает `Runner.ask_role(role, text)` и пишет ответ.
    available() делегирует пробе ctl (`brain_iface.probe`, heartbeat сервиса brain + бюджет)."""

    def __init__(self, root):
        self.root = root

    def _dir(self):
        return paths.state("brain", "jobs", root=self.root)

    def available(self, root, now):
        try:
            from tb.control import brain_iface
            r = brain_iface.probe(self.root, now)
            return bool(r.get("ok")), str(r.get("msg") or "")[:160]
        except Exception:
            return False, "проба мозга недоступна"

    def submit(self, role, payload, timeout_s, now, meta=None):
        jid = "L-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)), hashlib.sha1(("%s|%r|%s" % (role, sorted((meta or {}).items()), now)).encode("utf-8")).hexdigest()[:8])
        os.makedirs(self._dir(), exist_ok=True)
        atomic_write_json(os.path.join(self._dir(), jid + ".json"),
                          {"schema": JOB_SCHEMA, "id": jid, "kind": "lifecycle", "role": role, "ts": round(now, 3), "deadline_ts": round(now + float(timeout_s), 3),
                           "meta": meta or {}, "payload": payload}, indent=None)
        return jid

    def poll(self, job_id, now):
        if not isinstance(job_id, str) or not RE_JOB.match(job_id):
            return {"ok": False, "output": None, "err": "BAD_JOB"}
        d = _read(os.path.join(self._dir(), job_id + ".result.json"))
        if not isinstance(d, dict):
            return None
        return {"ok": bool(d.get("ok")), "output": d.get("output") if isinstance(d.get("output"), dict) else None, "err": str(d.get("err") or "")[:60] or None}

    def cancel(self, job_id):
        try:
            if isinstance(job_id, str) and RE_JOB.match(job_id):
                os.remove(os.path.join(self._dir(), job_id + ".json"))
        except OSError:
            pass


# ------------------------------------------------------------------ исполнитель
class ExecPort(object):
    """Порт исполнителя для lifecycle: снимок, планы, очередь команд."""

    def snapshot(self, now):
        """-> {"present","age_s","ts","equity","free","positions":[{sym,side,size,avg,mark,sl,tp,liq,upnl,lev,margin,owner}],"orders":[...]}."""
        raise NotImplementedError

    def health(self, now):
        """-> {"present","alive","age_s","data":{...exec_health...}}."""
        raise NotImplementedError

    def plans(self):
        """-> [{"id","sym","side","mode","stype","status","hold","spec","rt"}] только не терминальные планы."""
        raise NotImplementedError

    def send(self, op, plan_id, reason, payload=None, now=None, tag=""):
        """Кладёт команду в очередь -> cmd_id."""
        raise NotImplementedError

    def result(self, cmd_id):
        """-> dict ответа исполнителя | None."""
        raise NotImplementedError


class FileExecPort(ExecPort):
    ACTOR = "system"

    def __init__(self, root):
        self.root = root

    def snapshot(self, now):
        d = _read(paths.exec_snapshot_file(root=self.root))
        if not isinstance(d, dict):
            return {"present": False, "age_s": None, "ts": None, "equity": None, "free": None, "positions": [], "orders": []}
        ts = d.get("ts_epoch")
        acct = d.get("account") if isinstance(d.get("account"), dict) else {}
        return {"present": True, "ts": ts, "age_s": round(max(0.0, now - ts), 1) if isinstance(ts, (int, float)) else None, "equity": acct.get("equity"),
                "free": acct.get("free"), "positions": [p for p in (d.get("positions") or []) if isinstance(p, dict)],
                "orders": [o for o in (d.get("orders") or []) if isinstance(o, dict)]}

    def health(self, now):
        from tb.brain import common
        return common.exec_health(root=self.root, now=now)

    def plans(self):
        out = []
        for it in plan_store.PlanStore(self.root).list(status=ACTIVE_PLAN):
            spec, rt = it["spec"], it["rt"]
            out.append({"id": spec.get("id") or rt.get("plan_id"), "sym": spec.get("symbol"), "side": spec.get("side"), "mode": spec.get("mode", "live"),
                        "stype": spec.get("session_type") or "intraday", "status": rt.get("status"), "hold": rt.get("hold"), "spec": spec, "rt": rt})
        return out

    def send(self, op, plan_id, reason, payload=None, now=None, tag=""):
        now = time.time() if now is None else now
        h = hashlib.sha1(("%s|%s|%s|%s|%s" % (op, plan_id, tag, json.dumps(payload, sort_keys=True), int(now // 1))).encode("utf-8")).hexdigest()[:8]
        cmd = {"actor": self.ACTOR, "op": op, "reason": str(reason or "")[:200], "cmd_id": "C-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)), h)}
        if plan_id:
            cmd["plan_id"] = plan_id
        if payload is not None:
            cmd["payload"] = payload
        return plan_store.PlanStore(self.root).inbox_put(cmd)

    def result(self, cmd_id):
        return plan_store.PlanStore(self.root).outbox_get(cmd_id)
