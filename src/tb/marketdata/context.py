"""Опрос медленных данных -> runtime/context.json. Только stdlib, Python 3.9.

    ./tb context [--root DIR] [--config config/context_config.json] [--once] [--selftest]

Источники (адреса и пороги ТОЛЬКО в config/context_config.json):
  dvol_<SYM>     Deribit get_volatility_index_data
  fear_greed     alternative.me /fng/
  global         CoinGecko /api/v3/global (доминация BTC, общий объём)
  derivs_<SYM>   funding / open interest / long-short ratio: OKX, при отказе Bybit
  macro          локальный календарь config/macro_events.json, окно +-macro_block_minutes

Форматы ответов написаны по памяти документации и НЕ ПРОВЕРЕНЫ ВЖИВУЮ; парсинг защитный.
Недоступный источник: ok:false + запись в degraded_sources, но risk_off он сам НЕ включает.
"""
import argparse
import json
import os
import sys
import tempfile
import time

from tb import paths  # noqa: E402
from tb.marketdata.data import epoch_to_iso, parse_ts  # noqa: E402
from tb.marketdata.history import http_get_json  # noqa: E402,F401  (тонкая сетевая функция; тесты подменяют context.http_get_json)
from tb import fsutil

_now = time.time
_sleep = time.sleep


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _first(lst):
    if isinstance(lst, list) and lst:
        return lst[0]
    raise ValueError("пустой список в ответе")


# ---------- загрузчики отдельных источников: -> dict значений или исключение ----------

def fetch_dvol(cfg, ccy, now):
    sc = cfg["sources"]["dvol"]
    params = {"currency": ccy, "resolution": str(sc["resolution"]),
              "start_timestamp": str(int((now - sc["lookback_s"]) * 1000)), "end_timestamp": str(int(now * 1000))}
    resp = http_get_json(sc["url"], params, cfg["timeout_s"])
    rows = resp["result"]["data"]  # [[ts_ms, open, high, low, close], ...]
    last = None
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 5 and _f(r[4]) is not None:
            if last is None or r[0] >= last[0]:
                last = r
    if last is None:
        raise ValueError("DVOL: нет валидных точек")
    return {"value": _f(last[4]), "data_ts": epoch_to_iso(float(last[0]) / 1000.0)}


def fetch_fear_greed(cfg, now):
    resp = http_get_json(cfg["sources"]["fear_greed"]["url"], {"limit": "1", "format": "json"}, cfg["timeout_s"])
    item = _first(resp["data"])
    v = _f(item["value"])
    if v is None:
        raise ValueError("F&G: нет value")
    out = {"value": v, "classification": item.get("value_classification")}
    t = _f(item.get("timestamp"))
    if t:
        out["data_ts"] = epoch_to_iso(t)
    return out


def fetch_global(cfg, now):
    resp = http_get_json(cfg["sources"]["global"]["url"], {}, cfg["timeout_s"])
    d = resp["data"]
    dom = _f((d.get("market_cap_percentage") or {}).get("btc"))
    vol = _f((d.get("total_volume") or {}).get("usd"))
    mcap = _f((d.get("total_market_cap") or {}).get("usd"))
    if dom is None and vol is None:
        raise ValueError("CoinGecko: нет данных")
    return {"btc_dominance_pct": dom, "total_volume_usd": vol, "total_mcap_usd": mcap,
            "mcap_change_24h_pct": _f(d.get("market_cap_change_percentage_24h_usd"))}


def _derivs_okx(cfg, inst):
    c = cfg["sources"]["derivs"]["okx"]
    t = cfg["timeout_s"]
    out, missing = {}, []
    try:
        d = _first(http_get_json(c["funding_url"], {"instId": inst["okx_inst"]}, t)["data"])
        rate = _f(d.get("fundingRate"))
        ft, nt = _f(d.get("fundingTime")), _f(d.get("nextFundingTime"))
        interval_h = (nt - ft) / 3.6e6 if ft and nt and nt > ft else 8.0
        out.update({"funding_rate": rate, "funding_interval_h": interval_h,
                    "funding_hourly": rate / interval_h if rate is not None else None})
    except Exception as e:  # noqa: BLE001 — частичный отказ допустим
        missing.append("funding: %s" % e)
    try:
        d = _first(http_get_json(c["oi_url"], {"instType": "SWAP", "instId": inst["okx_inst"]}, t)["data"])
        out.update({"oi": _f(d.get("oi")), "oi_ccy": _f(d.get("oiCcy"))})
    except Exception as e:  # noqa: BLE001
        missing.append("oi: %s" % e)
    try:
        d = _first(http_get_json(c["ls_url"], {"ccy": inst["okx_ccy"], "period": c["ls_period"]}, t)["data"])
        out["long_short_ratio"] = _f(d[1])  # [[ts_ms, ratio], ...] новые первыми
    except Exception as e:  # noqa: BLE001
        missing.append("long_short: %s" % e)
    return out, missing


def _derivs_bybit(cfg, inst):
    c = cfg["sources"]["derivs"]["bybit"]
    t = cfg["timeout_s"]
    out, missing = {}, []
    try:
        d = _first(http_get_json(c["ticker_url"], {"category": "linear", "symbol": inst["bybit_symbol"]}, t)
                   ["result"]["list"])
        rate = _f(d.get("fundingRate"))
        interval_h = _f(d.get("fundingIntervalHour")) or 8.0
        out.update({"funding_rate": rate, "funding_interval_h": interval_h,
                    "funding_hourly": rate / interval_h if rate is not None else None,
                    "oi": _f(d.get("openInterest")), "oi_value_usd": _f(d.get("openInterestValue"))})
    except Exception as e:  # noqa: BLE001
        missing.append("ticker: %s" % e)
    try:
        d = _first(http_get_json(c["ls_url"], {"category": "linear", "symbol": inst["bybit_symbol"],
                                                "period": c["ls_period"], "limit": "1"}, t)["result"]["list"])
        b, s = _f(d.get("buyRatio")), _f(d.get("sellRatio"))
        out["long_short_ratio"] = b / s if b is not None and s else None
    except Exception as e:  # noqa: BLE001
        missing.append("long_short: %s" % e)
    return out, missing


def fetch_derivs(cfg, sym, now):
    inst = cfg["inst"][sym]
    errors = []
    for name in cfg["sources"]["derivs"].get("order", ["okx", "bybit"]):
        fn = {"okx": _derivs_okx, "bybit": _derivs_bybit}.get(name)
        if fn is None:
            continue
        try:
            out, missing = fn(cfg, inst)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (name, e))
            continue
        if out.get("funding_rate") is None and out.get("oi") is None:
            errors.append("%s: %s" % (name, "; ".join(missing) or "нет данных"))
            continue
        out["exchange"] = name
        if missing:
            out["partial"] = missing
        return out
    raise ValueError("; ".join(errors) or "нет источников derivs")


# ---------- макро-календарь ----------

def compute_macro(events, now, block_minutes, impacts):
    """-> dict. Окно активно, если |now - ts| <= block_minutes для события с impact из impacts."""
    win = float(block_minutes) * 60.0
    active, nxt = [], None
    for ev in events:
        try:
            ts = parse_ts(ev["ts_iso"])
        except (KeyError, ValueError, TypeError):
            continue
        if str(ev.get("impact", "high")).lower() not in impacts:
            continue
        rec = {"name": ev.get("name", "?"), "ts_iso": ev["ts_iso"], "impact": ev.get("impact", "high")}
        if abs(now - ts) <= win:
            active.append(dict(rec, until_epoch=round(ts + win, 1)))        # конец окна: дашборд показывает, сколько ещё действует risk_off
        if ts > now and (nxt is None or ts < nxt[0]):
            nxt = (ts, rec)
    out = {"active": bool(active), "active_events": active, "block_minutes": block_minutes}
    if nxt:
        out["next_event"] = nxt[1]
        out["minutes_to_next"] = round((nxt[0] - now) / 60.0, 1)
    return out


def intel_union(m, root, now):
    """WP13: объединяет макро-окно с runtime/intel/blackouts.json (ручные окна watch_config + события календаря intel). Нет файла/ошибка -> m без изменений."""
    try:
        with open(paths.runtime("intel", "blackouts.json", root=root), encoding="utf-8") as f:
            wins = json.load(f)["windows"]
        for w in wins:
            a, z = parse_ts(w["from"]), parse_ts(w["to"])
            rec = {"name": str(w.get("name", "?")), "ts_iso": w["from"], "impact": "high"}
            if a <= now <= z:
                m["active"] = True
                m.setdefault("active_events", []).append(dict(rec, until_epoch=round(z, 1)))
            elif a > now and (m.get("next_event") is None or (a - now) / 60.0 < m.get("minutes_to_next", 1e18)):
                m["next_event"], m["minutes_to_next"] = rec, round((a - now) / 60.0, 1)
        m["ok"] = True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return m


def load_events(path):
    with open(path, encoding="utf-8") as f:
        ev = json.load(f)
    if not isinstance(ev, list):
        raise ValueError("macro_events.json должен быть списком")
    return ev


# ---------- итог ----------

def compute_risk(sources, cfg):
    """Чистая функция: sources -> (risk_off, reasons, degraded). Недоступные источники риск не включают."""
    th = cfg["thresholds"]
    reasons, degraded = [], []
    for name in sorted(sources):
        s = sources[name]
        if not s.get("ok"):
            degraded.append(name)
            continue
        if name == "macro" and s.get("active"):
            reasons.append("macro_window: " + ", ".join(e["name"] for e in s["active_events"]))
        elif name.startswith("dvol_"):
            sym = name[5:]
            lim = th.get("dvol_max", {}).get(sym)
            v = _f(s.get("value"))
            if lim is not None and v is not None and v > lim:
                reasons.append("dvol_high: %s %.1f > %.1f" % (sym, v, lim))
        elif name == "fear_greed":
            v = _f(s.get("value"))
            if v is not None and th.get("fng_low") is not None and v <= th["fng_low"]:
                reasons.append("fear_greed_extreme_fear: %g <= %g" % (v, th["fng_low"]))
            elif v is not None and th.get("fng_high") is not None and v >= th["fng_high"]:
                reasons.append("fear_greed_extreme_greed: %g >= %g" % (v, th["fng_high"]))
    return bool(reasons), reasons, degraded


def atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    if not os.path.isdir(d):
        os.makedirs(d)
    fd, tmp = tempfile.mkstemp(prefix=".context.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _jobs(cfg):
    """[(source_name, interval_s, fn(now)->dict)] для сетевых источников."""
    s = cfg["sources"]
    jobs = []
    if s.get("dvol", {}).get("enabled"):
        for sym in cfg["symbols"]:
            jobs.append(("dvol_" + sym, s["dvol"]["interval_s"],
                         lambda now, sym=sym: fetch_dvol(cfg, cfg["inst"][sym]["deribit_ccy"], now)))
    if s.get("fear_greed", {}).get("enabled"):
        jobs.append(("fear_greed", s["fear_greed"]["interval_s"], lambda now: fetch_fear_greed(cfg, now)))
    if s.get("global", {}).get("enabled"):
        jobs.append(("global", s["global"]["interval_s"], lambda now: fetch_global(cfg, now)))
    if s.get("derivs", {}).get("enabled"):
        for sym in cfg["symbols"]:
            jobs.append(("derivs_" + sym, s["derivs"]["interval_s"],
                         lambda now, sym=sym: fetch_derivs(cfg, sym, now)))
    return jobs


def _resolve(root, p):
    """Путь из конфига: абсолютный как есть; имя без папки -> config/ (macro_events.json); иначе от корня проекта."""
    if os.path.isabs(p):
        return p
    if os.path.dirname(p) == "":
        return paths.config(p, root=root)
    return os.path.join(root, p)


def poll_cycle(cfg, root, cache, now=None):
    """Один цикл: опрашивает источники, у которых подошёл срок, пересчитывает итог, атомарно пишет файл."""
    now = _now() if now is None else now
    for name, interval, fn in _jobs(cfg):
        c = cache.setdefault(name, {"next_due": 0.0, "entry": None})
        if now < c["next_due"]:
            continue
        try:
            entry = fn(now)
            entry.update({"ok": True, "ts": epoch_to_iso(now), "ts_epoch": now})
            c["last_ok_ts"] = now
        except Exception as e:  # noqa: BLE001 — любой отказ источника = ok:false
            entry = {"ok": False, "ts": epoch_to_iso(now), "ts_epoch": now, "error": "%s: %s" % (type(e).__name__, e)}
            if "last_ok_ts" in c:
                entry["last_ok_ts"] = epoch_to_iso(c["last_ok_ts"])
        c["entry"] = entry
        c["next_due"] = now + interval
    sources = {n: c["entry"] for n, c in cache.items() if c.get("entry") and not n == "macro"}
    if cfg["sources"].get("macro", {}).get("enabled", True):
        path = cfg["macro_events_file"]
        path = _resolve(root, path)
        try:
            m = compute_macro(load_events(path), now, cfg["macro_block_minutes"],
                              [i.lower() for i in cfg.get("macro_impacts", ["high"])])
            m.update({"ok": True})
        except Exception as e:  # noqa: BLE001
            m = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        m = intel_union(m, root, now)
        m.update({"ts": epoch_to_iso(now), "ts_epoch": now})
        sources["macro"] = m
    risk_off, reasons, degraded = compute_risk(sources, cfg)
    ctx = {"ts": epoch_to_iso(now), "ts_epoch": now, "risk_off": risk_off, "risk_off_reasons": reasons,
           "degraded_sources": degraded, "sources": sources}
    out = cfg["output"]
    atomic_write_json(_resolve(root, out), ctx)
    return ctx


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("poll_seconds", "timeout_s", "output", "symbols", "inst", "thresholds", "sources",
              "macro_events_file", "macro_block_minutes"):
        if k not in cfg:
            raise ValueError("context_config.json: нет ключа %s" % k)
    return cfg


# ---------- встроенные фикстуры (НЕ ПРОВЕРЕНО ВЖИВУЮ: формат по памяти документации) ----------

def fixture_router(url, params, timeout=10):
    p = params or {}
    if "deribit" in url:
        base = 1.7e12
        return {"result": {"data": [[base, 50, 51, 49, 50.5], [base + 60000, 50.5, 52, 50, 51.25]]}}
    if "alternative.me" in url:
        return {"data": [{"value": "55", "value_classification": "Greed", "timestamp": "1793000000"}]}
    if "coingecko" in url:
        return {"data": {"market_cap_percentage": {"btc": 52.3, "eth": 17.1}, "total_volume": {"usd": 9.9e10},
                         "total_market_cap": {"usd": 3.1e12}, "market_cap_change_percentage_24h_usd": -0.4}}
    if "okx.com" in url and "funding-rate" in url:
        return {"code": "0", "data": [{"fundingRate": "0.0001", "fundingTime": "1793000000000",
                                       "nextFundingTime": "1793028800000"}]}
    if "okx.com" in url and "open-interest" in url:
        return {"code": "0", "data": [{"oi": "123456", "oiCcy": "1234.5"}]}
    if "okx.com" in url and "long-short" in url:
        return {"code": "0", "data": [["1793000000000", "1.35"], ["1792999700000", "1.30"]]}
    if "bybit" in url and "tickers" in url:
        return {"retCode": 0, "result": {"list": [{"fundingRate": "0.0001", "openInterest": "5000",
                                                    "openInterestValue": "1.1e7"}]}}
    if "bybit" in url and "account-ratio" in url:
        return {"retCode": 0, "result": {"list": [{"buyRatio": "0.55", "sellRatio": "0.45"}]}}
    raise ValueError("fixture: неизвестный URL %s %s" % (url, p))


def selftest(config_path):
    global http_get_json
    cfg = load_config(config_path)
    real = http_get_json
    http_get_json = fixture_router
    try:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "config"))
            with open(paths.config("macro_events.json", root=d), "w") as f:
                json.dump([], f)
            ctx = poll_cycle(cfg, d, {}, now=1793000100.0)
            with open(_resolve(d, cfg["output"])) as f:
                disk = json.load(f)
    finally:
        http_get_json = real
    assert disk == json.loads(json.dumps(ctx)), "файл не совпал с результатом"
    assert not ctx["degraded_sources"], ctx["degraded_sources"]
    assert ctx["risk_off"] is False, ctx["risk_off_reasons"]
    assert ctx["sources"]["dvol_ETH"]["value"] == 51.25
    assert ctx["sources"]["derivs_BTC"]["exchange"] == "okx"
    print("selftest OK: sources=%s" % ",".join(sorted(ctx["sources"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Опрос медленных данных -> runtime/context.json")
    ap.add_argument("--root", default=paths.root())
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    config = a.config or paths.config("context_config.json", root=a.root)
    if a.selftest:
        return selftest(config)
    cfg = load_config(config)
    cache = {}
    try:
        while True:
            ctx = poll_cycle(cfg, a.root, cache)
            print(json.dumps({"ts": ctx["ts"], "risk_off": ctx["risk_off"], "reasons": ctx["risk_off_reasons"],
                              "degraded": ctx["degraded_sources"]}, ensure_ascii=False), flush=True)
            if a.once:
                return 0
            _sleep(cfg["poll_seconds"])
    except KeyboardInterrupt:
        print("остановлено", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
