"""Итог дня: `report.json` (tb.dayreport/1) и `report.md` (детерминированный шаблон), отчёты «старт» и «аварийный»; событие шины, баннер, уведомление. WP14.

В отчёт попадают только данные платформы: числа, идентификаторы, теги разбора, очищенный текст агента (`narrative`, `why`). Данных третьих лиц нет.
"""
import os

from tb import paths
from tb.control import ctlstate
from tb.core import bus, journal, session
from tb.core.filelock import atomic_write_json
from tb.lifecycle import briefing, days, notify, notifycfg
from tb.lifecycle import carry as CARRY

SCHEMA = "tb.dayreport/1"


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _r(v, n=4):
    return round(float(v), n) if _num(v) else 0.0


def _iso(ts):
    return days.iso(ts)


def _rd(root, d, name):
    return ctlstate.read_json(os.path.join(days.day_dir(root, d["day_id"]), name))


# ------------------------------------------------------------------ сбор данных
def day_window(d, now):
    return float(d.get("started") or now) - 1.0, float(d.get("stopped") or now)


def trades_block(ctx, d, now, sess_ids):
    t0, t1 = day_window(d, now)
    try:
        tr, order = journal._load()
    except Exception:
        tr, order = {}, []
    rows, pnl, fees, fund, rsum = [], 0.0, 0.0, 0.0, 0.0
    tags = {}
    open_n = 0
    for i in order:
        t = tr[i]
        o = bus.parse_iso(t.get("ts_open"))
        c = bus.parse_iso(t.get("ts_close"))
        mine = t.get("session") in sess_ids or (o is not None and t0 <= o <= t1 + 5)
        closed_in = c is not None and t0 <= c <= t1 + 5
        if not (mine or closed_in):
            continue
        if not t.get("ts_close"):
            open_n += 1
            continue
        if not closed_in and not mine:
            continue
        p = t.get("pnl_usdt") or 0.0
        pnl += p
        fees += t.get("fees_usdt") or 0.0
        fund += t.get("funding_usdt") or 0.0
        rsum += t.get("r") or 0.0
        for e in t.get("err") or []:
            tags[e] = tags.get(e, 0) + 1
        setup = t.get("setup")
        if not setup:
            setup = next((x.split(":", 1)[1] for x in (t.get("tags") or []) if str(x).startswith("setup:")), None)
        rows.append({"journal_id": i, "sym": t.get("sym"), "side": t.get("side"), "pnl": _r(p), "r": t.get("r"), "setup": setup, "rule_followed": t.get("rule_followed"),
                     "err": list(t.get("err") or [])})
    cap = int(ctx.cfg["report"]["max_trades"])
    return {"rows": rows[-cap:], "n": len(rows), "open_n": open_n, "pnl": {"realized_gross": _r(pnl + fees + fund), "fees": _r(fees), "funding": _r(fund), "net": _r(pnl), "r_sum": round(rsum, 2)},
            "err_tags": tags}


def sessions_block(d, now):
    t0, t1 = day_window(d, now)
    out, ids = [], set()
    try:
        sess, order = session.load_all()
        tr, torder = journal._load()
    except Exception:
        return [], ids
    for i in order:
        s = sess[i]
        if not (t0 - 5 <= (s.get("start") or 0) <= t1 + 5 or i == (d.get("session") or {}).get("id")):
            continue
        ids.add(i)
        rows = [tr[x] for x in torder if tr[x].get("session") == i]
        closed = [x for x in rows if x.get("ts_close")]
        realized = sum(x.get("pnl_usdt") or 0.0 for x in closed)
        budget = s.get("budget_usdt") or 0.0
        out.append({"id": i, "type": s.get("type"), "pnl": _r(realized), "trades": len(rows), "budget_usdt": _r(budget),
                    "budget_used": round(max(0.0, -realized) / budget, 3) if budget else 0.0, "stopped": bool(s.get("stopped"))})
    return out, ids


def errors_block(ctx, d, now):
    t0, _t1 = day_window(d, now)
    e = {"exec": 0, "rejected_actions": 0, "brain_format": 0, "fallbacks": 0, "conn": 0}
    try:
        for ev in bus.read_tail(3000, root=ctx.root):
            t = bus.parse_iso(ev.get("ts"))
            if t is None or t < t0:
                continue
            if ev.get("type") == "error":
                e["exec"] += 1
            elif ev.get("type") == "action_rejected":
                e["rejected_actions"] += 1
            elif ev.get("type") == "connectivity":
                e["conn"] += 1
    except Exception:
        pass
    return e


def cost_block(ctx, d, now, errors):
    c = {"usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0, "by_profile": {}, "by_role": {}}
    try:
        from tb.llm import usage
        rows = usage.read_rows(ctx.root, since=float(d.get("started") or now))
        for r in rows:
            if r.get("kind") == "fmt":
                errors["brain_format"] += 1
                continue
            c["usd"] += r.get("usd") or 0.0
            c["tokens_in"] += r.get("in") or 0
            c["tokens_out"] += r.get("out") or 0
            c["calls"] += 1
            if r.get("fallback_from"):
                errors["fallbacks"] += 1
        for by, key in (("profile", "by_profile"), ("role", "by_role")):
            for k, g in usage.aggregate([r for r in rows if r.get("kind") != "fmt"], by).items():
                c[key][k] = {"calls": g["calls"], "usd": g["usd"], "in": g["in"], "out": g["out"]}
    except Exception:
        pass
    c["usd"] = round(c["usd"], 6)
    return c


def build(ctx, d, kind="stop"):
    now = ctx.now()
    sessions, ids = sessions_block(d, now)
    tb_ = trades_block(ctx, d, now, ids)
    errors = errors_block(ctx, d, now)
    cost = cost_block(ctx, d, now, errors)
    carry = _rd(ctx.root, d, "carry.json") or {}
    audit = _rd(ctx.root, d, "audit.json") or {}
    chk = (_rd(ctx.root, d, "checklist.json") or {}).get("rows") or []
    cl = d.get("closing") or {}
    final = dict((x["item"], x) for x in cl.get("final") or [])
    carried = carry.get("items") or []
    closed_eod = [{"item": x["item"], "codes": x.get("codes") or []} for x in final.values() if x["item"].startswith("pos:") and x["decision"] == "close"]
    cancelled = [{"item": "plan:%s" % pid, "rule": "A1"} for pid in sorted(cl.get("cancel_cmds") or {})]
    fx = briefing.read_facts(ctx.root) or {}
    dp = briefing.read_dayplan(ctx.root, d["day_id"]) or {}
    qs = []
    for i, q in enumerate((dp.get("questions") or [])[:3]):
        qs.append({"id": "A%d" % (i + 1), "text": CARRY.clean_text(q, 160), "options": []})
    for i, it in enumerate(cl.get("manual") or []):
        qs.append({"id": "M%d" % (i + 1), "text": "Позиция %s без плана не закрыта автоматически: разобрать вручную" % it.split(":", 1)[-1][:30], "options": ["закрыть вручную"]})
    for q in (d.get("owner") or {}).get("questions") or []:
        if q.get("answer") is None:
            qs.append({"id": q["id"], "text": q.get("text") or "", "options": q.get("options") or []})
    failed = cl.get("failed")
    amber = [s for s in d.get("steps") or [] if isinstance(s.get("detail"), dict) and s["detail"].get("amber")]
    llm_state = audit.get("llm")
    if kind == "emergency" or failed or cl.get("manual"):
        verdict = "alarm"
    elif amber or qs or errors["exec"] or errors["brain_format"] or audit.get("rejected") or llm_state in ("unavailable", "timeout", "error") or tb_["err_tags"]:
        verdict = "attention"
    else:
        verdict = "ok"
    rep = {"schema": SCHEMA, "day_id": d["day_id"], "kind": kind, "mode": d.get("mode"), "agent": d.get("agent"), "team": d.get("team"),
           "profiles": _profiles(ctx), "started": _iso(d.get("started")), "stopped": _iso(d.get("stopped") or now), "stop_source": d.get("stop_source"), "session": sessions,
           "pnl": tb_["pnl"], "trades": tb_["rows"], "trades_n": tb_["n"], "open_trades_n": tb_["open_n"], "err_tags": tb_["err_tags"], "cancelled": cancelled, "carried": carried,
           "closed_eod": closed_eod, "errors": errors, "cost": cost, "intel": {"degraded": (d.get("facts") or {}).get("degraded") or [], "events_used": len(fx.get("events") or []),
                                                                         "status": (d.get("facts") or {}).get("status")},
           "open_questions": qs, "checklist": {"rows": chk[:40]}, "agent_audit": {"llm": llm_state, "decisions": audit.get("agent") or [], "rejected": audit.get("rejected") or []},
           "narrative": CARRY.clean_text(audit.get("narrative"), 600), "waivers": [{"id": w.get("id"), "code": w.get("code")} for w in d.get("live_waivers") or [] if isinstance(w, dict)],
           "dayplan": d.get("dayplan"), "close_failed": failed, "llm_unavailable": llm_state in ("unavailable", "timeout", "error", "submit_failed"), "verdict": verdict}
    return rep


def _profiles(ctx):
    try:
        from tb.control import brain_iface
        return brain_iface.status(ctx.root).get("profiles") or {}
    except Exception:
        return {}


# ------------------------------------------------------------------ markdown
def render_md(rep):
    L = []
    a = L.append
    a("# Итог дня %s" % rep["day_id"])
    a("")
    a("Режим: %s. Агент: %s. Оценка: **%s**. Старт %s, остановка %s UTC (%s)." % (rep["mode"], rep.get("agent") or "-", rep["verdict"], rep["started"], rep["stopped"],
                                                                         rep.get("stop_source") or "-"))
    a("")
    p = rep["pnl"]
    a("## Результат")
    a("")
    a("- PnL чистый: %+.4f USDT (грязный %+.4f, комиссии %.4f, funding %.4f), R сумма: %+.2f" % (p["net"], p["realized_gross"], p["fees"], p["funding"], p["r_sum"]))
    a("- Сделок закрыто: %d, открытых осталось: %d" % (rep["trades_n"], rep["open_trades_n"]))
    for s in rep["session"]:
        a("- Сессия %s (%s): PnL %+.4f, сделок %d, бюджет убытка использован %.0f%%" % (s["id"], s["type"], s["pnl"], s["trades"], s["budget_used"] * 100))
    if rep["trades"]:
        a("")
        a("## Сделки")
        a("")
        for t in rep["trades"]:
            a("- %s %s %s: %+.4f USDT, R %s, сетап %s, по правилам: %s%s" % (t["journal_id"], t["sym"], t["side"], t["pnl"], t["r"] if t["r"] is not None else "-", t["setup"] or "-",
                                                                           t["rule_followed"] or "-", (", теги: " + ",".join(t["err"])) if t["err"] else ""))
    if rep["err_tags"]:
        a("")
        a("Теги разбора: " + ", ".join("%s x%d" % (k, v) for k, v in sorted(rep["err_tags"].items())))
    a("")
    a("## Перенос через сутки")
    a("")
    if rep["carried"]:
        for c in rep["carried"]:
            a("- %s %s %s: размер %s, вход %s, стоп %s, риск до стопа %.4f USDT; решил: %s; основания: %s; против: %s; причина: %s" % (
                c["plan"], c["sym"], c["side"], c["size"], c["avg"], c["stop"], c["risk_usdt"], c["decided_by"], ",".join(c.get("basis") or []) or "-",
                ",".join(c.get("against") or []) or "-", c.get("why") or "-"))
            ks = c.get("K") or {}
            a("  K-проверки: " + ", ".join("%s %s" % (k, "ok" if v else "нет") for k, v in sorted(ks.items())))
    else:
        a("Ничего не перенесено.")
    a("")
    a("## Закрыто и отменено")
    a("")
    for x in rep["closed_eod"]:
        a("- закрыто: %s (%s)" % (x["item"], ", ".join(x["codes"]) or "правило"))
    for x in rep["cancelled"]:
        a("- отменён вход: %s (%s)" % (x["item"], x["rule"]))
    if not rep["closed_eod"] and not rep["cancelled"]:
        a("Закрывать и отменять было нечего.")
    if rep.get("close_failed"):
        a("")
        a("**ВНИМАНИЕ: закрытие не подтверждено исполнителем: %s. Позиции остаются под стопом на бирже; день не закрыт до подтверждения.**" % ", ".join(rep["close_failed"].get("items") or []))
    a("")
    a("## Ошибки и издержки")
    a("")
    e = rep["errors"]
    a("- Ошибки исполнителя: %d, отклонённые действия: %d, ошибки формата мозга: %d, смены на запасной: %d, обрывы связи: %d" % (e["exec"], e["rejected_actions"], e["brain_format"],
                                                                                                                 e["fallbacks"], e["conn"]))
    c = rep["cost"]
    a("- LLM: %d вызовов, %d токенов вход / %d выход, %.4f USD" % (c["calls"], c["tokens_in"], c["tokens_out"], c["usd"]))
    if rep["llm_unavailable"]:
        a("- Мозг был недоступен при закрытии: решения приняты чек-листом (llm_unavailable)")
    if rep["intel"]["degraded"]:
        a("- Фактура деградирована: " + ", ".join(rep["intel"]["degraded"]))
    if rep["waivers"]:
        a("- Принятые предупреждения live: " + ", ".join(w["id"] for w in rep["waivers"]))
    if rep["narrative"]:
        a("")
        a("## Комментарий аналитика")
        a("")
        a(rep["narrative"])
    a("")
    a("## Вопросы владельцу")
    a("")
    if rep["open_questions"]:
        for q in rep["open_questions"]:
            a("- %s: %s%s" % (q["id"], q["text"], (" [" + "/".join(q["options"]) + "]") if q["options"] else ""))
    else:
        a("Открытых вопросов нет.")
    a("")
    return "\n".join(L)


# ------------------------------------------------------------------ публикация
def _write(ctx, d, rep, name="report"):
    base = days.day_dir(ctx.root, d["day_id"])
    os.makedirs(base, exist_ok=True)
    atomic_write_json(os.path.join(base, name + ".json"), rep, indent=1)
    from tb.core.filelock import atomic_write
    atomic_write(os.path.join(base, name + ".md"), render_md(rep))


def publish(ctx, d, rep, kind):
    now = ctx.now()
    days.emit("day_report", {"kind": kind, "day": d["day_id"], "net": rep["pnl"]["net"], "carried_n": len(rep["carried"]), "questions_n": len(rep["open_questions"]),
                             "verdict": rep["verdict"]}, ctx.root, now, pri=bus.PRI_WAKE)
    cfg = ctx.cfg
    if cfg.get("notify", {}).get("os") and not notifycfg.engine_on(ctx.root):     # есть state/control/notify.json: рассылает подписчик шины (notifyd)
        try:
            notify.send_os(notify.template(kind, rep), cfg)
        except Exception:
            pass


def finish(ctx, d, kind="stop"):
    """Итог дня (C8): report.json/md, баннер, событие, уведомление; переход closing -> stopped (если закрытие подтверждено)."""
    now = ctx.now()
    rep = build(ctx, d, kind)
    _write(ctx, d, rep)
    d["report"] = {"ready": True, "ts": round(now, 1), "verdict": rep["verdict"], "acked": False, "net": rep["pnl"]["net"], "carried_n": len(rep["carried"]),
                   "questions_n": len(rep["open_questions"])}
    level = {"ok": "info", "attention": "warn", "alarm": "alarm"}[rep["verdict"]]
    if rep.get("close_failed"):
        days.set_banner(ctx.root, "alarm", "Закрытие дня %s не подтверждено: позиции под стопом, нужен разбор (report.ack с пометкой)" % d["day_id"], d["day_id"], report=True)
    else:
        days.set_banner(ctx.root, level, "Итог дня %s готов: %+.2f USDT, перенесено %d, вопросов %d" % (d["day_id"], rep["pnl"]["net"], len(rep["carried"]), len(rep["open_questions"])),
                        d["day_id"], report=True)
    publish(ctx, d, rep, kind)
    if not rep.get("close_failed"):
        days.set_state(ctx.root, d, "stopped", now, step="C8")
        days.set_trading(ctx.root, False, d)
    else:
        days.save(ctx.root, d)
    return rep


def start_report(ctx, d):
    """S9: отчёт «старт» (документ start.md + баннер + событие)."""
    dp = briefing.read_dayplan(ctx.root, d["day_id"]) or {}
    steps = [s for s in d.get("steps") or [] if s.get("phase") == "start"]
    amber = [s["name"] for s in steps if isinstance(s.get("detail"), dict) and s["detail"].get("amber")]
    L = ["# Старт дня %s" % d["day_id"], "", "Режим: %s. Сессия: %s. План дня: %s, режим рынка %s, смещение %s." % (
        d.get("mode"), (d.get("session") or {}).get("type"), (dp.get("meta") or {}).get("source", "-"), dp.get("regime", "-"), dp.get("bias", "-")), ""]
    lim = dp.get("limits") or {}
    L.append("Лимиты дня: новых планов %s, риск на план до %s USDT, множитель размера %s, символы: %s." % (lim.get("max_new_plans"), lim.get("max_risk_usdt"), lim.get("size_mult"),
                                                                                         ", ".join(lim.get("symbols") or []) or "-"))
    if d.get("carry_in"):
        L.append("Перенесено со вчера: " + ", ".join(x.get("plan", "?") for x in d["carry_in"]))
    if amber:
        L.append("Замечания старта (amber): " + ", ".join(amber))
    for q in dp.get("questions") or []:
        L.append("Вопрос аналитика: " + CARRY.clean_text(q, 160))
    base = days.day_dir(ctx.root, d["day_id"])
    from tb.core.filelock import atomic_write
    atomic_write(os.path.join(base, "start.md"), "\n".join(L) + "\n")
    verdict = "attention" if amber else "ok"
    days.emit("day_report", {"kind": "start", "day": d["day_id"], "net": 0.0, "carried_n": len(d.get("carry_in") or []), "questions_n": len(dp.get("questions") or []), "verdict": verdict},
              ctx.root, ctx.now(), pri=bus.PRI_WAKE)
    days.set_banner(ctx.root, "info" if not amber else "warn", "День %s запущен (%s)%s" % (d["day_id"], d.get("mode"), "; замечания: " + ",".join(amber) if amber else ""), d["day_id"])
    if ctx.cfg.get("notify", {}).get("os") and not notifycfg.engine_on(ctx.root):
        try:
            notify.send_os(notify.template("start", {"day_id": d["day_id"], "mode": d.get("mode")}), ctx.cfg)
        except Exception:
            pass
    return "ok", {"amber": bool(amber), "doc": "start.md"}, None


def emergency(ctx, d):
    """Аварийный отчёт без LLM и без шагов закрытия: что известно из файлов."""
    rep = build(ctx, d, "emergency")
    rep["verdict"] = "alarm"
    _write(ctx, d, rep)
    d["report"] = {"ready": True, "ts": round(ctx.now(), 1), "verdict": "alarm", "acked": False, "net": rep["pnl"]["net"], "carried_n": 0, "questions_n": len(rep["open_questions"])}
    days.set_banner(ctx.root, "alarm", "Аварийная остановка дня %s: итог %+.2f USDT (отчёт готов)" % (d["day_id"], rep["pnl"]["net"]), d["day_id"], report=True)
    publish(ctx, d, rep, "emergency")
    return rep
