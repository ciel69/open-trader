"""Старт дня: шаги S1-S9 (07 п. 2.2), откат при провале. Блокирующий (выполняется в потоке старта ctl). WP14."""
import os

from tb import paths
from tb.control import ctlstate
from tb.core import risklevels, session
from tb.lifecycle import briefing, days, preflight, report
from tb.lifecycle import cfg as cfgmod

STEPS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
CODE_OF_STEP = {"S1": "E_GATE", "S3": "E_GATE", "S5": "E_GATE", "S7": "E_LIMITS"}


def _args(d):
    a = dict(d.get("args") or {})
    a["mode"] = d.get("mode")
    a["agent"] = d.get("agent")
    return a


def s1_preflight(ctx, d):
    args = _args(d)
    v = preflight.run(ctx, args, d["day_id"])
    compact = [{"id": c["id"], "sev": c["sev"], "ok": c["ok"], "msg": c["msg"], "action": preflight.FIX.get(c["id"], "")} for c in v["checks"]]
    from tb.core.filelock import atomic_write_json
    atomic_write_json(os.path.join(days.day_dir(ctx.root, d["day_id"]), "preflight.json"), {"ts": round(ctx.now(), 1), "mode": d.get("mode"), "checks": v["checks"]}, indent=1)
    d["preflight"] = {"red": [c["id"] for c in v["red"]], "amber": [c["id"] for c in v["amber"]]}
    blocking_ids = [b["id"] for b in v["blocking"]]
    red_ids = [c["id"] for c in v["red"]]
    if not v["ok"]:
        if any(i in red_ids for i in blocking_ids):       # красные = жёсткие условия
            return "fail", {"kind": "hard", "blocking": [c for c in compact if not c["ok"] and c["id"] in red_ids]}, "E_GATE"
        return "fail", {"kind": "amber", "blocking": [c for c in compact if not c["ok"] and c["id"] in blocking_ids]}, "E_AMBER"
    acc = [x for x in (args.get("accepted") or []) if isinstance(x, str)]
    if args.get("allow_amber") and acc and d.get("mode") != "live":      # владелец принял конкретный набор: новое замечание, появившееся позже, не принято
        extra = [c for c in compact if not c["ok"] and c["id"] in d["preflight"]["amber"] and c["id"] not in acc]
        if extra:
            return "fail", {"kind": "amber", "blocking": extra}, "E_AMBER"
    return "ok", {"amber": bool(v["amber"]), "amber_ids": d["preflight"]["amber"]}, None


def s2_facts(ctx, d):
    return briefing.step_facts(ctx, d, _args(d))


def s3_sync(ctx, d):
    """Сверка портфеля с биржей, инвентаризация позиций и планов, carry.json вчерашнего дня."""
    mode = d.get("mode")
    snap = ctx.execp.snapshot(ctx.now())
    amber = []
    if mode in ("testnet", "live"):
        eq = snap.get("equity")
        pf = ctlstate.read_json(paths.state("portfolio.json", root=ctx.root)) or {}
        tot = sum(float(v.get("equity") or 0.0) for v in (pf.get("venues") or {}).values() if isinstance(v, dict))
        lim = ctlstate.read_json(paths.config("exec_limits.json", root=ctx.root)) or {}
        dev = float((lim.get("equity_sync") or {}).get("max_dev_frac") or 0.05)
        if not isinstance(eq, (int, float)) or (tot and abs(eq - tot) / max(eq, tot, 1e-9) > dev):
            return "fail", {"why": "equity биржи %s и портфель %.2f расходятся (допуск %.0f%%)" % (eq, tot, dev * 100)}, "E_GATE"
    prev = days.last_day(ctx.root, exclude=d["day_id"])
    carried = []
    if prev:
        c = ctlstate.read_json(os.path.join(days.day_dir(ctx.root, prev["day_id"]), "carry.json"))
        plans = dict((p["id"], p) for p in ctx.execp.plans())
        held = set(p.get("owner") for p in snap.get("positions") or [])
        for it in (c or {}).get("items") or []:
            alive = it.get("plan") in held
            carried.append({"plan": it.get("plan"), "sym": it.get("sym"), "side": it.get("side"), "stop": it.get("stop"), "risk_usdt": it.get("risk_usdt"), "alive": alive,
                            "in_plans": it.get("plan") in plans})
            if not alive:
                amber.append(it.get("plan"))
    d["carry_in"] = carried
    return "ok", {"positions": len(snap.get("positions") or []), "plans": len(ctx.execp.plans()), "carry_in": len(carried), "gone": amber, "amber": bool(amber)}, None


def s4_brief(ctx, d):
    return briefing.step_brief(ctx, d, _args(d))


def s5_briefing(ctx, d):
    return briefing.step_briefing(ctx, d, _args(d))


def s6_dayplan(ctx, d):
    return briefing.step_dayplan(ctx, d, _args(d))


def s7_session(ctx, d):
    if d.get("mode") == "observe":
        return "ok", {"skipped": "observe"}, None
    dp = briefing.read_dayplan(ctx.root, d["day_id"]) or {}
    ses = dp.get("session") or d.get("session") or {}
    own = (d.get("session") or {}).get("type") or briefing.DAY
    # сессия-день: тип всегда day (горизонт сделок выбирает агент в планах; dayplan.session.type лишь потолок горизонта)
    stype = briefing.DAY if own == briefing.DAY else (ses.get("type") or own)
    now = ctx.now()
    cur = session.active()
    if cur is not None:
        if cur.get("type") == stype and (d.get("session") or {}).get("id") in (None, cur["id"]):
            d["session"] = dict(d.get("session") or {}, id=cur["id"], type=stype)
            return "ok", {"reused": cur["id"]}, None
        return "fail", {"why": "уже идёт сессия %s (%s)" % (cur.get("id"), cur.get("type"))}, "E_LIMITS"
    prof = ctlstate.read_json(paths.config("risk_profile.json", root=ctx.root))
    try:
        prof = risklevels.effective(prof, ctx.root)
        thesis = "день %s (%s): %s" % (d["day_id"], d.get("mode"), (dp.get("why") or "план дня")[:160])
        rec = session.start(stype, thesis, "platform" if d.get("mode") in ("paper", "observe") else "bybit", None, None, prof, now=now)
    except SystemExit as e:
        return "fail", {"why": str(e)[:120]}, "E_LIMITS"
    d["session"] = dict(d.get("session") or {}, id=rec["id"], type=stype)
    return "ok", {"session": rec["id"], "budget_usdt": rec.get("budget_usdt")}, None


def s8_enable(ctx, d):
    days.set_trading(ctx.root, d.get("mode") != "observe", d, until_ts=None)       # срока у торгового дня нет: до day.stop
    return "ok", {"trading": d.get("mode") != "observe"}, None


def s9_report(ctx, d):
    return report.start_report(ctx, d)


FUNCS = {"S1": s1_preflight, "S2": s2_facts, "S3": s3_sync, "S4": s4_brief, "S5": s5_briefing, "S6": s6_dayplan, "S7": s7_session, "S8": s8_enable, "S9": s9_report}


def abort(ctx, d, step, code, detail):
    """Откат: сессия (если начата этим стартом) -> aborted, торговый флаг выключен, state=stopped, причина в day.json, баннер."""
    now = ctx.now()
    s7 = days.get_step(d, "S7", "start")
    if s7 and s7.get("ok") is True and (d.get("session") or {}).get("id") and d.get("mode") != "observe":
        try:
            cur = session.active()
            if cur and cur["id"] == d["session"]["id"]:
                session._append({"ev": "stop", "id": cur["id"], "ts": session._iso(now), "note": "aborted: %s" % step})
        except Exception:
            pass
    days.set_trading(ctx.root, False, d)
    d["abort"] = {"step": step, "code": code, "detail": detail if isinstance(detail, (dict, str)) else str(detail)[:200]}
    d["stop_source"] = "abort"
    days.set_state(ctx.root, d, "stopped", now, code=("%s" % (code or "abort")).replace(":", "_")[:40], step=step)
    why = ""
    if isinstance(detail, dict):
        why = "; ".join("%s %s" % (x.get("id", ""), x.get("msg") or x) for x in (detail.get("blocking") or [])[:2]) or str(detail.get("why") or "")
        if code == "E_AMBER":
            why = "предупреждения (жёлтые) не приняты: " + why
    days.set_banner(ctx.root, "alarm", ("Старт дня %s отклонён на шаге %s: %s" % (d["day_id"], step, why))[:200], d["day_id"])
    return {"ok": False, "code": code or "E_INTERNAL", "msg": "", "data": {"day_id": d["day_id"], "step": step, "detail": d["abort"]["detail"]}}


def run_start(ctx, d):
    for name in STEPS:
        st = days.get_step(d, name, "start")
        if st is not None and st.get("ok") is True:
            continue
        days.step_begin(ctx.root, d, name, "start", ctx.now())
        days.touch(ctx.root, d, ctx.now())
        try:
            status, detail, code = FUNCS[name](ctx, d)
        except Exception as e:
            status, detail, code = "fail", {"why": "исключение %s" % e.__class__.__name__}, "E_INTERNAL"
        days.step_end(ctx.root, d, name, "start", status == "ok", ctx.now(), detail, code)
        if status != "ok" and name == "S9":
            continue                                    # отчёт «старт» не критичен (amber)
        if status != "ok":
            return abort(ctx, d, name, CODE_OF_STEP.get(name, code) if code in (None, "E_GATE") else code, detail)
    d["phase"] = "run"
    days.set_state(ctx.root, d, "running", ctx.now(), step="S9")
    days.touch(ctx.root, d, ctx.now())
    return {"ok": True, "code": "OK", "msg": "", "data": {"day_id": d["day_id"]}}
