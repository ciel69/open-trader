"""Структура рынка для досье мозга: `runtime/structure.json` (режим, тренд, волатильность, дневные/недельные границы, ближайшие уровни, оборот к норме).

Зачем: runtime/levels.json исполнитель пишет только при планах/позициях, а EMA/режим в market.json фида нужен прогрев в десятки минут, поэтому мозг на старте дня
видел `regime: unknown` и ни одного уровня. Здесь свечи берутся из ПУБЛИЧНОГО REST Bybit (kline 5м и 1ч, без ключа), поэтому данные есть сразу и не зависят от планов.

Состав символов (по приоритету): BTC, ETH, SOL; символы открытых позиций (exec_snapshot.json); монеты по запросу (runtime/structure_req.json: их пишут сканер при
сигнале, досье мозга и get_brief, когда по монете события структуры нет; запрос живёт REQ_TTL_S); топ-`hot_n` горячих tradable монет из runtime/hot.json. Всего до `max_syms`
(MAX_SYMS=16), монеты, ушедшие из состава, выбрасываются (свечи и запись в файле). Размер файла ограничен (~700 байт на монету).
Запрос на символ: kline 5м (300 свечей = 25 ч) раз в `every_s` (базовые и позиции) или `hot_every_s` (горячие и по запросу), kline 1ч (200 свечей = 8 дней, хватает на неделю
с понедельника UTC) раз в `h1_every_s` / `hot_h1_every_s`. Между запросами к бирже пауза `pause_s` (по умолчанию 2 с): прогрев 11 монет ~1 мин, дальше ~6 запросов в минуту.
Монета по запросу получает свечи вне очереди: цикл просыпается на новый запрос (не чаще раза в 5 с).
Расчёт: `analyze(book, now)` (чистая функция над logic.candles.CandleBook + logic.levels.key_levels). Файл содержит только числа и enum (белый список).

  ./tb bybit read levels [--symbols BTC,ETH] [--write]     разовый расчёт (печать JSON; --write пишет runtime/structure.json)
Сервис: поток внутри платформенного сканера (platform/scansvc.py), включается config/platform.json -> scan.structure.enabled.
"""
import json
import math
import os
import re
import time

from tb import paths
from tb.logic.candles import CandleBook
from tb.logic import levels as L

SCHEMA = "tb.structure/1"
FILE = "structure.json"
BASE = ("BTC", "ETH", "SOL")
MAX_SYMS = 16
HOT_N = 8                     # сколько горячих монет держать в структуре помимо базовых и позиций
HARD_MAX_SYMS = 24            # потолок размера файла независимо от конфига
HOT_EVERY_S = 180.0
HOT_H1_EVERY_S = 1800.0
PAUSE_S = 2.0                 # пауза между запросами свечей к публичному REST (лимит биржи многократно выше)
LIMIT_5M = 300
LIMIT_1H = 200
EXTRA_PAUSE_S = 0.15
REQ_FILE = "structure_req.json"
REQ_SCHEMA = "tb.structure_req/1"
REQ_TTL_S = 1800.0            # запрос на монету живёт 30 минут
MAX_REQ = 6
REQ_MIN_GAP_S = 5.0           # внеочередной проход не чаще
REQ_RETRY_S = 30.0            # повтор по монете, которую не удалось загрузить
STALE_OK_S = 900.0            # при сбое обновления структура по монете строится по прежним свечам, пока им не больше 15 минут
TREND_ATR_MULT = 1.0          # |EMA20-EMA50| / ATR(1ч) от этого значения = тренд (при совпадении знака с доходностью 4ч)
STORM_VX = 2.5                # средний истинный диапазон 5м за последний час к медиане за сутки: шторм
REGIMES = ("trend_up", "trend_down", "range", "storm", "unknown")
TAGS = ("wh", "wl", "pdh", "pdl", "pdc", "dh", "dl", "s1h", "s5m")      # приоритет при слиянии близких уровней
_SYM_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def sig(x, n=5):
    """Число с n значащими цифрами (float) | None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return float("%.*g" % (n, v))


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _tr(prev_c, c):
    return max(c.h - c.l, abs(c.h - prev_c), abs(c.l - prev_c))


def _pct(a, b):
    return None if not b else (a / b - 1.0) * 100.0


def _day_ranges(c1h, day0_ms):
    """Диапазоны (hi-lo)/lo, % полных суток UTC до текущих по свечам 1ч (только сутки, покрытые 24 свечами)."""
    days = {}
    for c in c1h:
        if c.start < day0_ms:
            days.setdefault(c.start // 86400000, []).append(c)
    out = []
    for cs in days.values():
        if len(cs) >= 22:
            lo = min(c.l for c in cs)
            out.append((max(c.h for c in cs) - lo) / lo * 100.0 if lo > 0 else None)
    return [x for x in out if x is not None]


def _levels_around(px, cands, atr_ref, eps_frac=0.0012, per_side=2):
    """cands [(px, tag)] в порядке приоритета -> (res, sup): ближайшие уровни над и под ценой, близкие (< eps_frac) схлопываются, остаётся более приоритетный."""
    kept = []
    for lv, tag in cands:
        if lv is None or lv <= 0:
            continue
        if any(abs(lv - k[0]) / px < eps_frac for k in kept):
            continue
        kept.append((lv, tag))
    res = sorted([k for k in kept if k[0] > px], key=lambda k: k[0] - px)[:per_side]
    sup = sorted([k for k in kept if k[0] < px], key=lambda k: px - k[0])[:per_side]

    def row(k):
        d = abs(k[0] - px)
        return {"px": sig(k[0]), "d_pct": sig(d / px * 100.0, 3), "d_atr": sig(d / atr_ref, 3) if atr_ref else None, "t": k[1]}
    return [row(k) for k in res], [row(k) for k in sup]


def analyze(book, now):
    """-> dict структуры по одному символу (числа и enum) | None, если свечей нет. now в секундах."""
    now_ms = int(now * 1000)
    lv = L.key_levels(book, now_ms)
    px = lv.get("price")
    if not px:
        return None
    c5 = book.candles(5)                                 # закрытые
    c1h = book.candles(60)
    out = {"px": sig(px)}
    atr5, atr1h = lv.get("atr_5m_14"), lv.get("atr_1h_14")
    out["a5_pct"] = sig(atr5 / px * 100.0, 3) if atr5 else None
    out["a1h_pct"] = sig(atr1h / px * 100.0, 3) if atr1h else None
    # доходности
    closes5 = [c.c for c in c5]
    if len(closes5) >= 13:
        out["r1h"] = sig(_pct(px, closes5[-13]), 3)
    closes1h = [c.c for c in c1h]
    if len(closes1h) >= 5:
        out["r4h"] = sig(_pct(closes1h[-1], closes1h[-5]), 3)
    if len(closes1h) >= 25:
        out["r24h"] = sig(_pct(closes1h[-1], closes1h[-25]), 3)
    # волатильность к норме (5м): последний час к суткам; шторм
    vx = None
    if len(c5) >= 60:
        trs = [_tr(p.c, c) for p, c in zip(c5, c5[1:])]
        base = _median(trs[-288:])
        if base and base > 0:
            vx = sum(trs[-12:]) / 12.0 / base
    out["vx"] = sig(vx, 3)
    # режим и тренд по 1ч
    ef, es = _ema(closes1h, 20), _ema(closes1h, 50)
    reg, te = "unknown", None
    if ef is not None and es is not None and atr1h:
        te = (ef - es) / atr1h
        r4 = out.get("r4h")
        if vx is not None and vx >= STORM_VX:
            reg = "storm"
        elif abs(te) >= TREND_ATR_MULT and r4 is not None and (te > 0) == (r4 > 0):
            reg = "trend_up" if te > 0 else "trend_down"
        else:
            reg = "range"
    elif vx is not None and vx >= STORM_VX:
        reg = "storm"
    out["reg"] = reg
    out["te"] = sig(te, 3)
    # границы суток и недели
    d, pd, w = lv.get("day"), lv.get("prev_day"), lv.get("week")
    if d:
        out["day"] = [sig(d["hi"]), sig(d["lo"])]
        if d["hi"] > d["lo"]:
            out["dpos"] = sig((px - d["lo"]) / (d["hi"] - d["lo"]), 2)
        base_rng = _median(_day_ranges(c1h, (now_ms // 86400000) * 86400000)[-7:])
        if base_rng and d["lo"] > 0:
            out["dx"] = sig((d["hi"] - d["lo"]) / d["lo"] * 100.0 / base_rng, 3)
    if pd:
        out["pd"] = [sig(pd["hi"]), sig(pd["lo"]), sig(pd["close"])]
    if w:
        out["wk"] = [sig(w["hi"]), sig(w["lo"])]
    # оборот к норме: последний час (12 свечей 5м) к медиане 5м за сутки; оборот суток
    if len(c5) >= 60:
        med = _median([c.turn for c in c5[-288:-12]])
        if med and med > 0:
            out["tx"] = sig(sum(c.turn for c in c5[-12:]) / 12.0 / med, 3)
    if len(c1h) >= 24:
        out["t24_musd"] = sig(sum(c.turn for c in c1h[-24:]) / 1e6, 3)
    # уровни: границы периодов важнее свингов; расстояния в % и в ATR(1ч)
    cands = []
    if w:
        cands += [(w["hi"], "wh"), (w["lo"], "wl")]
    if pd:
        cands += [(pd["hi"], "pdh"), (pd["lo"], "pdl"), (pd["close"], "pdc")]
    if d:
        cands += [(d["hi"], "dh"), (d["lo"], "dl")]
    cands += [(x, "s1h") for x in (book.swing_levels(60, 3, 3, 8) or [])[::-1]]
    cands += [(x, "s5m") for x in (book.swing_levels(5, 3, 3, 8) or [])[::-1]]
    res, sup = _levels_around(px, cands, atr1h or atr5)
    if res:
        out["res"] = res
    if sup:
        out["sup"] = sup
    out["n5"], out["n1h"] = len(c5), len(c1h)
    return {k: v for k, v in out.items() if v is not None}


# ------------------------------------------------------------------ загрузка свечей
def _rows(c, symbol, interval, limit):
    """REST kline -> [(start_ms,o,h,l,c,vol,turn)] по возрастанию (формат CandleBook.seed)."""
    from tb.marketdata import klines
    res = klines.call_retry(c, {"category": "linear", "symbol": symbol, "interval": str(interval), "limit": str(limit)}, tries=2)
    rows, _bad = klines.parse_kline(res)
    return sorted([(r[0] * 1000,) + tuple(float(x) for x in r[1:7]) for r in rows])


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pos_syms(root):
    snap = read_json(paths.exec_snapshot_file(root=root))
    return [p.get("sym") for p in (snap.get("positions") if isinstance(snap, dict) and isinstance(snap.get("positions"), list) else []) if isinstance(p, dict)]


def read_requests(root, now=None):
    """-> [SYM] живых запросов на структуру (новые первыми), без просроченных."""
    now = time.time() if now is None else now
    d = read_json(paths.runtime(REQ_FILE, root=root))
    req = d.get("req") if isinstance(d, dict) and isinstance(d.get("req"), dict) else {}
    live = [(float(t), s) for s, t in req.items() if isinstance(s, str) and _SYM_RE.match(s) and isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= now - t < REQ_TTL_S]
    return [s for _t, s in sorted(live, reverse=True)][:MAX_REQ]


def request(root, syms, now=None):
    """Просит сервис структуры подтянуть свечи по монетам (runtime/structure_req.json, потокобезопасно, лишнего не пишет). -> число новых/обновлённых запросов; ошибки гасятся."""
    now = time.time() if now is None else now
    want = [s for s in dict.fromkeys(syms or ()) if isinstance(s, str) and _SYM_RE.match(s) and s not in BASE]
    if not want:
        return 0
    p = paths.runtime(REQ_FILE, root=root)
    try:
        from tb.core.filelock import FileLock, atomic_write_json
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with FileLock(p + ".lock", wait=2.0):
            d = read_json(p)
            req = d.get("req") if isinstance(d, dict) and isinstance(d.get("req"), dict) else {}
            req = {s: t for s, t in req.items() if isinstance(s, str) and isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= now - t < REQ_TTL_S}
            n = 0
            for s in want:
                if s not in req or now - req[s] > REQ_TTL_S / 2:
                    req[s] = round(now, 3)
                    n += 1
            if n:
                keep = sorted(req, key=lambda k: req[k], reverse=True)[:MAX_REQ]
                atomic_write_json(p, {"schema": REQ_SCHEMA, "req": {k: req[k] for k in keep}}, indent=None)
            return n
    except Exception:
        return 0


def wanted_symbols(root, extra=(), n=MAX_SYMS, hot_n=HOT_N, now=None):
    """-> [(SYM, SYMUSDT)]: BTC, ETH, SOL, extra, символы позиций, монеты по запросу, затем до hot_n tradable из hot.json; без дублей, до n."""
    out = []
    n = max(1, min(int(n), HARD_MAX_SYMS))

    def add(sym, symbol=None):
        if isinstance(sym, str) and _SYM_RE.match(sym) and sym not in [x[0] for x in out] and len(out) < n:
            out.append((sym, symbol if isinstance(symbol, str) and _SYM_RE.match(symbol) else sym + "USDT"))
            return True
        return False
    for s in BASE:
        add(s)
    for s in extra:
        add(s)
    for s in _pos_syms(root):
        add(s)
    for s in read_requests(root, now):
        add(s)
    hot = read_json(paths.runtime("hot.json", root=root))
    k = 0
    for r in (hot.get("top") if isinstance(hot, dict) and isinstance(hot.get("top"), list) else []):
        if k >= hot_n:
            break
        if isinstance(r, dict) and r.get("tradable") is not False and add(r.get("sym"), r.get("symbol")):
            k += 1
    return out


class Collector(object):
    """Держит CandleBook по символам, подкачивает свечи по расписанию и пишет runtime/structure.json."""

    def __init__(self, client, root=None, every_s=60.0, h1_every_s=600.0, max_syms=MAX_SYMS, clock=time.time, sleep=time.sleep,
                 hot_n=HOT_N, hot_every_s=HOT_EVERY_S, hot_h1_every_s=HOT_H1_EVERY_S, pause_s=PAUSE_S):
        self.c, self.root, self.clock, self.sleep = client, root, clock, sleep
        self.every_s, self.h1_every_s, self.max_syms = float(every_s), float(h1_every_s), min(int(max_syms), HARD_MAX_SYMS)
        self.hot_n, self.pause_s = int(hot_n), float(pause_s)
        self.hot_every_s, self.hot_h1_every_s = max(self.every_s, float(hot_every_s)), max(self.h1_every_s, float(hot_h1_every_s))
        self.books, self.t5, self.t1h, self.errors = {}, {}, {}, 0
        self.tried = {}
        self.last = None
        self.last_step = -1e18

    def _ttls(self, sym, fast):
        return (self.every_s, self.h1_every_s) if (sym in BASE or sym in fast) else (self.hot_every_s, self.hot_h1_every_s)

    def pending_requests(self, now=None):
        """Запрошенные монеты, которых ещё нет в файле структуры (и которые не пробовали менее REQ_RETRY_S назад)."""
        now = self.clock() if now is None else now
        have = ((self.last or {}).get("symbols") or {})
        return [s for s in read_requests(self.root, now) if s not in have and now - self.tried.get(s, -1e18) >= REQ_RETRY_S]

    def step(self, symbols=None, write=True):
        """Один проход: свежие свечи -> структура всех символов. Ошибка по символу не валит остальные. -> dict файла."""
        now = self.clock()
        syms = symbols or wanted_symbols(self.root, n=self.max_syms, hot_n=self.hot_n, now=now)
        fast = set(x for x in _pos_syms(self.root) if isinstance(x, str))
        # новые монеты (ещё без свечей) вне очереди: сигнал уже пришёл, структура нужна сейчас
        syms = sorted(syms, key=lambda x: 0 if x[0] not in self.t5 else 1)
        data, errs, why = {}, [], None
        for sym, symbol in syms:
            book = self.books.setdefault(sym, CandleBook())
            ttl5, ttl1 = self._ttls(sym, fast)
            self.tried[sym] = now
            try:
                if now - self.t1h.get(sym, -1e18) >= ttl1:
                    book.seed(60, _rows(self.c, symbol, 60, LIMIT_1H), now=now)
                    self.t1h[sym] = now
                    self.sleep(self.pause_s)
                if now - self.t5.get(sym, -1e18) >= ttl5:
                    book.seed(5, _rows(self.c, symbol, 5, LIMIT_5M), now=now)
                    self.t5[sym] = now
                    self.sleep(self.pause_s)
            except Exception as e:                       # сеть/лимит/формат/геоблок: цикл живёт, причина попадает в файл (err_why)
                errs.append(sym)
                self.errors += 1
                why = why or ("%s: %s" % (type(e).__name__, e))[:80]
                if now - self.t5.get(sym, -1e18) > STALE_OK_S:        # свечи старше STALE_OK_S: структуру по монете не отдаём
                    continue
            try:
                row = analyze(book, now)
            except Exception:
                row = None
            if row:
                row["ts"] = int(self.t5.get(sym, now))
                data[sym] = row
        keep = set(x[0] for x in syms)
        for d in (self.books, self.t5, self.t1h, self.tried):          # монеты, ушедшие из состава (вышли из hot, запрос истёк), выбрасываются
            for k in [k for k in d if k not in keep]:
                del d[k]
        doc = {"schema": SCHEMA, "ts_epoch": round(now, 3), "symbols": data}
        if errs:
            doc["err"] = errs
            doc["err_why"] = why
        self.last = doc
        self.last_step = now
        if write and data:
            from tb.core.filelock import atomic_write_json
            p = paths.runtime(FILE, root=self.root)
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                atomic_write_json(p, doc, indent=None)
            except OSError:
                pass
        return doc


def run_loop(client, root=None, cfg=None, clock=time.time, sleep=time.sleep, stop=lambda: False):
    """Цикл потока сервиса: шаг раз в every_s или раньше, если пришёл запрос на новую монету; любые ошибки гасятся (структура вспомогательная)."""
    cfg = cfg or {}
    col = Collector(client, root, cfg.get("every_s", 60.0), cfg.get("h1_every_s", 600.0), cfg.get("max_syms", MAX_SYMS), clock, sleep,
                    hot_n=cfg.get("hot_n", HOT_N), hot_every_s=cfg.get("hot_every_s", HOT_EVERY_S), hot_h1_every_s=cfg.get("hot_h1_every_s", HOT_H1_EVERY_S),
                    pause_s=cfg.get("pause_s", PAUSE_S))
    while not stop():
        try:
            col.step()
        except Exception:
            pass
        end = clock() + col.every_s
        while clock() < end and not stop():
            sleep(1.0)
            try:
                if clock() - col.last_step >= REQ_MIN_GAP_S and col.pending_requests():
                    break
            except Exception:
                pass
    return 0


def add_args(ap):
    ap.add_argument("--symbols", default="", help="BTC,ETH,... (без USDT); по умолчанию BTC, ETH, SOL + горячие + позиции")
    ap.add_argument("--write", action="store_true", help="записать runtime/structure.json (по умолчанию только печать)")


def main_levels(c, a, out=None):
    import sys
    out = out or sys.stdout
    syms = [(s.strip().upper(), s.strip().upper() + "USDT") for s in a.symbols.split(",") if _SYM_RE.match(s.strip().upper())] or None
    doc = Collector(c, paths.root()).step(syms, write=bool(a.write))
    out.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return 0 if doc["symbols"] else 1
