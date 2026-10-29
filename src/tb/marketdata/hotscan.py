"""Сканер «горячих монет» по ВСЕМУ рынку USDT-перпов Bybit. Только чтение (публичные эндпоинты, ключ не нужен), ничего не торгует.

Зачем: фид и volwatch смотрят 5 монет, а большая часть волатильных монет пропускается. Сканер раз в interval секунд берёт один запрос
/v5/market/tickers?category=linear (весь список) и хранит в памяти скользящее окно цен, open interest и оборота по каждому символу.
Метрики: изменение за 24ч / 15м / 5м, диапазон (макс-мин)/цена за 5 и 15 минут, оборот 24ч и его отношение к медиане по рынку,
всплеск оборота за 5м (прирост turnover24h за 5 минут к средней норме 5 минут: turnover24h/288), прирост OI за 15м, funding, спред (ask1/bid1).
Скоринг «горячести» (сумма четырёх слагаемых, без подгонки; каждое слагаемое видно в reasons):
  t5   = min(range_5m / range_pct, 3)            диапазон 5м относительно порога события (по умолчанию 0.8%)
  t15  = min(range_15m / ref15_pct, 3)           диапазон 15м относительно 1.5%
  tsrg = min(max(surge_5m - 1, 0), 3)            всплеск оборота: 1 = норма, 4 и выше даёт максимум
  t24  = min(|chg_24h| / 10, 1)                  движение за сутки (единственное слагаемое, доступное в первые минуты после старта)
Окна 5м и 15м считаются только когда накоплено >= 75% окна (иначе поле None и слагаемое 0). Диапазон считается по замерам lastPrice раз в interval,
поэтому это нижняя оценка настоящего диапазона. Прирост turnover24h за 5 минут приближённый: оборот скользящий, старые сделки выпадают из 24-часового окна.
Фильтры под малый счёт: оборот 24ч >= min_turnover, спред <= max_spread_bps, статус Trading, минимальный лот (minQty*цена или minNotionalValue) <= max_lot USDT.
Не прошёл рыночные фильтры (оборот/спред/статус) -> монеты нет нигде. Прошёл их, но лот слишком большой (BTC и т. п.) -> в hot.json в списке blocked с tradable=false.
Группа корреляции из config/risk_profile.json (BTC/ETH/SOL) помечается corr_group=true.

Выход: runtime/hot.json (атомарно, топ-N только tradable + blocked, с ценой, tick/qty-шагами, minOrderQty); в режиме --watch в stdout ТОЛЬКО события,
одна JSON-строка на событие (для Monitor; каждая строка будит агента).
На каждом скане трекер пути (core/tradepath.py) обновляет runtime/trade_paths.json: min/max цены открытых live-сделок с момента входа (MAE/MFE для journal close).
Рядом на каждом скане пишется runtime/prices.json (атомарно, компактно): lastPrice и markPrice ВСЕХ тикеров скана без суффикса USDT, для цен на дашборде (без новых запросов). Лог событий: runtime/hot_events.jsonl.
События (type): hot_enter (монета впервые вошла в топ, score >= enter_score), range_5m (диапазон 5м >= range_pct), turnover_surge (всплеск оборота >= surge_min при диапазоне 5м >= range_pct/2),
служебные scan_start / scan_end / scan_error / scan_recovered. Антиспам: cooldown на символ (общий для всех типов), не больше max_per_scan событий за скан,
пауза global_gap (60 с) между событиями, не больше max_per_hour (12) в час; отброшенное событие не штампуется и сработает позже, если условие сохранится. Первый скан только запоминает топ (hot_enter не шлёт).
  ./tb bybit read scan [--watch] [--top 10] [--min-turnover 5000000] [--max-spread-bps 8] [--range-pct 0.8] [--cooldown 1200] [--interval 30] [--max-seconds 0]
  ./tb hotscan ...   то же без обёртки (из песочницы REST закрыт 403: для живых данных используй ./tb bybit read scan)
Шум для агента: в stdout (будит агента) идут только события со score >= --wake-score (по умолчанию 4.0; 0 = все, как раньше). Остальные («тихие», "wake":false)
пишутся в runtime/hot_events.jsonl и в статистику, но агента не будят и лимиты будильника (max_per_hour, global_gap) не тратят. Статистика: marketdata/outcomes.py
(форвард-доходность +15/+60 мин [--outcome-min 15,60,240] в state/hot_outcomes.jsonl, также исходы идей агента), смотреть: ./tb ideas stats и дашборд.
Идеи агента: тот же скан сверяет цены с уровнями активных идей и печатает idea_near / idea_level / idea_invalidated / idea_tp (будят агента в обход --wake-score и max-per-hour,
свой антиспам; в статистику пропущенных не попадают), по ttl молча ставит expired; зона --idea-zone-pct (0.3%). Подробности: marketdata/ideawatch.py.
Скан без --watch: один проход, печатает топ (окон 5м/15м ещё нет, ранжирование только по 24ч). Для живой картины запускай --watch.
"""
import argparse
import json
import os
import statistics
import sys
import time
from collections import deque

from tb import fsutil, paths
from tb.core import ideas, journal, tradepath
from tb.marketdata import ideawatch, outcomes

DEFAULTS = {"top": 10, "min_turnover": 5e6, "max_spread_bps": 8.0, "max_lot": 15.0, "range_pct": 0.8, "ref15_pct": 1.5,
            "surge_min": 4.0, "enter_score": 2.0, "cooldown": 1200.0, "interval": 30.0, "max_per_scan": 2, "global_gap": 60.0, "max_per_hour": 12,
            "wake_score": 4.0, "max_quiet_per_scan": 5, "outcome_min": "15,60", "blocked_n": 5, "keep_s": 1260.0, "cover": 0.75, "ins_refresh_s": 3600.0, "max_seconds": 0.0,
            "idea_zone_pct": ideawatch.ZONE_PCT, "idea_cooldown": ideawatch.COOLDOWN_S}
SUFFIX = "USDT"


# ---------- разбор ответов (чистые функции) ----------

def num(x):
    """float или None (пусто, нет поля, мусор)."""
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_tickers(result):
    """result из /v5/market/tickers -> {symbol: {...числа}} только USDT-символы (без dated-фьючерсов вида BTC-27MAR26 и USDC)."""
    out = {}
    for it in (result or {}).get("list") or []:
        sym = it.get("symbol") or ""
        if not sym.endswith(SUFFIX) or "-" in sym:
            continue
        price = num(it.get("lastPrice"))
        if not price or price <= 0:
            continue
        fr = num(it.get("fundingRate"))
        out[sym] = {"price": price, "mark": num(it.get("markPrice")), "bid": num(it.get("bid1Price")), "ask": num(it.get("ask1Price")),
                    "turn24": num(it.get("turnover24h")), "chg24": (num(it.get("price24hPcnt")) or 0.0) * 100.0,
                    "oi": num(it.get("openInterest")), "funding_pct": fr * 100.0 if fr is not None else None}
    return out


def parse_instruments(items):
    """list из /v5/market/instruments-info -> {symbol: {...}} только USDT-перпы."""
    out = {}
    for it in items or []:
        sym = it.get("symbol") or ""
        if not sym.endswith(SUFFIX) or it.get("quoteCoin", "USDT") != "USDT":
            continue
        if it.get("contractType") not in (None, "LinearPerpetual"):
            continue
        lot, pf, lv = it.get("lotSizeFilter") or {}, it.get("priceFilter") or {}, it.get("leverageFilter") or {}
        out[sym] = {"status": it.get("status"), "min_qty": num(lot.get("minOrderQty")), "qty_step": lot.get("qtyStep"),
                    "min_notional": num(lot.get("minNotionalValue")) or 5.0, "tick": pf.get("tickSize"),
                    "max_leverage": num(lv.get("maxLeverage"))}
    return out


def spread_bps(bid, ask):
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return 10000.0 * (ask - bid) / ((ask + bid) / 2.0)


# ---------- окна ----------

def ref_sample(buf, ts, w, cover):
    """Замер примерно w секунд назад: последний не позже ts-w; если истории меньше, но покрыто >= cover*w, то самый старый; иначе None."""
    if not buf:
        return None
    best = None
    for s in buf:
        if s[0] <= ts - w:
            best = s
        else:
            break
    if best is not None:
        return best if ts - best[0] <= 1.5 * w else None      # слишком старый замер (был перерыв в сканах) не годится
    return buf[0] if ts - buf[0][0] >= cover * w else None


def window_range(buf, ts, w, cover):
    """(макс-мин)/последняя цена, % за последние w секунд по замерам; None, если окно не накоплено."""
    if not buf or ts - buf[0][0] < cover * w:
        return None
    ps = [s[1] for s in buf if s[0] >= ts - w]
    if len(ps) < 2:
        return None
    return 100.0 * (max(ps) - min(ps)) / ps[-1]


def window_ret(buf, ts, w, cover):
    r = ref_sample(buf, ts, w, cover)
    if r is None or r[1] <= 0:
        return None
    return 100.0 * (buf[-1][1] / r[1] - 1.0)


def turnover_surge(buf, ts, turn24, w, cover):
    """Прирост turnover24h за w секунд к норме за такое же время (turnover24h * w / 86400). None, если окна нет."""
    r = ref_sample(buf, ts, w, cover)
    if r is None or r[3] is None or not turn24 or turn24 <= 0 or buf[-1][3] is None:
        return None
    span = max(ts - r[0], 1.0)
    delta = max(buf[-1][3] - r[3], 0.0)
    return delta / (turn24 * span / 86400.0)


def score_row(m, p):
    """(score, reasons) по метрикам m; слагаемое None -> 0."""
    reasons, total = [], 0.0
    r5, r15, sg, ch = m.get("range_5m_pct"), m.get("range_15m_pct"), m.get("turn_surge_5m"), m.get("chg_24h_pct")
    if r5 is not None:
        t = min(r5 / p["range_pct"], 3.0)
        total += t
        if t >= 1.0:
            reasons.append("диапазон 5м %.2f%%" % r5)
    if r15 is not None:
        t = min(r15 / p["ref15_pct"], 3.0)
        total += t
        if t >= 1.0:
            reasons.append("диапазон 15м %.2f%%" % r15)
    if sg is not None:
        t = min(max(sg - 1.0, 0.0), 3.0)
        total += t
        if sg >= 2.0:
            reasons.append("оборот 5м x%.1f нормы" % sg)
    if ch is not None:
        t = min(abs(ch) / 10.0, 1.0)
        total += t
        if t >= 0.5:
            reasons.append("24ч %+.1f%%" % ch)
    return round(total, 3), reasons


def r_(x, n=3):
    return None if x is None else round(x, n)


class HotScanner:
    """Держит историю замеров и по каждому тикер-срезу строит строки метрик."""

    def __init__(self, p, corr=()):
        self.p = dict(DEFAULTS, **p)
        self.corr = set(corr)
        self.hist = {}                      # symbol -> deque((ts, price, oi, turn24))

    def step(self, tickers, instruments, now):
        """tickers: parse_tickers; instruments: parse_instruments (может быть пуст). -> (top, blocked, stats)."""
        p = self.p
        turns = [t["turn24"] for t in tickers.values() if t["turn24"]]
        med = statistics.median(turns) if turns else None
        for sym, t in tickers.items():
            h = self.hist.setdefault(sym, deque())
            h.append((now, t["price"], t["oi"], t["turn24"]))
            while h and now - h[0][0] > p["keep_s"]:
                h.popleft()
        for sym in [s for s in self.hist if s not in tickers]:       # делистинг/пропажа из выдачи
            if now - self.hist[sym][-1][0] > p["keep_s"]:
                del self.hist[sym]
        eligible, tradable_rows, blocked = 0, [], []
        for sym, t in tickers.items():
            ins = instruments.get(sym)
            sp = spread_bps(t["bid"], t["ask"])
            if (t["turn24"] or 0) < p["min_turnover"] or sp is None or sp > p["max_spread_bps"]:
                continue
            if ins is not None and ins["status"] != "Trading":
                continue
            eligible += 1
            h = self.hist[sym]
            cv = p["cover"]
            m = {"range_5m_pct": window_range(h, now, 300, cv), "range_15m_pct": window_range(h, now, 900, cv),
                 "ret_5m_pct": window_ret(h, now, 300, cv), "ret_15m_pct": window_ret(h, now, 900, cv),
                 "turn_surge_5m": turnover_surge(h, now, t["turn24"], 300, cv), "chg_24h_pct": t["chg24"]}
            r15 = ref_sample(h, now, 900, cv)
            oi_chg = 100.0 * (t["oi"] / r15[2] - 1.0) if r15 and r15[2] and t["oi"] else None
            score, reasons = score_row(m, p)
            skip, lot = [], None
            if ins is None:
                skip.append("нет данных instruments-info")
            else:
                if not ins["min_qty"]:
                    skip.append("нет minOrderQty")
                else:
                    lot = max(ins["min_qty"] * t["price"], ins["min_notional"])
                    if lot > p["max_lot"]:
                        skip.append("минимальный лот %.1f USDT > %.1f" % (lot, p["max_lot"]))
            base = sym[:-len(SUFFIX)]
            row = {"sym": base, "symbol": sym, "price": t["price"], "score": score, "reasons": reasons, "tradable": not skip,
                   "corr_group": base in self.corr, "chg_24h_pct": r_(t["chg24"], 2), "ret_5m_pct": r_(m["ret_5m_pct"]),
                   "ret_15m_pct": r_(m["ret_15m_pct"]), "range_5m_pct": r_(m["range_5m_pct"]), "range_15m_pct": r_(m["range_15m_pct"]),
                   "turn_24h_musd": round(t["turn24"] / 1e6, 1), "turn_x_median": r_(t["turn24"] / med, 2) if med else None,
                   "turn_surge_5m": r_(m["turn_surge_5m"], 2), "oi_chg_15m_pct": r_(oi_chg, 2), "funding_pct": r_(t["funding_pct"], 4),
                   "spread_bps": round(sp, 2), "lot_usdt": r_(lot, 2)}
            if ins is not None:
                row.update({"min_qty": ins["min_qty"], "qty_step": ins["qty_step"], "tick": ins["tick"],
                            "min_notional": ins["min_notional"], "max_leverage": ins["max_leverage"]})
            if skip:
                row["skip"] = skip
                blocked.append(row)
            else:
                tradable_rows.append(row)
        key = lambda r: (-r["score"], -r["turn_24h_musd"])
        tradable_rows.sort(key=key)
        blocked.sort(key=key)
        for i, r in enumerate(tradable_rows):
            r["rank"] = i + 1
        top = tradable_rows[:p["top"]]
        stats = {"symbols": len(tickers), "eligible": eligible, "tradable": len(tradable_rows), "blocked": len(blocked),
                 "median_turn_musd": round(med / 1e6, 1) if med else None}
        return top, tradable_rows, blocked[:p["blocked_n"]], stats


class EventGate:
    """Решает, какие события выпускать: cooldown на символ, лимит на скан, пауза между событиями."""

    def __init__(self, p):
        self.p = dict(DEFAULTS, **p)
        self.last = {}                      # sym -> ts последнего громкого события (будит агента)
        self.last_q = {}                    # sym -> ts последнего любого события (громкого или тихого)
        self.last_any = -1e18
        self.recent = []                    # ts выпущенных событий за последний час
        self.prev_top = None                # None до первого скана

    def pick(self, top, tradable_rows, now):
        p = self.p
        cur = {r["sym"] for r in top}
        cands = []
        for r in tradable_rows:
            kind = None
            if r["range_5m_pct"] is not None and r["range_5m_pct"] >= p["range_pct"]:
                kind = "range_5m"
            elif (r["turn_surge_5m"] is not None and r["turn_surge_5m"] >= p["surge_min"] and r["range_5m_pct"] is not None
                  and r["range_5m_pct"] >= p["range_pct"] / 2.0):         # всплеск оборота без движения цены (стейбл-токены, золото) не событие
                kind = "turnover_surge"
            elif self.prev_top is not None and r["sym"] in cur and r["sym"] not in self.prev_top and r["score"] >= p["enter_score"]:
                kind = "hot_enter"
            if kind:
                cands.append((r["score"], kind, r))
        self.prev_top = cur
        cands.sort(key=lambda c: -c[0])
        out, loud_n, quiet_n = [], 0, 0
        self.recent = [t for t in self.recent if now - t < 3600.0]
        for _, kind, r in cands:
            if r["score"] >= p["wake_score"]:         # громкое: попадёт в stdout и разбудит агента, лимиты как раньше
                if loud_n >= p["max_per_scan"] or len(self.recent) >= p["max_per_hour"]:
                    continue
                if now - self.last.get(r["sym"], -1e18) < p["cooldown"] or now - self.last_any < p["global_gap"]:
                    continue
                self.last[r["sym"]] = self.last_q[r["sym"]] = self.last_any = now
                self.recent.append(now)
                loud_n += 1
                ev = make_event(kind, r, now, True)
            else:                                      # тихое: только лог и статистика, лимитов будильника не тратит
                if quiet_n >= p["max_quiet_per_scan"] or now - self.last_q.get(r["sym"], -1e18) < p["cooldown"]:
                    continue
                self.last_q[r["sym"]] = now
                quiet_n += 1
                ev = make_event(kind, r, now, False)
            out.append(ev)
        return out


def make_event(kind, r, now=0.0, wake=True):
    ret = r.get("ret_5m_pct")
    ev = {"id": "%s-%s" % (time.strftime("%d%H%M%S", time.gmtime(now)), r["sym"]), "wake": wake, "type": kind, "sym": r["sym"], "price": r["price"], "score": r["score"], "reasons": r["reasons"],
          "range_5m_pct": r["range_5m_pct"], "ret_5m_pct": ret, "direction": None if not ret else ("up" if ret > 0 else "down"),
          "chg_24h_pct": r["chg_24h_pct"], "turn_24h_musd": r["turn_24h_musd"], "turn_surge_5m": r["turn_surge_5m"],
          "spread_bps": r["spread_bps"], "funding_pct": r["funding_pct"], "corr_group": r["corr_group"], "tradable": r["tradable"],
          "note": "информационно: не сигнал входа; проверь guard и тезис"}
    return ev


# ---------- ввод-вывод ----------

def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def corr_symbols():
    try:
        with open(paths.config("risk_profile.json"), encoding="utf-8") as f:
            groups = json.load(f).get("correlation_groups") or []
        return {s for g in groups for s in g}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def write_hot(path, payload):
    """Атомарная запись: во временный файл рядом и os.replace, читатель не увидит половину файла."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def build_prices(tickers, env, now):
    """Цены ВСЕХ тикеров скана для дашборда: {"prices": {МОНЕТА без USDT: lastPrice}, "marks": {МОНЕТА: markPrice}}. Без новых запросов."""
    px, mk = {}, {}
    for sym, t in tickers.items():
        base = sym[:-len(SUFFIX)] if sym.endswith(SUFFIX) else sym
        px[base] = t["price"]
        if t.get("mark"):
            mk[base] = t["mark"]
    return {"ts": iso(now), "ts_epoch": now, "env": env, "n": len(px), "prices": px, "marks": mk}


def write_prices(path, payload):
    """Атомарно и компактно (без отступов): runtime/prices.json читает дашборд. Replace с повторами для Windows."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    fsutil.replace(tmp, path)


def fetch_tickers(c):
    return parse_tickers(c.call("GET", "/v5/market/tickers", {"category": "linear"}, auth=False))


def fetch_instruments(c):
    items, cursor = [], None
    for _ in range(6):                      # 1000 на страницу, USDT-перпов около 550
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        r = c.call("GET", "/v5/market/instruments-info", params, auth=False)
        items += r.get("list") or []
        cursor = r.get("nextPageCursor")
        if not cursor:
            break
    return parse_instruments(items)


class Emitter:
    def __init__(self, out, log_path, env):
        self.out, self.log_path, self.env = out, log_path, env

    def __call__(self, ev, now):
        ev = dict(ev, ts=iso(now), env=self.env)
        line = json.dumps(ev, ensure_ascii=False)
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if ev.get("wake", True):                       # тихие события (wake=false) только в лог и статистику, агента не будят
            self.out.write(line + "\n")
            self.out.flush()


def build_payload(p, env, now, top, blocked, stats, warm):
    return {"ts": iso(now), "ts_epoch": now, "env": env, "warm": warm,
            "params": {k: p[k] for k in ("top", "min_turnover", "max_spread_bps", "max_lot", "range_pct", "interval")},
            "stats": stats, "top": top, "blocked": blocked}


def run(c, p, watch, out=sys.stdout, hot_path=None, log_path=None, clock=time.time, sleep=time.sleep, state_dir=None, prices_path=None):
    """Основной цикл. Возвращает код выхода. Ошибки сети не валят демон: одно событие scan_error, потом scan_recovered."""
    p = dict(DEFAULTS, **p)
    prices_path = prices_path or (os.path.join(os.path.dirname(hot_path), "prices.json") if hot_path else paths.runtime("prices.json"))
    hot_path = hot_path or paths.runtime("hot.json")
    emit = Emitter(out, log_path or paths.runtime("hot_events.jsonl"), c.env)
    sc, gate = HotScanner(p, corr_symbols()), EventGate(p)
    # состояние статистики: рядом с лог-файлом, если путь задан явно (тесты), иначе state/ и runtime/ проекта
    sd = state_dir or (os.path.dirname(log_path) if log_path else None)
    hours = [int(x) for x in str(p["outcome_min"]).split(",") if x.strip().isdigit()]
    tracker = outcomes.OutcomeTracker(os.path.join(sd, "hot_outcomes.jsonl") if sd else ideas.outcomes_path(),
                                      os.path.join(sd, "hot_pending.json") if sd else ideas.pending_path(),
                                      os.path.join(sd, "ideas.jsonl") if sd else ideas.ideas_path(), hours, p["interval"],
                                      ideas.costs()["taker_rt_pct"], c.env) if watch else None
    watcher = ideawatch.IdeaWatcher(os.path.join(sd, "ideas.jsonl") if sd else ideas.ideas_path(),
                                    os.path.join(sd, "idea_watch.json") if sd else paths.runtime("idea_watch.json"),
                                    p["idea_zone_pct"], p["idea_cooldown"]) if watch else None
    # путь цены открытых сделок (MAE/MFE): бесплатно, из тех же тикеров; журнал и состояние рядом с остальными путями (в тестах во временной папке)
    ptracker = tradepath.PathTracker(os.path.join(sd, "trades.jsonl") if sd else journal.PATH,
                                     os.path.join(os.path.dirname(prices_path), tradepath.STATE_NAME),
                                     mode={"mainnet": "live", "testnet": "testnet"}.get(c.env, "live"))
    t0, ins, ins_ts, failing, n = clock(), {}, -1e18, False, 0
    while True:
        now = clock()
        try:
            if now - ins_ts >= p["ins_refresh_s"]:
                try:
                    ins = fetch_instruments(c)
                    ins_ts = now
                except Exception:
                    ins_ts = now - p["ins_refresh_s"] + 60.0          # повтор через минуту; без instruments монеты не tradable
            tickers = fetch_tickers(c)
            top, trad, blocked, stats = sc.step(tickers, ins, now)
            warm = bool(top) and all(r["range_15m_pct"] is not None for r in top[:3])
            write_hot(hot_path, build_payload(p, c.env, now, top, blocked, stats, warm))
            try:                                  # цены всех монет для дашборда; сбой записи не должен ронять сканер
                write_prices(prices_path, build_prices(tickers, c.env, now))
            except OSError:
                pass
            try:
                ptracker.update(tickers, now)
            except Exception:                     # трекер пути вспомогательный: сбой не должен ронять сканер
                pass
            if failing:
                failing = False
                if watch:
                    emit({"type": "scan_recovered"}, now)
            if not watch:
                out.write(json.dumps(build_payload(p, c.env, now, top, blocked, stats, warm), ensure_ascii=False) + "\n")
                return 0
            if n == 0:
                emit({"type": "scan_start", "stats": stats, "top": [r["sym"] for r in top], "range_pct": p["range_pct"],
                      "min_turnover": p["min_turnover"], "interval": p["interval"], "cooldown": p["cooldown"]}, now)
            for ev in gate.pick(top, trad, now):
                emit(ev, now)
                tracker.add_event(ev, now)
            try:                                  # будильник по идеям агента: свой антиспам, в трекер исходов не идёт
                for ev in watcher.check(tickers, now):
                    emit(ev, now)
            except Exception:
                pass
            try:
                tracker.update(tickers, now)
            except Exception:                    # статистика не должна ронять сканер
                pass
            n += 1
        except Exception as e:
            if not watch:
                out.write(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False) + "\n")
                return 1
            if not failing:
                failing = True
                emit({"type": "scan_error", "error": ("%s: %s" % (type(e).__name__, e))[:160]}, now)
        if p["max_seconds"] and clock() - t0 + p["interval"] > p["max_seconds"]:
            emit({"type": "scan_end", "scans": n, "elapsed_s": round(clock() - t0)}, clock())
            return 0
        sleep(p["interval"])


def add_args(ap):
    ap.add_argument("--watch", action="store_true", help="демон: в stdout только события (одна JSON-строка на событие)")
    ap.add_argument("--top", type=int, default=DEFAULTS["top"])
    ap.add_argument("--min-turnover", type=float, default=DEFAULTS["min_turnover"], help="минимальный оборот 24ч, USDT")
    ap.add_argument("--max-spread-bps", type=float, default=DEFAULTS["max_spread_bps"])
    ap.add_argument("--max-lot", type=float, default=DEFAULTS["max_lot"], help="максимальный минимальный лот, USDT номинала")
    ap.add_argument("--range-pct", type=float, default=DEFAULTS["range_pct"], help="порог события: диапазон 5м, %%")
    ap.add_argument("--surge-min", type=float, default=DEFAULTS["surge_min"], help="порог события: всплеск оборота 5м к норме")
    ap.add_argument("--cooldown", type=float, default=DEFAULTS["cooldown"], help="секунд тишины по символу после события")
    ap.add_argument("--wake-score", type=float, default=DEFAULTS["wake_score"], help="в stdout (будит агента) только события со score >= порога; остальные только в лог и статистику (0 = все)")
    ap.add_argument("--outcome-min", default=DEFAULTS["outcome_min"], help="горизонты авто-статистики пропущенных, минуты через запятую (15,60 или 15,60,240)")
    ap.add_argument("--max-per-hour", type=int, default=DEFAULTS["max_per_hour"], help="потолок событий в час")
    ap.add_argument("--idea-zone-pct", type=float, default=DEFAULTS["idea_zone_pct"], help="зона у уровня идеи, %%: цена внутри даёт idea_near (будит агента)")
    ap.add_argument("--interval", type=float, default=DEFAULTS["interval"], help="период опроса REST, секунд (30-60)")
    ap.add_argument("--max-seconds", type=float, default=0.0, help="самозавершение (0 = работать до остановки)")


def params_of(a):
    return {"top": a.top, "min_turnover": a.min_turnover, "max_spread_bps": a.max_spread_bps, "max_lot": a.max_lot,
            "range_pct": a.range_pct, "surge_min": a.surge_min, "cooldown": a.cooldown, "max_per_hour": a.max_per_hour, "wake_score": a.wake_score, "outcome_min": a.outcome_min, "idea_zone_pct": a.idea_zone_pct, "interval": max(a.interval, 5.0),
            "max_seconds": a.max_seconds}


def main_scan(c, a):
    """Вызывается из venues/bybit.py (команда scan)."""
    try:
        return run(c, params_of(a), a.watch)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


def main(argv):
    from tb.venues import bybit
    ap = argparse.ArgumentParser(prog="hotscan")
    add_args(ap)
    a = ap.parse_args(argv)
    return main_scan(bybit.Client(os.environ.get("BYBIT_ENV", "mainnet")), a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
