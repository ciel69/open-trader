"""Вычисление условий плана (словарь: core/plan_schema.COND_KINDS; 01 п. 1.3, 06 п. 6.9). Владелец: WP3.

Чистые функции: время и рынок приходят аргументами (`view`: MarketView, секунды epoch), вход не мутируется.

Память условия `st` (словарь, хранится исполнителем как `rt.entry.cond_state`):
  st["already"][path] = True|False   price_beyond с allow_already=false: True = на взводе цена уже была за уровнем (ждём возврата);
  st["seq"][path]     = {"step": k, "started": ts_s}   шаг seq и время выполнения первого шага.
`path` = положение узла в дереве: "c" (корень), "c.0", "c.1.2" (дети all/any), "c.s0" (шаги seq).
Узлы вычисляются ВСЕ (без короткого замыкания), поэтому память листьев обновляется на каждом вызове.

Контекст ctx: {"armed_ms", "entered_ms", "pos": {...rt.pos}|None, "targets": {...rt.targets}, опц. "side", "stale_s" (по умолчанию 10 с),
"stateless" (True: price_beyond ведёт себя как allow_already=true, память не нужна: invalidation и manage)}.
База свечей: entered_ms, если есть позиция, иначе armed_ms: считаются только свечи, начавшиеся НЕ РАНЬШЕ базы (история не срабатывает).

Трёхзначная логика внутри: True / False / None (нет данных, дыра, устарела цена). Наружу None = «не выполнено»; `not` от None
остаётся None (отсутствие данных не превращается в сигнал).

Границы: price_beyond нестрогий (>=, <=); закрытие свечи строгое (close > level / close < level); прокол с допуском 1e-9.
"""
import copy
import math

from tb.logic.candles import tf_min

DEFAULT_STALE_S = 10.0
_EPS = 1e-9


def _f(x):
    """-> float | None (конечное число)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ge(a, b):
    return a >= b - _EPS * abs(b)


def _le(a, b):
    return a <= b + _EPS * abs(b)


def _fmt(x):
    v = _f(x)
    return "?" if v is None else ("%g" % v)


def _tf_ru(tf):
    s = str(tf)
    return s[:-1] + {"m": "м", "h": "ч"}.get(s[-1:], s[-1:]) if s and not s.isdigit() else s + "м"


def last_price(view):
    """-> float | None: последняя цена снимка (> 0, конечная)."""
    v = _f(getattr(view, "last", None))
    return v if v is not None and v > 0 else None


def _fresh_price(view, ctx):
    """-> цена | None, если цены нет или она старше ctx["stale_s"]."""
    px = last_price(view)
    if px is None:
        return None
    lim = _f((ctx or {}).get("stale_s"))
    lim = DEFAULT_STALE_S if lim is None else lim
    return px if view.price_age() <= lim else None


def _base_ms(ctx):
    ctx = ctx or {}
    if ctx.get("pos") and ctx.get("entered_ms"):
        return ctx["entered_ms"]
    return ctx.get("entered_ms") or ctx.get("armed_ms")


def pos_side(pos, ctx=None):
    """-> +1 (long) | -1 (short) | None. Сторона из ctx["side"], pos["side"], иначе из положения начального стопа относительно входа."""
    for src in ((ctx or {}).get("side"), (pos or {}).get("side")):
        if src in ("long", "buy", "Buy"):
            return 1
        if src in ("short", "sell", "Sell"):
            return -1
    avg, st = _f((pos or {}).get("avg")), _f((pos or {}).get("initial_stop"))
    if avg is None or st is None or avg == st:
        return None
    return 1 if st < avg else -1


def r_multiple(pos, price, side=None):
    """-> float | None: side*(price-avg)/r_unit. r_unit = pos["r_unit"] либо |avg-initial_stop|. Нет данных = None.

    >>> r_multiple({"avg": 100.0, "initial_stop": 99.0, "r_unit": 1.0}, 102.0)
    2.0
    >>> r_multiple({"avg": 100.0, "initial_stop": 101.0}, 99.0)
    1.0
    """
    if not pos:
        return None
    avg, px = _f(pos.get("avg")), _f(price)
    if avg is None or px is None or avg <= 0 or px <= 0:
        return None
    ru = _f(pos.get("r_unit"))
    if ru is None or ru <= 0:
        st = _f(pos.get("initial_stop"))
        ru = abs(avg - st) if st is not None else None
    if ru is None or ru <= 0:
        return None
    s = side if side in (1, -1) else pos_side(pos)
    if s is None:
        return None
    return s * (px - avg) / ru


# ---------------------------------------------------------------- листья
def _book_tf(cond, view):
    book = getattr(view, "book", None)
    tf = tf_min(cond.get("tf"))
    return (book, tf) if book is not None and tf else (None, None)


def _last_closed_after(cond, view, ctx):
    """-> (candle|None, why): последняя подтверждённая свеча tf, начавшаяся не раньше базы."""
    book, tf = _book_tf(cond, view)
    base = _base_ms(ctx)
    if book is None:
        return None, "нет свечей"
    if base is None:
        return None, "нет базы времени"
    lc = book.last_closed(tf, 1)
    if not lc or lc[-1].start < base:
        return None, "ждём первую свечу после взвода"
    return lc[-1], ""


def _eval_leaf(cond, view, st, ctx, path):
    """-> (True|False|None, why)."""
    kind = cond.get("kind")
    ctx = ctx or {}
    if kind == "price_beyond":
        px = _fresh_price(view, ctx)
        lv = _f(cond.get("level"))
        if px is None or lv is None:
            return None, "нет свежей цены"
        beyond = _ge(px, lv) if cond.get("dir") == "above" else _le(px, lv)
        if cond.get("allow_already") or ctx.get("stateless"):
            return beyond, ""
        al = st.setdefault("already", {})
        prev = al.get(path)
        if not beyond:
            al[path] = False
            return False, ""
        if prev is None:
            al[path] = True          # на момент первой оценки цена уже за уровнем: нужно сначала вернуться
            return False, "цена уже за уровнем, ждём возврата"
        return (prev is False), ("ждём возврата" if prev else "")
    if kind == "candle_close":
        book, tf = _book_tf(cond, view)
        base, lv = _base_ms(ctx), _f(cond.get("level"))
        if book is None or base is None or lv is None:
            return None, "нет свечей"
        try:
            n = max(1, min(5, int(cond.get("n", 1))))
        except (TypeError, ValueError):
            n = 1
        cs = book.closed_since(tf, base)
        if len(cs) < n:
            return None, "свечей после взвода %d из %d" % (len(cs), n)
        cs = cs[-n:]
        step = tf * 60000
        if any(b.start - a.start != step for a, b in zip(cs, cs[1:])):
            return None, "дыра в свечах"
        above = cond.get("dir") == "above"
        return all((c.c > lv) if above else (c.c < lv) for c in cs), ""
    if kind == "candle_reject":
        c, why = _last_closed_after(cond, view, ctx)
        lv = _f(cond.get("level"))
        if c is None or lv is None:
            return None, why
        w = _f(cond.get("wick_min_pct")) or 0.0
        if cond.get("dir") == "short":
            return (_ge(c.h, lv * (1 + w / 100.0)) and c.c < lv), ""
        return (_le(c.l, lv * (1 - w / 100.0)) and c.c > lv), ""
    if kind == "candle_body":
        c, why = _last_closed_after(cond, view, ctx)
        if c is None:
            return None, why
        up = cond.get("dir") == "up"
        if (c.c > c.o) != up or c.c == c.o:
            return False, ""
        body = abs(c.c - c.o) / c.o * 100.0
        if not _ge(body, _f(cond.get("min_body_pct")) or 0.0):
            return False, ""
        pos_min = _f(cond.get("min_close_pos")) or 0.0
        if pos_min > 0:
            rng = c.h - c.l
            if rng <= 0:
                return False, ""
            pos = (c.c - c.l) / rng if up else (c.h - c.c) / rng
            if not _ge(pos, pos_min):
                return False, ""
        return True, ""
    if kind == "volume_spike":
        book, tf = _book_tf(cond, view)
        if book is None:
            return None, "нет свечей"
        if cond.get("on") == "live":
            c, base = book.forming(tf), _base_ms(ctx)
            if c is None or base is None or c.start < base:
                return None, "нет формирующейся свечи после взвода"
        else:
            c, why = _last_closed_after(cond, view, ctx)
            if c is None:
                return None, why
        try:
            lb = int(cond.get("lookback", 20))
        except (TypeError, ValueError):
            lb = 20
        med = book.vol_median(tf, lb, before_ms=c.start)
        mult = _f(cond.get("mult"))
        if med is None or med <= 0 or mult is None:
            return None, "мало истории для медианы объёма"
        d = cond.get("dir", "any")
        if (d == "up" and not c.c > c.o) or (d == "down" and not c.c < c.o):
            return False, ""
        return _ge(c.vol, mult * med), ""
    if kind == "price_in_zone":
        px = _fresh_price(view, ctx)
        lo, hi = _f(cond.get("low")), _f(cond.get("high"))
        if px is None or lo is None or hi is None:
            return None, "нет свежей цены"
        return lo <= px <= hi, ""
    if kind == "spread_bps":
        bid, ask, lim = _f(view.bid), _f(view.ask), _f(cond.get("lte"))
        if _fresh_price(view, ctx) is None or bid is None or ask is None or lim is None or bid <= 0 or ask < bid:
            return None, "нет стакана"
        return _le((ask - bid) / ((ask + bid) / 2.0) * 1e4, lim), ""
    if kind == "pnl_r":
        px = _fresh_price(view, ctx)
        r = r_multiple(ctx.get("pos"), px, pos_side(ctx.get("pos"), ctx)) if px is not None else None
        if r is None:
            return None, "нет позиции или цены"
        ok = True
        for k, f in (("gte", lambda a, b: _ge(a, b)), ("gt", lambda a, b: a > b + _EPS),
                     ("lte", lambda a, b: _le(a, b)), ("lt", lambda a, b: a < b - _EPS)):
            if k in cond:
                v = _f(cond[k])
                if v is None:
                    return None, "неверный порог"
                ok = ok and f(r, v)
        return ok, ""
    if kind == "elapsed":
        base = _base_ms(ctx)
        m = _f(cond.get("min"))
        if base is None or m is None or view.now is None:
            return None, "нет базы времени"
        return (view.now * 1000.0 - base) / 60000.0 >= m - _EPS, ""
    if kind == "target_filled":
        t = (ctx.get("targets") or {}).get(cond.get("target"))
        if not t:
            return None, "нет цели"
        qty, fq = _f(t.get("qty")), _f(t.get("filled_qty"))
        return (t.get("state") == "filled") or bool(qty and fq is not None and qty > 0 and fq >= qty - _EPS), ""
    if kind == "near_target":
        t = (ctx.get("targets") or {}).get(cond.get("target"))
        px = _fresh_price(view, ctx)
        avg, tp, fr = _f((ctx.get("pos") or {}).get("avg")), _f((t or {}).get("price")), _f(cond.get("frac"))
        if px is None or avg is None or tp is None or fr is None or tp == avg:
            return None, "нет позиции, цели или цены"
        return (px - avg) / (tp - avg) >= fr - _EPS, ""
    return None, "неизвестный вид условия %r" % (kind,)


def _combine_all(rs):
    if not rs:
        return None
    if any(r is False for r, _ in rs):
        return False
    return None if any(r is None for r, _ in rs) else True


def _combine_any(rs):
    if not rs:
        return None
    if any(r is True for r, _ in rs):
        return True
    return None if any(r is None for r, _ in rs) else False


def _ev(cond, view, st, ctx, path):
    """-> (True|False|None, why); st мутируется (это рабочая копия)."""
    if not isinstance(cond, dict):
        return None, "условие не словарь"
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        items = cond.get(key) or []
        rs = [_ev(ch, view, st, ctx, "%s.%d" % (path, i)) for i, ch in enumerate(items)]
        res = (_combine_all if key == "all" else _combine_any)(rs)
        why = next((w for r, w in rs if r is not True and w), "")
        return res, why
    if "not" in cond:
        r, w = _ev(cond["not"], view, st, ctx, path + ".n")
        return (None if r is None else (not r)), w
    if cond.get("kind") == "seq":
        return _ev_seq(cond, view, st, ctx, path)
    return _eval_leaf(cond, view, st, ctx, path)


def _ev_seq(cond, view, st, ctx, path):
    steps = cond.get("steps") or []
    if not steps:
        return None, "seq без шагов"
    seqs = st.setdefault("seq", {})
    cur = dict(seqs.get(path) or {"step": 0, "started": None})
    if cur["step"] >= len(steps):
        return True, ""                                   # защёлка: seq выполнен
    within = _f(cond.get("within_min"))
    if cur["step"] > 0 and within is not None and cur["started"] is not None and view.now - cur["started"] > within * 60.0:
        cur = {"step": 0, "started": None}                 # слишком долго после первого шага: сброс
    # за один вызов продвигается не более одного шага: шаги строго по порядку во времени
    r, why = _ev(steps[cur["step"]], view, st, ctx, "%s.s%d" % (path, cur["step"]))
    if r is True:
        if cur["step"] == 0:
            cur["started"] = view.now
        cur["step"] += 1
    seqs[path] = cur
    if cur["step"] >= len(steps):
        return True, ""
    return False, why or ("шаг %d из %d" % (cur["step"] + 1, len(steps)))


def eval_cond(cond, view, st, ctx):
    """-> (bool, st2). Не мутирует cond, view, st, ctx; st2 = обновлённая память (см. заголовок модуля).

    >>> from tb.logic.candles import MarketView
    >>> v = MarketView("SOL", 101.0, 100.9, 101.1, 101.0, 10.0, None, 10.5)
    >>> eval_cond({"kind": "price_beyond", "dir": "above", "level": 100.0, "allow_already": True}, v, {}, {})[0]
    True
    >>> eval_cond({"kind": "price_beyond", "dir": "above", "level": 100.0}, v, {}, {})[0]   # уже за уровнем на взводе
    False
    """
    st2 = copy.deepcopy(st) if st else {}
    try:
        r, _ = _ev(cond, view, st2, ctx or {}, "c")
    except (TypeError, ValueError, KeyError, AttributeError, ZeroDivisionError):
        r = None                                           # мусор в условии = «не выполнено», не исключение
    return r is True, st2


# ---------------------------------------------------------------- описание для дашборда
def _text(cond):
    if not isinstance(cond, dict):
        return "?"
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        return "(" + (" И " if key == "all" else " ИЛИ ").join(_text(c) for c in cond.get(key) or []) + ")"
    if "not" in cond:
        return "НЕ " + _text(cond["not"])
    k = cond.get("kind")
    ab = {"above": "выше", "below": "ниже"}.get(cond.get("dir"), "?")
    if k == "seq":
        return "последовательно: " + " -> ".join(_text(c) for c in cond.get("steps") or [])
    if k == "price_beyond":
        return "цена %s %s" % (ab, _fmt(cond.get("level")))
    if k == "candle_close":
        return "%s закрытие %s %s (n=%s)" % (_tf_ru(cond.get("tf")), ab, _fmt(cond.get("level")), cond.get("n", 1))
    if k == "candle_reject":
        return "%s прокол %s и закрытие обратно (%s)" % (_tf_ru(cond.get("tf")), _fmt(cond.get("level")),
                                                         "шорт" if cond.get("dir") == "short" else "лонг")
    if k == "candle_body":
        return "%s свеча %s, тело >= %s%%" % (_tf_ru(cond.get("tf")), "вверх" if cond.get("dir") == "up" else "вниз",
                                              _fmt(cond.get("min_body_pct", 0)))
    if k == "volume_spike":
        return "%s объём >= %sx медианы (%s)" % (_tf_ru(cond.get("tf")), _fmt(cond.get("mult")),
                                                 "живая" if cond.get("on") == "live" else "по закрытию")
    if k == "price_in_zone":
        return "цена в зоне %s..%s" % (_fmt(cond.get("low")), _fmt(cond.get("high")))
    if k == "spread_bps":
        return "спред <= %s бп" % _fmt(cond.get("lte"))
    if k == "pnl_r":
        parts = ["%s %s" % ({"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}[o], _fmt(cond[o])) for o in ("gte", "gt", "lte", "lt") if o in cond]
        return "результат %sR" % " и ".join(parts)
    if k == "elapsed":
        return "прошло >= %s мин" % _fmt(cond.get("min"))
    if k == "target_filled":
        return "цель %s исполнена" % cond.get("target")
    if k == "near_target":
        return "цена прошла %s%% пути к %s" % (_fmt(_f(cond.get("frac")) * 100 if _f(cond.get("frac")) is not None else None), cond.get("target"))
    return "условие %r" % (k,)


def _first_open_leaf(cond, view, st, ctx, path):
    """Первый невыполненный лист в порядке обхода (для seq: текущий шаг)."""
    if not isinstance(cond, dict):
        return None
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        for i, ch in enumerate(cond.get(key) or []):
            r, _ = _ev(ch, view, copy.deepcopy(st), ctx, "%s.%d" % (path, i))
            if r is not True:
                return _first_open_leaf(ch, view, st, ctx, "%s.%d" % (path, i))
        return None
    if "not" in cond:
        return None
    if cond.get("kind") == "seq":
        steps = cond.get("steps") or []
        k = ((st.get("seq") or {}).get(path) or {}).get("step", 0)
        return _first_open_leaf(steps[k], view, st, ctx, "%s.s%d" % (path, k)) if k < len(steps) else None
    return cond


def _candle_eta(tf, now):
    t = tf_min(tf)
    if not t or now is None:
        return None
    p = t * 60.0
    return int(p - (now % p))


def describe(cond, view, st, ctx):
    """-> {"met":bool,"text":str,"dist_pct":float|None,"eta_s":int|None} для дашборда.

    dist_pct: оставшееся расстояние от цены до уровня первого невыполненного листа в % (0.0 если условие выполнено);
    eta_s: секунд до закрытия текущей свечи для свечных условий или до наступления `elapsed`.
    """
    ctx = ctx or {}
    met, _ = eval_cond(cond, view, st, ctx)
    out = {"met": met, "text": _text(cond), "dist_pct": 0.0 if met else None, "eta_s": None}
    if met:
        return out
    try:
        r, why = _ev(cond, view, copy.deepcopy(st) if st else {}, ctx, "c")
        if why:
            out["text"] += " [%s]" % why
        leaf = _first_open_leaf(cond, view, st or {}, ctx, "c")
    except (TypeError, ValueError, KeyError, AttributeError, ZeroDivisionError):
        return out
    if not leaf:
        return out
    px, k = last_price(view), leaf.get("kind")
    lv = _f(leaf.get("level"))
    if k in ("price_beyond", "candle_close", "candle_reject") and px and lv:
        out["dist_pct"] = round(abs(lv - px) / px * 100.0, 4)
    elif k == "price_in_zone" and px:
        lo, hi = _f(leaf.get("low")), _f(leaf.get("high"))
        if lo is not None and hi is not None:
            out["dist_pct"] = round(0.0 if lo <= px <= hi else min(abs(px - lo), abs(px - hi)) / px * 100.0, 4)
    if k in ("candle_close", "candle_reject", "candle_body", "volume_spike"):
        out["eta_s"] = _candle_eta(leaf.get("tf"), view.now)
    elif k == "elapsed":
        base, m = _base_ms(ctx), _f(leaf.get("min"))
        if base is not None and m is not None and view.now is not None:
            out["eta_s"] = max(0, int(m * 60 - (view.now - base / 1000.0)))
    return out
