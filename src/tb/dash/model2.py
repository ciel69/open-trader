"""Модель дашборда v2 (SPEC_V2, 04_paper_dash.md п. 4.3): планы, позиции под планом, решения, события, досье, эксперименты.

ТОЛЬКО ЧТЕНИЕ файлов (форматы в 06_contracts.md и 01..03): state/plans/PL-*/{spec,runtime}.json + revs.jsonl, state/decisions.jsonl,
state/bus/events.jsonl + cursors/*.json, state/briefs/B-*.json, state/KILL, runtime/exec_{health,snapshot}.json, runtime/levels.json,
config/experiments.json. Модули исполнителя, шины, планов и биржевой код НЕ импортируются: формат читается напрямую и терпимо
(нет файла, пустой каталог, битая строка, чужие поля, огромные числа дают пустое состояние или пропуск, а не ошибку).
Весь свободный текст из файлов обрезается и очищается от управляющих символов; страница вставляет его только через textContent.
"""
import json
import math
import os
import re
import time

from tb import paths
from tb.dash import model

NEAR_PCT = 0.3                  # как idea_near: расстояние до уровня не больше этого процента = «ждёт входа»
CLOSED_WINDOW_S = 86400.0       # терминальные планы на доске только за последние сутки
PLANS_LIMIT = 200               # карточек на доске (остальные считаются в счётчиках)
DEC_TAIL = 3000                 # сколько последних строк журнала решений разбираем
DEC_LIMIT = 200                 # потолок строк в одном ответе
EV_TAIL = 600
HB_ALIVE_S = 10.0               # heartbeat исполнителя: живой / запаздывает / не отвечает
HB_DEAD_S = 30.0
RECON_WARN_S = 120.0
CONN_RECOVER_S = 60.0       # после события connectivity state=up чип «Связь» остаётся жёлтым («восстановлена») не дольше этого окна, затем зелёный
UNACKED_WARN_S = 600.0
MIN_N = 30                      # порог «достаточно данных» в статистике экспериментов (как exp stats)
TERMINAL = ("closed", "cancelled", "expired", "invalidated", "rejected")
COLUMNS = ("watching", "waiting", "in_trade", "closed")
RE_PLAN = re.compile(r"^PL-[A-Za-z0-9._-]{1,48}$")
RE_BRIEF = re.compile(r"^B-[A-Za-z0-9._-]{1,48}$")
RE_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")
TF_RU = {"1m": "1м", "3m": "3м", "5m": "5м", "15m": "15м", "1h": "1ч", "60m": "1ч"}
_PUBLIC_KEYS = ("spec", "rt")


# ---------------------------------------------------------------- мелкие помощники
def _s(x, n=280):
    """Текст из файла -> безопасная короткая строка (без управляющих символов); не строки превращаются в пусто."""
    if x is None:
        return ""
    if not isinstance(x, str):
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return str(x)[:n]
        return ""
    s = RE_CTRL.sub(" ", x).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _n(x):
    return model.num(x)


def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, list) else []


def _tf(tf):
    return TF_RU.get(tf, _s(tf, 8) or "?")


def _tf_s(tf):
    m = re.match(r"^(\d{1,3})([mh])$", str(tf or ""))
    if not m:
        return None
    return int(m.group(1)) * (3600 if m.group(2) == "h" else 60)


def _pf(x):
    """Цена текстом с значащими цифрами (как на странице)."""
    return model.fmt_price(x) if _n(x) is not None else "–"


def _g(x):
    """Число без хвоста «.0» (5.0 -> 5, 1.8 -> 1.8); не число -> «?»."""
    v = _n(x)
    return "?" if v is None else ("%g" % v)


def _rn(x, d=2):
    v = _n(x)
    return None if v is None else round(v, d)


def _epoch(x):
    """epoch или ISO-строка -> epoch либо None."""
    v = _n(x)
    return v if v is not None else model.parse_ts(x)


# ---------------------------------------------------------------- человекочитаемые условия
def _lvl_side(c):
    return "выше" if c.get("dir") in ("above", "up", "long") else "ниже"


def describe_cond(c, depth=0):
    """Условие плана (01_plan, словарь условий) -> короткая строка по-русски."""
    if not isinstance(c, dict) or depth > 4:
        return "?"
    for key, word in (("all", " и "), ("any", " или ")):
        if isinstance(c.get(key), list):
            parts = [describe_cond(x, depth + 1) for x in c[key][:8]]
            txt = word.join(parts)
            return txt if depth == 0 else "(" + txt + ")"
    if "not" in c:
        return "не " + describe_cond(c.get("not"), depth + 1)
    k = c.get("kind")
    if k == "seq":
        steps = [describe_cond(x, depth + 1) for x in _l(c.get("steps"))[:6]]
        w = _n(c.get("within_min"))
        return "по порядку: " + "; ".join("%d) %s" % (i + 1, t) for i, t in enumerate(steps)) + (" в течение %d мин" % w if w else "")
    if k == "price_beyond":
        return "цена %s %s" % (_lvl_side(c), _pf(c.get("level")))
    if k == "candle_close":
        n = int(_n(c.get("n")) or 1)
        return "%s закрытие %s %s%s" % (_tf(c.get("tf")), _lvl_side(c), _pf(c.get("level")), " (%d подряд)" % n if n > 1 else "")
    if k == "candle_reject":
        wk = _n(c.get("wick_min_pct"))
        return "%s прокол %s и закрытие обратно%s" % (_tf(c.get("tf")), _pf(c.get("level")), " (тень ≥ %s%%)" % _g(wk) if wk else "")
    if k == "candle_body":
        return "%s свеча %s с телом ≥ %s%%" % (_tf(c.get("tf")), "вверх" if c.get("dir") == "up" else "вниз", _g(c.get("min_body_pct") or 0))
    if k == "volume_spike":
        return "объём %s ≥ x%s медианы" % (_tf(c.get("tf")), _g(c.get("mult")))
    if k == "price_in_zone":
        return "цена в зоне %s…%s" % (_pf(c.get("low")), _pf(c.get("high")))
    if k == "spread_bps":
        return "спред ≤ %s бп" % _g(c.get("lte"))
    if k == "pnl_r":
        for op, sign in (("gte", "≥"), ("gt", ">"), ("lte", "≤"), ("lt", "<")):
            if _n(c.get(op)) is not None:
                return "результат %s %s%sR" % (sign, "+" if c[op] > 0 else "", _rn(c[op]))
        return "результат в R"
    if k == "elapsed":
        return "прошло ≥ %s мин" % _g(c.get("min"))
    if k == "target_filled":
        return "цель %s исполнена" % _s(c.get("target"), 4)
    if k == "near_target":
        return "пройдено %d%% пути до %s" % (round((_n(c.get("frac")) or 0) * 100), _s(c.get("target"), 4))
    return _s(k, 24) or "условие"


def describe_do(do):
    """Действие правила сопровождения -> строка."""
    do = _d(do)
    a = do.get("action")
    if a == "move_stop":
        to = do.get("to")
        if to == "breakeven":
            return "стоп в безубыток"
        if to == "lock_r":
            return "стоп фиксирует +%sR" % _rn(do.get("r"))
        if to == "price":
            return "стоп на %s" % _pf(do.get("price"))
        if to == "entry_pct":
            return "стоп на %s%% от входа" % _rn(do.get("pct"))
        return "подтянуть стоп"
    if a == "trail":
        m = do.get("mode")
        extra = {"candle_extreme": "по экстремуму %s свечей %s" % (int(_n(do.get("n")) or 1), _tf(do.get("tf"))),
                 "atr": "по ATR x%s (%s)" % (_n(do.get("mult")) or "?", _tf(do.get("tf"))),
                 "percent": "на %s%% от лучшей цены" % _rn(do.get("pct")),
                 "step_r": "шагом %sR" % _rn(do.get("step_r"))}.get(m, "")
        return "трейл " + extra
    if a == "partial_close":
        fr = _n(do.get("frac_of_remaining"))
        return "закрыть %d%% остатка" % round(fr * 100) if fr else "закрыть часть %d%% исходного" % round((_n(do.get("frac_of_initial")) or 0) * 100)
    if a == "close":
        return "закрыть %d%% (%s)" % (round((_n(do.get("frac")) if _n(do.get("frac")) is not None else 1.0) * 100), _s(do.get("reason"), 20) or "правило")
    if a == "move_target":
        return "цель %s → %s" % (_s(do.get("target"), 4), _pf(do.get("price")))
    if a == "add":
        return "добор %s%% риска" % round((_n(do.get("risk_frac")) or 0) * 100)
    if a == "notify":
        return "разбудить мозг (%s)" % _s(do.get("code"), 16)
    return _s(a, 20) or "действие"


def _leaves(c, out=None, depth=0):
    """Листья условия в порядке обхода в глубину; индекс листа = номер в latched ('c0', 'c1', ...)."""
    out = [] if out is None else out
    if not isinstance(c, dict) or depth > 5:
        return out
    for key in ("all", "any"):
        if isinstance(c.get(key), list):
            for x in c[key][:8]:
                _leaves(x, out, depth + 1)
            return out
    if "not" in c:
        return _leaves(c.get("not"), out, depth + 1)
    if c.get("kind") == "seq":
        for x in _l(c.get("steps"))[:6]:
            _leaves(x, out, depth + 1)
        return out
    out.append(c)
    return out


def _leaf_state(c, price, now, met_flag):
    """Состояние одного листа условия по текущей цене: {met, text, dist_pct, level, eta_s, tf}."""
    k = c.get("kind")
    out = {"text": describe_cond(c), "met": met_flag, "dist_pct": None, "level": None, "eta_s": None, "tf": None, "kind": k, "dir": None, "beyond": None}
    lvl = _n(c.get("level"))
    if lvl is not None and price and c.get("dir") in ("above", "below"):       # цена уже по нужную сторону уровня (для свечных условий этого мало: нужно закрытие свечи)
        out["dir"] = c["dir"]
        out["beyond"] = price >= lvl if c["dir"] == "above" else price <= lvl
    if k == "price_in_zone":
        lo, hi = _n(c.get("low")), _n(c.get("high"))
        if lo is not None and hi is not None and price:
            out["met"] = lo <= price <= hi
            out["dist_pct"] = 0.0 if out["met"] else min(abs(price - lo), abs(price - hi)) / price * 100.0
            out["level"] = lo if abs(price - lo) < abs(price - hi) else hi
        return out
    if lvl is not None and price:
        out["level"] = lvl
        out["dist_pct"] = abs(lvl - price) / price * 100.0
        if k == "price_beyond":
            out["met"] = price >= lvl if c.get("dir") == "above" else price <= lvl
            if out["met"]:
                out["dist_pct"] = 0.0
    if k in ("candle_close", "candle_reject", "candle_body", "volume_spike"):
        step = _tf_s(c.get("tf"))
        if step:
            out["tf"] = _tf(c.get("tf"))
            out["eta_s"] = int(step - (now % step))
    return out


def trigger_info(spec, rt, price, now):
    """Ближайший триггер входа: что именно не хватает и расстояние. Возвращает dict для карточки плана."""
    entry = _d(spec.get("entry"))
    ent_rt = _d(rt.get("entry"))
    how = _d(entry.get("how"))
    when = entry.get("when")
    cs = _d(ent_rt.get("cond_state"))
    latched = _d(cs.get("latched"))
    seq_done = int(_n(cs.get("seq_step")) or 0)
    info = {"has_when": isinstance(when, dict) and bool(when), "text": "", "dist_pct": None, "level": None, "eta_s": None, "eta_tf": None,
            "missing": [], "met_n": 0, "leaf_n": 0, "price": price, "seq": None, "limit_px": None}
    lim = _n(how.get("price")) if how.get("type") == "limit" else None
    info["limit_px"] = lim
    if info["has_when"]:
        leaves = _leaves(when)
        info["leaf_n"] = len(leaves)
        is_seq = isinstance(when, dict) and when.get("kind") == "seq"
        sts = []
        for i, lf in enumerate(leaves):
            flag = latched.get("c%d" % i)
            if is_seq:
                flag = True if i < seq_done else (False if flag is None else flag)
            sts.append(_leaf_state(lf, price, now, flag if isinstance(flag, bool) else None))
        info["met_n"] = sum(1 for s in sts if s["met"] is True)
        if is_seq:
            info["seq"] = {"done": min(seq_done, len(sts)), "total": len(sts)}
        pending = [s for s in sts if s["met"] is not True]
        if is_seq and seq_done < len(sts):
            pending = [sts[seq_done]]       # в seq важен только текущий шаг
        with_d = [s for s in pending if s["dist_pct"] is not None]
        best = min(with_d, key=lambda s: s["dist_pct"]) if with_d else (pending[0] if pending else None)
        info["missing"] = [s["text"] for s in pending][:6]
        if best:
            info["dist_pct"] = _rn(best["dist_pct"], 3)
            info["level"] = best["level"]
            info["eta_s"], info["eta_tf"] = best["eta_s"], best["tf"]
            info["text"] = best["text"]
            info["kind"], info["dir"], info["beyond"] = best["kind"], best["dir"], best["beyond"]
        elif sts:
            info["text"] = "условия выполнены, идёт попытка входа"
    elif lim is not None:
        info["text"] = "лимит %s" % _pf(lim)
        info["level"] = lim
        if price:
            info["dist_pct"] = _rn(abs(lim - price) / price * 100.0, 3)
    else:
        info["text"] = "вход сразу по рынку"
    if lim is not None and ent_rt.get("state") == "resting":     # лимит уже стоит на бирже: ближайший триггер это он
        info["text"] = "лимит стоит на %s" % _pf(lim)
        info["level"] = lim
        info["eta_s"] = info["eta_tf"] = None
        info["dist_pct"] = _rn(abs(lim - price) / price * 100.0, 3) if price else None
    return info


def entry_text(spec):
    entry = _d(spec.get("entry"))
    how = _d(entry.get("how"))
    t = how.get("type")
    if isinstance(entry.get("when"), dict) and entry.get("when"):
        base = describe_cond(entry["when"])
    elif t == "limit":
        base = "лимит сразу"
    elif t == "adopt":
        return "взята существующая позиция"
    else:
        base = "по рынку сразу"
    how_t = {"market": "рынок", "limit": "лимит %s%s" % (_pf(how.get("price")), " post-only" if how.get("post_only") else ""), "adopt": "adopt"}.get(t, "")
    return base + (" → " + how_t if how_t and entry.get("when") else "")


# ---------------------------------------------------------------- чтение источников
def _read_levels(root):
    d = model.read_json(paths.levels_file(root=root))
    d = _d(d)
    d = _d(d.get("symbols")) if isinstance(d.get("symbols"), dict) else d
    return {k: v for k, v in d.items() if isinstance(v, dict) and re.match(r"^[A-Z0-9]{2,20}$", str(k))}


def read_health(root, now):
    h = model.read_json(paths.exec_health_file(root=root))
    if not isinstance(h, dict):
        return None
    ts = _epoch(h.get("ts_epoch")) or _epoch(h.get("ts"))
    h = dict(h)
    h["_age_s"] = None if ts is None else max(0.0, now - ts)
    return h


def read_snapshot(root, now):
    sn = model.read_json(paths.exec_snapshot_file(root=root))
    if not isinstance(sn, dict):
        return None
    ts = _epoch(sn.get("ts_epoch")) or _epoch(sn.get("ts"))
    sn = dict(sn)
    sn["_age_s"] = None if ts is None else max(0.0, now - ts)
    return sn


def read_kill(root, now):
    """state/KILL: файл-флаг. Содержимое (JSON или текст) необязательно. -> None или {flatten, reason, age_s}."""
    p = paths.kill_file(root=root)
    try:
        st = os.stat(p)
    except OSError:
        return None
    out = {"flatten": False, "reason": "", "age_s": max(0.0, now - st.st_mtime)}
    d = model.read_json(p)
    if isinstance(d, dict):
        out["flatten"] = bool(d.get("flatten"))
        out["reason"] = _s(d.get("reason"), 120)
        ts = _epoch(d.get("ts"))
        if ts:
            out["age_s"] = max(0.0, now - ts)
    else:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                out["reason"] = _s(f.read(200), 120)
        except OSError:
            pass
    return out


def _plan_dirs(root):
    base = paths.plans(root=root)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted((n for n in names if RE_PLAN.match(n) and os.path.isdir(os.path.join(base, n))), reverse=True)


def read_plans(root):
    """-> (список {'id','spec','rt','mtime'}, число битых папок). Битый spec.json = папка пропускается и считается."""
    out, broken = [], 0
    for name in _plan_dirs(root):
        d = os.path.join(paths.plans(root=root), name)
        spec = model.read_json(os.path.join(d, "spec.json"))
        if not isinstance(spec, dict):
            broken += 1
            continue
        rtp = os.path.join(d, "runtime.json")
        rt = model.read_json(rtp)
        st = model._stamp(rtp) or model._stamp(os.path.join(d, "spec.json"))
        out.append({"id": name, "spec": spec, "rt": rt if isinstance(rt, dict) else {}, "mtime": st[0] if st else None})
    return out, broken


def read_decisions(root, tail=DEC_TAIL):
    return [r for r in model.read_jsonl(paths.decisions_file(root=root), tail=tail) if isinstance(r.get("seq"), (int, float))]


def read_events(root, tail=EV_TAIL):
    return [r for r in model.read_jsonl(paths.bus_events(root=root), tail=tail) if isinstance(r.get("seq"), (int, float))]


def read_cursors(root):
    d = paths.bus("cursors", root=root)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and re.match(r"^[A-Za-z0-9_-]{1,24}\.json$", n):
            c = model.read_json(os.path.join(d, n))
            if isinstance(c, dict):
                out[n[:-5]] = {"delivered": int(_n(c.get("delivered")) or 0), "acked": int(_n(c.get("acked")) or 0)}
    return out


# ---------------------------------------------------------------- плановые карточки
def _price_for(mkt, sp, sym, levels, snap):
    pr = model.resolve_price(mkt, sp, sym, "bybit")
    if pr.get("price"):
        return pr["price"], pr.get("stale"), pr.get("age_s"), pr.get("src")
    lv = _n(_d(levels.get(sym)).get("price"))
    if lv:
        return lv, True, None, "levels.json"
    for p in _l(_d(snap).get("positions")):
        if isinstance(p, dict) and p.get("sym") == sym and _n(p.get("mark")):
            return _n(p["mark"]), (snap.get("_age_s") or 0) > 30, snap.get("_age_s"), "exec_snapshot"
    return None, None, None, None


def _ttl_left(spec, rt, now):
    ts = _d(rt.get("ts"))
    base = _epoch(ts.get("armed")) or _epoch(ts.get("created")) or _epoch(spec.get("armed_ts")) or _epoch(spec.get("created_ts"))
    ttl = _n(_d(_d(spec.get("entry")).get("valid")).get("ttl_min")) or _n(_d(spec.get("lifecycle")).get("ttl_min"))
    if base is None or not ttl:
        return None
    return base + ttl * 60.0 - now


def _classify(status, trig, rt, price, ttl_left):
    if status in ("entered", "managing"):
        return "in_trade"
    if status in TERMINAL:
        return "closed"
    ent = _d(rt.get("entry"))
    if status == "armed":
        near = trig["dist_pct"] is not None and trig["dist_pct"] <= NEAR_PCT
        partial = trig["has_when"] and (trig["met_n"] > 0 and trig["met_n"] < trig["leaf_n"] or (trig["seq"] or {}).get("done", 0) > 0)
        if ent.get("state") in ("placing", "resting") or near or partial:
            return "waiting"
    return "watching"


def _prev_stop(rt):
    return _n(_d(rt.get("pos")).get("cur_stop"))


def _chase_cap(root):
    """Допуск входа вдогонку из config/exec_limits.json (entry.max_chase_bps, б.п.); нет файла или сбой -> умолчание 15."""
    try:
        from tb.core import exec_cfg
        return float(exec_cfg.load(root)["entry"]["max_chase_bps"])
    except Exception:
        return 15.0


def plan_card(item, mkt, sp, levels, snap, now, chase_cap=15.0):
    spec, rt = item["spec"], item["rt"]
    sym = _s(spec.get("symbol"), 20)
    side = spec.get("side") if spec.get("side") in ("long", "short") else None
    status = _s(rt.get("status") or spec.get("status"), 16) or "draft"
    price, stale, page, psrc = _price_for(mkt, sp, sym, levels, snap)
    trig = trigger_info(spec, rt, price, now) if status in ("draft", "armed") else {"has_when": False, "text": "", "dist_pct": None, "level": None, "eta_s": None,
                                                                                   "eta_tf": None, "missing": [], "met_n": 0, "leaf_n": 0, "seq": None, "limit_px": None, "price": price}
    ttl_left = _ttl_left(spec, rt, now) if status in ("draft", "armed") else None
    col = _classify(status, trig, rt, price, ttl_left)
    ts = _d(rt.get("ts"))
    closed_ts = _epoch(ts.get("closed")) or (item["mtime"] if status in TERMINAL else None)
    gp = _d(spec.get("guard_preview"))
    tags = _d(spec.get("tags"))
    src = _d(spec.get("source"))
    warns = []
    for w in _l(spec.get("warnings")) + _l(rt.get("warnings")):
        code = w.get("code") if isinstance(w, dict) else w
        if isinstance(code, str) and re.match(r"^[EW]_[A-Z0-9_]{2,32}$", code) and code not in warns:
            warns.append(code)
    tgt_rt = _d(rt.get("targets"))
    targets = []
    for t in _l(spec.get("targets"))[:4]:
        if isinstance(t, dict):
            r = _d(tgt_rt.get(t.get("id")))
            targets.append({"id": _s(t.get("id"), 4), "price": _n(t.get("price")), "frac": _n(t.get("frac")), "state": _s(r.get("state"), 12) or None})
    close = _d(rt.get("close"))
    realized = _d(rt.get("realized"))
    hold = rt.get("hold")
    ent = _d(rt.get("entry"))
    chase_bps = chase_max = None
    if status == "armed" and ent.get("blocked_why") == "CHASE" and price and side:             # вход заблокирован: цена дальше допуска от уровня
        ref = _n(_d(spec.get("entry")).get("ref_price")) or _n(ent.get("ref_px")) or trig.get("level")
        mine = _n(_d(_d(spec.get("entry")).get("valid")).get("max_chase_bps"))
        chase_max = min(chase_cap, mine) if mine is not None else chase_cap
        if ref:
            chase_bps = _rn((price - ref) / ref * 1e4 * (1 if side == "long" else -1), 1)
    return {
        "id": item["id"], "sym": sym, "side": side, "mode": spec.get("mode") if spec.get("mode") in ("live", "paper") else ("paper" if rt.get("paper") else "live"),
        "status": status, "col": col, "rev": int(_n(spec.get("rev")) or 1), "rev_applied": _n(rt.get("rev_applied")),
        "exp": _s(tags.get("experiment"), 40) or None, "var": _s(tags.get("variant"), 40) or None, "setup": _s(tags.get("setup"), 40) or None,
        "hyp": _s(tags.get("hypothesis"), 40) or None, "src": _s(src.get("kind"), 12) or None, "parent": _s(src.get("parent_plan"), 40) or None,
        "thesis": _s(spec.get("thesis"), 280), "session_type": _s(spec.get("session_type"), 12) or None,
        "entry_text": entry_text(spec), "trigger": trig, "price": price, "price_stale": bool(stale) if price else None, "price_age_s": _rn(page, 1),
        "ttl_left_s": _rn(ttl_left, 0), "hold": _s(hold, 40) if hold else None, "hold_reason": _s(rt.get("hold_reason"), 80) or None,
        "entry_state": _s(ent.get("state"), 12) or None, "blocked_why": _s(ent.get("blocked_why"), 40) or None, "chase_bps": chase_bps, "chase_max_bps": chase_max,
        "stop": _n(_d(spec.get("stop")).get("price")), "targets": targets,
        "risk_usdt": _rn(gp.get("risk_usdt"), 3), "net_rr": _rn(gp.get("net_rr"), 2), "leverage": _n(gp.get("leverage")), "margin": _rn(gp.get("margin"), 2),
        "warnings": warns[:6], "ts_created": _epoch(ts.get("created")) or _epoch(spec.get("created_ts")), "ts_armed": _epoch(ts.get("armed")) or _epoch(spec.get("armed_ts")),
        "ts_entered": _epoch(ts.get("entered")), "ts_closed": closed_ts,
        "close_reason": _s(close.get("reason"), 24) or None, "close_pnl": _rn(close.get("pnl") if close.get("pnl") is not None else realized.get("pnl"), 4),
        "created_by": _s(spec.get("created_by"), 16) or None, "prereg": _s(spec.get("prereg_hash"), 16) or None,
    }


def _fmt_rule_need(c, ctx):
    """Что ждёт правило: пары (текст, нужное R или None). ctx: {r_now, elapsed_min, targets}."""
    if not isinstance(c, dict):
        return "", None
    if c.get("kind") == "pnl_r":
        for op, sign in (("gte", "≥"), ("gt", ">")):
            need = _n(c.get(op))
            if need is not None:
                now_r = ctx.get("r_now")
                return "нужно %s%s%sR, сейчас %s" % (sign, "+" if need >= 0 else "", _rn(need, 2), ("%s%sR" % ("+" if now_r >= 0 else "", _rn(now_r, 2))) if now_r is not None else "–"), need
    if c.get("kind") == "elapsed":
        need = _n(c.get("min"))
        if need is not None:
            em = ctx.get("elapsed_min")
            return "нужно ≥ %d мин, прошло %s" % (need, ("%d" % em) if em is not None else "–"), None
    if c.get("kind") == "target_filled":
        return "ждёт исполнения %s" % _s(c.get("target"), 4), None
    return describe_cond(c), None


def _stop_candidate(do, pos, side):
    """Куда сдвинется стоп по правилу move_stop (абсолютная цена) или None."""
    avg, r_unit = pos.get("avg"), pos.get("r_unit")
    sgn = 1 if side == "long" else -1
    to = do.get("to")
    if not avg:
        return None
    if to == "breakeven":
        off = _n(do.get("offset_bps")) or 0.0
        return avg * (1 + sgn * off / 10000.0)
    if to == "lock_r" and r_unit and _n(do.get("r")) is not None:
        return avg + sgn * do["r"] * r_unit
    if to == "price" and _n(do.get("price")):
        return _n(do.get("price"))
    if to == "entry_pct" and _n(do.get("pct")) is not None:
        return avg * (1 + sgn * do["pct"] / 100.0)
    return None


def decision_row(r, plan_modes=None):
    """Запись decisions.jsonl -> безопасная строка ленты. Текст мозга/правила обрезается; args/res/basis оставляют только числа и короткие строки."""
    args, basis, res = _d(r.get("args")), _d(r.get("basis")), _d(r.get("res"))

    def flat(dct):
        o = {}
        for k, v in list(dct.items())[:16]:
            if any(w in str(k).lower() for w in model._SECRET_WORDS):    # ключ похож на секрет: значение не попадает ни в поля, ни в текст действия
                continue
            if isinstance(v, bool) or v is None:
                o[_s(k, 24)] = v
            elif isinstance(v, (int, float)):
                o[_s(k, 24)] = _n(v)
            elif isinstance(v, str):
                o[_s(k, 24)] = _s(v, 80)
            elif isinstance(v, (list, dict)):
                o[_s(k, 24)] = _s(json.dumps(v, ensure_ascii=False), 120)
        return o
    what = _s(r.get("what"), 32)
    kind = _s(r.get("kind"), 12)
    code = _s(res.get("code"), 40) or None
    ok = res.get("ok") if isinstance(res.get("ok"), bool) else None
    idle = bool(code and code.startswith("IDLE_"))          # служебное «ждёт» (IDLE_NO_PLAN): не отказ и не пропуск, нейтральный статус
    if code == "DOSSIER" and str(r.get("why") or "").startswith("NO_PLAN"):
        idle = True          # записи старого мозга (до IDLE_NO_PLAN): «нет планов и позиций» тоже не отказ Gate
    denied = not idle and ((ok is False) or kind == "reject" or bool(code and (code.startswith("DENY") or code.startswith("E_"))))
    noop = kind == "skip" and not denied and not idle
    prev, to = _n(args.get("prev")), _n(args.get("to"))
    if what in ("move_stop", "set_stop", "trail_step", "stop") and to is not None:
        act = "стоп %s → %s" % (_pf(prev) if prev is not None else "?", _pf(to))
    elif what in ("place_entry", "fire_entry", "entry"):
        act = "вход %s%s%s" % (args.get("qty") if _n(args.get("qty")) is not None else "", " @ " + _pf(args.get("px")) if _n(args.get("px")) else "", " " + _s(args.get("type"), 8) if args.get("type") else "")
    elif what in ("place_target", "move_target"):
        act = "цель %s %s" % (_s(args.get("target"), 4), _pf(args.get("price") if args.get("price") is not None else args.get("px")))
    elif what in ("partial_close", "close", "close_market"):
        act = "%s%s" % ("частичное закрытие" if what == "partial_close" else "закрытие", " " + str(_n(args.get("qty"))) if _n(args.get("qty")) is not None else (" " + str(round(_n(args.get("frac")) * 100)) + "%" if _n(args.get("frac")) is not None else ""))
    else:
        act = what
        if args:
            act += " " + " ".join("%s=%s" % (_s(k, 16), v) for k, v in list(flat(args).items())[:4])    # flat() уже без секретных ключей
    pid = _s(r.get("plan"), 48) or None
    return {
        "seq": int(r["seq"]), "ts": _s(r.get("ts"), 24), "t": _epoch(r.get("t")) or _epoch(r.get("ts")), "actor": _s(r.get("actor"), 16), "plan": pid,
        "rev": int(_n(r.get("rev"))) if _n(r.get("rev")) is not None else None, "kind": kind, "what": what, "action": act.strip(), "why": _s(r.get("why"), 240),
        "args": flat(args), "basis": flat(basis), "res": flat(res), "ok": ok, "code": code, "denied": denied, "noop": noop, "idle": idle,
        "px": _n(basis.get("px")), "r": _rn(basis.get("r"), 2), "brief_id": _s(basis.get("brief_id"), 48) or None,
        "link_id": _s(res.get("link_id"), 40) or None, "action_id": _s(r.get("action_id"), 32) or None, "exch_ms": _n(res.get("exch_ms")),
        "h": _s(r.get("h"), 16), "mode": (plan_modes or {}).get(pid) if pid else None,
    }


def build_position(card, item, snap_pos, snap_age, decs, now):
    """Позиция под планом: правда биржи (exec_snapshot) поверх runtime, лесенка цен, правила сопровождения, история действий."""
    spec, rt = item["spec"], item["rt"]
    pos, side = _d(rt.get("pos")), card["side"]
    sgn = 1 if side == "long" else -1
    snap_pos = _d(snap_pos)
    avg = _n(snap_pos.get("avg")) or _n(pos.get("avg"))
    qty = _n(snap_pos.get("size")) or _n(pos.get("qty"))
    stop_ex = _n(snap_pos.get("sl")) if (_n(snap_pos.get("sl")) or 0) > 0 else None
    cur_stop = stop_ex if stop_ex is not None else _prev_stop(rt)
    init_stop = _n(pos.get("initial_stop")) or _n(_d(spec.get("stop")).get("price"))
    r_unit = _n(pos.get("r_unit")) or (abs(avg - init_stop) if avg and init_stop else None)
    price = _n(snap_pos.get("mark")) or card["price"]
    r_now = sgn * (price - avg) / r_unit if (price and avg and r_unit) else None
    best, worst = _n(pos.get("best_px")), _n(pos.get("worst_px"))
    mfe_r = sgn * (best - avg) / r_unit if (best and avg and r_unit) else None
    mae_r = sgn * (worst - avg) / r_unit if (worst and avg and r_unit) else None
    ts_ent = _epoch(_d(rt.get("ts")).get("entered"))
    held = (now - ts_ent) if ts_ent else None
    mh = _n(_d(spec.get("lifecycle")).get("max_hold_min"))
    hold_left = (ts_ent + mh * 60.0 - now) if (ts_ent and mh) else None
    ctx = {"r_now": r_now, "elapsed_min": (held / 60.0) if held is not None else None}
    pdecs = [d for d in decs if d["plan"] == card["id"]]
    rules_rt = _d(rt.get("rules"))
    rules, nxt = [], None
    for ru in _l(spec.get("manage"))[:12]:
        if not isinstance(ru, dict):
            continue
        rid = _s(ru.get("id"), 20)
        st = _d(rules_rt.get(rid))
        state = _s(st.get("state"), 10) or "waiting"
        need_txt, need_r = _fmt_rule_need(ru.get("when"), ctx)
        do = _d(ru.get("do"))
        rec = {"id": rid, "state": state if state in ("waiting", "done", "skipped") else "waiting", "do": describe_do(do), "fired": int(_n(st.get("fired")) or 0),
               "need": need_txt, "detail": None, "code": None, "ts": _epoch(st.get("last_ts")), "once": bool(ru.get("once"))}
        mine = [d for d in pdecs if d["kind"] in ("rule", "skip") and d["why"].endswith(rid)]
        if mine:
            last = mine[-1]
            rec["ts"] = rec["ts"] or last["t"]
            if rec["state"] == "skipped" or last["kind"] == "skip":
                rec["code"] = last["code"]
            if last["kind"] == "rule" and _n(last["args"].get("prev")) is not None and _n(last["args"].get("to")) is not None:
                rec["detail"] = "%s → %s" % (_pf(last["args"]["prev"]), _pf(last["args"]["to"]))
        if rec["state"] == "waiting" and do.get("action") == "move_stop":
            cand = _stop_candidate(do, {"avg": avg, "r_unit": r_unit}, side)
            if cand is not None and cur_stop is not None and sgn * (cand - cur_stop) > 0:
                score = need_r if need_r is not None else 99.0
                if nxt is None or score < nxt["score"]:
                    nxt = {"to": cand, "rule": rid, "why": need_txt or describe_do(do), "score": score}
        rules.append(rec)
    if nxt:
        nxt.pop("score", None)
    tg_rt = _d(rt.get("targets"))
    targets = []
    for t in card["targets"]:
        r = _d(tg_rt.get(t["id"]))
        targets.append({"id": t["id"], "price": _n(r.get("price")) or t["price"], "state": _s(r.get("state"), 10) or t["state"] or "pending", "qty": _n(r.get("qty")), "filled": _n(r.get("filled_qty"))})
    realized = _d(rt.get("realized"))
    return {
        "plan": card["id"], "sym": card["sym"], "side": side, "mode": card["mode"], "status": card["status"], "journal_id": _s(pos.get("journal_id"), 48) or None,
        "qty": qty, "avg": avg, "price": price, "price_stale": card["price_stale"], "r_now": _rn(r_now, 2), "mae_r": _rn(mae_r, 2), "mfe_r": _rn(mfe_r, 2),
        "cur_stop": cur_stop, "initial_stop": init_stop, "stop_src": "биржа" if stop_ex is not None else "runtime", "next_stop": nxt, "r_unit": r_unit,
        "targets": targets, "rules": rules, "held_s": _rn(held, 0), "hold_left_s": _rn(hold_left, 0), "snap_age_s": _rn(snap_age, 1) if snap_pos else None,
        "upnl": _rn(_n(snap_pos.get("upnl")), 4), "liq": _n(snap_pos.get("liq")) or None, "lev": _n(snap_pos.get("lev")), "margin": _n(snap_pos.get("margin")),
        "realized_pnl": _rn(realized.get("pnl"), 4), "fees": _rn(realized.get("fees"), 4), "hold": card["hold"], "exp": card["exp"], "var": card["var"],
        "thesis": card["thesis"], "history": [{k: d[k] for k in ("seq", "t", "actor", "action", "why", "ok", "code", "denied", "noop", "kind")} for d in pdecs[-10:]][::-1],
    }


# ---------------------------------------------------------------- события и шина
def event_row(e):
    d = _d(e.get("d"))
    dd = {}
    for k, v in list(d.items())[:12]:
        if any(w in str(k).lower() for w in model._SECRET_WORDS):
            continue
        if isinstance(v, bool) or v is None:
            dd[_s(k, 24)] = v
        elif isinstance(v, (int, float)):
            dd[_s(k, 24)] = _n(v)
        elif isinstance(v, str):
            dd[_s(k, 24)] = _s(v, 40)
        elif isinstance(v, list):
            dd[_s(k, 24)] = _s(",".join(str(x) for x in v[:6]), 60)
    return {"seq": int(e["seq"]), "ts": _s(e.get("ts"), 24), "t": _epoch(e.get("ts")), "type": _s(e.get("type"), 24), "pri": _s(e.get("pri"), 6),
            "plan": _s(e.get("plan"), 48) or None, "sym": _s(e.get("sym"), 20) or None, "d": dd}


def backlog_info(events, cursors, now):
    last = events[-1] if events else None
    out = {"last_seq": int(last["seq"]) if last else None, "last_age_s": None, "consumers": {}, "n": 0, "oldest_age_s": None}
    if last:
        ts = _epoch(last.get("ts"))
        out["last_age_s"] = None if ts is None else _rn(max(0.0, now - ts), 1)
    for name, c in cursors.items():
        pend = [e for e in events if e.get("pri") == "wake" and e["seq"] > c["acked"]]
        oldest = None
        if pend:
            ts = _epoch(pend[0].get("ts"))
            oldest = None if ts is None else max(0.0, now - ts)
        out["consumers"][name] = {"acked": c["acked"], "delivered": c["delivered"], "unacked": len(pend), "oldest_age_s": _rn(oldest, 0)}
    if out["consumers"]:
        worst = max(out["consumers"].values(), key=lambda c: (c["unacked"], c["oldest_age_s"] or 0))
        out["n"], out["oldest_age_s"] = worst["unacked"], worst["oldest_age_s"]
    return out


def pauses_info(events, now):
    """Паузы и cooldown: события limit_hit, у которых until_ts ещё впереди."""
    out = {}
    for e in events[-200:]:
        if e.get("type") != "limit_hit":
            continue
        d = _d(e.get("d"))
        until = _epoch(d.get("until_ts"))
        if until and until > now:
            out[(_s(d.get("which"), 24), _s(e.get("sym"), 20))] = {"which": _s(d.get("which"), 24), "sym": _s(e.get("sym"), 20) or None, "left_s": _rn(until - now, 0)}
    return sorted(out.values(), key=lambda p: p["left_s"])


# ---------------------------------------------------------------- верхняя полоса
STRIP_ACTIONS = {      # одна строка действия на замечание полосы состояния (показывается в расшифровке индикатора)
    "exec": "проверить ./tb status и ./tb logs exec; позиции защищены стопами на бирже",
    "mode": "observe меняется только запуском дня в нужном режиме (вкладка «Управление»)",
    "hold": "причина указана выше; обычно проходит сама после сверки, иначе ./tb exec status",
    "net": "ничего делать не нужно, если связь вернулась; иначе проверить интернет и ./tb logs exec",
    "recon": "проверить ./tb exec status; сверку можно запустить ./tb exec reconcile",
    "events": "события ждут подтверждения мозга или владельца: ./tb events",
    "scan": "сервис scan: ./tb status; ./tb up --profile paper --detach",
    "kill": "снять в терминале: ./tb exec resume",
}


def build_strip(root, now, health, kill, backlog, scan, sess_view, plans_counts, events=None, recover_s=CONN_RECOVER_S):
    items = []

    def add(id_, label, level, value, text):
        items.append({"id": id_, "label": label, "level": level, "value": value, "text": text, "action": STRIP_ACTIONS.get(id_, "") if level in ("warn", "bad") else ""})
    ex = {"present": health is not None}
    if health is None:
        add("exec", "Исполнитель", "off", "нет данных", "runtime/exec_health.json не найден: исполнитель v2 не запущен (планы не исполняются, торговля идёт прежним путём)")
    else:
        age = health["_age_s"]
        env, prof = _s(health.get("env"), 12), _s(health.get("profile"), 12)
        observe = bool(health.get("observe"))
        mode = "observe" if observe else (prof or env or "?")
        ex.update({"age_s": _rn(age, 1), "env": env, "profile": prof, "mode": mode, "observe": observe, "pid": int(_n(health.get("pid")) or 0) or None,
                   "version": _s(health.get("version"), 16), "uptime_s": _rn(health.get("uptime_s"), 0), "loop_lag_ms": _rn(health.get("loop_lag_ms"), 0)})
        if age is None or age > HB_DEAD_S:
            add("exec", "Исполнитель", "bad", "не отвечает", "heartbeat %s назад: исполнитель мёртв или завис. Позиции защищены стопами на бирже, новые входы по планам не идут." % ("неизвестно" if age is None else "%.0f с" % age))
        elif age > HB_ALIVE_S:
            add("exec", "Исполнитель", "warn", "запаздывает", "heartbeat %.0f с назад (норма до %d с)" % (age, HB_ALIVE_S))
        else:
            add("exec", "Исполнитель", "ok", "жив", "heartbeat %.1f с назад · %s%s" % (age, env or "?", " · pid %s" % ex["pid"] if ex["pid"] else ""))
        add("mode", "Режим", "warn" if observe else "ok", mode, "observe: исполнитель только наблюдает, ничего не пишет на биржу" if observe else "профиль %s, среда %s" % (prof or "?", env or "?"))
        hold, hr = health.get("hold"), [_s(x, 40) for x in _l(health.get("hold_reasons"))][:6]
        ex["hold"], ex["hold_reasons"] = (_s(hold, 40) if hold else None), hr
        if hold or hr:
            add("hold", "Hold", "warn", _s(hold, 24) if hold else "да", "новые входы и добор запрещены, защитные действия разрешены" + (": " + ", ".join(hr) if hr else ""))
        ws = _d(health.get("ws"))
        rest = _d(health.get("rest"))
        parts, lvl = [], "ok"
        no_private = env == "paper" or prof == "paper"           # в paper приватного канала нет: up=false там норма, а не сбой
        last_conn = {}
        for e in events or []:                                    # последнее событие connectivity по каналу: state и время
            d_ = _d(e.get("d"))
            if e.get("type") == "connectivity" and isinstance(d_.get("channel"), str):
                last_conn[d_["channel"]] = (_s(d_.get("state"), 8), _epoch(e.get("ts")), _n(d_.get("down_s")))
        recovered = []
        for ch in ("public", "private"):
            c = _d(ws.get(ch))
            if not c:
                continue
            if ch == "private" and no_private:
                parts.append("private нет (paper)")
                continue
            up = c.get("up") is True
            lc = last_conn.get(ch)
            if up and lc and lc[0] == "up" and lc[1] is not None and 0 <= now - lc[1] < recover_s:
                recovered.append("%s восстановлен %.0f с назад (обрыв %.0f с)" % (ch, now - lc[1], lc[2] or 0))
            down = _n(c.get("down_s")) or 0
            parts.append("%s %s" % (ch, "up" if up else "DOWN" + (" %.0f с" % down if down else "")))
            if not up:
                lvl = "bad" if (down >= 20 or lvl == "bad") else "warn"
        if rest:
            rok = rest.get("ok") is not False
            parts.append("REST %s" % ("ok" if rok else "ошибки"))
            if not rok:
                lvl = "bad" if (_n(rest.get("last_ok_s")) or 0) >= 60 else ("warn" if lvl == "ok" else lvl)
            elif (_n(rest.get("errors_1m")) or 0) > 0 and lvl == "ok":
                lvl = "warn"
        if parts:
            if lvl == "ok" and recovered:       # короткое окно после восстановления (CONN_RECOVER_S), затем снова зелёный
                add("net", "Связь", "warn", "восстановлена", " · ".join(parts) + " · " + "; ".join(recovered))
            else:
                add("net", "Связь", lvl, "норма" if lvl == "ok" else ("потеряна" if lvl == "bad" else "сбой"), " · ".join(parts))
        rec = _epoch(health.get("last_reconcile_ts"))
        if rec:
            ra = max(0.0, now - rec)
            ex["reconcile_age_s"] = _rn(ra, 0)
            add("recon", "Реконсиляция", "warn" if ra > RECON_WARN_S else "ok", "%.0f с" % ra, "последняя сверка с биржей %.0f с назад" % ra)
        ex["plans"] = {k: int(_n(v) or 0) for k, v in _d(health.get("plans")).items() if isinstance(k, str)}
        ex["positions"] = int(_n(health.get("positions")) or 0)
    if kill:
        add("kill", "KILL", "bad", "ВКЛЮЧЁН", "есть файл state/KILL%s: исполнитель не открывает новые позиции%s" % (" (%s)" % kill["reason"] if kill.get("reason") else "", " и закрывает позиции (flatten)" if kill.get("flatten") else ""))
    if backlog["last_seq"] is not None or backlog["consumers"]:
        n, old = backlog["n"], backlog["oldest_age_s"]
        lvl = "warn" if (n and old is not None and old > UNACKED_WARN_S) else "ok"
        add("events", "События", lvl, ("не подтв. %d" % n) if n else "ок",
            "последнее событие %s назад · не подтверждено wake: %d%s" % ("–" if backlog["last_age_s"] is None else "%.0f с" % backlog["last_age_s"], n, (" (самое старое %.0f с)" % old) if old else ""))
    if scan is not None:
        add("scan", "Сканер", "ok" if scan.get("running") else ("warn" if health is not None else "off"), "работает" if scan.get("running") else "стоит",
            "цены от сканера обновлены %.0f с назад" % scan["age_s"] if scan.get("running") else "сканер не запущен или данные устарели")
    order = {"bad": 3, "warn": 2, "ok": 1, "off": 0}
    top = max(items, key=lambda i: order[i["level"]])["level"] if items else "off"
    return {"level": "ok" if top == "off" else top, "items": items, "exec": ex, "kill": kill, "backlog": backlog,
            "available": health is not None or bool(kill) or backlog["last_seq"] is not None}


# ---------------------------------------------------------------- сводка решений
def decisions_day(decs, now):
    t0 = model.day_start(now)
    rows = [d for d in decs if (d["t"] or 0) >= t0]
    codes = {}
    for d in rows:
        if d["denied"] and d["code"]:
            codes[d["code"]] = codes.get(d["code"], 0) + 1
    return {"n": len(rows), "brain": sum(1 for d in rows if d["actor"].startswith("brain") and d["kind"] == "cmd"),
            "rejects": sum(1 for d in rows if d["denied"]), "reject_codes": sorted(({"code": k, "n": v} for k, v in codes.items()), key=lambda x: -x["n"])[:8],
            "auto": sum(1 for d in rows if d["kind"] == "rule" and d["actor"] == "exec"), "skips": sum(1 for d in rows if d["noop"]),
            "protect": sum(1 for d in rows if d["kind"] == "protect"), "start": t0}


# ---------------------------------------------------------------- общий вход
def _ctx(root, now):
    mkt = model.market_info(root, now)
    sp = model.scan_prices(root, now)
    return mkt, sp


def v2_state(root=None, now=None, mkt=None, sp=None, scan=None, sess_view=None):
    """Ключ `v2` ответа /api/state: полоса состояния, доска планов, позиции под планом, сводка решений, события."""
    now = time.time() if now is None else now
    if mkt is None or sp is None:
        mkt, sp = _ctx(root, now)
    levels = _read_levels(root)
    health = read_health(root, now)
    snap = read_snapshot(root, now)
    kill = read_kill(root, now)
    items, broken = read_plans(root)
    events = read_events(root)
    backlog = backlog_info(events, read_cursors(root), now)
    cap = _chase_cap(root)
    cards = [plan_card(it, mkt, sp, levels, snap, now, cap) for it in items]
    by_id = {it["id"]: it for it in items}
    modes = {c["id"]: c["mode"] for c in cards}
    decs = [decision_row(r, modes) for r in read_decisions(root)]
    counts = {c: 0 for c in COLUMNS}
    shown = []
    for c in cards:
        if c["col"] == "closed":
            if (c["ts_closed"] or 0) < now - CLOSED_WINDOW_S:
                continue
        counts[c["col"]] += 1
        shown.append(c)
    order = {"waiting": 0, "watching": 1, "in_trade": 2, "closed": 3}
    shown.sort(key=lambda c: (order[c["col"]], (c["trigger"]["dist_pct"] if c["col"] in ("waiting", "watching") and c["trigger"]["dist_pct"] is not None else 1e9), -(c["ts_closed"] or c["ts_armed"] or 0)))
    snap_pos = {}
    for p in _l(_d(snap).get("positions")):
        if isinstance(p, dict) and isinstance(p.get("owner"), str) and p["owner"].startswith("plan:"):
            snap_pos[p["owner"][5:]] = p
    positions = []
    for c in cards:
        if c["col"] == "in_trade":
            positions.append(build_position(c, by_id[c["id"]], snap_pos.get(c["id"]), (snap or {}).get("_age_s"), decs, now))
    strip = build_strip(root, now, health, kill, backlog, scan, sess_view, counts, events)
    other = [{"sym": _s(p.get("sym"), 20), "side": p.get("side"), "owner": _s(p.get("owner"), 60)} for p in _l(_d(snap).get("positions"))
             if isinstance(p, dict) and not str(p.get("owner") or "").startswith("plan:")][:10]
    ev_rows = [event_row(e) for e in events[-60:]]
    return {
        "available": bool(strip["available"] or items or decs or events),
        "strip": strip, "plans": {"counts": counts, "total": len(items), "broken": broken, "cards": shown[:PLANS_LIMIT], "truncated": max(0, len(shown) - PLANS_LIMIT)},
        "positions": positions, "foreign_positions": other, "decisions_day": decisions_day(decs, now),
        "last_decisions": [{k: d[k] for k in ("seq", "t", "actor", "plan", "action", "why", "ok", "code", "denied", "noop", "idle", "kind")} for d in decs[-8:]][::-1],
        "pauses": pauses_info(events, now), "wake_events": [e for e in ev_rows if e["pri"] == "wake"][-6:][::-1], "now": now,
    }


def plans_list(root, status=None, mode=None, now=None):
    now = time.time() if now is None else now
    mkt, sp = _ctx(root, now)
    levels, snap = _read_levels(root), read_snapshot(root, now)
    items, broken = read_plans(root)
    cap = _chase_cap(root)
    cards = [plan_card(it, mkt, sp, levels, snap, now, cap) for it in items]
    want = {s for s in (status or "").split(",") if s}
    cards = [c for c in cards if (not want or c["status"] in want) and (mode in (None, "", "all") or c["mode"] == mode)]
    return {"plans": cards[:PLANS_LIMIT], "total": len(cards), "broken": broken}


def _flat(d, pre="", out=None, depth=0):
    out = {} if out is None else out
    if isinstance(d, dict) and depth < 5:
        for k, v in d.items():
            _flat(v, pre + ("." if pre else "") + str(k), out, depth + 1)
    elif isinstance(d, list) and depth < 5:
        out[pre] = json.dumps(d, ensure_ascii=False, sort_keys=True)
    else:
        out[pre] = json.dumps(d, ensure_ascii=False) if not isinstance(d, str) else d
    return out


def rev_changes(prev, cur):
    """Различия двух спецификаций (плоские пути) -> список «путь: было → стало» (не более 12)."""
    a, b = _flat(prev), _flat(cur)
    out = []
    for k in sorted(set(a) | set(b)):
        if k in ("rev", "armed_ts", "prereg_hash", "guard_preview") or k.startswith(("guard_preview.", "warnings")):
            continue
        if a.get(k) != b.get(k):
            out.append("%s: %s → %s" % (_s(k, 60), _s(a.get(k), 50) or "—", _s(b.get(k), 50) or "—"))
        if len(out) >= 12:
            break
    return out


def plan_detail(root, pid, now=None):
    """/api/v2/plan?id=: полный spec, runtime, история ревизий с различиями, решения и события по плану."""
    now = time.time() if now is None else now
    if not isinstance(pid, str) or not RE_PLAN.match(pid):
        return 400, {"error": "bad plan id"}
    d = os.path.join(paths.plans(root=root), pid)
    spec = model.read_json(os.path.join(d, "spec.json"))
    if not isinstance(spec, dict):
        return 404, {"error": "plan not found"}
    rt = model.read_json(os.path.join(d, "runtime.json"))
    rt = rt if isinstance(rt, dict) else {}
    revs_raw = model.read_jsonl(os.path.join(d, "revs.jsonl"))[-40:]
    revs, prev = [], None
    for r in revs_raw:
        full = r.get("spec") if isinstance(r.get("spec"), dict) else None
        if full is not None:
            ch = rev_changes(prev, full) if prev is not None else ["создан"]
            prev = full
        elif isinstance(r.get("patch"), dict):
            ch = [("%s → %s" % (k, _s(v, 60))) for k, v in list(_flat(r["patch"]).items())[:12]]
        else:
            ch = []
        revs.append({"rev": int(_n(r.get("rev")) or 0), "ts": _s(r.get("ts"), 24), "why": _s(r.get("why") or r.get("reason"), 200), "by": _s(r.get("by") or r.get("actor"), 16),
                     "changes": ch})
    mkt, sp = _ctx(root, now)
    card = plan_card({"id": pid, "spec": spec, "rt": rt, "mtime": None}, mkt, sp, _read_levels(root), read_snapshot(root, now), now, _chase_cap(root))
    decs = [decision_row(r, {pid: card["mode"]}) for r in read_decisions(root) if r.get("plan") == pid][-120:]
    evs = [event_row(e) for e in read_events(root) if e.get("plan") == pid][-40:]
    return 200, {"id": pid, "card": card, "spec": spec, "runtime": rt, "revs": revs, "decisions": decs[::-1], "events": evs[::-1]}


def decisions_feed(root, query, now=None):
    """/api/v2/decisions?after=SEQ&before=SEQ&plan=&actor=&kind=&only=fail&mode=&limit=: новые сверху."""
    def q(k, default=""):
        v = query.get(k)
        return (v[0] if isinstance(v, list) and v else v) or default
    items, _ = read_plans(root)
    modes = {}
    for it in items:
        m = it["spec"].get("mode")
        modes[it["id"]] = m if m in ("live", "paper") else ("paper" if it["rt"].get("paper") else "live")
    rows = [decision_row(r, modes) for r in read_decisions(root)]
    after, before = int(_n(q("after")) or 0), int(_n(q("before")) or 0)
    plan, actor, kind, only, mode = q("plan"), q("actor"), q("kind"), q("only"), q("mode")
    limit = max(1, min(DEC_LIMIT, int(_n(q("limit")) or 100)))

    def ok(d):
        if after and d["seq"] <= after:
            return False
        if before and d["seq"] >= before:
            return False
        if plan and d["plan"] != plan:
            return False
        if actor and not (d["actor"] == actor or d["actor"].startswith(actor + ":")):
            return False
        if kind and d["kind"] != kind and d["what"] != kind:
            return False
        if only == "fail" and not (d["denied"] or d["noop"]):
            return False
        if mode in ("live", "paper") and d["mode"] != mode:
            return False
        return True
    sel = [d for d in rows if ok(d)]
    sel.sort(key=lambda d: -d["seq"])
    now = time.time() if now is None else now
    return {"decisions": sel[:limit], "total": len(sel), "last_seq": rows[-1]["seq"] if rows else 0, "day": decisions_day(rows, now),
            "plans": sorted({d["plan"] for d in rows if d["plan"]})[-60:], "kinds": sorted({d["what"] for d in rows if d["what"]})[:40]}


def events_feed(root, query):
    def q(k, default=""):
        v = query.get(k)
        return (v[0] if isinstance(v, list) and v else v) or default
    after = int(_n(q("after")) or 0)
    limit = max(1, min(200, int(_n(q("limit")) or 50)))
    rows = [event_row(e) for e in read_events(root) if e["seq"] > after]
    return {"events": rows[-limit:][::-1], "last_seq": rows[-1]["seq"] if rows else after}


# ---------------------------------------------------------------- досье
_BRIEF_ALLOW = ("brief_id", "ts", "scope", "trunc", "system", "account", "session", "room", "positions", "plans", "market", "hot", "levels", "ideas", "recent",
                "lessons", "checklist", "since_last", "unacked", "struct", "limits", "risk_level", "economics", "experiments", "intel", "news_digest")


def _clean_brief(x, depth=0):
    """Рекурсивная очистка: строки обрезаются, вложенность ограничена, ключи с секретами вырезает model.scrub."""
    if depth > 7:
        return None
    if isinstance(x, dict):
        return {_s(k, 40): _clean_brief(v, depth + 1) for k, v in list(x.items())[:60]}
    if isinstance(x, list):
        return [_clean_brief(v, depth + 1) for v in x[:60]]
    if isinstance(x, str):
        return _s(x, 200)
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def brief_list(root):
    d = paths.briefs(root=root)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json") and RE_BRIEF.match(n[:-5])]
    except OSError:
        return []
    return sorted((n[:-5] for n in names), reverse=True)


def brief_get(root, bid):
    ids = brief_list(root)
    want = bid if (bid and bid != "last") else (ids[0] if ids else None)
    if want is None:
        return 200, {"available": False, "ids": [], "brief": None}
    if not RE_BRIEF.match(want):
        return 400, {"error": "bad brief id"}
    b = model.read_json(os.path.join(paths.briefs(root=root), want + ".json"))
    if not isinstance(b, dict):
        return 200, {"available": False, "ids": ids[:30], "missing": want, "brief": None}
    clean = {k: _clean_brief(b[k]) for k in _BRIEF_ALLOW if k in b}
    for p in _l(clean.get("plans")):                     # own_thesis: свой прошлый текст, обрезан
        if isinstance(p, dict) and "own_thesis" in p:
            p["own_thesis"] = _s(p["own_thesis"], 160)
    return 200, {"available": True, "id": want, "ids": ids[:30], "total": len(ids), "brief": clean}


# ---------------------------------------------------------------- эксперименты
def _tag(t, key, field):
    v = t.get(field)
    if isinstance(v, str) and v:
        return _s(v, 40)
    for g in t.get("tags") or []:
        if isinstance(g, str) and g.startswith(key + ":"):
            return _s(g[len(key) + 1:], 40)
    return None


def _group_stats(rows):
    s = model.summarize(rows)
    pn = sum(abs(x.get("pnl_usdt") or 0.0) for x in rows)

    def avg(sel, key):
        v = [x["path"][key] for x in sel if isinstance(x.get("path"), dict) and _n(x["path"].get(key)) is not None]
        return (sum(v) / len(v)) if v else None
    wins = [x for x in rows if (x.get("pnl_usdt") or 0) > 0]
    loss = [x for x in rows if (x.get("pnl_usdt") or 0) <= 0]
    byrule = sum(1 for x in rows if x.get("reason") in ("be", "trail", "timeout", "invalidation", "rule"))
    return {"n": s["n"], "winrate": s["winrate"], "avg_r": s["avg_r"], "r_se": s["r_se"], "r_n": s["r_n"], "pf": s["pf"], "pf_inf": s["pf_inf"], "pnl": s["pnl"],
            "fees": s["fees"], "fees_share": (s["fees"] / pn) if pn > 0 else None, "mae_win": _rn(avg(wins, "mae_r"), 2), "mae_loss": _rn(avg(loss, "mae_r"), 2),
            "mfe_win": _rn(avg(wins, "mfe_r"), 2), "mfe_loss": _rn(avg(loss, "mfe_r"), 2), "by_rule_share": (byrule / s["n"]) if s["n"] else None,
            "star": s["n"] < MIN_N}


def exp_stats(root, by="exp", mode="all"):
    """/api/v2/exp?by=exp|variant|setup|hyp|symbol|mode&mode=: таблица как `./tb exp stats`, без вердиктов. n < 30 помечается звёздочкой."""
    by = by if by in ("exp", "variant", "setup", "hyp", "symbol", "mode") else "exp"
    trades, _ = model.load_trades(root)
    closed = [t for t in trades if t["status"] == "closed" and (mode in (None, "", "all") or t["mode"] == mode)]
    groups = {}
    for t in closed:
        e, v = _tag(t, "exp", "exp"), _tag(t, "var", "var")
        key = {"exp": e, "variant": ("%s/%s" % (e, v)) if e and v else e, "setup": _tag(t, "setup", "setup"), "hyp": _tag(t, "hyp", "hyp"),
               "symbol": t.get("sym"), "mode": t.get("mode")}[by]
        if not key:
            continue
        groups.setdefault(str(key), []).append(t)
    rows = []
    for k, v in groups.items():
        g = _group_stats(v)
        g["name"] = k
        g["modes"] = sorted({x["mode"] for x in v})
        g["plan_n"] = sum(1 for x in v if x.get("plan_id"))
        rows.append(g)
    rows.sort(key=lambda g: (-g["n"], g["name"]))
    meta = model.read_json(paths.config("experiments.json", root=root))
    meta = {k: {"hypothesis": _s(_d(v).get("hypothesis"), 200), "prereg": _s(_d(v).get("prereg"), 280), "started": _s(_d(v).get("started"), 12),
                "min_n": int(_n(_d(v).get("min_n")) or MIN_N)} for k, v in _d(meta).items() if isinstance(v, dict)}
    k_cmp = len(rows)
    return {"by": by, "mode": mode, "rows": rows[:60], "k": k_cmp, "alpha": (0.05 / k_cmp) if k_cmp else None, "min_n": MIN_N, "meta": meta,
            "total_closed": len(closed), "with_plan": sum(1 for t in closed if t.get("plan_id"))}


# ---------------------------------------------------------------- маршрутизатор /api/v2/*
def api_v2(path, query, root=None, now=None):
    """-> (статус, объект) для /api/v2/*; scrub применяет вызывающий (server.api_payload)."""
    def q(k, default=""):
        v = query.get(k)
        return (v[0] if isinstance(v, list) and v else v) or default
    now = time.time() if now is None else now
    if path == "/api/v2/state":
        mkt, sp = _ctx(root, now)
        scan = {"ok": sp["scan"]["ok"], "age_s": sp["scan"]["age_s"], "running": bool(sp["scan"]["ok"] and sp["scan"]["age_s"] is not None and sp["scan"]["age_s"] <= model.SCAN_STALE_S)}
        return 200, v2_state(root, now, mkt, sp, scan, None)
    if path == "/api/v2/plans":
        return 200, plans_list(root, q("status"), q("mode"), now)
    if path == "/api/v2/plan":
        return plan_detail(root, q("id"), now)
    if path == "/api/v2/decisions":
        return 200, decisions_feed(root, query, now)
    if path == "/api/v2/brief":
        return brief_get(root, q("id", "last"))
    if path == "/api/v2/events":
        return 200, events_feed(root, query)
    if path == "/api/v2/exp":
        return 200, exp_stats(root, q("by", "exp"), q("mode", "all"))
    return 404, {"error": "unknown endpoint"}
