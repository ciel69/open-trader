"""Служебные файлы исполнителя (02 п. 2.11): exec_health.json (heartbeat), exec_snapshot.json, levels.json, instruments.json. Владелец: WP5.

Пишутся атомарной заменой (core.filelock.atomic_write_json); ошибки записи не останавливают исполнитель. write_all(ex) вызывается из on_timer:
heartbeat раз в heartbeat_every_s, снимок раз в snapshot_every_s, levels и instruments раз в 60 с / час.
"""
import json
import os

from tb import paths
from tb.core.filelock import atomic_write_json
from tb.logic import levels as L


def _w(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_json(path, obj)
    except OSError:
        pass


def health(ex, final=False):
    now = ex.clock()
    plans = {"armed": 0, "managing": 0, "draft": 0, "entered": 0}
    for p in ex.plans.values():
        s = p["rt"].get("status")
        plans[s] = plans.get(s, 0) + 1
    holds = list(ex.holds)
    from tb.core import exec_cfg

    def ws(ch):
        w = ex.ws[ch]
        return {"up": bool(w.get("up")), "age_s": round(w.get("down_s") or 0.0, 1)}
    last_ok = now - ex.rest["last_ok"]
    return {"ts_epoch": round(now, 3), "pid": os.getpid(), "version": "v2.0-wp5", "env": ex.port.env if ex.port is not None else None,
            "profile": ex.profile, "mode": "observe" if ex.observe else ("paper" if ex.port is not None and ex.port.env == "paper" else "live"),
            "observe": ex.observe, "venue": ex.venue_id, "uptime_s": round(now - ex.started, 1), "alive": not final,
            "ws": {"public": ws("public"), "private": ws("private")},
            "rest": {"ok": last_ok < 30, "last_ok_s": round(last_ok, 1), "errors_1m": len([t for t in ex.rest["errors"] if now - t < 60])},
            "loop_lag_ms": 0, "plans": plans, "positions": len(ex.exch_pos), "kill": exec_cfg.kill_state(ex.root) is not None,
            "hold": holds[0] if holds else None, "hold_reasons": holds, "last_reconcile_ts": ex.last_reconcile_ts,
            "reconcile_age_s": (round(now - ex.last_reconcile_ts, 1) if ex.last_reconcile_ts else None), "diffs": len(ex.diffs),
            "last_event_seq": ex.last_event_seq, "profile_hash": ex.profile_hash, "bad_ticks": ex.bad_ticks}


def snapshot(ex):
    from tb.executor import reconcile
    now = ex.clock()
    acct = ex.acct or {}
    pos = []
    for sym, x in sorted(ex.exch_pos.items()):
        pos.append({"sym": sym, "side": x.get("side"), "size": x.get("size"), "avg": x.get("avg"), "mark": x.get("mark"), "sl": x.get("sl"),
                    "tp": x.get("tp"), "liq": x.get("liq"), "upnl": x.get("upnl"), "lev": x.get("lev"), "margin": x.get("margin"),
                    "owner": reconcile.owner_of(ex, sym)})
    ords = []
    link_plan = {}
    for p in ex.plans.values():
        ent = p["rt"].get("entry") or {}
        if ent.get("link_id"):
            link_plan[ent["link_id"]] = (p["spec"]["id"], "entry")
        for t in (p["rt"].get("targets") or {}).values():
            if t.get("link"):
                link_plan[t["link"]] = (p["spec"]["id"], "target")
    for o in ex.exch_orders:
        pl, role = link_plan.get(o.get("link_id"), (None, "other"))
        ords.append({"sym": o.get("sym"), "link_id": o.get("link_id"), "side": o.get("side"), "type": o.get("type"), "price": o.get("price"), "qty": o.get("qty"),
                     "filled": o.get("filled"), "reduce_only": o.get("reduce_only"), "role": role, "plan_id": pl})
    return {"ts_epoch": round(now, 3), "env": ex.port.env if ex.port is not None else None,
            "account": {"equity": acct.get("equity"), "free": acct.get("free")}, "positions": pos, "orders": ords}


def levels(ex):
    out = {}
    now_ms = int(ex.clock() * 1000)
    for sym, book in ex.books.items():
        try:
            out[sym] = L.key_levels(book, now_ms)
        except Exception:
            pass
    return out


def write_all(ex, force=False, final=False):
    now = ex.clock()
    cfg = ex.cfg["connectivity"]
    t = ex._t
    if force or now - t.get("hb", -1e9) >= float(cfg["heartbeat_every_s"]):
        t["hb"] = now
        _w(paths.exec_health_file(root=ex.root, venue=ex.venue_id), health(ex, final))
    if force or now - t.get("snap", -1e9) >= float(cfg["snapshot_every_s"]):
        t["snap"] = now
        _w(paths.exec_snapshot_file(root=ex.root, venue=ex.venue_id), snapshot(ex))
    if force or now - t.get("lvl", -1e9) >= 60.0:
        t["lvl"] = now
        if ex.books:
            _w(paths.levels_file(root=ex.root, venue=ex.venue_id), {"ts_epoch": round(now, 3), "symbols": levels(ex)})
    if force or now - t.get("ins", -1e9) >= 3600.0:
        t["ins"] = now
        syms = dict((s, v[1]) for s, v in ex.instruments.items())
        if syms:
            _w(paths.instruments_file(root=ex.root, venue=ex.venue_id), {"ts_epoch": round(now, 3), "symbols": syms})
