"""Статистика экспериментов (04_paper_dash.md п. 4.2). Владелец: WP7.

Источник: state/trades.jsonl (plan_id, exp, var, setup, hyp, plan_rev, mode; теги exp:/var:/setup: как запасной источник, те же правила, что
`journal analyze --by`) и state/decisions.jsonl (разрез по правилам). Режимы НЕ смешиваются: группы всегда делятся по `mode`
(paper / live / testnet), поэтому бумажная статистика не попадает в живую.

Правила честности (ref/landscape.md п. 3, 10): при n < 30 строка помечена `*` и не несёт p-значения; считается число сравнений K
(число групп в запросе) и порог Бонферрони alpha/K; `compare` (Уэлч) требует n >= 30 в обеих группах и по умолчанию «не значимо»;
группы с разными ревизиями правил помечаются (`mixed_rules`) и не получают вердикта. Модуль ничего не меняет и не выносит громких выводов.
Без сети и без биржевого кода. Время только из данных.
"""
import copy
import hashlib
import json
import math
import re

from tb import paths
from tb.core import decisions as _decisions

MIN_N = 30
ALPHA = 0.05
GROUP_BY = ("exp", "variant", "setup", "hyp", "rule", "symbol", "tf_session", "agent", "model", "mem", "blackout")
MODES = ("paper", "live", "testnet", "all")
RULE_REASONS = ("be", "trail", "timeout", "invalidation", "rule")      # закрытия «по правилам»
SHADOW_ALLOWED_RISK_DOWN = ("score", "size_mult")
PAPER_MAX_ACTIVE = 20
RE_SHADOW_PARENT = re.compile(r"^(.*)-S[1-9]$")


# ---------------------------------------------------------------- загрузка

def _tag(tags, pref):
    for tg in tags or []:
        if isinstance(tg, str) and tg.startswith(pref + ":"):
            return tg[len(pref) + 1:]
    return None


def _dot_tag(tags, pref):
    """Теги шлюза вида `agent.<id>`, `model.<профиль>` (WP11-C2, п. 6) -> значение или None."""
    for tg in tags or []:
        if isinstance(tg, str) and tg.startswith(pref + "."):
            return tg[len(pref) + 1:]
    return None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _norm(t):
    """Закрытая сделка журнала -> плоская запись для статистики."""
    tags = t.get("tags") or []
    risk = _num(t.get("risk_usdt"))
    pnl = _num(t.get("pnl_usdt"))
    r = _num(t.get("r"))
    if r is None and pnl is not None and risk:
        r = pnl / risk
    path = t.get("path") if isinstance(t.get("path"), dict) else {}
    plan_id = t.get("plan_id") or _tag(tags, "plan")
    parent = t.get("parent_plan")
    if not parent and isinstance(plan_id, str):
        m = RE_SHADOW_PARENT.match(plan_id)
        parent = m.group(1) if m else plan_id           # живой/родительский план = сам себе родитель (пара по сигналу)
    return {"id": t.get("id"), "plan_id": plan_id, "parent": parent, "plan_rev": t.get("plan_rev"),
            "rules_key": t.get("prereg_hash") or t.get("rules_hash") or t.get("plan_rev"),
            "exp": t.get("exp") or _tag(tags, "exp"), "var": t.get("var") or _tag(tags, "var"),
            "setup": t.get("setup") or _tag(tags, "setup"), "hyp": t.get("hyp"),
            "agent": _dot_tag(tags, "agent"), "model": _dot_tag(tags, "model"), "mem": _dot_tag(tags, "mem") or _tag(tags, "mem"),
            "blackout": "blackout" if "blackout" in tags else "обычные",      # вход в макро-окне (paper, blackout_mode=warn): отдельный срез
            "mode": t.get("mode") or "live", "sym": t.get("sym"), "stype": t.get("stype") or t.get("session"), "side": t.get("side"),
            "r": r, "pnl": pnl, "fees": _num(t.get("fees_usdt")) or 0.0, "risk": risk, "reason": t.get("reason"),
            "mae_r": _num(path.get("mae_r")), "mfe_r": _num(path.get("mfe_r")),
            "ts_open": t.get("ts"), "ts_close": t.get("ts_close"), "shadow": "shadow" in tags, "rules": []}


def rules_by_plan(root=None):
    """-> {plan_id: [rule_id]} по decisions.jsonl: сработавшие правила (kind=rule, res.ok). Нет файла = {}."""
    out = {}
    try:
        recs = _decisions.read(0, None, 0, root)
    except Exception:
        return out
    for r in recs:
        if r.get("kind") != "rule" or not r.get("plan"):
            continue
        res = r.get("res")
        if isinstance(res, dict) and res.get("ok") is False:
            continue
        why = r.get("why") or ""
        rid = why[5:] if isinstance(why, str) and why.startswith("RULE:") else ((r.get("args") or {}).get("rule") or r.get("what"))
        if rid and rid not in out.setdefault(r["plan"], []):
            out[r["plan"]].append(rid)
    return out


def _excluded_ids(root=None):
    """Сделки, результат которых искажён сбоем платформы, а не рынком: state/exclude_trades.json {"ids": [...]} (правит владелец/агент вручную, журнал не меняется)."""
    try:
        with open(paths.state("exclude_trades.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return set(x for x in (d.get("ids") or []) if isinstance(x, str)) if isinstance(d, dict) else set()
    except (OSError, ValueError):
        return set()


def load_trades(mode="all", root=None, with_rules=True):
    """-> [закрытые сделки] из trades.jsonl, отфильтрованные по mode (paper|live|testnet|all). Битые строки пропускаются; события
    add/part/adjust/post не нужны (итог берётся из close). Нет файла = []."""
    if mode not in MODES:
        raise ValueError("mode: %s" % "|".join(MODES))
    path = paths.state("trades.jsonl", root=root)
    opened, order = {}, []
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return []
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not r.get("id"):
                continue
            ev = r.get("ev")
            if ev == "open":
                opened[r["id"]] = dict(r)
                order.append(r["id"])
            elif ev == "close" and r["id"] in opened:
                opened[r["id"]].update(r)
    out = []
    skip = _excluded_ids(root)
    for i in order:
        t = opened[i]
        if i in skip or not t.get("ts_close") or _num(t.get("pnl_usdt")) is None:
            continue
        n = _norm(t)
        if mode == "all" or n["mode"] == mode:
            out.append(n)
    if with_rules and out:
        rb = rules_by_plan(root)
        for n in out:
            n["rules"] = list(rb.get(n["plan_id"], []))
    return out


def load_meta(root=None):
    """config/experiments.json (необязательный): {exp: {hypothesis, prereg, started, min_n}}. Нет файла или мусор = {}."""
    try:
        with open(paths.config("experiments.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return dict((k, v) for k, v in d.items() if isinstance(v, dict)) if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- математика (stdlib)

def _betacf(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betai(a, b, x):
    """Регуляризованная неполная бета I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t, df):
    """Двусторонний p-value распределения Стьюдента."""
    if df is None or df <= 0 or t is None:
        return None
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(p, df):
    """Квантиль Стьюдента для p in (0.5, 1) (бисекция по точной функции распределения)."""
    lo, hi = 0.0, 1.0
    while t_two_sided_p(hi, df) > 2.0 * (1.0 - p) and hi < 1e7:
        hi *= 2.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if t_two_sided_p(mid, df) > 2.0 * (1.0 - p):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _mean_sd(xs):
    n = len(xs)
    if not n:
        return None, None
    m = sum(xs) / n
    if n < 2:
        return m, None
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def bonferroni(k, alpha=ALPHA):
    """Порог значимости с поправкой Бонферрони на k сравнений."""
    return alpha / max(1, int(k))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- группы

def _keys(t, by):
    """Значения группировки сделки (список: разрез `rule` даёт несколько групп на сделку)."""
    if by == "exp":
        v = [t["exp"]]
    elif by == "variant":
        v = ["%s/%s" % (t["exp"], t["var"]) if t["exp"] and t["var"] else (t["var"] or t["exp"])]
    elif by == "setup":
        v = [t["setup"]]
    elif by == "hyp":
        v = [t["hyp"]]
    elif by == "symbol":
        v = [t["sym"]]
    elif by == "tf_session":
        v = [t["stype"]]
    elif by in ("agent", "model", "mem", "blackout"):
        v = [t.get(by)]
    elif by == "rule":
        v = t["rules"] or ["(нет)"]
    else:
        raise ValueError("by: %s" % "|".join(GROUP_BY))
    return [str(x) if x not in (None, "") else "(нет)" for x in v]


def _row(key, mode, rows, min_n):
    n = len(rows)
    rs = [t["r"] for t in rows if t["r"] is not None]
    pnls = [t["pnl"] for t in rows]
    wins = [t for t in rows if t["pnl"] > 0]
    gl = -sum(p for p in pnls if p <= 0)
    gw = sum(p for p in pnls if p > 0)
    m, sd = _mean_sd(rs)
    se = sd / math.sqrt(len(rs)) if sd is not None else None
    fees = sum(t["fees"] for t in rows)
    gross = sum(abs(p) for p in pnls)
    loss = [t for t in rows if t["pnl"] <= 0]
    cap = [t["r"] / t["mfe_r"] for t in wins if t["r"] is not None and t["mfe_r"] and t["mfe_r"] > 0]
    revs = sorted(set(str(t["rules_key"]) for t in rows if t["rules_key"] is not None))
    row = {"key": key, "mode": mode, "n": n, "wins": len(wins), "win_rate": len(wins) / n if n else None,
           "n_r": len(rs), "avg_r": m, "sd_r": sd, "se_r": se,
           "pf": (gw / gl) if gl > 0 else None, "pf_inf": bool(gl <= 0 and gw > 0),
           "pnl": sum(pnls), "expectancy_usdt": sum(pnls) / n if n else None,
           "fees": fees, "fees_share": (fees / gross) if gross > 0 else None,
           "mae_r_win": _avg([t["mae_r"] for t in wins]), "mfe_r_win": _avg([t["mfe_r"] for t in wins]),
           "mae_r_loss": _avg([t["mae_r"] for t in loss]), "mfe_r_loss": _avg([t["mfe_r"] for t in loss]),
           "rule_close_share": (sum(1 for t in rows if t["reason"] in RULE_REASONS) / n) if n else None,
           "mfe_capture": _avg(cap), "revs": revs, "mixed_rules": len(revs) > 1,
           "small": n < min_n, "mark": "*" if n < min_n else "", "p": None, "below_alpha": None}
    if n >= min_n and se and se > 0 and len(rs) > 1:
        row["p"] = t_two_sided_p(m / se, len(rs) - 1)         # H0: средний R = 0
    return row


def group_stats(trades, by="exp", min_n=MIN_N, alpha=ALPHA):
    """-> [{"key","mode","n","win_rate","avg_r","se_r","pf","fees","small","mark",...,"k","alpha_adj","below_alpha"}] по группам.

    Группы делятся по (ключ, mode): живое и бумажное не смешиваются. k = число групп в запросе; alpha_adj = alpha / k (Бонферрони).
    p-значение (H0: средний R = 0) считается только при n >= min_n; below_alpha = p < alpha_adj, но это НЕ вердикт."""
    groups = {}
    for t in trades:
        for k in _keys(t, by):
            groups.setdefault((k, t["mode"]), []).append(t)
    rows = [_row(k, mode, g, min_n) for (k, mode), g in sorted(groups.items())]
    k = len(rows)
    adj = bonferroni(k, alpha)
    for r in rows:
        r["k"], r["alpha_adj"] = k, adj
        if r["p"] is not None:
            r["below_alpha"] = r["p"] < adj
    return rows


# ---------------------------------------------------------------- сравнение вариантов

def _welch(xs, ys):
    ma, sa = _mean_sd(xs)
    mb, sb = _mean_sd(ys)
    if sa is None or sb is None:
        return None
    va, vb = sa * sa / len(xs), sb * sb / len(ys)
    se = math.sqrt(va + vb)
    if se <= 0:
        return {"diff": ma - mb, "se": 0.0, "df": None, "t": None, "p": None}
    df = (va + vb) ** 2 / (va * va / (len(xs) - 1) + vb * vb / (len(ys) - 1))
    t = (ma - mb) / se
    return {"diff": ma - mb, "se": se, "df": df, "t": t, "p": t_two_sided_p(t, df)}


def _ci(diff, se, df, level):
    if se is None or df is None:
        return None
    q = t_quantile(1.0 - (1.0 - level) / 2.0, df)
    return [diff - q * se, diff + q * se]


def compare(trades, exp, a, b, min_n=MIN_N, k=1, alpha=ALPHA):
    """Разность средних R вариантов a и b эксперимента exp (Уэлч) с доверительным интервалом и честной оговоркой.

    -> {"status": "ok|insufficient_n|no_data|mixed_rules", "significant": bool, ...}. significant=True только при n >= min_n в обеих
    группах, одной ревизии правил и p < alpha/k (Бонферрони, k = число сравнений, заявленных заранее); иначе всегда False.
    Если сделки есть у обоих вариантов с общим родительским планом, добавляется парное сравнение `paired`."""
    a, b = str(a).lower(), str(b).lower()
    ra = [t for t in trades if t["exp"] == exp and str(t["var"]).lower() == a and t["r"] is not None]
    rb = [t for t in trades if t["exp"] == exp and str(t["var"]).lower() == b and t["r"] is not None]
    adj = bonferroni(k, alpha)
    out = {"exp": exp, "a": a, "b": b, "n_a": len(ra), "n_b": len(rb), "k": k, "alpha": alpha, "alpha_adj": adj, "min_n": min_n,
           "significant": False, "status": "ok", "ci95": None, "ci_adj": None, "p": None, "diff": None, "paired": None,
           "modes": sorted(set(t["mode"] for t in ra + rb)), "notes": []}
    if not ra or not rb:
        out["status"] = "no_data"
        out["notes"].append("нет закрытых сделок у %s" % ("обоих вариантов" if not ra and not rb else (a if not ra else b)))
        return out
    xs, ys = [t["r"] for t in ra], [t["r"] for t in rb]
    ma, _ = _mean_sd(xs)
    mb, _ = _mean_sd(ys)
    out.update({"mean_a": ma, "mean_b": mb, "diff": ma - mb})
    if len(out["modes"]) > 1:
        out["notes"].append("режимы различаются (%s): модели исполнения разные, разность смещена" % ",".join(out["modes"]))
    revs = [sorted(set(str(t["rules_key"]) for t in g if t["rules_key"] is not None)) for g in (ra, rb)]
    if len(revs[0]) > 1 or len(revs[1]) > 1:
        out["status"] = "mixed_rules"
        out["notes"].append("внутри варианта смешаны ревизии правил (%s | %s): сравнивать можно только одну ревизию" % (",".join(revs[0]), ",".join(revs[1])))
    if len(ra) < min_n or len(rb) < min_n:
        if out["status"] == "ok":
            out["status"] = "insufficient_n"
        out["notes"].append("n=%d и n=%d, нужно >= %d в обеих группах: вывода нет" % (len(ra), len(rb), min_n))
    w = _welch(xs, ys)
    if w:
        out.update({"se": w["se"], "df": w["df"], "t": w["t"], "p": w["p"]})
        out["ci95"] = _ci(w["diff"], w["se"], w["df"], 0.95)
        out["ci_adj"] = _ci(w["diff"], w["se"], w["df"], 1.0 - adj)
    # парное сравнение по общему сигналу (shadow-варианты входят по тем же ценам)
    pa = dict((t["parent"], t["r"]) for t in ra if t["parent"])
    pb = dict((t["parent"], t["r"]) for t in rb if t["parent"])
    common = sorted(set(pa) & set(pb))
    if len(common) >= 2:
        d = [pa[c] - pb[c] for c in common]
        md, sd = _mean_sd(d)
        pr = {"n_pairs": len(d), "mean_diff": md, "se": None, "p": None}
        if sd is not None and sd > 0:
            pr["se"] = sd / math.sqrt(len(d))
            pr["p"] = t_two_sided_p(md / pr["se"], len(d) - 1)
        out["paired"] = pr
    if out["status"] == "ok" and w and w["p"] is not None:
        out["significant"] = w["p"] < adj
    if not out["significant"] and out["status"] == "ok":
        out["notes"].append("не значимо при пороге %.4g (alpha %.2g / k=%d)" % (adj, alpha, k))
    return out


# ---------------------------------------------------------------- список экспериментов

def list_experiments(trades, meta=None, min_n=MIN_N):
    """-> [{"exp","modes","n","variants":{var:n},"first","last","min_n","hypothesis","started","status"}]; status: идёт | достаточно данных."""
    meta = meta or {}
    by = {}
    for t in trades:
        if t["exp"]:
            by.setdefault(t["exp"], []).append(t)
    out = []
    for e in sorted(by):
        rows = by[e]
        m = meta.get(e) or {}
        mn = m.get("min_n") if isinstance(m.get("min_n"), int) and m.get("min_n") > 0 else min_n
        var = {}
        for t in rows:
            var[t["var"] or "(нет)"] = var.get(t["var"] or "(нет)", 0) + 1
        ts = sorted(str(t["ts_open"])[:10] for t in rows if t["ts_open"])
        out.append({"exp": e, "modes": sorted(set(t["mode"] for t in rows)), "n": len(rows), "variants": var,
                    "first": ts[0] if ts else None, "last": ts[-1] if ts else None, "min_n": mn,
                    "hypothesis": m.get("hypothesis"), "prereg": m.get("prereg"), "started": m.get("started"),
                    "status": "достаточно данных" if var and min(var.values()) >= mn else "идёт"})
    return out


# ---------------------------------------------------------------- shadow-варианты

def assign_variant(key, variants, exp="", salt=""):
    """Детерминированное присвоение варианта сигналу: sha256(exp|salt|key) -> индекс. Один ключ всегда даёт один вариант."""
    if not variants:
        raise ValueError("нет вариантов")
    h = hashlib.sha256(("%s|%s|%s" % (exp, salt, key)).encode("utf-8")).digest()
    return variants[int.from_bytes(h[:8], "big") % len(variants)]


def shadow_variants_report(plan):
    """-> [{"index","name","suffix","ok","issues","spec"}] по plan.shadow_variants (до 3). spec = бумажная копия родителя с патчем.

    Правила: патч только по plan_schema.SHADOW_PATCH_KEYS (entry, stop, targets, manage, invalidation, lifecycle, risk); symbol/side
    запрещены; риск вверх запрещён (risk.score и size_mult не выше родителя); копия получает mode=paper, source={kind: shadow, parent_plan},
    tags.variant = имя варианта (experiment/setup/hypothesis от родителя); поля исполнителя (id, rev, armed_ts, guard_preview, prereg_hash)
    убираются: id назначает исполнитель, суффикс S1..S3 в report["suffix"]. Результат проходит plan_schema.validate_static."""
    from tb.core import plan_schema as S
    out = []
    if not isinstance(plan, dict):
        return out
    base = copy.deepcopy(plan)
    pid = base.get("id")
    variants = base.pop("shadow_variants", None) or []
    for k in S.EXEC_FIELDS:
        base.pop(k, None)
    base.pop("created_by", None)
    risk0 = base.get("risk") if isinstance(base.get("risk"), dict) else {}
    for i, v in enumerate(variants[:S.MAX_SHADOW_VARIANTS]):
        rep = {"index": i, "name": v.get("name") if isinstance(v, dict) else None, "suffix": "S%d" % (i + 1), "ok": False, "issues": [], "spec": None}
        out.append(rep)
        if not isinstance(v, dict) or not isinstance(v.get("patch"), dict) or not isinstance(v.get("name"), str):
            rep["issues"].append({"code": "E_TYPE", "path": "shadow_variants[%d]" % i, "msg": "нужны name и patch-объект"})
            continue
        patch = v["patch"]
        bad = [k for k in patch if k not in S.SHADOW_PATCH_KEYS]
        for k in bad:
            rep["issues"].append({"code": "E_UNKNOWN_FIELD", "path": "patch.%s" % k, "msg": "поле нельзя менять в теневом варианте"})
        rk = patch.get("risk")
        if isinstance(rk, dict):
            for f in SHADOW_ALLOWED_RISK_DOWN:
                nv, ov = rk.get(f), risk0.get(f)
                if isinstance(nv, (int, float)) and isinstance(ov, (int, float)) and nv > ov:
                    rep["issues"].append({"code": "E_RISK_UP", "path": "patch.risk.%s" % f, "msg": "риск вверх запрещён (%s > %s)" % (nv, ov)})
        if rep["issues"]:
            continue
        try:
            spec = S.merge_patch(base, patch)
        except ValueError as e:
            rep["issues"].append({"code": "E_TOO_LARGE", "path": "patch", "msg": str(e)})
            continue
        spec["mode"] = "paper"
        spec["source"] = {"kind": "shadow", "parent_plan": pid}
        tags = dict(spec.get("tags") or {})
        tags["variant"] = str(v["name"]).lower()
        spec["tags"] = tags
        spec["schema"] = base.get("schema", S.SCHEMA)
        iss = [x for x in S.validate_static(spec) if x.get("severity") == "error"]
        if iss:
            rep["issues"].extend({"code": x.get("code"), "path": x.get("path"), "msg": x.get("msg")} for x in iss)
            continue
        rep["ok"], rep["spec"] = True, spec
    return out


def shadow_variants(plan):
    """-> [теневые планы] (только прошедшие проверку); подробности и причины отказов: shadow_variants_report."""
    return [r["spec"] for r in shadow_variants_report(plan) if r["ok"]]


def can_start_paper(active, spec, max_active=PAPER_MAX_ACTIVE):
    """Ограничения 04 п. 4.1: бумажных планов не более max_active; по одной монете не более одного одновременно в каждом варианте.
    active: список spec активных планов (учитываются только mode=paper). -> (ok, код): "OK" | "PAPER_MAX_ACTIVE" | "PAPER_DUP_SYMBOL_VARIANT"."""
    act = [a for a in active or [] if isinstance(a, dict) and a.get("mode") == "paper"]
    if len(act) >= max_active:
        return False, "PAPER_MAX_ACTIVE"

    def key(s):
        tg = s.get("tags") or {}
        return (s.get("symbol"), tg.get("experiment"), tg.get("variant"))
    if any(key(a) == key(spec) for a in act):
        return False, "PAPER_DUP_SYMBOL_VARIANT"
    return True, "OK"
