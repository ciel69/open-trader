"""Ретроспективная проверка памяти и сравнение периодов (08 п. 5, пункт 5; WP15b). Без LLM, без сети, только числа. ЧИСТАЯ логика (время только из данных).

`backtest`: закрытые сделки режима (участвующие в статистике) по времени.
  * walk_forward: для каждой сделки похожие ищутся ТОЛЬКО среди более ранних (as_of = её время); сравнивается знак результата, который предсказал бы «совет похожих
    случаев» (средний R похожих > 0), со знаком базовой частоты (средний R всех более ранних > 0), и Brier по вероятности плюса (доля плюсов похожих, сглаженная к
    базовой на prior_n псевдо-наблюдений). Разность Brier парная (положительная = совет точнее базовой), se и n показываются.
  * split: случаи делятся по времени на старые и новые (по умолчанию по медиане); для каждого нового похожие берутся из СТАРЫХ; совет «избегать» = у похожих
    n_closed >= min_similar и средний R < avoid_below_r. Считается, сколько R было бы сохранено, если бы советованные пропуски не были сделаны (сумма R избегаемых
    с обратным знаком), доля избегаемых, которые действительно закончились минусом, и средний R оставшихся против всех.
`compare_periods`: два периода (до/после включения памяти или любые) с разрезом по метке `mem`; формулировка «до/после, не A/B» (рынок мог измениться).
Предупреждение о малой выборке везде: INSUFFICIENT n<5, LOW_N n<30 (agg.warn_for). Слов-рекомендаций нет: факты и предупреждение.
"""
import math

from tb.memory import agg, cases, similar

DEFAULTS = {"min_train": 5, "min_similar": 3, "top_k": 5, "min_sim": 0.55, "avoid_below_r": 0.0, "prior_n": 2}
NOTE_BEFORE_AFTER = "до/после, не A/B: рынок и набор сделок между периодами различались"


def _bc(cfg):
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("backtest") or {})
    return out


def closed_trades(all_cases, cfg, mode=None):
    """Закрытые сделки с R, участвующие в статистике (не exclude_from_stats, не inj_suspect, не роль mechanics), режим mode, по возрастанию времени."""
    ex = tuple((cfg or {}).get("exclude_roles") or ())
    out = [c for c in all_cases if agg.closed_r(c) is not None and not c.get("exclude_from_stats") and "inj_suspect" not in (c.get("flags") or [])
           and (c.get("src") or {}).get("role") not in ex           # mem=off здесь остаётся: это те же сделки, поиск похожих их видит (как get_similar_cases)
           and (mode is None or (c.get("src") or {}).get("mode", "live") == mode) and similar.ts_epoch(c.get("ts")) is not None]
    out.sort(key=lambda c: (similar.ts_epoch(c["ts"]), c["case_id"]))
    return out


def advice(train, case, cfg, bc=None):
    """Совет похожих: поиск среди train строго до времени случая, только сделки. -> {n, mean_r, wins}; n = число похожих с R."""
    bc = bc or _bc(cfg)
    ts = similar.ts_epoch(case["ts"])
    q = {"features": dict((k, v) for k, v in similar.vector(case).items() if k != "side"), "side": (case.get("situation") or {}).get("side"),
         "mode": (case.get("src") or {}).get("mode", "live")}
    sel = similar.select(train, q, cfg, ts, k=bc["top_k"], min_sim=bc["min_sim"], scope="all", rules="any", types=("trade",), max_age_days=0, as_of=ts)
    rs = [agg.closed_r(h["case"]) for h in sel["hits"] if agg.closed_r(h["case"]) is not None]
    return {"n": len(rs), "mean_r": (sum(rs) / len(rs)) if rs else None, "wins": sum(1 for r in rs if r > 0)}


def _r(x, n=4):
    return None if x is None else round(x, n)


def _mean_se(xs):
    if not xs:
        return None, None
    m = sum(xs) / len(xs)
    if len(xs) < 3:
        return m, None
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return m, sd / math.sqrt(len(xs))


def walk_forward(trades, cfg, bc=None):
    bc = bc or _bc(cfg)
    rows = []
    for i, c in enumerate(trades):
        train = trades[:i]
        if len(train) < bc["min_train"]:
            continue
        a = advice(train, c, cfg, bc)
        if a["n"] < bc["min_similar"]:
            continue
        rs = [agg.closed_r(t) for t in train]
        p0 = sum(1 for r in rs if r > 0) / len(rs)
        r0 = sum(rs) / len(rs)
        pm = (a["wins"] + bc["prior_n"] * p0) / (a["n"] + bc["prior_n"])
        y = 1.0 if agg.closed_r(c) > 0 else 0.0
        rows.append({"hit_mem": (a["mean_r"] > 0) == (y == 1.0), "hit_base": (r0 > 0) == (y == 1.0), "d_brier": (p0 - y) ** 2 - (pm - y) ** 2,
                     "b_mem": (pm - y) ** 2, "b_base": (p0 - y) ** 2})
    n = len(rows)
    out = {"n_trades": len(trades), "n_eval": n, "covered_share": _r(n / len(trades), 3) if trades else None}
    if n:
        dm, dse = _mean_se([r["d_brier"] for r in rows])
        out.update({"acc_mem": _r(sum(r["hit_mem"] for r in rows) / n, 3), "acc_base": _r(sum(r["hit_base"] for r in rows) / n, 3),
                    "brier_mem": _r(sum(r["b_mem"] for r in rows) / n), "brier_base": _r(sum(r["b_base"] for r in rows) / n),
                    "d_brier_mean": _r(dm), "d_brier_se": _r(dse)})
    warn = agg.warn_for(n, cfg)
    out.update({"warn": warn, "warn_text": agg.WARN_TEXT[warn]})
    return out


def split(trades, cfg, split_ts=None, bc=None):
    """Старые/новые по времени -> результат «совета похожих из старых» на новых. split_ts: ISO или None (медиана по числу сделок)."""
    bc = bc or _bc(cfg)
    if len(trades) < 2:
        warn = agg.warn_for(0, cfg)
        return {"n_old": len(trades), "n_new": 0, "n_covered": 0, "warn": warn, "warn_text": agg.WARN_TEXT[warn]}
    cut = similar.ts_epoch(split_ts) if split_ts else None
    if cut is None:
        cut = similar.ts_epoch(trades[len(trades) // 2]["ts"])
    old = [c for c in trades if similar.ts_epoch(c["ts"]) < cut]
    new = [c for c in trades if similar.ts_epoch(c["ts"]) >= cut]
    kept, avoided, covered = [], [], 0
    for c in new:
        a = advice(old, c, cfg, bc)
        if a["n"] >= bc["min_similar"]:
            covered += 1
            if a["mean_r"] < bc["avoid_below_r"]:
                avoided.append(agg.closed_r(c))
                continue
        kept.append(agg.closed_r(c))
    allr = kept + avoided
    out = {"split_ts": cases.iso(cut), "n_old": len(old), "n_new": len(new), "n_covered": covered, "n_avoid": len(avoided),
           "avoid_lost_share": _r(sum(1 for r in avoided if r < 0) / len(avoided), 3) if avoided else None,
           "r_saved_if_avoided": _r(-sum(avoided)) if avoided else 0.0,
           "mean_r_all": _r(sum(allr) / len(allr)) if allr else None, "mean_r_kept": _r(sum(kept) / len(kept)) if kept else None,
           "mean_r_avoided": _r(sum(avoided) / len(avoided)) if avoided else None}
    warn = agg.warn_for(len(avoided) if avoided else covered, cfg)
    out.update({"warn": warn, "warn_text": agg.WARN_TEXT[warn]})
    return out


def run(all_cases, cfg, mode=None, split_ts=None):
    """-> {"mode","walk_forward","split"}: полный отчёт ретро-теста по режиму (None = все режимы вместе)."""
    trades = closed_trades(all_cases, cfg, mode)
    bc = _bc(cfg)
    return {"mode": mode, "n_trades": len(trades), "params": dict(bc), "walk_forward": walk_forward(trades, cfg, bc), "split": split(trades, cfg, split_ts, bc)}


# ------------------------------------------------------------------ сравнение периодов
def _period(all_cases, cfg, lo, hi, mode):
    ex = tuple((cfg or {}).get("exclude_roles") or ())
    lo_t, hi_t = similar.ts_epoch(lo) if lo else None, similar.ts_epoch(hi) if hi else None
    sel = []
    for c in all_cases:           # mem=off здесь НЕ исключается: сравнивается именно периоды с памятью и без неё
        if agg.closed_r(c) is None or c.get("exclude_from_stats") or "inj_suspect" in (c.get("flags") or []) or (c.get("src") or {}).get("role") in ex:
            continue
        if mode is not None and (c.get("src") or {}).get("mode", "live") != mode:
            continue
        t = similar.ts_epoch(c.get("ts"))
        if t is None or (lo_t is not None and t < lo_t) or (hi_t is not None and t >= hi_t):
            continue
        sel.append(c)
    s = agg.summarize(sel, None, cfg)
    s["by_mem"] = dict((r["value"], dict((k, r[k]) for k in ("n", "n_closed", "mean_r", "win", "rule_followed_no_share", "warn"))) for r in agg.breakdown(sel, "mem", None, cfg))
    return dict((k, s[k]) for k in ("n", "n_closed", "mean_r", "se", "win", "rule_followed_no_share", "top_err", "warn", "by_mem"))


def compare_periods(all_cases, cfg, a_from=None, a_to=None, b_from=None, b_to=None, mode=None, split_ts=None):
    """Период A против B. split_ts вместо четырёх границ: A = до, B = с этого момента. -> {a, b, diff_mean_r, se, warn, note}."""
    if split_ts:
        a_from, a_to, b_from, b_to = None, split_ts, split_ts, None
    a, b = _period(all_cases, cfg, a_from, a_to, mode), _period(all_cases, cfg, b_from, b_to, mode)
    diff = se = None
    if a["mean_r"] is not None and b["mean_r"] is not None:
        diff = round(b["mean_r"] - a["mean_r"], 4)
        if a["se"] is not None and b["se"] is not None:
            se = round(math.sqrt(a["se"] ** 2 + b["se"] ** 2), 4)
    n_min = min(a["n_closed"], b["n_closed"])
    warn = agg.warn_for(n_min, cfg)
    return {"mode": mode, "a": a, "b": b, "diff_mean_r": diff, "diff_se": se, "warn": warn, "warn_text": agg.WARN_TEXT[warn], "note": NOTE_BEFORE_AFTER}
