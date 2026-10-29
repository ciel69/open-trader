"""Симулятор на минутных тиках. Единицы: комиссия/проскальзывание/funding в config — ДОЛИ; PnL в USDT.
Ограничения: стоп/TP проверяются только по ценам тиков (внутриминутные экстремумы не видны);
вход по цене следующего тика после закрытия бара; ликвидация — грубая оценка."""
import argparse
import copy
import json
import math
import os
import sys

from tb import paths
from tb.marketdata import data
from tb.legacy_engine import signals

MAINT_MARGIN_PCT = 1.2
WINDOW_BARS = 200


def load_config(path=None):
    with open(path or paths.config("config.json"), encoding="utf-8") as f:
        return json.load(f)


def run_backtest(ticks, cfg, equity0=30.0, funding_hourly=None):
    c, r = cfg["costs"], cfg["risk"]
    fee, slip = c["taker_fee_pct"], c["slippage_buffer_pct"]
    fr = c["funding_hourly_assumed"] if funding_hourly is None else funding_hourly
    lev, margin = r["leverage"], r["margin_per_trade"]
    notional = lev * margin
    liq_move = max(0.0, 1.0 / lev - MAINT_MARGIN_PCT / 100.0)
    bar_s = cfg["bar_seconds"]
    cooldown = r["cooldown_after_stop_minutes"] * 60.0
    max_hold = r["max_hold_minutes"] * 60.0

    bars = []
    cur = None
    pos = None
    pending = None
    last_stop_ts = None
    trades = []
    day_pnl = {}
    day_cnt = {}
    equity = equity0
    peak = equity0
    max_dd = 0.0

    def close_pos(ts, px_raw, reason):
        nonlocal pos, equity, peak, max_dd, last_stop_ts
        side = pos["side"]
        sgn = 1.0 if side == "long" else -1.0
        exit_px = px_raw * (1.0 - sgn * slip)  # проскальзывание против нас
        gross = sgn * (px_raw - pos["raw_entry"]) / pos["raw_entry"] * notional
        slip_cost = notional * slip * 2.0
        fee_cost = notional * fee * 2.0
        hours = (ts - pos["ts"]) / 3600.0
        fund_cost = sgn * fr * hours * notional
        costs = slip_cost + fee_cost + fund_cost
        net = gross - costs
        if reason == "liquidation":
            net = -margin
            costs = gross - net  # потеря всей маржи; издержки = остаток до -margin
        trades.append({"side": side, "entry_ts": pos["ts"], "exit_ts": ts, "entry": pos["entry"], "exit": exit_px,
                       "gross": gross, "costs": costs, "pnl": net, "reason": reason, "hold_min": (ts - pos["ts"]) / 60.0})
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        d = int(ts // 86400)
        day_pnl[d] = day_pnl.get(d, 0.0) + net
        if reason in ("stop", "liquidation"):
            last_stop_ts = ts
        pos = None

    for i, (ts, px) in enumerate(ticks):
        # 1) исполнение отложенного входа по цене этого (следующего) тика
        if pending is not None and pos is None:
            side, stop_pct, tp_pct = pending
            sgn = 1.0 if side == "long" else -1.0
            entry = px * (1.0 + sgn * slip)
            pos = {"side": side, "ts": ts, "raw_entry": px, "entry": entry,
                   "stop": px * (1.0 - sgn * stop_pct / 100.0), "tp": px * (1.0 + sgn * tp_pct / 100.0),
                   "liq": px * (1.0 - sgn * liq_move)}
            d = int(ts // 86400)
            day_cnt[d] = day_cnt.get(d, 0) + 1
        pending = None

        # 2) управление позицией (не в тот же тик, что вход)
        if pos is not None and pos["ts"] != ts:
            sgn = 1.0 if pos["side"] == "long" else -1.0
            if sgn * (px - pos["liq"]) <= 0:
                close_pos(ts, pos["liq"], "liquidation")
            elif sgn * (px - pos["stop"]) <= 0:  # стоп приоритетнее TP
                close_pos(ts, px, "stop")
            elif sgn * (px - pos["tp"]) >= 0:
                close_pos(ts, pos["tp"], "tp")
            elif ts - pos["ts"] >= max_hold:
                close_pos(ts, px, "max_hold")

        # 3) бары: закрытие бара -> сигнал
        b = math.floor(ts / bar_s) * bar_s
        if cur is None or b != cur["ts"]:
            if cur is not None:
                bars.append(cur)
                if len(bars) > WINDOW_BARS:
                    del bars[0]
                if pos is None and i + 1 < len(ticks):
                    d = int(ts // 86400)
                    ok = True
                    if last_stop_ts is not None and ts - last_stop_ts < cooldown:
                        ok = False
                    if day_cnt.get(d, 0) >= r["max_trades_per_day"]:
                        ok = False
                    if day_pnl.get(d, 0.0) <= -r["daily_loss_limit_usdt"]:
                        ok = False
                    if ok:
                        sig = signals.compute_signal(bars, fr, cfg)
                        if sig["action"] in ("long", "short"):
                            pending = (sig["action"], sig["stop_pct"], sig["tp_pct"])
            cur = {"ts": float(b), "o": px, "h": px, "l": px, "c": px}
        else:
            cur["h"] = max(cur["h"], px)
            cur["l"] = min(cur["l"], px)
            cur["c"] = px

    if pos is not None and ticks:
        close_pos(ticks[-1][0], ticks[-1][1], "end_of_data")
    return summarize(trades, equity0, max_dd)


def summarize(trades, equity0, max_dd):
    n = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    gross_total = sum(t["gross"] for t in trades)
    gross_pos = sum(t["gross"] for t in trades if t["gross"] > 0)
    costs = sum(t["costs"] for t in trades)
    return {
        "trades": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "pnl_usdt": pnl,
        "pnl_pct": pnl / equity0 * 100.0 if equity0 else 0.0,
        "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "max_dd_usdt": max_dd,
        "avg_hold_min": (sum(t["hold_min"] for t in trades) / n) if n else 0.0,
        "gross_pnl_usdt": gross_total,
        "costs_usdt": costs,
        "costs_share_of_gross_profit": (costs / gross_pos) if gross_pos > 0 else None,
        "exits": {k: sum(1 for t in trades if t["reason"] == k) for k in ("tp", "stop", "max_hold", "liquidation", "end_of_data")},
        "trade_list": trades,
    }


def walkforward(ticks, cfg, k, equity0=30.0):
    """k независимых окон подряд (параметры не подбираются — это проверка стабильности, не оптимизация)."""
    n = len(ticks)
    size = n // k
    out = []
    for j in range(k):
        seg = ticks[j * size:(j + 1) * size if j < k - 1 else n]
        out.append(run_backtest(seg, cfg, equity0))
    return out


def fmt(m, title=""):
    pf = m["profit_factor"]
    cs = m["costs_share_of_gross_profit"]
    return ("%s trades=%d win=%.1f%% pnl=%+.3f USDT (%+.2f%%) PF=%s maxDD=%.3f hold=%.0fmin costs=%.3f gross=%+.3f costs/grossProfit=%s exits=%s" % (
        title, m["trades"], m["win_rate"] * 100, m["pnl_usdt"], m["pnl_pct"],
        "inf" if pf == float("inf") else "%.2f" % pf, m["max_dd_usdt"], m["avg_hold_min"], m["costs_usdt"],
        m["gross_pnl_usdt"], "n/a" if cs is None else "%.0f%%" % (cs * 100),
        ",".join("%s:%d" % kv for kv in m["exits"].items() if kv[1])))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Бэктест трендовой стратегии на минутных тиках")
    ap.add_argument("--ticks")
    ap.add_argument("--synthetic", type=int, help="число минутных тиков")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=0, help="прогнать seed..seed+N-1 и дать сводку")
    ap.add_argument("--regime", default="mixed", choices=["mixed", "trend", "range"])
    ap.add_argument("--walkforward", type=int, default=0)
    ap.add_argument("--config")
    ap.add_argument("--equity", type=float, default=30.0)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.synthetic and a.seeds:
        tot = []
        for s in range(a.seed, a.seed + a.seeds):
            m = run_backtest(data.synthetic_ticks(a.synthetic, s, a.regime), cfg, a.equity)
            tot.append(m)
            print(fmt(m, "seed=%d" % s))
        print("MEAN pnl=%+.3f USDT, positive seeds %d/%d, mean trades %.1f" % (
            sum(m["pnl_usdt"] for m in tot) / len(tot), sum(1 for m in tot if m["pnl_usdt"] > 0), len(tot),
            sum(m["trades"] for m in tot) / len(tot)))
        return 0
    if a.ticks:
        ticks = data.load_ticks(a.ticks)
    elif a.synthetic:
        ticks = data.synthetic_ticks(a.synthetic, a.seed, a.regime)
    else:
        ap.error("нужен --ticks или --synthetic N")
    if not ticks:
        print("нет тиков")
        return 1
    if a.walkforward:
        for j, m in enumerate(walkforward(ticks, cfg, a.walkforward, a.equity)):
            print(fmt(m, "window %d/%d" % (j + 1, a.walkforward)))
        return 0
    print(fmt(run_backtest(ticks, cfg, a.equity), "ALL"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
