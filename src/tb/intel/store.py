"""Конфиг intel, пути и сборка `runtime/intel/facts.json` (07 п. 3.3). Владелец: WP13.

facts.json: ТОЛЬКО числа, bool, enum и идентификаторы (`^[A-Za-z0-9_.-]{1,40}$`); текст третьих лиц сюда не попадает ни при каких условиях.
Страховка: `check_facts` обходит весь документ и отвергает любую строку, не похожую на идентификатор (без пробелов, <= 40 символов) (используется при записи и в тестах).
Устаревшее значение (старше stale_mult x интервал источника) из документа ИСКЛЮЧАЕТСЯ, свежесть каждого источника лежит в `sources`.
"""
import copy
import json
import math
import re

from tb import paths
from tb.core import filelock, localcfg
from tb.intel import calendar as ical
from tb.intel import fetch as ifetch
from tb.intel import sources as src_mod

SCHEMA = "tb.facts/1"
RE_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
RE_VAL = re.compile(r"^[A-Za-z0-9_.:/-]{1,40}$")       # строковые значения: идентификаторы и метка схемы (tb.facts/1); пробелов и текста нет
RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")

DEFAULTS = {
    "version": 1,
    "symbols": ["BTC", "ETH", "SOL"],           # всегда в deriv; к ним добавляются символы watch_config.json и hot_n горячих монет
    "hot_n": 3, "dvol_ccys": ["BTC", "ETH"],
    "timeout_s": 10.0, "min_gap_s": 2.0, "max_per_hour": 600, "backoff_base_s": 60.0, "max_backoff_s": 3600.0, "proxy": False,
    "workers": 4, "tick_s": 2.0, "publish_s": 60.0, "hb_s": 5.0, "cal_s": 60.0, "symbols_s": 120.0,
    "stale_mult": 3.0, "degrade_fails": 2,
    "ann": {"delist_ttl_h": 168, "listing_ttl_h": 72, "maint_horizon_h": 72},
    "flash": {"enabled": True, "min_imp": 3, "max_age_s": 900.0, "dedup_s": 1800.0, "horizons_min": [1, 5, 15], "max_syms": 2,
              "aliases": {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP", "dogecoin": "DOGE", "cardano": "ADA", "chainlink": "LINK",
                          "avalanche": "AVAX", "toncoin": "TON", "hyperliquid": "HYPE", "binance coin": "BNB"}},
    "calendar": {"horizon_days": 14, "warn_min": [60, 15], "windows": {}, "unverified_blackout": True, "blackout_min_impact": 2, "candidates": []},
    "us_holidays": [], "us_half_days": [],
    "sources": {},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_cfg(root=None):
    """config/intel.json поверх DEFAULTS. Битый файл -> умолчания + cfg["_warn"]."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        cfg = deep_merge(cfg, localcfg.read("intel", root))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        cfg["_warn"] = "config/intel.json не прочитан: умолчания"
    return cfg


def intel_dir(root=None):
    return paths.runtime("intel", root=root)


def facts_path(root=None):
    return paths.runtime("intel", "facts.json", root=root)


def read_facts(root=None):
    try:
        with open(facts_path(root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def symbols_now(root, cfg):
    """BTC/ETH/SOL (cfg.symbols) + symbols из watch_config.json + hot_n горячих монет из runtime/hot.json (только tradable)."""
    out = []

    def add(s):
        if isinstance(s, str) and RE_SYM.match(s) and s not in out:
            out.append(s)
    for s in cfg.get("symbols") or []:
        add(s)
    try:
        with open(paths.config("watch_config.json", root=root), encoding="utf-8") as f:
            for s in (json.load(f) or {}).get("symbols") or []:
                add(s)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        with open(paths.runtime("hot.json", root=root), encoding="utf-8") as f:
            n = 0
            for r in (json.load(f) or {}).get("top") or []:
                if isinstance(r, dict) and r.get("tradable", True) and n < int(cfg.get("hot_n", 3)):
                    add(r.get("sym"))
                    n += 1
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out[:12]


def _num(x, nd=4):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(float(x), nd)


def _int(x):
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def source_states(root, srcs, cfg, now):
    """-> ({id: {"ok","age_s","fails","v"}}, [degraded ids], {id: cache}). Источники без кэша (ещё не опрашивались) пропускаются."""
    st, deg, caches = {}, [], {}
    mult, dfails = float(cfg.get("stale_mult", 3.0)), int(cfg.get("degrade_fails", 2))
    for s in srcs:
        if not s.get("enabled", True):
            continue
        c = ifetch.read_cache(s["id"], root)
        if not c:
            continue
        caches[s["id"]] = c
        fetched = c.get("fetched")
        age = int(max(0, now - fetched)) if isinstance(fetched, (int, float)) else None
        fails = int(c.get("fails") or 0)
        st[s["id"]] = {"ok": bool(c.get("ok")), "age_s": age, "fails": fails, "v": bool(s.get("verified"))}
        if fails >= dfails or (age is not None and age > mult * float(s["interval_s"])):
            deg.append(s["id"])
    return st, sorted(deg), caches


def _fresh(s, c, now, cfg):
    """Данные кэша пригодны для facts: был успех и он не старше stale_mult x интервал."""
    f = c.get("fetched")
    return bool(c.get("data")) and isinstance(f, (int, float)) and now - f <= float(cfg.get("stale_mult", 3.0)) * float(s["interval_s"])


def hist_change(hist, now, hours=24.0, tol_h=2.5):
    """Изменение значения за ~24 ч по собственной истории [[ts, v], ...]; None, если подходящей точки нет."""
    best = None
    for ts, v in hist or []:
        d = abs((now - ts) - hours * 3600)
        if d <= tol_h * 3600 and (best is None or d < best[0]):
            best = (d, v)
    last = hist[-1][1] if hist else None
    if best is None or last is None:
        return None
    return round(last - best[1], 3)


def build_facts(root, cfg, now, srcs=None, symbols=None):
    srcs = srcs if srcs is not None else src_mod.expand(cfg, symbols or symbols_now(root, cfg))
    symbols = symbols or symbols_now(root, cfg)
    states, degraded, caches = source_states(root, srcs, cfg, now)
    by_id = {s["id"]: s for s in srcs}

    def data(sid):
        c, s = caches.get(sid), by_id.get(sid)
        return (c.get("data") or {}) if c and s and _fresh(s, c, now, cfg) else {}
    f = {"schema": SCHEMA, "ts": int(now), "sources": states}
    # --- настроение ---
    sent = {}
    d = data("fng")
    if d.get("v") is not None:
        sent["fng"] = {k: v for k, v in (("v", _int(d.get("v"))), ("cls", d.get("cls") if d.get("cls") in ("extreme_fear", "fear", "neutral", "greed", "extreme_greed") else None),
                                         ("prev", _int(d.get("prev")))) if v is not None}
    dv = {}
    for ccy in cfg.get("dvol_ccys") or []:
        v = _num(data("dvol_%s" % ccy).get("v"), 2)
        if v is not None and RE_SYM.match(ccy):
            dv[ccy] = v
    if dv:
        sent["dvol"] = dv
    d = data("btc_dom")
    if d.get("v") is not None:
        dom = {"v": _num(d["v"], 2)}
        chg = hist_change((caches.get("btc_dom") or {}).get("hist"), now)
        if chg is not None:
            dom["chg_24h"] = chg
        sent["btc_dom"] = dom
    f["sentiment"] = sent
    # --- деривативы ---
    tick = data("bybit_tickers").get("symbols") or {}
    der = {}
    for sym in symbols:
        row = {}
        t = tick.get(sym) if isinstance(tick, dict) else None
        if isinstance(t, dict) and _num(t.get("funding_pct")) is not None:
            row["funding_pct"] = _num(t["funding_pct"], 5)
        o = _num(data("oi_%s" % sym).get("oi_chg_24h_pct"), 3)
        if o is not None:
            row["oi_chg_24h_pct"] = o
        ls = _num(data("ls_%s" % sym).get("ls_ratio"), 3)
        if ls is not None:
            row["ls_ratio"] = ls
        if row:
            der[sym] = row
    f["deriv"] = der
    # --- календарь ---
    ccfg = cfg.get("calendar") or {}
    imp = {s["id"]: bool(s.get("verified")) for s in srcs if s["id"].startswith("cal_") and s.get("enabled", True)}
    events = ical.all_events(root, ccfg, imp)
    nxt = ical.next_events(events, now, float(ccfg.get("horizon_days", 14)), 6)
    cal = {"next": nxt}
    bl = ical.blackout_state(ical.build_blackouts(root, ccfg, events, now)["windows"], now)
    if bl:
        cal["blackout"] = bl
    f["calendar"] = cal
    f["us_market"] = ical.us_market(cfg, now)
    # --- биржа и события объявлений ---
    f["exchange"], f["events"] = exchange_and_events(data("bybit_ann"), cfg, now)
    f["degraded"] = degraded
    return f


def exchange_and_events(ann_data, cfg, now):
    acfg = cfg.get("ann") or {}
    rows = [r for r in (ann_data.get("ann") or []) if isinstance(r, dict)]
    ex = {"status": "ok" if ann_data else "unknown", "maint_in_min": None, "delist_syms": [], "new_listing_syms": []}
    events = []
    for r in rows:
        typ = r.get("type") if r.get("type") in src_mod.ANN_IMP else "other"
        ts = _int(r.get("ts"))
        age_min = int((now - ts) // 60) if ts else None
        if typ == "maintenance":
            st, en = _int(r.get("start")), _int(r.get("end"))
            if st and en and st <= now <= en:
                ex["status"] = "maintenance"
            elif st and 0 < st - now <= float(acfg.get("maint_horizon_h", 72)) * 3600:
                m = int((st - now) // 60)
                ex["maint_in_min"] = m if ex["maint_in_min"] is None else min(ex["maint_in_min"], m)
        if ts and typ == "delisting" and now - ts <= float(acfg.get("delist_ttl_h", 168)) * 3600:
            ex["delist_syms"] += [s for s in r.get("syms") or [] if RE_SYM.match(str(s)) and s not in ex["delist_syms"]]
        if ts and typ == "listing" and now - ts <= float(acfg.get("listing_ttl_h", 72)) * 3600:
            ex["new_listing_syms"] += [s for s in r.get("syms") or [] if RE_SYM.match(str(s)) and s not in ex["new_listing_syms"]]
        ttl = src_mod.ANN_TTL[typ]
        ref = r.get("ref") if isinstance(r.get("ref"), str) and RE_IDENT.match(r["ref"]) else None
        if ref and age_min is not None and 0 <= age_min <= ttl:
            events.append({"ref": ref, "src": "bybit_ann", "type": typ, "syms": [s for s in (r.get("syms") or []) if RE_SYM.match(str(s))][:5],
                           "imp": src_mod.ANN_IMP[typ], "dir": "unknown", "ttl_min": ttl, "conf": 0.9, "age_min": age_min})
    ex["delist_syms"], ex["new_listing_syms"] = ex["delist_syms"][:8], ex["new_listing_syms"][:8]
    events.sort(key=lambda e: (-e["imp"], e["age_min"]))
    return ex, events[:12]


def check_facts(doc, path="$", depth=0):
    """-> список нарушений формата: любая строка (ключ или значение) обязана быть идентификатором, числа конечны, глубина <= 6."""
    bad = []
    if depth > 6:
        return [path + ":deep"]
    if isinstance(doc, dict):
        for k, v in doc.items():
            if not isinstance(k, str) or not RE_IDENT.match(k):
                bad.append("%s:key" % path)
                continue
            bad += check_facts(v, "%s.%s" % (path, k), depth + 1)
    elif isinstance(doc, list):
        if len(doc) > 40:
            bad.append(path + ":long")
        for i, v in enumerate(doc[:40]):
            bad += check_facts(v, "%s[%d]" % (path, i), depth + 1)
    elif isinstance(doc, str):
        if not RE_VAL.match(doc):
            bad.append(path + ":str")
    elif isinstance(doc, float):
        if not math.isfinite(doc):
            bad.append(path + ":nan")
    elif doc is None or isinstance(doc, (bool, int)):
        pass
    else:
        bad.append(path + ":type")
    return bad


def write_facts(root, facts):
    """Атомарная запись; документ с нарушением формата не записывается (ValueError)."""
    bad = check_facts(facts)
    if bad:
        raise ValueError("facts: нарушения формата: %s" % ",".join(bad[:3]))
    filelock.atomic_write_json(facts_path(root), facts, indent=None)


def facts_signature(facts):
    """Содержимое без метки времени и возрастов: для решения «изменилось ли»."""
    d = {k: v for k, v in facts.items() if k not in ("ts", "sources")}
    return json.dumps(d, sort_keys=True)
