#!/usr/bin/env python3
"""Демон рыночного фида (см. docs/SPEC_FEED.md). Ничего не торгует: только читает публичные WS,
считает признаки и пишет runtime/market.json, runtime/events.jsonl, runtime/health.json.
Цена фида - вспомогательный сигнал/кросс-проверка; исполнение идёт по цене платформы.

Запуск: ./tb feed start [--root DIR] [--config FILE] [--once] [--selftest]

ВСЕ форматы сообщений биржевых WS ниже написаны по памяти документации и НЕ ПРОВЕРЕНЫ ВЖИВУЮ.
"""
import argparse
import asyncio
import collections
import datetime
import json
import math
import os
import signal
import sys
import time

from tb import paths
from tb.marketdata import features
from tb.marketdata.wsclient import WSClient
from tb import fsutil


# ---------------------------------------------------------------- адаптеры бирж
# Нормализованные события (dict, поле "kind"):
#   trade  {inst, price, size, side('buy'|'sell' - сторона тейкера)}
#   ticker {inst, price?, bid?, ask?}
#   book   {inst, snapshot(bool), bids[(p,s)], asks[(p,s)]}   (size 0 в delta = удалить уровень)
#   funding{inst, rate}  (ставка за интервал биржи)
#   oi     {inst, oi}    (в базовом активе)
# parse_* возвращает [] для служебных сообщений (ack/pong) и бросает ValueError на мусор.

def _num(x):
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def _levels(rows):
    return [(_num(r[0]), _num(r[1])) for r in rows]


def parse_okx(raw):
    """OKX v5 public WS (wss://ws.okx.com:8443/ws/v5/public). НЕ ПРОВЕРЕНО ВЖИВУЮ.
    Опора (по памяти документации OKX API v5 WebSocket):
      подписка: {"op":"subscribe","args":[{"channel":"trades","instId":"ETH-USDT-SWAP"}]}
      ack: {"event":"subscribe","arg":{...}}; ошибка: {"event":"error","code":..,"msg":..}
      keepalive: клиент шлёт текст "ping", сервер отвечает текстом "pong"
      данные: {"arg":{"channel":..,"instId":..},"data":[...]}
      trades: px, sz (контракты!), side (buy/sell = сторона тейкера), ts(мс)
      tickers: last, bidPx, askPx; books5: bids/asks [[px,sz,0,orders]] - полный снапшот
      funding-rate: fundingRate (за 8ч); open-interest: oi (контракты), oiCcy (в монете)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    s = raw.strip()
    if s == "pong":
        return []
    msg = json.loads(s)
    if not isinstance(msg, dict):
        raise ValueError("okx: not an object")
    if "event" in msg:
        if msg["event"] == "error":
            raise ValueError("okx error: %s" % msg.get("msg"))
        return []
    arg, data = msg.get("arg"), msg.get("data")
    if not isinstance(arg, dict) or not isinstance(data, list):
        raise ValueError("okx: unexpected shape")
    ch = arg.get("channel")
    out = []
    for d in data:
        inst = d.get("instId", arg.get("instId"))
        if ch == "trades":
            side = d["side"]
            if side not in ("buy", "sell"):
                raise ValueError("okx: bad side")
            out.append({"kind": "trade", "inst": inst, "price": _num(d["px"]),
                        "size": _num(d["sz"]), "side": side})
        elif ch == "tickers":
            ev = {"kind": "ticker", "inst": inst, "price": _num(d["last"])}
            if d.get("bidPx") not in (None, ""):
                ev["bid"] = _num(d["bidPx"])
            if d.get("askPx") not in (None, ""):
                ev["ask"] = _num(d["askPx"])
            out.append(ev)
        elif ch in ("books5", "books", "bbo-tbt"):
            out.append({"kind": "book", "inst": inst, "snapshot": True,
                        "bids": _levels(d["bids"]), "asks": _levels(d["asks"])})
        elif ch == "funding-rate":
            out.append({"kind": "funding", "inst": inst, "rate": _num(d["fundingRate"])})
        elif ch == "open-interest":
            out.append({"kind": "oi", "inst": inst, "oi": _num(d.get("oiCcy") or d["oi"])})
        else:
            raise ValueError("okx: unknown channel %r" % ch)
    return out


def parse_bybit(raw):
    """Bybit v5 public linear (wss://stream.bybit.com/v5/public/linear). НЕ ПРОВЕРЕНО ВЖИВУЮ.
    Опора (по памяти документации Bybit v5 WebSocket):
      подписка: {"op":"subscribe","args":["publicTrade.ETHUSDT","tickers.ETHUSDT","orderbook.50.ETHUSDT"]}
      ack: {"success":true,"ret_msg":"subscribe","op":"subscribe"}; ping: {"op":"ping"} -> {"op":"pong",...}
      publicTrade: data=[{"s","S":"Buy|Sell"(тейкер),"v","p","T"}], v в монете для linear
      tickers: data - dict; snapshot, затем delta только с изменёнными полями
        (lastPrice, bid1Price, ask1Price, fundingRate, openInterest)
      orderbook.N: data={"s","b":[[p,s]],"a":[[p,s]]}, type snapshot|delta, size "0" = удалить."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise ValueError("bybit: not an object")
    topic = msg.get("topic")
    if topic is None:
        if msg.get("success") is False:
            raise ValueError("bybit error: %s" % msg.get("ret_msg"))
        if "op" in msg or "ret_msg" in msg:
            return []
        raise ValueError("bybit: no topic")
    data = msg.get("data")
    out = []
    if topic.startswith("publicTrade."):
        if not isinstance(data, list):
            raise ValueError("bybit: trade data")
        for d in data:
            side = {"Buy": "buy", "Sell": "sell"}.get(d["S"])
            if side is None:
                raise ValueError("bybit: bad side")
            out.append({"kind": "trade", "inst": d["s"], "price": _num(d["p"]),
                        "size": _num(d["v"]), "side": side})
    elif topic.startswith("tickers."):
        if not isinstance(data, dict):
            raise ValueError("bybit: ticker data")
        inst = data.get("symbol") or topic.split(".", 1)[1]
        ev = {"kind": "ticker", "inst": inst}
        if data.get("lastPrice") not in (None, ""):
            ev["price"] = _num(data["lastPrice"])
        if data.get("bid1Price") not in (None, ""):
            ev["bid"] = _num(data["bid1Price"])
        if data.get("ask1Price") not in (None, ""):
            ev["ask"] = _num(data["ask1Price"])
        if len(ev) > 2:
            out.append(ev)
        if data.get("fundingRate") not in (None, ""):
            out.append({"kind": "funding", "inst": inst, "rate": _num(data["fundingRate"])})
        if data.get("openInterest") not in (None, ""):
            out.append({"kind": "oi", "inst": inst, "oi": _num(data["openInterest"])})
    elif topic.startswith("orderbook."):
        if not isinstance(data, dict):
            raise ValueError("bybit: book data")
        out.append({"kind": "book", "inst": data.get("s") or topic.rsplit(".", 1)[1],
                    "snapshot": msg.get("type") == "snapshot",
                    "bids": _levels(data.get("b", [])), "asks": _levels(data.get("a", []))})
    else:
        raise ValueError("bybit: unknown topic %r" % topic)
    return out


def subscribe_okx(scfg, insts):
    args = [{"channel": ch, "instId": i} for i in insts for ch in scfg["channels"]]
    return [json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))]


def subscribe_bybit(scfg, insts):
    args = ["%s.%s" % (t, i) for i in insts for t in scfg["topics"]]
    return [json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))]


ADAPTERS = {"okx": (parse_okx, subscribe_okx), "bybit": (parse_bybit, subscribe_bybit)}


# ---------------------------------------------------------------- вспомогательное
def iso(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    fsutil.replace(tmp, path)


def _r(x, nd=8):
    return None if x is None else round(x, nd)


class SymSrc(object):
    """Состояние одного символа на одном источнике."""

    def __init__(self):
        self.price = self.price_ts = None
        self.bid = self.ask = None
        self.bids = {}
        self.asks = {}
        self.book_ts = None
        self.trades = collections.deque(maxlen=60000)
        self.funding = self.funding_ts = None
        self.oi = None
        self.oi_series = collections.deque(maxlen=4000)


class Feed(object):
    def __init__(self, cfg, now=None):
        self.cfg = cfg
        self.start = time.time() if now is None else now
        self.sources = [s for s in cfg["source_priority"] if cfg["sources"].get(s, {}).get("enabled")]
        self.inst2sym = {}
        for s in self.sources:
            self.inst2sym[s] = {m[s]: sym for sym, m in cfg["symbols"].items() if s in m}
        self.state = {(sym, s): SymSrc() for sym in cfg["symbols"] for s in self.sources}
        self.series = {sym: collections.deque(maxlen=int(cfg["price_hist_s"] / max(cfg["write_interval_s"], 0.2)) + 10)
                       for sym in cfg["symbols"]}
        self.src_stats = {s: {"messages": 0, "parse_errors": 0, "last_msg": self.start, "last_error": None}
                          for s in self.sources}
        self.src_state = {s: "ok" for s in self.sources}
        self.last_event = {}
        self.prev_regime = {}
        self.pending = []
        self.last_market = None

    # ---- вход сообщений ----
    def on_raw(self, src, raw, now):
        st = self.src_stats[src]
        st["last_msg"] = now
        st["messages"] += 1
        try:
            events = ADAPTERS[src][0](raw)
            for ev in events:
                self.apply(src, ev, now)
        except Exception as e:  # битое/неизвестное - пропуск со счётчиком
            st["parse_errors"] += 1
            st["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])

    def apply(self, src, ev, now):
        sym = self.inst2sym[src].get(ev["inst"])
        if sym is None:
            raise ValueError("unknown instrument %r" % ev["inst"])
        ss = self.state[(sym, src)]
        k = ev["kind"]
        if k == "trade":
            size = ev["size"] * self.cfg["sources"][src].get("contract_size", {}).get(sym, 1.0)
            ss.trades.append((now, size, ev["side"]))
            ss.price, ss.price_ts = ev["price"], now
        elif k == "ticker":
            if "price" in ev:
                ss.price, ss.price_ts = ev["price"], now
            if "bid" in ev:
                ss.bid = ev["bid"]
            if "ask" in ev:
                ss.ask = ev["ask"]
        elif k == "book":
            if ev["snapshot"]:
                ss.bids, ss.asks = {}, {}
            for side, rows in ((ss.bids, ev["bids"]), (ss.asks, ev["asks"])):
                for p, s in rows:
                    if s == 0:
                        side.pop(p, None)
                    else:
                        side[p] = s
            ss.book_ts = now
        elif k == "funding":
            ss.funding, ss.funding_ts = ev["rate"], now
        elif k == "oi":
            ss.oi = ev["oi"]
            ss.oi_series.append((now, ev["oi"]))
        else:
            raise ValueError("unknown kind")

    # ---- состояние источников ----
    def live_price(self, sym, src, now):
        ss = self.state[(sym, src)]
        return ss.price if ss.price is not None and now - ss.price_ts <= self.cfg["stale_seconds"] else None

    def primary(self, sym, now):
        for s in self.sources:
            if self.live_price(sym, s, now) is not None:
                return s
        return None

    def src_live(self, src, now):
        return now - self.src_stats[src]["last_msg"] <= self.cfg["stale_seconds"]

    def sample(self, now):
        """Раз в write_interval: добавить точку цены в ряд символа."""
        for sym in self.cfg["symbols"]:
            p = self.primary(sym, now)
            if p is not None:
                self.series[sym].append((now, self.state[(sym, p)].price))

    # ---- признаки ----
    def compute_symbol(self, sym, now):
        cfg = self.cfg
        src = self.primary(sym, now)
        by_src = {s: self.live_price(sym, s, now) for s in self.sources}
        by_src = {s: p for s, p in by_src.items() if p is not None}
        out = {"price": None, "src": src, "age_s": None, "prices_by_src": by_src}
        series = list(self.series[sym])
        if src is None:
            out.update({"ret_1m": None, "ret_5m": None, "ret_15m": None, "rv_5m": None, "rv_1h": None,
                        "book": {"spread_bps": None, "imbalance": None}, "cvd_1m": None, "cvd_5m": None,
                        "funding_hourly": None, "oi": None, "oi_chg_5m": None,
                        "ema_fast": None, "ema_slow": None, "regime": "unknown"})
            return out
        ss = self.state[(sym, src)]
        out["price"] = ss.price
        out["age_s"] = _r(now - ss.price_ts, 2)
        out["ret_1m"] = _r(features.window_return(series, now, 60))
        out["ret_5m"] = _r(features.window_return(series, now, 300))
        ret15 = features.window_return(series, now, 900)
        out["ret_15m"] = _r(ret15)
        rv5 = features.realized_vol(series, now, 300, 5)
        out["rv_5m"] = _r(rv5)
        out["rv_1h"] = _r(features.realized_vol(series, now, 3600, 30))
        n = cfg["book_levels"]
        fresh_book = ss.book_ts is not None and now - ss.book_ts <= cfg["stale_seconds"]
        bids = sorted(ss.bids.items(), reverse=True)[:n] if fresh_book else []
        asks = sorted(ss.asks.items())[:n] if fresh_book else []
        bid = bids[0][0] if bids else ss.bid
        ask = asks[0][0] if asks else ss.ask
        out["book"] = {"spread_bps": _r(features.spread_bps(bid, ask), 4),
                       "imbalance": _r(features.book_imbalance(bids, asks, n), 4)}
        trades = ss.trades
        out["cvd_1m"] = _r(features.cvd(trades, now, 60), 6)
        out["cvd_5m"] = _r(features.cvd(trades, now, 300), 6)
        interval = cfg["sources"][src].get("funding_interval_h", 8)
        out["funding_hourly"] = None if ss.funding is None else _r(ss.funding / interval, 10)
        out["oi"] = ss.oi
        out["oi_chg_5m"] = _r(features.oi_change(list(ss.oi_series), now, 300))
        bars = features.bars_from_series(series, cfg["bar_s"])
        closes = [b["c"] for b in bars]
        ef = features.ema(closes, cfg["ema_fast"]) if len(closes) >= cfg["ema_slow"] else None
        es = features.ema(closes, cfg["ema_slow"]) if len(closes) >= cfg["ema_slow"] else None
        out["ema_fast"], out["ema_slow"] = _r(ef, 6), _r(es, 6)
        a = features.atr(bars, cfg["atr_n"])
        out["regime"] = features.classify_regime(rv5, ef, es, a, ret15, cfg["regime"])
        return out

    def market(self, now):
        syms = {sym: self.compute_symbol(sym, now) for sym in self.cfg["symbols"]}
        ok = bool(syms) and all(v["price"] is not None for v in syms.values())
        m = {"ts": iso(now), "ts_epoch": round(now, 3), "feed_ok": ok, "symbols": syms}
        self.last_market = m
        return m

    # ---- события ----
    def _emit(self, typ, sym, data, now, extra=None):
        key = (typ, sym, extra)
        last = self.last_event.get(key)
        if last is not None and now - last < self.cfg["event_cooldown_s"]:
            return None
        self.last_event[key] = now
        ev = {"ts": iso(now), "type": typ, "symbol": sym, "data": data}
        self.pending.append(ev)
        return ev

    def detect(self, now, market=None):
        market = market or self.last_market or self.market(now)
        ec = self.cfg["events"]
        for src in self.sources:
            live = self.src_live(src, now)
            if not live and self.src_state[src] == "ok":
                self.src_state[src] = "stale"
                self._emit("feed_stale", "*", {"src": src, "silent_s": round(now - self.src_stats[src]["last_msg"], 1)},
                           now, src)
            elif live and self.src_state[src] == "stale":
                self.src_state[src] = "ok"
                self.last_event.pop(("feed_recovered", "*", src), None)
                self._emit("feed_recovered", "*", {"src": src}, now, src)
        for sym, v in market["symbols"].items():
            if v["price"] is None:
                continue
            prev = self.prev_regime.get(sym)
            if prev is not None and v["regime"] != prev and "unknown" not in (prev, v["regime"]):
                self._emit("regime_change", sym, {"from": prev, "to": v["regime"]}, now)
            self.prev_regime[sym] = v["regime"]
            imb = v["book"]["imbalance"]
            if imb is not None and abs(imb) >= ec["imbalance_spike"]:
                self._emit("imbalance_spike", sym, {"imbalance": imb}, now)
            rv5, rv1h = v["rv_5m"], v["rv_1h"]
            if rv5 is not None and rv1h:
                ratio = rv5 / (rv1h * math.sqrt(300.0 / 3600.0))
                if ratio >= ec["vol_spike_ratio"]:
                    self._emit("vol_spike", sym, {"rv_5m": rv5, "rv_1h": rv1h, "ratio": round(ratio, 2)}, now)
            ps = [p for p in v["prices_by_src"].values()]
            if len(ps) >= 2:
                div = (max(ps) - min(ps)) / min(ps) * 1e4
                if div > self.cfg["events"]["divergence_bps"]:
                    self._emit("src_divergence", sym, {"bps": round(div, 2), "prices": v["prices_by_src"]}, now)
            self._detect_breakout(sym, v, now)

    def _detect_breakout(self, sym, v, now):
        ec = self.cfg["events"]
        win, rec = ec["breakout_window_s"], ec["breakout_recent_s"]
        series = self.series[sym]
        hist = [p for t, p in series if now - win <= t <= now - rec]
        if not series or series[0][0] > now - 0.5 * win or len(hist) < 10:
            return
        hi, lo, px = max(hist), min(hist), v["price"]
        if lo <= px <= hi:
            return
        trades = self.state[(sym, v["src"])].trades
        vol_recent = features.volume(trades, now, 60)
        avg = features.volume(trades, now, win) / (win / 60.0)
        if avg > 0 and vol_recent >= ec["breakout_vol_mult"] * avg:
            self._emit("breakout", sym, {"dir": "up" if px > hi else "down", "price": px,
                                         "range": [lo, hi], "vol_ratio": round(vol_recent / avg, 2)}, now)

    # ---- health ----
    def health(self, now, clients=None):
        clients = clients or {}
        srcs = {}
        for s in self.sources:
            st, c = self.src_stats[s], clients.get(s)
            srcs[s] = {"connected": bool(c and c.connected),
                       "last_msg_age_s": round(now - st["last_msg"], 2),
                       "reconnects": c.reconnects if c else 0,
                       "errors": (c.errors if c else 0) + st["parse_errors"],
                       "parse_errors": st["parse_errors"], "messages": st["messages"],
                       "last_error": st["last_error"] or (c.last_error if c else None),
                       "state": self.src_state[s]}
        return {"ts": iso(now), "ts_epoch": round(now, 3), "pid": os.getpid(),
                "uptime_s": round(now - self.start, 1),
                "feed_ok": bool(self.last_market and self.last_market["feed_ok"]), "sources": srcs}


# ---------------------------------------------------------------- runtime
def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_events(path, events):
    if not events:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_cycle(feed, rdir, now, clients=None, running=True):
    feed.sample(now)
    m = feed.market(now)
    feed.detect(now, m)
    atomic_write_json(os.path.join(rdir, "market.json"), m)
    ev, feed.pending = feed.pending, []
    append_events(os.path.join(rdir, "events.jsonl"), ev)
    h = feed.health(now, clients)
    h["running"] = running
    atomic_write_json(os.path.join(rdir, "health.json"), h)


async def source_task(feed, name, clients):
    cfg = feed.cfg
    scfg, w = cfg["sources"][name], cfg["ws"]
    insts = [m[name] for m in cfg["symbols"].values() if name in m]
    client = WSClient(scfg["url"], allow_plain=scfg.get("allow_plain", False),
                      max_frame=w["max_frame_bytes"], silence_timeout=w["silence_timeout_s"],
                      connect_timeout=w["connect_timeout_s"], backoff_base=w["backoff_base_s"],
                      backoff_max=w["backoff_max_s"], keepalive_text=scfg.get("keepalive_text"),
                      keepalive_interval=scfg.get("keepalive_interval_s", 0),
                      subscriptions=ADAPTERS[name][1](scfg, insts), name=name)
    clients[name] = client
    try:
        async for raw in client.messages():
            feed.on_raw(name, raw, time.time())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        feed.src_stats[name]["last_error"] = "task: %s" % e


async def run(cfg, root, once=False, stop=None):
    rdir = paths.runtime(root=root)  # cfg["runtime_dir"] оставлен в конфиге для совместимости; путь задаёт tb.paths
    feed = Feed(cfg)
    clients = {}
    stop = stop or asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    tasks = [asyncio.ensure_future(source_task(feed, s, clients)) for s in feed.sources]
    deadline = time.time() + cfg["once_seconds"] if once else None
    try:
        while not stop.is_set():
            write_cycle(feed, rdir, time.time(), clients)
            if deadline and time.time() >= deadline:
                break
            try:
                await asyncio.wait_for(stop.wait(), cfg["write_interval_s"])
            except asyncio.TimeoutError:
                pass
    finally:
        for c in clients.values():
            try:
                await c.close()
            except Exception:
                pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        write_cycle(feed, rdir, time.time(), clients, running=False)
    return feed


# ---------------------------------------------------------------- selftest
FIXTURES = {
    "okx_trade": ("okx", '{"arg":{"channel":"trades","instId":"ETH-USDT-SWAP"},"data":[{"instId":"ETH-USDT-SWAP","tradeId":"1","px":"2307.9","sz":"5","side":"buy","ts":"1793000000000"}]}',
                  [("trade", "price", 2307.9)]),
    "okx_ticker": ("okx", '{"arg":{"channel":"tickers","instId":"BTC-USDT-SWAP"},"data":[{"instId":"BTC-USDT-SWAP","last":"65000.1","bidPx":"65000","askPx":"65000.2","ts":"1793000000000"}]}',
                   [("ticker", "bid", 65000.0)]),
    "okx_book": ("okx", '{"arg":{"channel":"books5","instId":"ETH-USDT-SWAP"},"data":[{"asks":[["2308","10","0","2"],["2309","5","0","1"]],"bids":[["2307.9","30","0","3"],["2307","5","0","1"]],"ts":"1793000000000"}]}',
                 [("book", "snapshot", True)]),
    "okx_funding": ("okx", '{"arg":{"channel":"funding-rate","instId":"ETH-USDT-SWAP"},"data":[{"instId":"ETH-USDT-SWAP","fundingRate":"0.0001","nextFundingRate":"0.00012","fundingTime":"1793004000000"}]}',
                    [("funding", "rate", 0.0001)]),
    "okx_oi": ("okx", '{"arg":{"channel":"open-interest","instId":"ETH-USDT-SWAP"},"data":[{"instId":"ETH-USDT-SWAP","oi":"1234567","oiCcy":"123456.7","ts":"1793000000000"}]}',
               [("oi", "oi", 123456.7)]),
    "okx_ack": ("okx", '{"event":"subscribe","arg":{"channel":"trades","instId":"ETH-USDT-SWAP"}}', []),
    "okx_pong": ("okx", "pong", []),
    "bybit_trade": ("bybit", '{"topic":"publicTrade.ETHUSDT","type":"snapshot","ts":1793000000000,"data":[{"T":1793000000000,"s":"ETHUSDT","S":"Sell","v":"0.5","p":"2307.5","i":"x"}]}',
                    [("trade", "size", 0.5)]),
    "bybit_ticker": ("bybit", '{"topic":"tickers.ETHUSDT","type":"snapshot","data":{"symbol":"ETHUSDT","lastPrice":"2307.6","bid1Price":"2307.5","ask1Price":"2307.7","fundingRate":"0.0001","openInterest":"500000"}}',
                     [("ticker", "price", 2307.6), ("funding", "rate", 0.0001), ("oi", "oi", 500000.0)]),
    "bybit_book": ("bybit", '{"topic":"orderbook.50.ETHUSDT","type":"delta","data":{"s":"ETHUSDT","b":[["2307.5","0"]],"a":[["2307.7","3"]],"u":2}}',
                   [("book", "snapshot", False)]),
    "bybit_ack": ("bybit", '{"success":true,"ret_msg":"subscribe","conn_id":"x","op":"subscribe"}', []),
    "bybit_pong": ("bybit", '{"success":true,"ret_msg":"pong","op":"ping"}', []),
}
GARBAGE = [("okx", "{not json"), ("okx", "[1,2]"), ("okx", '{"arg":{"channel":"zzz"},"data":[{}]}'),
           ("bybit", '{"topic":"weird.X","data":1}'), ("bybit", "null"), ("okx", '{"event":"error","msg":"bad"}')]


def run_selftest(out=print):
    fails = []

    def check(name, cond):
        out("%s %s" % ("OK  " if cond else "FAIL", name))
        if not cond:
            fails.append(name)

    for name, (src, raw, expect) in sorted(FIXTURES.items()):
        try:
            evs = ADAPTERS[src][0](raw)
        except Exception as e:
            check("%s (exception %s)" % (name, e), False)
            continue
        good = len(evs) == len(expect) or (not expect and not evs)
        for ev, (kind, field, val) in zip(evs, expect):
            good = good and ev["kind"] == kind and ev.get(field) == val
        check("parse " + name, good)
    for src, raw in GARBAGE:
        try:
            ADAPTERS[src][0](raw)
            check("garbage rejected %s %r" % (src, raw[:20]), False)
        except Exception:
            check("garbage rejected %s %r" % (src, raw[:20]), True)
    f = features
    check("ema const", abs(f.ema([5.0] * 10, 4) - 5.0) < 1e-12)
    ser = [(i, 100.0 * (1.001 ** i)) for i in range(0, 1000)]
    r = f.window_return(ser, 999, 300)
    check("window_return", r is not None and abs(r - (1.001 ** 300 - 1)) < 1e-9)
    check("imbalance", f.book_imbalance([(1, 3)], [(2, 1)], 5) == 0.5)
    check("spread_bps", abs(f.spread_bps(99.99, 100.01) - 2.0) < 1e-6)
    check("cvd", f.cvd([(10, 2, "buy"), (11, 1, "sell")], 12, 5) == 1)
    reg = {"storm_rv_5m": 0.004, "trend_atr_mult": 0.5}
    check("regime storm", f.classify_regime(0.01, 1, 1, 1, 0, reg) == "storm")
    check("regime up", f.classify_regime(0.001, 102, 100, 2, 0.01, reg) == "trend_up")
    check("regime range", f.classify_regime(0.001, 100.1, 100, 2, 0.01, reg) == "range")
    check("regime unknown", f.classify_regime(0.001, None, 100, 2, 0.01, reg) == "unknown")
    out("SELFTEST %s" % ("OK" if not fails else "FAIL (%d)" % len(fails)))
    return not fails


def main(argv=None):
    ap = argparse.ArgumentParser(description="market feed daemon (no trading)")
    ap.add_argument("--root", default=paths.root())
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="collect for once_seconds, write once, exit")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if run_selftest() else 1
    cfg = load_config(a.config or paths.config("feed_config.json", root=a.root))
    try:
        asyncio.run(run(cfg, a.root, once=a.once))
    except KeyboardInterrupt:  # Windows: add_signal_handler недоступен, Ctrl+C приходит исключением
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
