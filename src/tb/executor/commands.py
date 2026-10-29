"""Команды из _inbox: submit/amend/stop/close/cancel/hold/release/adopt (01 п. 1.11; 03 п. 3.1). Владелец: WP5.

handle(ex, cmd) -> dict результата для _outbox: {"ok","status","plan_id","rev","errors":[],"warnings":[],"guard_preview":{}}.
Любая команда пишется в журнал решений (kind cmd + accept/reject). Мозг не передаёт размер/плечо/цену ордера: их считает Gate/guard.
"""
import argparse
import time

from tb.core import decisions, exec_cfg, plan_schema, session
from tb.executor import gate as G
from tb.executor import protect
from tb.executor.ports import PortError

ACTORS = decisions.ACTORS


def _created_by(actor):
    """created_by плана: значение enum схемы; агенты шлюза (brain:<id>) записываются как brain:api (кто именно: журнал решений)."""
    if actor in plan_schema.CREATED_BY:
        return actor
    return "brain:api" if decisions.valid_actor(actor) and str(actor).startswith("brain:") else "owner"


def _err(code, msg, path="", status="rejected"):
    return {"ok": False, "status": status, "errors": [{"code": code, "path": path, "msg": msg}], "warnings": []}


def _issues(iss):
    return [{"code": i["code"], "path": i.get("path", ""), "msg": i.get("msg", "")[:200]} for i in iss]


def _load_json(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _find_plan(ex, pid):
    p = ex.plans.get(pid)
    if p is not None:
        return p
    spec, rt = ex.store.load(pid)
    return {"spec": spec, "rt": rt} if spec else None


def handle(ex, cmd):
    op = cmd.get("op")
    cid = cmd.get("cmd_id")
    actor = cmd.get("actor") if decisions.valid_actor(cmd.get("actor")) else "owner"
    ex.decide("cmd", str(op or "?")[:30], plan=cmd.get("plan_id") if isinstance(cmd.get("plan_id"), str) and cmd["plan_id"].startswith("PL-") else None,
              why="CMD:%s %s" % (cid, cmd.get("reason") or ""), actor=actor, fsync=True)
    fn = HANDLERS.get(op)
    if fn is None:
        return _err("E_SCHEMA", "неизвестная операция %r" % (op,))
    res = fn(ex, cmd)
    pid = res.get("plan_id") or cmd.get("plan_id")
    kind = "accept" if res.get("ok") else "reject"
    try:
        ex.decide(kind, str(op)[:30], plan=pid if isinstance(pid, str) and pid.startswith("PL-") else None,
                  res={"ok": bool(res.get("ok")), "code": (res.get("errors") or [{"code": "OK"}])[0]["code"]}, why="CMD:%s" % cid, actor="exec")
    except Exception:
        pass
    return res


# ---------------------------------------------------------------- вспомогательное
def guard_summary(g):
    """Сводка guard для guard_preview. Маржа и риск относятся к ФАКТИЧЕСКОМУ размеру (с size_mult): margin_q, а не margin полного размера guard."""
    out = {k: g[k] for k in ("ok", "leverage", "margin", "risk_usdt", "net_rr", "stop_pct", "qty") if k in g and g[k] is not None}
    if g.get("margin_q") is not None:
        out["margin"] = g["margin_q"]
    if g.get("reasons"):       # отказ guard/MIN_SIZE: пустое {"ok": true} вводило бы в заблуждение
        out["reasons"] = [str(x)[:100] for x in g["reasons"][:3]]
    return out


def _price(ex, sym):
    t = ex.px.get(sym)
    if t and ex.clock() - t["ts"] <= float(ex.cfg["connectivity"]["public_price_stale_s"]):
        return t["last"], ex.clock() - t["ts"]
    try:
        q = ex.port.quote(sym)
        if q and q[2]:
            return float(q[2]), 0.0
    except Exception:
        pass
    return None, None


def _submits_1h(ex, now):
    n = 0
    for r in decisions.last(300, root=ex.root):
        if r.get("kind") == "cmd" and r.get("what") == "submit" and now - float(r.get("t") or 0) < 3600.0:
            n += 1
    return n


def _ctx(ex, plan, now):
    sym = plan["symbol"]
    ins = ex.instrument(sym)
    price, age = _price(ex, sym)
    paper = plan.get("mode") == "paper" or ex.port.env == "paper"
    active = [{"spec": p["spec"], "status": p["rt"].get("status")} for p in ex.active_plans()]
    for s, x in ex.exch_pos.items():
        if not any(p["spec"]["symbol"] == s for p in ex.active_plans()):
            active.append({"spec": {"symbol": s, "mode": plan.get("mode", "live")}, "status": "armed"})
    prof = {}
    try:
        import json
        from tb import paths
        prof = _load_json(paths.config("risk_profile.json", root=ex.root))
    except Exception:
        pass
    ctx = {"now": now, "limits": ex.cfg, "instrument": ins and dict(ins, qty_step=ins.get("qty_step"), min_qty=ins.get("min_qty")), "price": price,
           "price_age_s": age, "profile": prof, "active_plans": active, "cooldowns": protect.cooldowns(ex.trades(), now, ex.cfg["protections"]),
           "submits_1h": _submits_1h(ex, now)}
    if not paper:
        ctx["session"] = session.active()
    if ins is not None and price:
        ref = plan_schema.entry_ref(plan, price)
        try:
            ex_ = ex.gate.preview(plan, ref or price, ins, paper)
            ctx["guard"] = ex_
        except Exception:
            pass
    return ctx


# ---------------------------------------------------------------- submit
def cmd_submit(ex, cmd):
    now = ex.clock()
    cid = cmd.get("cmd_id")
    for p in ex.plans.values():     # повторная доставка после падения между созданием плана и inbox_done
        if p["rt"].get("cmd_id") == cid:
            return {"ok": True, "status": p["rt"]["status"], "plan_id": p["spec"]["id"], "rev": p["spec"]["rev"], "warnings": [], "errors": []}
    raw = cmd.get("payload")
    plan, iss = plan_schema.normalize(raw, now)
    if not any(i["severity"] == "error" for i in iss):
        iss += plan_schema.validate_static(plan)
    if (plan.get("entry") or {}).get("how", {}).get("type") == "adopt":
        return _submit_adopt(ex, cmd, plan, iss)
    if not any(i["severity"] == "error" for i in iss):
        ctx = _ctx(ex, plan, now)
        iss += plan_schema.validate_context(plan, ctx)
    else:
        ctx = {}
    errs = [i for i in iss if i["severity"] == "error"]
    warns = [i for i in iss if i["severity"] != "error"]
    if not errs and plan.get("mode") == "paper":
        try:
            from tb.core import expstats
            act = [p["spec"] for p in ex.active_plans() if p["spec"].get("mode") == "paper"]
            ok = expstats.can_start_paper(act, plan, int(ex.cfg["paper"]["max_active"]))
            if ok is not True and ok not in (None,) and not (isinstance(ok, tuple) and ok[0]):
                errs.append({"code": "E_LIMIT_PLANS", "severity": "error", "path": "mode", "msg": "лимит бумажных планов"})
        except Exception:
            pass
    if errs:
        ex.emit("plan_rejected", {"rev": 1, "codes": sorted(set(i["code"] for i in errs))[:8]})
        return {"ok": False, "status": "rejected", "errors": _issues(errs), "warnings": _issues(warns)}
    pid, spec, p = _register(ex, plan, cid, cmd, now, ctx.get("guard") or {}, warns)
    g = ctx.get("guard") or {}
    res = {"ok": True, "status": p["rt"]["status"] if p["rt"]["status"] in plan_schema.STATUSES else "armed", "plan_id": pid, "rev": spec["rev"],
           "warnings": _issues(warns), "guard_preview": guard_summary(g), "errors": []}
    shadows = _spawn_shadows(ex, spec, cmd, now)
    if shadows:
        res["shadows"] = shadows
    return res


def _register(ex, plan, cid, cmd, now, g, warns):
    """Создать план в хранилище и взвести его: -> (plan_id, spec, p)."""
    from tb.executor import core
    spec = dict(plan)
    spec["created_ts"] = now
    spec["created_by"] = _created_by(cmd.get("actor"))
    spec["armed_ts"] = now
    if g:
        spec["guard_preview"] = guard_summary(g)
    spec["prereg_hash"] = plan_schema.canon_hash(plan)
    rt = core.new_runtime(now)
    rt["cmd_id"] = cid
    rt["paper"] = plan.get("mode") == "paper" or ex.port.env == "paper"
    pid = ex.store.create(spec, rt)
    spec, rt = ex.store.load(pid)
    p = {"spec": spec, "rt": rt}
    ex.plans[pid] = p
    ex._arm(p)
    ex.emit("plan_armed", {"rev": spec["rev"], "codes": sorted(set(i["code"] for i in warns))[:8],
                           "preview": {k: v for k, v in guard_summary(g).items() if k in ("leverage", "risk_usdt", "net_rr") and isinstance(v, (int, float))}},
            plan=pid, sym=spec["symbol"])
    ex.need_reconcile = False
    return pid, spec, p


def _spawn_shadows(ex, parent, cmd, now):
    """Теневые планы (WP7 shadow_variants) для плана с тегом эксперимента в бумажном профиле: бумажная копия родителя с патчем на вариант.
    Каждый вариант проходит ту же статическую и контекстную проверку и can_start_paper (лимит бумажных планов, одна монета в варианте).
    Сбой одного варианта не влияет на родителя. -> список {"plan_id","name"} созданных."""
    if ex.port.env != "paper" or not (parent.get("tags") or {}).get("experiment") or not parent.get("shadow_variants"):
        return []
    from tb.core import expstats
    out = []
    try:
        reps = expstats.shadow_variants_report(parent)
    except Exception:
        return []
    for rep in reps:
        if not rep.get("ok"):
            ex.decide("event", "shadow_skip", plan=parent["id"], why="SHADOW:%s %s" % (rep.get("name"), (rep.get("issues") or [{}])[0].get("code")))
            continue
        try:
            plan, iss = plan_schema.normalize(rep["spec"], now)
            if not any(i["severity"] == "error" for i in iss):
                iss += plan_schema.validate_static(plan)
            if any(i["severity"] == "error" for i in iss):
                ex.decide("event", "shadow_skip", plan=parent["id"], why="SHADOW:%s INVALID" % rep.get("name"))
                continue
            ctx = _ctx(ex, plan, now)
            iss += plan_schema.validate_context(plan, ctx)
            errs = [i for i in iss if i["severity"] == "error"]
            act = [q["spec"] for q in ex.active_plans() if q["spec"].get("mode") == "paper"]
            ok = expstats.can_start_paper(act, plan, int(ex.cfg["paper"]["max_active"]))
            if errs or not ok[0]:
                ex.decide("event", "shadow_skip", plan=parent["id"], why="SHADOW:%s %s" % (rep.get("name"), (errs[0]["code"] if errs else ok[1])))
                continue
            pid, _, _ = _register(ex, plan, None, cmd, now, ctx.get("guard") or {}, [i for i in iss if i["severity"] != "error"])
            ex.decide("event", "shadow_spawn", plan=pid, why="SHADOW:parent=%s variant=%s" % (parent["id"], rep.get("name")))
            out.append({"plan_id": pid, "name": rep.get("name")})
        except Exception as e:      # теневой вариант вспомогателен: исключение не должно ронять приём родителя
            ex.error("shadow", type(e).__name__)
    return out


def _submit_adopt(ex, cmd, plan, iss):
    sym = plan.get("symbol")
    ex_pos = ex.exch_pos.get(sym)
    ctx = {"now": ex.clock(), "position": ex_pos} if ex_pos else {}
    return _err("E_ADOPT_NO_POSITION", "adopt: используйте plan adopt SYM") if not ex_pos else _do_adopt(ex, cmd, sym, plan.get("manage"))


# ---------------------------------------------------------------- adopt
def cmd_adopt(ex, cmd):
    pl = cmd.get("payload") or {}
    sym = str(pl.get("symbol") or "").upper()
    return _do_adopt(ex, cmd, sym, pl.get("manage"))


def _do_adopt(ex, cmd, sym, manage):
    from tb.executor import core
    now = ex.clock()
    try:
        ps = [x for x in ex.port.positions() if x["sym"] == sym and x["size"] > 0]
    except PortError:
        return _err("E_ADOPT_NO_POSITION", "не удалось прочитать позиции", status="error")
    if not ps:
        return _err("E_ADOPT_NO_POSITION", "позиции по %s нет" % sym, "symbol")
    pos = ps[0]
    if not pos.get("sl") or pos["sl"] <= 0:
        return _err("E_ADOPT_NO_STOP", "на бирже у позиции нет стопа", "stop.price")
    for p in ex.active_plans(sym):
        return _err("E_SYMBOL_BUSY", "по %s уже есть план %s" % (sym, p["spec"]["id"]), "symbol")
    side, avg, sl = pos["side"], float(pos["avg"]), float(pos["sl"])
    r = abs(avg - sl)
    s = 1.0 if side == "long" else -1.0
    tp = float(pos["tp"]) if pos.get("tp") and s * (pos["tp"] - avg) > 0 else avg + s * 10 * r
    sess = session.active()
    raw = {"schema": plan_schema.SCHEMA, "symbol": sym, "side": side, "mode": "live" if ex.port.env != "paper" else "paper",
           "session_type": session.geometry_type(sess), "source": {"kind": "adopt"},
           "thesis": "Принятая под управление позиция %s %s" % (sym, side), "entry": {"how": {"type": "adopt"}},
           "stop": {"price": sl, "trigger_by": "last"}, "targets": [{"id": "t1", "price": tp, "frac": 1.0, "exec": "touch"}],
           "manage": manage if isinstance(manage, list) else [], "lifecycle": {}}
    plan, iss = plan_schema.normalize(raw, now)
    iss += plan_schema.validate_static(plan)
    errs = [i for i in iss if i["severity"] == "error"]
    if errs:
        return {"ok": False, "status": "rejected", "errors": _issues(errs), "warnings": []}
    spec = dict(plan, created_ts=now, armed_ts=now, created_by=_created_by(cmd.get("actor")))
    spec["prereg_hash"] = plan_schema.canon_hash(plan)
    rt = core.new_runtime(now)
    rt["cmd_id"] = cmd.get("cmd_id")
    pid = ex.store.create(spec, rt)
    spec, rt = ex.store.load(pid)
    p = {"spec": spec, "rt": rt}
    ex.plans[pid] = p
    ex.adopt_position(p, pos)
    return {"ok": True, "status": p["rt"]["status"], "plan_id": pid, "rev": spec["rev"], "warnings": [], "errors": []}


# ---------------------------------------------------------------- amend
def cmd_amend(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    spec, rt = p["spec"], p["rt"]
    status = rt["status"]
    patch = cmd.get("payload") or {}
    pos = status in ("entered", "managing")
    exch = float((ex.exch_pos.get(spec["symbol"]) or {}).get("sl") or 0) or None
    cur_stop = rt["pos"].get("cur_stop")
    be = bool(pos and cur_stop and rt["pos"].get("avg") and ((cur_stop - rt["pos"]["avg"]) * (1 if spec["side"] == "long" else -1)) >= 0)
    live = {"exch_stop": exch if pos else None, "position": pos, "stop_at_or_better_be": be,
            "filled_targets": [t for t, v in (rt.get("targets") or {}).items() if v.get("state") == "filled"],
            "has_order": rt["entry"].get("state") in ("placing", "resting")}
    new, iss = plan_schema.amend(spec, patch, status, live, cmd.get("rev_expected"), ex.clock())
    errs = [i for i in iss if i["severity"] == "error"]
    warns = [i for i in iss if i["severity"] != "error"]
    if new is None or errs:
        return {"ok": False, "status": "rejected", "errors": _issues(errs), "warnings": _issues(warns), "plan_id": spec["id"]}
    if not pos and status == "armed":
        if (new.get("entry") != spec.get("entry") or new.get("stop") != spec.get("stop")) and rt["entry"].get("state") in ("placing", "resting"):
            r = ex.act.cancel_entry_order(p, why="AMEND")
            if r == "filled":
                ex._poll_entry(p)
                return _err("E_STATUS_FORBIDS", "amend: вход уже заполнен", status="rejected")
            rt["entry"].update({"state": "waiting", "link_id": None, "order_id": None, "cond_state": {}, "fills": []})
        elif new.get("entry") != spec.get("entry"):
            rt["entry"]["cond_state"] = {}
    stop_to = None
    if pos and new["stop"]["price"] != spec["stop"]["price"]:
        stop_to = float(new["stop"]["price"])
    new = ex.store.save_spec(dict(new, rev=spec["rev"]), "amend:%s" % (cmd.get("reason") or "")[:100])
    p["spec"] = new
    rt["rev_applied"] = new["rev"]
    if stop_to is not None:
        kind, info = ex.act.move_stop(p, stop_to, by="cmd", why="CMD:%s" % cmd.get("cmd_id"))
        if kind not in ("ok", "unchanged"):
            ex.save(p, sync=True)
            return {"ok": False, "status": "partial", "plan_id": new["id"], "rev": new["rev"],
                    "errors": [{"code": "E_STOP_WIDEN" if info.get("code") in ("STOP_NOT_TIGHTER", "INVARIANT_VIOLATION") else "E_STATUS_FORBIDS", "path": "stop.price",
                                "msg": "стоп не применён: %s" % info.get("code")}], "warnings": _issues(warns)}
    if pos:
        _resync_targets(ex, p, spec, new)
    ex.save(p, sync=True)
    return {"ok": True, "status": rt["status"], "plan_id": new["id"], "rev": new["rev"], "warnings": _issues(warns), "errors": []}


def _resync_targets(ex, p, old, new):
    rt = p["rt"]
    old_t = dict((t["id"], t) for t in old.get("targets") or [])
    new_t = dict((t["id"], t) for t in new.get("targets") or [])
    for tid, t in list(rt.get("targets", {}).items()):
        if tid not in new_t:
            ex.act.cancel_target(p, tid, why="AMEND")
        elif new_t[tid]["price"] != t.get("price") and t.get("state") in ("resting", "touch", "pending"):
            ex.act.move_target(p, tid, float(new_t[tid]["price"]), why="AMEND")
    if any(tid not in rt.get("targets", {}) for tid in new_t):
        ex.act.place_ladder(p, why="AMEND")


# ---------------------------------------------------------------- stop / close / cancel / hold / release
def cmd_stop(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    spec, rt = p["spec"], p["rt"]
    iss = plan_schema.check_rev(spec, cmd.get("rev_expected"))
    if iss:
        return {"ok": False, "status": "rejected", "errors": _issues(iss), "warnings": []}
    to = (cmd.get("payload") or {}).get("to")
    if rt["status"] == "armed":
        return cmd_amend(ex, dict(cmd, payload={"stop": {"price": to}}) if to != "be" else cmd) if to != "be" else _err("E_STATUS_FORBIDS", "be без позиции", "stop")
    if rt["status"] not in ("entered", "managing"):
        return _err("E_STATUS_FORBIDS", "статус %s" % rt["status"], "status")
    pos = rt["pos"]
    s = 1.0 if spec["side"] == "long" else -1.0
    if to == "be":
        from tb.logic import rules
        to = pos["avg"] * (1 + s * rules._be_offset_bps(rt, ex.limits_for(p)) / 1e4)
        tick = float((ex.instrument(spec["symbol"]) or {}).get("tick") or 0)
        if tick > 0:
            import math
            to = (math.ceil(to / tick - 1e-9) if s > 0 else math.floor(to / tick + 1e-9)) * tick
    try:
        to = float(to)
    except (TypeError, ValueError):
        return _err("E_RANGE", "to", "to")
    kind, info = ex.act.move_stop(p, to, by="cmd", why="CMD:%s" % cmd.get("cmd_id"))
    if kind in ("ok", "unchanged"):
        return {"ok": True, "status": rt["status"], "plan_id": spec["id"], "rev": spec["rev"], "warnings": [], "errors": []}
    code = "E_STOP_WIDEN" if info.get("code") in ("STOP_NOT_TIGHTER", "NOT_TIGHTER", "INVARIANT_VIOLATION") else "E_STATUS_FORBIDS"
    return _err(code, "стоп не применён: %s %s" % (info.get("code"), info.get("detail", "")), "to")


def cmd_close(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    spec, rt = p["spec"], p["rt"]
    if rt["status"] not in ("entered", "managing"):
        return _err("E_STATUS_FORBIDS", "закрывать можно только позицию; до входа используйте cancel", "status")
    frac = float((cmd.get("payload") or {}).get("frac", 1.0))
    a = {"type": "close", "frac": frac, "reason": "manual", "why": "CMD:%s" % cmd.get("cmd_id"), "rule": None}
    ok = ex._do_close(p, a, ex.view(spec["symbol"]))
    if not ok:
        return _err("E_STATUS_FORBIDS", "закрытие не выполнено (Gate/биржа)", "frac", status="error")
    return {"ok": True, "status": rt["status"], "plan_id": spec["id"], "rev": spec["rev"], "warnings": [], "errors": []}


def cmd_cancel(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    spec, rt = p["spec"], p["rt"]
    if rt["status"] != "armed":
        return _err("E_STATUS_FORBIDS", "cancel только для armed; в позиции используйте close", "status")
    if ex.end_plan(p, "cancelled", "CMD"):
        return {"ok": True, "status": "cancelled", "plan_id": spec["id"], "rev": spec["rev"], "warnings": [], "errors": []}
    return _err("E_STATUS_FORBIDS", "cancel: filled", "status")


def cmd_hold(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    rt = p["rt"]
    if rt["status"] in plan_schema.TERMINAL:
        return _err("E_STATUS_FORBIDS", "терминальный статус", "status")
    rt["hold"], rt["hold_reason"] = "brain", "brain"
    ex.save(p, sync=True)
    return {"ok": True, "status": rt["status"], "plan_id": p["spec"]["id"], "rev": p["spec"]["rev"], "warnings": [], "errors": []}


def cmd_release(ex, cmd):
    p = _find_plan(ex, cmd.get("plan_id"))
    if p is None:
        return _err("E_ID", "плана нет", "plan_id")
    rt = p["rt"]
    if rt.get("hold") and rt.get("hold_reason") not in ("brain", "manual") and str(cmd.get("actor", "")).startswith("brain"):
        return _err("E_STATUS_FORBIDS", "системный hold снимает исполнитель или владелец", "hold")
    rt["hold"], rt["hold_reason"] = None, None
    ex.save(p, sync=True)
    return {"ok": True, "status": rt["status"], "plan_id": p["spec"]["id"], "rev": p["spec"]["rev"], "warnings": [], "errors": []}


HANDLERS = {"submit": cmd_submit, "amend": cmd_amend, "stop": cmd_stop, "close": cmd_close, "cancel": cmd_cancel, "hold": cmd_hold,
            "release": cmd_release, "adopt": cmd_adopt}
