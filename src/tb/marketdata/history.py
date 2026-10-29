"""REST-загрузчик истории минутных свечей. Только stdlib, Python 3.9.

    ./tb history --sym ETH --days 30 [--source okx|bybit] [--dry-run]

Пишет <out>/candles_<SYM>.csv (ts_iso,o,h,l,c,volume) и <out>/ticks_<SYM>_hist.csv (ts_iso,price=close).
Живой <out>/ticks_<SYM>.csv НЕ трогается. ts_iso = время ОТКРЫТИЯ минутной свечи, price = её close
(так data.to_bars(…, 60) восстанавливает исходную свечу без заглядывания вперёд).

Форматы ответов написаны по памяти документации и НЕ ПРОВЕРЕНЫ ВЖИВУЮ:
  OKX   GET /api/v5/market/history-candles?instId=ETH-USDT-SWAP&bar=1m&limit=100&after=<ms>
        -> {"code":"0","data":[[ts_ms,o,h,l,c,vol,volCcy,volCcyQuote,confirm],...]} новые -> старые;
        after=ts возвращает записи СТАРШЕ ts. Лимит ~20 запросов / 2 с.
  Bybit GET /v5/market/kline?category=linear&symbol=ETHUSDT&interval=1&end=<ms>&limit=1000
        -> {"retCode":0,"result":{"list":[[start_ms,o,h,l,c,volume,turnover],...]}} новые -> старые.
Парсинг защитный: битые строки пропускаются и считаются в отчёте.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tb import paths  # noqa: E402
from tb.marketdata.data import epoch_to_iso, parse_ts  # noqa: E402
from tb import fsutil

BAR_S = 60
OKX_BAR = "1m"
BYBIT_INTERVAL = "1"
SUFFIX = ""  # "_1h" для часовых свечей, чтобы не смешивать с минутными файлами
OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
OKX_LIMIT = 100
BYBIT_LIMIT = 1000
OKX_PAUSE_S = 0.15      # ~13 запросов / 2 с при лимите 20 / 2 с
BYBIT_PAUSE_S = 0.2
HTTP_TIMEOUT_S = 10.0
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
OUTLIER_FRAC = 0.20     # |close/prev_close - 1| > 20% за минуту -> пометка
USER_AGENT = "trader-history/1.0"

_sleep = time.sleep     # подменяется в тестах
_now = time.time


class HttpError(Exception):
    def __init__(self, status, msg="", retry_after=None):
        Exception.__init__(self, "HTTP %s %s" % (status, msg))
        self.status = status
        self.retry_after = retry_after


class FetchError(Exception):
    pass


def http_get_json(url, params, timeout=HTTP_TIMEOUT_S):
    """Единственная сетевая функция. -> разобранный JSON. HTTP-ошибки -> HttpError, сеть -> URLError/OSError."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        ra = None
        try:
            ra = float(e.headers.get("Retry-After")) if e.headers and e.headers.get("Retry-After") else None
        except (ValueError, TypeError):
            ra = None
        raise HttpError(e.code, str(e.reason), ra)
    return json.loads(body)


def fetch_json(url, params, timeout=HTTP_TIMEOUT_S, retries=MAX_RETRIES):
    """http_get_json с повторами: 429/5xx/сетевые ошибки/биржевые коды rate limit -> экспоненциальный backoff
    (с уважением Retry-After). 4xx кроме 429 -> сразу FetchError."""
    attempt = 0
    while True:
        try:
            resp = http_get_json(url, params, timeout)
            if isinstance(resp, dict) and str(resp.get("code", "0")) in ("50011", "50061"):
                raise HttpError(429, "okx body rate limit")
            if isinstance(resp, dict) and resp.get("retCode") in (10006, 10018):
                raise HttpError(429, "bybit body rate limit")
            return resp
        except HttpError as e:
            if e.status != 429 and e.status < 500:
                raise FetchError("HTTP %s (не повторяется)" % e.status)
            delay = e.retry_after if e.retry_after else min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** attempt))
            err = e
        except (urllib.error.URLError, OSError, ValueError) as e:  # включая таймауты и битый JSON
            delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** attempt))
            err = e
        attempt += 1
        if attempt > retries:
            raise FetchError("исчерпаны повторы: %s" % err)
        _sleep(delay)


# ---------- парсинг ----------

def _row(ts_s, o, h, l, c, v):
    try:
        return (int(ts_s), float(o), float(h), float(l), float(c), float(v))
    except (ValueError, TypeError):
        return None


def parse_okx(resp):
    """-> (rows[(ts_s,o,h,l,c,vol)], bad_count). Незакрытая свеча (confirm=0) отбрасывается."""
    rows, bad = [], 0
    if not isinstance(resp, dict) or str(resp.get("code")) != "0" or not isinstance(resp.get("data"), list):
        raise FetchError("OKX: неожиданный ответ: %s" % str(resp)[:200])
    for it in resp["data"]:
        try:
            if len(it) >= 9 and str(it[8]) == "0":
                continue
            r = _row(float(it[0]) / 1000.0, it[1], it[2], it[3], it[4], it[5])
        except (TypeError, ValueError, IndexError):
            r = None
        if r is None:
            bad += 1
        else:
            rows.append(r)
    return rows, bad


def parse_bybit(resp):
    rows, bad = [], 0
    if (not isinstance(resp, dict) or resp.get("retCode") != 0 or not isinstance(resp.get("result"), dict)
            or not isinstance(resp["result"].get("list"), list)):
        raise FetchError("Bybit: неожиданный ответ: %s" % str(resp)[:200])
    for it in resp["result"]["list"]:
        try:
            r = _row(float(it[0]) / 1000.0, it[1], it[2], it[3], it[4], it[5])
        except (TypeError, ValueError, IndexError):
            r = None
        if r is None:
            bad += 1
        else:
            rows.append(r)
    return rows, bad


def okx_inst(sym):
    return "%s-USDT-SWAP" % sym.upper()


def bybit_symbol(sym):
    return "%sUSDT" % sym.upper()


# ---------- постраничная загрузка (новые -> старые) ----------

def _print_progress(sym, every=25):
    """Строка прогресса в stderr раз в `every` страниц, чтобы долгая загрузка не выглядела зависшей."""
    def cb(pages, oldest):
        if pages % every == 0:
            sys.stderr.write("%s: страниц %d, дошли до %s\n" % (sym, pages, epoch_to_iso(oldest)))
            sys.stderr.flush()
    return cb


def fetch_range(source, sym, since_epoch, until_epoch, pause=None, max_pages=5000, progress=None):
    """Свечи с open-time в [since, until]. -> (rows, bad_count, pages)."""
    if source == "okx":
        url, parse, limit = OKX_URL, parse_okx, OKX_LIMIT
        pause = OKX_PAUSE_S if pause is None else pause
    elif source == "bybit":
        url, parse, limit = BYBIT_URL, parse_bybit, BYBIT_LIMIT
        pause = BYBIT_PAUSE_S if pause is None else pause
    else:
        raise ValueError("unknown source %s" % source)
    cursor_ms = int(until_epoch * 1000) + 1  # OKX: строго старше; Bybit: end включительно (безвреден)
    out, bad, pages = {}, 0, 0
    while pages < max_pages:
        if source == "okx":
            params = {"instId": okx_inst(sym), "bar": OKX_BAR, "limit": str(limit), "after": str(cursor_ms)}
        else:
            params = {"category": "linear", "symbol": bybit_symbol(sym), "interval": BYBIT_INTERVAL,
                      "limit": str(limit), "end": str(cursor_ms - 1)}
        resp = fetch_json(url, params)
        rows, b = parse(resp)
        bad += b
        pages += 1
        if not rows:
            break
        for r in rows:
            if since_epoch <= r[0] <= until_epoch:
                out[r[0]] = r
        oldest = min(r[0] for r in rows)
        if progress:
            progress(pages, oldest)
        if oldest <= since_epoch:
            break
        new_cursor = oldest * 1000
        if new_cursor >= cursor_ms:  # нет прогресса -> защита от зацикливания
            break
        cursor_ms = new_cursor
        if pause:
            _sleep(pause)
    return sorted(out.values()), bad, pages


# ---------- хранение ----------

def read_candles(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.strip().split(",")
            if len(p) < 6 or p[0].startswith("ts"):
                continue
            try:
                rows.append((int(parse_ts(p[0])),) + tuple(float(x) for x in p[1:6]))
            except (ValueError, TypeError):
                continue
    return rows


def merge_candles(*lists):
    """Дедупликация по ts (поздние списки перекрывают ранние) + сортировка."""
    d = {}
    for lst in lists:
        for r in lst:
            d[r[0]] = r
    return [d[k] for k in sorted(d)]


def validate(rows, outlier_frac=OUTLIER_FRAC):
    """-> (clean_rows, report). Нулевые/отрицательные/NaN цены и несогласованные OHLC исключаются из ВЫВОДА, но
    считаются и перечисляются в отчёте. Выбросы (>outlier_frac за минуту) остаются в данных и помечаются."""
    clean, dropped, outliers, gaps = [], [], [], []
    for r in rows:
        ts, o, h, l, c, v = r
        vals = (o, h, l, c)
        if any((not (x > 0)) or x != x or x in (float("inf"),) for x in vals):
            dropped.append({"ts_iso": epoch_to_iso(ts), "reason": "нулевая/невалидная цена"})
            continue
        if h < max(o, c, l) * (1 - 1e-12) or l > min(o, c, h) * (1 + 1e-12):
            dropped.append({"ts_iso": epoch_to_iso(ts), "reason": "несогласованный OHLC"})
            continue
        clean.append(r)
    for i in range(1, len(clean)):
        dt = clean[i][0] - clean[i - 1][0]
        if dt > BAR_S:
            gaps.append({"from": epoch_to_iso(clean[i - 1][0]), "to": epoch_to_iso(clean[i][0]),
                         "missing_bars": int(dt // BAR_S) - 1})
        prev_c = clean[i - 1][4]
        move = clean[i][4] / prev_c - 1.0
        if abs(move) > outlier_frac:
            outliers.append({"ts_iso": epoch_to_iso(clean[i][0]), "prev_close": prev_c,
                             "close": clean[i][4], "move": round(move, 4)})
    report = {
        "rows": len(clean), "dropped": len(dropped), "dropped_detail": dropped[:50],
        "gaps": len(gaps), "missing_bars_total": sum(g["missing_bars"] for g in gaps), "gaps_detail": gaps[:50],
        "outliers": len(outliers), "outliers_detail": outliers[:50],
        "first": epoch_to_iso(clean[0][0]) if clean else None,
        "last": epoch_to_iso(clean[-1][0]) if clean else None,
    }
    return clean, report


def _atomic_write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    fsutil.replace(tmp, path)


def write_outputs(out_dir, sym, rows):
    cpath = os.path.join(out_dir, "candles_%s%s.csv" % (sym, SUFFIX))
    tpath = os.path.join(out_dir, "ticks_%s_hist%s.csv" % (sym, SUFFIX))
    _atomic_write(cpath, "ts_iso,o,h,l,c,volume\n" + "".join(
        "%s,%r,%r,%r,%r,%r\n" % (epoch_to_iso(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows))
    _atomic_write(tpath, "ts_iso,price\n" + "".join("%s,%r\n" % (epoch_to_iso(r[0]), r[4]) for r in rows))
    return cpath, tpath


def plan(out_dir, sym, days, now=None):
    """Окно загрузки с учётом докачки. -> dict(since, until, resume, existing_rows)."""
    now = _now() if now is None else now
    until = int(now // BAR_S) * BAR_S - BAR_S  # последняя полностью закрытая минута
    start = until - int(days * 86400)
    existing = read_candles(os.path.join(out_dir, "candles_%s%s.csv" % (sym, SUFFIX)))
    since, resume = start, False
    if existing and existing[0][0] <= start + 2 * BAR_S:
        since, resume = max(start, existing[-1][0]), True  # перекрытие в одну свечу — дедуп склеит
    return {"since": since, "until": until, "resume": resume, "existing": existing}


def run(sym, days, out_dir, source="okx", dry_run=False, fallback=True, now=None):
    p = plan(out_dir, sym, days, now)
    info = {"sym": sym, "source": source, "since": epoch_to_iso(p["since"]), "until": epoch_to_iso(p["until"]),
            "resume": p["resume"], "existing_rows": len(p["existing"]), "dry_run": dry_run}
    per_page = OKX_LIMIT if source == "okx" else BYBIT_LIMIT
    info["est_requests"] = max(0, (p["until"] - p["since"]) // BAR_S // per_page + 1)
    if dry_run:
        return info
    sources = [source] + ([s for s in ("okx", "bybit") if s != source] if fallback else [])
    new, bad, pages, used, errors = None, 0, 0, None, []
    for s in sources:
        try:
            new, bad, pages = fetch_range(s, sym, p["since"], p["until"], progress=_print_progress(sym))
            used = s
            break
        except FetchError as e:
            errors.append("%s: %s" % (s, e))
    if new is None:
        info["ok"] = False
        info["errors"] = errors
        return info
    merged = merge_candles(p["existing"], new)
    clean, report = validate(merged)
    cpath, tpath = write_outputs(out_dir, sym, clean)
    report.update({"unparsed_rows": bad, "pages": pages, "fetched": len(new)})
    info.update({"ok": True, "used_source": used, "errors": errors, "report": report,
                 "candles": cpath, "ticks_hist": tpath})
    _atomic_write(os.path.join(out_dir, "history_report_%s.json" % sym), json.dumps(info, indent=1) + "\n")
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="Загрузка минутных свечей OKX/Bybit")
    ap.add_argument("--sym", required=True, choices=["ETH", "BTC", "SOL", "HYPE", "ZEC"])
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--out", default=None, help="папка вывода (по умолчанию data/ проекта)")
    ap.add_argument("--source", choices=["okx", "bybit"], default="okx")
    ap.add_argument("--bar", choices=["1m", "1h", "1d"], default="1m",
                    help="1h/1d: часовые/дневные свечи (в 60/1440 раз меньше запросов), файлы с суффиксом _1h/_1d")
    ap.add_argument("--dry-run", action="store_true", help="только план: без сети и без записи")
    ap.add_argument("--no-fallback", action="store_true", help="не переключаться на другой источник при отказе")
    a = ap.parse_args(argv)
    global BAR_S, OKX_BAR, BYBIT_INTERVAL, SUFFIX
    if a.bar == "1h":
        BAR_S, OKX_BAR, BYBIT_INTERVAL, SUFFIX = 3600, "1H", "60", "_1h"
    elif a.bar == "1d":
        BAR_S, OKX_BAR, BYBIT_INTERVAL, SUFFIX = 86400, "1Dutc", "D", "_1d"  # OKX "1D" = сутки по UTC+8, нужен "1Dutc"
    info = run(a.sym, a.days, a.out or paths.data(), a.source, a.dry_run, not a.no_fallback)
    print(json.dumps(info, indent=1, ensure_ascii=False))
    return 0 if info.get("ok", a.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
