"""Описание источников фактуры и парсеры их ответов (07 п. 3.2). Владелец: WP13. Чистые функции, без сети.

Каждый источник: id, url, params, формат, частота, вид (struct = числа, text = заголовки/сырой текст, local = считается локально), `verified`.
ВСЕ источники `verified:false`, пока владелец не запустил `./tb intel verify` и не подтвердил формат (результаты: docs/spec_v2/WP13_sources.md);
эндпоинты Bybit/Deribit/alternative.me/CoinGecko частично подтверждены кодом проекта (context.py), RSS/ICS/HTML и announcements по памяти.
Переопределение в config/intel.json: sources.<id>.{enabled,url,interval_s,verified}.
Парсер: parse(parser_name, body_bytes, ctx) -> dict; ошибка формата = ParseError с КОДОМ (без цитат тела ответа). Текстовые элементы идут в ключе "items"
(сервис вынимает их до записи кэша: в кэше и facts текста третьих лиц нет).
"""
import calendar as _cal
import email.utils
import json
import re
import xml.etree.ElementTree as ET

from tb.intel import calendar as ical
from tb.intel import items as iitems

UA = "traider-bot/2"
MAX_ITEMS = 40


class ParseError(Exception):
    """Формат ответа не совпал с ожидаемым. Сообщение = короткий код (например `field:result.list`), без текста ответа."""


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _json(body):
    try:
        txt = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
        return json.loads(txt)
    except (ValueError, TypeError):
        raise ParseError("json")


def _get(d, *path):
    cur = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            raise ParseError("field:" + ".".join(str(x) for x in path))
    return cur


def _bybit(body):
    d = _json(body)
    if not isinstance(d, dict):
        raise ParseError("shape")
    if str(d.get("retCode")) != "0":
        raise ParseError("retCode:%s" % re.sub(r"[^0-9-]", "", str(d.get("retCode")))[:6])
    return d


# ---------- структурные источники ----------
def parse_bybit_time(body, ctx):
    d = _bybit(body)
    res = d.get("result") if isinstance(d.get("result"), dict) else {}
    sec = _f(res.get("timeSecond"))
    if sec is None:
        ms = _f(d.get("time"))
        sec = ms / 1000.0 if ms else None
    if sec is None:
        raise ParseError("field:result.timeSecond")
    now = ctx.get("now")
    return {"server_ts": int(sec), "drift_s": round(sec - now, 1) if now else None}


def parse_bybit_tickers(body, ctx):
    d = _bybit(body)
    rows = _get(d, "result", "list")
    if not isinstance(rows, list) or not rows:
        raise ParseError("empty:result.list")
    want = set(ctx.get("symbols") or [])
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        s = str(r.get("symbol") or "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if want and base not in want:
            continue
        fr = _f(r.get("fundingRate"))
        if fr is None:
            continue
        out[base] = {"funding_pct": round(fr * 100.0, 5), "oi_usd": _f(r.get("openInterestValue")), "turn_usd": _f(r.get("turnover24h"))}
    if want and not out:
        raise ParseError("no_wanted_symbols")
    return {"symbols": out}


def parse_bybit_oi(body, ctx):
    d = _bybit(body)
    rows = _get(d, "result", "list")
    vals = [_f(r.get("openInterest")) for r in rows if isinstance(r, dict)]            # новые первыми
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        raise ParseError("short:result.list")
    out = {"oi": vals[0], "n": len(vals)}
    base = vals[24] if len(vals) >= 25 else None
    out["oi_chg_24h_pct"] = round((vals[0] - base) / base * 100.0, 3) if base else None
    return out


def parse_bybit_ls(body, ctx):
    d = _bybit(body)
    r = _get(d, "result", "list", 0)
    b, s = _f(r.get("buyRatio")), _f(r.get("sellRatio"))
    if b is None or not s:
        raise ParseError("field:buyRatio")
    return {"ls_ratio": round(b / s, 4)}


def fng_class(v):
    """Класс индекса страха и жадности по ЧИСЛУ (текст класса из ответа не используется)."""
    return "extreme_fear" if v <= 25 else "fear" if v <= 46 else "neutral" if v <= 54 else "greed" if v <= 75 else "extreme_greed"


def parse_fng(body, ctx):
    d = _json(body)
    rows = _get(d, "data")
    if not isinstance(rows, list) or not rows:
        raise ParseError("empty:data")
    v = _f(rows[0].get("value")) if isinstance(rows[0], dict) else None
    if v is None or not 0 <= v <= 100:
        raise ParseError("field:data.0.value")
    prev = _f(rows[1].get("value")) if len(rows) > 1 and isinstance(rows[1], dict) else None
    return {"v": int(round(v)), "cls": fng_class(v), "prev": int(round(prev)) if prev is not None and 0 <= prev <= 100 else None}


def parse_dvol(body, ctx):
    d = _json(body)
    rows = _get(d, "result", "data")
    best = None
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, (list, tuple)) and len(r) >= 5 and _f(r[4]) is not None and _f(r[0]) is not None:
            if best is None or r[0] >= best[0]:
                best = r
    if best is None:
        raise ParseError("empty:result.data")
    return {"v": round(_f(best[4]), 2), "data_ts": int(_f(best[0]) / 1000.0)}


def parse_btc_dom(body, ctx):
    d = _json(body)
    v = _f(_get(d, "data", "market_cap_percentage", "btc"))
    if v is None or not 0 < v < 100:
        raise ParseError("field:market_cap_percentage.btc")
    return {"v": round(v, 2)}


# ---------- текстовые источники ----------
def _text_of(el):
    return "".join(el.itertext()) if el is not None else ""


def _local(tag):
    return tag.rpartition("}")[2].lower() if isinstance(tag, str) else ""


def parse_time_any(s):
    """RFC 822 (RSS) или ISO 8601 (Atom) -> epoch или None."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt.timestamp() if dt.tzinfo else float(_cal.timegm(dt.timetuple()))
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(m.group(i) or 0) for i in range(1, 7))
    try:
        ts = float(_cal.timegm((y, mo, d, h, mi, sec, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None
    tz = m.group(7)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        ts -= sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    return ts


def parse_feed(body, ctx):
    """RSS 2.0 / Atom. Отказ от DTD/ENTITY (взрыв сущностей); сырые поля режутся до 2000 символов, очистка делается позже (items.make_item)."""
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8", "replace")
    low = bytes(raw[:200000]).lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ParseError("xml_dtd")
    try:
        root = ET.fromstring(bytes(raw))
    except ET.ParseError:
        raise ParseError("xml")
    entries = []
    for el in root.iter():
        t = _local(el.tag)
        if t in ("item", "entry"):
            entries.append(el)
    if not entries:
        raise ParseError("empty:items")
    out = []
    for el in entries[:MAX_ITEMS]:
        f = {}
        for ch in el:
            t = _local(ch.tag)
            if t in ("title", "pubdate", "published", "updated", "date", "description", "summary", "content", "encoded") and t not in f:
                f[t] = _text_of(ch)[:2000]
            elif t == "link" and "link" not in f:
                href = ch.get("href")
                if href and (ch.get("rel") in (None, "", "alternate")):
                    f["link"] = href
                elif _text_of(ch).strip():
                    f["link"] = _text_of(ch).strip()
        ts = parse_time_any(f.get("pubdate") or f.get("published") or f.get("updated") or f.get("date"))
        out.append({"title": f.get("title", ""), "snip": f.get("description") or f.get("summary") or f.get("content") or f.get("encoded") or "",
                    "url": f.get("link", ""), "ts": ts})
    if not any(x["title"].strip() for x in out):
        raise ParseError("field:title")
    return {"items": out, "n": len(out)}


ANN_TYPE = {"new_crypto": "listing", "delistings": "delisting", "maintenance_updates": "maintenance"}
ANN_IMP = {"delisting": 3, "maintenance": 2, "listing": 2, "other": 1}
ANN_TTL = {"delisting": 2880, "maintenance": 360, "listing": 720, "other": 120}
_RE_PAIR = re.compile(r"\b([A-Z0-9]{2,15})(?:USDT|USDC|PERP)\b")
_RE_SLASH = re.compile(r"\b([A-Z0-9]{2,15})/(?:USDT|USDC)\b")
_RE_TOKEN = re.compile(r"\b[A-Z0-9]{2,12}\b")
_RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")


def ann_syms(title, known):
    """Символы из заголовка объявления биржи ТОЛЬКО по закрытым шаблонам (XXXUSDT, XXX/USDT) или из списка известных; <= 5, regex ^[A-Z0-9]{2,20}$."""
    t = title if isinstance(title, str) else ""
    hits = [(m.start(), m.group(1)) for m in list(_RE_PAIR.finditer(t)) + list(_RE_SLASH.finditer(t))]
    hits += [(m.start(), m.group(0)) for m in _RE_TOKEN.finditer(t) if m.group(0) in known]
    uniq = []
    for _pos, s in sorted(hits):
        if _RE_SYM.match(s) and s not in uniq:
            uniq.append(s)
    return uniq[:5]


def parse_bybit_ann(body, ctx):
    d = _bybit(body)
    rows = _get(d, "result", "list")
    if not isinstance(rows, list):
        raise ParseError("shape:result.list")
    known = set(ctx.get("symbols") or []) | {"BTC", "ETH", "SOL"}
    group = ctx.get("group") or "bybit_ann"
    items, ann = [], []
    for r in rows[:MAX_ITEMS]:
        if not isinstance(r, dict) or not isinstance(r.get("title"), str):
            continue
        key = r["type"].get("key") if isinstance(r.get("type"), dict) else ""
        typ = ANN_TYPE.get(key if isinstance(key, str) else "", "other")
        ts = (_f(r.get("publishTime")) or _f(r.get("dateTimestamp")) or 0) / 1000.0
        st, en = _f(r.get("startDateTimestamp")), _f(r.get("endDateTimestamp"))
        title = r["title"]
        ctitle = iitems.clean_text(title, iitems.TITLE_LEN)
        items.append({"title": title, "snip": r.get("description") if isinstance(r.get("description"), str) else "", "url": r.get("url") or "", "ts": ts or None})
        ann.append({"ref": iitems.make_ref(ctitle, group), "type": typ, "ts": int(ts) if ts else None,
                    "start": int(st / 1000.0) if st else None, "end": int(en / 1000.0) if en else None, "syms": ann_syms(title, known)})
    if rows and not items:
        raise ParseError("field:title")
    return {"items": items, "ann": ann, "n": len(items)}


# ---------- импорт календаря ----------
_ICS_KIND = (("consumer price index", "CPI"), ("employment situation", "NFP"), ("personal income and outlays", "PCE"), ("gross domestic product", "GDP"))


def parse_ics(body, ctx):
    """ICS Бюро статистики труда: CPI и Employment Situation (NFP) (и PCE/GDP) -> {"events":[{"type","ts","impact"}]}. Время TZID=America/New_York или Z."""
    txt = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if "BEGIN:VCALENDAR" not in txt:
        raise ParseError("ics")
    txt = re.sub(r"\r?\n[ \t]", "", txt)
    out = []
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", txt, re.S):
        sm = re.search(r"^SUMMARY[^:]*:(.*)$", blk, re.M)
        dm = re.search(r"^DTSTART([^:]*):(\d{8})(?:T(\d{6})(Z?))?", blk, re.M)
        if not sm or not dm:
            continue
        low = sm.group(1).lower()
        kind = next((k for needle, k in _ICS_KIND if needle in low), None)
        if not kind:
            continue
        y, mo, d = int(dm.group(2)[:4]), int(dm.group(2)[4:6]), int(dm.group(2)[6:8])
        if dm.group(3):
            hh, mi = int(dm.group(3)[:2]), int(dm.group(3)[2:4])
            ts = float(_cal.timegm((y, mo, d, hh, mi, 0, 0, 0, 0))) if dm.group(4) else ical.et_to_utc(y, mo, d, hh, mi)
        else:
            ts = ical.et_to_utc(y, mo, d, 8, 30)               # дата без времени: релиз 08:30 ET
        out.append({"type": kind, "ts": int(ts), "impact": 3 if kind in ("CPI", "NFP") else 2})
    if not out:
        raise ParseError("empty:events")
    return {"events": out[:60]}


_MONTHS = {m.lower(): i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                                               "November", "December"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def parse_fomc_html(body, ctx):
    """Страница календаря ФРС: блоки года `YYYY FOMC Meetings`, в них пары month/date (`27-28`). Решение = последний день заседания 14:00 ET. Хрупко."""
    txt = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4})\s+FOMC\s+Meetings", txt)]
    if not heads:
        raise ParseError("fomc_no_years")
    out = []
    for i, (pos, year) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(txt)
        sec = txt[pos:end]
        for mm in re.finditer(r"fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z/]+)(?:.|\n){0,400}?fomc-meeting__date[^>]*>\s*(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", sec):
            mon = mm.group(1).split("/")[-1].strip().lower()
            month = _MONTHS.get(mon) or _MONTHS.get(mon[:3])
            day = int(mm.group(3) or mm.group(2))
            if month and 1 <= day <= 31:
                try:
                    out.append({"type": "FOMC", "ts": int(ical.et_to_utc(year, month, day, 14, 0)), "impact": 3})
                except (ValueError, OverflowError):
                    pass
    if not out:
        raise ParseError("empty:events")
    return {"events": out[:30]}


PARSERS = {
    "bybit_time": parse_bybit_time, "bybit_tickers": parse_bybit_tickers, "bybit_oi": parse_bybit_oi, "bybit_ls": parse_bybit_ls,
    "fng": parse_fng, "dvol": parse_dvol, "btc_dom": parse_btc_dom, "feed": parse_feed, "bybit_ann": parse_bybit_ann,
    "ics": parse_ics, "fomc_html": parse_fomc_html,
}

# ---------- реестр источников ----------
_B = "https://api.bybit.com"
SOURCE_DEFS = [
    {"id": "bybit_ann", "kind": "text", "group": "bybit_ann", "url": _B + "/v5/announcements/index", "params": {"locale": "en-US", "limit": "20"},
     "format": "json", "parser": "bybit_ann", "interval_s": 30, "fields": ["items", "ann"],
     "note": "объявления биржи; эндпоинт по памяти документации (V5 announcements), в клиенте проекта не используется"},
    {"id": "bybit_time", "kind": "struct", "group": "bybit", "url": _B + "/v5/market/time", "params": {}, "format": "json", "parser": "bybit_time",
     "interval_s": 300, "fields": ["server_ts"], "note": "доступность REST и дрейф часов (в 07 было 60 с, укрупнено ради лимита запросов)"},
    {"id": "bybit_tickers", "kind": "struct", "group": "bybit", "url": _B + "/v5/market/tickers", "params": {"category": "linear"}, "format": "json",
     "parser": "bybit_tickers", "interval_s": 300, "fields": ["symbols"], "note": "funding, OI(usd), оборот по всем linear; в кэш попадают только нужные символы"},
    {"id": "oi", "kind": "struct", "group": "bybit", "per": "symbol", "url": _B + "/v5/market/open-interest",
     "params": {"category": "linear", "symbol": "{symbol}USDT", "intervalTime": "1h", "limit": "25"}, "format": "json", "parser": "bybit_oi",
     "interval_s": 300, "fields": ["oi", "oi_chg_24h_pct"], "note": "OI за 24 ч по часовым точкам (эндпоинт по памяти, context.py его не использует)"},
    {"id": "ls", "kind": "struct", "group": "bybit", "per": "symbol", "url": _B + "/v5/market/account-ratio",
     "params": {"category": "linear", "symbol": "{symbol}USDT", "period": "1h", "limit": "1"}, "format": "json", "parser": "bybit_ls",
     "interval_s": 300, "fields": ["ls_ratio"], "note": "account-ratio: подтверждён context.py (_derivs_bybit)"},
    {"id": "fng", "kind": "struct", "group": "altme", "url": "https://api.alternative.me/fng/", "params": {"limit": "2", "format": "json"}, "format": "json",
     "parser": "fng", "interval_s": 3600, "fields": ["v", "cls", "prev"], "note": "подтверждён context.py"},
    {"id": "dvol", "kind": "struct", "group": "deribit", "per": "ccy", "url": "https://www.deribit.com/api/v2/public/get_volatility_index_data",
     "params": {"currency": "{ccy}", "resolution": "3600", "start_timestamp": "{start_ms}", "end_timestamp": "{end_ms}"}, "format": "json", "parser": "dvol",
     "interval_s": 1800, "fields": ["v"], "note": "подтверждён context.py"},
    {"id": "btc_dom", "kind": "struct", "group": "coingecko", "url": "https://api.coingecko.com/api/v3/global", "params": {}, "format": "json",
     "parser": "btc_dom", "interval_s": 1800, "fields": ["v"], "hist_key": "v", "note": "подтверждён context.py (sources.global); chg_24h считается по своей истории"},
    {"id": "rss_coindesk", "kind": "text", "group": "coindesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "params": {}, "format": "rss",
     "parser": "feed", "interval_s": 90, "fields": ["items"]},
    {"id": "rss_cointelegraph", "kind": "text", "group": "cointelegraph", "url": "https://cointelegraph.com/rss", "params": {}, "format": "rss",
     "parser": "feed", "interval_s": 90, "fields": ["items"]},
    {"id": "rss_theblock", "kind": "text", "group": "theblock", "url": "https://www.theblock.co/rss.xml", "params": {}, "format": "rss",
     "parser": "feed", "interval_s": 90, "fields": ["items"]},
    {"id": "rss_decrypt", "kind": "text", "group": "decrypt", "url": "https://decrypt.co/feed", "params": {}, "format": "rss", "parser": "feed",
     "interval_s": 180, "fields": ["items"]},
    {"id": "cal_bls_ics", "kind": "struct", "group": "bls", "url": "https://www.bls.gov/schedule/news_release/bls.ics", "params": {}, "format": "ics",
     "parser": "ics", "interval_s": 86400, "fields": ["events"], "note": "CPI/NFP; сайт может отдавать 403 ботам -> источник degraded, календарь остаётся на кандидатах"},
    {"id": "cal_fomc_html", "kind": "struct", "group": "fed", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "params": {},
     "format": "html", "parser": "fomc_html", "interval_s": 86400, "fields": ["events"], "note": "разбор регулярками, хрупко"},
]
LOCAL_SOURCES = (
    {"id": "calendar", "kind": "local", "note": "config/macro_events.json + импорт + кандидаты config/intel.json (v:false до проверки)"},
    {"id": "us_market", "kind": "local", "note": "расписание NYSE по config/intel.json: us_holidays (локально, без сети)"},
)
ACCEPT = {"json": "application/json", "rss": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
          "ics": "text/calendar, */*;q=0.5", "html": "text/html, */*;q=0.5"}
RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")


def _dom(url):
    m = re.match(r"^https?://([^/:]+)", url)
    return m.group(1).lower() if m else ""


def expand(cfg, symbols=None):
    """-> список конкретных источников (per=symbol/ccy раскрыты) с применёнными переопределениями config/intel.json. verified по умолчанию false."""
    symbols = [s for s in (symbols or []) if RE_SYM.match(s)]
    ccys = [c for c in (cfg.get("dvol_ccys") or ["BTC", "ETH"]) if RE_SYM.match(str(c))]
    over = cfg.get("sources") or {}
    out = []
    for d in SOURCE_DEFS:
        per = d.get("per")
        insts = [None]
        if per == "symbol":
            insts = symbols
        elif per == "ccy":
            insts = ccys
        for x in insts:
            s = dict(d)
            s["params"] = dict(d.get("params") or {})
            if x is not None:
                sid = "%s_%s" % (d["id"], x)
                s["base"], s["id"], s["inst"] = d["id"], sid, x
                s["symbol" if per == "symbol" else "ccy"] = x
            else:
                s["base"] = d["id"]
            o = over.get(s["id"]) or over.get(s["base"]) or {}
            for k in ("url", "interval_s", "timeout_s"):
                if k in o:
                    s[k] = o[k]
            s["enabled"] = bool(o.get("enabled", True))
            s["verified"] = bool(o.get("verified", False))
            s["domain"] = _dom(s["url"])
            s.setdefault("timeout_s", cfg.get("timeout_s", 10))
            out.append(s)
    return out


def request_of(src, now):
    """-> (url, params) с подстановкой {symbol}/{ccy}/{start_ms}/{end_ms}."""
    ctx = {"symbol": src.get("symbol", ""), "ccy": src.get("ccy", ""), "start_ms": int((now - 6 * 3600) * 1000), "end_ms": int(now * 1000)}
    params = {k: (v.format_map(ctx) if isinstance(v, str) else v) for k, v in (src.get("params") or {}).items()}
    return src["url"], params


def parse(src, body, ctx):
    fn = PARSERS.get(src["parser"])
    if fn is None:
        raise ParseError("no_parser")
    data = fn(body, dict(ctx, group=src.get("group")))
    if not isinstance(data, dict):
        raise ParseError("shape")
    return data


def missing_fields(src, data):
    """Список обязательных полей источника, которых нет в разобранных данных (для verify)."""
    miss = []
    for k in src.get("fields") or []:
        v = data.get(k)
        if v is None or v == [] or v == {}:
            miss.append(k)
    return miss
