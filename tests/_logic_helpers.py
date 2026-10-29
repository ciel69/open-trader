"""Общие помощники тестов logic/* (WP3): синтетические свечи, снимок рынка, план и runtime."""
import copy

import _bootstrap  # noqa: F401
from tb.logic.candles import CandleBook, MarketView

T0 = 1700000100          # секунды, кратно 300 (5м) и 60
MS = 1000


def row(start_ms, o, h, l, c, vol=10.0):
    return (int(start_ms), float(o), float(h), float(l), float(c), float(vol), float(vol) * float(c))


def close_candle(book, tf, start_s, o, h, l, c, vol=10.0):
    """Подтверждённая свеча, начавшаяся в start_s (секунды)."""
    return book.on_kline(tf, row(start_s * MS, o, h, l, c, vol), True)


def view(last, book=None, now=T0, ts=None, bid=None, ask=None, sym="SOL"):
    ok = isinstance(last, (int, float)) and last == last
    bid = (last - 0.01 if ok else None) if bid is None else bid
    ask = (last + 0.01 if ok else None) if ask is None else ask
    return MarketView(sym, last, bid, ask, last, now if ts is None else ts, book if book is not None else CandleBook(), now)


def long_plan(**kw):
    p = {"schema": "tb.plan/1", "symbol": "SOL", "side": "long",
         "entry": {"how": {"type": "market"}, "ref_price": 100.0, "valid": {"ttl_min": 30}},
         "stop": {"price": 99.0}, "targets": [{"id": "t1", "price": 103.0, "frac": 0.5}, {"id": "t2", "price": 105.0, "frac": 0.5}],
         "manage": [], "invalidation": [], "lifecycle": {}}
    p.update(kw)
    return p


def short_plan(**kw):
    p = {"schema": "tb.plan/1", "symbol": "SOL", "side": "short",
         "entry": {"how": {"type": "market"}, "ref_price": 100.0, "valid": {"ttl_min": 30}},
         "stop": {"price": 101.0}, "targets": [{"id": "t1", "price": 97.0, "frac": 1.0}],
         "manage": [], "invalidation": [], "lifecycle": {}}
    p.update(kw)
    return p


def managing_rt(side="long", avg=100.0, stop=None, r=1.0, qty=1.0, entered=T0 - 600, **kw):
    s = 1 if side == "long" else -1
    stop = avg - s * r if stop is None else stop
    rt = {"status": "managing", "hold": None, "ts": {"armed": T0 - 1200, "entered": entered},
          "entry": {"state": "filled"},
          "pos": {"qty": qty, "avg": avg, "initial_stop": avg - s * r, "cur_stop": stop, "r_unit": r, "best_px": avg,
                  "entries": [{"px": avg, "qty": qty, "fee": 0.0009 * avg * qty, "kind": "entry"}]},
          "targets": {"t1": {"state": "resting", "qty": 0.5, "price": avg + s * 3 * r, "filled_qty": 0.0}},
          "rules": {}, "adds": {"n": 0, "risk_used": 0.0}, "realized": {"pnl": 0.0, "fees": 0.0009 * avg * qty, "parts": []},
          "actions": {"seq": 1, "pending": []}, "counters": {}}
    rt.update(kw)
    return rt


def frozen(obj):
    return copy.deepcopy(obj)
