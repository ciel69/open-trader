"""Поток ликвидаций Bybit (публичный WebSocket, без ключей): тема allLiquidation.<SYMBOL>USDT. Формат проверен живым подключением:
{"topic":"allLiquidation.HYPEUSDT","type":"snapshot","ts":..,"data":[{"T":мс,"s":"HYPEUSDT","S":"Sell","v":"17.62","p":"91.939"}]}; v в монете, S это СТОРОНА ЛИКВИДИРОВАННОЙ позиции
(Buy = ликвидирован лонг, цена падает; Sell = ликвидирован шорт, цена растёт: на живых данных Sell шёл при росте цены).

Что делает: держит кольцо ликвидаций по монетам (24 ч), раз в publish_s пишет runtime/liq.json (объём ликвидаций лонгов и шортов за 5м/1ч и карта цен: где были ликвидации) для
дашборда и досье; при каскаде (односторонний объём за окно >= min_usd) дописывает событие liq_cascade в runtime/hot_events.jsonl (дальше как у остальных сигналов скана: публикатор
-> шина -> entry_scout; антиспам: cooldown на монету, громких не больше max_loud_per_hour в час, остальные тихие). Ничего не торгует, ключей не читает; сбой гасится внутри.
Состав монет: топ по обороту из runtime/heat.json (его пишет скан), пересмотр каждые resync_s.
"""
import asyncio
import json
import os
import time
from collections import defaultdict, deque

from tb import fsutil, paths

DEFAULTS = {"enabled": True, "env": "mainnet", "min_usd": 100000.0, "window_s": 300.0, "cooldown_s": 900.0, "loud_mult": 2.0, "max_loud_per_hour": 6,
            "publish_s": 5.0, "resync_s": 600.0, "n_syms": 80, "keep_s": 86400.0, "cap_per_sym": 4000, "map_step_pct": 0.25, "map_top": 12}
URLS = {"mainnet": "wss://stream.bybit.com/v5/public/linear", "testnet": "wss://stream-testnet.bybit.com/v5/public/linear"}
SUFFIX = "USDT"


def _f(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class LiqBook(object):
    """Кольцо ликвидаций по монетам и детектор каскадов. Чистая логика: время передаётся снаружи, файлов и сети нет."""

    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.ev = defaultdict(lambda: deque(maxlen=int(self.cfg["cap_per_sym"])))      # sym -> deque((t_s, side, usd, px))
        self.last_event = {}                    # sym -> ts последнего каскада
        self.recent_loud = []
        self.last_px = {}
        self.n = 0

    def add(self, sym, side, size, px, t_s):
        """side: long (ликвидирован лонг) | short. size в монете. -> usd или None (мусор)."""
        size, px = _f(size), _f(px)
        if not size or not px or size <= 0 or px <= 0 or side not in ("long", "short"):
            return None
        usd = size * px
        self.ev[sym].append((t_s, side, usd, px))
        self.last_px[sym] = px
        self.n += 1
        return usd

    def handle(self, msg, now):
        """Сообщение WS -> сколько ликвидаций учтено."""
        topic, data = msg.get("topic") or "", msg.get("data")
        if not topic.startswith("allLiquidation.") or not isinstance(data, list):
            return 0
        n = 0
        for it in data:
            if not isinstance(it, dict):
                continue
            s = it.get("s") or topic.split(".", 1)[1]
            sym = s[:-len(SUFFIX)] if isinstance(s, str) and s.endswith(SUFFIX) else s
            side = {"Buy": "long", "Sell": "short"}.get(it.get("S"))                # Buy = ликвидирован лонг
            t = _f(it.get("T"))
            if self.add(sym, side, it.get("v"), it.get("p"), (t / 1000.0) if t else now) is not None:
                n += 1
        return n

    def prune(self, now):
        keep = float(self.cfg["keep_s"])
        for sym in list(self.ev):
            d = self.ev[sym]
            while d and now - d[0][0] > keep:
                d.popleft()
            if not d:
                del self.ev[sym]

    def window(self, sym, now, w):
        """{"long": usd, "short": usd, "n": число} за последние w секунд."""
        out = {"long": 0.0, "short": 0.0, "n": 0}
        for t, side, usd, _ in reversed(self.ev.get(sym) or ()):
            if now - t > w:
                break
            out[side] += usd
            out["n"] += 1
        return out

    def cascades(self, now):
        """Монеты, где за window_s односторонний объём >= min_usd и cooldown прошёл -> события liq_cascade (в порядке убывания объёма)."""
        c = self.cfg
        cands = []
        for sym in list(self.ev):
            if now - self.last_event.get(sym, -1e18) < c["cooldown_s"]:
                continue
            w = self.window(sym, now, c["window_s"])
            side = "long" if w["long"] >= w["short"] else "short"
            if w[side] >= c["min_usd"]:
                cands.append((w[side], sym, side, w))
        cands.sort(reverse=True)
        self.recent_loud = [t for t in self.recent_loud if now - t < 3600.0]
        out = []
        for usd, sym, side, w in cands:
            self.last_event[sym] = now
            wake = usd >= c["min_usd"] * c["loud_mult"] and len(self.recent_loud) < c["max_loud_per_hour"]
            if wake:
                self.recent_loud.append(now)
            out.append(make_event(sym, side, usd, w, self.last_px.get(sym), now, wake, c["window_s"]))
        return out

    def levels(self, sym, now, step_pct=None, top=None):
        """Карта ликвидаций за 24 ч: цены, сгруппированные корзинами step_pct, самые объёмные: [{"px","long","short"}] по убыванию суммы (где толпу уже выбивали)."""
        c = self.cfg
        step = (step_pct or c["map_step_pct"]) / 100.0
        ref = self.last_px.get(sym)
        if not ref:
            return []
        b = defaultdict(lambda: {"long": 0.0, "short": 0.0})
        for t, side, usd, px in self.ev.get(sym) or ():
            k = round(px / ref / step)
            b[k][side] += usd
        rows = [{"px": float("%.6g" % (k * step * ref)), "long": round(v["long"]), "short": round(v["short"])} for k, v in b.items()]
        rows.sort(key=lambda r: -(r["long"] + r["short"]))
        return rows[:top or c["map_top"]]

    def snapshot(self, now, env="mainnet", top=60):
        """runtime/liq.json: по монетам объёмы за 5м и 1ч (USD), число ликвидаций, карта цен для самых активных."""
        rows = []
        for sym in self.ev:
            w5, w1 = self.window(sym, now, 300.0), self.window(sym, now, 3600.0)
            if w1["n"] == 0:
                continue
            rows.append({"sym": sym, "long_5m": round(w5["long"]), "short_5m": round(w5["short"]), "long_1h": round(w1["long"]), "short_1h": round(w1["short"]), "n_1h": w1["n"],
                         "last_ts": self.ev[sym][-1][0], "px": self.last_px.get(sym)})
        rows.sort(key=lambda r: -(r["long_1h"] + r["short_1h"]))
        rows = rows[:top]
        for r in rows[:12]:
            r["map"] = self.levels(r["sym"], now)
        return {"ts": iso(now), "ts_epoch": now, "env": env, "n_total": self.n, "totals_1h": {"long": sum(r["long_1h"] for r in rows), "short": sum(r["short_1h"] for r in rows)},
                "syms": rows}


def make_event(sym, side, usd, w, px, now, wake, window_s):
    """Событие скана liq_cascade. direction: куда вынуждены двигаться ликвидаторы (ликвидированы лонги -> продажи -> down); это описание уже случившегося, не прогноз."""
    d = "down" if side == "long" else "up"
    return {"id": "%s-%s-liq" % (time.strftime("%d%H%M%S", time.gmtime(now)), sym), "wake": wake, "type": "liq_cascade", "sym": sym, "price": px, "score": round(usd / 1e5, 2),
            "reasons": ["ликвидации %s %s за %d мин: %d USD (n=%d)" % ("лонгов" if side == "long" else "шортов", sym, int(window_s // 60), int(usd), w["n"])],
            "direction": d, "liq_usd": round(usd), "liq_side": side, "ret_5m_pct": None, "range_5m_pct": None,
            "note": "информационно: не сигнал входа; каскад часто означает исчерпание движения или его продолжение: решает структура"}


def read_symbols(root, n):
    """Топ-n монет по обороту из runtime/heat.json (пишет скан) -> [SYM без USDT]. Нет файла -> []."""
    try:
        with open(paths.runtime("heat.json", root=root), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    rows = doc.get("rows") if isinstance(doc, dict) else None
    return [r["sym"] for r in (rows or [])[:n] if isinstance(r, dict) and isinstance(r.get("sym"), str)]


def _append_event(path, ev):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


class Runner(object):
    def __init__(self, root, cfg=None, clock=time.time, client_factory=None, stop=lambda: False, log=None):
        self.root = root
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.book = LiqBook(self.cfg)
        self.clock = clock
        self.stop = stop
        self.log = log or (lambda m: None)
        self.client_factory = client_factory
        self.subscribed = set()
        self.client = None

    def publish(self, now):
        doc = self.book.snapshot(now, self.cfg["env"])
        fsutil_write(paths.runtime("liq.json", root=self.root), doc)
        for ev in self.book.cascades(now):
            ev = dict(ev, ts=iso(now), env=self.cfg["env"])
            try:
                _append_event(paths.runtime("hot_events.jsonl", root=self.root), ev)
            except OSError as e:
                self.log("liq: событие не записано (%s)" % type(e).__name__)

    def _args(self, syms, op):
        return [json.dumps({"op": op, "args": ["allLiquidation.%s%s" % (s, SUFFIX) for s in syms[i:i + 10]]}, separators=(",", ":")) for i in range(0, len(syms), 10)]

    async def _resync(self, cl, now):
        want = set(read_symbols(self.root, int(self.cfg["n_syms"])))
        add, gone = sorted(want - self.subscribed), sorted(self.subscribed - want)
        for m in self._args(add, "subscribe") + self._args(gone, "unsubscribe"):
            await cl.send(m)
            await asyncio.sleep(0.05)
        self.subscribed = want
        # подписки хранятся в клиенте для автоповтора после реконнекта
        cl.subscriptions = self._args(sorted(want), "subscribe")

    async def main(self):
        from tb.marketdata.wsclient import WSClient
        url = URLS.get(self.cfg["env"], URLS["mainnet"])
        cl = (self.client_factory or (lambda u: WSClient(u, keepalive_text='{"op":"ping"}', keepalive_interval=20.0, silence_timeout=45.0, name="liq")))(url)
        self.client = cl
        state = {"pub": 0.0, "sync": -1e18, "prune": 0.0}

        async def ticker():
            while not self.stop():
                now = self.clock()
                try:
                    if now - state["sync"] >= (self.cfg["resync_s"] if self.subscribed else 15.0) and getattr(cl, "connected", False):
                        state["sync"] = now
                        await self._resync(cl, now)
                    if now - state["pub"] >= self.cfg["publish_s"]:
                        state["pub"] = now
                        self.publish(now)
                    if now - state["prune"] >= 600.0:
                        state["prune"] = now
                        self.book.prune(now)
                except Exception as e:  # noqa: BLE001 - вспомогательный поток, гасим
                    self.log("liq: сбой цикла (%s)" % type(e).__name__)
                await asyncio.sleep(1.0)
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


def fsutil_write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    fsutil.replace(tmp, path)


def run_loop(root, cfg, stop=lambda: False, log=None):
    """Поток сервиса скана: до остановки держит соединение (WSClient сам переподключается); при падении цикла ждёт и начинает снова."""
    while not stop():
        r = Runner(root, cfg, stop=stop, log=log)
        try:
            asyncio.run(r.main())
        except Exception as e:  # noqa: BLE001
            if log:
                log("liq: поток упал (%s), повтор через 15 с" % type(e).__name__)
        for _ in range(15):
            if stop():
                return
            time.sleep(1.0)
