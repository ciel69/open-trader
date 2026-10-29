"""Наблюдатель за триггерами плана дня. Читает runtime/market.json (его пишет feed.py), торгует НЕ он.

Одна строка stdout = одно событие (JSON), для Monitor. Тихо, пока триггеров нет.
Триггеры (state/plans/2026-10-27.md), на часовых барах, окно 168 часов:
  short_breakdown : часовое закрытие ниже минимума предыдущих 168 закрытий
  long_reclaim    : внутри часа цена прокололо этот минимум, а часовое закрытие вернулось выше него
Объёмный фильтр из плана НЕ реализован (в market.json нет часового объёма): в событии стоит volume_check="not_implemented".
Событие даёт предложение уровней; решение и проверка guard.py остаются за агентом.
  ./tb watch [--root DIR] [--once] [--poll 5]
"""
import argparse
import calendar
import json
import os
import sys
import time

from tb import paths
from tb.marketdata import data

DEFAULTS = {
    "symbols": ["ETH", "BTC"],
    "lookback_hours": 168,
    "min_history_hours": 72,
    "cooldown_s": 10800,
    "stale_s": 90,
    "max_stop_pct": 3.0,
    "rr": 1.8,
    "blackouts": [],
    "patterns_events": False,  # опционально: события "pattern" (свечные фигуры + уровни, см. patterns.py). ВЫКЛЮЧЕНО; фигуры не доказаны как сигнал
}


def load_config(root):
    cfg = dict(DEFAULTS)
    try:
        with open(paths.config("watch_config.json", root=root), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


class SymState:
    def __init__(self, sym, closes, cfg, bars=None, pcfg=None):
        self.sym, self.cfg = sym, cfg
        self.closes = list(closes)[-(cfg["lookback_hours"] + 24):]
        self.cur_h = None
        self.hi = self.lo = self.last = self.op = None
        # pcfg is None (по умолчанию) -> фигуры не считаются, поведение прежнее
        self.pcfg, self.bars = pcfg, list(bars or [])

    def _start(self, h, price):
        self.cur_h, self.hi, self.lo, self.last, self.op = h, price, price, price, price

    def update(self, ts, price):
        h = int(ts // 3600)
        events = []
        if self.cur_h is None:
            self._start(h, price)
        elif h > self.cur_h:
            events = self._finalize()
            self._start(h, price)
        else:
            self.hi, self.lo, self.last = max(self.hi, price), min(self.lo, price), price
        return events

    def _finalize(self):
        close, hi, lo = self.last, self.hi, self.lo
        prev = self.closes[-self.cfg["lookback_hours"]:]
        out = []
        if len(prev) >= self.cfg["min_history_hours"]:
            low7 = min(prev)
            rr, cap = self.cfg["rr"], self.cfg["max_stop_pct"]
            if close < low7:
                stop = hi * 1.001
                out.append(self._event("short_breakdown", "short", close, stop, rr, cap, low7, hi, lo))
            elif lo < low7 and close > low7:
                stop = lo * 0.999
                out.append(self._event("long_reclaim", "long", close, stop, rr, cap, low7, hi, lo))
        if self.pcfg is not None:
            out += self._pattern_events(close, hi, lo)
        self.closes.append(close)
        return out

    def _pattern_events(self, close, hi, lo):
        """Событие "pattern": свеча по направлению + счёт подтверждений >= watch_min_score. Объёма в фиде нет (v=0), уровни по непрерывному хвосту баров."""
        try:
            from tb.marketdata import patterns as pt
            self.bars.append((self.cur_h * 3600.0, self.op, hi, lo, close, 0.0))
            del self.bars[:-700]
            run = [self.bars[-1]]
            for b in reversed(self.bars[:-1]):
                if run[0][0] - b[0] != 3600:
                    break
                run.insert(0, b)
            if len(run) < 120:
                return []
            an = pt.Analyzer(run, self.pcfg["cfg"])
            i = len(run) - 1
            conf = an.confluence(i)
            side = max(("long", "short"), key=lambda k: conf[k]["score"])
            c = conf[side]
            if c["score"] < self.pcfg["min_score"] or not any(r.startswith("свеча") for r in c["reasons"]):
                return []
            a = an.atr(i)
            sign = 1 if side == "long" else -1
            stop = close - sign * 1.5 * a
            return [{"type": "pattern", "sym": self.sym, "side": side, "score": c["score"], "reasons": c["reasons"],
                     "bar_close": round(close, 6), "suggest_stop": round(stop, 6),
                     "suggest_tp": round(close + sign * self.cfg["rr"] * abs(close - stop), 6),
                     "stop_pct": round(abs(close - stop) / close * 100.0, 3), "volume_check": "not_available",
                     "unvalidated": True, "note": "фигуры не прошли проверку на истории (docs/RESEARCH.md): только подтверждение"}]
        except Exception:
            return []

    def _event(self, kind, side, entry, stop, rr, cap, low7, hi, lo):
        dist = abs(stop - entry)
        stop_pct = dist / entry * 100.0
        sign = -1 if side == "short" else 1
        d = {"type": kind, "sym": self.sym, "side": side, "bar_close": round(entry, 6),
             "low7d_close": round(low7, 6), "bar_high": round(hi, 6), "bar_low": round(lo, 6),
             "suggest_stop": round(stop, 6), "suggest_tp": round(entry + sign * rr * dist, 6),
             "stop_pct": round(stop_pct, 3), "volume_check": "not_implemented"}
        if stop_pct > cap:
            d["note"] = "stop_too_far (%.2f%% > %.1f%%): по guard.py не пройдёт без сдвига стопа" % (stop_pct, cap)
        return d


def warm_closes(root, sym):
    path = paths.data("ticks_%s_hist_1h.csv" % sym, root=root)
    try:
        return [p for _, p in data.load_ticks(path)]
    except Exception:
        return []


class Watcher:
    def __init__(self, root, cfg, out=sys.stdout):
        self.root, self.cfg, self.out = root, cfg, out
        self.states = {s: SymState(s, warm_closes(root, s), cfg, **self._pattern_kw(root, cfg, s)) for s in cfg["symbols"]}
        self.last_emit, self.stale_flag = {}, False
        self.log_path = paths.runtime("watch_events.jsonl", root=root)

    @staticmethod
    def _pattern_kw(root, cfg, sym):
        if not cfg.get("patterns_events"):
            return {}
        from tb.marketdata import patterns_scan
        dcfg, min_score = patterns_scan.load_cfg(root)
        return {"bars": patterns_scan.load_bars(sym, "1h", root)[-700:], "pcfg": {"cfg": dcfg, "min_score": min_score}}

    def emit(self, ev, now):
        key = (ev.get("sym"), ev["type"])
        if ev["type"] not in ("feed_stale", "feed_recovered") and \
                now - self.last_emit.get(key, 0) < self.cfg["cooldown_s"]:
            return
        for b in self.cfg["blackouts"]:
            if parse_iso(b["from"]) <= now <= parse_iso(b["to"]):
                ev = dict(ev, suppressed="blackout: %s" % b.get("name", ""))
                self._log(ev, now)
                return
        self.last_emit[key] = now
        ev = dict(ev, ts=iso(now))
        self._log(ev, now)
        self.out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.out.flush()

    def _log(self, ev, now):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(ev, ts=iso(now)), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def step(self, now=None):
        now = time.time() if now is None else now
        try:
            with open(paths.runtime("market.json", root=self.root), encoding="utf-8") as f:
                m = json.load(f)
            age = now - float(m.get("ts_epoch") or 0)
        except (OSError, ValueError):
            m, age = None, 1e9
        if m is None or age > self.cfg["stale_s"] or m.get("feed_ok") is not True:
            if not self.stale_flag:
                self.stale_flag = True
                self.emit({"type": "feed_stale", "age_s": round(min(age, 1e6), 1)}, now)
            return
        if self.stale_flag:
            self.stale_flag = False
            self.emit({"type": "feed_recovered"}, now)
        for sym, st in self.states.items():
            p = ((m.get("symbols") or {}).get(sym) or {}).get("price")
            if isinstance(p, (int, float)) and p > 0:
                for ev in st.update(float(m["ts_epoch"]), float(p)):
                    self.emit(ev, now)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=paths.root())
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    w = Watcher(a.root, load_config(a.root))
    if a.once:
        w.step()
        return
    while True:
        try:
            w.step()
        except Exception as e:  # наблюдатель не должен падать молча
            w.emit({"type": "watch_error", "error": "%s: %s" % (type(e).__name__, e)}, time.time())
        time.sleep(a.poll)


if __name__ == "__main__":
    main(sys.argv[1:])
