"""Агрегат по отобранным случаям и предупреждения о малой выборке (08 п. 3.3-3.4). ЧИСТАЯ логика.

В агрегате только факты и предупреждение (INSUFFICIENT n<5, LOW_N n<30, OK_NO_MULTIPLE n>=30 без поправки), без слов-рекомендаций.
warn считается по n_closed (закрытые сделки с R), а при их отсутствии по n отобранных. Базовая частота base_r = средний R всех закрытых
подходящих случаев того же режима (чтобы +0.2R не казалось много при общем +0.3R). Разрезы by=...: поправка Бонферрони на число групп.
"""
import math

from tb.core import expstats

WARN_TEXT = {"INSUFFICIENT": "нет статистики; это только примеры", "LOW_N": "n<30, выводов не делаем", "OK_NO_MULTIPLE": "n>=30; без поправки на множественные сравнения"}
BY_KEYS = ("err", "setup", "side", "model", "confirmed", "coin_class", "regime", "utc_session", "mode", "mem")


def warn_for(n, cfg):
    w = cfg.get("warn") or {}
    if n < w.get("insufficient", 5):
        return "INSUFFICIENT"
    return "LOW_N" if n < w.get("low_n", 30) else "OK_NO_MULTIPLE"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    h = len(s) // 2
    return s[h] if len(s) % 2 else (s[h - 1] + s[h]) / 2.0


def closed_r(c):
    """R закрытой сделки или None (idea/skip/open/без R не считаются)."""
    r = c.get("result") or {}
    return r["r"] if c.get("type") == "trade" and r.get("status") == "closed" and r.get("r") is not None else None


def base_rate(cases, mode=None, exclude_roles=()):
    """Средний R по всем закрытым случаям режима (учитываются только участвующие в статистике)."""
    xs = [closed_r(c) for c in cases if closed_r(c) is not None and not c.get("exclude_from_stats") and (c.get("src") or {}).get("role") not in exclude_roles
          and (mode is None or (c.get("src") or {}).get("mode", "live") == mode)]
    return {"base_r": round(_mean(xs), 3) if xs else None, "base_n": len(xs)}


def _r4(x):
    return None if x is None else round(x, 4)


def summarize(cases, base_r=None, cfg=None):
    """Список свёрнутых случаев -> агрегат (см. модуль)."""
    cfg = cfg or {}
    rs = [closed_r(c) for c in cases if closed_r(c) is not None]
    n_closed = len(rs)
    mean = _mean(rs)
    sd = _sd(rs)
    se = sd / math.sqrt(n_closed) if sd is not None and n_closed >= 3 else None
    mae = [(c["result"].get("mae_r")) for c in cases if closed_r(c) is not None and c["result"].get("mae_r") is not None]
    mfe = [(c["result"].get("mfe_r")) for c in cases if closed_r(c) is not None and c["result"].get("mfe_r") is not None]
    errs = {}
    for c in cases:
        for t in ((c.get("tags") or {}).get("err") or []):
            errs[t] = errs.get(t, 0) + 1
    n = len(cases)
    top_err = [[t, round(v / n, 2)] for t, v in sorted(errs.items(), key=lambda kv: (-kv[1], kv[0]))[:3]] if n else []
    rf = [(c.get("tags") or {}).get("rule_followed") for c in cases]
    rf = [x for x in rf if x in ("yes", "no", "partial")]
    wi = [c["whatif"]["net_dir_pct"] for c in cases if c.get("type") in ("skip", "idea") and isinstance(c.get("whatif"), dict) and c["whatif"].get("net_dir_pct") is not None]
    warn = warn_for(n_closed if n_closed else n, cfg)
    out = {"n": n, "n_closed": n_closed, "mean_r": _r4(mean), "se": _r4(se), "win": round(sum(1 for r in rs if r > 0) / n_closed, 3) if n_closed else None,
           "median_r": _r4(_median(rs)), "mean_mae_r": _r4(_mean(mae)), "mean_mfe_r": _r4(_mean(mfe)), "top_err": top_err,
           "rule_followed_no_share": round(sum(1 for x in rf if x == "no") / len(rf), 3) if rf else None,
           "skip_whatif": ({"n": len(wi), "mean_net_dir_pct": _r4(_mean(wi)), "correct_share": round(sum(1 for x in wi if x <= 0) / len(wi), 3)} if wi else None),
           "base_r": base_r, "warn": warn, "warn_text": WARN_TEXT[warn]}
    return out


def group_key(c, by):
    s = c.get("situation") or {}
    if by == "err":
        return list((c.get("tags") or {}).get("err") or []) or ["нет"]
    if by == "model":
        return [(c.get("src") or {}).get("model") or "нет"]
    if by == "mode":
        return [(c.get("src") or {}).get("mode") or "live"]
    if by == "mem":
        return [(c.get("src") or {}).get("mem") or "unknown"]       # on|off по тегу mem.* плана (WP15b); нет тега = unknown
    if by == "confirmed":
        v = s.get("confirmed")
        return ["unknown" if v is None else str(v).lower()]
    return [str(s.get(by) if s.get(by) is not None else "нет")]


def breakdown(cases, by, base_r=None, cfg=None, alpha=0.05):
    """Разрез по ключу by: строки {value, ...summarize}; p (t-критерий против base_r) и поправка Бонферрони на число групп."""
    if by not in BY_KEYS:
        raise ValueError("by: %s" % ", ".join(BY_KEYS))
    groups = {}
    for c in cases:
        for key in group_key(c, by):
            groups.setdefault(key, []).append(c)
    rows = []
    k = max(1, len(groups))
    adj = expstats.bonferroni(k, alpha)
    for key in sorted(groups):
        row = summarize(groups[key], base_r, cfg)
        row["value"] = key
        p = None
        if row["se"] and base_r is not None and row["mean_r"] is not None and row["n_closed"] >= 3:
            p = expstats.t_two_sided_p((row["mean_r"] - base_r) / row["se"], row["n_closed"] - 1)
        row.update({"p": None if p is None else round(p, 4), "k": k, "alpha_adj": round(adj, 4), "below_alpha": (p is not None and p < adj)})
        if row["warn"] == "OK_NO_MULTIPLE" and k > 1:
            row["warn"], row["warn_text"] = "OK_MULTIPLE", "n>=30; поправка Бонферрони на %d групп учтена в alpha_adj" % k
        rows.append(row)
    rows.sort(key=lambda r: (-r["n"], str(r["value"])))
    return rows


def build_report(sel, all_cases, cfg, by=None):
    """sel = результат similar.select; all_cases = все случаи (для базовой частоты). -> агрегат + служебные поля."""
    hits = sel["hits"]
    cs = [h["case"] for h in hits]
    ex = tuple((cfg or {}).get("exclude_roles") or ())
    base = base_rate(all_cases, sel.get("mode"), ex)
    out = summarize(cs, base["base_r"], cfg)
    out.update({"base_n": base["base_n"], "min_sim_used": sel["min_sim_used"], "avg_sim": sel["avg_sim"], "coverage": sel["coverage"], "mode": sel.get("mode"),
                "scope": sel.get("scope"), "k": sel["k"]})
    if by:
        out["by"] = {by: breakdown(cs, by, base["base_r"], cfg)}
    return out
