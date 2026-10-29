"""Загрузка свечей Bybit USDT-перпов (публичные данные, только чтение): /v5/market/kline с объёмом и оборотом.

  ./tb bybit read klines --top 80 --interval 5 --days 40 --out data/klines [--symbols AAAUSDT,BBBUSDT] [--dry-run]

Выбор монет: топ-N по turnover24h из /v5/market/tickers (только USDT-перпы), без BTC/ETH/SOL (они в прошлых тестах) и без
стейблов. Токенизированные акции/ETF (STOCK_LIKE) помечаются группой "stock" в манифесте и по умолчанию тоже загружаются.
Файл: <out>/k<interval>_<SYMBOL>.csv: ts_iso,o,h,l,c,volume,turnover (ts_iso = время ОТКРЫТИЯ свечи, UTC; volume в монетах, turnover в USDT).
Дозагрузка: существующий файл дополняется недостающими хвостом и началом, дубликаты по времени убираются, запись атомарна.
Незакрытая свеча (открытая позже now - interval) не сохраняется. Ключ не нужен (auth=False), ответы идут через белый список клиента.
Манифест <out>/manifest_k<interval>.json: символы, группа, turnover24h на момент выбора, число свечей, период.
Формат ответа по документации Bybit v5: result.list = [[start_ms,o,h,l,c,volume,turnover],...] от новых к старым, лимит 1000.
"""
import csv
import json
import os
import sys
import time

from tb import fsutil
from tb import paths
from tb.marketdata.data import epoch_to_iso, parse_ts

KLINE_PATH = "/v5/market/kline"
LIMIT = 1000
PAUSE_S = 0.25                      # ~4 запроса/с, далеко от лимита 600 / 5 с на IP
EXCLUDE = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "USDCUSDT", "USDEUSDT", "FDUSDUSDT", "TUSDUSDT"}
# токенизированные акции/ETF/металлы/нефть на Bybit (ведут себя иначе, чем крипта: торговые часы, гэпы); список неполный, пополняется вручную
STOCK_LIKE = {"SOXL", "TQQQ", "SQQQ", "INTC", "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AMD", "MSTR", "COIN", "HOOD",
              "SPY", "QQQ", "CRCL", "PLTR", "SMCI", "MU", "AVGO", "NFLX", "ORCL", "BABA", "TSM", "USO", "SLV", "GLD", "IWM",
              "XAU", "XAG", "XAUT", "CL", "BZ", "SNDK", "KORU", "SKHYNIX", "SKHY", "SOXS", "OKTA", "SNXX", "SPCX", "SAMSUNG", "NBIS", "EWY", "DRAM"}  # + металлы и нефть
HEADER = ["ts_iso", "o", "h", "l", "c", "volume", "turnover"]


def group_of(symbol):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return "stock" if base in STOCK_LIKE else "crypto"


def pick_symbols(tickers_result, top, exclude=EXCLUDE):
    """list из /v5/market/tickers -> [(symbol, turnover24h)] по убыванию оборота, только USDT-перпы (без дат-фьючерсов), без exclude."""
    rows = []
    for it in (tickers_result or {}).get("list") or []:
        s = it.get("symbol") or ""
        if not s.endswith("USDT") or "-" in s or s in exclude:
            continue
        try:
            t = float(it.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if t > 0:
            rows.append((s, t))
    rows.sort(key=lambda x: -x[1])
    return rows[:top]


def parse_kline(result):
    """result из kline -> ([(start_s, o, h, l, c, volume, turnover)] строками как пришло, число битых). Порядок как в ответе."""
    out, bad = [], 0
    for it in (result or {}).get("list") or []:
        try:
            ts = int(it[0]) // 1000
            vals = [str(x) for x in it[1:7]]
            for v in vals:
                float(v)
            out.append((ts,) + tuple(vals))
        except (TypeError, ValueError, IndexError):
            bad += 1
    return out, bad


def load_csv(path):
    """-> {ts_s: (ts_s, o, h, l, c, volume, turnover)}"""
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.reader(f):
            if len(r) < 7 or r[0] == "ts_iso":
                continue
            try:
                ts = int(parse_ts(r[0]))
            except ValueError:
                continue
            rows[ts] = (ts,) + tuple(r[1:7])
    return rows


def save_csv(path, rows):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for ts in sorted(rows):
            r = rows[ts]
            w.writerow((epoch_to_iso(ts),) + tuple(r[1:7]))
    fsutil.replace(tmp, path)


def call_retry(c, params, sleep=time.sleep, tries=5):
    """c.call с повторами при сетевых ошибках/таймаутах/лимитах (экспоненциальная пауза 1, 2, 4, 8 с)."""
    for k in range(tries):
        try:
            return c.call("GET", KLINE_PATH, params, auth=False)
        except Exception as e:                      # BybitError (HTTP/retCode), таймаут, сброс соединения
            if k == tries - 1:
                raise
            sleep(2.0 ** k)


def fetch_span(c, symbol, interval, since_s, until_s, pause=PAUSE_S, sleep=time.sleep, max_pages=200):
    """Свечи с open-time в [since_s, until_s] постранично назад от until_s. -> ({ts: row}, bad, pages)."""
    step = int(interval) * 60
    out, bad, pages = {}, 0, 0
    end_ms = int(until_s) * 1000
    while pages < max_pages and end_ms >= since_s * 1000:
        res = call_retry(c, {"category": "linear", "symbol": symbol, "interval": str(interval),
                             "end": str(end_ms), "limit": str(LIMIT)}, sleep)
        rows, b = parse_kline(res)
        bad += b
        pages += 1
        if not rows:
            break
        for r in rows:
            if since_s <= r[0] <= until_s:
                out[r[0]] = r
        oldest = min(r[0] for r in rows)
        if oldest <= since_s or len(rows) < LIMIT:
            break
        end_ms = oldest * 1000 - 1               # end включительно: берём строго старше самой старой полученной
        sleep(pause)
    return out, bad, pages


def sync_symbol(c, symbol, interval, days, out_dir, now=None, pause=PAUSE_S, sleep=time.sleep):
    """Дозагружает файл символа до окна [now-days, последняя закрытая свеча]. -> {rows, new, bad, pages, first, last}."""
    now = int(now if now is not None else time.time())
    step = int(interval) * 60
    since = (now - int(days * 86400)) // step * step
    until = (now // step) * step - step          # последняя ЗАКРЫТАЯ свеча: открытая в (now//step)*step ещё формируется
    path = os.path.join(out_dir, "k%s_%s.csv" % (interval, symbol))
    have = load_csv(path)
    n0, bad, pages = len(have), 0, 0
    if have:
        first, last = min(have), max(have)
        spans = []
        if first > since + step:
            spans.append((since, first - step))
        if last < until:
            spans.append((last + step, until))
    else:
        spans = [(since, until)]
    for lo, hi in spans:
        got, b, p = fetch_span(c, symbol, interval, lo, hi, pause, sleep)
        have.update(got)
        bad += b
        pages += p
        sleep(pause)
    have = {t: r for t, r in have.items() if t >= since}
    if len(have) != n0 or not os.path.exists(path):
        save_csv(path, have)
    ts = sorted(have)
    return {"symbol": symbol, "rows": len(have), "new": len(have) - n0, "bad": bad, "pages": pages,
            "first": epoch_to_iso(ts[0]) if ts else None, "last": epoch_to_iso(ts[-1]) if ts else None}


def add_args(ap):
    ap.add_argument("--top", type=int, default=80, help="сколько самых ликвидных USDT-перпов (по turnover24h) брать")
    ap.add_argument("--symbols", default="", help="явный список через запятую (SYMUSDT); вместо выбора по тикерам")
    ap.add_argument("--interval", type=int, default=5, choices=[1, 3, 5, 15, 30, 60], help="минут в свече")
    ap.add_argument("--days", type=float, default=40.0)
    ap.add_argument("--out", default="", help="папка (по умолчанию data/klines)")
    ap.add_argument("--no-stocks", action="store_true", help="не загружать токенизированные акции/ETF")
    ap.add_argument("--dry-run", action="store_true", help="только показать выбор монет и оценку запросов/размера")


def main_klines(c, a, out=sys.stdout, err=sys.stderr):
    out_dir = a.out or paths.data("klines")
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    turn = {}
    if not syms:
        res = c.call("GET", "/v5/market/tickers", {"category": "linear"}, auth=False)
        picked = pick_symbols(res, a.top)
        turn = dict(picked)
        syms = [s for s, _ in picked]
    if a.no_stocks:
        syms = [s for s in syms if group_of(s) != "stock"]
    per_sym = int(a.days * 1440 / a.interval)
    pages = max(1, -(-per_sym // LIMIT))
    est = {"symbols": len(syms), "candles_per_symbol": per_sym, "requests": len(syms) * pages,
           "approx_mb": round(len(syms) * per_sym * 62 / 1e6, 1), "out": out_dir}
    if a.dry_run:
        print(json.dumps(dict(est, list=syms), ensure_ascii=False), file=out)
        return 0
    err.write("загрузка: %s\n" % json.dumps(est, ensure_ascii=False))
    manifest, failed = [], []
    for i, s in enumerate(syms, 1):
        try:
            r = sync_symbol(c, s, a.interval, a.days, out_dir)
        except Exception as e:                     # одна монета не должна ронять всю загрузку; дозагрузка повторным запуском
            failed.append(s)
            err.write("[%d/%d] %s: ОШИБКА %s\n" % (i, len(syms), s, str(e)[:120]))
            continue
        r.update({"group": group_of(s), "turnover24h": turn.get(s)})
        manifest.append(r)
        err.write("[%d/%d] %s: %d свечей (+%d) %s .. %s\n" % (i, len(syms), s, r["rows"], r["new"], r["first"], r["last"]))
    if manifest:
        mp = os.path.join(out_dir, "manifest_k%d.json" % a.interval)
        old = {}
        if os.path.exists(mp):
            try:
                with open(mp, encoding="utf-8") as f:
                    old = {x["symbol"]: x for x in json.load(f).get("symbols", [])}
            except (OSError, ValueError, KeyError, TypeError):
                old = {}
        for r in manifest:
            if r.get("turnover24h") is None and r["symbol"] in old:
                r["turnover24h"] = old[r["symbol"]].get("turnover24h")
            old[r["symbol"]] = r
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": epoch_to_iso(time.time()), "interval": a.interval, "days": a.days,
                       "symbols": sorted(old.values(), key=lambda x: x["symbol"])}, f, ensure_ascii=False, indent=1)
        fsutil.replace(tmp, mp)
    print(json.dumps({"ok": not failed, "symbols": len(manifest), "failed": failed,
                      "candles": sum(r["rows"] for r in manifest)}, ensure_ascii=False), file=out)
    return 0 if not failed else 1
