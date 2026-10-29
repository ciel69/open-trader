"""Реконсиляция с биржей (02 п. 2.7): старт, раз в reconcile_every_s, после переподключения WS. Владелец: WP5.

Источники истины: биржа для размера, средней, стопа, ордеров, исполнений; журнал и планы для намерений. run(ex) читает позиции, ордера,
счёт и неучтённые исполнения, сопоставляет владельцев (plan:ID / legacy / unknown) и исправляет расхождения по таблице. Неразрешённые расхождения
остаются в ex.diffs и блокируют новые входы (hold reconcile_diffs). Режим observe: только чтение, события и diffs, никаких записей на биржу.
unknown-позиции не трогаются никогда (даже стоп не ставится): только событие.
"""
from tb.core import journal, linkid
from tb.executor import gate as G
from tb.executor.ports import PortError

IN_POS = ("entered", "managing")


def _alert(ex, kind, sym, key=None, why=None):
    """Событие reconcile_alert не чаще раза в 5 мин на (kind, sym)."""
    k = ("recon", kind, sym, key)
    now = ex.clock()
    if now - ex.alerts.get(k, -1e9) < 300.0:
        return
    ex.alerts[k] = now
    ex.emit("reconcile_alert", {"kind": kind, "sym": sym}, sym=sym)
    try:
        ex.decide("reconcile", kind, why="RECON:%s %s" % (kind, why or sym), res={"ok": False})
    except Exception:
        pass


def _owner(ex, sym):
    for p in ex.active_plans(sym):
        if p["rt"]["status"] in IN_POS:
            return "plan:%s" % p["spec"]["id"]
    try:
        tr, order = journal._load()
        for i in order:
            t = tr[i]
            if t.get("sym") == sym and not t.get("ts_close") and not t.get("plan_id") and t.get("venue") == ex.venue_id:
                return "legacy"
    except Exception:
        pass
    return "unknown"


def owner_of(ex, sym):
    return _owner(ex, sym)


def run(ex, startup=False):
    """-> True, если проход выполнен (REST доступен). Побочно: ex.exch_pos/exch_orders, ex.diffs, holds, last_reconcile_*."""
    now = ex.clock()
    try:
        positions = ex.port.positions()
        orders = ex.port.open_orders()
        acct = ex.port.account()
        ex.rest_event(None)
    except PortError as e:
        ex.rest_event(e)
        return False
    ex.exch_pos = dict((x["sym"], dict(x)) for x in positions if x["size"] > 0)
    ex.exch_orders = orders
    ex.acct = {"equity": acct.get("equity"), "free": acct.get("free"), "ts": now}
    # исполнения, пропущенные за время обрыва
    try:
        since = max(0, ex.last_exec_ts_ms - 5000) if ex.last_exec_ts_ms else int((now - 600) * 1000)
        for e in ex.port.executions(since):
            ex.apply_execution(e)
    except PortError as e:
        ex.rest_event(e)
    diffs = []
    by_link = dict((o["link_id"], o) for o in orders if o.get("link_id"))
    for p in list(ex.active_plans()):
        _plan(ex, p, by_link, diffs, now)
    _orphans(ex, orders, diffs, now)
    ex.diffs = diffs
    if diffs:
        ex.holds.setdefault("reconcile_diffs", now)
    else:
        ex.holds.pop("reconcile_diffs", None)
    ex.last_reconcile_ok = now
    ex.last_reconcile_ts = now
    ex.holds.pop("reconciling", None)
    if ex.ws["private"].get("up") is not False:
        ex.holds.pop("private_ws", None)
    return True


def _plan(ex, p, by_link, diffs, now):
    spec, rt = p["spec"], p["rt"]
    sym, side = spec["symbol"], spec["side"]
    pos = ex.exch_pos.get(sym)
    st = rt["status"]
    if st == "armed":
        ent = rt["entry"]
        if ent.get("state") in ("placing", "resting"):
            if ent.get("link_id") not in by_link:
                ex._poll_entry(p)       # ордера нет: заполнен / отменён / не отправлен -> poll_entry разберётся по link_id
        elif pos and ent.get("state") in ("waiting", "blocked") and not ex.observe:
            diffs.append({"kind": "orphan_position", "sym": sym})
            _alert(ex, "orphan_position", sym, why="позиция при armed плане")
        return
    if st not in IN_POS:
        return
    if not pos:
        ex.sync_position(p)      # позиции нет: финализация (применит исполнения, запишет close)
        return
    real, mine = float(pos["size"]), float(rt["pos"].get("qty") or 0)
    ins = ex.instrument(sym) or {"qty_step": "0.001"}
    step = float(ins["qty_step"])
    if abs(real - mine) > step / 2.0:
        if real < mine:        # кто-то закрыл часть вручную: принять факт, пересчитать лесенку
            ex.decide("reconcile", "position_drift", plan=spec["id"], why="RECON:position_drift accept", args={"exch": real, "plan": mine})
            _alert(ex, "position_drift", sym, key="less")
            rt["pos"]["qty"] = real
            ex.save(p, sync=True)
            if not ex.observe:
                ex.act.resync_ladder(p, why="RECON:drift")
        else:                  # кто-то добрал: hold, ничего не добирать и не закрывать самим
            rt["hold"], rt["hold_reason"] = "position_drift", "position_drift"
            ex.save(p, sync=True)
            diffs.append({"kind": "position_drift", "sym": sym})
            _alert(ex, "position_drift", sym, key="more")
            return
    elif rt.get("hold_reason") == "position_drift":
        rt["hold"], rt["hold_reason"] = None, None
        ex.save(p)
    sl = float(pos.get("sl") or 0)
    mine_sl = float(rt["pos"].get("cur_stop") or rt["pos"].get("initial_stop") or 0)
    if sl <= 0:
        diffs.append({"kind": "missing_stop", "sym": sym})
        _alert(ex, "missing_stop", sym, why="нет стопа на бирже")
        if not ex.observe and mine_sl > 0:
            kind, _ = ex.act.move_stop(p, mine_sl, by="protect", why="RECON:missing_stop", vd=G.allow(cur=0.0, to=mine_sl))
            if kind in ("ok", "unchanged"):
                diffs.pop()
            else:
                ex.act.close_by_protect(p, "RECON:missing_stop")
                ex.sync_position(p)
        return
    if G.tighter(side, sl, mine_sl):
        rt["pos"]["cur_stop"] = sl      # кто-то подтянул вручную: принять
        ex.save(p)
    elif G.tighter(side, mine_sl, sl) and not ex.observe:     # шире ожидаемого: вернуть (восстановление защиты)
        ex.decide("reconcile", "restore_stop", plan=spec["id"], why="RECON:stop_wider", args={"exch": sl, "plan": mine_sl})
        ex.act.move_stop(p, mine_sl, by="protect", why="RECON:stop_wider", vd=G.allow(cur=sl, to=mine_sl))
    _ladder(ex, p, by_link, now)


def _ladder(ex, p, by_link, now):
    """Лесенка: недостающие reduce-only ордера поставить, исчезнувшие по неизвестной причине выяснить."""
    if ex.observe:
        return
    rt = p["rt"]
    changed = False
    for tid, t in sorted((rt.get("targets") or {}).items()):
        if t.get("state") != "resting" or t.get("link") in by_link:
            continue
        try:
            o = ex.port.order_by_link(p["spec"]["symbol"], t["link"])
        except PortError:
            continue
        if o and o.get("status") == "Filled":
            continue          # исполнение придёт через executions
        left = float(t["qty"]) - float(t.get("filled_qty") or 0)
        remaining = float(rt["pos"].get("qty") or 0)
        t["state"] = "pending"
        t["qty"] = round(min(left, remaining) + float(t.get("filled_qty") or 0), 12)
        t["link"] = None
        changed = True
    if changed:
        ex.save(p, sync=True)
        ex.act.place_ladder(p, why="RECON:ladder")


def _orphans(ex, orders, diffs, now):
    mine = set()
    for p in ex.active_plans():
        ent = p["rt"].get("entry") or {}
        if ent.get("link_id"):
            mine.add(ent["link_id"])
        for t in (p["rt"].get("targets") or {}).values():
            if t.get("link"):
                mine.add(t["link"])
    for sym, pos in ex.exch_pos.items():
        if any(p["rt"]["status"] in IN_POS for p in ex.active_plans(sym)):
            continue
        own = _owner(ex, sym)
        if own == "unknown":
            diffs.append({"kind": "orphan_position", "sym": sym})
            _alert(ex, "orphan_position", sym, why="позиция без журнала")
        elif float(pos.get("sl") or 0) <= 0 and not ex.observe:      # legacy без стопа: поставить стоп из журнала
            try:
                tr, order = journal._load()
                jid = next(i for i in reversed(order) if tr[i].get("sym") == sym and not tr[i].get("ts_close") and not tr[i].get("plan_id"))
                sl = float(tr[jid].get("sl") or 0)
            except Exception:
                sl = 0.0
            _alert(ex, "missing_stop", sym, why="legacy без стопа")
            if sl > 0:
                try:
                    ex.act.call("set_stop", G.RISK_DOWN, sym, pos["side"], sl)
                except Exception:
                    diffs.append({"kind": "missing_stop", "sym": sym})
    for o in orders:
        lk = o.get("link_id") or ""
        if o.get("status") not in ("New", "PartiallyFilled", None) or lk in mine:
            continue
        if linkid.is_ours(lk):
            pn = linkid.parse(lk)
            plan = ex.plan_by_n(pn[0]) if pn else None
            if (plan is None or plan["rt"]["status"] not in ("armed",) + IN_POS) and not ex.observe:
                try:
                    ex.act.call("cancel_order", G.RISK_DOWN, o["sym"], lk)
                    ex.decide("reconcile", "cancel_orphan_order", why="RECON:orphan_tbx_order")
                except Exception:
                    pass
            continue
        diffs.append({"kind": "unknown_order", "sym": o["sym"]})
        _alert(ex, "unknown_order", o["sym"], key=lk)
