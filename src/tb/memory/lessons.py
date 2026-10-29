"""Уроки как гипотезы: `state/lessons.jsonl` (08 п. 4). Формулировки из шаблонов (cond + claim), без свободного текста.

Файл append-only с хеш-цепочкой (chain.py), каждая строка = полное состояние урока (rev растёт, kind new|update). Статусы:
hyp -> confirmed (n_closed>=30 и >=5 дней и >=2 режимов, либо >=15 с эффектом |diff|>t*se с поправкой Бонферрони на число активных уроков, либо решение
владельца), rejected (новые случаи опровергают или владелец), expired (срок годности без подтверждений), merged (дубликат по условию).
Подтверждённый урок остаётся подсказкой, в guard/TRADING НЕ превращается автоматически. conf затухает: conf_eff = conf * 0.5^(дни без подтверждений / half_life);
ниже hide_below урок скрыт от brief. Противоречащие активные уроки получают contradicts и множитель conf. Случаи с mem=off, exclude_from_stats, inj_suspect и
старой версией правил уроки не поднимают и не подтверждают.
"""
import math
import time

from tb import paths
from tb.core import expstats
from tb.memory import agg, cases, chain, privacy, similar

SCHEMA = "tb.lesson/1"
OPS = ("=", ">=", "<=", "in")
STATUSES = ("hyp", "confirmed", "rejected", "expired", "merged")
ACTIVE = ("hyp", "confirmed")
REASONS = ("NEW", "ENOUGH_N", "CONTRADICTED", "EXPIRED", "MERGED", "OWNER", "EVICTED", "REFRESH")
METRICS = ("mean_r", "win_rate", "stop_hit_share", "err_share")
DIRS = ("lower", "higher")
VS = ("base", "zero")
CREATORS = ("code", "reviewer", "owner")
CAT_FEATURES = {"coin_class": cases.COIN_CLASSES, "setup": cases.SETUPS, "regime": cases.REGIMES, "utc_session": cases.SESSIONS, "side": cases.SIDES,
                "confirm_kind": cases.CONFIRM_KINDS}
BOOL_FEATURES = ("confirmed", "no_confirm_tag", "blackout")
NUM_FEATURES = ("range_5m_pct", "range_15m_pct", "ret_5m_pct", "turn_surge_5m", "chg_24h_pct", "spread_bps", "funding_pct", "stop_pct",
                "stop_dist_vs_range5", "rr_planned", "size_mult", "btc_ret_1h_pct")
FEATURES = tuple(CAT_FEATURES) + BOOL_FEATURES + NUM_FEATURES
FEATURE_RU = {"coin_class": "класс монеты", "setup": "сетап", "regime": "режим", "utc_session": "сессия", "side": "сторона", "confirm_kind": "тип подтверждения",
              "confirmed": "подтверждение", "no_confirm_tag": "метка no-confirm", "blackout": "блэкаут", "range_5m_pct": "диапазон 5м %", "range_15m_pct": "диапазон 15м %",
              "ret_5m_pct": "доходность 5м %", "turn_surge_5m": "всплеск оборота", "chg_24h_pct": "изменение 24ч %", "spread_bps": "спред bps",
              "funding_pct": "funding %", "stop_pct": "стоп %", "stop_dist_vs_range5": "стоп/диапазон5м", "rr_planned": "RR плана", "size_mult": "множитель размера",
              "btc_ret_1h_pct": "BTC 1ч %"}
METRIC_RU = {"mean_r": "средний R", "win_rate": "доля плюсовых", "stop_hit_share": "доля стопов", "err_share": "доля ошибок"}
DIR_RU = {"lower": "ниже", "higher": "выше"}
VS_RU = {"base": "базового", "zero": "нуля"}
STATUS_RU = {"hyp": "гипотеза", "confirmed": "подтверждено", "rejected": "отвергнуто", "expired": "устарело", "merged": "объединено"}
# коды lesson_hint ревьюера -> условие (проверяется по данным; урок создаётся только при статистических предпосылках)
LESSON_CODES = {
    "L-NO_CONFIRM": [{"feature": "confirmed", "op": "=", "value": False}],
    "L-WIDE_STOP": [{"feature": "stop_pct", "op": ">=", "value": 2.0}],
    "L-RANGE_GT_STOP": [{"feature": "stop_dist_vs_range5", "op": "<=", "value": 1.0}],
    "L-SPREAD": [{"feature": "spread_bps", "op": ">=", "value": 5.0}],
    "L-CHASE": [{"feature": "setup", "op": "=", "value": "momentum_chase"}],
    "L-BLACKOUT": [{"feature": "blackout", "op": "=", "value": True}],
    "L-MEME": [{"feature": "coin_class", "op": "=", "value": "meme"}],
    "L-BIG_RANGE": [{"feature": "range_5m_pct", "op": ">=", "value": 1.5}],
}
DAY = 86400.0


def lessons_path(root=None):
    return paths.state("lessons.jsonl", root=root)


def lessons_lock(root=None):
    return paths.state("lessons.lock", root=root)


# ---------------------------------------------------------------- условия

def _fmt(v):
    return str(v).lower() if isinstance(v, bool) else str(v)


def render_cond(cond):
    parts = []
    for c in cond:
        v = c["value"]
        if c["feature"] == "confirmed" and c["op"] == "=":
            parts.append("нет подтверждения" if v is False else "есть подтверждение")
            continue
        parts.append("%s %s %s" % (FEATURE_RU.get(c["feature"], c["feature"]), c["op"], ("{" + ",".join(_fmt(x) for x in v) + "}") if isinstance(v, list) else _fmt(v)))
    return " и ".join(parts)


def render_txt(l):
    cl = l["claim"]
    n = (l.get("evidence") or {}).get("n_closed", 0)
    return ("%s -> %s %s %s (%d случаев, %s)" % (render_cond(l["cond"]), METRIC_RU[cl["metric"]], DIR_RU[cl["dir"]], VS_RU[cl["vs"]], n, STATUS_RU[l["status"]]))[:140]


def validate_cond(cond):
    """-> список ошибок. Конъюнкция <= 3 условий из белого списка признаков."""
    errs = []
    if not isinstance(cond, list) or not (1 <= len(cond) <= 3):
        return ["cond: список из 1-3 условий"]
    seen = set()
    for c in cond:
        if not isinstance(c, dict) or set(c) != {"feature", "op", "value"}:
            errs.append("cond: элемент {feature, op, value}")
            continue
        f, op, v = c["feature"], c["op"], c["value"]
        if f not in FEATURES:
            errs.append("cond: признак %r вне белого списка" % f)
            continue
        if f in seen:
            errs.append("cond: признак %s повторён" % f)
        seen.add(f)
        if op not in OPS:
            errs.append("cond: op %r не из %s" % (op, list(OPS)))
            continue
        vals = v if op == "in" else [v]
        if op == "in" and (not isinstance(v, list) or not v or len(v) > 8):
            errs.append("cond: in требует непустой список (до 8)")
            continue
        for x in vals:
            if f in CAT_FEATURES and x not in CAT_FEATURES[f]:
                errs.append("cond: %s=%r не из словаря" % (f, x))
            elif f in BOOL_FEATURES and not isinstance(x, bool):
                errs.append("cond: %s нужен bool" % f)
            elif f in NUM_FEATURES and (isinstance(x, bool) or not isinstance(x, (int, float))):
                errs.append("cond: %s нужно число" % f)
        if (f in CAT_FEATURES or f in BOOL_FEATURES) and op in (">=", "<="):
            errs.append("cond: для %s допустимы = и in" % f)
    return errs


def validate_claim(cl):
    errs = []
    if not isinstance(cl, dict) or set(cl) - {"metric", "dir", "vs", "effect"}:
        return ["claim: {metric, dir, vs, effect}"]
    if cl.get("metric") not in METRICS:
        errs.append("claim.metric не из %s" % list(METRICS))
    if cl.get("dir") not in DIRS:
        errs.append("claim.dir не из %s" % list(DIRS))
    if cl.get("vs") not in VS:
        errs.append("claim.vs не из %s" % list(VS))
    if cl.get("vs") == "zero" and cl.get("metric") != "mean_r":
        errs.append("claim.vs=zero только для mean_r")
    e = cl.get("effect")
    if isinstance(e, bool) or not isinstance(e, (int, float)) or not (0 <= e <= 10):
        errs.append("claim.effect: число 0..10")
    return errs


def _cmp_val(op, v, x):
    if x is None:
        return False
    if op == "=":
        return x == v
    if op == "in":
        return x in v
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return x >= v if op == ">=" else x <= v


def matches(cond, case):
    vec = similar.vector(case)
    return all(_cmp_val(c["op"], c["value"], vec.get(c["feature"])) for c in cond)


def matches_features(cond, feats):
    return all(_cmp_val(c["op"], c["value"], feats.get(c["feature"])) for c in cond)


def _domain(c):
    op, v = c["op"], c["value"]
    if op == "=":
        return ("set", {v})
    if op == "in":
        return ("set", set(v))
    return ("ge", v) if op == ">=" else ("le", v)


def _compat(a, b):
    ka, va = a
    kb, vb = b
    if ka == "set" and kb == "set":
        return bool(va & vb)
    if ka == "set" or kb == "set":
        s, (k, v) = (va, b) if ka == "set" else (vb, a)
        return any(isinstance(x, (int, float)) and not isinstance(x, bool) and ((x >= v) if k == "ge" else (x <= v)) for x in s)
    if ka == kb:
        return True
    lo, hi = (va, vb) if ka == "ge" else (vb, va)
    return lo <= hi


def overlap(cond_a, cond_b):
    """Условия пересекаются: все общие признаки совместимы (признаки только одного урока не мешают)."""
    da = dict((c["feature"], _domain(c)) for c in cond_a)
    for c in cond_b:
        if c["feature"] in da and not _compat(da[c["feature"]], _domain(c)):
            return False
    return True


def _cond_set(cond):
    return set((c["feature"], c["op"], str(c["value"])) for c in cond)


def jaccard(a, b):
    x, y = _cond_set(a), _cond_set(b)
    return len(x & y) / float(len(x | y)) if x | y else 0.0


# ---------------------------------------------------------------- статистика урока

def _val(metric, c):
    r = c["result"]
    if metric == "mean_r":
        return float(r["r"])
    if metric == "win_rate":
        return 1.0 if r["r"] > 0 else 0.0
    if metric == "stop_hit_share":
        return 1.0 if r.get("exit_reason") == "sl" else 0.0
    errs = [e for e in ((c.get("tags") or {}).get("err") or []) if e not in ("good", "good-process-bad-luck")]
    return 1.0 if errs else 0.0


def pool(all_cases, cfg):
    """Закрытые сделки, которым разрешено поднимать и подтверждать уроки (не исключены, mem!=off, версия правил допустима)."""
    ok = similar.allowed_rules(cfg)
    return [c for c in all_cases if agg.closed_r(c) is not None and cases.eligible(c, cfg) and (c.get("src") or {}).get("rules_ver") in ok]


def baseline(metric, vs, pool_cases):
    if vs == "zero":
        return 0.0
    xs = [_val(metric, c) for c in pool_cases]
    return sum(xs) / len(xs) if xs else None


def stats(lesson, all_cases, cfg, created_ts=None):
    """Показатели условия урока на пуле -> evidence (+служебные поля diff, n_new, mean_new, days, regimes)."""
    pl = pool(all_cases, cfg)
    cond, cl = lesson["cond"], lesson["claim"]
    m = [c for c in pl if matches(cond, c)]
    base = baseline(cl["metric"], cl["vs"], pl)
    xs = [_val(cl["metric"], c) for c in m]
    n = len(xs)
    mean = sum(xs) / n if n else None
    sd = agg._sd(xs)
    se = sd / math.sqrt(n) if sd is not None and n >= 3 else None
    rs = [c["result"]["r"] for c in m]
    base_r = baseline("mean_r", "base", pl)
    ts = sorted(c["ts"] for c in m)
    ct = created_ts
    new = [c for c in m if ct is not None and (similar.ts_epoch(c["ts"]) or 0) > ct]
    xn = [_val(cl["metric"], c) for c in new]
    days = set(c["ts"][:10] for c in m)
    regs = set((c.get("situation") or {}).get("regime") for c in m) - {None, "unknown"}
    ids = [c["case_id"] for c in sorted(m, key=lambda c: (c["ts"], c["case_id"]))][-20:]
    return {"cases": ids, "n": n, "n_closed": n, "mean_r": round(sum(rs) / n, 3) if n else None, "se": round(se, 3) if se is not None else None,
            "base_r": round(base_r, 3) if base_r is not None else None, "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
            "_metric": mean, "_base": base, "_diff": (mean - base) if mean is not None and base is not None else None, "_n_new": len(new),
            "_mean_new": (sum(xn) / len(xn)) if xn else None, "_days": len(days), "_regimes": len(regs), "_pool_n": len(pl),
            "_last_new": max([similar.ts_epoch(c["ts"]) or 0.0 for c in new]) if new else None}


def supports(lesson, st):
    d = st["_diff"]
    if d is None or d == 0:
        return False
    return d < 0 if lesson["claim"]["dir"] == "lower" else d > 0


def _public_ev(st):
    ev = dict((k, v) for k, v in st.items() if not k.startswith("_"))
    ev["diff"] = round(st["_diff"], 3) if st["_diff"] is not None else None
    ev["days"] = st["_days"]
    return ev


def conf_for(lesson, st, cfg):
    lc = cfg.get("lessons") or {}
    n = st["n_closed"]
    d, se = st["_diff"], st["se"]
    if n == 0 or d is None:
        return 0.05
    n_eff = n * (abs(d) / (abs(d) + se) if se else 0.5)
    c = 1.0 - math.exp(-n_eff / 10.0)
    cap = lc.get("conf_cap_hyp", 0.9)
    if lesson.get("created_by") == "reviewer" and not (n >= lc.get("min_n_propose", 4) and supports(lesson, st)):
        cap = min(cap, lc.get("conf_cap_reviewer", 0.3))
    if lesson["status"] == "confirmed":
        cap = 0.95
    return round(min(c, cap), 3)


def conf_eff(lesson, now, cfg):
    lc = cfg.get("lessons") or {}
    last = similar.ts_epoch((lesson.get("evidence") or {}).get("last_ts")) or similar.ts_epoch(lesson.get("created_ts")) or now
    age = max(0.0, now - last) / DAY
    c = lesson.get("conf", 0.0) * (0.5 ** (age / float(lc.get("half_life_days", 21))))
    if lesson.get("contradicts"):
        c *= lc.get("contradict_mult", 0.6)
    return round(c, 3)


# ---------------------------------------------------------------- хранилище

def read_all(root=None):
    """-> {lesson_id: последнее состояние} (битые строки пропущены)."""
    rows, _ = chain.read(lessons_path(root))
    out = {}
    for r in rows:
        if r.get("schema") == SCHEMA and r.get("lesson_id"):
            out[r["lesson_id"]] = r
    return out


def _next_id(root):
    n = 0
    for k in read_all(root):
        try:
            n = max(n, int(k.split("-")[1]))
        except (IndexError, ValueError):
            pass
    return "L-%04d" % (n + 1)


def _write(root, rec):
    rec["txt"] = render_txt(rec)
    errs = privacy.privacy_scan(rec)
    if errs:
        raise ValueError("; ".join(errs))
    return chain.append(lessons_path(root), lessons_lock(root), rec, max_bytes=4096)


def _hist(l, now, status, reason):
    h = list(l.get("history") or [])
    h.append({"ts": cases.iso(now), "status": status, "reason_code": reason})
    return h[-8:]


def create(root, cond, claim, created_by, all_cases, cfg, now, evidence_ids=None, status="hyp"):
    """Создаёт урок-гипотезу (evidence считается кодом по случаям). ValueError при неверном cond/claim."""
    errs = validate_cond(cond) + validate_claim(claim)
    if created_by not in CREATORS:
        errs.append("created_by не из %s" % list(CREATORS))
    if errs:
        raise ValueError("; ".join(errs))
    lc = cfg.get("lessons") or {}
    l = {"schema": SCHEMA, "lesson_id": _next_id(root), "rev": 1, "ts": cases.iso(now), "created_ts": now, "kind": "new", "status": status, "cond": cond, "claim": claim,
         "created_by": created_by, "contradicts": [], "merged_into": None, "rules_ver": cfg.get("rules_ver")}
    st = stats(l, all_cases, cfg, created_ts=now)
    l["evidence"] = _public_ev(st)
    if evidence_ids:
        l["evidence"]["cases"] = [i for i in evidence_ids][:20]
    l["conf"] = conf_for(l, st, cfg)
    l["expires_ts"] = now + (lc.get("ttl_confirmed_days", 90) if status == "confirmed" else lc.get("ttl_hyp_days", 30)) * DAY
    l["history"] = [{"ts": l["ts"], "status": status, "reason_code": "NEW"}]
    return _write(root, l)


def _update(root, l, now, status=None, reason="REFRESH", **kw):
    n = dict((k, v) for k, v in l.items() if k not in ("seq", "prev", "h"))
    n.update(kw)
    n.update({"rev": l["rev"] + 1, "kind": "update", "ts": cases.iso(now)})
    if status and status != l["status"]:
        n["status"] = status
        n["history"] = _hist(l, now, status, reason)
    return _write(root, n)


def _same(a, b):
    keys = ("status", "conf", "expires_ts", "contradicts", "merged_into", "evidence")
    return all(a.get(k) == b.get(k) for k in keys)


def owner_set(root, lesson_id, action, cfg, now, into=None):
    """Решение владельца: confirm | reject | expire | merge (into=L-id). -> новое состояние."""
    ls = read_all(root)
    l = ls.get(lesson_id)
    if l is None:
        raise ValueError("нет урока %s" % lesson_id)
    lc = cfg.get("lessons") or {}
    if action == "confirm":
        if l["status"] not in ACTIVE:
            raise ValueError("подтвердить можно только активный урок (статус %s)" % l["status"])
        return _update(root, l, now, "confirmed", "OWNER", expires_ts=now + lc.get("ttl_confirmed_days", 90) * DAY)
    if action == "reject":
        return _update(root, l, now, "rejected", "OWNER")
    if action == "expire":
        return _update(root, l, now, "expired", "OWNER")
    if action == "merge":
        if into not in ls or into == lesson_id:
            raise ValueError("merge: нужен существующий другой урок")
        return _update(root, l, now, "merged", "MERGED", merged_into=into)
    raise ValueError("action: confirm|reject|expire|merge")


def refresh(root, all_cases, cfg, now, dry_run=False):
    """Пересчёт активных уроков по случаям: evidence, conf, статус, срок, противоречия, слияние, вытеснение. -> список изменений [{lesson_id, change, reason}]."""
    lc = cfg.get("lessons") or {}
    ls = read_all(root)
    act = [l for l in ls.values() if l["status"] in ACTIVE]
    changes = []
    k_active = max(1, len(act))
    allowed = similar.allowed_rules(cfg)
    new_state = {}
    for l in sorted(act, key=lambda x: x["lesson_id"]):
        st = stats(l, all_cases, cfg, created_ts=l.get("created_ts"))
        ev = _public_ev(st)
        status, reason = l["status"], "REFRESH"
        ttl = (lc.get("ttl_confirmed_days", 90) if l["status"] == "confirmed" else lc.get("ttl_hyp_days", 30)) * DAY
        expires = l.get("expires_ts") or now + ttl
        last = st["_last_new"]
        if last is not None and last + ttl > expires:
            expires = last + ttl                                      # новые подтверждающие случаи продлевают срок
        if l.get("rules_ver") not in allowed and (not last or last <= (similar.ts_epoch((l.get("evidence") or {}).get("last_ts")) or 0)):
            expires = min(expires, now + lc.get("old_rules_expire_days", 14) * DAY)
        n_new, mean_new, base = st["_n_new"], st["_mean_new"], st["_base"]
        lower = l["claim"]["dir"] == "lower"
        if expires <= now:
            status, reason = "expired", "EXPIRED"
        elif n_new >= lc.get("reject_n_new", 10) and base is not None and mean_new is not None and ((mean_new >= base) if lower else (mean_new <= base)):
            status, reason = "rejected", "CONTRADICTED"
        elif st["n_closed"] >= lc.get("reject_n_new", 10) and st["_diff"] is not None and not supports(l, st) and l["status"] == "hyp" and l.get("created_by") == "code":
            status, reason = "rejected", "CONTRADICTED"               # эффект сменил знак на достаточной выборке
        elif l["status"] == "hyp" and supports(l, st):
            ok_days, ok_reg = st["_days"] >= lc.get("confirm_days", 5), st["_regimes"] >= lc.get("confirm_min_regimes", 2)
            by_n = st["n_closed"] >= lc.get("confirm_n", 30)
            by_fast = False
            if st["n_closed"] >= lc.get("confirm_n_fast", 15) and st["se"]:
                tq = expstats.t_quantile(1.0 - expstats.bonferroni(k_active) / 2.0, st["n_closed"] - 1)
                by_fast = abs(st["_diff"]) > max(2.0, tq) * st["se"]
            conflict = any(ls.get(o, {}).get("status") == "confirmed" for o in (l.get("contradicts") or []))
            if (by_n or by_fast) and ok_days and ok_reg and not conflict:
                status, reason = "confirmed", "ENOUGH_N"
                expires = max(expires, now + lc.get("ttl_confirmed_days", 90) * DAY)
        cand = dict(l, status=status, evidence=ev, expires_ts=expires)
        cand["conf"] = conf_for(cand, st, cfg)
        new_state[l["lesson_id"]] = (cand, reason)
    # противоречия между активными (по новому статусу)
    live = dict((i, c) for i, (c, r) in new_state.items() if c["status"] in ACTIVE)
    for i, c in live.items():
        c["contradicts"] = sorted(j for j, d in live.items() if j != i and d["claim"]["metric"] == c["claim"]["metric"] and d["claim"]["dir"] != c["claim"]["dir"]
                                  and overlap(c["cond"], d["cond"]))
    # слияние дубликатов: остаётся урок с большим n (при равенстве старший id)
    ids = sorted(live, key=lambda i: (-live[i]["evidence"]["n_closed"], i))
    for a_i, a in enumerate(ids):
        if a not in live:
            continue
        for b in ids[a_i + 1:]:
            if b in live and live[a]["claim"]["metric"] == live[b]["claim"]["metric"] and live[a]["claim"]["dir"] == live[b]["claim"]["dir"] \
                    and jaccard(live[a]["cond"], live[b]["cond"]) > lc.get("merge_jaccard", 0.8):
                new_state[b] = (dict(live[b], status="merged", merged_into=a, contradicts=[]), "MERGED")
                del live[b]
    # вытеснение сверх лимита
    cap = lc.get("max_active", 200)
    if len(live) > cap:
        worst = sorted(live, key=lambda i: (conf_eff(live[i], now, cfg), i))[:len(live) - cap]
        for i in worst:
            new_state[i] = (dict(live[i], status="expired"), "EVICTED")
    for i, (c, reason) in sorted(new_state.items()):
        old = ls[i]
        if _same(old, c) and c["status"] == old["status"]:
            continue
        what = "status:%s->%s" % (old["status"], c["status"]) if c["status"] != old["status"] else "evidence"
        changes.append({"lesson_id": i, "change": what, "reason": reason})
        if not dry_run:
            _update(root, old, now, c["status"], reason, evidence=c["evidence"], conf=c["conf"], expires_ts=c["expires_ts"], contradicts=c.get("contradicts") or [],
                    merged_into=c.get("merged_into"))
    return changes


# ---------------------------------------------------------------- предложения кода

def _singles(cfg, pl):
    th = (cfg.get("lessons") or {}).get("thresholds") or {}
    le = (cfg.get("lessons") or {}).get("le_thresholds") or {}
    out = []
    for f, vals in CAT_FEATURES.items():
        out += [{"feature": f, "op": "=", "value": v} for v in vals if v not in ("unknown", "none") and any((c.get("situation") or {}).get(f) == v for c in pl)]
    out += [{"feature": "confirmed", "op": "=", "value": False}, {"feature": "blackout", "op": "=", "value": True}]
    for f, vs in th.items():
        out += [{"feature": f, "op": ">=", "value": v} for v in vs]
    for f, vs in le.items():
        out += [{"feature": f, "op": "<=", "value": v} for v in vs]
    return out


def propose(root, all_cases, cfg, now):
    """Кандидаты в уроки по устойчивым отклонениям mean_r от базы (не пишет). -> [{cond, claim, n, diff, share}]; уже имеющиеся условия и лимит в сутки учтены."""
    lc = cfg.get("lessons") or {}
    pl = pool(all_cases, cfg)
    if len(pl) < lc.get("min_n_propose", 4):
        return []
    base = sum(c["result"]["r"] for c in pl) / len(pl)
    existing = read_all(root)
    have = [_cond_set(l["cond"]) for l in existing.values() if l["status"] in ACTIVE]
    day0 = now - (now % DAY)
    made = sum(1 for l in existing.values() if l["created_by"] == "code" and l.get("rev") == 1 and (l.get("created_ts") or 0) >= day0)
    budget = max(0, lc.get("max_new_per_day", 3) - made)
    singles = _singles(cfg, pl)
    sing_cases = dict((i, [c for c in pl if matches([s], c)]) for i, s in enumerate(singles))
    combos = [[s] for s in singles]
    for i, a in enumerate(singles):
        for j in range(i + 1, len(singles)):
            b = singles[j]
            if a["feature"] != b["feature"] and len(sing_cases[i]) >= lc.get("min_n_propose", 4) and len(sing_cases[j]) >= lc.get("min_n_propose", 4):
                combos.append([a, b])
    cands = {}
    for cond in combos:
        m = [c for c in pl if matches(cond, c)]
        if len(m) < lc.get("min_n_propose", 4) or _cond_set(cond) in have:
            continue
        rs = [c["result"]["r"] for c in m]
        diff = sum(rs) / len(rs) - base
        if abs(diff) < lc.get("min_diff_r", 0.5):
            continue
        lower = diff < 0
        share = sum(1 for r in rs if (r < base if lower else r > base)) / float(len(rs))
        if share < lc.get("min_share", 0.75):
            continue
        key = tuple(sorted(c["case_id"] for c in m))
        cur = cands.get(key)
        if cur is None or len(cond) < len(cur["cond"]):
            cands[key] = {"cond": cond, "claim": {"metric": "mean_r", "dir": "lower" if lower else "higher", "vs": "base", "effect": round(abs(diff), 2)},
                          "n": len(m), "diff": round(diff, 3), "share": round(share, 2)}
    ranked = sorted(cands.values(), key=lambda x: (-abs(x["diff"]) * math.sqrt(x["n"]), str(x["cond"])))
    return ranked[:budget]


def from_hint(hint, root, all_cases, cfg, now):
    """lesson_hint ревьюера -> урок, ТОЛЬКО если предпосылки выполнены по данным (n_closed >= min_n_propose и отклонение >= min_diff_r); иначе None."""
    lc = cfg.get("lessons") or {}
    cond = LESSON_CODES.get((hint or {}).get("code"))
    if not cond:
        return None
    pl = pool(all_cases, cfg)
    m = [c for c in pl if matches(cond, c)]
    if len(m) < lc.get("min_n_propose", 4) or not pl:
        return None
    base = sum(c["result"]["r"] for c in pl) / len(pl)
    diff = sum(c["result"]["r"] for c in m) / len(m) - base
    if abs(diff) < lc.get("min_diff_r", 0.5):
        return None
    if _cond_set(cond) in [_cond_set(l["cond"]) for l in read_all(root).values() if l["status"] in ACTIVE]:
        return None
    return create(root, cond, {"metric": "mean_r", "dir": "lower" if diff < 0 else "higher", "vs": "base", "effect": round(abs(diff), 2)}, "reviewer", all_cases, cfg, now)


def applicable(lessons, feats, now, cfg, limit=None):
    """Активные уроки, условия которых выполнены признаками запроса, по убыванию conf_eff (скрытые ниже hide_below не берутся).
    -> [{id, st, n, conf, txt, contradicts}] для brief и инструмента шлюза."""
    lc = cfg.get("lessons") or {}
    out = []
    for l in lessons:
        if l["status"] not in ACTIVE or not matches_features(l["cond"], feats):
            continue
        ce = conf_eff(l, now, cfg)
        if ce < lc.get("hide_below", 0.15):
            continue
        out.append({"id": l["lesson_id"], "st": l["status"], "n": (l.get("evidence") or {}).get("n_closed", 0), "conf": ce, "txt": l.get("txt"),
                    "contradicts": list(l.get("contradicts") or [])})
    out.sort(key=lambda x: (-x["conf"], x["id"]))
    return out[:limit or lc.get("brief_max", 3)]
