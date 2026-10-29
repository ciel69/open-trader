"""Остановка дня: шаги C1-C8 (07 п. 2.4), аудит закрытия детерминированным чек-листом и консультация day_auditor. WP14.

Все шаги идемпотентны и НЕ блокируют ctl: ожидание (ответ мозга, ответ владельца, подтверждение закрытия исполнителем) — состояние `pending`, которое
подхватывает следующий `tick`. C1, C6, C7 выполняются при полном отказе LLM. Исполнитель управляется только очередью команд плана (close/cancel/adopt),
подтверждение — по снимку `exec_snapshot.json` и статусам планов; прямых биржевых вызовов нет.
"""
import json
import os

from tb import paths
from tb.control import ctlstate
from tb.core import bus, risklevels, session
from tb.core.filelock import atomic_write_json
from tb.lifecycle import briefing, days
from tb.lifecycle import carry as CARRY
from tb.lifecycle import checklist as CL

STEPS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")
AUDIT_SCHEMA = "tb.audit/1"


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _wr(root, d, name, obj):
    atomic_write_json(os.path.join(days.day_dir(root, d["day_id"]), name), obj, indent=1)


def _rd(root, d, name):
    x = ctlstate.read_json(os.path.join(days.day_dir(root, d["day_id"]), name))
    return x


# ------------------------------------------------------------------ вход аудита
def collect_input(ctx, d):
    """Снимок для чек-листа и агента (только файлы и порты, без сети)."""
    root, now = ctx.root, ctx.now()
    snap = ctx.execp.snapshot(now)
    h = ctx.execp.health(now)
    hd = dict(h.get("data") or {})
    hd["alive"] = bool(h.get("alive"))
    plans = dict((p["id"], p) for p in ctx.execp.plans() if p.get("id"))
    prof = ctlstate.read_json(paths.config("risk_profile.json", root=root)) or {}
    pol = risklevels.carry_policy(root)
    dp = briefing.read_dayplan(root, d["day_id"])
    facts = briefing.read_facts(root)
    c = briefing.caps(ctx, d)
    ok_brain, _m = ctx.brain.available(root, now)
    alerts = {}
    lim = float(ctx.cfg["carry"]["reconcile_alert_min"]) * 60.0
    try:
        for e in bus.read_tail(300, root=root):
            if e.get("type") == "reconcile_alert" and e.get("sym"):
                t = bus.parse_iso(e.get("ts"))
                if t is not None and now - t <= lim:
                    alerts[e["sym"]] = t
    except Exception:
        pass
    prov_ok = bool(ok_brain)
    if not prov_ok:
        try:
            from tb.llm import usage
            prov_ok = any(r.get("ok") for r in usage.read_rows(root, since=now - 1800.0) if r.get("kind") != "fmt")
        except Exception:
            prov_ok = False
    fts = facts.get("ts") if isinstance(facts, dict) else None
    plan_cap = None
    if isinstance(dp, dict) and (dp.get("meta") or {}).get("source") == "agent":
        plan_cap = (dp.get("carry") or {}).get("max_positions")
    act = None
    try:
        act = session.active()
    except Exception:
        act = None
    from tb.lifecycle import cfg as cfgmod
    return {"now": now, "next_day_start": cfgmod.next_day_start(now, ctx.cfg), "cfg": ctx.cfg, "policy": pol, "profile": prof, "equity": c.get("equity"),
            "daily_loss_usdt": c.get("daily_loss_usdt") if _num(c.get("daily_loss_usdt")) else float(prof.get("daily_loss_limit_usdt") or 0.0),
            "health": hd, "snap": snap, "positions": [p for p in snap.get("positions") or [] if _num(p.get("size")) and p["size"] > 0], "plans": plans, "facts": facts,
            "facts_age_s": (now - fts) if _num(fts) else None, "dayplan": dp, "plan_max_positions": plan_cap, "brain_ok": bool(ok_brain), "provider_recent_ok": prov_ok,
            "alerts": alerts, "sessions": [act] if act else [], "ideas_n": 0}


def rows_for(ctx, d):
    inp = collect_input(ctx, d)
    res = CL.evaluate(inp)
    rows = CARRY.finalize_defaults(res["rows"])
    return inp, rows


def _save_rows(ctx, d, rows):
    _wr(ctx.root, d, "checklist.json", {"day_id": d["day_id"], "ts": round(ctx.now(), 1), "rows": CL.public_rows(rows)})


def load_rows(root, d):
    x = _rd(root, d, "checklist.json")
    return x.get("rows") if isinstance(x, dict) and isinstance(x.get("rows"), list) else []


def _c(d):
    return d.setdefault("closing", {})


# ------------------------------------------------------------------ шаги
def c1_freeze(ctx, d):
    """Отменить все ждущие входы (кроме разрешённых переносом A1). Команды в очередь; подтверждение проверит C6."""
    inp, rows = rows_for(ctx, d)
    cmds = _c(d).setdefault("cancel_cmds", {})
    n = kept = 0
    for r in rows:
        if r["kind"] != "entry":
            continue
        p = inp["plans"].get(r["plan"]) or {}
        if r["decision"] == "keep":
            kept += 1
            continue
        if r["decision"] == "cancel" and p.get("status") == "armed" and r["plan"] not in cmds:
            cmds[r["plan"]] = {"cmd": ctx.execp.send("cancel", r["plan"], "eod: отмена ждущего входа (%s)" % d["day_id"], now=ctx.now(), tag="c1"), "ts": round(ctx.now(), 1)}
            n += 1
    return "ok", {"cancel_sent": n, "kept": kept}, None


def c2_snapshot(ctx, d):
    inp = collect_input(ctx, d)
    cc = _c(d)
    cc.setdefault("c2_ts", round(ctx.now(), 1))
    diffs = (inp["health"] or {}).get("diffs")
    if diffs and ctx.now() - cc["c2_ts"] < float(ctx.cfg["closing"]["reconcile_wait_s"]) and inp["health"].get("alive"):
        return "pending", {"waiting": "reconcile", "diffs": diffs}, None
    fx = inp.get("facts") or {}
    summary = {"day_id": d["day_id"], "ts": round(ctx.now(), 1), "positions": [{k: p.get(k) for k in ("sym", "side", "size", "avg", "mark", "sl", "liq", "owner")} for p in inp["positions"]],
               "plans": [{"id": p["id"], "sym": p.get("sym"), "stype": p.get("stype"), "status": p.get("status"), "hold": p.get("hold")} for p in inp["plans"].values()],
               "sessions": [{"id": s.get("id"), "type": s.get("type")} for s in inp["sessions"]], "calendar": (fx.get("calendar") or {}) if isinstance(fx, dict) else {},
               "carry_policy": inp["policy"], "daily_loss_usdt": inp["daily_loss_usdt"], "exec_alive": bool(inp["health"].get("alive")), "diffs": diffs or 0,
               "llm_available": inp["brain_ok"]}
    _wr(ctx.root, d, "audit_input.json", summary)
    return "ok", {"positions": len(inp["positions"]), "plans": len(inp["plans"]), "diffs": diffs or 0}, None


def c3_checklist(ctx, d):
    inp, rows = rows_for(ctx, d)
    _save_rows(ctx, d, rows)
    return "ok", {"rows": len(rows), "candidates": sum(1 for r in rows if r.get("candidate"))}, None


def _auditor_payload(inp, rows, d):
    return {"schema": briefing.JOB_SCHEMA, "kind": "audit", "day_id": d["day_id"], "items": [r["item"] for r in rows if r.get("candidate")],
            "rows": [r for r in CL.public_rows(rows) if r.get("candidate")], "policy": inp["policy"], "facts": inp.get("facts"), "dayplan": inp.get("dayplan"),
            "next_day_start": inp["next_day_start"], "daily_loss_usdt": inp["daily_loss_usdt"]}


def c4_agent(ctx, d):
    cc = _c(d)
    inp, rows = rows_for(ctx, d)
    cands = [r for r in rows if r.get("candidate")]
    audit = {"schema": AUDIT_SCHEMA, "day_id": d["day_id"], "llm": None, "agent": [], "rejected": [], "narrative": "", "questions": []}
    job = cc.get("job")
    if job is None:
        if not cands:
            audit["llm"] = "no_candidates"
            _wr(ctx.root, d, "audit.json", audit)
            return "ok", {"llm": "no_candidates"}, None
        ok, msg = ctx.brain.available(ctx.root, ctx.now())
        if not ok:
            audit["llm"] = "unavailable"
            _wr(ctx.root, d, "audit.json", audit)
            return "ok", {"llm": "unavailable", "amber": True}, None
        tmo = float(ctx.cfg["closing"]["auditor_timeout_s"])
        try:
            jid = ctx.brain.submit("day_auditor", _auditor_payload(inp, rows, d), tmo, ctx.now(), meta={"day_id": d["day_id"]})
        except Exception:
            audit["llm"] = "submit_failed"
            _wr(ctx.root, d, "audit.json", audit)
            return "ok", {"llm": "submit_failed", "amber": True}, None
        cc["job"] = job = {"id": jid, "deadline": round(ctx.now() + tmo, 1)}
        days.save(ctx.root, d)
    r = ctx.brain.poll(job["id"], ctx.now())
    if r is None:
        if ctx.now() >= job["deadline"]:
            try:
                ctx.brain.cancel(job["id"])
            except Exception:
                pass
            audit["llm"] = "timeout"
            _wr(ctx.root, d, "audit.json", audit)
            return "ok", {"llm": "timeout", "amber": True}, None
        return "pending", {"waiting": "day_auditor"}, None
    if not r.get("ok") or not isinstance(r.get("output"), dict):
        audit["llm"] = "error"
        audit["err"] = str(r.get("err") or "")[:40]
        _wr(ctx.root, d, "audit.json", audit)
        return "ok", {"llm": "error", "amber": True}, None
    res = CARRY.apply_decisions(inp, rows, r["output"], ctx.cfg)
    audit.update({"llm": "ok", "agent": res["decisions"], "rejected": res["rejected"], "narrative": res["narrative"], "questions": res["questions"]})
    _wr(ctx.root, d, "audit.json", audit)
    _save_rows(ctx, d, rows)
    own = d.setdefault("owner", {"questions": [], "deadline_ts": None})
    have = set(q["item"] for q in own["questions"])
    for q in res["questions"]:
        if q["item"] in have:
            continue
        own["questions"].append({"id": "Q%d" % (len(own["questions"]) + 1), "item": q["item"], "plan": q["plan"], "text": q["text"], "options": q["options"],
                                 "asked_ts": round(ctx.now(), 1), "answer": None, "answered_by": None, "answered_ts": None})
    if own["questions"] and own.get("deadline_ts") is None:
        own["deadline_ts"] = round(ctx.now() + float(ctx.cfg["closing"]["owner_wait_min"]) * 60.0, 1)
    return "ok", {"llm": "ok", "decisions": len(res["decisions"]), "rejected": len(res["rejected"]), "questions": len(res["questions"])}, None


def pending_questions(d):
    return [q for q in (d.get("owner") or {}).get("questions") or [] if q.get("answer") is None]


def c5_owner(ctx, d):
    qs = pending_questions(d)
    if not qs:
        return "ok", {"questions": len((d.get("owner") or {}).get("questions") or [])}, None
    own = d["owner"]
    if _c(d).get("emergency"):
        for q in qs:
            q.update(answer="close", answered_by="emergency", answered_ts=round(ctx.now(), 1))
        return "ok", {"emergency": True}, None
    if ctx.now() >= (own.get("deadline_ts") or 0):
        for q in qs:
            q.update(answer="close", answered_by="timeout", answered_ts=round(ctx.now(), 1))
        return "ok", {"timeout": True, "closed": len(qs)}, None
    days.set_banner(ctx.root, "warn", "Жду решения по переносу: позиций %d (до %s UTC); без ответа закрою" % (len(qs), days.iso(own["deadline_ts"])[11:16]), d["day_id"])
    return "pending", {"waiting": "owner", "questions": len(qs)}, None


def final_decisions(ctx, d, inp, rows):
    """Итоговые решения: ask_owner -> ответ владельца (нет ответа = close), повторная проверка K1-K8 перед переносом. Возвращает (rows, rechecks[])."""
    qa = dict((q["item"], q) for q in (d.get("owner") or {}).get("questions") or [])
    chosen, rech = [], []
    out = []
    for r in rows:
        r = dict(r)
        r.setdefault("codes", [])
        if r["kind"] == "pos" and r["decision"] in ("ask_owner", "carry"):
            by = r.get("decided_by") or "agent"
            if r["decision"] == "ask_owner":
                q = qa.get(r["item"])
                ans = q.get("answer") if q else None
                by = (q or {}).get("answered_by") or "owner"
                if ans != "carry":
                    r["decision"] = "close"
                    r["codes"] = r["codes"] + ["OWNER_CLOSE" if ans == "close" and by == "owner" else "OWNER_NO_ANSWER"]
                else:
                    r["decision"], by = "carry", "owner"
            if r["decision"] == "carry":
                codes = CARRY.recheck_for_execution(inp, r)
                if not codes:
                    pos = next((p for p in inp["positions"] if p.get("owner") == r["plan"]), None)
                    n = CL.pos_numbers(inp, pos, inp["plans"].get(r["plan"])) if pos else None
                    if n is not None:
                        k3, _fx = CL.k3_check(inp, chosen, n)
                        if k3:
                            codes = [k3]
                        else:
                            chosen.append(n["risk_now"] or 0.0)
                if codes:
                    r["decision"] = "close"
                    r["codes"] = r["codes"] + ["K_FAILED:%s" % codes[0]]
                    rech.append({"item": r["item"], "codes": codes})
                else:
                    r["decided_by"] = by
        out.append(r)
    return out, rech


def _pos_of(inp, plan_id, sym=None):
    """Позиция плана (owner == plan_id) либо, при plan_id=None, позиция БЕЗ владельца по символу."""
    for p in inp["positions"]:
        if plan_id is not None and p.get("owner") == plan_id:
            return p
        if plan_id is None and sym and not p.get("owner") and p.get("sym") == sym:
            return p
    return None


def _c6_pass(ctx, d, cc, inp):
    """Один проход C6: решения -> команды в очередь (каждая позиция один раз, повтор через retry_after_s) -> что ещё не подтверждено снимком."""
    rows = load_rows(ctx.root, d)
    have = set(r.get("item") for r in rows)
    for r in CL.public_rows(CARRY.finalize_defaults(CL.evaluate(inp)["rows"])):      # позиции, появившиеся после C3 (вход успел исполниться): закрываем по умолчанию
        if r["kind"] == "pos" and r["item"] not in have:
            r["decision"], r["codes"], r["candidate"] = "close", r["codes"] + ["LATE_POSITION"], False
            rows.append(r)
    final, rech = final_decisions(ctx, d, inp, rows)
    now = ctx.now()
    cmds = cc.setdefault("close_cmds", {})
    retry_s = float(ctx.cfg["closing"]["retry_after_s"])
    waiting, manual, closed, sent = [], [], [], 0
    for r in final:
        if r["kind"] != "pos" or r["decision"] != "close":
            continue
        item = r["item"]
        if r["plan"] is None:                                      # orphan: сначала adopt (позиция получает план), потом close
            sym = r["sym"]
            pos = _pos_of(inp, None, sym)
            if pos is None:
                continue
            st = cmds.get(item)
            if st is None:
                cmds[item] = {"cmd": ctx.execp.send("adopt", None, "eod: позиция без плана %s, беру под управление для закрытия" % sym, {"symbol": sym}, now=now, tag="adopt-%s" % sym),
                              "ts": round(now, 1), "op": "adopt", "tries": 1}
                sent += 1
                waiting.append(item)
            else:
                res = ctx.execp.result(st["cmd"])
                if res is not None and not res.get("ok"):
                    manual.append(item)
                else:
                    waiting.append(item)
            continue
        pos = _pos_of(inp, r["plan"])
        if pos is None:
            closed.append(item)
            continue
        st = cmds.get(item)
        if st is None or st.get("op") == "adopt":
            cmds[item] = {"cmd": ctx.execp.send("close", r["plan"], "eod: закрытие по чек-листу (%s %s)" % (r["rule"], ",".join(r["codes"][:2])), {"frac": 1.0}, now=now, tag="c6.1"),
                          "ts": round(now, 1), "op": "close", "tries": 1}
            sent += 1
        elif now - st["ts"] >= retry_s and st.get("tries", 1) < 2:
            st["tries"] = st.get("tries", 1) + 1
            st["cmd"] = ctx.execp.send("close", r["plan"], "eod: повтор закрытия", {"frac": 1.0}, now=now, tag="c6.%d" % st["tries"])
            st["ts"] = round(now, 1)
            sent += 1
        waiting.append(item)
    cancels = cc.get("cancel_cmds") or {}
    left_cancel = [pid for pid in cancels if pid in inp["plans"] and inp["plans"][pid].get("status") == "armed"]
    return {"waiting": waiting, "manual": manual, "closed": closed, "left_cancel": left_cancel, "final": final, "rech": rech, "sent": sent}


def c6_execute(ctx, d):
    cc = _c(d)
    cc.setdefault("exec_started", round(ctx.now(), 1))
    for _attempt in range(3):                                        # повторные проходы: быстрый исполнитель (paper) успевает подтвердить (adopt, затем close) в том же вызове
        inp = collect_input(ctx, d)
        out = _c6_pass(ctx, d, cc, inp)
        if not out["sent"] or not (out["waiting"] or out["left_cancel"]):
            break
    now = ctx.now()
    wait_s = float(ctx.cfg["closing"]["exec_wait_s"])
    final, rech, waiting, manual = out["final"], out["rech"], out["waiting"], out["manual"]
    cc["final"] = [{"item": r["item"], "decision": r["decision"], "codes": r["codes"], "decided_by": r.get("decided_by")} for r in final if r["kind"] in ("pos", "entry")]
    if waiting or out["left_cancel"]:
        if now - cc["exec_started"] >= wait_s:
            cc["failed"] = {"items": waiting + out["left_cancel"], "manual": manual, "reason": "exec_down" if not inp["health"].get("alive") else "not_confirmed"}
            days.emit("error", {"where": "lifecycle", "code": "DAY_CLOSE_FAILED"}, ctx.root, now, pri=bus.PRI_WAKE)
            _write_carry(ctx, d, final, inp, rech)
            return "fail", {"not_closed": waiting + out["left_cancel"], "manual": manual}, "day_close_failed"
        return "pending", {"waiting": waiting + out["left_cancel"]}, None
    if manual:
        cc["manual"] = manual
    _write_carry(ctx, d, final, inp, rech)
    return "ok", {"closed": out["closed"], "carried": [r["item"] for r in final if r["decision"] == "carry"], "manual": manual, "rechecks": rech}, None


def _write_carry(ctx, d, final, inp, rech):
    items = []
    audit = _rd(ctx.root, d, "audit.json") or {}
    agent = dict((a.get("item"), a) for a in audit.get("agent") or [])
    for r in final:
        if r["kind"] != "pos" or r["decision"] != "carry":
            continue
        pos = _pos_of(inp, r["plan"])
        if pos is None:
            continue
        n = CL.pos_numbers(inp, pos, inp["plans"].get(r["plan"]))
        a = agent.get(r["item"]) or {}
        items.append({"plan": r["plan"], "sym": r["sym"], "side": r["side"], "stype": r["stype"], "size": n["size"], "avg": n["avg"], "stop": n["sl"],
                      "risk_usdt": round(n["risk_now"] or 0.0, 4), "why": a.get("why") or "перенос по правилам плана (%s)" % r["rule"], "codes": r["codes"],
                      "basis": a.get("basis") or [], "against": a.get("against") or [], "view": a.get("view") or {}, "decided_by": r.get("decided_by") or "default",
                      "rule": r["rule"], "K": dict((k, (v or {}).get("why") is None) for k, v in (r.get("facts") or {}).get("K", {}).items()),
                      "carried_at": round(ctx.now(), 1)})
    atomic_write_json(os.path.join(days.day_dir(ctx.root, d["day_id"]), "carry.json"),
                      {"schema": "tb.carryfile/1", "day_id": d["day_id"], "ts": round(ctx.now(), 1), "next_day_start": inp["next_day_start"], "items": items}, indent=1)


def c7_session(ctx, d):
    cur = session.active()
    if cur is None:
        return "ok", {"session": None}, None
    carried = [x["plan"] for x in (_rd(ctx.root, d, "carry.json") or {}).get("items") or []]
    note = ("eod; carry=[%s]" % ",".join(carried))[:200]
    session._append({"ev": "stop", "id": cur["id"], "ts": session._iso(ctx.now()), "note": note})
    return "ok", {"session": cur["id"], "carry": len(carried)}, None


def c8_report(ctx, d):
    from tb.lifecycle import report
    rep = report.finish(ctx, d, kind="stop")
    return "ok", {"verdict": rep["verdict"], "net": rep["pnl"]["net"]}, None


FUNCS = {"C1": c1_freeze, "C2": c2_snapshot, "C3": c3_checklist, "C4": c4_agent, "C5": c5_owner, "C6": c6_execute, "C7": c7_session, "C8": c8_report}


# ------------------------------------------------------------------ оркестрация
def run_close(ctx, d):
    """Выполняет шаги C1..C8, пока они идут. -> 'done' (всё), 'pending' (ждём), 'failed' (C6 не подтверждено: день остаётся closing)."""
    for name in STEPS:
        st = days.get_step(d, name, "stop")
        if st is not None and st.get("ok") is not None:
            continue
        if st is None:
            days.step_begin(ctx.root, d, name, "stop", ctx.now())
        try:
            status, detail, code = FUNCS[name](ctx, d)
        except Exception as e:
            status, detail, code = "fail", {"why": "исключение %s" % e.__class__.__name__}, "E_INTERNAL"
        now = ctx.now()
        if status == "pending":
            days.save(ctx.root, d)
            return "pending"
        days.step_end(ctx.root, d, name, "stop", status == "ok", now, detail, code)
        if status == "fail" and name not in ("C6",):
            # сбой вспомогательного шага не останавливает закрытие; C1/C6/C7 критичны, но продолжаем: безопасность держат стопы на бирже
            pass
    d["closing"]["done_ts"] = round(ctx.now(), 1)
    c6 = days.get_step(d, "C6", "stop")
    return "failed" if (c6 and c6.get("ok") is False) else "done"
