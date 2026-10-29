#!/usr/bin/env python3
"""tick.py — один тик трейдера (Agent B). stdin JSON -> stdout JSON. Сеть не используется.

  ./tb tick [--root DIR] [--config FILE] [--now ISO]        обычный тик
  ./tb tick --start [--force]   ./tb tick --stop   ./tb tick --report [--day YYYY-MM-DD]
При любой ошибке: {"halt": true, "halt_reason": "error: ...", "decisions": []}, код 0.
"""
import argparse
import importlib
import json
import math
import os
import sys
import time
import traceback

from tb import paths
from tb.legacy_engine import risk
from tb.legacy_engine import sizing
from tb.legacy_engine import state as S


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("bad arguments: " + message)


def _parser():
    p = _Parser(add_help=False)
    p.add_argument("--root", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--now", default=None, help="переопределить реальные часы (реплей/тесты)")
    p.add_argument("--day", default=None)
    p.add_argument("--start", action="store_true")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--report", action="store_true")
    p.add_argument("--force", action="store_true")
    return p


def clean(o):
    """Рекурсивно делает объект JSON-безопасным (NaN/inf -> None)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def error_out(e):
    return {"halt": True, "halt_reason": "error: %s: %s" % (type(e).__name__, e), "decisions": []}


def _load_config(args, root):
    path = args.config
    if not path:
        path = paths.config("config.json", root=root)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config is not an object")
    for k in ("risk", "strategy", "symbols", "bar_seconds"):
        if k not in cfg:
            raise ValueError("config missing key: " + k)
    return cfg


def _read_input(text, required):
    if text is None or not text.strip():
        if required:
            raise ValueError("empty input")
        return None
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("input is not a JSON object")
    return obj


def _build_snapshot(raw, cfg, st, wall_now):
    ts = S.parse_ts(raw.get("ts"))
    if ts is None:
        raise ValueError("missing/invalid ts")
    if not isinstance(raw.get("positions"), list):
        raise ValueError("positions missing or not a list")
    prices = {}
    pr = raw.get("prices")
    if isinstance(pr, dict):
        for sym, v in pr.items():
            f = risk.fnum(v)
            if f is not None and f > 0:
                prices[sym] = f
    funding = {}
    fr = raw.get("funding_hourly")
    if isinstance(fr, dict):
        for sym, v in fr.items():
            f = risk.fnum(v)
            if f is not None:
                funding[sym] = f
    contracts = raw.get("contracts") if isinstance(raw.get("contracts"), dict) else {}
    return {
        "now": ts, "wall_now": wall_now,
        "prices": prices, "price_ts": {s: ts for s in prices},
        "funding_hourly": funding,
        "balance": risk.fnum(raw.get("balance_usdt")),
        "positions": [p for p in raw["positions"] if isinstance(p, dict)],
        "contracts": contracts, "planned": [],
    }


def _reconcile(st, snap, cfg, jpath):
    """Сверка позиций платформы с трекингом: усыновление, закрытия, realized, cooldown."""
    now = snap["now"]
    tracked = st["positions"]
    current = {p["cid"]: p for p in risk.open_positions(snap)}
    for cid, tr in list(tracked.items()):
        if cid in current:
            continue
        sym = tr.get("symbol") or cid
        pnl = risk.fnum(tr.get("last_pnl"))
        if pnl is not None:
            st["realized_pnl_today"] = (st.get("realized_pnl_today") or 0.0) + pnl
        code = tr.get("closing")
        is_stop = code == "stop" or (code is None and pnl is not None and pnl < 0)
        if is_stop:
            st["last_stop_ts"][sym] = now
        st["last_close_ts"][sym] = now
        S.journal(jpath, {"ts": S.iso_utc(now), "type": "position_closed", "symbol": sym,
                          "est_pnl": pnl, "closing": code, "stop_like": is_stop})
        del tracked[cid]
    for cid, pos in current.items():
        sym = risk.symbol_for(snap, pos["contract_id"])
        price = snap["prices"].get(sym) if sym else None
        pnl = risk.position_pnl(pos, price)
        tr = tracked.get(cid)
        if tr is None:
            po = st["pending_open"].pop(sym, None) if sym else None
            tr = {"symbol": sym or cid, "side": pos["side"], "first_seen": S.iso_utc(now),
                  "entry": pos["entry"], "stop": None, "tp": None, "closing": None}
            if isinstance(po, dict):
                tr["stop"], tr["tp"] = po.get("stop"), po.get("tp")
            tracked[cid] = tr
            S.journal(jpath, {"ts": S.iso_utc(now), "type": "position_opened" if po else "position_adopted",
                              "symbol": sym or cid, "side": pos["side"], "entry": pos["entry"]})
        elif sym:
            st["pending_open"].pop(sym, None)
        if pnl is not None:
            tr["last_pnl"] = pnl
    # просроченные pending_open
    lim = 2.0 * cfg["risk"]["stale_seconds"]
    for sym, po in list(st["pending_open"].items()):
        pts = S.parse_ts(po.get("ts")) if isinstance(po, dict) else None
        if pts is None or now - pts > lim:
            del st["pending_open"][sym]


def _bars_for(dmod, path, cfg, now):
    bs = cfg["bar_seconds"]
    ticks = dmod.load_ticks(path, max_rows=3000)
    bars = list(dmod.to_bars(ticks, bs))
    if bars:
        try:
            if float(bars[-1]["ts"]) == (now // bs) * bs:
                bars = bars[:-1]  # бар ещё формируется
        except (KeyError, TypeError, ValueError):
            pass
    return bars


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _market_ctx(root, sym, now, cfg):
    """Режим рынка и risk_off из runtime/*.json фида. Нет файла или устарел -> пустой контекст (режим unknown)."""
    ctx = {}
    ttl = 5.0 * float(cfg["risk"].get("stale_seconds", 150))
    m = _read_json(paths.runtime("market.json", root=root))
    s = (m.get("symbols") or {}).get(sym)
    if m.get("feed_ok") is True and isinstance(s, dict) and now - float(m.get("ts_epoch") or 0) <= ttl:
        ctx["regime"] = s.get("regime")
    c = _read_json(paths.runtime("context.json", root=root))
    if c.get("risk_off") is True:
        ctx["risk_off"] = True
    return ctx


def _do_tick(args, root, cfg, raw, wall_now):
    spath = paths.state("state.json", root=root)
    jpath = paths.state("journal.jsonl", root=root)
    ddir = paths.data(root=root)
    st = S.load_state(spath)
    snap = _build_snapshot(raw, cfg, st, wall_now)
    now = snap["now"]
    warnings = []

    eq0, _ = risk.compute_equity(snap)
    if st.get("day") != S.day_str(now):
        if st.get("halted") and str(st.get("halt_reason", "")).startswith("state_"):
            pass  # битое состояние: ручной --start
        else:
            S.new_day_reset(st, now, eq0)
            S.journal(jpath, {"ts": S.iso_utc(now), "type": "new_day", "day": st["day"],
                              "day_start_equity": st["day_start_equity"]})
    if st.get("day_start_equity") is None and eq0 is not None and st.get("day") == S.day_str(now):
        st["day_start_equity"] = eq0
        warnings.append("day_start_equity was unknown; set from current equity")

    _reconcile(st, snap, cfg, jpath)

    # тики и сигналы
    dmod = importlib.import_module("tb.marketdata.data")
    smod = importlib.import_module("tb.legacy_engine.signals")
    last = st.get("last_tick_ts")
    fresh = last is None or now > last
    if not fresh:
        warnings.append("duplicate_or_old_tick: no new opens")
    os.makedirs(ddir, exist_ok=True)
    signals = {}
    for sym in cfg["symbols"]:
        tpath = os.path.join(ddir, "ticks_%s.csv" % sym)
        try:
            if fresh and sym in snap["prices"]:
                dmod.append_tick(tpath, S.iso_utc(now), snap["prices"][sym])
            bars = _bars_for(dmod, tpath, cfg, now)
            f = snap["funding_hourly"].get(sym, cfg.get("costs", {}).get("funding_hourly_assumed", 0.0001))
            sig = smod.compute_signal(bars, f, cfg)
            if not isinstance(sig, dict) or sig.get("action") not in ("long", "short", "flat"):
                raise ValueError("bad signal")
            signals[sym] = sig
        except Exception as e:  # сигнал по символу сломан -> flat
            signals[sym] = {"action": "flat", "score": 0.0, "warmup": True, "reasons": ["signal_error: %s" % e]}

    eq, einfo = risk.compute_equity(snap)
    dse = st.get("day_start_equity")
    ks = risk.kill_switch(st, snap, cfg)
    decisions = []
    rejections = {}
    if ks["halt"]:
        st["halted"] = True
        st["halt_reason"] = ks["reason"]
        decisions = risk.flatten_decisions(snap, [ks["reason"]], "kill_switch")
        S.journal(jpath, {"ts": S.iso_utc(now), "type": "halt", "reason": ks["reason"]})
    else:
        decisions = risk.manage_positions(st, snap, cfg)
        managed = {str(d.get("contract_id")) for d in decisions if d["op"] == "close"}
        cands = sorted(
            [(s, g) for s, g in signals.items() if g.get("action") in ("long", "short")],
            key=lambda x: -(risk.fnum(x[1].get("score"), 0.0)))
        for sym, sig in cands:
            c = snap["contracts"].get(sym) if isinstance(snap["contracts"].get(sym), dict) else {}
            cid = c.get("contract_id")
            if str(cid) in managed:
                continue
            ref = snap["prices"].get(sym)
            prop = {"symbol": sym, "side": sig["action"], "contract_id": cid, "ref_price": ref,
                    "stop_pct": risk.fnum(sig.get("stop_pct")), "tp_pct": risk.fnum(sig.get("tp_pct")),
                    "score": sig.get("score")}
            if "sizing" in cfg:
                planned_m = sum(risk.fnum(x.get("margin"), 0.0) for x in snap["planned"] if isinstance(x, dict))
                avail = (risk.fnum(snap.get("balance"), 0.0) or 0.0) - planned_m - cfg["risk"]["min_free_reserve"]
                sz = sizing.dynamic_size(sig, sig["action"], eq, avail, c, cfg, _market_ctx(root, sym, now, cfg))
                if not sz["ok"]:
                    rejections[sym] = ["sizing: " + "; ".join(sz["reasons"])]
                    continue
                prop["leverage"], prop["margin"] = sz["leverage"], sz["margin"]
                prop["sizing"] = {"risk_usdt": sz["risk_usdt"], "mult": sz["mult"]}
            res = risk.check_open(st, snap, prop, cfg) if fresh else \
                {"ok": False, "reasons": ["duplicate_or_old_tick"]}
            if not res["ok"]:
                rejections[sym] = res["reasons"]
                continue
            stop_p, tp_p = risk.compute_levels(sig["action"], ref, prop["stop_pct"], prop["tp_pct"])
            why = list(sig.get("reasons") or []) + ["score=%s" % sig.get("score")]
            d = {"op": "open", "symbol": sym, "contract_id": cid, "side": sig["action"],
                 "margin": res["margin"], "leverage": res["leverage"], "ref_price": ref,
                 "stop_price": stop_p, "tp_price": tp_p, "why": why}
            decisions.append(d)
            snap["planned"].append({"symbol": sym, "margin": res["margin"]})
            st["trades_today"] += 1
            st["pending_open"][sym] = {"ts": now, "side": sig["action"], "stop": stop_p,
                                       "tp": tp_p, "margin": res["margin"]}

    # журнал и пометки closing
    for d in decisions:
        code = d.pop("code", None)
        if d["op"] == "close":
            tr = st["positions"].get(str(d.get("contract_id")))
            if tr is not None:
                tr["closing"] = code
        S.journal(jpath, {"ts": S.iso_utc(now), "type": d["op"] + "_decision", "decision": d})
    if not decisions:
        decisions = [{"op": "hold", "why": ["no actionable signal or all entries rejected"]}]

    if fresh:
        st["last_tick_ts"] = now
    st["last_equity"] = eq
    S.save_state(spath, st)

    day_loss = (dse - eq) if (dse is not None and eq is not None) else None
    return {
        "halt": bool(st.get("halted")), "halt_reason": st.get("halt_reason") or "",
        "decisions": decisions,
        "diagnostics": {
            "equity": eq, "day_start_equity": dse, "day_loss": day_loss,
            "unrealized": einfo["unrealized"], "equity_uncertain": einfo["uncertain"],
            "session_active": st["session_active"], "trades_today": st["trades_today"],
            "open_positions": len(risk.open_positions(snap)),
            "signals": {s: {"action": g.get("action"), "score": g.get("score"), "warmup": g.get("warmup")}
                        for s, g in signals.items()},
            "open_rejections": rejections, "warnings": warnings,
        },
    }


def _do_start(args, root, cfg, raw, wall_now):
    spath = paths.state("state.json", root=root)
    jpath = paths.state("journal.jsonl", root=root)
    st = S.load_state(spath)
    ts = S.parse_ts((raw or {}).get("ts")) if raw else None
    now = ts if ts is not None else (wall_now if wall_now is not None else time.time())
    snap = None
    equity = None
    if raw and isinstance(raw.get("positions"), list):
        snap = _build_snapshot(dict(raw, ts=S.iso_utc(now)), cfg, st, wall_now)
        equity, _ = risk.compute_equity(snap)
    elif raw and risk.fnum(raw.get("balance_usdt")) is not None and not raw.get("positions"):
        equity = risk.fnum(raw.get("balance_usdt"))
    corrupt = str(st.get("halt_reason", "")).startswith("state_")
    same_day_loss_halt = (st.get("halted") and st.get("day") == S.day_str(now)
                          and str(st.get("halt_reason", "")).startswith("daily_loss"))
    if same_day_loss_halt and not args.force:
        return {"started": False, "halt": True, "halt_reason": st["halt_reason"], "decisions": [],
                "note": "daily loss halt is active for today; use --force to override"}
    S.new_day_reset(st, now, equity)
    st["session_active"] = True
    st["last_tick_ts"] = None
    st["pending_open"] = {}
    S.save_state(spath, st)
    S.journal(jpath, {"ts": S.iso_utc(now), "type": "start", "day_start_equity": equity,
                      "recovered_from_corrupt_state": corrupt})
    out = {"started": True, "halt": False, "halt_reason": "", "decisions": [],
           "day": st["day"], "day_start_equity": equity}
    if equity is None:
        out["warning"] = "equity unknown: day_start_equity will be set from the first tick; no opens until then"
    return out


def _do_stop(args, root, cfg, raw, wall_now):
    spath = paths.state("state.json", root=root)
    jpath = paths.state("journal.jsonl", root=root)
    st = S.load_state(spath)
    st["session_active"] = False
    S.save_state(spath, st)
    now = (S.parse_ts((raw or {}).get("ts")) if raw else None) or wall_now or time.time()
    flatten = bool(cfg.get("session", {}).get("flatten_on_stop", True))
    decisions = []
    if flatten and raw and isinstance(raw.get("positions"), list):
        snap = _build_snapshot(dict(raw, ts=S.iso_utc(now)), cfg, st, wall_now)
        decisions = risk.flatten_decisions(snap, ["session stopped"], "stop_command")
        for d in decisions:
            d.pop("code", None)
    S.journal(jpath, {"ts": S.iso_utc(now), "type": "stop", "flatten": flatten})
    return {"flatten": flatten, "halt": False, "halt_reason": "", "decisions": decisions}


def _do_report(args, root, cfg):
    st = S.load_state(paths.state("state.json", root=root))
    day = args.day or st.get("day") or S.day_str(time.time())
    ev = [e for e in S.read_journal(paths.state("journal.jsonl", root=root))
          if str(e.get("ts", ""))[:10] == day]
    opens = [e["decision"] for e in ev if e.get("type") == "open_decision" and isinstance(e.get("decision"), dict)]
    closes = [e["decision"] for e in ev if e.get("type") == "close_decision" and isinstance(e.get("decision"), dict)]
    closed = [e for e in ev if e.get("type") == "position_closed"]
    dse, le = st.get("day_start_equity"), st.get("last_equity")
    return {
        "day": day, "session_active": st.get("session_active"), "halted": st.get("halted"),
        "halt_reason": st.get("halt_reason"),
        "pnl_equity": (le - dse) if (dse is not None and le is not None and st.get("day") == day) else None,
        "realized_pnl_estimate": st.get("realized_pnl_today") if st.get("day") == day else None,
        "trades_today": st.get("trades_today") if st.get("day") == day else len(opens),
        "opens": [{"symbol": d.get("symbol"), "side": d.get("side"), "why": d.get("why")} for d in opens],
        "closes": [{"symbol": d.get("symbol"), "why": d.get("why")} for d in closes],
        "closed_positions": [{"symbol": e.get("symbol"), "est_pnl": e.get("est_pnl"),
                              "stop_like": e.get("stop_like")} for e in closed],
        "halts": [e.get("reason") for e in ev if e.get("type") == "halt"],
        "decisions": [],
    }


def process(argv, stdin_text, wall_now=None):
    """Никогда не бросает исключений: любая ошибка -> halt-ответ."""
    try:
        args = _parser().parse_args(argv)
        root = os.path.abspath(args.root or paths.root())
        if args.now:
            wall_now = S.parse_ts(args.now)
            if wall_now is None:
                raise ValueError("bad --now")
        if wall_now is None:
            wall_now = time.time()
        cfg = _load_config(args, root)
        if args.report:
            return clean(_do_report(args, root, cfg))
        if args.start:
            return clean(_do_start(args, root, cfg, _read_input(stdin_text, False), wall_now))
        if args.stop:
            return clean(_do_stop(args, root, cfg, _read_input(stdin_text, False), wall_now))
        raw = _read_input(stdin_text, True)
        return clean(_do_tick(args, root, cfg, raw, wall_now))
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        out = error_out(e)
        if os.environ.get("TRADER_DEBUG"):
            out["trace"] = traceback.format_exc()
        return out


def main(argv=None):
    text = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    out = process(sys.argv[1:] if argv is None else argv, text)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
