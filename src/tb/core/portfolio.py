"""Портфель: капитал по площадкам, высшая отметка, просадка, лестница риска, множитель по результатам журнала.

  portfolio.py set --venue platform --equity 30.3 --free 20.3      обновить снимок площадки (из balance_get и позиций)
  portfolio.py deposit --venue bybit --amount 10                   пополнение: поднимает высшую отметку, не считается прибылью
  portfolio.py show
Файл state/portfolio.json. Риск на сделку считается от ОБЩЕГО капитала, маржа и резерв от капитала площадки.
"""
import argparse
import json
import os
import sys
import time

from tb import paths
from tb.core import journal
from tb import fsutil

PATH = paths.state("portfolio.json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"venues": {}, "hwm": 0.0}


def save(p):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    fsutil.replace(tmp, PATH)


def total(p):
    return sum(v.get("equity", 0.0) for v in p["venues"].values())


def set_venue(venue, equity, free, p=None):
    p = p or load()
    p["venues"][venue] = {"equity": float(equity), "free": float(free), "ts": _now()}
    p["hwm"] = max(p.get("hwm", 0.0), total(p))
    save(p)
    return p


def deposit(venue, amount, p=None):
    p = p or load()
    v = p["venues"].setdefault(venue, {"equity": 0.0, "free": 0.0, "ts": _now()})
    v["equity"] += float(amount)
    v["free"] += float(amount)
    p["hwm"] = p.get("hwm", 0.0) + float(amount)
    save(p)
    return p


def drawdown(p):
    h = p.get("hwm", 0.0)
    return 0.0 if h <= 0 else max(0.0, 1.0 - total(p) / h)


def dd_mult(p, prof):
    """Лестница: чем глубже просадка от максимума, тем меньше риск. Глубже drawdown_halt = 0 (стоп до разбора)."""
    dd = drawdown(p)
    if dd >= prof["drawdown_halt"]:
        return 0.0
    mult = 1.0
    for thr, m in prof["drawdown_ladder"]:
        if dd >= thr:
            mult = m
    return mult


def perf_mult(stype, prof, trades=None):
    """Множитель размера по реальным live-сделкам этого типа сессии: пока мало сделок, риск урезан («испытательный срок»)."""
    if trades is None:
        tr, order = journal._load()
        trades = [tr[i] for i in order]
    rows = [t for t in trades if t.get("mode") == "live" and t.get("stype") == stype and t.get("ts_close")]
    pm = prof["perf_mult"]
    if len(rows) < prof["probation_trades"]:
        return pm["probation"], len(rows)
    win = sum(t["pnl_usdt"] for t in rows if t["pnl_usdt"] > 0)
    loss = -sum(t["pnl_usdt"] for t in rows if t["pnl_usdt"] <= 0)
    pf = win / loss if loss > 0 else float("inf")
    rs = [t["r"] for t in rows if t.get("r") is not None]
    avg_r = sum(rs) / len(rs) if rs else 0.0
    if avg_r > 0 and pf >= 1.2:
        return pm["good"], len(rows)
    if avg_r > 0:
        return pm["ok"], len(rows)
    return pm["bad"], len(rows)


def scaled_profile(prof, p=None, venue="platform"):
    """Абсолютные лимиты от долей текущего капитала: растут и падают вместе со счётом."""
    p = p or load()
    eq_total = total(p)
    eq_v = p["venues"].get(venue, {}).get("equity", 0.0)
    q = dict(prof)
    if eq_total > 0:
        q["daily_loss_limit_usdt"] = round(prof["daily_loss_frac"] * eq_total, 4)
    if eq_v > 0:
        q["margin_per_trade_max"] = round(prof["margin_per_trade_frac"] * eq_v, 4)
        q["min_free_reserve"] = round(prof["reserve_frac"] * eq_v, 4)
    return q


def main(argv):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("set")
    s.add_argument("--venue", required=True)
    s.add_argument("--equity", type=float, required=True)
    s.add_argument("--free", type=float, required=True)
    d = sp.add_parser("deposit")
    d.add_argument("--venue", required=True)
    d.add_argument("--amount", type=float, required=True)
    sp.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "set":
        set_venue(a.venue, a.equity, a.free)
    elif a.cmd == "deposit":
        deposit(a.venue, a.amount)
    p = load()
    prof = json.load(open(paths.config("risk_profile.json"), encoding="utf-8"))
    print("Капитал: %.2f (высшая отметка %.2f, просадка %.1f%%, множитель лестницы %.2f)" % (
        total(p), p.get("hwm", 0.0), 100 * drawdown(p), dd_mult(p, prof)))
    for v, x in p["venues"].items():
        print("  %s: equity %.2f, свободно %.2f (%s)" % (v, x["equity"], x["free"], x["ts"]))
    for t in prof["session_types"]:
        m, n = perf_mult(t, prof)
        print("  тип %-8s: множитель по результатам %.2f (закрытых live-сделок %d из %d для оценки)" % (t, m, n, prof["probation_trades"]))


if __name__ == "__main__":
    main(sys.argv[1:])
