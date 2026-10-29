"""Учёт вызовов: цена, запись `state/llm_calls.jsonl`, чтение и агрегаты (WP11-C1, 07b п. 6.6).

Строка (без текста промптов и ответов, без секретов):
 {"ts","t","job","role","team","agent","profile","provider","model","prompt_hash","rev","event_seq","in","in_hit","in_write","out","reasoning",
  "usd","lat_ms","ok","err","fmt_err","retries","fallback_from","finish","round"}
Строка kind="fmt" (ошибка формата ответа, найденная после вызова): {"kind":"fmt","ts","t","job","role",...,"provider","model","err":"BAD_ENUM:x","fmt_err":true}.
`model` = ключ модели из providers.json (не имя у провайдера), чтобы смена имени не рвала историю.
"""
import hashlib
import json
import os
import re
import time

from tb import paths
from tb.core.filelock import FileLock

MAX_READ = 32 * 1024 * 1024
_RE_FIELD = re.compile(r"^[A-Za-z0-9_.:@-]{1,60}$")
META_KEYS = ("job", "role", "team", "agent", "profile", "prompt_hash", "fallback_from")
META_NUM = ("rev", "event_seq", "round")


def calls_file(root=None):
    return paths.state("llm_calls.jsonl", root=root)


def calls_lock(root=None):
    return paths.state("llm_calls.jsonl.lock", root=root)


def prompt_hash(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------- цена
def is_peak(peak_utc, ts):
    """Пиковое ли время (UTC). Нет описания окон -> True (цены конфига считаются пиковыми, скидка не применяется)."""
    if not peak_utc:
        return True
    g = time.gmtime(ts)
    if g.tm_wday not in peak_utc.get("days", []):
        return False
    return any(a <= g.tm_hour < b for a, b in peak_utc.get("hours", []))


def price_usd(price, usage, ts=None):
    """Стоимость вызова в USD по ценам модели (за 1M токенов): miss*in_miss(in) + hit*in_hit + write*in*cache_write_factor + out*out;
    вне пика цены умножаются на offpeak_factor (если задан). Нет цены -> 0.0."""
    if not price:
        return 0.0
    base = price.get("in_miss", price.get("in", 0.0))
    hit = price.get("in_hit", base)
    wf = price.get("cache_write_factor", 1.0)
    usd = (usage.miss * base + usage.hit * hit + usage.write * base * wf + usage.out * price.get("out", 0.0)) / 1e6
    if price.get("offpeak_factor") is not None and not is_peak(price.get("peak_utc"), time.time() if ts is None else ts):
        usd *= price["offpeak_factor"]
    return usd


# ---------------------------------------------------------------- запись
def _fld(v):
    return v if isinstance(v, str) and _RE_FIELD.match(v) else None


def _meta_fields(meta):
    meta = meta or {}
    out = {}
    for k in META_KEYS:
        out[k] = _fld(meta.get(k))
    for k in META_NUM:
        v = meta.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
    return out


def _append(row, root):
    path = calls_file(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with FileLock(calls_lock(root), wait=5.0):
            with open(path, "ab") as f:
                f.write((json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii"))
        return True
    except (OSError, TimeoutError):
        return False


def record(meta, provider, model, usage=None, usd=0.0, lat_ms=0, ok=True, err=None, retries=0, finish=None, root=None, now=None):
    """Дописывает строку вызова -> True/False. Сбой записи учёта не роняет вызов."""
    now = time.time() if now is None else now
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "t": round(now, 3)}
    row.update(_meta_fields(meta))
    u = usage.as_dict() if usage is not None else {"in": 0, "in_hit": 0, "in_write": 0, "out": 0, "reasoning": 0}
    row.update({"provider": _fld(provider), "model": _fld(model), "in": u["in"], "in_hit": u["in_hit"], "in_write": u["in_write"], "out": u["out"],
                "reasoning": u["reasoning"], "usd": round(float(usd), 8), "lat_ms": int(max(0, lat_ms)), "ok": bool(ok),
                "err": _fld(err) if err else None, "fmt_err": False, "retries": int(retries), "finish": _fld(finish)})
    return _append(row, root)


def record_format_error(meta, provider, model, codes, root=None, now=None):
    now = time.time() if now is None else now
    row = {"kind": "fmt", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "t": round(now, 3)}
    row.update(_meta_fields(meta))
    row.update({"provider": _fld(provider), "model": _fld(model), "err": ";".join(str(c) for c in (codes or []))[:120] or "BAD_FORMAT", "fmt_err": True})
    row["err"] = re.sub(r"[^A-Za-z0-9_.:;,\[\]-]", "?", row["err"])
    return _append(row, root)


# ---------------------------------------------------------------- чтение
def read_rows(root=None, since=None, max_bytes=MAX_READ):
    """Строки учёта (по возрастанию времени), начиная с since (unix); битые строки пропускаются."""
    path = calls_file(root)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    rows = []
    for ln in data.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("t"), (int, float)) and (since is None or r["t"] >= since):
            rows.append(r)
    return rows


def window_totals(rows, provider=None):
    usd = tokens = calls = 0
    for r in rows:
        if r.get("kind") == "fmt" or (provider and r.get("provider") != provider):
            continue
        usd += r.get("usd") or 0.0
        tokens += (r.get("in") or 0) + (r.get("out") or 0)
        calls += 1
    return {"usd": usd, "tokens": tokens, "calls": calls}


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def aggregate(rows, by="profile"):
    """Агрегаты по ключу by (profile|provider|model|role|team|agent): вызовы, токены, $, латентность p50/p95, доли ошибок и ошибок формата, fallback."""
    groups = {}
    for r in rows:
        k = r.get(by) or "?"
        g = groups.setdefault(k, {"calls": 0, "in": 0, "in_hit": 0, "out": 0, "reasoning": 0, "usd": 0.0, "errors": 0, "fmt_errors": 0, "fallbacks": 0, "_lat": []})
        if r.get("kind") == "fmt":
            g["fmt_errors"] += 1
            continue
        g["calls"] += 1
        for f in ("in", "in_hit", "out", "reasoning"):
            g[f] += r.get(f) or 0
        g["usd"] += r.get("usd") or 0.0
        if r.get("ok") is False:
            g["errors"] += 1
        if r.get("fallback_from"):
            g["fallbacks"] += 1
        if r.get("ok") and isinstance(r.get("lat_ms"), (int, float)):
            g["_lat"].append(r["lat_ms"])
    out = {}
    for k, g in groups.items():
        lat = g.pop("_lat")
        c = max(1, g["calls"])
        g.update({"usd": round(g["usd"], 6), "lat_p50_ms": _pct(lat, 0.5), "lat_p95_ms": _pct(lat, 0.95),
                  "err_rate": round(g["errors"] / float(c), 4), "fmt_err_rate": round(g["fmt_errors"] / float(c), 4),
                  "cache_hit_rate": round(g["in_hit"] / float(g["in"]), 4) if g["in"] else None})
        out[k] = g
    return out
