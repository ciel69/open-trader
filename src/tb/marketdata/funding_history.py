"""Загрузка истории funding (Bybit v5 linear) -> data/funding_<SYM>.csv (ts_iso,rate; rate = доля за одно начисление).

  ./tb funding --sym ETH --days 1460 [--dry-run]
Эндпоинт GET /v5/market/funding/history?category=linear&symbol=ETHUSDT&startTime=&endTime=&limit=200 написан по памяти,
НЕ ПРОВЕРЕНО ВЖИВУЮ: формат list[{symbol, fundingRate, fundingRateTimestamp(ms)}], новые -> старые. Парсинг защитный.
Сеть только через history.fetch_json (повторы, rate limit). Дедупликация и сортировка по времени.
"""
import argparse
import json
import os
import sys
import time

from tb import paths
from tb.marketdata import history
from tb import fsutil

URL = "https://api.bybit.com/v5/market/funding/history"
LIMIT = 200
PAUSE_S = 0.25


def parse(resp):
    rows, bad = [], 0
    try:
        lst = resp["result"]["list"]
    except (KeyError, TypeError):
        raise history.FetchError("неожиданный ответ Bybit funding: %r" % (str(resp)[:120],))
    for x in lst:
        try:
            rows.append((int(x["fundingRateTimestamp"]) // 1000, float(x["fundingRate"])))
        except (KeyError, ValueError, TypeError):
            bad += 1
    return rows, bad


def fetch(sym, since, until, fetcher=None, sleep=None):
    fetcher = fetcher or history.fetch_json
    sleep = sleep or time.sleep
    out, cursor, bad, pages = {}, until * 1000, 0, 0
    while cursor > since * 1000 and pages < 2000:
        resp = fetcher(URL, {"category": "linear", "symbol": "%sUSDT" % sym, "startTime": str(since * 1000),
                             "endTime": str(cursor), "limit": str(LIMIT)})
        rows, b = parse(resp)
        bad += b
        pages += 1
        if not rows:
            break
        for ts, r in rows:
            if since <= ts <= until:
                out[ts] = r
        oldest = min(ts for ts, _ in rows)
        if oldest * 1000 >= cursor:
            break
        cursor = oldest * 1000 - 1
        sleep(PAUSE_S)
    return sorted(out.items()), bad, pages


def write(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ts_iso,rate\n")
        for ts, r in rows:
            f.write("%s,%r\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), r))
    fsutil.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="История funding Bybit")
    ap.add_argument("--sym", required=True, choices=["ETH", "BTC", "SOL", "HYPE", "ZEC"])
    ap.add_argument("--days", type=float, default=365)
    ap.add_argument("--out", default=None, help="папка вывода (по умолчанию data/ проекта)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    until = int(time.time())
    since = until - int(a.days * 86400)
    info = {"sym": a.sym, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
            "est_requests": int(a.days * 3 / LIMIT) + 1, "dry_run": a.dry_run}
    if a.dry_run:
        print(json.dumps(info, indent=1))
        return 0
    try:
        rows, bad, pages = fetch(a.sym, since, until)
    except history.FetchError as e:
        info.update({"ok": False, "error": str(e)})
        print(json.dumps(info, indent=1, ensure_ascii=False))
        return 1
    out_dir = a.out or paths.data()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "funding_%s.csv" % a.sym)
    write(path, rows)
    gaps = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
    info.update({"ok": bool(rows), "rows": len(rows), "bad_rows": bad, "pages": pages, "file": path,
                 "first": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rows[0][0])) if rows else None,
                 "interval_h_seen": sorted(set(round(g / 3600.0, 1) for g in gaps))[:6] if gaps else []})
    print(json.dumps(info, indent=1, ensure_ascii=False))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
