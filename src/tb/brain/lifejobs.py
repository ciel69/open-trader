"""Задания жизненного цикла для рантайма мозга (шов WP14 <-> WP11-C2).

WP14 (`lifecycle.ports.FileJobBrain`) кладёт `state/brain/jobs/L-*.json` (kind=lifecycle, роль morning_analyst | day_auditor) и ждёт `L-*.result.json`
{"id","ok","output","err","usd"}. Здесь рантайм берёт такое задание, строит текст запроса и контекст смысловой проверки ИЗ ДАННЫХ СИСТЕМЫ (не из ответа модели),
вызывает `Runner.ask_role(role, text, ctx=...)` и атомарно пишет ответ. Применяет ответ сам lifecycle (свой валидатор, K1-K8); раннер действий не исполняет.
Захват задания: файл `L-*.claim` (O_EXCL), чтобы воркеры не взяли одно задание дважды; при старте демона брошенные захваты снимаются (`reset_claims`).
"""
import json
import os
import re
import time

from tb import paths
from tb.core.filelock import atomic_write_json

ROLES = ("morning_analyst", "day_auditor")
RE_ID = re.compile(r"^L-[A-Za-z0-9_.-]{1,60}$")
MAX_TEXT = 60000


def _dir(root):
    return paths.state("brain", "jobs", root=root)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pending(root):
    """-> [(id, job)] заданий lifecycle без ответа, по возрастанию id."""
    d = _dir(root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("L-") and n.endswith(".json") and not n.endswith(".result.json"))
    except OSError:
        return []
    out = []
    for n in names:
        jid = n[:-5]
        if not RE_ID.match(jid) or os.path.exists(os.path.join(d, jid + ".result.json")):
            continue
        j = _read(os.path.join(d, n))
        if isinstance(j, dict) and j.get("kind") == "lifecycle":
            out.append((jid, j))
    return out


def _claim(root, jid):
    try:
        fd = os.open(os.path.join(_dir(root), jid + ".claim"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except OSError:
        return False


def reset_claims(root):
    """Старт демона: захваты без ответа остались от прошлого запуска -> снять. -> число."""
    n = 0
    d = _dir(root)
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for nm in names:
        if nm.endswith(".claim") and not os.path.exists(os.path.join(d, nm[:-6] + ".result.json")):
            try:
                os.remove(os.path.join(d, nm))
                n += 1
            except OSError:
                pass
    return n


def build_request(role, payload):
    """-> (текст запроса, ctx проверки). Теги как в config/roles/<роль>.md: <brief>/<facts> для утра, <audit_input> для аудита."""
    from tb.brain import validate
    p = payload if isinstance(payload, dict) else {}

    def tag(name, val):
        return "<%s>\n%s\n</%s>" % (name, json.dumps(val, ensure_ascii=False, sort_keys=True, separators=(",", ":")), name)

    if role == "morning_analyst":
        brief = p.get("brief") if isinstance(p.get("brief"), dict) else {}
        facts = p.get("facts") if isinstance(p.get("facts"), dict) else None
        parts = [tag("brief", brief)]
        if facts is not None:
            parts.append(tag("facts", facts))
        parts.append(tag("owner_choice", p.get("owner_choice") or {}))
        parts.append(tag("caps", p.get("caps") or {}))
        parts.append(tag("symbols", p.get("symbols") or []))
        if p.get("carried_in"):
            parts.append(tag("carried_in", p["carried_in"]))
        if p.get("previous_errors"):
            parts.append("CODES: " + ", ".join(str(x)[:40] for x in p["previous_errors"][:6]))
        caps = p.get("caps") if isinstance(p.get("caps"), dict) else {}
        oc = p.get("owner_choice") if isinstance(p.get("owner_choice"), dict) else {}
        over = {}
        lim = {k: caps[k] for k in ("max_risk_usdt", "size_mult") if isinstance(caps.get(k), (int, float))}
        if lim:
            over["limits"] = lim
        if oc.get("session"):
            over["session_type"] = oc["session"]
        syms = p.get("symbols")
        ctx = validate.dayplan_context(brief, facts, **over)
        if isinstance(syms, list) and syms:
            ctx["symbols"] = sorted(set(ctx.get("symbols") or []) | set(s for s in syms if isinstance(s, str)))
        return "\n".join(parts)[:MAX_TEXT], ctx
    body = dict((k, v) for k, v in p.items() if k not in ("schema", "kind"))
    inp = dict(body, items=body.get("items") or [])
    ctx = validate.audit_context({"day_id": p.get("day_id"), "items": [{"item": x, "decision": next((r.get("decision") for r in (p.get("rows") or []) if isinstance(r, dict) and r.get("item") == x), None)}
                                                                      for x in (p.get("items") or []) if isinstance(x, str)]})
    return tag("audit_input", inp)[:MAX_TEXT], ctx


def step(runner):
    """Берёт одно задание lifecycle -> True, если задание было. Любая ошибка = ответ ok:false (lifecycle уйдёт в запасной путь), воркер не падает."""
    root = runner.root
    for jid, job in pending(root):
        if not _claim(root, jid):
            continue
        now = runner.clock()
        out = {"id": jid, "ok": False, "output": None, "err": None, "usd": 0}
        role = job.get("role")
        try:
            if role not in ROLES:
                out["err"] = "BAD_ROLE"
            elif job.get("deadline_ts") and now > float(job["deadline_ts"]) + 5:
                out["err"] = "EXPIRED"
            else:
                text, ctx = build_request(role, job.get("payload"))
                r = runner.ask_role(role, text, note="L", ctx=ctx)
                out.update(ok=bool(r.get("ok")), output=r.get("obj") if r.get("ok") else None, err=None if r.get("ok") else str(r.get("code") or "ERR")[:40],
                           usd=r.get("usd") or 0)
        except Exception as e:
            out["err"] = e.__class__.__name__[:40]
        atomic_write_json(os.path.join(_dir(root), jid + ".result.json"), out, indent=None)
        try:
            os.remove(os.path.join(_dir(root), jid + ".claim"))
        except OSError:
            pass
        return True
    return False


def prune(root, now, keep_s=259200.0):
    n = 0
    d = _dir(root)
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for nm in names:
        if nm.startswith("L-") and (nm.endswith(".json") or nm.endswith(".claim")):
            p = os.path.join(d, nm)
            try:
                if now - os.path.getmtime(p) > keep_s:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    return n
