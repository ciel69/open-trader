"""Paper-форвард-тест: пробой часового канала 24 ч (правила из research_hourly: donchian 24, обе стороны) на SOL, HYPE, ZEC.

Без реальных ордеров. Цены берёт из runtime/market.json (marketdata/feed.py, OKX/Bybit). Сделки пишет в журнал (mode=paper, теги paper,donchian24,bybit-costs).
Правила (как в research_hourly.positions, проверено тестом на эквивалентность):
  вход: часовое закрытие выше максимума предыдущих 24 закрытий -> лонг; ниже минимума -> шорт;
  выход: лонг, если закрытие ниже минимума предыдущих 12 закрытий; шорт, если выше максимума предыдущих 12.
Издержки Bybit: taker 0.055% на сторону + проскальзывание 0.02% на сторону (заложено в цену входа/выхода). Funding не учитывается (удержание
порядка суток, ориентировочно 0.03%/сутки: мало; при разборе учесть). Условный номинал 100 USDT без плеча.
Состояние: runtime/paper_state.json, перезапуск безопасен. Разгон из data/ticks_<SYM>_hist_1h.csv.
  ./tb paper [--root DIR] [--poll 5]
"""
import argparse
import json
import os
import sys
import time

from tb import paths
from tb.marketdata import data
from tb.core import journal
from tb import fsutil

SYMS = ("SOL", "HYPE", "ZEC")
LOOK, HALF = 24, 12
FEE, SLIP = 0.00055, 0.0002
NOTIONAL = 100.0
TAGS = "paper,donchian24,bybit-costs"


def decide(closes, pos, close):
    """Чистая функция: closes — предыдущие закрытия (без текущего). -> новая позиция 0/1/-1."""
    if len(closes) < LOOK:
        return pos
    hi, lo = max(closes[-LOOK:]), min(closes[-LOOK:])
    if pos == 0:
        if close > hi:
            return 1
        if close < lo:
            return -1
        return 0
    if pos == 1 and close < min(closes[-HALF:]):
        return 0
    if pos == -1 and close > max(closes[-HALF:]):
        return 0
    return pos


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Runner:
    def __init__(self, root, out=sys.stdout):
        self.root, self.out = root, out
        self.path = paths.runtime("paper_state.json", root=root)
        self.st = self._load()
        for s in SYMS:
            if s not in self.st:
                try:
                    closes = [p for _, p in data.load_ticks(paths.data("ticks_%s_hist_1h.csv" % s, root=root))]
                except Exception:
                    closes = []
                self.st[s] = {"closes": closes[-(LOOK + 24):], "cur_h": None, "last": None, "pos": 0, "id": None}
        self.save()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f)
        fsutil.replace(tmp, self.path)

    def say(self, msg):
        self.out.write(msg + "\n")
        self.out.flush()

    def on_price(self, sym, ts, price):
        s = self.st[sym]
        h = int(ts // 3600)
        if s["cur_h"] is None:
            s["cur_h"], s["last"] = h, price
            return
        if h == s["cur_h"]:
            s["last"] = price
            return
        close, bar_ts = s["last"], s["cur_h"] * 3600
        new = decide(s["closes"], s["pos"], close)
        if new != s["pos"]:
            self._transition(sym, s, new, close, bar_ts)
        s["closes"] = (s["closes"] + [close])[-(LOOK + 24):]
        s["cur_h"], s["last"] = h, price

    def _transition(self, sym, s, new, close, bar_ts):
        import argparse as ap
        if s["pos"] != 0:
            sign = 1 if s["pos"] == 1 else -1
            exit_eff = close * (1 - SLIP * sign)
            journal.cmd_close(ap.Namespace(id=s["id"], exit=exit_eff, reason="channel-exit", funding=0.0, fee=FEE,
                                           note="paper %s %s" % (sym, iso(bar_ts))))
            self.say("%s paper CLOSE %s по %.6g" % (iso(bar_ts), sym, exit_eff))
            s["id"] = None
        if new != 0:
            sign = 1 if new == 1 else -1
            entry_eff = close * (1 + SLIP * sign)
            prev = s["closes"][-LOOK:]
            stop = min(prev) if new == 1 else max(prev)
            tid = "P-DON24-%s-%s" % (sym, time.strftime("%m%d-%H%M", time.gmtime(bar_ts)))
            journal.cmd_open(ap.Namespace(
                sym=sym, side="long" if new == 1 else "short", entry=entry_eff, lev=1.0, margin=NOTIONAL,
                sl=stop, tp=None, thesis="Paper-форвард: пробой 24ч канала (часовое закрытие %.6g), издержки Bybit, funding не учтён" % close,
                news="", regime="unknown", tags=TAGS, mode="paper", id=tid))
            s["id"] = tid
            self.say("%s paper OPEN %s %s по %.6g" % (iso(bar_ts), sym, "long" if new == 1 else "short", entry_eff))
        s["pos"] = new

    def step(self, now=None):
        now = time.time() if now is None else now
        try:
            with open(paths.runtime("market.json", root=self.root), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            return
        if m.get("feed_ok") is not True or now - float(m.get("ts_epoch") or 0) > 90:
            return
        for sym in SYMS:
            p = ((m.get("symbols") or {}).get(sym) or {}).get("price")
            if isinstance(p, (int, float)) and p > 0:
                self.on_price(sym, float(m["ts_epoch"]), float(p))
        self.save()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=paths.root())
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    r = Runner(a.root)
    while True:
        try:
            r.step()
        except Exception as e:  # раннер не должен падать молча
            r.say("%s ERROR %s: %s" % (iso(time.time()), type(e).__name__, e))
        time.sleep(a.poll)


if __name__ == "__main__":
    main(sys.argv[1:])
