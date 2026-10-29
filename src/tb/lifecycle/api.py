"""Контракт `tb.lifecycle.api` (WP14): функции берут (root, args, now) и возвращают {"ok","code","msg","data"}. Вызывает ctl (control/lifecycle.py).

  start_day, stop_day, emergency_summary, carry_decide, report_ack, notify_test  — действия;
  tick(root, now)  — вызывается ctl каждый цикл: возобновление после падения, автоостановка по времени, продолжение закрытия, финал аварийного дня;
  banner(root)     — баннер для status.json ctl;
  resume_day(root, args, now) — возобновление торговли после перезапуска платформы (ctl пока не имеет такой кнопки: интеграция).
Слой не импортирует venues и executor: исполнитель — только очередь команд плана и снимок.
"""
import threading
import time

from tb import paths
from tb.control import brain_iface, ctlstate
from tb.core import exec_cfg, session
from tb.lifecycle import audit, days, notify, notifycfg, report, runtime, starter
from tb.lifecycle import carry as CARRY
from tb.lifecycle import cfg as cfgmod


CLOSE_TICK_S = 2.0
_LAST_CLOSE = {}


def _res(ok, code="OK", msg="", data=None):
    return {"ok": bool(ok), "code": code, "msg": msg, "data": data or {}}


def _live_run(ctx, d):
    return runtime.sync_mode()


# ------------------------------------------------------------------ старт
def start_day(root, args, now):
    ctx = runtime.Ctx(root, now)
    cur = days.current(root)
    if cur is not None and cur.get("state") in ("starting", "running", "closing", "halted"):
        return _res(False, "E_STATE", "день %s уже в состоянии %s" % (cur["day_id"], cur["state"]), {"state": cur["state"], "day_id": cur["day_id"]})
    agent = args.get("agent") or brain_iface.status(root).get("agent") or brain_iface.DEFAULT_AGENT
    d = days.create(root, args, ctx.now(), agent)
    days.emit("day_state", {"state": "starting"}, root, ctx.now())
    days.clear_banner(root)
    r = starter.run_start(ctx, d)
    return r


def preflight_preview(root, args, now):
    """Предпросмотр предстартовых проверок для окна «Старт» (ничего не запускает): жёлтые замечания с причиной и действием."""
    from tb.lifecycle import preflight
    ctx = runtime.Ctx(root, now)
    v = preflight.run(ctx, dict(args, allow_amber=True), None)
    return _res(True, "OK", "", {"amber": [preflight.public(c) for c in v["amber"]], "red": [preflight.public(c) for c in v["red"]]})


# ------------------------------------------------------------------ остановка
def _begin_close(ctx, d, source, flatten=False):
    now = ctx.now()
    days.set_state(ctx.root, d, "closing", now, step="C0")
    d["phase"] = "stop"
    d["stop_source"] = source
    d["closing"] = {"started": round(now, 1), "source": source, "flatten": bool(flatten)}
    days.set_trading(ctx.root, False, d)
    days.clear_banner(ctx.root)
    days.save(ctx.root, d)


def _close_result(ctx, d, outcome):
    own = audit.pending_questions(d)
    au = ctlstate.read_json(paths.state("days", d["day_id"], "audit.json", root=ctx.root)) or {}
    return _res(True, "OK", "", {"day_id": d["day_id"], "state": d["state"], "close": outcome, "audit": {"llm": au.get("llm"), "carried_n": len((ctlstate.read_json(
        paths.state("days", d["day_id"], "carry.json", root=ctx.root)) or {}).get("items") or []), "pending_questions": len(own), "report": bool((d.get("report") or {}).get("ready"))}})


def stop_day(root, args, now):
    ctx = runtime.Ctx(root, now)
    d = days.current(root)
    if d is None or d.get("state") not in ("running", "closing"):
        return _res(False, "E_STATE", "день не идёт", {"state": (d or {}).get("state", "stopped")})
    if d["state"] == "running":
        _begin_close(ctx, d, args.get("source") or "owner")
    outcome = audit.run_close(ctx, d)
    return _close_result(ctx, d, outcome)


def emergency_summary(root, args, now):
    """KILL/аварийная остановка: день -> halted, торговый флаг выключен, краткий отчёт без LLM. Шаги закрытия НЕ выполняются (позиции ведёт исполнитель и стопы биржи)."""
    ctx = runtime.Ctx(root, now)
    d = days.current(root)
    if d is None or d.get("state") not in ("starting", "running", "closing"):
        return _res(True, "OK", "", {"day_id": None})
    prev = d["state"]
    d.setdefault("closing", {})["emergency"] = True
    d["stop_source"] = "emergency"
    d["halted_from"] = prev
    days.set_state(ctx.root, d, "halted", ctx.now(), code="kill")
    days.set_trading(ctx.root, False, d)
    rep = report.emergency(ctx, d)
    days.save(root, d)
    return _res(True, "OK", "", {"day_id": d["day_id"], "verdict": rep["verdict"], "net": rep["pnl"]["net"]})


# ------------------------------------------------------------------ решения владельца
def carry_decide(root, args, now):
    ctx = runtime.Ctx(root, now)
    d = days.current(root)
    plan, dec = args.get("plan"), args.get("decision")
    if d is None or d.get("state") != "closing":
        return _res(False, "E_STATE", "вопросов о переносе сейчас нет", {"state": (d or {}).get("state", "stopped")})
    q = next((x for x in audit.pending_questions(d) if x.get("plan") == plan or x.get("item") == plan), None)
    if q is None:
        return _res(False, "E_STATE", "по этой позиции нет ожидающего вопроса", {"plan": plan})
    if dec not in ("carry", "close"):
        return _res(False, "E_ARGS", "decision: carry|close")
    q.update(answer=dec, answered_by="owner", answered_ts=round(ctx.now(), 1))
    days.save(root, d)
    outcome = audit.run_close(ctx, d)
    r = _close_result(ctx, d, outcome)
    r["data"].update({"plan": plan, "decision": dec})
    return r


def report_ack(root, args, now):
    ctx = runtime.Ctx(root, now)
    day_id = args.get("day_id")
    d = days.load(root, day_id)
    if d is None:
        return _res(False, "E_STATE", "такого дня нет", {"day_id": day_id})
    if not (d.get("report") or {}).get("ready"):
        return _res(False, "E_STATE", "отчёт дня ещё не готов", {"state": d.get("state")})
    note = CARRY.clean_text(args.get("note"), 160)
    if d.get("state") == "closing" and (d.get("closing") or {}).get("failed"):
        if len(note) < 3:
            return _res(False, "E_ARGS", "закрытие не подтверждено: нужна пометка (разобран вручную)", {"day_id": day_id})
        d["closing"]["manual_ack"] = note
        days.set_state(root, d, "stopped", ctx.now(), code="manual_ack")
        days.set_trading(root, False, d)
    d["report"]["acked"], d["report"]["acked_ts"] = True, round(ctx.now(), 1)
    if note:
        d["report"]["ack_note"] = note
    days.save(root, d)
    b = days.banner(root)
    if b and b.get("day_id") == day_id:
        days.clear_banner(root)
    return _res(True, "OK", "", {"day_id": day_id, "state": d["state"]})


def notify_test(root, args, now):
    """Одно тестовое уведомление ОС по явной команде владельца (мастер и категории не нужны). В unittest-режиме ОС не вызывается."""
    ok, how = notify.send_os("Проверка уведомлений: traider-bot", {}, force=True)
    if ok:
        return _res(True, "OK", "", {"os": True, "how": how})
    return _res(False, "E_UNAVAILABLE", "уведомление не показано (%s)" % how, {"os": False, "how": how})


def notify_set(root, args, now):
    """notify.set: изменить state/control/notify.json (мастер, категории, тихие часы, лимиты). Аргументы уже проверены protocol.validate_notify."""
    args = dict(args)
    by = str(args.pop("_by", None) or "ctl")[:8]
    ok, msg, s, changes = notifycfg.set_patch(root, args, now, by)
    if not ok:
        return _res(False, "E_ARGS", msg)
    return _res(True, "OK", "", {"changes": changes, "master": s["master"], "categories": s["categories"], "quiet": s["quiet"], "limits": s["limits"]})


# ------------------------------------------------------------------ перезапуск, время, возобновление
def _supervisor_started(root):
    try:
        from tb.platform import status as ST
        s = ST.supervisor_info(root)
        return s.get("started") if s.get("running") else None
    except Exception:
        return None


def resume_day(root, args, now):
    """Включить торговый флаг после перезапуска платформы (кнопка «Возобновить день»; в ctl пока нет такого действия — интеграция)."""
    ctx = runtime.Ctx(root, now)
    d = days.current(root)
    if d is None or d.get("state") != "running" or not d.get("needs_resume"):
        return _res(False, "E_STATE", "возобновлять нечего")
    d["needs_resume"] = False
    days.set_trading(root, d.get("mode") != "observe", d)
    days.clear_banner(root)
    days.save(root, d)
    return _res(True, "OK", "", {"day_id": d["day_id"]})


def _auto_stop_reason(ctx, d, now):
    cfg = ctx.cfg
    st = d.get("started") or now
    if now - st > float(cfg["max_day_hours"]) * 3600.0:
        return "max_day_hours"
    mins = cfgmod.parse_hhmm(cfg.get("day_end_utc"))
    if mins is not None:
        end = int(st // 86400) * 86400 + mins * 60
        if end <= st:
            end += 86400
        if now >= end:
            return "day_end_utc"
    if cfg.get("stop_day_on_session_end") and (d.get("session") or {}).get("id"):
        try:
            sess, _o = session.load_all()
            s = sess.get((d.get("session") or {}).get("id"))
            if s and (s.get("stopped") or now > s.get("expires", now + 1)):
                return "session_end"
        except Exception:
            pass
    return None


def tick(root, now):
    """Цикл ctl. -> {"day_id","state","changed","mode","agent"} (нет дня: state=stopped, day_id=None). Исключения не пробрасываются (None)."""
    try:
        return _tick(root, now)
    except Exception:
        return None


def _tick(root, now):
    d = days.current(root)
    if d is None:
        return {"day_id": None, "state": "stopped", "changed": False, "mode": None, "agent": None}
    ctx = runtime.Ctx(root, now)
    now = ctx.now()
    before = d["state"]
    lc = ctx.cfg["resume"]
    if d["state"] == "starting":
        if days.runner_stale(d, now, float(lc["runner_stale_s"])):
            if now - float(d.get("started") or now) > float(lc["starting_abort_s"]):
                starter.abort(ctx, d, "resume", "E_STATE", {"why": "aborted_start: старт завис и не возобновлён за %d с" % lc["starting_abort_s"]})
            else:
                days.touch(ctx.root, d, now)
                if runtime.sync_mode():
                    starter.run_start(ctx, d)
                else:
                    threading.Thread(target=starter.run_start, args=(ctx, d), daemon=True).start()
    elif d["state"] == "running":
        if d.get("runner", {}).get("pid") != _pid() and not d.get("needs_resume"):
            sup = _supervisor_started(root)
            if sup is not None and sup > float(d.get("started") or 0):
                d["needs_resume"] = True
                days.set_trading(root, False, d)
                days.set_banner(root, "warn", "Платформа перезапущена: торговля выключена до подтверждения (день %s)" % d["day_id"], d["day_id"])
            d["runner"] = {"pid": _pid(), "hb": round(now, 1)}
            days.save(root, d)
        why = _auto_stop_reason(ctx, d, now)
        if why:
            _begin_close(ctx, d, "auto")
            d["closing"]["auto_reason"] = why
            audit.run_close(ctx, d)
            days.set_banner(root, "warn", "День %s закрыт автоматически (%s)" % (d["day_id"], why), d["day_id"], report=True)
    elif d["state"] == "closing":
        if now - _LAST_CLOSE.get(root, 0.0) >= CLOSE_TICK_S:
            _LAST_CLOSE[root] = now
            c6 = days.get_step(d, "C6", "stop")
            if c6 and c6.get("ok") is False and (d.get("report") or {}).get("ready"):
                _recheck_failed(ctx, d)
            else:
                audit.run_close(ctx, d)
    elif d["state"] == "halted":
        if exec_cfg.kill_state(root) is None:
            days.set_state(root, d, "stopped", now, code="kill_cleared")
    d = days.load(root, d["day_id"]) or d
    return {"day_id": d["day_id"], "state": d["state"], "changed": d["state"] != before, "mode": d.get("mode"), "agent": d.get("agent")}


def _pid():
    import os
    return os.getpid()


def _recheck_failed(ctx, d):
    """Закрытие было провалено (исполнитель не подтвердил): если позиции уже исчезли, день можно закрыть без ручной пометки."""
    inp = audit.collect_input(ctx, d)
    left = (d.get("closing", {}).get("failed") or {}).get("items") or []
    pos_left = [p for p in inp["positions"] if p.get("owner") and any(it.endswith(p["owner"]) for it in left)]
    plans_left = [pid for pid in (d["closing"].get("cancel_cmds") or {}) if pid in inp["plans"] and inp["plans"][pid].get("status") == "armed"]
    if not pos_left and not plans_left and not (d["closing"].get("manual")):
        d["closing"]["failed"] = None
        c6 = days.get_step(d, "C6", "stop")
        c6["ok"], c6["code"] = True, "recovered"
        days.save(ctx.root, d)
        days.set_state(ctx.root, d, "stopped", ctx.now(), code="recovered")
        days.set_trading(ctx.root, False, d)
        days.clear_banner(ctx.root)


def banner(root):
    return days.banner(root)


def current_day(root):
    return days.current(root)
