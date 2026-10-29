"""Учебный режим: небольшие реальные сделки на одной площадке ради статистики, с жёсткими лимитами, которые держит guard.

  ./tb learning start --max-trades 30 --max-loss 1.5 [--venue bybit] [--note "..."]
  ./tb learning status | stop
Лимиты считаются по журналу (mainnet, mode=live) с момента старта: число открытых сделок и результат (закрытые) минус риск открытых до стопа.
Когда сделок набрано max_trades или остаток убытка (max_loss + результат - риск открытых) <= 0, guard отказывает во входе и пишет причину.
Остаток убытка также ограничивает размер новой сделки. Файл state/learning.json.
"""
import argparse
import json
import os
import sys
import time

from tb import paths
from tb.core import journal
from tb.core import pending
from tb import fsutil

PATH = paths.state("learning.json")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(cfg):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    fsutil.replace(tmp, PATH)


def start(max_trades, max_loss, venue="bybit", note="", now=None):
    cur = load()
    if cur and cur.get("active"):
        raise SystemExit("учебный режим уже идёт (stop, чтобы начать заново)")
    now = time.time() if now is None else now
    cfg = {"active": True, "start": _iso(now), "max_trades": int(max_trades), "max_loss_usdt": float(max_loss),
           "venue": venue, "note": note}
    save(cfg)
    return cfg


def progress(cfg=None):
    """None, если режим не активен. Иначе счётчики и остаток убытка."""
    cfg = cfg if cfg is not None else load()
    if not cfg or not cfg.get("active"):
        return None
    tr, order = journal._load()
    rows = [tr[i] for i in order if tr[i].get("venue") == cfg["venue"] and tr[i].get("mode") == "live"
            and (tr[i].get("ts_open") or "") >= cfg["start"]]
    closed = [t for t in rows if t.get("ts_close")]
    opened = [t for t in rows if not t.get("ts_close")]
    realized = sum(t["pnl_usdt"] for t in closed)
    pend = pending.orders(mode="live") if cfg["venue"] == "bybit" else []   # ждущие лимитные входы Bybit: риск и счётчик сделок учитываются заранее
    open_risk = sum(t.get("risk_usdt") or 0.0 for t in opened) + pending.risk(pend)
    room = cfg["max_loss_usdt"] + realized - open_risk
    reasons = []
    if len(rows) + len(pend) >= cfg["max_trades"]:
        reasons.append("learning_mode: сделок %d из %d" % (len(rows), cfg["max_trades"]))
    if room <= 0:
        reasons.append("learning_mode: лимит убытка %.2f исчерпан (результат %+.3f, риск открытых %.3f)" % (cfg["max_loss_usdt"], realized, open_risk))
    return {"venue": cfg["venue"], "trades": len(rows), "closed": len(closed), "open": len(opened), "max_trades": cfg["max_trades"],
            "realized": round(realized, 4), "open_risk": round(open_risk, 4), "max_loss": cfg["max_loss_usdt"],
            "room": round(room, 4), "reasons": reasons, "wins": sum(1 for t in closed if t["pnl_usdt"] > 0)}


def main(argv):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("start")
    s.add_argument("--max-trades", type=int, default=30)
    s.add_argument("--max-loss", type=float, default=1.5)
    s.add_argument("--venue", default="bybit")
    s.add_argument("--note", default="")
    sp.add_parser("status")
    sp.add_parser("stop")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        cfg = start(a.max_trades, a.max_loss, a.venue, a.note)
        print("учебный режим начат %s: до %d сделок и потерь %.2f USDT на %s" % (cfg["start"], cfg["max_trades"], cfg["max_loss_usdt"], cfg["venue"]))
    elif a.cmd == "stop":
        cfg = load()
        if not cfg or not cfg.get("active"):
            raise SystemExit("учебный режим не активен")
        cfg["active"], cfg["stopped"] = False, _iso(time.time())
        save(cfg)
        print("учебный режим остановлен", cfg["stopped"])
    else:
        p = progress()
        print("не активен" if p is None else json.dumps(p, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
