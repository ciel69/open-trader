"""Модель дашборда: читает ЛОКАЛЬНЫЕ файлы проекта и считает всё, что показывает страница. Только чтение.

Источники (пути только через tb.paths): state/trades.jsonl, sessions.jsonl, portfolio.json, learning.json,
runtime/market.json, prices.json (цены всех монет от сканера), context.json, hot.json, hot_events.jsonl, hot_pending.json, health.json, events.jsonl, state/ideas.jsonl, hot_outcomes.jsonl, config/risk_profile.json (лимиты сессий, лестница).
На биржу не ходит, ключей не читает. Любой отсутствующий или битый файл даёт пустое значение, а не ошибку.

Журнал разбирается устойчиво (как в core.journal, но без падений на мусоре):
  - повторный open с тем же id при ещё открытой записи заменяет её (так бывает в тестах); open после close = новая запись;
  - повторный close с тем же id правит запись (поправки по факту биржи: берётся последний);
  - close без open пропускается и считается в предупреждениях; поля, которых нет в старых записях, достраиваются.
"""
import json
import math
import os
import time

from tb import paths
from tb.core import ideas as ideas_core
from tb.marketdata import ideawatch
from tb.core import portfolio
from tb.core import tradepath


def session_cfg(stype, prof):
    """Лимиты сессии по типу (core.session.cfg_of): горизонт из session_types, день (`day`) как intraday / day_session."""
    from tb.core import session as _session
    return _session.cfg_of(stype, prof or {}) or {}

FEE = 0.0009  # запасная ставка taker на сторону, если в записи нет fee_open
STALE_FEED_S = 30.0
DEAD_FEED_S = 180.0
STALE_CONTEXT_S = 600.0
STALE_HOT_S = 180.0
PRICE_STALE_S = 60.0       # market.json (фид, пишет раз в секунды)
SCAN_STALE_S = 150.0       # prices.json / hot.json (сканер, раз в 30 с): старше -> цена устарела
SCAN_HINT = "цена не обновляется: сканер не запущен, запусти ./tb bybit read scan --watch"
HISTORY_LIMIT = 500
_SECRET_WORDS = ("key", "secret", "token", "passw", "signature", "apikey")

_CACHE = {}


# ---------------------------------------------------------------- чтение файлов
def scrub(obj):
    """Страховка: вырезает из любого ответа поля, похожие на секреты (ключ, токен, пароль, подпись)."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not any(w in str(k).lower() for w in _SECRET_WORDS)}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime, st.st_size
    except OSError:
        return None


def read_json(path):
    """-> dict/list или None (нет файла, битый JSON, идёт запись)."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    hit = _CACHE.get(("j", path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            val = json.load(f)
    except (OSError, ValueError):
        return None
    _CACHE[("j", path)] = (stamp, val)
    return val


def read_jsonl(path, tail=None):
    """-> список dict; битые и нецелые строки (идущая запись) пропускаются; tail = последние N строк."""
    stamp = _stamp(path)
    if stamp is None:
        return []
    key = ("l", path, tail)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if tail:
        lines = lines[-tail:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    _CACHE[key] = (stamp, out)
    return out


def num(x, default=None):
    """Число или default: строки, None, nan и inf не пропускаются."""
    if isinstance(x, bool):
        return default
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):   # огромное целое (10**400) тоже «не число»
        return default
    return v if math.isfinite(v) else default


def parse_ts(s):
    """'2026-10-27T07:26:57Z' -> epoch (UTC) или None."""
    if not isinstance(s, str):
        return num(s)
    try:
        import calendar
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- журнал сделок
def _norm_trade(r):
    t = dict(r)
    t.pop("ev", None)
    t["id"] = str(r.get("id"))
    t["side"] = r.get("side") if r.get("side") in ("long", "short") else None
    for k in ("entry", "lev", "margin", "notional", "sl", "tp", "risk_usdt", "fee_open"):
        t[k] = num(r.get(k))
    if t["notional"] is None and t["margin"] is not None and t["lev"] is not None:
        t["notional"] = t["margin"] * t["lev"]
    if t["risk_usdt"] is None and t["sl"] and t["entry"] and t["notional"]:
        t["risk_usdt"] = abs(t["entry"] - t["sl"]) / t["entry"] * t["notional"]
    t["mode"] = r.get("mode") or "live"
    t["venue"] = r.get("venue") or "platform"
    tags = r.get("tags")
    t["tags"] = [str(x) for x in tags] if isinstance(tags, list) else []
    t["thesis"] = r.get("thesis") or ""
    t["ts_open_epoch"] = parse_ts(r.get("ts_open"))
    return t


def _apply_close(t, r):
    for k in ("ts_close", "exit", "reason", "note"):
        if k in r:
            t[k] = r[k]
    if isinstance(r.get("path"), dict):
        t["path"] = r["path"]
    _apply_post(t, r)                    # теги ошибок/правила/урок могут прийти прямо в close
    t["exit"] = num(t.get("exit"))
    t["pnl_usdt"] = num(r.get("pnl_usdt"), t.get("pnl_usdt"))
    t["fees_usdt"] = num(r.get("fees_usdt"), t.get("fees_usdt"))
    t["funding_usdt"] = num(r.get("funding_usdt"), t.get("funding_usdt") or 0.0)
    if "r" in r:
        t["r"] = num(r.get("r"))
    if num(r.get("risk_usdt")) is not None:
        t["risk_usdt"] = num(r.get("risk_usdt"))
    t["ts_close_epoch"] = parse_ts(t.get("ts_close"))
    t["status"] = "closed"
    if t.get("pnl_usdt") is None and t.get("exit") and t.get("entry") and t.get("notional") and t.get("side"):
        sign = 1 if t["side"] == "long" else -1  # старая запись без pnl: считаем как journal.cmd_close
        t["pnl_usdt"] = sign * (t["exit"] / t["entry"] - 1.0) * t["notional"] - (t.get("fees_usdt") or 0.0)
    if t.get("r") is None and t.get("pnl_usdt") is not None and t.get("risk_usdt"):
        t["r"] = t["pnl_usdt"] / t["risk_usdt"]
    t["move_pct"], t["pnl_pct_margin"] = trade_pcts(t)


def _apply_v2_event(t, ev, r):
    """add: средняя цена и размер после добора (R относится к исходному риску, risk_usdt не меняется); part: накопление
    реализованного по частям, остаток позиции уменьшается; adjust: текущие стоп/тейк (исходный стоп остаётся в sl0)."""
    px, qty, fee = num(r.get("px")), num(r.get("qty")), num(r.get("fee"), 0.0)
    ts = parse_ts(r.get("ts"))
    if ev == "adjust":
        if t.get("sl0") is None:
            t["sl0"] = t.get("sl")
        for k in ("sl", "tp"):
            v = num(r.get(k))
            if v is not None and v > 0:
                t[k] = v
        t.setdefault("adjusts", []).append({"ts": ts, "sl": num(r.get("sl")), "tp": num(r.get("tp")), "by": str(r.get("by") or "")[:16]})
        return
    if not (px and px > 0 and qty and qty > 0) or not t.get("entry"):
        return
    cur_qty = t["qty"] if t.get("qty") is not None else ((t["notional"] / t["entry"]) if t.get("notional") else None)
    if cur_qty is None:
        return
    lev = t.get("lev") or 1.0
    if ev == "add":
        new_qty = cur_qty + qty
        t["entry"] = (cur_qty * t["entry"] + qty * px) / new_qty
        t["qty"], t["notional"] = new_qty, new_qty * t["entry"]
        t["margin"] = (t.get("margin") or 0.0) + qty * px / lev
        t["fee_open"] = (t.get("fee_open") or 0.0) + fee
        t.setdefault("adds", []).append({"ts": ts, "px": px, "qty": qty, "fee": fee})
    else:
        part_q = min(qty, cur_qty)
        left = max(0.0, cur_qty - part_q)
        frac = (left / cur_qty) if cur_qty else 0.0
        t["qty"], t["notional"] = left, left * t["entry"]
        t["margin"] = (t.get("margin") or 0.0) * frac
        pnl = num(r.get("pnl"), 0.0)
        fo = t.get("fee_open")
        if fo is not None:       # комиссия входа делится по долям: остаток несёт свою часть, закрытая часть уходит в зафиксированное (сумма не меняется)
            t["fee_open"] = fo * frac
            fee = fee + fo * (1.0 - frac)
        t["realized_parts"] = (t.get("realized_parts") or 0.0) + pnl
        t["fees_parts"] = (t.get("fees_parts") or 0.0) + fee
        t.setdefault("parts", []).append({"ts": ts, "px": px, "qty": part_q, "fee": fee, "pnl": pnl})


def trade_pcts(t):
    """Проценты закрытой сделки из журнала: move_pct (цена монеты от входа до выхода с учётом стороны, без плеча и комиссий)
    и margin_pct (pnl_usdt после комиссий / маржа * 100, с плечом). Нет нужных полей -> None, без ошибок."""
    entry, ext, side = t.get("entry"), t.get("exit"), t.get("side")
    move = None
    if entry and ext and side in ("long", "short"):
        move = (ext / entry - 1.0) * 100.0 if side == "long" else (1.0 - ext / entry) * 100.0
    pnl, margin = t.get("pnl_usdt"), t.get("margin")
    mp = (pnl / margin * 100.0) if (pnl is not None and margin) else None
    return move, mp


def _apply_post(t, r):
    err = r.get("err")
    if isinstance(err, list) and err:
        t["err"] = [str(x) for x in err]
    for k in ("rule_followed", "lesson"):
        if isinstance(r.get(k), str) and r[k]:
            t[k] = r[k]


def _norm_review(t):
    """Поля разбора и контекста нормализуются для страницы (мусор -> пусто). Не трогает остальное."""
    err = t.get("err")
    t["err"] = [str(x) for x in err] if isinstance(err, list) else []
    t["rule_followed"] = t.get("rule_followed") if t.get("rule_followed") in ("yes", "no", "partial") else None
    t["lesson"] = t.get("lesson") if isinstance(t.get("lesson"), str) else None
    p = t.get("path")
    t["path"] = {k: p.get(k) for k in ("mae_pct", "mfe_pct", "mae_r", "mfe_r", "mfe_to_tp", "t_mae_s", "t_mfe_s", "n", "path_coverage")
                 if num(p.get(k)) is not None} if isinstance(p, dict) else None
    if t["path"] is not None and not t["path"]:
        t["path"] = None


def path_live(root, opened):
    """Текущие MAE/MFE открытых сделок из runtime/trade_paths.json (пишет сканер): {id: поля path} только для тех, что сканер ведёт."""
    d = read_json(paths.runtime(tradepath.STATE_NAME, root=root))
    st = d.get("trades") if isinstance(d, dict) and isinstance(d.get("trades"), dict) else {}
    out = {}
    for t in opened:
        s = st.get(t["id"])
        if isinstance(s, dict):
            try:
                p = tradepath.compute(s, t.get("side"), sl=t.get("sl"), tp=t.get("tp"))
            except (TypeError, ValueError, KeyError):
                p = None
            if p:
                p["last_ts"] = num(s.get("last_ts"))
                out[t["id"]] = p
    return out


def load_trades(root=None):
    """-> (список сделок в порядке открытия, предупреждения-счётчики). Каждая сделка = нормализованный dict."""
    recs = read_jsonl(paths.state("trades.jsonl", root=root))
    trades, last, warn = [], {}, {"dup_open": 0, "orphan_close": 0, "bad_rows": 0}
    for r in recs:
        ev, tid = r.get("ev"), r.get("id")
        if tid is None or ev not in ("open", "close", "post", "add", "part", "adjust"):
            warn["bad_rows"] += 1
            continue
        tid = str(tid)
        if ev == "open":
            t = _norm_trade(r)
            t["status"] = "open"
            cur = last.get(tid)
            if cur is not None and cur["status"] == "open":
                warn["dup_open"] += 1
                trades[trades.index(cur)] = t  # повторный open при открытой записи: заменяем на месте
            else:
                trades.append(t)
            last[tid] = t
        elif ev == "post":              # разбор закрытой сделки, дописанный позже (journal post): только поля разбора
            t = last.get(tid)
            if t is not None:
                _apply_post(t, r)
        elif ev in ("add", "part", "adjust"):   # события v2 (06_contracts 6.10): добор, частичное закрытие, смена стопа/тейка
            t = last.get(tid)
            if t is None:
                warn["orphan_close"] += 1
            elif t["status"] == "open":
                _apply_v2_event(t, ev, r)
        else:
            t = last.get(tid)
            if t is None:
                warn["orphan_close"] += 1
                continue
            _apply_close(t, r)
    for t in trades:
        if t["status"] == "closed" and t.get("pnl_usdt") is None:
            t["pnl_usdt"] = 0.0
        _norm_review(t)
    return trades, warn


# ---------------------------------------------------------------- рынок и цены
def market_info(root=None, now=None):
    now = time.time() if now is None else now
    m = read_json(paths.runtime("market.json", root=root))
    if not isinstance(m, dict):
        return {"ok": False, "age_s": None, "symbols": {}}
    ts = num(m.get("ts_epoch")) or parse_ts(m.get("ts"))
    syms = m.get("symbols") if isinstance(m.get("symbols"), dict) else {}
    return {"ok": True, "ts": m.get("ts"), "age_s": None if ts is None else max(0.0, now - ts),
            "feed_ok": m.get("feed_ok"), "symbols": syms}


def price_for(mkt, sym, venue=None):
    """-> (цена или None, источник). Для сделок на bybit берём цену Bybit, если фид её даёт, иначе основную."""
    s = (mkt.get("symbols") or {}).get(sym)
    if not isinstance(s, dict):
        return None, None
    if venue == "bybit":
        p = num((s.get("prices_by_src") or {}).get("bybit"))
        if p:
            return p, "bybit"
    p = num(s.get("price"))
    return (p, s.get("src")) if p else (None, None)


def norm_sym(sym):
    s = str(sym or "").upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def scan_prices(root=None, now=None):
    """Цены сканера: runtime/prices.json (все монеты) и hot.json (топ+blocked). -> {"scan": {...}, "hot": {...}}, у каждого age_s и prices {SYM: цена}."""
    now = time.time() if now is None else now
    out = {}
    sp = read_json(paths.runtime("prices.json", root=root))
    pr = sp.get("prices") if isinstance(sp, dict) and isinstance(sp.get("prices"), dict) else {}
    ts = num(sp.get("ts_epoch")) if isinstance(sp, dict) else None
    out["scan"] = {"ok": bool(pr), "age_s": None if ts is None else max(0.0, now - ts), "n": len(pr),
                   "prices": {norm_sym(k): num(v) for k, v in pr.items() if num(v)}}
    h = read_json(paths.runtime("hot.json", root=root))
    hp = {}
    if isinstance(h, dict):
        for r in (h.get("top") or []) + (h.get("blocked") or []):
            if isinstance(r, dict) and r.get("sym") and num(r.get("price")):
                hp[norm_sym(r["sym"])] = num(r["price"])
    ts = num(h.get("ts_epoch")) if isinstance(h, dict) else None
    out["hot"] = {"ok": bool(hp), "age_s": None if ts is None else max(0.0, now - ts), "prices": hp}
    return out


def resolve_price(mkt, sp, sym, venue=None):
    """Цена монеты по приоритету: market.json (для bybit сначала prices_by_src.bybit) -> prices.json -> hot.json.
    -> {"price", "src", "age_s", "stale"}. Берётся первая свежая; если свежих нет, самая свежая из имеющихся с stale=True; цен нет нигде -> price None.
    Для площадок кроме bybit запасные источники (цены Bybit) не используются: чужая цена хуже её отсутствия."""
    base = norm_sym(sym)
    cands = []
    price, src = price_for(mkt, sym, venue)
    if price:
        cands.append((price, src or "market.json", mkt.get("age_s"), PRICE_STALE_S))
    if venue in (None, "bybit"):
        for key, name in (("scan", "prices.json"), ("hot", "hot.json")):
            d = sp.get(key) or {}
            if base in d.get("prices", {}):
                cands.append((d["prices"][base], name, d.get("age_s"), SCAN_STALE_S))
    for price, src, age, lim in cands:
        if age is not None and age <= lim:
            return {"price": price, "src": src, "age_s": age, "stale": False}
    if cands:  # свежих нет: самая свежая (без возраста считается самой старой)
        price, src, age, lim = min(cands, key=lambda c: c[2] if c[2] is not None else 1e18)
        return {"price": price, "src": src, "age_s": age, "stale": True}
    return {"price": None, "src": None, "age_s": None, "stale": False}


def fmt_price(x):
    """Цена по величине: значащие цифры, а не фиксированные знаки (0.002035 не должно стать 0.00203). Эталон для app.js price()."""
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        return "–"
    a = abs(x)
    if a >= 1000:
        d = 1
    elif a >= 100:
        d = 2
    elif a >= 10:
        d = 3
    elif a >= 1:
        d = 4
    elif a == 0:
        d = 2
    else:  # < 1: 5 значащих цифр, для < 0.01 хватает 4
        sig = 4 if a < 0.01 else 5
        d = min(10, sig - 1 - int(math.floor(math.log10(a))))
    return "%.*f" % (d, x)


def live_calc(t, price, mkt_age=None, mm_pct=1.0, stale=None):
    """Нереализованный PnL активной сделки по цене из фида. Нет цены -> has_price False, остальное None."""
    out = {"has_price": False, "price": None, "price_stale": False}
    entry, notional, side = t.get("entry"), t.get("notional"), t.get("side")
    sign = 1 if side == "long" else -1
    fee_open = t.get("fee_open")
    out["fee_open_known"] = fee_open is not None
    fee_open = fee_open if fee_open is not None else (FEE * notional if notional else 0.0)
    out["fee_open_usdt"] = round(fee_open, 5)
    if entry and t.get("lev"):  # оценка ликвидации изолированной маржи; поправка на комиссии не учитывается
        out["liq_est"] = entry * (1.0 - sign * (1.0 / t["lev"] - mm_pct / 100.0))
    if not (price and entry and notional and side):
        return out
    gross = sign * (price / entry - 1.0) * notional
    pnl = gross - fee_open
    out.update({"has_price": True, "price": price, "price_stale": bool(mkt_age is not None and mkt_age > PRICE_STALE_S) if stale is None else bool(stale),
                "move_pct": sign * (price / entry - 1.0) * 100.0, "pnl_gross": gross, "pnl": pnl,
                "pnl_pct_margin": (pnl / t["margin"] * 100.0) if t.get("margin") else None,
                "r_now": (pnl / t["risk_usdt"]) if t.get("risk_usdt") else None})
    # оценка с комиссией выхода taker (по цене выхода = текущей): pnl_pct_margin выше считается только после комиссии входа
    est = pnl - FEE * notional * (price / entry)
    out["pnl_est_exit"] = est
    out["pnl_pct_margin_est"] = (est / t["margin"] * 100.0) if t.get("margin") else None
    for name in ("sl", "tp"):  # расстояние до уровня: положительное = уровень ещё впереди, отрицательное = уровень пройден
        lvl = t.get(name)
        if lvl:
            gap = (price - lvl) if name == "sl" else (lvl - price)
            out["to_%s_pct" % name] = sign * gap / price * 100.0
            out["to_%s_usdt" % name] = sign * gap / entry * notional
            out["pnl_at_%s" % name] = sign * (lvl / entry - 1.0) * notional - fee_open
    if t.get("sl") and t.get("tp") and t["sl"] != t["tp"]:
        span = t["tp"] - t["sl"]
        out["gauge"] = {"price": min(1.0, max(0.0, (price - t["sl"]) / span)),
                        "entry": min(1.0, max(0.0, (entry - t["sl"]) / span))}
    return out


# ---------------------------------------------------------------- метрики
def _std_err(rs):
    if len(rs) < 2:
        return None
    m = sum(rs) / len(rs)
    return math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) / math.sqrt(len(rs))


def _avg(vals):
    v = [x for x in vals if x is not None]
    return (sum(v) / len(v)) if v else None


def _ext(fn, rows, key="pnl_pct_margin"):
    v = [x[key] for x in rows if x.get(key) is not None]
    return fn(v) if v else None


def summarize(rows):
    """Сводка по закрытым сделкам: n, винрейт, PnL, комиссии, средний R, профит-фактор, макс. просадка кривой."""
    n = len(rows)
    pnls = [x.get("pnl_usdt") or 0.0 for x in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [x["r"] for x in rows if x.get("r") is not None]
    gl = -sum(losses)
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "wins": len(wins), "losses": len(losses),
            "winrate": (len(wins) / n * 100.0) if n else None,
            "pnl": sum(pnls), "fees": sum(x.get("fees_usdt") or 0.0 for x in rows),
            "funding": sum(x.get("funding_usdt") or 0.0 for x in rows),
            "avg_r": (sum(rs) / len(rs)) if rs else None, "r_se": _std_err(rs), "r_n": len(rs),
            "pf": (sum(wins) / gl) if gl > 0 else None, "pf_inf": bool(gl <= 0 and wins),
            "avg_win": (sum(wins) / len(wins)) if wins else None,
            "avg_loss": (sum(losses) / len(losses)) if losses else None,
            "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
            "avg_move_pct": _avg([x.get("move_pct") for x in rows]), "avg_margin_pct": _avg([x.get("pnl_pct_margin") for x in rows]),
            "best_margin_pct": _ext(max, rows), "worst_margin_pct": _ext(min, rows),
            "best_move_pct": _ext(max, rows, "move_pct"), "worst_move_pct": _ext(min, rows, "move_pct"),
            "max_dd": dd}


def breakdown(rows, key):
    groups = {}
    for x in rows:
        if key == "tag":
            vals = x.get("tags") or ["(без тегов)"]
        elif key == "err":               # теги ошибок из разбора сделки
            vals = x.get("err") or ["(без разбора)"]
        else:
            vals = [str(x.get(key) or "-")]
        for v in vals:
            groups.setdefault(v, []).append(x)
    out = []
    for g, v in groups.items():
        s = summarize(v)
        out.append({"name": g, "n": s["n"], "pnl": s["pnl"], "fees": s["fees"], "winrate": s["winrate"],
                    "avg_r": s["avg_r"], "pf": s["pf"], "pf_inf": s["pf_inf"]})
    out.sort(key=lambda g: (-g["n"], g["name"]))
    return out


def curve(rows):
    pts, cum = [], 0.0
    for x in sorted(rows, key=lambda x: x.get("ts_close_epoch") or 0.0):
        cum += x.get("pnl_usdt") or 0.0
        pts.append({"ts": x.get("ts_close_epoch"), "cum": round(cum, 5), "pnl": x.get("pnl_usdt"), "id": x["id"], "sym": x.get("sym")})
    return pts


def day_start(now):
    """Начало текущих суток по ЛОКАЛЬНОМУ времени машины (как её видит владелец), epoch."""
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def pnl_summary(closed, open_pnl, now):
    """Главные числа обзора: PnL закрытых сегодня (локальные сутки) и всего, плюс нереализованный PnL открытых.
    total.pnl = реализованный + открытый (открытый None, если цен нет: тогда считается только реализованный)."""
    t0 = day_start(now)
    today = [x for x in closed if (x.get("ts_close_epoch") or 0.0) >= t0]
    realized = sum(x.get("pnl_usdt") or 0.0 for x in closed)
    return {"today": {"start": t0, "n": len(today), "wins": sum(1 for x in today if (x.get("pnl_usdt") or 0.0) > 0),
                      "pnl": sum(x.get("pnl_usdt") or 0.0 for x in today)},
            "total": {"realized": realized, "open": open_pnl, "pnl": realized + (open_pnl or 0.0), "n": len(closed)}}


# ---------------------------------------------------------------- сессии
def load_sessions(root=None):
    recs = read_jsonl(paths.state("sessions.jsonl", root=root))
    sess, order = {}, []
    for r in recs:
        sid = r.get("id")
        if sid is None:
            continue
        if r.get("ev") == "start":
            sess[sid] = dict(r)
            order.append(sid)
        elif sid in sess:
            if r.get("ev") == "extend" and num(r.get("expires")) is not None:
                sess[sid]["expires"] = r["expires"]
                sess[sid].setdefault("extensions", []).append(r.get("reason", ""))
            elif r.get("ev") == "stop":
                sess[sid]["stopped"] = r.get("ts") or True
                sess[sid]["stop_note"] = r.get("note", "")
    return [sess[i] for i in order]


def session_view(s, all_trades, prof, now):
    """Активная сессия: остаток времени, бюджет убытка, лимит сделок и вердикт (как session.verdict)."""
    rows = [t for t in all_trades if t.get("session") == s["id"]]
    closed = [t for t in rows if t["status"] == "closed"]
    opened = [t for t in rows if t["status"] == "open"]
    realized = sum(t.get("pnl_usdt") or 0.0 for t in closed)
    open_risk = sum(t.get("risk_usdt") or 0.0 for t in opened)
    try:
        cfg = session_cfg(s.get("type"), prof or {})
    except (KeyError, TypeError):
        cfg = {}
    expires = None            # срока у сессии нет (до stop)
    budget = num(s.get("budget_usdt"))
    why = []
    if budget and realized - open_risk <= -budget:
        why.append("бюджет убытка исчерпан")
    if cfg.get("max_trades") and len(rows) >= cfg["max_trades"]:
        why.append("лимит сделок")
    return {"id": s["id"], "type": s.get("type"), "venue": s.get("venue"), "thesis": s.get("thesis"),
            "start": s.get("ts"), "expires": expires, "left_s": None if expires is None else expires - now,
            "duration_s": (expires - num(s.get("start"))) if expires is not None and num(s.get("start")) else None,
            "budget_usdt": budget, "budget_used": max(0.0, -(realized - open_risk)),
            "equity_at_start": num(s.get("equity_at_start")), "trades": len(rows), "max_trades": cfg.get("max_trades"),
            "open": len(opened), "realized": realized, "open_risk": open_risk,
            "entries_allowed": not why, "block_reasons": why, "extensions": s.get("extensions") or []}


# ---------------------------------------------------------------- здоровье
def _age(ts_epoch, ts_iso, now):
    ts = num(ts_epoch) or parse_ts(ts_iso)
    return None if ts is None else max(0.0, now - ts)


def sync_info(root, now):
    """Статус автосинхронизации капитала (сервис sync): runtime/platform/sync.state.json. None, если сервис не писал состояния."""
    d = read_json(paths.runtime("platform", "sync.state.json", root=root))
    if not isinstance(d, dict):
        return None
    last = num(d.get("last_ok_ts"))
    st = d.get("status") if d.get("status") in ("ok", "error", "no_key", "mismatch") else "error"
    pend = d.get("pending") if isinstance(d.get("pending"), dict) else None
    return {"status": st, "reason": str(d.get("reason") or "")[:160], "last_ok_age_s": None if last is None else max(0.0, now - last),
            "stale": bool(d.get("stale")) or st != "ok", "pending": {"equity": num(pend.get("equity")), "old_equity": num(pend.get("old_equity"))} if pend else None}


def preflight_notes(root, now, max_age_s=6 * 3600.0):
    """Незакрытые замечания предстартовых проверок (P1-P16) из последнего preflight.json дня: id, сообщение и одна строка действия. Только чтение файла."""
    best = None
    try:
        base = paths.state("days", root=root)
        for n in os.listdir(base):
            f = os.path.join(base, n, "preflight.json")
            try:
                m = os.stat(f).st_mtime
            except OSError:
                continue
            if best is None or m > best[0]:
                best = (m, f)
    except OSError:
        return []
    if best is None or now - best[0] > max_age_s:
        return []
    d = read_json(best[1])
    if not isinstance(d, dict):
        return []
    try:
        from tb.platform.services import PREFLIGHT_FIX as FIX
    except Exception:  # noqa: BLE001
        FIX = {}
    out = []
    for c in d.get("checks") or []:
        if isinstance(c, dict) and c.get("ok") is False and isinstance(c.get("id"), str):
            out.append({"id": c["id"], "level": "bad" if c.get("sev") == "R" else "warn", "name": str(c.get("name") or "")[:60], "text": str(c.get("msg") or "")[:200],
                        "action": FIX.get(c["id"], ""), "age_s": round(now - best[0])})
    return out


def macro_until_s(ctx, now):
    """Сколько секунд ещё действует макроокно risk_off (самое позднее из активных событий), None если окна нет или конец неизвестен."""
    m = (ctx.get("sources") or {}).get("macro") if isinstance(ctx, dict) else None
    if not isinstance(m, dict) or not m.get("active"):
        return None
    ends = [num(e.get("until_epoch")) for e in (m.get("active_events") or []) if isinstance(e, dict) and num(e.get("until_epoch")) is not None]
    return max(0.0, max(ends) - now) if ends else None


def macro_until_text(left_s, now):
    """' · до HH:MM UTC (ещё N мин)' для карточки состояния: замечание про окно новостей не выглядит вечным."""
    if left_s is None:
        return ""
    return " · до %s UTC (ещё %d мин)" % (time.strftime("%H:%M", time.gmtime(now + left_s)), int(left_s // 60) + (1 if left_s % 60 else 0))


def _risk_off_level(root, reasons):
    """risk_off только из-за макроокна в paper: предупреждение (входы режутся, не запрещены); остальное и live: красный."""
    h = read_json(paths.exec_health_file(root=root))
    paper = isinstance(h, dict) and "paper" in (h.get("env"), h.get("profile")) and not h.get("observe")
    only_macro = bool(reasons) and all(str(r).startswith("macro_window") for r in reasons)
    return "warn" if paper and only_macro else "bad"


def health_info(root, now, mkt, trades_open, sess_view):
    hl = read_json(paths.runtime("health.json", root=root))
    ctx = read_json(paths.runtime("context.json", root=root))
    hot = read_json(paths.runtime("hot.json", root=root))
    warns = []

    def w(level, text):
        warns.append({"level": level, "text": text})

    out = {"feed": None, "context": None, "hot": None}
    if isinstance(hl, dict):
        age = _age(hl.get("ts_epoch"), hl.get("ts"), now)
        srcs = {}
        for name, s in (hl.get("sources") or {}).items():
            if isinstance(s, dict):
                srcs[name] = {"state": s.get("state"), "connected": s.get("connected"), "last_msg_age_s": s.get("last_msg_age_s"),
                              "reconnects": s.get("reconnects"), "errors": s.get("errors"), "last_error": s.get("last_error")}
        out["feed"] = {"age_s": age, "running": hl.get("running"), "feed_ok": hl.get("feed_ok"), "uptime_s": hl.get("uptime_s"),
                       "sources": srcs}
        if age is None or age > DEAD_FEED_S or hl.get("running") is False:
            w("bad", "Фид не пишет (health.json устарел или остановлен): цены могут быть старыми")
        elif age > STALE_FEED_S:
            w("warn", "Фид отстаёт: последняя запись %.0f с назад" % age)
        if hl.get("feed_ok") is False and age is not None and age <= DEAD_FEED_S:
            w("warn", "Фид сообщает feed_ok=false")
        for name, s in srcs.items():
            if s.get("state") not in (None, "ok"):
                w("warn", "Источник %s: состояние %s" % (name, s.get("state")))
    else:
        w("bad", "Нет runtime/health.json: фид не запущен")
    if isinstance(ctx, dict):
        age = _age(ctx.get("ts_epoch"), ctx.get("ts"), now)
        out["context"] = {"age_s": age, "risk_off": ctx.get("risk_off"), "risk_off_reasons": ctx.get("risk_off_reasons") or [],
                          "degraded_sources": ctx.get("degraded_sources") or [],
                          "macro_active": bool((ctx.get("sources") or {}).get("macro", {}).get("active")),
                          "macro_until_s": macro_until_s(ctx, now),
                          "sources_ok": {k: bool(v.get("ok")) for k, v in (ctx.get("sources") or {}).items() if isinstance(v, dict)}}
        if age is None or age > STALE_CONTEXT_S:
            w("warn", "Контекст (context.json) устарел: %s" % ("нет времени" if age is None else "%.0f мин назад" % (age / 60.0)))
        if ctx.get("risk_off"):
            w(_risk_off_level(root, ctx.get("risk_off_reasons") or []), "risk_off: %s%s" % (", ".join(str(x) for x in (ctx.get("risk_off_reasons") or ["без причины"])),
                                                                               macro_until_text(out["context"]["macro_until_s"], now)))
        if ctx.get("degraded_sources"):
            w("warn", "Деградировавшие источники контекста: %s" % ", ".join(str(x) for x in ctx["degraded_sources"]))
    else:
        w("warn", "Нет runtime/context.json")
    if isinstance(hot, dict):
        out["hot"] = {"age_s": _age(hot.get("ts_epoch"), hot.get("ts"), now)}
        if out["hot"]["age_s"] is not None and out["hot"]["age_s"] > STALE_HOT_S:
            w("warn", "Сканер горячих монет отстаёт: %.0f с" % out["hot"]["age_s"])
    for t in trades_open:
        c = t.get("calc") or {}
        if not c.get("has_price"):
            w("warn", "Нет цены для %s (%s): PnL не посчитан" % (t["id"], t.get("sym")))
        elif c.get("price_stale"):
            w("warn", "Цена %s устарела (%.0f с, %s)" % (t.get("sym"), c.get("price_age_s") or 0, c.get("price_src") or "?"))
        if not t.get("sl"):
            w("bad", "У открытой сделки %s нет стопа в журнале" % t["id"])
    if sess_view:
        if sess_view["block_reasons"]:
            w("warn", "Сессия: входы запрещены (%s)" % ", ".join(sess_view["block_reasons"]))
    elif trades_open:
        w("info", "Есть открытые позиции, но активной сессии нет")
    return out, warns


def _safe_room(root, trades, prof, port, active, now):
    """Блок «Запас до лимитов»; без risk_profile.json (пустой корень) или при мусоре в нём блока нет (None), страница показывает пустое состояние."""
    if not prof or not prof.get("session_types"):
        return None
    try:
        try:
            from tb.core import risklevels
            prof = risklevels.effective(prof, root)           # как в guard.evaluate: пресет уровня риска поверх базы
        except Exception:  # noqa: BLE001
            pass
        return risk_room(root, trades, prof, port, active, now)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _pending_live(root):
    """Все ждущие live-заявки (как pending.orders(mode='live') в guard: без фильтра по площадке)."""
    d = read_json(paths.state("pending_orders.json", root=root))
    recs = d.get("orders") if isinstance(d, dict) and isinstance(d.get("orders"), dict) else {}
    return [r for r in recs.values() if isinstance(r, dict) and r.get("id") and r.get("mode") == "live"]


def _room_row(id_, label, limit, used, room, unit, help_, extra=None):
    frac = max(0.0, min(1.0, room / limit)) if limit and limit > 0 else 0.0
    level = "bad" if (room <= 0 or frac < 0.10) else "warn" if frac < 0.30 else "ok"
    r = {"id": id_, "label": label, "limit": limit, "used": used, "room": room, "frac": frac, "level": level, "unit": unit, "help": help_}
    r.update(extra or {})
    return r


def _paper_snapshot_open(root, journal_open):
    """Открытые бумажные позиции исполнителя: снимок runtime/exec_snapshot.json (env=paper). В журнал сделок такая позиция попадает только при закрытии, поэтому без снимка
    блок лимитов её не видит. -> (число, риск до стопа USDT) для позиций, которых нет среди открытых записей журнала (по монете). Не paper-снимок или нет файла: (0, 0.0)."""
    snap = read_json(paths.exec_snapshot_file(root=root))
    if not isinstance(snap, dict) or snap.get("env") != "paper":
        return 0, 0.0
    have = set(t.get("sym") for t in journal_open)
    n, risk = 0, 0.0
    for p in snap.get("positions") if isinstance(snap.get("positions"), list) else []:
        if not isinstance(p, dict) or p.get("sym") in have:
            continue
        n += 1
        size, avg, sl = num(p.get("size")), num(p.get("avg")), num(p.get("sl"))
        if size and avg and sl:
            risk += size * abs(avg - sl)
    return n, risk


def _paper_day(root):
    """Исполнитель в paper (по снимку): учёт запаса ведётся по бумажным сделкам, иначе по live."""
    snap = read_json(paths.exec_snapshot_file(root=root))
    return isinstance(snap, dict) and snap.get("env") == "paper"


def risk_room(root, trades, prof, port, active, now):
    """«Запас до лимитов»: сколько ещё можно торговать до ограничений риск-менеджмента. Числа считаются по тем же формулам, что реально
    блокируют вход (core/guard.py: day_state/check/evaluate, core/session.py: stats/verdict, core/portfolio.py: scaled_profile/dd_mult/perf_mult,
    core/learning.py: progress). Учёт по режиму исполнителя: в live только live-сделки (как в guard), в paper бумажные сделки и открытые бумажные позиции из снимка
    исполнителя (их нет в журнале до закрытия); сутки по UTC. Без символа: корреляционный потолок не входит."""
    paper_day = _paper_day(root)
    live = [t for t in trades if t.get("mode") == ("paper" if paper_day else "live")]
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    open_t = [t for t in live if t["status"] == "open"]
    closed_today = [t for t in live if t["status"] == "closed" and str(t.get("ts_close") or "").startswith(today)]
    snap_n, snap_risk = _paper_snapshot_open(root, open_t) if paper_day else (0, 0.0)
    pend = [] if paper_day else _pending_live(root)
    pend_risk = sum(num(r.get("risk_usdt"), 0.0) for r in pend)
    realized = sum(t.get("pnl_usdt") or 0.0 for t in closed_today)
    streak = 0
    for t in sorted(closed_today, key=lambda x: str(x.get("ts_close") or ""), reverse=True):
        if t.get("reason") == "sl":
            streak += 1
        else:
            break
    open_risk = sum(t.get("risk_usdt") or 0.0 for t in open_t) + pend_risk + snap_risk
    equity = portfolio.total(port)
    p2 = dict(prof)
    if prof.get("daily_loss_frac") is not None and equity > 0:       # portfolio.scaled_profile: лимит дня = доля текущего капитала
        p2["daily_loss_limit_usdt"] = round(prof["daily_loss_frac"] * equity, 4)
    reasons, rows = [], []
    L = num(p2.get("daily_loss_limit_usdt"), 0.0)
    daily_room = L + realized - open_risk
    rows.append(_room_row("daily", "Дневной лимит убытка", L, max(0.0, L - daily_room), daily_room, "USDT",
                          "Лимит дня = доля капитала (daily_loss_frac). Запас = лимит + результат закрытых сегодня (UTC; прибыль увеличивает запас, убыток уменьшает) − риск открытых позиций и ждущих лимитов. "
                          "Когда запас исчерпан или реализованный убыток дошёл до лимита, guard не даёт входить.", {"realized_today": realized, "open_risk": open_risk}))
    if realized <= -L:
        reasons.append("дневной лимит убытка достигнут (%.2f USDT)" % realized)
    if daily_room <= 0:
        reasons.append("нет остатка дневного лимита риска")
    mx = num(p2.get("max_open_positions"), 0)
    used_slots = len(open_t) + len(pend) + snap_n
    if mx:
        rows.append(_room_row("slots", "Слоты позиций", mx, used_slots, mx - used_slots, "шт.",
                              "Открытые позиции плюс ждущие лимитные заявки не могут превышать max_open_positions. Ждущий лимит занимает слот, потому что может исполниться в любой момент.",
                              {"open": len(open_t) + snap_n, "pending": len(pend)}))
        if used_slots >= mx:
            reasons.append("заняты все слоты (%d из %d)" % (used_slots, mx))
    ms = num(p2.get("max_consecutive_stops"), 0)
    if ms:
        rows.append(_room_row("stops", "Стопов подряд сегодня", ms, streak, ms - streak, "шт.",
                              "Серия закрытий по стопу за сегодня (UTC), прерывается любым другим выходом. При достижении max_consecutive_stops guard останавливает входы до конца суток."))
        if rows[-1]["level"] == "ok" and ms - streak <= 1 and ms > 1:   # остался один стоп до остановки: жёлтый независимо от доли
            rows[-1]["level"] = "warn"
        if streak >= ms:
            reasons.append("%d стопов подряд: входы остановлены до конца дня" % streak)
    sess_cfg, sess_room, perf, perf_n = {}, None, 1.0, None
    if active:
        try:
            sess_cfg = session_cfg(active.get("type"), prof)
        except (KeyError, TypeError):
            sess_cfg = {}
        rows_s = [t for t in trades if t.get("session") == active["id"]]
        s_closed = [t for t in rows_s if t["status"] == "closed"]
        s_pend = [r for r in _pending_live_all(root) if r.get("session") == active["id"]]
        s_real = sum(t.get("pnl_usdt") or 0.0 for t in s_closed)
        s_orisk = sum(t.get("risk_usdt") or 0.0 for t in rows_s if t["status"] == "open") + sum(num(r.get("risk_usdt"), 0.0) for r in s_pend) + snap_risk
        budget = num(active.get("budget_usdt"), 0.0)
        sess_room = budget + s_real - s_orisk
        rows.insert(0, _room_row("session", "Бюджет убытка сессии", budget, max(0.0, budget - sess_room), sess_room, "USDT",
                                 "Бюджет сессии минус её реализованный убыток и риск открытых и ждущих позиций. Исчерпан: guard и сессия запрещают новые входы.",
                                 {"realized": s_real, "open_risk": s_orisk}))
        if sess_room <= 0:
            reasons.append("бюджет убытка сессии исчерпан")
        mt = sess_cfg.get("max_trades")
        n_tr = len(rows_s) + len(s_pend) + snap_n
        if mt:
            rows.append(_room_row("trades", "Сделок в сессии", mt, n_tr, mt - n_tr, "шт.", "Лимит числа сделок для этого типа сессии (открытые, закрытые и ждущие лимиты)."))
            if n_tr >= mt:
                reasons.append("лимит сделок сессии (%d из %d)" % (n_tr, mt))
        try:
            perf, perf_n = portfolio.perf_mult(active["type"], prof, trades)
        except (KeyError, TypeError, ValueError):
            perf, perf_n = 1.0, None
    else:
        reasons.append("нет активной сессии")
    # множитель по результатам зависит от типа сессии (guard берёт его у активной сессии). Без сессии показываем тип по умолчанию (intraday) и по каждому типу честно.
    perf_by_type = {}
    for t_ in ("intraday", "scalp"):
        if t_ in (prof.get("session_types") or {}):
            try:
                perf_by_type[t_] = portfolio.perf_mult(t_, prof, trades)[0]
            except (KeyError, TypeError, ValueError):
                pass
    stype, assumed = (active.get("type") if active else None), False
    if stype == "day":                       # сессия-день: горизонт у каждого плана свой, для карточки берём intraday как расчётный
        stype, assumed = "intraday", True
    if not active:
        stype = "intraday" if "intraday" in perf_by_type else next(iter(perf_by_type), None)
        assumed = stype is not None
        if stype is not None:
            perf = perf_by_type[stype]
            try:
                perf_n = portfolio.perf_mult(stype, prof, trades)[1]
            except (KeyError, TypeError, ValueError):
                perf_n = None
    lm = None
    lc = read_json(paths.state("learning.json", root=root))
    if isinstance(lc, dict) and lc.get("active") and num(lc.get("max_trades")) and num(lc.get("max_loss_usdt")):
        lrows = [t for t in live if t.get("venue") == lc.get("venue") and str(t.get("ts_open") or "") >= str(lc.get("start") or "")]
        l_real = sum(t.get("pnl_usdt") or 0.0 for t in lrows if t["status"] == "closed")
        lp = [r for r in pend if lc.get("venue") == "bybit"]
        l_room = lc["max_loss_usdt"] + l_real - sum(t.get("risk_usdt") or 0.0 for t in lrows if t["status"] == "open") - sum(num(r.get("risk_usdt"), 0.0) for r in lp)
        n_l = len(lrows) + len(lp)
        rows.append(_room_row("learn_loss", "Учебный режим: убыток", lc["max_loss_usdt"], max(0.0, lc["max_loss_usdt"] - l_room), l_room, "USDT", "Отдельный лимит убытка учебного режима (state/learning.json)."))
        rows.append(_room_row("learn_trades", "Учебный режим: сделки", lc["max_trades"], n_l, lc["max_trades"] - n_l, "шт.", "Лимит числа сделок учебного режима."))
        lm = l_room
        if l_room <= 0:
            reasons.append("учебный режим: лимит убытка исчерпан")
        if n_l >= lc["max_trades"]:
            reasons.append("учебный режим: набрано %d из %d сделок" % (n_l, lc["max_trades"]))
    dm = 1.0
    try:
        dm = portfolio.dd_mult(port, prof)
    except (KeyError, TypeError, ValueError):
        pass
    if dm <= 0:
        reasons.append("остановка по просадке (%.1f%% от максимума)" % (100 * portfolio.drawdown(port)))
    # риск на следующую сделку (guard.check): equity * доля * (0.5..1.0 по score) * множители, затем не выше всех остатков
    frac = num(prof.get("risk_per_trade_frac"), 0.0)
    mult = dm * perf
    full = equity * frac * mult
    caps = [("дневной остаток", daily_room)]
    if sess_room is not None:
        caps.append(("бюджет сессии", sess_room))
    if lm is not None:
        caps.append(("учебный остаток", lm))
    bind = min(caps, key=lambda c: c[1])
    nxt = max(0.0, min(full, bind[1]))
    out_ok = not reasons
    reasons_all = reasons
    return {"allowed": out_ok, "reasons": reasons_all, "rows": rows,
            "next_risk": {"usdt": round(nxt, 4), "full": round(full, 4), "equity": equity, "frac": frac, "dd_mult": dm, "perf_mult": perf, "perf_n": perf_n,
                          "session_type": stype, "assumed_type": assumed, "perf_by_type": perf_by_type,
                          "limited_by": bind[0] if bind[1] < full else None,
                          "help": "Сколько риска (потеря при стопе с комиссиями) разрешит guard на следующий вход: капитал × доля риска на сделку × множитель просадки × множитель по результатам "
                                  "(при оценке score 1.0; при 0.5 это 75% суммы), но не больше остатков дневного лимита, бюджета сессии и учебного режима. Корреляционный потолок зависит от монеты и не учтён."}}


def _pending_live_all(root):
    """Все ждущие заявки любого режима (session.stats берёт pending.orders() без фильтров)."""
    d = read_json(paths.state("pending_orders.json", root=root))
    recs = d.get("orders") if isinstance(d, dict) and isinstance(d.get("orders"), dict) else {}
    return [r for r in recs.values() if isinstance(r, dict) and r.get("id")]


# что сделать для закрытия замечания (одна строка на узел): показывается в расшифровке индикатора «Есть замечания»
ACTIONS = {
    "scanner": "сервис scan: ./tb status; если не запущен, ./tb up --profile paper --detach",
    "feed": "сервис feed: ./tb logs feed; при необходимости ./tb down и ./tb up (после правок кода платформу перезапускает владелец)",
    "context": "сервис context (входит в наборы по умолчанию): ./tb logs context; публичные источники могут быть недоступны, это деградация, а не поломка",
    "session": "начать сессию кнопкой «Старт» на вкладке «Управление» или ./tb session start",
    "stops": "поставить стоп: ./tb bybit trade close или план через ./tb plan amend",
    "capital": "проверить читающий ключ (./tb setup --check); большое расхождение подтвердить: ./tb sync confirm",
}


def system_status(health, warns, scan, sess_view, opened, pend, root=None, now=None, sync=None):
    """Один индикатор состояния системы: level ok|warn|bad и разбивка по узлам (сканер, фид, контекст, сессия).
    Связь дашборда с API определяет сама страница (если ответ не пришёл, сервер ничего не скажет)."""
    items = []

    def add(id_, label, level, text):
        items.append({"id": id_, "label": label, "level": level, "text": text, "action": ACTIONS.get(id_, "") if level in ("warn", "bad") else ""})
    need_px = bool(opened or pend)
    if scan.get("running"):
        add("scanner", "Сканер", "ok", "работает, цены обновлены %.0f с назад" % scan["age_s"])
    elif need_px:
        add("scanner", "Сканер", "bad", "не запущен, а есть позиции или лимиты: цены устарели (%s)" % SCAN_HINT)
    else:
        add("scanner", "Сканер", "warn", "не запущен: " + SCAN_HINT)
    feed = health.get("feed")
    if not feed:
        add("feed", "Фид цен", "bad", "нет runtime/health.json: фид не запущен")
    elif feed.get("age_s") is None or feed["age_s"] > DEAD_FEED_S or feed.get("running") is False:
        add("feed", "Фид цен", "bad", "не пишет или остановлен")
    elif feed["age_s"] > STALE_FEED_S or feed.get("feed_ok") is False or any(s.get("state") not in (None, "ok") for s in feed["sources"].values()):
        add("feed", "Фид цен", "warn", "отстаёт или есть проблемный источник (%.0f с)" % feed["age_s"])
    else:
        add("feed", "Фид цен", "ok", "работает, запись %.0f с назад" % feed["age_s"])
    ctx = health.get("context")
    if not ctx:
        add("context", "Контекст рынка", "warn", "нет runtime/context.json")
    elif ctx.get("risk_off"):
        add("context", "Контекст рынка", _risk_off_level(root, ctx["risk_off_reasons"]),
            "risk_off: " + (", ".join(str(x) for x in ctx["risk_off_reasons"]) or "без причины") + macro_until_text(ctx.get("macro_until_s"), now))
    elif ctx.get("age_s") is None or ctx["age_s"] > STALE_CONTEXT_S or ctx.get("degraded_sources"):
        add("context", "Контекст рынка", "warn", "устарел или источники деградировали")
    else:
        add("context", "Контекст рынка", "ok", "актуален, risk_off нет")
    if sess_view and not sess_view["block_reasons"]:
        add("session", "Сессия", "ok", "%s идёт, входы разрешены" % sess_view["type"])
    elif sess_view:
        add("session", "Сессия", "warn", "входы запрещены: " + ", ".join(sess_view["block_reasons"]))
    elif opened:
        add("session", "Сессия", "warn", "активной сессии нет, а позиция открыта")
    else:
        add("session", "Сессия", "off", "нет активной (торговля не идёт)")
    if any(w["level"] == "bad" for w in warns if "стопа в журнале" in w["text"]):
        add("stops", "Стопы", "bad", "у открытой сделки нет стопа в журнале")
    if sync and sync.get("status") != "ok":
        add("capital", "Синхронизация капитала", "warn", {"no_key": "нет читающего ключа: баланс не обновляется", "mismatch": "большое расхождение с биржей, ждёт подтверждения",
                                                        "error": "ошибка чтения баланса, показан прежний (устарел)"}.get(sync.get("status"), "баланс устарел") + (": " + sync["reason"] if sync.get("reason") else ""))
    for n in (preflight_notes(root, now) if root is not None and now is not None else []):
        items.append({"id": "pf-" + n["id"], "label": "Проверка запуска %s" % n["id"], "level": n["level"],
                      "text": "%s (по последнему запуску дня, %d мин назад)" % (n["text"], n["age_s"] // 60), "action": n["action"]})
    order = {"bad": 3, "warn": 2, "ok": 1, "off": 0}
    top = max(items, key=lambda i: order[i["level"]])["level"]
    return {"level": top if top != "off" else "ok", "items": items}


# ---------------------------------------------------------------- сборка ответа
def _struct_regimes(root):
    """runtime/structure.json -> {SYM: режим по свечам 1ч}. Фид после рестарта 1 ч держит regime=unknown (EMA из тиков): подставляем режим структуры."""
    d = read_json(paths.runtime("structure.json", root=root))
    body = d.get("symbols") if isinstance(d, dict) and d.get("schema") == "tb.structure/1" and isinstance(d.get("symbols"), dict) else {}
    return {k: v.get("reg") for k, v in body.items() if isinstance(v, dict) and v.get("reg") in ("trend_up", "trend_down", "range", "storm")}


def _market_rows(mkt, struct_reg=None):
    rows = []
    for sym, s in (mkt.get("symbols") or {}).items():
        if not isinstance(s, dict):
            continue
        book = s.get("book") if isinstance(s.get("book"), dict) else {}
        rows.append({"sym": sym, "price": num(s.get("price")), "src": s.get("src"), "age_s": num(s.get("age_s")),
                     "prices_by_src": s.get("prices_by_src") or {}, "regime": s.get("regime"),
                     "ret_1m": num(s.get("ret_1m")), "ret_5m": num(s.get("ret_5m")), "ret_15m": num(s.get("ret_15m")),
                     "rv_5m": num(s.get("rv_5m")), "rv_1h": num(s.get("rv_1h")), "spread_bps": num(book.get("spread_bps")),
                     "imbalance": num(book.get("imbalance")), "funding_hourly": num(s.get("funding_hourly")),
                     "oi_chg_5m": num(s.get("oi_chg_5m")), "regime_1h": (struct_reg or {}).get(sym)})
    return rows


_HOT_KEYS = ("sym", "price", "score", "reasons", "tradable", "corr_group", "chg_24h_pct", "ret_5m_pct", "range_5m_pct",
             "turn_24h_musd", "turn_surge_5m", "spread_bps", "funding_pct", "lot_usdt", "max_leverage", "rank", "skip")


def hot_info(root, now):
    h = read_json(paths.runtime("hot.json", root=root))
    if not isinstance(h, dict):
        return {"available": False}

    def pick(rows):
        return [{k: r.get(k) for k in _HOT_KEYS if k in r} for r in (rows or []) if isinstance(r, dict)]
    return {"available": True, "ts": h.get("ts"), "age_s": _age(h.get("ts_epoch"), h.get("ts"), now), "env": h.get("env"),
            "warm": h.get("warm"), "stats": h.get("stats") or {}, "top": pick(h.get("top")), "blocked": pick(h.get("blocked"))}


def deriv_info(root, now):
    """Тепловая карта, ликвидации и ордерфлоу для вкладки «Рынок»: runtime/heat.json (пишет скан: те же тикеры, без запросов), runtime/liq.json (поток liqwatch) и runtime/flow.json
    (поток flowwatch: CVD, стакан, разрыв с Binance по небольшому набору монет). Только чтение."""
    out = {"heat": {"available": False}, "liq": {"available": False}, "flow": {"available": False}}
    h = read_json(paths.runtime("heat.json", root=root))
    if isinstance(h, dict) and isinstance(h.get("rows"), list):
        cols = [c for c in (h.get("cols") or []) if isinstance(c, str)]
        out["heat"] = {"available": True, "age_s": _age(h.get("ts_epoch"), h.get("ts"), now), "cols": cols,
                       "rows": [r for r in h["rows"][:100] if isinstance(r, dict) and isinstance(r.get("sym"), str)]}
    q = read_json(paths.runtime("liq.json", root=root))
    if isinstance(q, dict) and isinstance(q.get("syms"), list):
        out["liq"] = {"available": True, "age_s": _age(q.get("ts_epoch"), q.get("ts"), now), "totals": q.get("totals_1h") or {}, "n_total": q.get("n_total"),
                      "syms": [r for r in q["syms"][:60] if isinstance(r, dict) and isinstance(r.get("sym"), str)]}
    fl = read_json(paths.runtime("flow.json", root=root))
    if isinstance(fl, dict) and isinstance(fl.get("syms"), list):
        out["flow"] = {"available": True, "age_s": _age(fl.get("ts_epoch"), fl.get("ts"), now), "xex_ok": fl.get("xex_ok"),
                       "syms": [r for r in fl["syms"][:24] if isinstance(r, dict) and isinstance(r.get("sym"), str)]}
    return out


IDEAS_LIMIT = 80
AUTO_RECENT = 12


def _stamps(*ps):
    return tuple(_stamp(p) for p in ps)


def _trade_view(t, mkt, sp, mm):
    """Краткий итог связанной сделки журнала: статус, PnL (итоговый или текущий), R."""
    if t["status"] == "closed":
        return {"id": t["id"], "status": "closed", "pnl": t.get("pnl_usdt"), "r": t.get("r"), "reason": t.get("reason")}
    pr = resolve_price(mkt, sp, t.get("sym"), t.get("venue"))
    c = live_calc(dict(t), pr["price"], None, mm, stale=pr["stale"])
    return {"id": t["id"], "status": "open", "pnl": c.get("pnl") if c.get("has_price") else None, "r": c.get("r_now") if c.get("has_price") else None, "reason": None}


def _idea_expires(i):
    """Когда идея истечёт (epoch): ts_epoch + ttl_h (по умолчанию как у сторожа ideawatch); ttl 0 или нет времени записи = None."""
    ttl = num(i.get("ttl_h"))
    ttl = ideas_core.TTL_H_DEFAULT if ttl is None else ttl
    t0 = num(i.get("ts_epoch"))
    return t0 + ttl * 3600.0 if ttl > 0 and t0 is not None else None


def ideas_info(root=None, now=None, trades=None, mm=1.0):
    """Блок «Идеи и пропущенное»: идеи агента (статусы idea/watching/taken/skipped/expired/invalidated), авто-история сканера (auto_skipped) и статистика.
    Читает state/ideas.jsonl, state/hot_outcomes.jsonl, runtime/hot_pending.json и цены из runtime/*.json; на биржу не ходит."""
    now = time.time() if now is None else now
    ip, op_, pp = ideas_core.ideas_path(root), ideas_core.outcomes_path(root), ideas_core.pending_path(root)
    key = ("ideas-stats", ip, op_, pp, _stamps(ip, op_, pp, paths.config("risk_profile.json", root=root)))
    hit = _CACHE.get(key)
    if hit is None:
        for k in [k for k in _CACHE if k[0] == "ideas-stats" and k[1] == ip]:
            del _CACHE[k]
        stats = ideas_core.compute_stats(root)
        outs = ideas_core.idea_outcomes(root)
        auto = {}
        for r in ideas_core.read_rows(op_):
            if r.get("kind") != "event" or not r.get("id"):
                continue
            a = auto.setdefault(r["id"], {"id": r["id"], "sym": r.get("sym"), "type": r.get("type"), "dir": r.get("dir"), "ts0": r.get("ts0_epoch"),
                                          "range_5m_pct": r.get("range_5m_pct"), "h": {}})
            d = ideas_core.num(r.get("dir_pct"))
            a["h"][str(r.get("h"))] = {"ret_pct": r.get("ret_pct"), "dir_pct": d, "net_pct": None if d is None else d - (ideas_core.num(r.get("cost_pct")) or 0.0)}
        recent = sorted(auto.values(), key=lambda a: a["ts0"] or 0.0, reverse=True)[:AUTO_RECENT]
        hit = _CACHE[key] = (stats, outs, recent)
    stats, outs, recent = hit
    book = ideas_core.price_book(root, now)
    mkt = market_info(root, now)
    sp = scan_prices(root, now)
    tmap = {t["id"]: t for t in (trades if trades is not None else load_trades(root)[0])}
    rows = []
    for i in reversed(ideas_core.load_ideas(root)[-IDEAS_LIMIT:]):
        cur = book.get(i.get("sym"))
        p0 = num(i.get("price"))
        shift = 100.0 * (cur[0] / p0 - 1.0) if cur and p0 else None
        sign = {"long": 1.0, "short": -1.0}.get(i.get("side"))
        tr = tmap.get(i.get("trade")) if i.get("trade") else None
        lvl = num(i.get("level"))
        dist = 100.0 * (cur[0] / lvl - 1.0) if cur and lvl else None          # расстояние до уровня идеи: (цена/level-1)*100
        rows.append({"dist_level_pct": dist, "zone": dist is not None and abs(dist) <= ideawatch.ZONE_PCT and (i.get("status") or "idea") in ideas_core.ACTIVE,
                     "id": i["id"], "ts": i.get("ts"), "ts_epoch": i.get("ts_epoch"), "sym": i.get("sym"), "side": i.get("side"), "note": i.get("note"),
                     "level": i.get("level"), "stop": i.get("stop"), "tp": i.get("tp"), "tag": i.get("tag"), "status": i.get("status") or "idea",
                     "price": p0, "now_price": cur[0] if cur else None, "now_src": cur[2] if cur else None,
                     "now_age_s": max(0.0, now - cur[1]) if cur and cur[1] else None, "shift_pct": shift,
                     "shift_dir_pct": shift * sign if shift is not None and sign else None, "mark_notes": i.get("mark_notes") or [],
                     "ttl_h": num(i.get("ttl_h")), "expires_epoch": _idea_expires(i), "trade_id": i.get("trade"), "plan_id": i.get("plan"), "trade": _trade_view(tr, mkt, sp, mm) if tr else None,
                     "outcomes": {str(h): v for h, v in (outs.get(i["id"]) or {}).items()}})
    counts = {k: 0 for k in ideas_core.STATUSES}
    for i in ideas_core.load_ideas(root):
        counts[i.get("status") or "idea"] = counts.get(i.get("status") or "idea", 0) + 1
    counts["auto_skipped"] = stats["n_events"]
    return {"available": bool(rows or stats["n_outcomes"] or stats["pending"]), "counts": counts, "items": rows, "stats": stats, "auto_recent": recent,
            "pending": stats["pending"], "costs": stats["costs"]}


def pending_info(root, mode, venue, mkt, sp, now):
    """Ждущие лимитные входы из state/pending_orders.json (пишет venues/bybit_limit.py): цена лимита, текущая цена фида, расстояние, стоп/тейк, возраст и остаток ttl.
    Только чтение файла; мусор в записях пропускается."""
    d = read_json(paths.state("pending_orders.json", root=root))
    recs = d.get("orders") if isinstance(d, dict) and isinstance(d.get("orders"), dict) else {}
    rows = []
    for r in recs.values():
        if not isinstance(r, dict) or not r.get("sym") or r.get("side") not in ("long", "short") or num(r.get("price")) is None:
            continue
        if mode != "all" and (r.get("mode") or "live") != mode or venue not in ("all", "bybit"):
            continue
        px = num(r.get("price"))
        pr = resolve_price(mkt, sp, r["sym"], "bybit")
        cur, src = pr["price"], pr["src"]
        ts, exp = num(r.get("ts")), num(r.get("expires"))
        rows.append({"id": str(r.get("id")), "sym": r["sym"], "side": r["side"], "price": px, "qty": r.get("qty"), "sl": num(r.get("sl")), "tp": num(r.get("tp")),
                     "lev": num(r.get("lev")), "margin": num(r.get("margin")), "risk_usdt": num(r.get("risk_usdt")), "mode": r.get("mode") or "live",
                     "env": r.get("env"), "status": r.get("status"), "cancel_above": num(r.get("cancel_above")), "cancel_below": num(r.get("cancel_below")),
                     "cur": cur, "price_src": src, "price_age_s": pr["age_s"], "price_stale": pr["stale"], "dist_pct": (100.0 * (px / cur - 1.0)) if cur and px else None,
                     "age_s": max(0.0, now - ts) if ts is not None else None, "left_s": (exp - now) if exp is not None else None,
                     "ttl_min": num(r.get("ttl_min")), "idea": r.get("idea"), "session": r.get("session"), "ts": ts})
    return sorted(rows, key=lambda x: x["ts"] or 0.0)


def events_info(root, limit=25):
    evs = read_jsonl(paths.runtime("events.jsonl", root=root), tail=400)
    return [{"ts": e.get("ts"), "type": e.get("type"), "symbol": e.get("symbol"), "data": e.get("data")}
            for e in evs[-limit:]][::-1]


def build_state(root=None, mode="live", venue="all", now=None):
    """Полный ответ /api/state для выбранного режима и площадки (mode: live|paper|testnet|all, venue: platform|bybit|all)."""
    now = time.time() if now is None else now
    trades, tw = load_trades(root)
    prof = read_json(paths.config("risk_profile.json", root=root))
    prof = prof if isinstance(prof, dict) else {}
    mm = num((prof or {}).get("maintenance_margin_pct"), 1.0)
    mkt = market_info(root, now)
    sp = scan_prices(root, now)

    modes = sorted({t["mode"] for t in trades} | {"live", "paper"})      # paper всегда в списке: планы исполнителя в paper идут без записей в журнале сделок
    venues = sorted({t["venue"] for t in trades})

    def sel(t):
        return (mode in ("all", t["mode"])) and (venue in ("all", t["venue"]))
    shown = [t for t in trades if sel(t)]
    opened = [t for t in shown if t["status"] == "open"]
    closed = [t for t in shown if t["status"] == "closed"]
    for t in opened:
        pr = resolve_price(mkt, sp, t.get("sym"), t.get("venue"))
        t["calc"] = live_calc(t, pr["price"], None, mm, stale=pr["stale"])
        t["calc"]["price_src"] = pr["src"]
        t["calc"]["price_age_s"] = pr["age_s"]
        t["held_s"] = (now - t["ts_open_epoch"]) if t.get("ts_open_epoch") else None
    live_paths = path_live(root, opened)
    for t in opened:
        t["path_live"] = live_paths.get(t["id"])
    for t in opened:           # частичные закрытия уже зафиксировали часть результата: показываем и её, и остаток
        if t.get("realized_parts") is not None:
            t["calc"]["realized_parts"] = t["realized_parts"]
            t["calc"]["fees_parts"] = t.get("fees_parts") or 0.0
    priced = [t["calc"]["pnl"] + (t.get("realized_parts") or 0.0) - (t.get("fees_parts") or 0.0) for t in opened if t["calc"].get("has_price")]
    open_pnl = sum(priced) if priced else None

    p = read_json(paths.state("portfolio.json", root=root))
    p = p if isinstance(p, dict) and isinstance(p.get("venues"), dict) else {"venues": {}, "hwm": 0.0}
    account = {"equity": portfolio.total(p), "hwm": num(p.get("hwm"), 0.0), "drawdown": portfolio.drawdown(p),
               "venues": p["venues"], "snapshot_age_s": None, "ladder_mult": None}
    ages = [_age(None, v.get("ts"), now) for v in p["venues"].values() if isinstance(v, dict)]
    ages = [a for a in ages if a is not None]
    account["snapshot_age_s"] = min(ages) if ages else None
    account["sync"] = sync_info(root, now)
    if prof.get("drawdown_ladder") and prof.get("drawdown_halt") is not None:
        try:
            account["ladder_mult"] = portfolio.dd_mult(p, prof)
        except (KeyError, TypeError, ValueError):
            pass

    sessions = load_sessions(root)
    active = next((s for s in reversed(sessions) if not s.get("stopped")), None)
    sess_view = session_view(active, trades, prof, now) if active else None
    if sess_view and prof.get("perf_mult") and prof.get("probation_trades") is not None:
        try:
            sess_view["perf_mult"], sess_view["perf_n"] = portfolio.perf_mult(active["type"], prof, trades)
        except (KeyError, TypeError, ValueError):
            pass
    recent_sessions = [{"id": s["id"], "type": s.get("type"), "venue": s.get("venue"), "ts": s.get("ts"),
                        "stopped": s.get("stopped") if isinstance(s.get("stopped"), str) else None,
                        "thesis": s.get("thesis"), "stop_note": s.get("stop_note")} for s in sessions[-8:]][::-1]

    health, warns = health_info(root, now, mkt, opened, sess_view)
    pend = pending_info(root, mode, venue, mkt, sp, now)
    scan = {"ok": sp["scan"]["ok"], "age_s": sp["scan"]["age_s"], "n": sp["scan"]["n"], "hint": SCAN_HINT}
    scan["running"] = bool(scan["ok"] and scan["age_s"] is not None and scan["age_s"] <= SCAN_STALE_S)
    lacking = [t.get("sym") for t in opened if not (t["calc"].get("has_price") and not t["calc"].get("price_stale"))]
    lacking += [r["sym"] for r in pend if r["price_src"] is None or r["price_stale"]]
    if lacking and not scan["running"]:
        warns.append({"level": "warn", "text": "%s: %s (%s)" % (", ".join(sorted(set(lacking))), SCAN_HINT, "prices.json нет" if not scan["ok"] else "prices.json старый, %.0f с" % scan["age_s"])})
    if any(tw.values()):
        warns.append({"level": "info", "text": "Журнал: повторных open %d, close без open %d, битых строк %d" % (
            tw["dup_open"], tw["orphan_close"], tw["bad_rows"])})

    history = sorted(closed, key=lambda x: x.get("ts_close_epoch") or 0.0, reverse=True)
    learning = read_json(paths.state("learning.json", root=root))
    from tb.dash import model2          # отложенный импорт: model2 сам импортирует model
    try:
        v2 = model2.v2_state(root, now, mkt, sp, scan, sess_view)
    except Exception as e:   # слой v2 не должен ронять прежний дашборд
        v2 = {"available": False, "error": type(e).__name__}
    if isinstance(v2, dict):
        try:                                 # тепловая карта и ликвидации живут внутри v2: верхний уровень состояния для прежнего интерфейса не меняется
            v2["derivs"] = deriv_info(root, now)
        except Exception:
            v2["derivs"] = {"heat": {"available": False}, "liq": {"available": False}}
    return scrub({
        "now": now, "now_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "filter": {"mode": mode, "venue": venue, "modes": modes, "venues": venues},
        "account": account, "session": sess_view, "sessions": recent_sessions,
        "learning": learning if isinstance(learning, dict) else None,
        "open_trades": opened, "open_pnl": open_pnl, "pending": pend, "price_feed": scan,
        "metrics": summarize(closed), "curve": curve(closed),
        "breakdown": {k: breakdown(closed, k) for k in ("stype", "tag", "sym", "side", "reason", "venue", "err", "exp", "var", "setup")},
        "summary": pnl_summary(closed, open_pnl, now), "risk_room": _safe_room(root, trades, prof, p, active, now), "status": system_status(health, warns, scan, sess_view, opened, pend, root, now, account.get("sync")),
        "history": history[:HISTORY_LIMIT], "history_total": len(history),
        "market": {"age_s": mkt["age_s"], "ts": mkt.get("ts"), "ok": mkt["ok"], "rows": _market_rows(mkt, _struct_regimes(root))},
        "hot": hot_info(root, now), "ideas": ideas_info(root, now, trades, mm), "events": events_info(root), "health": health, "warnings": warns,
        "v2": v2,
    })
