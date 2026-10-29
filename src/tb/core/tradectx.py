"""Снимок рынка на момент входа (поле ctx в событии open журнала) и словарь тегов ошибок для разбора сделок.

snapshot(sym) читает ТОЛЬКО локальные файлы runtime/ (hot.json, prices.json, market.json, context.json), без сети и без записи.
Поля (отсутствующее не пишется; нет ни одного источника -> {}; сбой чтения -> {}, вход в журнал это не ломает):
  ts, price, price_src, range_5m_pct, range_15m_pct, ret_5m_pct, ret_15m_pct, turn_surge_5m (оборот 5м, x к норме 5 минут),
  turn_x_median (оборот 24ч, x к медиане рынка), chg_24h_pct, spread_bps, funding_pct (ставка фандинга, %), oi_chg_15m_pct,
  rank, score, tradable (из hot.json: топ-N и blocked; монеты вне топа имеют только цену/фид), regime, risk_off (market.json, context.json),
  age_s {hot, prices, market, context}: возраст каждого источника в секундах на момент снимка, src: список использованных источников,
  late_s: если запись open делается позже самого входа (sync заполнил лимит, ts_open раньше) - на сколько секунд снимок позже входа.
"""
import json
import os
import time

from tb import paths

# Теги ошибок для ./tb journal close --err / journal post --err. Тег = причина проигрыша ИЛИ оценка процесса.
ERR_TAGS = {
    "no-confirm": "вход без реакции цены на уровень (нет отказа/закрепления, лимит просто стоит на уровне)",
    "chase": "догоняющий вход: цена уже ушла, вход по движению, а не по уровню",
    "tight-stop": "стоп слишком близко: выбит шумом, цена потом пошла по тезису",
    "wide-stop": "стоп слишком далеко: убыток больше нужного, тейк недостижим по R",
    "wrong-direction": "направление тезиса неверно (цена ушла против без возврата)",
    "news-miss": "пропущена новость/событие (релиз, листинг, разблокировка, макро)",
    "size": "размер/плечо не по ситуации (слишком велик или мал для риска)",
    "fee-drag": "комиссии и проскальзывание съели прибыль (малый ход, маленький тейк)",
    "rule-break": "нарушено собственное правило (CLAUDE.md, TRADING.md, LESSONS.md)",
    "good-process-bad-luck": "процесс верный, результат плохой (допустимый минус)",
    "good": "хороший процесс (правила соблюдены, тезис и уровни осмысленны)",
}
RULE_VALUES = ("yes", "no", "partial")


def parse_err(s):
    """'no-confirm, chase' -> ['no-confirm','chase']; неизвестный тег -> ValueError (опечатка не должна тихо портить статистику)."""
    out = []
    for t in str(s or "").replace(";", ",").split(","):
        t = t.strip().lower()
        if not t:
            continue
        if t not in ERR_TAGS:
            raise ValueError("неизвестный тег ошибки %r; допустимые: %s" % (t, ", ".join(ERR_TAGS)))
        if t not in out:
            out.append(t)
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _base(sym):
    s = str(sym or "").upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def _age(d, now):
    ts = _num(d.get("ts_epoch")) if d else None
    return None if ts is None else round(max(0.0, now - ts), 1)


def _r(x, n=4):
    return None if x is None else round(x, n)


def snapshot(sym, now=None, ts_open_epoch=None, root=None):
    """-> dict ctx (может быть пустым). Никогда не бросает исключений."""
    try:
        return _snapshot(sym, time.time() if now is None else now, ts_open_epoch, root)
    except Exception:
        return {}


def _snapshot(sym, now, ts_open_epoch, root):
    base = _base(sym)
    hot = _read(paths.runtime("hot.json", root=root))
    pr = _read(paths.runtime("prices.json", root=root))
    mk = _read(paths.runtime("market.json", root=root))
    cx = _read(paths.runtime("context.json", root=root))
    ctx, src, age = {}, [], {}

    row = None
    if hot:
        for r in (hot.get("top") or []) + (hot.get("blocked") or []):
            if isinstance(r, dict) and _base(r.get("sym")) == base:
                row = r
                break
    if row:
        src.append("hot")
        age["hot"] = _age(hot, now)
        for k in ("range_5m_pct", "range_15m_pct", "ret_5m_pct", "ret_15m_pct", "turn_surge_5m", "turn_x_median", "chg_24h_pct",
                  "spread_bps", "funding_pct", "oi_chg_15m_pct", "score"):
            v = _num(row.get(k))
            if v is not None:
                ctx[k] = _r(v)
        if _num(row.get("rank")) is not None:
            ctx["rank"] = int(row["rank"])
        if isinstance(row.get("tradable"), bool):
            ctx["tradable"] = row["tradable"]

    cands = []            # (возраст, цена, источник): берём самую свежую
    if row and _num(row.get("price")):
        cands.append((age.get("hot"), _num(row["price"]), "hot.json"))
    if pr and isinstance(pr.get("prices"), dict) and _num(pr["prices"].get(base)):
        age["prices"] = _age(pr, now)
        src.append("prices")
        cands.append((age["prices"], _num(pr["prices"][base]), "prices.json"))
    m = ((mk or {}).get("symbols") or {}).get(base) if mk else None
    if isinstance(m, dict):
        src.append("market")
        age["market"] = _age(mk, now)
        if _num(m.get("price")):
            a = _num(m.get("age_s")) or 0.0
            cands.append(((age["market"] or 0.0) + a if age["market"] is not None else None, _num(m["price"]), "market.json"))
        if m.get("regime"):
            ctx["regime"] = str(m["regime"])
        # поля, которых нет в hot.json (монета вне топа), добираем из фида; единицы приводим к процентам
        for k, v, f in (("ret_5m_pct", m.get("ret_5m"), 100.0), ("ret_15m_pct", m.get("ret_15m"), 100.0)):
            if k not in ctx and _num(v) is not None:
                ctx[k] = _r(_num(v) * f)
        sp = _num((m.get("book") or {}).get("spread_bps")) if isinstance(m.get("book"), dict) else None
        if "spread_bps" not in ctx and sp is not None:
            ctx["spread_bps"] = _r(sp)
        fh = _num(m.get("funding_hourly"))
        if "funding_pct" not in ctx and fh is not None:
            ctx["funding_hourly_pct"] = _r(fh * 100.0, 5)
    if cands:
        known = [c for c in cands if c[0] is not None]
        a, p, s = min(known, key=lambda c: c[0]) if known else cands[0]
        ctx["price"], ctx["price_src"] = p, s
    if not ctx:
        return {}                     # контекст рынка (context.json) относится ко всем монетам: без данных по самой монете снимок пуст
    if cx and "risk_off" in cx:
        src.append("context")
        age["context"] = _age(cx, now)
        ctx["risk_off"] = bool(cx.get("risk_off"))
        if cx.get("risk_off_reasons"):
            ctx["risk_off_reasons"] = [str(x) for x in cx["risk_off_reasons"]][:5]
    ctx["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    ctx["age_s"] = {k: v for k, v in age.items() if v is not None}
    ctx["src"] = src
    if ts_open_epoch and now - ts_open_epoch > 60.0:
        ctx["late_s"] = round(now - ts_open_epoch)
    return ctx
