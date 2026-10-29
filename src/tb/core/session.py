"""Торговые сессии разной длины: от минут до месяцев. У каждой свой бюджет убытка и лимит сделок; срока нет, сессия идёт до stop. Без активной сессии входов нет.

  session.py start --type day|scalp|intraday|swing|position --thesis "..." [--venue platform] [--budget USDT]
  session.py status | stop [--note "..."]
Типы и лимиты — config/risk_profile.json:session_types. Бюджет = budget_frac x общий капитал на старте.
Тип `day` (торговый день, его запускает дашборд): сессия только контейнер дня (бюджет убытка, лимит сделок как у intraday, либо
risk_profile.day_session), а горизонт каждой сделки (scalp/intraday/swing/position: стоп, RR, плечо, срок удержания) выбирает агент в плане
(`session_type` плана); guard берёт геометрию по горизонту плана (`cfg_of`, `geometry_type`). Сессия типа-горизонта (CLI) по-прежнему
задаёт один горизонт на все входы.
Входы в сессии закрываются сами, если убыток по сессии достиг бюджета или исчерпан лимит сделок: guard.py перестаёт их разрешать.
Файл state/sessions.jsonl (только дописывание).
"""
import argparse
import json
import os
import sys
import time

from tb import paths
from tb.core import journal
from tb.core import pending
from tb.core import portfolio
from tb.core import risklevels

PATH = paths.state("sessions.jsonl")


def _now():
    return time.time()


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _append(rec):
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_all():
    sess, order = {}, []
    if not os.path.exists(PATH):
        return sess, order
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["ev"] == "start":
                sess[r["id"]] = dict(r)
                order.append(r["id"])
            elif r["id"] in sess:
                if r["ev"] == "extend":
                    sess[r["id"]]["expires"] = r["expires"]
                    sess[r["id"]].setdefault("extensions", []).append(r.get("reason", ""))
                elif r["ev"] == "stop":
                    sess[r["id"]]["stopped"] = r["ts"]
                    sess[r["id"]]["stop_note"] = r.get("note", "")
    return sess, order


def active():
    sess, order = load_all()
    for i in reversed(order):
        if not sess[i].get("stopped"):
            return sess[i]
    return None


DAY = "day"
HORIZONS = ("scalp", "intraday", "swing", "position")
DAY_KEYS = ("max_minutes", "budget_frac", "max_trades")
DEFAULT_HORIZON = "intraday"


def known_type(stype, prof):
    return stype == DAY or stype in (prof.get("session_types") or {})


def cfg_of(stype, prof):
    """Лимиты сессии по её типу: у горизонта из session_types; у дня (`DAY`) бюджет/лимит сделок/срок как у intraday, поверх них risk_profile.day_session."""
    types = prof.get("session_types") or {}
    if stype != DAY:
        return types[stype]
    base = types.get(DEFAULT_HORIZON) or {}
    c = dict((k, base.get(k)) for k in DAY_KEYS)
    over = prof.get("day_session") if isinstance(prof.get("day_session"), dict) else {}
    c.update(dict((k, v) for k, v in over.items() if k in DAY_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)))
    return c


def geometry_type(sess, stype=None):
    """Горизонт, по которому guard берёт геометрию сделки (max_stop_pct, min_rr, плечо): у сессии-дня это горизонт плана (нет плана: intraday),
    у сессии-горизонта её собственный тип."""
    if sess and sess.get("type") == DAY:
        return stype if stype in HORIZONS else DEFAULT_HORIZON
    return (sess or {}).get("type") or (stype if stype in HORIZONS else DEFAULT_HORIZON)


def stats(s):
    """Сделки сессии: закрытые, открытые, реализованный PnL, риск открытых. Ждущие лимиты сессии (pending_orders.json) входят в риск открытых."""
    tr, order = journal._load()
    rows = [tr[i] for i in order if tr[i].get("session") == s["id"]]
    closed = [t for t in rows if t.get("ts_close")]
    opened = [t for t in rows if not t.get("ts_close")]
    pend = [r for r in pending.orders() if r.get("session") == s["id"]]
    return {"trades": len(rows), "realized": round(sum(t["pnl_usdt"] for t in closed), 4),
            "open_risk": round(sum(t.get("risk_usdt") or 0.0 for t in opened) + pending.risk(pend), 4), "open": len(opened),
            "pending": len(pend)}


def verdict(s, prof, now=None):
    """-> (можно_открывать, причины). Закрывать позиции сессия не заставляет: стопы стоят на бирже."""
    now = _now() if now is None else now
    why = []
    st = stats(s)
    cfg = cfg_of(s["type"], prof)
    if s.get("stopped"):
        why.append("session_stopped")
    if st["realized"] - st["open_risk"] <= -s["budget_usdt"]:
        why.append("session_budget: realized %.3f, open risk %.3f, budget %.3f" % (st["realized"], st["open_risk"], s["budget_usdt"]))
    if st["trades"] + st["pending"] >= cfg["max_trades"]:
        why.append("session_max_trades")
    return not why, why


def start(stype, thesis, venue, minutes, budget, prof, p=None, now=None):
    """Старт сессии. Срока у сессии нет: она идёт до stop (аргумент minutes оставлен для совместимости вызовов и не используется)."""
    if not known_type(stype, prof):
        raise SystemExit("неизвестный тип сессии: %s" % stype)
    cur = active()
    if cur:
        raise SystemExit("уже идёт сессия %s (%s): сначала stop" % (cur["id"], cur["type"]))
    p = p or portfolio.load()
    now = _now() if now is None else now
    cfg = cfg_of(stype, prof)
    eq = portfolio.total(p)
    rec = {"ev": "start", "id": "%s-%s" % (stype, time.strftime("%m%d-%H%M", time.gmtime(now))), "type": stype,
           "venue": venue, "thesis": thesis, "ts": _iso(now), "start": now,
           "budget_usdt": round(budget if budget else cfg["budget_frac"] * eq, 4), "equity_at_start": round(eq, 4)}
    _append(rec)
    return rec


def main(argv):
    prof = risklevels.effective(json.load(open(paths.config("risk_profile.json"), encoding="utf-8")))     # WP16: бюджет сессии по уровню риска
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("start")
    s.add_argument("--type", required=True)
    s.add_argument("--thesis", required=True)
    s.add_argument("--venue", default="platform")
    s.add_argument("--budget", type=float)
    t = sp.add_parser("stop")
    t.add_argument("--note", default="")
    sp.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        r = start(a.type, a.thesis, a.venue, None, a.budget, prof)
        print("начата %s, бюджет убытка %.3f USDT" % (r["id"], r["budget_usdt"]))
        return
    cur = active()
    if a.cmd == "status":
        if not cur:
            print("активной сессии нет")
            return
        ok, why = verdict(cur, prof)
        st = stats(cur)
        print("%s [%s] | сделок %d, PnL %+.3f, риск открытых %.3f, бюджет %.3f | входы %s %s" % (
            cur["id"], cur["venue"], st["trades"], st["realized"], st["open_risk"],
            cur["budget_usdt"], "РАЗРЕШЕНЫ" if ok else "ЗАПРЕЩЕНЫ", "; ".join(why)))
        return
    if not cur:
        raise SystemExit("активной сессии нет")
    if a.cmd == "stop":
        st = stats(cur)
        _append({"ev": "stop", "id": cur["id"], "ts": _iso(_now()), "note": a.note})
        print("остановлена %s: сделок %d, PnL %+.3f USDT (открытых позиций: %d, стопы на бирже остаются)" % (
            cur["id"], st["trades"], st["realized"], st["open"]))


if __name__ == "__main__":
    main(sys.argv[1:])
