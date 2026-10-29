"""Генератор фикстурного корня дашборда v2 (WP8). Форматы файлов = контракты (SPEC_V2 01, 02 п. 2.11, 03, 06 п. 6.7).

Время в фикстурах относительное (heartbeat «1 с назад»), поэтому корень строится функцией `build(dest, now)`:
  тесты:    from fixtures_v2.gen import build; build(tmp_root, NOW)
  браузер:  python3 tests/fixtures_v2/gen.py [DEST] [--kill] [--many] [--live]   (по умолчанию tests/fixtures_v2/root, время = сейчас)
            --live: процесс остаётся и каждые 2 с обновляет heartbeat/снимок/цены (иначе «исполнитель» через 30 с будет считаться мёртвым)
            TB_ROOT=tests/fixtures_v2/root ./tb dash --port 8774 --max-seconds 900
Реальные state/ и runtime/ проекта не трогаются. В корень попадает только config/risk_profile.json (копия) для блока «запас до лимитов».
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
PRICES = {"SOL": 142.31, "ETH": 2321.5, "BTC": 67850.0, "LINK": 14.62, "DOGE": 0.1625, "BEAT": 0.052, "AVAX": 28.26}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _w(root, sub, name, obj, raw=False):
    p = os.path.join(root, sub, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(obj if raw else json.dumps(obj, ensure_ascii=False, indent=1))


def _jl(root, sub, name, rows, extra=""):
    _w(root, sub, name, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n" + extra, raw=True)


def spec(pid, sym, side, now, mode="live", thesis="Тезис плана: прокол уровня и возврат на повышенном объёме", entry=None, stop=142.9, targets=None, manage=None,
         tags=None, rev=1, gp=None, **kw):
    s = {"schema": "tb.plan/1", "id": pid, "rev": rev, "symbol": sym, "side": side, "mode": mode, "session_type": "intraday",
         "source": {"kind": "brain"}, "thesis": thesis, "tags": tags or {"experiment": "sweep-reject-v1", "variant": "A", "setup": "level_reject", "hypothesis": "H3"},
         "entry": entry or {"when": {"all": [{"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.05},
                                             {"kind": "volume_spike", "tf": "5m", "mult": 1.8, "lookback": 20, "on": "close", "dir": "any"}]},
                            "how": {"type": "market"}, "ref_price": 142.3, "valid": {"ttl_min": 45, "cancel_above": 143.4, "cancel_below": 140.0}},
         "stop": {"price": stop, "trigger_by": "last"},
         "targets": targets or [{"id": "t1", "price": 141.2, "frac": 0.5, "exec": "resting"}, {"id": "t2", "price": 140.2, "frac": 0.5, "exec": "resting"}],
         "risk": {"score": 0.7, "size_mult": 1.0}, "manage": manage or [], "invalidation": [], "lifecycle": {"ttl_min": 45, "max_hold_min": 240},
         "created_ts": now - 1500, "created_by": "brain:chat", "armed_ts": now - 1400, "prereg_hash": "a1b2c3d4e5f60718",
         "guard_preview": gp or {"ok": True, "leverage": 14, "margin": 3.1, "risk_usdt": 0.46, "net_rr": 1.2, "stop_pct": 0.49, "qty": 0.3}}
    s.update(kw)
    return s


def rt(pid, status, now, **kw):
    r = {"plan_id": pid, "plan_n": int(pid.rsplit("-", 1)[1]), "status": status, "rev_applied": 1, "hold": None, "hold_reason": None,
         "ts": {"created": now - 1500, "armed": now - 1400, "entered": None, "closed": None},
         "entry": {"state": "waiting", "link_id": None, "order_id": None, "attempts": 0, "blocked_why": None, "cond_state": {"seq_step": 0, "latched": {}}},
         "pos": None, "targets": {}, "rules": {}, "realized": {"pnl": 0.0, "fees": 0.0, "parts": []}, "close": {"reason": None, "px": None, "pnl": None}, "paper": False}
    r.update(kw)
    return r


def plan_dir(root, spec_obj, rt_obj, revs=None):
    d = os.path.join("state", "plans", spec_obj["id"])
    _w(root, d, "spec.json", spec_obj)
    if rt_obj is not None:
        _w(root, d, "runtime.json", rt_obj)
    _jl(root, d, "revs.jsonl", revs or [{"rev": spec_obj["rev"], "ts": iso(spec_obj["created_ts"]), "why": "создан", "by": "brain:chat", "spec": spec_obj}])


def build(root, now, kill=False, many=0):
    """Собирает корень во временной/целевой папке. many>0 добавляет столько простых armed-планов и решений (проверка скорости)."""
    for sub in ("state", "runtime", "config"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    rp = os.path.join(PROJECT, "config", "risk_profile.json")
    if os.path.exists(rp):
        shutil.copy(rp, os.path.join(root, "config", "risk_profile.json"))
    _w(root, "runtime", "prices.json", {"ts": iso(now - 2), "ts_epoch": now - 2, "env": "mainnet", "n": len(PRICES), "prices": PRICES, "marks": {}})
    _w(root, "state", "portfolio.json", {"venues": {"bybit": {"equity": 39.4, "free": 31.2, "ts": iso(now - 20)}}, "hwm": 41.2})
    _jl(root, "state", "sessions.jsonl", [{"ev": "start", "id": "intraday-1028-0700", "type": "intraday", "venue": "bybit", "thesis": "фикстура v2", "ts": iso(now - 3 * 3600),
                                          "start": now - 3 * 3600, "expires": now + 4 * 3600, "budget_usdt": 1.18, "equity_at_start": 39.8}])
    _w(root, "config", "experiments.json", {"sweep-reject-v1": {"hypothesis": "после прокола уровня и возврата цена идёт к ближней цели",
                                                                   "prereg": "метрика средний R после издержек, n>=30, один вариант против другого", "started": "2026-10-28", "min_n": 30}})
    # ---- планы
    P1, P2, P3, P4, P5 = "PL-1028-SOL-01", "PL-1028-ETH-02", "PL-1028-BTC-03", "PL-1028-AVAX-04", "PL-1028-LINK-05"
    plan_dir(root, spec(P1, "SOL", "short", now), rt(P1, "armed", now, entry={"state": "waiting", "attempts": 0, "blocked_why": None, "cond_state": {"seq_step": 0, "latched": {"c1": True}}}))
    plan_dir(root, spec(P2, "ETH", "short", now, rev=2, thesis="ETH отскок к 2350 в зону предложения, ждём отказ 15м", stop=2362.0,
                        entry={"when": {"kind": "candle_close", "tf": "15m", "dir": "below", "level": 2345.0, "n": 1}, "how": {"type": "limit", "price": 2344.0, "post_only": True},
                               "valid": {"ttl_min": 120}},
                        targets=[{"id": "t1", "price": 2300.0, "frac": 1.0, "exec": "resting"}],
                        tags={"experiment": "sweep-reject-v1", "variant": "B", "setup": "level_reject"}, gp={"ok": True, "leverage": 8, "margin": 3.4, "risk_usdt": 0.40, "net_rr": 1.7}),
             rt(P2, "armed", now), revs=[{"rev": 1, "ts": iso(now - 2000), "why": "создан", "by": "brain:chat", "spec": spec(P2, "ETH", "short", now, stop=2358.0)},
                                        {"rev": 2, "ts": iso(now - 600), "why": "уровень сместил после новой свечи", "by": "brain:chat", "spec": spec(P2, "ETH", "short", now, rev=2, stop=2362.0)}])
    plan_dir(root, spec(P3, "BTC", "long", now, mode="paper", thesis="Снятие стопов под 67500 и возврат выше; бумажная проверка",
                        entry={"when": {"kind": "seq", "within_min": 90, "steps": [{"kind": "price_beyond", "dir": "below", "level": 67500.0},
                                                                                    {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 67800.0, "n": 1}]},
                               "how": {"type": "market"}, "valid": {"ttl_min": 180}}, stop=67200.0,
                        targets=[{"id": "t1", "price": 68500.0, "frac": 1.0}], tags={"experiment": "sweep-reclaim-v1", "variant": "B", "setup": "reclaim"},
                        gp={"ok": True, "leverage": 5, "margin": 2.0, "risk_usdt": 0.25, "net_rr": 1.9}, warnings=[{"code": "W_NO_INVALIDATION"}]),
             rt(P3, "armed", now, paper=True, entry={"state": "waiting", "cond_state": {"seq_step": 1, "seq_started": now - 300, "latched": {}}}))
    manage = [{"id": "wake_m05", "when": {"kind": "pnl_r", "lte": -0.5}, "do": {"action": "notify", "code": "R_MINUS"}, "once": True},
              {"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True},
              {"id": "lock_t1", "when": {"kind": "target_filled", "target": "t1"}, "do": {"action": "move_stop", "to": "lock_r", "r": 0.5}, "once": True},
              {"id": "trail", "when": {"kind": "pnl_r", "gte": 1.5}, "do": {"action": "trail", "mode": "candle_extreme", "tf": "1m", "n": 3}},
              {"id": "pyr", "when": {"kind": "pnl_r", "gte": 1.2}, "do": {"action": "add", "risk_frac": 0.5, "how": "market"}, "once": True},
              {"id": "tmax", "when": {"kind": "elapsed", "min": 240}, "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": True}]
    plan_dir(root, spec(P4, "AVAX", "long", now, thesis="Возврат выше 28 после снятия стопов; держим до 29.3", stop=27.80, manage=manage,
                        entry={"when": {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 28.0}, "how": {"type": "market"}, "valid": {"ttl_min": 60}},
                        targets=[{"id": "t1", "price": 28.70, "frac": 0.5, "exec": "resting"}, {"id": "t2", "price": 29.30, "frac": 0.5, "exec": "resting"}],
                        gp={"ok": True, "leverage": 14, "margin": 2.9, "risk_usdt": 0.42, "net_rr": 1.8}),
             rt(P4, "managing", now, rev_applied=1, ts={"created": now - 3000, "armed": now - 2900, "entered": now - 2520, "closed": None},
                entry={"state": "filled", "link_id": "tbx-4-001", "cond_state": {"seq_step": 0, "latched": {}}},
                pos={"qty": 1.5, "avg": 28.10, "entries": [{"ts": now - 2520, "px": 28.10, "qty": 1.5, "fee": 0.017, "link": "tbx-4-001", "kind": "entry"}], "initial_stop": 27.80,
                     "cur_stop": 28.10, "r_unit": 0.30, "best_px": 28.49, "worst_px": 27.95, "journal_id": "BB-M-AVAX-1028-101502"},
                targets={"t1": {"state": "resting", "qty": 0.75, "price": 28.70, "link": "tbx-4-002", "filled_qty": 0.0}, "t2": {"state": "pending", "qty": 0.75, "price": 29.30}},
                rules={"wake_m05": {"fired": 1, "last_ts": now - 2000, "state": "done"}, "be_1r": {"fired": 1, "last_ts": now - 400, "state": "done"}, "lock_t1": {"fired": 0, "state": "waiting"},
                       "trail": {"fired": 0, "state": "waiting"}, "pyr": {"fired": 0, "state": "skipped", "last_ts": now - 390}, "tmax": {"fired": 0, "state": "waiting"}},
                realized={"pnl": 0.0, "fees": 0.017, "parts": []}))
    plan_dir(root, spec(P5, "LINK", "long", now, mode="paper", thesis="Теневой вариант S1: трейл по 3 свечам", stop=14.30,
                        entry={"when": {"kind": "price_beyond", "dir": "above", "level": 14.5}, "how": {"type": "market"}},
                        targets=[{"id": "t1", "price": 15.0, "frac": 1.0}], manage=[{"id": "be", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}}],
                        tags={"experiment": "trail-v1", "variant": "S1", "setup": "breakout"}, source={"kind": "shadow", "parent_plan": P4}),
             rt(P5, "managing", now, paper=True, ts={"created": now - 900, "armed": now - 880, "entered": now - 600, "closed": None},
                entry={"state": "filled"}, pos={"qty": 5.0, "avg": 14.52, "initial_stop": 14.30, "cur_stop": 14.30, "r_unit": 0.22, "best_px": 14.7, "worst_px": 14.48},
                targets={"t1": {"state": "resting", "price": 15.0, "qty": 5.0}}, rules={"be": {"state": "waiting"}}))
    for i, (pid, st, sym, side, reason, extra) in enumerate([
            ("PL-1028-DOGE-06", "closed", "DOGE", "long", "tp", {"close": {"reason": "tp", "px": 0.1650, "pnl": 0.31}, "realized": {"pnl": 0.31, "fees": 0.04}}),
            ("PL-1028-BEAT-07", "cancelled", "BEAT", "short", None, {}), ("PL-1028-ETH-08", "expired", "ETH", "long", None, {}),
            ("PL-1028-SOL-09", "invalidated", "SOL", "short", None, {}), ("PL-1028-BTC-10", "rejected", "BTC", "long", None, {})]):
        tsd = {"created": now - 7000, "armed": now - 6900, "entered": (now - 5000) if st == "closed" else None, "closed": now - 3600 - i * 600}
        plan_dir(root, spec(pid, sym, side, now, mode="paper" if i == 3 else "live", tags={"experiment": "sweep-reject-v1", "variant": "A"}), rt(pid, st, now, ts=tsd, **extra))
    # старый закрытый (больше суток) и сломанная/неполная папки
    plan_dir(root, spec("PL-1026-SOL-99", "SOL", "long", now), rt("PL-1026-SOL-99", "closed", now, ts={"created": now - 9e5, "armed": now - 9e5, "entered": None, "closed": now - 3 * 86400}))
    os.makedirs(os.path.join(root, "state", "plans", "PL-1028-XXX-98"), exist_ok=True)
    _w(root, "state/plans/PL-1028-XXX-98", "spec.json", "{битый json", raw=True)
    _w(root, "state/plans/PL-1028-ADA-97", "spec.json", spec("PL-1028-ADA-97", "ADA", "long", now, thesis="черновик без runtime"))
    os.makedirs(os.path.join(root, "state", "plans", "_inbox"), exist_ok=True)
    if many:
        for i in range(many):
            pid = "PL-1028-S%03d-%02d" % (i % 900, i % 99 + 1)
            plan_dir(root, spec(pid, "SOL", "long", now, mode="paper"), rt(pid, "armed", now))
    # ---- исполнитель
    _w(root, "runtime", "exec_health.json", {"ts_epoch": now - 1.2, "pid": 4242, "version": "2.0-fixture", "env": "mainnet", "profile": "trade", "observe": False, "uptime_s": 5400,
                                             "ws": {"public": {"up": True, "age_s": 0.2}, "private": {"up": True, "age_s": 1.1}}, "rest": {"ok": True, "last_ok_s": 2, "errors_1m": 0},
                                             "loop_lag_ms": 12, "plans": {"armed": 3, "managing": 2, "draft": 0}, "positions": 1, "kill": bool(kill), "hold": None, "hold_reasons": [],
                                             "last_reconcile_ts": now - 14, "last_event_seq": 1843, "profile_hash": "3f9a1c"})
    _w(root, "runtime", "exec_snapshot.json", {"ts_epoch": now - 1.5, "env": "mainnet", "account": {"equity": 39.4, "free": 31.2},
                                               "positions": [{"sym": "AVAX", "side": "long", "size": 1.5, "avg": 28.10, "mark": PRICES["AVAX"], "sl": 28.10, "tp": 0.0, "liq": 26.2, "upnl": 0.24, "lev": 14,
                                                              "margin": 3.0, "owner": "plan:" + P4}],
                                               "orders": [{"sym": "AVAX", "link_id": "tbx-4-002", "side": "Sell", "type": "limit", "price": 28.70, "qty": 0.75, "filled": 0.0, "reduce_only": True,
                                                           "role": "target", "plan_id": P4}]})
    _w(root, "runtime", "levels.json", {"SOL": {"price": 142.31, "day": {"hi": 143.6, "lo": 139.9, "open": 140.8}, "prev_day": {"hi": 142.2, "lo": 138.7, "close": 140.9},
                                                "week": {"hi": 146.0, "lo": 136.5}, "swing_5m": [141.0, 142.5, 143.1], "atr_5m_14": 0.31, "vol_med_5m_20": 1850, "vwap_day": 141.6}})
    if kill:
        _w(root, "state", "KILL", {"flatten": False, "reason": "owner halt", "ts": iso(now - 40)})
    # ---- решения
    rows, seq = [], 5500

    def dec(dt, actor, plan, kind, what, why, args=None, basis=None, res=None):
        rows.append({"ts": iso(now - dt), "t": now - dt, "actor": actor, "plan": plan, "rev": 1, "kind": kind, "what": what, "args": args or {}, "why": why,
                     "basis": basis or {}, "res": res or {}})
    dec(3000, "brain:chat", P4, "cmd", "submit", "Возврат выше 28 после снятия стопов; тезис в плане", basis={"brief_id": "B-20261028-1015-02", "event_seq": 1840}, res={"ok": True, "code": "OK"})
    dec(2990, "exec", P4, "accept", "accept", "CMD:C-20261028T101502-a1b2c3", res={"ok": True, "code": "OK"})
    dec(2520, "exec", P4, "intent", "place_entry", "TRIGGER:candle_close", args={"qty": 1.5, "px": 28.10, "type": "market"}, basis={"px": 28.10, "spread_bps": 2.1}, res={})
    dec(2519, "exec", P4, "result", "place_entry", "TRIGGER:candle_close", args={"qty": 1.5, "px": 28.10, "type": "market"}, basis={"px": 28.10, "r": 0.0},
        res={"ok": True, "code": "OK", "link_id": "tbx-4-001", "order_id": "98123", "exch_ms": 240})
    dec(2500, "exec", P4, "result", "place_target", "PROTECT:ladder", args={"target": "t1", "price": 28.70}, res={"ok": True, "code": "OK", "link_id": "tbx-4-002", "exch_ms": 180})
    dec(2000, "exec", P4, "rule", "notify", "RULE:wake_m05", args={}, basis={"px": 27.95, "r": -0.5}, res={"ok": True, "code": "OK"})
    dec(900, "brain:chat", P4, "cmd", "stop", "Подтянул стоп под минимум отката, риск не растёт", args={"to": 27.95, "prev": 27.80}, basis={"event_seq": 1851, "brief_id": "B-20261028-1015-02", "px": 28.20, "r": 0.3},
        res={"ok": True, "code": "OK"})
    dec(899, "exec", P4, "result", "set_stop", "CMD:C-20261028T104003-ff0011", args={"to": 27.95, "prev": 27.80}, basis={"px": 28.20, "r": 0.3}, res={"ok": True, "code": "OK", "exch_ms": 210})
    dec(400, "exec", P4, "rule", "move_stop", "RULE:be_1r", args={"to": 28.10, "prev": 27.95}, basis={"px": 28.40, "r": 1.0}, res={"ok": True, "code": "OK", "exch_ms": 190})
    dec(390, "exec", P4, "skip", "add", "RULE:pyr", args={}, basis={"px": 28.46, "r": 1.2}, res={"ok": False, "code": "ADD_DISABLED"})
    dec(300, "brain:chat", P4, "cmd", "amend", "Расширить стоп до 27.60: даём больше воздуха", args={"stop": 27.60}, basis={"px": 28.30}, res={"ok": False, "code": "E_STOP_WIDEN"})
    dec(280, "exec", P4, "reject", "amend", "CMD:C-20261028T105001-aa22bb", basis={"px": 28.30}, res={"ok": False, "code": "E_STOP_WIDEN"})
    dec(100, "exec", P4, "skip", "move_stop", "RULE:trail", args={"to": 28.05}, basis={"px": 28.27, "r": 0.5}, res={"ok": True, "code": "NOT_TIGHTER"})
    dec(2000, "brain:chat", P1, "cmd", "submit", "Отказ от сопротивления 142.5 на 5м, объём подтверждает", basis={"event_seq": 1842}, res={"ok": True, "code": "OK"})
    dec(1990, "exec", P1, "accept", "accept", "CMD:C-1", res={"ok": True, "code": "OK"})
    dec(1500, "exec", P1, "reject", "place_entry", "TRIGGER:candle_reject", args={"qty": 0.3, "px": 142.4}, basis={"px": 142.4, "spread_bps": 7.0}, res={"ok": False, "code": "DENY_GUARD"})
    dec(1200, "exec", P1, "reject", "place_entry", "TRIGGER:candle_reject", args={"qty": 0.3}, res={"ok": False, "code": "DENY_SPREAD"})
    dec(600, "owner", None, "note", "note", "владелец: наблюдаем, вход не форсировать")
    dec(500, "exec", None, "protect", "set_hold", "PROTECT:private_ws_down", res={"ok": True, "code": "OK"})
    dec(450, "exec", None, "reconcile", "reconcile", "RECON:ok", res={"ok": True, "code": "OK"})
    dec(7200, "brain:chat", "PL-1028-DOGE-06", "cmd", "close", "Закрыть по тейку вручную", args={"frac": 1.0}, res={"ok": True, "code": "OK"})
    if many:
        for i in range(many * 5):
            dec(10000 - i, "exec", "PL-1028-S%03d-%02d" % (i % 900, i % 99 + 1), "rule", "move_stop", "RULE:be_1r", args={"to": 100.0 + i % 7, "prev": 99.0}, res={"ok": True, "code": "OK"})
    rows.sort(key=lambda r: r["t"])                  # seq монотонен по времени, как в настоящем журнале
    for r in rows:
        seq += 1
        r.update({"seq": seq, "prev": "%016x" % (seq - 1), "h": "%016x" % seq})
    _jl(root, "state", "decisions.jsonl", rows, extra='{"seq": 9999, "ts": "обрыв записи\n')
    # ---- шина событий и курсоры
    evs = [(1830, "plan_armed", "wake", P1, "SOL", {"rev": 1}), (1840, "scanner_signal", "wake", None, "DOOD", {"kind": "range_5m", "px": 0.002, "score": 5.1}),
           (1843, "entry_filled", "wake", P4, "AVAX", {"avg": 28.10, "qty": 1.5, "sl": 27.80, "risk_usdt": 0.45}),
           (1851, "milestone", "wake", P4, "AVAX", {"name": "near_target", "r": 0.9, "px": 28.40}), (1852, "rule_fired", "info", P4, "AVAX", {"rule": "pyr", "ok": False, "code": "ADD_DISABLED"}),
           (1853, "limit_hit", "wake", None, "BEAT", {"which": "cooldown", "until_ts": now + 18 * 60}), (1854, "connectivity", "wake", None, None, {"channel": "private", "state": "up", "down_s": 24})]
    _jl(root, "state/bus", "events.jsonl", [{"seq": s, "ts": iso(now - (1860 - s) * 20), "type": t, "pri": p, "plan": pl, "sym": sy, "d": d} for s, t, p, pl, sy, d in evs]
        + [{"seq": 1900, "ts": iso(now - 5), "type": "error", "pri": "wake", "d": {"where": "ws", "code": "E_X"}}][:0])
    _w(root, "state/bus/cursors", "chat.json", {"delivered": 1854, "acked": 1843, "ts": iso(now - 30)})
    # ---- досье
    _w(root, "state/briefs", "B-20261028-1015-02.json", {
        "brief_id": "B-20261028-1015-02", "ts": iso(now - 3000), "scope": "start", "trunc": False,
        "system": {"exec": {"alive": True, "age_s": 1.2, "env": "mainnet", "mode": "live", "observe": False, "hold": None, "ws": "up", "reconcile_age_s": 12}, "kill": False,
                   "scanner": {"alive": True, "age_s": 14}, "feed": {"alive": True}, "unacked": 0},
        "account": {"equity": 39.2, "free": 31.0, "dd_pct": 1.2, "dd_mult": 1.0, "day_pnl": -0.4, "stops_today": 1, "stop_streak": 1, "day_trades": 3},
        "session": {"id": "intraday-1028-0700", "type": "intraday", "min_left": 412, "budget": 1.18, "realized": -0.4, "open_risk": 0.5, "trades": 3, "max_trades": 6},
        "room": {"allowed": True, "reasons": [], "next_risk_usdt": 0.52, "limited_by": None, "slots_free": 1, "daily_room": 2.1, "cooldowns": [{"sym": "BEAT", "min_left": 18}], "pause_min_left": 0},
        "positions": [{"plan": P4, "sym": "AVAX", "side": "long", "size": 1.5, "avg": 28.10, "px": 28.26, "sl": 27.95, "liq": 26.2, "r": 0.54, "mae_r": -0.2, "mfe_r": 0.8, "age_min": 42,
                       "targets": [{"id": "t1", "px": 28.70, "state": "resting"}], "rules": [{"id": "be_1r", "state": "waiting", "need": "pnl_r>=1.0 (now 0.54)"}]}],
        "plans": [{"id": P2, "sym": "ETH", "side": "short", "status": "armed", "rev": 2, "entry": "candle_close 15m 2345 (ждёт)", "dist_pct": 1.1, "ttl_left_min": 38, "exp": "sweep-reject-v1/B",
                   "own_thesis": "ETH отскок к 2350 в зону предложения, ждём отказ 15м"}],
        "market": {"btc": {"ret_1h_pct": -0.3, "ret_24h_pct": -0.6, "range_5m_pct": 0.12}, "regime": "range", "risk_off": False, "utc_session": "europe", "blackout": {"event": "FOMC", "in_min": 340}},
        "hot": [{"sym": "DOOD", "score": 5.1, "reasons": ["range_5m", "turnover_surge"], "px": 0.002035, "ret_5m_pct": 1.2, "range_5m_pct": 1.8, "turn_surge_5m": 5.4, "spread_bps": 4.1,
                 "funding_pct": 0.01, "lot_usdt": 6.2, "tradable": True}],
        "levels": {"SOL": {"px": 142.31, "day": {"hi": 143.6, "lo": 139.9}, "prev_day": {"hi": 142.2, "lo": 138.7}, "week": {"hi": 146.0, "lo": 136.5}, "swing_5m": [141.0, 142.5, 143.1], "atr_5m": 0.31, "vol_med_5m": 1850}},
        "ideas": {"active": [{"id": "I7", "sym": "SOL", "side": "long", "level": 140.0, "dist_pct": 1.6, "status": "watching", "age_h": 3.1}], "stats": {"n_closed": 12, "note": "n<30"}},
        "recent": [{"seq": 1840, "type": "scanner_signal", "sym": "DOOD", "d": {"kind": "range_5m", "px": 0.002}}],
        "lessons": ["rule-break: 2 из 4 последних сделок"], "checklist": [{"id": "C1", "text": "Активная сессия нужного типа?"}, {"id": "C3", "text": "Стоп за экстремумом отката и в пределах max_stop_pct?"}],
        "api_key": "SHOULD_BE_SCRUBBED"})
    _w(root, "state/briefs", "B-20261028-1100-03.json", {"brief_id": "B-20261028-1100-03", "ts": iso(now - 300), "scope": "event", "account": {"equity": 39.4, "free": 31.2}, "recent": []})
    # ---- журнал сделок: прежние, плановые (с add/part/adjust) и бумажные для статистики
    def op(i, ts, **kw):
        r = {"ev": "open", "id": i, "ts_open": iso(ts), "sym": "SOL", "side": "long", "entry": 100.0, "lev": 10.0, "margin": 2.0, "notional": 20.0, "sl": 99.0, "tp": 103.0, "risk_usdt": 0.2,
             "thesis": "тезис", "tags": ["bybit"], "mode": "live", "venue": "bybit", "session": "intraday-1028-0700", "stype": "intraday"}
        r.update(kw)
        return r

    def cl(i, ts, pnl, **kw):
        r = {"ev": "close", "id": i, "ts_close": iso(ts), "exit": 101.0, "reason": "tp", "pnl_usdt": pnl, "fees_usdt": 0.04, "funding_usdt": 0.0}
        r.update(kw)
        return r
    tr = [op("LEG1", now - 8000, sym="ETH", side="short", entry=2330.0, sl=2350.0, tp=2290.0), cl("LEG1", now - 6000, 0.21, exit=2310.0, reason="tp"),
          op("LEG2", now - 5000, sym="DOGE", entry=0.16, sl=0.158, tp=0.165, notional=20.0), cl("LEG2", now - 4400, -0.2, exit=0.158, reason="sl")]
    tr += [op("BB-M-AVAX-1028-101502", now - 2520, sym="AVAX", entry=28.10, notional=42.15, margin=3.0, lev=14, sl=27.80, tp=29.30, risk_usdt=0.45, plan_id=P4, plan_rev=1, exp="sweep-reject-v1", var="A",
              setup="level_reject", tags=["bybit", "mainnet", "plan", "exp:sweep-reject-v1", "var:A"]),
           {"ev": "add", "id": "BB-M-AVAX-1028-101502", "ts": iso(now - 1500), "px": 28.20, "qty": 0.2, "fee": 0.008},
           {"ev": "part", "id": "BB-M-AVAX-1028-101502", "ts": iso(now - 800), "px": 28.45, "qty": 0.5, "fee": 0.01, "pnl": 0.2},
           {"ev": "adjust", "id": "BB-M-AVAX-1028-101502", "ts": iso(now - 700), "sl": 28.10, "tp": 29.30, "by": "rule"}]
    import random
    rnd = random.Random(7)
    for k in range(36):                        # бумажные сделки: вариант A (n>=30) и B (n<30)
        var, mode = ("A" if k < 33 else "B"), "paper"
        pid = "PX%02d" % k
        win = rnd.random() < (0.45 if var == "A" else 0.6)
        pnl = round(rnd.uniform(0.15, 0.35) if win else -rnd.uniform(0.1, 0.22), 3)
        tr += [op(pid, now - 86400 * 2 + k * 600, mode=mode, plan_id="PL-1027-X%02d" % k, plan_rev=1, exp="sweep-reject-v1", var=var, setup="level_reject", tags=["bybit", "paper", "shadow"],
                  risk_usdt=0.2), cl(pid, now - 86400 * 2 + k * 600 + 300, pnl, reason=rnd.choice(["tp", "sl", "trail", "be", "timeout"]), path={"mae_r": -rnd.random(), "mfe_r": rnd.random() * 2})]
    _jl(root, "state", "trades.jsonl", tr)
    return root


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    kill = "--kill" in argv
    many = 0
    if "--many" in argv:
        many = 200
    argv = [a for a in argv if not a.startswith("--")]
    dest = argv[0] if argv else os.path.join(HERE, "root")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    build(dest, time.time(), kill=kill, many=many)
    print("фикстурный корень: %s" % dest, flush=True)
    if "--live" in sys.argv:
        try:
            while True:
                now = time.time()
                for name in ("exec_health.json", "exec_snapshot.json", "prices.json"):
                    p = os.path.join(dest, "runtime", name)
                    try:
                        with open(p, encoding="utf-8") as f:
                            d = json.load(f)
                        d["ts_epoch"] = now - 0.5
                        with open(p + ".tmp", "w", encoding="utf-8") as f:
                            json.dump(d, f)
                        os.replace(p + ".tmp", p)
                    except (OSError, ValueError):
                        pass
                time.sleep(2)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
