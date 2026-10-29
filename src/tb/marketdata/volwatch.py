"""Оповещение о всплеске волатильности (информационное, НЕ сигнал входа). Читает runtime/market.json (его пишет feed.py).

Зачем: скальп при наших издержках (круг 0.24-0.28%) имеет смысл только когда за минуты цена проходит около 1% и больше. Тихий рынок такого не даёт.
Событие говорит «проверь рынок», решение о сделке и проверка guard остаются за агентом.
Правило (правила заданы заранее, значения в DEFAULTS, можно переопределить config/volwatch_config.json):
  диапазон последних 5 минут (макс-мин)/цена >= floor_pct И >= ratio_min x медиана таких диапазонов за последние 2 часа (если накоплено >= 15 замеров).
  Антиспам: не чаще одного события на символ за cooldown_s. Поле scalp_tp_feasible = диапазон >= tp_min_pct (минимальный тейк скальпа после издержек ~1%).
Одна строка stdout = одно событие JSON (для Monitor). Лог: runtime/volwatch_events.jsonl.
  ./tb volwatch [--once] [--poll 5]
"""
import argparse
import json
import os
import statistics
import sys
import time
from collections import deque

from tb import paths

DEFAULTS = {"symbols": ["ETH", "BTC", "SOL"], "window_s": 300, "base_s": 7200, "floor_pct": 0.6, "ratio_min": 2.5,
            "tp_min_pct": 1.0, "cooldown_s": 1200, "min_samples": 15, "stale_s": 90}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(paths.config("volwatch_config.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SymWatch:
    def __init__(self, sym, cfg):
        self.sym, self.cfg = sym, cfg
        self.buf = deque()          # (ts, price) за base_s
        self.ranges = deque()       # (ts, диапазон 5 мин в %), по одному замеру в минуту
        self.last_sample = 0.0
        self.last_event = -1e18

    def update(self, ts, price):
        c = self.cfg
        self.buf.append((ts, price))
        while self.buf and ts - self.buf[0][0] > c["base_s"]:
            self.buf.popleft()
        win = [(t, p) for t, p in self.buf if ts - t <= c["window_s"]]
        if len(win) < 2 or ts - win[0][0] < 0.8 * c["window_s"]:
            return None                                     # окно ещё не накоплено
        prices = [p for _, p in win]
        rng = 100.0 * (max(prices) - min(prices)) / price
        base = statistics.median(r for _, r in self.ranges) if len(self.ranges) >= c["min_samples"] else None
        ev = None
        if rng >= c["floor_pct"] and (base is None or rng >= c["ratio_min"] * base) and ts - self.last_event >= c["cooldown_s"]:
            ret = 100.0 * (price / win[0][1] - 1.0)
            ev = {"type": "vol_spike", "sym": self.sym, "price": price, "range_5m_pct": round(rng, 3), "ret_5m_pct": round(ret, 3),
                  "direction": "up" if ret > 0 else "down", "baseline_range_pct": round(base, 3) if base is not None else None,
                  "ratio": round(rng / base, 2) if base else None, "scalp_tp_feasible": rng >= c["tp_min_pct"],
                  "note": "информационно: не сигнал входа; проверь guard и тезис"}
            self.last_event = ts
        if ts - self.last_sample >= 60:                      # замер в базу после проверки: всплеск не завышает собственную базу
            self.ranges.append((ts, rng))
            self.last_sample = ts
            while self.ranges and ts - self.ranges[0][0] > c["base_s"]:
                self.ranges.popleft()
        return ev


class VolWatcher:
    def __init__(self, cfg=None, out=sys.stdout, log_path=None):
        self.cfg = cfg or load_config()
        self.out = out
        self.log_path = log_path if log_path is not None else paths.runtime("volwatch_events.jsonl")
        self.w = {s: SymWatch(s, self.cfg) for s in self.cfg["symbols"]}
        self.stale = False

    def emit(self, ev, now):
        ev = dict(ev, ts=iso(now))
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self.out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.out.flush()

    def step(self, market, now):
        """market: разобранный runtime/market.json или None."""
        age = now - float((market or {}).get("ts_epoch") or 0)
        if market is None or market.get("feed_ok") is not True or age > self.cfg["stale_s"]:
            if not self.stale:
                self.stale = True
                self.emit({"type": "feed_stale", "age_s": round(min(age, 1e6), 1)}, now)
            return
        if self.stale:
            self.stale = False
            self.emit({"type": "feed_recovered"}, now)
        for sym, w in self.w.items():
            p = ((market.get("symbols") or {}).get(sym) or {}).get("price")
            if isinstance(p, (int, float)) and p > 0:
                ev = w.update(float(market["ts_epoch"]), float(p))
                if ev:
                    self.emit(ev, now)


def read_market():
    try:
        with open(paths.runtime("market.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    vw = VolWatcher()
    while True:
        try:
            vw.step(read_market(), time.time())
        except Exception as e:
            vw.emit({"type": "volwatch_error", "error": "%s: %s" % (type(e).__name__, e)}, time.time())
        if a.once:
            return 0
        time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
