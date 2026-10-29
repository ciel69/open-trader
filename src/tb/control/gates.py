"""Допуск в live: жёсткие гейты H1-H8 (обойти нельзя) и мягкие предупреждения W1-W9 (владелец принимает под свою ответственность). Владелец: WP12.

`collect(root, now, cfg, args)` читает файлы (без сети и без биржи) в словарь фактов; `hard(f, args)` и `soft(f, args)` чистые функции от фактов (их и тестируют по одному).
Гейт: {"id","name","ok","msg","numbers"}; предупреждение: {"id","code","msg","numbers"}. Любая причина с цифрами. Исполнитель не в целевом профиле (идёт переключение на live):
H2-H4 помечаются `deferred` и перепроверяются жёстко после переключения (ctl при исполнении `day.start`).
"""
import json
import math
import time

from tb import paths
from tb.brain import common
from tb.control import brain_iface, ctlstate, riskstore
from tb.core import exec_cfg, localcfg
from tb.platform import services as S

TARGET_PROFILE = {"live": "trade", "testnet": "testnet", "paper": "paper", "observe": "paper"}
HOLD_BAD = ("private_ws", "blind", "bad_data")
HB_MAX_S = 10.0
RECONCILE_MAX_S = 60.0
SNAP_MAX_S = 30.0
BLOCK_BEFORE_MIN = 30


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _g(gid, name, ok, msg, **numbers):
    return {"id": gid, "name": name, "ok": bool(ok), "msg": msg, "numbers": numbers}


def _w(wid, code, msg, **numbers):
    return {"id": wid, "code": code, "msg": msg, "numbers": numbers}


def _trades(root):
    """state/trades.jsonl -> закрытые сделки [{"mode","stype","ts_close","pnl_usdt"}] и открытые (ids)."""
    opened, closed = {}, []
    try:
        with open(paths.state("trades.jsonl", root=root), encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("ev") == "open" and r.get("id"):
                    opened[r["id"]] = r
                elif r.get("ev") == "close" and r.get("id") in opened and _num(r.get("pnl_usdt")):
                    o = opened[r["id"]]
                    closed.append({"mode": o.get("mode"), "stype": o.get("stype"), "ts_close": str(r.get("ts_close") or ""), "pnl_usdt": float(r["pnl_usdt"])})
    except OSError:
        pass
    return closed


def collect(root, now, cfg, args=None):
    args = args or {}
    mode = args.get("mode") or "live"
    f = {"now": now, "root": root, "mode": mode, "args": args, "cfg": cfg}
    f["kill"] = exec_cfg.kill_state(root)
    f["health"] = common.exec_health(root=root, now=now)
    snap = common.read_json(paths.exec_snapshot_file(root=root))
    f["snapshot"] = snap if isinstance(snap, dict) else None
    sts = f["snapshot"].get("ts_epoch") if f["snapshot"] else None
    f["snapshot_age"] = max(0.0, now - sts) if _num(sts) else None
    port = ctlstate.read_json(paths.state("portfolio.json", root=root))
    f["portfolio"] = port if isinstance(port, dict) else {}
    prof = ctlstate.read_json(paths.config("risk_profile.json", root=root))
    f["profile"] = prof if isinstance(prof, dict) else None
    try:
        f["limits"] = exec_cfg.load(root)
        f["limits_warn"] = exec_cfg.warnings(root)
        f["limits_err"] = None
    except ValueError as e:
        f["limits"], f["limits_warn"], f["limits_err"] = None, [], str(e)[:120]
    f["risk"] = riskstore.current(root)
    f["trades"] = _trades(root)
    f["live_starts"] = ctlstate.live_starts(root, now)
    f["facts"] = ctlstate.read_json(paths.runtime("intel", "facts.json", root=root))
    f["brain"] = brain_iface.status(root, now)
    f["agent"] = args.get("agent") or f["brain"].get("agent") or brain_iface.DEFAULT_AGENT
    f["agents"] = ctlstate.read_json(paths.config("agents.json", root=root))
    f["intel_cfg"] = localcfg.load("intel", root)
    try:
        from tb.platform import status as ST
        f["platform"] = ST.build_status(root, now)
    except Exception:
        f["platform"] = None
    f["lifecycle_cfg"] = ctlstate.read_json(paths.config("lifecycle.json", root=root)) or {}
    return f


# ---------------------------------------------------------------- жёсткие гейты
def _exec_state(f):
    """-> (is_target, h, d): исполнитель уже в целевом профиле?"""
    h = f["health"]
    d = h.get("data") or {}
    want = TARGET_PROFILE.get(f["mode"], "paper")
    is_target = bool(h.get("present") and d.get("profile") == want and bool(d.get("observe")) == (f["mode"] == "observe"))
    return is_target, h, d


def h1(f):
    k = f["kill"]
    if k is None:
        return _g("H1", "KILL чист", True, "state/KILL нет")
    return _g("H1", "KILL чист", False, "KILL активен (flatten=%s, причина: %s)" % (bool(k.get("flatten")), (str(k.get("reason")) or "-")[:60]), flatten=bool(k.get("flatten")))


def h2(f):
    name = "Стоп на бирже у каждой позиции"
    is_target, h, d = _exec_state(f)
    if not is_target:
        return dict(_g("H2", name, True, "проверится после переключения исполнителя на %s" % f["mode"]), deferred=True)
    snap = f["snapshot"]
    if snap is None or f["snapshot_age"] is None or f["snapshot_age"] > SNAP_MAX_S:
        return _g("H2", name, False, "снимок портфеля исполнителя устарел или отсутствует (%s с)" % ("нет" if f["snapshot_age"] is None else int(f["snapshot_age"])), age_s=f["snapshot_age"])
    bad = [p.get("sym") or "?" for p in (snap.get("positions") or []) if isinstance(p, dict) and not (_num(p.get("sl")) and p["sl"] > 0)]
    if bad:
        return _g("H2", name, False, "позиции без стопа на бирже: %s" % ", ".join(bad[:5]), count=len(bad))
    return _g("H2", name, True, "позиций %d, стоп у каждой" % len(snap.get("positions") or []), positions=len(snap.get("positions") or []))


def h3(f):
    name = "Связь с биржей и исполнителем"
    is_target, h, d = _exec_state(f)
    if not h.get("present") or not h.get("alive"):
        return _g("H3", name, False, "исполнитель не отвечает (heartbeat %s)" % ("нет" if h.get("age_s") is None else "%d с" % h["age_s"]), age_s=h.get("age_s"))
    if not is_target:
        return dict(_g("H3", name, True, "исполнитель жив, режим будет переключён на %s; связь перепроверится после переключения" % f["mode"], age_s=h.get("age_s")), deferred=True)
    why = []
    if h["age_s"] is None or h["age_s"] > HB_MAX_S:
        why.append("heartbeat %s с (лимит %d)" % (h["age_s"], HB_MAX_S))
    ws = d.get("ws") or {}
    if not ((ws.get("private") or {}).get("up")):
        why.append("приватный WS не подключён")
    if not ((d.get("rest") or {}).get("ok")):
        why.append("REST не отвечает")
    bad = [x for x in (d.get("hold_reasons") or []) if any(b in str(x) for b in HOLD_BAD)]
    if bad:
        why.append("hold: %s" % ", ".join(str(x) for x in bad[:3]))
    ra = d.get("reconcile_age_s")
    if not _num(ra) or ra >= RECONCILE_MAX_S:
        why.append("сверка с биржей %s" % ("не проводилась" if not _num(ra) else "%d с назад (лимит %d)" % (ra, RECONCILE_MAX_S)))
    if why:
        return _g("H3", name, False, "; ".join(why), reconcile_age_s=ra)
    return _g("H3", name, True, "исполнитель жив, WS и REST в порядке, сверка %d с назад" % ra, reconcile_age_s=ra)


def h4(f):
    name = "Сверка портфеля с биржей"
    is_target, h, d = _exec_state(f)
    if not is_target:
        return dict(_g("H4", name, True, "проверится после переключения исполнителя на %s" % f["mode"]), deferred=True)
    why = []
    if "equity_mismatch" in [str(x) for x in (d.get("hold_reasons") or [])]:
        why.append("equity_mismatch")
    diffs = d.get("diffs")
    if not _num(diffs):
        why.append("число расхождений неизвестно")
    elif diffs != 0:
        why.append("расхождений позиций: %d" % diffs)
    dev, tol = None, ((f["limits"] or {}).get("equity_sync") or {}).get("max_dev_frac", 0.10)
    snap = f["snapshot"] or {}
    eq = (snap.get("account") or {}).get("equity")
    mine = (f["portfolio"].get("venues") or {}).get("bybit", {}).get("equity")
    if _num(eq) and _num(mine) and max(eq, mine) > 0:
        dev = abs(eq - mine) / max(eq, mine)
        if dev > tol:
            why.append("equity расходится на %.1f%% (допуск %.1f%%)" % (dev * 100, tol * 100))
    if why:
        return _g("H4", name, False, "; ".join(why), diffs=diffs, dev=dev)
    return _g("H4", name, True, "расхождений нет", diffs=diffs, dev=dev)


def h5(f):
    name = "Суточный лимит убытка"
    prof = f["profile"]
    if prof is None:
        return _g("H5", name, False, "risk_profile.json не читается")
    day = ctlstate.utc_day(f["now"])
    realized = sum(t["pnl_usdt"] for t in f["trades"] if t["mode"] == "live" and t["ts_close"].startswith(day))
    total = sum((v or {}).get("equity", 0.0) for v in (f["portfolio"].get("venues") or {}).values() if isinstance(v, dict) and _num((v or {}).get("equity")))
    lim = prof.get("daily_loss_limit_usdt")
    frac = riskstore.details(f["risk"]["level"], f["root"]).get("daily_loss_frac")
    if not _num(frac):
        frac = prof.get("daily_loss_frac")
    cands = [x for x in (lim if _num(lim) else None, total * frac if _num(frac) and total > 0 else None) if x is not None and x > 0]
    limit = min(cands) if cands else None
    hwm, dd = f["portfolio"].get("hwm"), 0.0
    if _num(hwm) and hwm > 0 and total > 0:
        dd = max(0.0, 1.0 - total / hwm)
    halt = prof.get("drawdown_halt")
    if limit is None:
        return _g("H5", name, False, "лимит убытка не определён в risk_profile.json")
    if _num(halt) and dd >= halt:
        return _g("H5", name, False, "просадка %.1f%% от максимума (остановка с %.0f%%): drawdown_halt" % (dd * 100, halt * 100), dd=dd)
    if realized <= -limit:
        return _g("H5", name, False, "дневной убыток %.2f USDT исчерпал лимит %.2f USDT" % (-realized, limit), realized=realized, limit=limit)
    return _g("H5", name, True, "убыток за сутки %.2f из %.2f USDT" % (max(0.0, -realized), limit), realized=realized, limit=limit)


def h6(f):
    name = "Мозг доступен"
    p = brain_iface.probe(f["root"], f["now"], f["cfg"], f["agent"])
    return _g("H6", name, p.get("ok"), p.get("msg") or "", **(p.get("numbers") or {}))


def h7(f):
    name = "Конфигурации читаются и не ослабляют защиту"
    why = []
    if f["profile"] is None:
        why.append("risk_profile.json не читается")
    if f["limits_err"]:
        why.append(f["limits_err"])
    elif f["limits_warn"]:
        why.append("exec_limits: %s" % f["limits_warn"][0][:100])
    from tb.core import risklevels as RL
    cfgl, _w_ = RL.load_config(f["root"])
    if cfgl is None:
        why.append("нет или не читается config/risk_levels.json")
    if not f["risk"]["valid"]:
        why.append("файл уровня риска не прошёл проверку подписи (действует уровень 1)")
    ag = ((f["agents"] or {}).get("agents") or {}).get(f["agent"]) if isinstance(f["agents"], dict) else None
    if not isinstance(ag, dict):
        why.append("агент %s не найден в agents.json" % f["agent"])
    else:
        lim = ag.get("limits") or {}
        if "live" not in (lim.get("modes") or []) or (isinstance(ag.get("autonomy"), dict) and ag["autonomy"].get("live") is False):
            why.append("агенту %s не разрешён live (agents.json limits.modes)" % f["agent"])
        for k in ("max_risk_usdt", "max_risk_pct"):
            if not (_num(lim.get(k)) and lim[k] > 0):
                why.append("у агента нет конечного %s" % k)
        if not (_num(lim.get("max_size_mult")) and 0 < lim["max_size_mult"] <= 1):
            why.append("max_size_mult агента должен быть в (0, 1]")
        if lim.get("allow_add") is not False:
            why.append("allow_add у агента должен быть false")
    if why:
        return _g("H7", name, False, "; ".join(why[:4]), level=f["risk"]["level"])
    return _g("H7", name, True, "конфигурации в порядке, уровень риска %s «%s»" % (f["risk"]["level"], f["risk"]["name"]), level=f["risk"]["level"], agent=f["agent"])


def h8(f):
    name = "Лимит запусков live"
    cfg, ls, now = f["cfg"], f["live_starts"], f["now"]
    n, mx = len(ls["starts"]), int(cfg["max_live_starts_day"])
    if n >= mx:
        return _g("H8", name, False, "запусков live за сутки UTC: %d из %d" % (n, mx), starts=n, limit=mx)
    cd = float(cfg["live_restart_cooldown_min"]) * 60
    if ls["last_stop_ts"] is not None and now - ls["last_stop_ts"] < cd:
        return _g("H8", name, False, "пауза после остановки live: ещё %d мин" % math.ceil((cd - (now - ls["last_stop_ts"])) / 60.0), wait_s=cd - (now - ls["last_stop_ts"]))
    return _g("H8", name, True, "запусков за сутки %d из %d" % (n, mx), starts=n, limit=mx)


HARD = (h1, h2, h3, h4, h5, h6, h7, h8)


def hard(f, args=None):
    return [g(f) for g in HARD]


# ---------------------------------------------------------------- мягкие предупреждения
def w1(f):
    rec = f["cfg"]["recommend"]
    paper = [t for t in f["trades"] if t["mode"] == "paper"]
    days = len(set(t["ts_close"][:10] for t in paper if t["ts_close"]))
    if len(paper) < rec["paper_trades"] or days < rec["paper_days"]:
        return _w("W1", "paper_history", "paper-сделок %d из рекомендованных %d; дней %d из %d" % (len(paper), rec["paper_trades"], days, rec["paper_days"]),
                  trades=len(paper), need_trades=rec["paper_trades"], days=days, need_days=rec["paper_days"])


def w2(f):
    br, now = f["brain"], f["now"]
    profiles = sorted(set((br.get("profiles") or {}).values())) or sorted(br.get("live_ok") or {})[:1]
    lo = br.get("live_ok") or {}
    bad = []
    age_max = float(f["cfg"]["eval_max_age_days"]) * 86400
    for p in profiles or ["не назначен"]:
        e = lo.get(p) or {}
        if not e.get("ok"):
            bad.append("профиль %s: метки live_ok нет, eval не проводился" % p)
        elif _num(e.get("eval_ts")) and now - e["eval_ts"] > age_max:
            bad.append("профиль %s: eval старше %d дн" % (p, f["cfg"]["eval_max_age_days"]))
    if bad:
        return _w("W2", "no_live_ok", "; ".join(bad[:3]), profiles=len(profiles))


def w3(f):
    fx, now = f["facts"], f["now"]
    mx = float((f["intel_cfg"] or {}).get("max_age_min") or f["cfg"]["facts_max_age_min"])
    if not isinstance(fx, dict):
        return _w("W3", "facts_missing", "facts.json отсутствует: фактура не собрана")
    parts, ts = [], fx.get("ts")
    age = (now - ts) / 60.0 if _num(ts) else None
    if age is None or age > mx:
        parts.append("facts.json %s" % ("без времени" if age is None else "%d мин (лимит %d)" % (age, mx)))
    if fx.get("degraded"):
        parts.append("недоступны: %s" % ", ".join(str(x) for x in fx["degraded"][:4]))
    cal = fx.get("calendar") or {}
    unv = [e for e in (cal.get("next") or []) if isinstance(e, dict) and e.get("v") is False]
    if unv or (isinstance(cal.get("blackout"), dict) and cal["blackout"].get("v") is False):
        parts.append("календарь не проверен владельцем")
    if parts:
        return _w("W3", "facts_stale", "; ".join(parts), age_min=age, degraded=len(fx.get("degraded") or []))


def w4(f):
    ts = brain_iface.last_swap_ts(f["root"])
    cd = float(f["cfg"]["model_change_cooldown_min"]) * 60
    if _num(ts) and f["now"] - ts < cd:
        return _w("W4", "recent_swap", "модель или команда live-ролей сменена %d мин назад (порог %d)" % ((f["now"] - ts) / 60, cd / 60), age_min=(f["now"] - ts) / 60)


def w5(f):
    r = f["risk"]
    if r["level"] >= 4:
        d = riskstore.details(r["level"], f["root"])
        rp = d.get("risk_per_trade_frac")
        return _w("W5", "risk_level", "уровень %s «%s»%s" % (r["level"], r["name"], (": риск на сделку %.0f%% капитала" % (rp * 100)) if _num(rp) else ""), level=r["level"], risk_per_trade=rp)


def w6(f):
    st = f["platform"]
    if not st:
        return None
    bad = []
    for n, v in (st.get("services") or {}).items():
        if n in ("exec", "brain", "ctl", "dash"):
            continue
        if v.get("state") in ("failed", "backoff", "unavailable") or (v.get("state") == "running" and v.get("hb_ok") is False):
            bad.append("%s: %s" % (n, v.get("state")))
    sc = (st.get("scanner") or {}).get("age_s")
    if isinstance(sc, (int, float)) and sc > 140:
        bad.append("сканер молчит %d с" % sc)
    if bad:
        return _w("W6", "degraded_services", "; ".join(bad[:4]), count=len(bad))


def w7(f):
    lc = (f["lifecycle_cfg"].get("carry") or {}) if isinstance(f["lifecycle_cfg"], dict) else {}
    parts = []
    if lc.get("mode") == "agent":
        parts.append("перенос через сутки включён в режиме agent")
    snap = f["snapshot"]
    n = len((snap or {}).get("positions") or []) if f["health"].get("data", {}).get("profile") == "trade" else 0
    if n:
        parts.append("перенесено или открыто позиций: %d" % n)
    if parts:
        return _w("W7", "carry", "; ".join(parts), positions=n)


def w8(f):
    bl = ((f["facts"] or {}).get("calendar") or {}).get("blackout") if isinstance(f["facts"], dict) else None
    if isinstance(bl, dict) and _num(bl.get("in_min")) and bl["in_min"] <= BLOCK_BEFORE_MIN:
        where = "окно уже идёт" if bl["in_min"] <= 0 else "через %d мин" % bl["in_min"]
        return _w("W8", "blackout", "%s: %s" % (bl.get("name") or "событие", where), in_min=bl["in_min"])


def w9(f):
    st = f["args"].get("session") or "day"
    n = len([t for t in f["trades"] if t["mode"] == "live" and (st == "day" or t["stype"] == st)])
    need = int(f["cfg"]["min_session_trades"])
    if n < need:
        return _w("W9", "session_stats", "%s: сделок в журнале %d из %d" % (st, n, need), trades=n, need=need)


SOFT = (w1, w2, w3, w4, w5, w6, w7, w8, w9)


def soft(f, args=None):
    return [x for x in (w(f) for w in SOFT) if x]


def evaluate(root, args, now, cfg=None):
    """-> {"gates":[...],"warnings":[...],"ok":bool (все H), "facts_ts"}. Для не-live режимов: только H1 (и список W пуст)."""
    cfg = cfg or ctlstate.load_cfg(root)
    f = collect(root, now, cfg, args)
    mode = args.get("mode")
    if mode == "live":
        gates, warns = hard(f), soft(f)
    else:
        gates, warns = [h1(f)] if mode in ("paper", "testnet") else [], []
    return {"gates": gates, "warnings": warns, "ok": all(g["ok"] for g in gates), "ts": now}


def monitor(root, now, cfg=None):
    """Проверки авто-понижения live (H1-H6) -> список провалов [{"id","msg"}]."""
    cfg = cfg or ctlstate.load_cfg(root)
    f = collect(root, now, cfg, {"mode": "live"})
    bad = [g for g in (h1(f), h2(f), h3(f), h4(f), h5(f), h6(f)) if not g["ok"]]
    is_target, h, _d = _exec_state(f)
    if not is_target and not any(g["id"] == "H3" for g in bad):
        bad.append(_g("H3", "Связь с биржей и исполнителем", False, "исполнитель не в режиме live"))
    return bad
