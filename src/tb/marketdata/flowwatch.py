"""Поток ордерфлоу Bybit (публичный WebSocket, без ключей): сделки и стакан по небольшому набору монет + разрыв цены с Binance. Владелец: маркетдата.

Форматы проверены живым подключением: publicTrade.<SYM>: data=[{"T":мс,"s","S":"Buy|Sell"(СТОРОНА ТЕЙКЕРА),"v":"0.140"(в монете),"p":"80379.70"}];
orderbook.<N>.<SYM>: type snapshot|delta, data={"s","b":[[цена,размер]],"a":[[цена,размер]],"u","seq"}, размер "0" = удалить уровень, u == 1 = сброс книги.

Что считается (на монету, окна по сделкам):
  CVD: агрессивные покупки минус продажи в USD за 1м и 5м, доля перекоса cvd_5m_pct, крупные принты;
  стакан: дисбаланс заявок bid/ask в пределах imb_range_pct от середины (imb_pct), стены (уровень в mult раз крупнее медианы и не меньше min_wall_usd), которые стоят дольше persist_s
          (фильтр «мигающих» заявок: spoof);
  Binance: разница середины Bybit и bookTicker Binance, б.п. (xex_bps), раз в xex_s по REST; недоступно (гео-блок, сеть) = поле пустое, остальное работает.
События в runtime/hot_events.jsonl (дальше как у остальных сигналов скана): cvd_divergence (цена идёт в одну сторону, а агрессия в другую), book_wall (устойчивая стена у цены),
xex_gap (разрыв с Binance держится). Антиспам: cooldown на (вид, монета), громких не больше max_loud_per_hour в час, остальные тихие (лог и статистика исходов).
runtime/flow.json пишется раз в publish_s для дашборда и досье. Ничего не торгует, ключей не читает; сбой гасится внутри.
"""
import asyncio
import json
import os
import statistics
import time
import urllib.request
from collections import deque

from tb import fsutil, paths

DEFAULTS = {"enabled": True, "env": "mainnet", "n_syms": 16, "always": ["BTC", "ETH", "SOL"], "depth": 200, "publish_s": 2.0, "resync_s": 30.0,
            "div_ret_pct": 0.25, "div_cvd_pct": 12.0, "min_flow_usd": 300000.0, "big_usd": 100000.0,
            "imb_range_pct": 0.5, "wall_mult": 6.0, "min_wall_usd": 400000.0, "wall_range_pct": 1.0, "persist_s": 30.0,
            "cooldown_s": 900.0, "max_loud_per_hour": 4, "wall_wake_score": 2.5, "xex": True, "xex_s": 5.0, "xex_gap_bps": 12.0, "xex_confirm": 2}
URLS = {"mainnet": "wss://stream.bybit.com/v5/public/linear", "testnet": "wss://stream-testnet.bybit.com/v5/public/linear"}
BINANCE_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
REQ_TTL_S = 900.0            # сколько монета остаётся в наборе после запроса (сигнал скана по ней)
SUFFIX = "USDT"


def _f(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class OrderBook(object):
    """Книга заявок по снимку и дельтам."""

    def __init__(self):
        self.b, self.a = {}, {}
        self.ready = False

    def apply(self, d, typ):
        if typ == "snapshot" or d.get("u") == 1:
            self.b.clear()
            self.a.clear()
        for rows, book in ((d.get("b"), self.b), (d.get("a"), self.a)):
            for it in rows or ():
                try:
                    p, s = _f(it[0]), _f(it[1])
                except (TypeError, IndexError):
                    continue
                if p is None or s is None:
                    continue
                if s == 0:
                    book.pop(p, None)
                else:
                    book[p] = s
        self.ready = bool(self.b and self.a)

    def best(self):
        if not (self.b and self.a):
            return None
        return max(self.b), min(self.a)

    def mid(self):
        bb = self.best()
        if bb is None or bb[0] >= bb[1]:
            return None
        return (bb[0] + bb[1]) / 2.0

    def spread_bps(self):
        bb, m = self.best(), self.mid()
        return 1e4 * (bb[1] - bb[0]) / m if bb and m else None

    def side_levels(self, side, pct, mid):
        """[(цена, USD)] стороны side ('b'|'a') в пределах pct % от середины."""
        lo, hi = mid * (1 - pct / 100.0), mid * (1 + pct / 100.0)
        book = self.b if side == "b" else self.a
        return [(p, p * s) for p, s in book.items() if lo <= p <= hi]

    def imbalance(self, pct):
        """(bid_usd, ask_usd, imb_pct) в пределах pct %; imb = (bid-ask)/(bid+ask)*100."""
        m = self.mid()
        if m is None:
            return None
        bid = sum(u for _, u in self.side_levels("b", pct, m))
        ask = sum(u for _, u in self.side_levels("a", pct, m))
        tot = bid + ask
        return bid, ask, (100.0 * (bid - ask) / tot if tot > 0 else 0.0)

    def wall_candidates(self, pct, mult, min_usd):
        """Уровни, крупнее медианы стороны в mult раз и не меньше min_usd: [(side, price, usd, dist_pct)]."""
        m = self.mid()
        out = []
        if m is None:
            return out
        for side in ("b", "a"):
            lv = self.side_levels(side, pct, m)
            if len(lv) < 8:
                continue
            med = statistics.median(u for _, u in lv)
            for p, u in lv:
                if u >= max(min_usd, mult * med):
                    out.append((side, p, u, 100.0 * abs(p - m) / m))
        return out


class Tape(object):
    """Лента сделок монеты: знаковый объём тейкеров и цена по секундам."""

    def __init__(self, keep_s=900.0, cap=20000):
        self.keep_s = keep_s
        self.t = deque(maxlen=cap)            # (t_s, sign, usd)
        self.px = deque(maxlen=1200)          # (t_s, price) не чаще раза в секунду
        self.last = None

    def add(self, trades, now):
        n = 0
        for it in trades or ():
            if not isinstance(it, dict):
                continue
            p, v = _f(it.get("p")), _f(it.get("v"))
            sign = {"Buy": 1, "Sell": -1}.get(it.get("S"))
            ts = _f(it.get("T"))
            if not p or not v or p <= 0 or v <= 0 or sign is None:
                continue
            ts = ts / 1000.0 if ts else now
            self.t.append((ts, sign, p * v))
            self.last = p
            if not self.px or ts - self.px[-1][0] >= 1.0:
                self.px.append((ts, p))
            n += 1
        return n

    def window(self, now, w, big_usd):
        buy = sell = big = 0.0
        n = nb = 0
        for ts, sign, usd in reversed(self.t):
            if now - ts > w:
                break
            if sign > 0:
                buy += usd
            else:
                sell += usd
            n += 1
            if usd >= big_usd:
                big += sign * usd
                nb += 1
        tot = buy + sell
        return {"buy": buy, "sell": sell, "net": buy - sell, "pct": (100.0 * (buy - sell) / tot) if tot > 0 else 0.0, "n": n, "big_net": big, "big_n": nb}

    def ret(self, now, w):
        """Изменение цены за w секунд, % (None, если истории меньше 90% окна)."""
        if len(self.px) < 2 or now - self.px[0][0] < 0.9 * w:
            return None
        ref = None
        for ts, p in self.px:
            if ts <= now - w:
                ref = p
            else:
                break
        if ref is None:
            ref = self.px[0][1]
        return 100.0 * (self.px[-1][1] / ref - 1.0) if ref else None

    def span(self, now):
        """Сколько секунд ленты накоплено (до keep_s): меньше 120 с CVD неполный."""
        return max(0.0, now - self.t[0][0]) if self.t else 0.0

    def prune(self, now):
        while self.t and now - self.t[0][0] > self.keep_s:
            self.t.popleft()


class FlowBook(object):
    """Состояние по монетам, детекторы и снимок для flow.json. Чистая логика: время приходит снаружи, сети и файлов нет."""

    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.books, self.tapes = {}, {}
        self.wall_seen = {}                 # (sym, side, price) -> first_seen
        self.last_event = {}                # (kind, sym[, side]) -> ts
        self.recent_loud = []
        self.xex = {}                       # sym -> {"bps": float, "n": подряд превышений, "ts": ts}
        self.xex_ok = None

    def book(self, sym):
        return self.books.setdefault(sym, OrderBook())

    def tape(self, sym):
        return self.tapes.setdefault(sym, Tape())

    def handle(self, msg, now):
        topic, data = msg.get("topic") or "", msg.get("data")
        if topic.startswith("publicTrade.") and isinstance(data, list):
            s = topic.split(".", 1)[1]
            return self.tape(s[:-len(SUFFIX)] if s.endswith(SUFFIX) else s).add(data, now)
        if topic.startswith("orderbook.") and isinstance(data, dict):
            s = data.get("s") or topic.rsplit(".", 1)[1]
            self.book(s[:-len(SUFFIX)] if s.endswith(SUFFIX) else s).apply(data, msg.get("type"))
            return 1
        return 0

    def set_xex(self, sym, binance_mid, now):
        """Разрыв середины Bybit и Binance в б.п. (положительный: Bybit дороже). Подряд превышений порога считается для xex_gap."""
        bm = self.book(sym).mid()
        if not bm or not binance_mid:
            return None
        bps = 1e4 * (bm - binance_mid) / binance_mid
        prev = self.xex.get(sym)
        if abs(bps) < self.cfg["xex_gap_bps"]:
            n = 0
        elif prev and prev["n"] > 0 and (prev["bps"] > 0) == (bps > 0):
            n = prev["n"] + 1                                  # тот же знак разрыва подряд
        else:
            n = 1
        self.xex[sym] = {"bps": bps, "n": n, "ts": now}
        return bps

    def detect(self, now):
        """-> события (cvd_divergence, book_wall, xex_gap) с антиспамом."""
        c = self.cfg
        cands = []
        for sym, tape in self.tapes.items():
            w5 = tape.window(now, 300.0, c["big_usd"])
            ret = tape.ret(now, 300.0)
            if ret is not None and (w5["buy"] + w5["sell"]) >= c["min_flow_usd"]:
                if (ret >= c["div_ret_pct"] and w5["pct"] <= -c["div_cvd_pct"]) or (ret <= -c["div_ret_pct"] and w5["pct"] >= c["div_cvd_pct"]):
                    score = min(abs(w5["pct"]) / c["div_cvd_pct"], 3.0)
                    cands.append((score, "cvd_divergence", sym, {"cvd_pct": round(w5["pct"], 1), "ret_5m_pct": round(ret, 3), "flow_usd": round(w5["buy"] + w5["sell"]),
                                                                  "direction": "up" if ret > 0 else "down"}))
        for sym, bk in self.books.items():
            if not bk.ready:
                continue
            cur = {}
            for side, p, usd, dist in bk.wall_candidates(c["wall_range_pct"], c["wall_mult"], c["min_wall_usd"]):
                key = (sym, side, p)
                cur[key] = self.wall_seen.get(key, now)
                if now - cur[key] >= c["persist_s"] and dist <= 0.5 * c["wall_range_pct"] + 1e-9:
                    cands.append((min(usd / c["min_wall_usd"], 3.0), "book_wall", sym, {"wall_usd": round(usd), "wall_dist_pct": round(dist, 3), "wall_side": "bid" if side == "b" else "ask", "wall_px": p}))
            for k in [k for k in self.wall_seen if k[0] == sym and k not in cur]:
                del self.wall_seen[k]
            self.wall_seen.update(cur)
        for sym, x in self.xex.items():
            if x["n"] >= c["xex_confirm"] and abs(x["bps"]) >= c["xex_gap_bps"]:
                cands.append((min(abs(x["bps"]) / c["xex_gap_bps"], 3.0), "xex_gap", sym, {"xex_bps": round(x["bps"], 1)}))
        cands.sort(key=lambda t: -t[0])
        self.recent_loud = [t for t in self.recent_loud if now - t < 3600.0]
        out = []
        for score, kind, sym, f in cands:
            key = (kind, sym, f.get("wall_side"))
            if now - self.last_event.get(key, -1e18) < c["cooldown_s"]:
                continue
            self.last_event[key] = now
            need = c["wall_wake_score"] if kind == "book_wall" else 1.5                      # стены крупные вдвое чаще встречаются: будим от 2.5 минимальных размеров
            wake = kind != "xex_gap" and score >= need and len(self.recent_loud) < c["max_loud_per_hour"]       # xex_gap всегда тихий: копим статистику, агента не будим
            if wake:
                self.recent_loud.append(now)
            out.append(make_event(kind, sym, score, f, self.tape(sym).last or self.book(sym).mid(), now, wake))
        return out

    def snapshot(self, now, env="mainnet"):
        c = self.cfg
        syms = []
        for sym in sorted(set(self.books) | set(self.tapes)):
            tape, bk = self.tape(sym), self.book(sym)
            w1, w5 = tape.window(now, 60.0, c["big_usd"]), tape.window(now, 300.0, c["big_usd"])
            row = {"sym": sym, "px": tape.last or bk.mid(), "cvd_1m_usd": round(w1["net"]), "cvd_5m_usd": round(w5["net"]), "cvd_5m_pct": round(w5["pct"], 1),
                   "buy_5m_usd": round(w5["buy"]), "sell_5m_usd": round(w5["sell"]), "big_5m_usd": round(w5["big_net"]), "big_5m_n": w5["big_n"],
                   "ret_5m_pct": tape.ret(now, 300.0), "warm_s": int(min(300.0, tape.span(now)))}
            im = bk.imbalance(c["imb_range_pct"]) if bk.ready else None
            if im:
                row.update({"bid_usd": round(im[0]), "ask_usd": round(im[1]), "imb_pct": round(im[2], 1), "spread_bps": round(bk.spread_bps() or 0.0, 2)})
            walls = []
            for side, p, usd, dist in bk.wall_candidates(c["wall_range_pct"], c["wall_mult"], c["min_wall_usd"]) if bk.ready else ():
                age = now - self.wall_seen.get((sym, side, p), now)
                walls.append({"side": "bid" if side == "b" else "ask", "px": p, "usd": round(usd), "dist_pct": round(dist, 3), "age_s": int(age), "stable": age >= c["persist_s"]})
            walls.sort(key=lambda w: -w["usd"])
            if walls:
                row["walls"] = walls[:4]
            x = self.xex.get(sym)
            if x is not None:
                row["xex_bps"] = round(x["bps"], 1)
            if row.get("px") is not None:
                syms.append(row)
        return {"ts": iso(now), "ts_epoch": now, "env": env, "xex_ok": self.xex_ok, "syms": syms}


def make_event(kind, sym, score, f, px, now, wake):
    f = dict(f)
    direction = f.pop("direction", None)
    reasons = {"cvd_divergence": "цена %s%% за 5м, агрессия %s%% (CVD): расхождение" % (f.get("ret_5m_pct"), f.get("cvd_pct")),
               "book_wall": "устойчивая стена %s %d USD на расстоянии %s%%" % (f.get("wall_side"), f.get("wall_usd", 0), f.get("wall_dist_pct")),
               "xex_gap": "середина Bybit отличается от Binance на %s б.п." % f.get("xex_bps")}[kind]
    ev = {"id": "%s-%s-%s" % (time.strftime("%d%H%M%S", time.gmtime(now)), sym, kind[:3]), "wake": wake, "type": kind, "sym": sym, "price": px, "score": round(score, 2),
          "reasons": [reasons], "direction": direction, "ret_5m_pct": f.pop("ret_5m_pct", None), "range_5m_pct": None,
          "note": "информационно: не сигнал входа; расхождение или стена сами по себе не повод входить"}
    ev.update({k: v for k, v in f.items() if k in ("cvd_pct", "wall_usd", "wall_dist_pct", "wall_side", "xex_bps")})
    return ev


def request(root, syms, now):
    """Скан увидел сигнал по монете: просим поток держать её сделки и стакан (runtime/flow_req.json {монета: ts}, живёт REQ_TTL_S). Сбой записи не мешает сканеру."""
    path = paths.runtime("flow_req.json", root=root)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d = {k: v for k, v in (d if isinstance(d, dict) else {}).items() if isinstance(v, (int, float)) and now - v < REQ_TTL_S}
    for sym in syms:
        if isinstance(sym, str) and sym and sym.isalnum() and sym.upper() == sym:
            d[sym] = now
    try:
        fsutil_write(path, d)
    except OSError:
        pass


def read_symbols(root, cfg, now=None):
    """Набор монет потока: always (BTC/ETH/SOL), затем запрошенные сканом по сигналам (новые первыми), затем горячие из runtime/hot.json (tradable); не больше n_syms."""
    now = time.time() if now is None else now
    out = [x for x in cfg.get("always") or [] if isinstance(x, str)]
    try:
        with open(paths.runtime("flow_req.json", root=root), encoding="utf-8") as f:
            req = json.load(f)
        for sym, ts in sorted(((k, v) for k, v in req.items() if isinstance(v, (int, float)) and now - v < REQ_TTL_S), key=lambda kv: -kv[1]):
            if sym not in out:
                out.append(sym)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        with open(paths.runtime("hot.json", root=root), encoding="utf-8") as f:
            for r in (json.load(f) or {}).get("top") or []:
                if isinstance(r, dict) and r.get("tradable", True) and isinstance(r.get("sym"), str) and r["sym"] not in out:
                    out.append(r["sym"])
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out[:int(cfg.get("n_syms", 16))]


def fetch_binance(timeout=5.0):
    """bookTicker Binance USDT-M по всем символам -> {SYM без USDT: mid}. Исключение при сбое (вызывающий гасит)."""
    req = urllib.request.Request(BINANCE_URL, headers={"User-Agent": "open-trader/flowwatch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rows = json.loads(r.read().decode("utf-8"))
    out = {}
    for it in rows if isinstance(rows, list) else ():
        s = it.get("symbol") or ""
        b, a = _f(it.get("bidPrice")), _f(it.get("askPrice"))
        if s.endswith(SUFFIX) and b and a and b > 0 and a >= b:
            out[s[:-len(SUFFIX)]] = (b + a) / 2.0
    return out


def fsutil_write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    fsutil.replace(tmp, path)


def _append_event(path, ev):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


class Runner(object):
    def __init__(self, root, cfg=None, clock=time.time, client_factory=None, stop=lambda: False, log=None, binance=None):
        self.root = root
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.book = FlowBook(self.cfg)
        self.clock, self.stop = clock, stop
        self.log = log or (lambda m: None)
        self.client_factory = client_factory
        self.binance = binance or fetch_binance
        self.subscribed = set()
        self._xex_fail_until = 0.0

    def topics(self, sym):
        return ["publicTrade.%s%s" % (sym, SUFFIX), "orderbook.%d.%s%s" % (int(self.cfg["depth"]), sym, SUFFIX)]

    def _msgs(self, syms, op):
        return [json.dumps({"op": op, "args": [t for s in syms[i:i + 4] for t in self.topics(s)]}, separators=(",", ":")) for i in range(0, len(syms), 4)]

    def publish(self, now):
        fsutil_write(paths.runtime("flow.json", root=self.root), self.book.snapshot(now, self.cfg["env"]))
        for ev in self.book.detect(now):
            ev = dict(ev, ts=iso(now), env=self.cfg["env"])
            try:
                _append_event(paths.runtime("hot_events.jsonl", root=self.root), ev)
            except OSError as e:
                self.log("flow: событие не записано (%s)" % type(e).__name__)

    def xex_step(self, now):
        """Синхронный шаг (вызывается в потоке): Binance bookTicker -> разрыв по монетам набора."""
        if now < self._xex_fail_until:
            return
        try:
            mids = self.binance()
            self.book.xex_ok = True
        except Exception as e:  # noqa: BLE001 - гео-блок, сеть, формат: канал разрыва просто молчит
            if self.book.xex_ok is not False:
                self.log("flow: Binance недоступен (%s), разрыв цен отключён на 60 с" % type(e).__name__)
            self.book.xex_ok = False
            self._xex_fail_until = now + 60.0
            return
        for sym in list(self.subscribed):
            if sym in mids:
                self.book.set_xex(sym, mids[sym], now)

    async def _resync(self, cl):
        want = set(read_symbols(self.root, self.cfg, self.clock()))
        add, gone = sorted(want - self.subscribed), sorted(self.subscribed - want)
        for m in self._msgs(add, "subscribe") + self._msgs(gone, "unsubscribe"):
            await cl.send(m)
            await asyncio.sleep(0.05)
        for s in gone:
            self.book.books.pop(s, None)
            self.book.tapes.pop(s, None)
            self.book.xex.pop(s, None)
        self.subscribed = want
        cl.subscriptions = self._msgs(sorted(want), "subscribe")

    async def main(self):
        from tb.marketdata.wsclient import WSClient
        url = URLS.get(self.cfg["env"], URLS["mainnet"])
        cl = (self.client_factory or (lambda u: WSClient(u, keepalive_text='{"op":"ping"}', keepalive_interval=20.0, silence_timeout=45.0, name="flow")))(url)
        state = {"pub": 0.0, "sync": -1e18, "prune": 0.0, "xex": 0.0}
        loop = asyncio.get_event_loop()

        async def ticker():
            while not self.stop():
                now = self.clock()
                try:
                    if now - state["sync"] >= (self.cfg["resync_s"] if self.subscribed else 15.0) and getattr(cl, "connected", False):
                        state["sync"] = now
                        await self._resync(cl)
                    if now - state["pub"] >= self.cfg["publish_s"]:
                        state["pub"] = now
                        self.publish(now)
                    if self.cfg["xex"] and now - state["xex"] >= self.cfg["xex_s"] and self.subscribed:
                        state["xex"] = now
                        await loop.run_in_executor(None, self.xex_step, now)
                    if now - state["prune"] >= 300.0:
                        state["prune"] = now
                        for t in self.book.tapes.values():
                            t.prune(now)
                except Exception as e:  # noqa: BLE001
                    self.log("flow: сбой цикла (%s)" % type(e).__name__)
                await asyncio.sleep(0.5)
            await cl.close()

        task = asyncio.ensure_future(ticker())
        try:
            async for raw in cl.messages():
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.book.handle(msg, self.clock())
                if self.stop():
                    break
        finally:
            task.cancel()
            try:
                await cl.close()
            except Exception:  # noqa: BLE001
                pass


def run_loop(root, cfg, stop=lambda: False, log=None):
    """Поток сервиса скана: держит соединение; при падении цикла ждёт и начинает снова."""
    while not stop():
        r = Runner(root, cfg, stop=stop, log=log)
        try:
            asyncio.run(r.main())
        except Exception as e:  # noqa: BLE001
            if log:
                log("flow: поток упал (%s), повтор через 15 с" % type(e).__name__)
        for _ in range(15):
            if stop():
                return
            time.sleep(1.0)
