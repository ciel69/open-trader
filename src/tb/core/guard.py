"""Проверка сделки перед входом (дискреционный режим, профиль config/risk_profile.json).

  guard.py --sym ETH --side long --entry 2300 --sl 2254 --tp 2392 --equity 30.3 --free 20.3 [--score 0.7] [--regime range]
Печатает JSON: {"ok", "leverage", "margin", "notional", "risk_usdt", "reasons", "limits"}. Ничего не торгует.
Решение «входить» принимает агент, но вход разрешён только при ok=true; leverage и margin берутся отсюда, не придумываются.
Контракты (min_size_quote, max_leverage) берутся из --min-quote/--contract-max-lev (из perps_list_contracts).
Учёт по журналу state/trades.jsonl: открытые и закрытые live-сделки за сегодня (UTC).
"""
import argparse
import json
import math
import os
import sys
import time

from tb import paths
from tb.core import journal
from tb.core import learning
from tb.core import pending
from tb.core import portfolio
from tb.core import risklevels
from tb.core import session


def _venue_cap(venue):
    """Лимит открытых позиций ОДНОЙ биржи из config/exchanges.json (null = только общий лимит профиля); нет биржи в реестре = без лимита."""
    try:
        from tb.core import exchcfg
        e = exchcfg.load()["exchanges"].get(venue)
        return None if e is None else e["limits"]["max_open_positions"]
    except Exception:
        return None


def load_profile(path=None):
    with open(path or paths.config("risk_profile.json"), encoding="utf-8") as f:
        return json.load(f)


def day_state(prof, today):
    trades, order = journal._load()
    live = [trades[i] for i in order if trades[i].get("mode") == "live"]
    open_t = [t for t in live if not t.get("ts_close")]
    closed_today = [t for t in live if t.get("ts_close", "").startswith(today)]
    realized = sum(t["pnl_usdt"] for t in closed_today)
    # v2: убыточные ЧАСТИ ещё открытых позиций (частичное закрытие по плану) уже потеряны: идут в дневной убыток; прибыльные части не засчитываются
    realized += sum(min(0.0, (t.get("realized_parts") or 0.0) - (t.get("fees_parts") or 0.0)) for t in open_t if (t.get("ts_part") or "").startswith(today))
    streak = 0
    for t in sorted(closed_today, key=lambda x: x["ts_close"], reverse=True):
        if t.get("reason") == "sl":
            streak += 1
        else:
            break
    # ждущие лимитные входы (state/pending_orders.json): заполнятся одновременно, поэтому их риск, слот и корреляция считаются как у открытых
    pend = pending.orders(mode="live")
    open_risk = sum(t.get("risk_usdt") or 0.0 for t in open_t) + pending.risk(pend)
    # несколько бирж: все лимиты считаются по сумме (суммарный открытый риск, слоты, коррелированный риск, дневной убыток, одна монета на двух биржах = дубль позиции);
    # разрез по биржам нужен для лимита одной биржи (config/exchanges.json limits.max_open_positions) и для отчёта
    by_venue = {}
    for t in open_t:
        v = by_venue.setdefault(t.get("venue") or "platform", {"open": 0, "risk": 0.0, "realized_today": 0.0})
        v["open"] += 1
        v["risk"] = round(v["risk"] + (t.get("risk_usdt") or 0.0), 4)
    for t in closed_today:
        v = by_venue.setdefault(t.get("venue") or "platform", {"open": 0, "risk": 0.0, "realized_today": 0.0})
        v["realized_today"] = round(v["realized_today"] + t["pnl_usdt"], 4)
    return {"by_venue": by_venue, "realized_today": round(realized, 4), "open_count": len(open_t), "open_risk": round(open_risk, 4),
            "stop_streak": streak, "open_syms": [t["sym"] for t in open_t],
            "pending_count": len(pend), "pending_risk": round(pending.risk(pend), 4), "pending_syms": [r["sym"] for r in pend],
            "open_positions": [{"sym": t["sym"], "side": t["side"], "risk": t.get("risk_usdt") or 0.0} for t in open_t]
            + [{"sym": r["sym"], "side": r["side"], "risk": float(r.get("risk_usdt") or 0.0), "pending": True} for r in pend]}


def round_trip_cost_pct(prof, venue):
    """Издержки круга (вход+выход) в % номинала: 2 x (комиссия + проскальзывание) по площадке."""
    c = (prof.get("costs") or {}).get(venue) or {"fee": prof.get("taker_fee_pct", 0.0009), "slip": 0.0005}
    return 2.0 * (c["fee"] + c["slip"]) * 100.0


def trade_costs_pct(prof, venue, mode="taker"):
    """(издержки при тейке, издержки при стопе) в % номинала.
    taker: обе стороны по рынку. maker: лимитный post-only вход и лимитный тейк (по maker), а стоп исполняется по рынку (taker + проскальзывание).
    limit: ждущий лимитный вход со встроенными стопом и тейком: вход по maker, ОБА выхода по рынку (триггер Bybit Full-режима) = maker + taker + проскальзывание."""
    c = (prof.get("costs") or {}).get(venue) or {}
    rt = round_trip_cost_pct(prof, venue)
    if mode == "maker" and c.get("maker_fee") is not None:
        return 2.0 * c["maker_fee"] * 100.0, (c["maker_fee"] + c["fee"] + c["slip"]) * 100.0
    if mode == "limit" and c.get("maker_fee") is not None:
        both = (c["maker_fee"] + c["fee"] + c["slip"]) * 100.0
        return both, both
    return rt, rt


def same_side_risk(prof, st, sym, side):
    """Открытый риск в ту же сторону по коррелированной группе (BTC/ETH/SOL ~0.85 корреляции: почти одна ставка)."""
    for grp in prof.get("correlation_groups") or []:
        if sym in grp:
            return sum(p["risk"] for p in st["open_positions"] if p["sym"] in grp and p["side"] == side), grp
    return 0.0, None


def check(a, prof, today=None, ctx=None):
    ctx = ctx or {}
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    paper = bool(ctx.get("paper"))
    if paper:   # бумажная сделка: ни журнал live, ни слоты, ни дневные лимиты не участвуют
        st = {"realized_today": 0.0, "open_count": 0, "open_risk": 0.0, "stop_streak": 0, "open_syms": [], "pending_count": 0,
              "pending_risk": 0.0, "pending_syms": [], "open_positions": []}
    else:
        st = day_state(prof, today)
    by_venue = st.pop("by_venue", None) or {}        # разрез по биржам нужен только лимиту одной биржи; в ответ guard (limits) не попадает
    why = []
    sign = 1 if a.side == "long" else -1
    cost_win, cost_loss = trade_costs_pct(prof, ctx.get("venue", "platform"), ctx.get("exec", "taker"))
    cost_pct = cost_loss
    stop_pct = abs(a.entry - a.sl) / a.entry * 100.0 if a.sl else 0.0
    tp_pct = abs(a.tp - a.entry) / a.entry * 100.0 if a.tp else 0.0
    if not a.sl or not a.tp:
        why.append("stop_and_tp_required")
    else:
        if sign * (a.sl - a.entry) >= 0:
            why.append("stop_on_wrong_side")
        if sign * (a.tp - a.entry) <= 0:
            why.append("tp_on_wrong_side")
        if stop_pct > prof["max_stop_pct"]:
            why.append("stop %.2f%% > max %.2f%%" % (stop_pct, prof["max_stop_pct"]))
        # RR после издержек круга: выигрыш уменьшается на cost, проигрыш растёт на cost
        if stop_pct > 0 and (tp_pct - cost_win) / (stop_pct + cost_loss) < prof["min_rr"]:
            why.append("net_rr %.2f < %.2f (брутто %.2f, издержки при тейке %.2f%%, при стопе %.2f%%)" % (
                (tp_pct - cost_win) / (stop_pct + cost_loss), prof["min_rr"], tp_pct / stop_pct, cost_win, cost_loss))
    if st["realized_today"] <= -prof["daily_loss_limit_usdt"]:
        why.append("daily_loss_limit_reached: %.2f" % st["realized_today"])
    if st["stop_streak"] >= prof["max_consecutive_stops"]:
        why.append("%d stops in a row: halt for the day" % st["stop_streak"])
    if st["open_count"] + st["pending_count"] >= prof["max_open_positions"]:
        why.append("max_open_positions" + (" (открытых %d + ждущих лимитов %d)" % (st["open_count"], st["pending_count"]) if st["pending_count"] else ""))
    cap_v = _venue_cap(ctx.get("venue"))
    if cap_v is not None and by_venue.get(ctx.get("venue"), {}).get("open", 0) >= cap_v:
        why.append("max_open_positions_on_exchange %s: %d" % (ctx.get("venue"), cap_v))
    if a.sym in st["open_syms"]:
        why.append("position_exists_on_symbol (no averaging)")      # открытые позиции считаются по ВСЕМ биржам: монета на второй бирже тоже дубль
    if a.sym in st["pending_syms"]:
        why.append("pending_limit_exists_on_symbol (no averaging): сначала ./tb bybit trade cancel")

    corr_risk, corr_grp = same_side_risk(prof, st, a.sym, a.side) if not paper else (0.0, None)
    st = dict(st, same_side_risk=round(corr_risk, 4), corr_group=corr_grp)
    out = {"ok": False, "leverage": 0, "margin": 0.0, "notional": 0.0, "risk_usdt": 0.0, "reasons": why, "limits": st}
    if why or stop_pct <= 0:
        return out
    risk_budget = a.equity * prof["risk_per_trade_frac"] * (0.5 + 0.5 * max(0.0, min(1.0, a.score))) * ctx.get("risk_mult", 1.0)
    if a.regime == "storm":
        why.append("regime_storm")
        return out
    # худший случай за день: потери сегодня + риск открытых + риск новой сделки не выше дневного лимита
    room = prof["daily_loss_limit_usdt"] + st["realized_today"] - st["open_risk"] if not paper else float("inf")
    if "session_room" in ctx:
        room = min(room, ctx["session_room"])
    if "learning_room" in ctx:
        room = min(room, ctx["learning_room"])
    if "risk_cap_usdt" in ctx:      # WP16: agents.json limits.max_risk_usdt (итог = минимум с уровнем)
        room = min(room, ctx["risk_cap_usdt"])
    if prof.get("total_open_risk_cap_frac") is not None and not paper:     # WP16: суммарный открытый риск уровня (только когда строже слотов x риск)
        troom = prof["total_open_risk_cap_frac"] * a.equity - st["open_risk"]
        if troom <= 0:
            why.append("total_open_risk_cap: открытый риск %.3f >= лимита уровня %.3f" % (st["open_risk"], prof["total_open_risk_cap_frac"] * a.equity))
            return out
        room = min(room, troom)
    risk_budget = min(risk_budget, room)
    if risk_budget <= 0:
        why.append("no_daily_risk_room")
        return out
    # коррелированный риск: суммарный открытый риск в одну сторону по группе не выше одного бюджета сделки
    if corr_grp:
        corr_room = prof.get("corr_cap_mult", 1.0) * prof["risk_per_trade_frac"] * a.equity - corr_risk
        if corr_room <= 0:
            why.append("correlated_same_side: открытый риск %.3f по %s уже >= лимита %.3f" % (
                corr_risk, "/".join(corr_grp), prof.get("corr_cap_mult", 1.0) * prof["risk_per_trade_frac"] * a.equity))
            return out
        risk_budget = min(risk_budget, corr_room)
    # размер от стопа ПЛЮС издержки круга: убыток по стопу вместе с комиссиями не выше бюджета
    notional_target = risk_budget / ((stop_pct + cost_pct) / 100.0)
    lev_cap = min(prof["max_leverage"], a.contract_max_lev,
                  100.0 / (prof["liq_factor"] * stop_pct + prof["maintenance_margin_pct"]))
    lev_cap = math.floor(lev_cap)
    if lev_cap < 1:
        why.append("leverage_below_1_for_stop")
        return out
    avail = a.free - (0.0 if paper else prof["min_free_reserve"])
    margin_cap = min(prof["margin_per_trade_max"], avail / (1.0 + lev_cap * prof["taker_fee_pct"]))
    if margin_cap <= 0:
        why.append("no_free_margin_above_reserve")
        return out
    # плечо по умолчанию типа сессии (scalp: 30) — предпочтение, а не нарушение: упирается в потолки контракта, ликвидации и профиля
    lev = int(min(lev_cap, max(1, math.ceil(notional_target / margin_cap), int(ctx.get("lev_default", 1)))))
    margin = math.floor(min(margin_cap, notional_target / lev) * 100.0) / 100.0
    if margin < a.min_quote:
        why.append("margin %.2f < min_size_quote %.2f" % (margin, a.min_quote))
        return out
    notional = margin * lev
    out.update({"ok": True, "leverage": lev, "margin": margin, "notional": round(notional, 2),
                "risk_usdt": round(notional * (stop_pct + cost_pct) / 100.0, 3), "stop_pct": round(stop_pct, 3),
                "tp_pct": round(tp_pct, 3), "cost_pct": round(cost_pct, 3), "cost_win_pct": round(cost_win, 3),
                "net_rr": round((tp_pct - cost_win) / (stop_pct + cost_loss), 2), "liq_dist_pct_est": round(100.0 / lev - prof["maintenance_margin_pct"], 2)})
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", required=True)
    ap.add_argument("--side", required=True, choices=["long", "short"])
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--sl", type=float)
    ap.add_argument("--tp", type=float)
    ap.add_argument("--equity", type=float, help="общий капитал; по умолчанию из portfolio.json")
    ap.add_argument("--free", type=float, help="свободно на площадке; по умолчанию из portfolio.json")
    ap.add_argument("--no-session", action="store_true", dest="no_session", help="без активной сессии (только для ручных проверок)")
    ap.add_argument("--score", type=float, default=0.5)
    ap.add_argument("--regime", default="unknown")
    ap.add_argument("--min-quote", type=float, default=1.0, dest="min_quote")
    ap.add_argument("--contract-max-lev", type=float, default=10, dest="contract_max_lev")
    ap.add_argument("--profile")
    a = ap.parse_args(argv)
    print(json.dumps(evaluate(a), ensure_ascii=False))


def _evaluate_paper(a):
    """Бумажная сделка (a.paper=True): остаются геометрия стопа/RR/издержки, потолок плеча, размер от risk_per_trade_frac x equity x score
    и маржа margin_per_trade_frac x equity; без сессии, слотов, дневных лимитов, серии стопов, корреляции, learning, резерва; risk_mult = 1.0.
    Дополнительно (необязательно): a.stype применяет геометрию типа сессии плана (max_stop_pct, min_rr, потолок плеча)."""
    prof = risklevels.effective(load_profile(getattr(a, "profile", None)))
    stype = getattr(a, "stype", None)
    if stype in prof["session_types"]:
        cfg = prof["session_types"][stype]
        prof = dict(prof, max_stop_pct=cfg["max_stop_pct"], min_rr=cfg["min_rr"], max_leverage=min(prof["max_leverage"], cfg["max_leverage"]))
    if a.equity is None:
        a.equity = portfolio.total(portfolio.load())
    if a.free is None:
        a.free = a.equity
    prof = dict(prof, margin_per_trade_max=round(prof["margin_per_trade_frac"] * a.equity, 4))
    ctx = {"venue": getattr(a, "venue", "bybit"), "exec": getattr(a, "exec", "taker"), "risk_mult": 1.0, "paper": True}
    ctx["lev_default"] = 1
    out = check(a, prof, ctx=ctx)
    out["ctx"] = {"venue": ctx["venue"], "session": None, "risk_mult": 1.0, "paper": True, "equity_total": round(a.equity, 2)}
    return out


def evaluate(a):
    """Полное решение: сессия, портфель, лестница просадки, множитель по результатам, затем check(). a — Namespace как у CLI.
    a.paper=True: упрощённая бумажная проверка (_evaluate_paper)."""
    if getattr(a, "paper", False):
        return _evaluate_paper(a)
    prof = risklevels.effective(load_profile(getattr(a, "profile", None)))      # WP16: пресет уровня риска поверх базы (уровень 3 / нет файла = без изменений)
    port = portfolio.load()
    venue = "platform"
    why_pre, ctx = [], {}
    al = getattr(a, "agent_limits", None)
    if al:       # лимиты агента (agents.json) только сужают: итог = минимум с уровнем
        prof, cap = risklevels.apply_agent_limits(prof, al)
        if cap is not None:
            ctx["risk_cap_usdt"] = cap
    setup = getattr(a, "setup", None)
    if setup:
        ok_su, code_su = risklevels.setup_allowed(setup)
        if not ok_su:
            why_pre.append("%s: сетап %s агрессивнее выбранного уровня риска" % (code_su, setup))
    sess = session.active()
    if sess:
        venue = sess.get("venue", venue)
        # геометрия сделки по горизонту: у сессии-дня горизонт плана (a.stype), у сессии-горизонта её тип; бюджет/срок/лимит сделок всегда у сессии
        gt = session.geometry_type(sess, getattr(a, "stype", None))
        cfg = prof["session_types"][gt]
        ok_s, why_s = session.verdict(sess, prof)
        why_pre += why_s
        prof = dict(prof, max_stop_pct=cfg["max_stop_pct"], min_rr=cfg["min_rr"],
                    max_leverage=min(prof["max_leverage"], cfg["max_leverage"]))
        st = session.stats(sess)
        ctx["session_room"] = sess["budget_usdt"] + st["realized"] - st["open_risk"]
        pm, n = portfolio.perf_mult(gt, prof)
        ctx["perf_mult"], ctx["closed_of_type"] = pm, n
        ctx["horizon"] = gt
        if cfg.get("default_leverage"):
            ctx["lev_default"] = cfg["default_leverage"]
    elif not a.no_session:
        why_pre.append("no_active_session")
    lm = learning.progress()
    if lm and lm["venue"] == venue:
        why_pre += lm["reasons"]
        ctx["learning_room"] = max(0.0, lm["room"])
    prof = portfolio.scaled_profile(prof, port, venue)
    dm = portfolio.dd_mult(port, prof)
    if dm <= 0:
        why_pre.append("drawdown_halt: %.1f%% от максимума" % (100 * portfolio.drawdown(port)))
    ctx["risk_mult"] = dm * ctx.get("perf_mult", 1.0)
    ctx["venue"] = venue
    ctx["exec"] = getattr(a, "exec", "taker")
    if a.equity is None:
        a.equity = portfolio.total(port)
    if a.free is None:
        a.free = port["venues"].get(venue, {}).get("free", 0.0)
    out = check(a, prof, ctx=ctx)
    if why_pre:
        out["ok"], out["reasons"] = False, why_pre + out["reasons"]
    out["ctx"] = {"venue": venue, "session": sess["id"] if sess else None, "risk_mult": round(ctx["risk_mult"], 3),
                  "dd_mult": dm, "perf_mult": ctx.get("perf_mult"), "learning": ({k: lm[k] for k in ("trades", "max_trades", "realized", "room")} if lm and lm["venue"] == venue else None), "equity_total": round(portfolio.total(port), 2)}
    if risklevels.level_changes_behavior(prof):
        out["ctx"]["risk_level"] = {k: prof["risk_level"][k] for k in ("level", "name", "source")}
    return out


def _pyramid_enabled(a):
    """Флаг пирамидинга: a.pyramid_enabled, иначе config/exec_limits.json (pyramid.enabled), иначе False (решение владельца Q1)."""
    v = getattr(a, "pyramid_enabled", None)
    if v is not None:
        return bool(v)
    try:
        from tb.core import exec_cfg
        return bool(exec_cfg.load()["pyramid"]["enabled"])
    except Exception:
        return False


def evaluate_add(a):
    """Добор прибыльной позиции плана (пирамидинг, SPEC_V2 п. 1.7). Только уменьшает допустимое, ничего не ослабляет.
    a: sym, side, px (свежая цена), stop (текущий биржевой стоп), qty_open, avg_open, plan_risk0 (R0, USDT), risk_add_cap (USDT риска добора, None = R0),
    score, contract_max_lev, exec; необязательно: realized_net (результат плана по частям, уже за комиссии, по умолчанию 0), min_r (1.0), lev_open,
    qty_step, min_qty, min_quote, free, venue, pyramid_enabled, no_session, profile.
    -> {"ok","reasons","qty_max","margin_total","leverage"}. Не проверяет position_exists, слоты и max_open_positions (позиция уже занимает слот):
    сумма рисков плана не выходит за резерв R0, нового слота и дневного резерва добор не требует."""
    prof = risklevels.effective(load_profile(getattr(a, "profile", None)))
    port = portfolio.load()
    why, sign = [], (1 if a.side == "long" else -1)
    out = {"ok": False, "reasons": why, "qty_max": 0.0, "margin_total": 0.0, "leverage": 0}
    if not _pyramid_enabled(a):
        why.append("pyramid_disabled")
    venue = getattr(a, "venue", "platform")
    sess = session.active()
    if sess:
        venue = sess.get("venue", venue)
        cfg = prof["session_types"][session.geometry_type(sess, getattr(a, "stype", None))]
        why += [w for w in session.verdict(sess, prof)[1] if w != "session_max_trades"]
        prof = dict(prof, max_leverage=min(prof["max_leverage"], cfg["max_leverage"]))
    elif not getattr(a, "no_session", False):
        why.append("no_active_session")
    prof = portfolio.scaled_profile(prof, port, venue)
    dm = portfolio.dd_mult(port, prof)
    if dm <= 0:
        why.append("drawdown_halt: %.1f%% от максимума" % (100 * portfolio.drawdown(port)))
    st = day_state(prof, time.strftime("%Y-%m-%d", time.gmtime()))
    if st["realized_today"] <= -prof["daily_loss_limit_usdt"]:
        why.append("daily_loss_limit_reached: %.2f" % st["realized_today"])
    if st["stop_streak"] >= prof["max_consecutive_stops"]:
        why.append("%d stops in a row: halt for the day" % st["stop_streak"])
    r0 = float(getattr(a, "plan_risk0", 0) or 0)
    if r0 <= 0:
        why.append("plan_risk0_required")
    px, stop, avg, q0 = float(a.px), float(a.stop), float(a.avg_open), float(a.qty_open)
    if px <= 0 or avg <= 0 or q0 <= 0 or stop <= 0:
        why.append("bad_inputs")
    if why:
        return out
    c = (prof.get("costs") or {}).get(venue) or {"fee": prof.get("taker_fee_pct", 0.0009), "slip": 0.0005}
    f = c["fee"] + c["slip"]
    if getattr(a, "exec", "taker") == "maker" and c.get("maker_fee") is not None:
        f = c["maker_fee"] + c["slip"]
    # P2: позиция в плюсе не менее min_r по R0 (по плану в целом: нереализованное + реализованное)
    realized = float(getattr(a, "realized_net", 0.0) or 0.0)
    pnl_r = (realized + sign * (px - avg) * q0) / r0
    min_r = float(getattr(a, "min_r", 1.0) or 1.0)
    if pnl_r < min_r:
        why.append("ADD_NOT_IN_PROFIT: pnl_r %.2f < %.2f (добор в убыток и усреднение запрещены)" % (pnl_r, min_r))
    # стоп должен стоять с нужной стороны от цены
    if sign * (px - stop) <= 0:
        why.append("stop_on_wrong_side")
    # P3: стоп в безубытке или лучше
    if sign * (stop - avg) < 2 * f * avg:
        why.append("ADD_STOP_NOT_BE: стоп %.6g хуже безубытка (средняя %.6g, нужно запас >= %.6g)" % (stop, avg, 2 * f * avg))
    if why:
        return out
    # P4: размер добора: суммарный худший исход плана при стопе не хуже -R0
    w_now = realized + q0 * sign * (stop - avg) - f * stop * q0
    loss_unit = abs(px - stop) + f * (px + stop)
    cap_in = getattr(a, "risk_add_cap", None)
    cap_usdt = r0 if cap_in is None else min(r0, float(cap_in))
    q_risk = min(cap_usdt / loss_unit, (w_now + r0) / loss_unit)
    if q_risk <= 0:
        why.append("ADD_TOO_SMALL: нет места под риск плана (W(S)=%.4f, R0=%.4f)" % (w_now, r0))
        return out
    # маржа и плечо: суммарная маржа <= margin_per_trade_max, ликвидационный зазор по новой средней и стопу
    lev_cap = min(prof["max_leverage"], float(a.contract_max_lev))
    liq_lev = None
    q = q_risk
    avg_new = (avg * q0 + px * q) / (q0 + q)
    stop_pct_new = abs(avg_new - stop) / avg_new * 100.0
    liq_lev = 100.0 / (prof["liq_factor"] * stop_pct_new + prof["maintenance_margin_pct"])
    lev_cap = math.floor(min(lev_cap, liq_lev))
    if lev_cap < 1:
        why.append("leverage_below_1_for_stop")
        return out
    margin_cap = prof["margin_per_trade_max"]
    q_margin = (margin_cap * lev_cap - q0 * avg) / px
    q = min(q, q_margin)
    free = getattr(a, "free", None)
    if free is not None:
        q = min(q, (free - prof["min_free_reserve"]) * lev_cap / (px * (1.0 + lev_cap * prof["taker_fee_pct"])))
    step = float(getattr(a, "qty_step", 0) or 0)
    if step > 0:
        q = math.floor(q / step + 1e-9) * step
    if q <= 0 or q < float(getattr(a, "min_qty", 0) or 0) or q * px < float(getattr(a, "min_quote", 0) or 0):
        why.append("ADD_TOO_SMALL: допустимый добор %.8g (лимит маржи %.4f)" % (max(q, 0.0), margin_cap))
        return out
    avg_new = (avg * q0 + px * q) / (q0 + q)
    notional_total = avg_new * (q0 + q)
    lev = max(1, int(math.ceil(notional_total / margin_cap - 1e-9)), int(getattr(a, "lev_open", 1) or 1))
    if lev > lev_cap:
        lev = int(lev_cap)
    margin_total = notional_total / lev
    if margin_total > margin_cap * (1 + 1e-9):
        why.append("margin_total %.4f > margin_per_trade_max %.4f" % (margin_total, margin_cap))
        return out
    out.update({"ok": True, "qty_max": q, "margin_total": round(margin_total, 4), "leverage": lev, "pnl_r": round(pnl_r, 2),
                "w_stop": round(w_now, 4), "avg_new": avg_new})
    return out


if __name__ == "__main__":
    main(sys.argv[1:])
