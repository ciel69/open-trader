"""Ждущие лимитные входы на Bybit: post-only лимит на заданной цене (уровень, откат, ретест), который живёт до заполнения, отмены или ttl.

  ./tb bybit <trade|testnet> open --sym SOL --side long --limit-price 140.5 --sl 139 --tp 144 --thesis "..." [--ttl 60] [--cancel-above X] [--cancel-below Y] [--snap] [--idea I7]
  ./tb bybit <trade|testnet> pending                       список ждущих лимитов (цена, расстояние до рынка, возраст, ttl)
  ./tb bybit <trade|testnet> cancel ID | --all-pending     снять заявку (ID из pending или orderId биржи)
  ./tb bybit <trade|testnet> sync                          заодно обрабатывает ждущие лимиты (заполнение -> журнал, ttl/инвалидация -> отмена)

Защита от окна без стопа: stopLoss и takeProfit (tpslMode=Full, триггер по LastPrice) передаются В САМОМ лимитном ордере. Биржа вешает их на позицию
атомарно с исполнением (в том числе частичным), поэтому позиция не бывает без стопа, даже если мы в этот момент не работаем (ночь, обрыв связи, cron раз в 15 мин).
Цена за это: тейк срабатывает по рынку (taker), а не лимитом; guard считает издержки `exec=limit` = maker вход + taker выход + проскальзывание при тейке и стопе.
Состояние заявок: state/pending_orders.json (core/pending.py): запись пишется ДО отправки ордера (status=placing, orderLinkId = id заявки),
поэтому при обрыве на create sync найдёт ордер по orderLinkId. Заполнение пишется в журнал один раз (поле pending_id в записи open).
Частичное исполнение: остаток отменяется при первой же обработке, в журнал идёт исполненное количество (стоп и тейк на бирже уже на этом количестве).
Эндпоинты и формы ответов по документации Bybit v5; что проверено вживую, см. docs/HARNESS.md.
"""
import argparse
import time
from decimal import Decimal

from tb.core import guard
from tb.core import journal
from tb.core import pending
from tb.core import tradectx
from tb.venues import bybit as B

DEFAULT_TTL_MIN = 60.0
PLACING_GRACE_S = 120.0          # запись status=placing без ордера на бирже старше этого срока удаляется (create не дошёл)
OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered"}
GONE = {"Cancelled", "Rejected", "Deactivated"}


def mode_of(c):
    return "live" if c.env == "mainnet" else "testnet"


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _num(x):
    return float(x) if x is not None else None


# ------------------------------------------------------------------ постановка
def open_limit(c, a, sleep=time.sleep, clock=time.time):
    """Поставить ждущий post-only лимит. Возвращает dict (ok False с stage/reasons при отказе: guard, post_only, min_size, levels)."""
    if c.env == "mainnet" and not a.mainnet_confirm:
        raise B.BybitError("mainnet: нужен флаг --mainnet-confirm")
    sym, long_ = a.sym.upper(), a.side == "long"
    if B.get_position(c, sym):
        raise B.BybitError("по %s уже есть позиция: усреднение запрещено" % sym)
    if pending.orders(env=c.env, sym=sym):
        raise B.BybitError("по %s уже есть ждущий лимит: сначала ./tb bybit ... cancel" % sym)
    lp = float(a.limit_price)
    if lp <= 0:
        raise B.BybitError("--limit-price должен быть больше нуля")
    bid, ask, last = B.quote(c, sym)
    if bid <= 0 or ask <= 0 or ask < bid or last <= 0:
        raise B.BybitError("нет нормальной котировки (bid %s, ask %s)" % (bid, ask))
    ins = B.instrument(c, sym)
    tick = ins["tick"]
    price = B.step_floor(lp, tick) if long_ else B.step_ceil(lp, tick)   # пассивное округление: покупка вниз, продажа вверх
    snapped = False
    if (long_ and price >= Decimal(str(ask))) or (not long_ and price <= Decimal(str(bid))):
        if not getattr(a, "snap", False):
            return {"ok": False, "stage": "post_only", "reasons": [
                "цена %s пересекла бы стакан (bid %s, ask %s): post-only лимит стал бы taker. Выбери %s или добавь --snap (сместить к лучшей цене стакана)"
                % (B.fmt(price), bid, ask, "цену ниже %s" % ask if long_ else "цену выше %s" % bid)]}
        price, snapped = (B.step_floor(bid, tick) if long_ else B.step_ceil(ask, tick)), True
    px = float(price)
    # уровни инвалидации: цена дошла до уровня без заполнения -> отмена. Уровень должен быть по ту сторону рынка, куда цена ещё не ушла
    cab, cbl = getattr(a, "cancel_above", None), getattr(a, "cancel_below", None)
    if cab is not None and cab <= max(last, px):
        return {"ok": False, "stage": "levels", "reasons": ["--cancel-above %s должен быть выше текущей цены %s и цены лимита %s" % (cab, last, B.fmt(price))]}
    if cbl is not None and cbl >= min(last, px):
        return {"ok": False, "stage": "levels", "reasons": ["--cancel-below %s должен быть ниже текущей цены %s и цены лимита %s" % (cbl, last, B.fmt(price))]}
    # стоп отодвигается от входа, тейк придвигается: округление не ухудшает посчитанный guard-ом риск
    sl_r = B.step_floor(a.sl, tick) if long_ else B.step_ceil(a.sl, tick)
    tp_r = B.step_floor(a.tp, tick) if long_ else B.step_ceil(a.tp, tick)
    ns = argparse.Namespace(sym=sym, side=a.side, entry=px, sl=float(sl_r), tp=float(tp_r), equity=None, free=None, score=a.score,
                            regime=a.regime, min_quote=0.0, contract_max_lev=ins["max_leverage"], profile=None, no_session=False, exec="limit")
    g = guard.evaluate(ns)
    if not g["ok"]:
        return {"ok": False, "stage": "guard", "reasons": g["reasons"], "ctx": g.get("ctx")}
    lev, margin = g["leverage"], g["margin"]
    qty = B.step_floor(Decimal(str(margin * lev / px)), ins["qty_step"])
    if qty < Decimal(ins["min_qty"]) or float(qty) * px < float(ins["min_notional"]):
        need = max(float(ins["min_qty"]) * px, float(ins["min_notional"])) / lev
        return {"ok": False, "stage": "min_size", "reasons": ["qty %s < min %s (или номинал < %s): при плече %d нужна маржа около %.2f, guard разрешил %.2f"
                                                           % (B.fmt(qty), ins["min_qty"], ins["min_notional"], lev, need, margin)]}
    # срок жизни: по умолчанию 60 мин (у сессии срока нет, обрезки по её концу не бывает)
    ttl = float(getattr(a, "ttl", None) or DEFAULT_TTL_MIN)
    if ttl <= 0:
        raise B.BybitError("--ttl должен быть больше нуля (минут)")
    sess = guard.session.active()
    clamped = False
    side = "Buy" if long_ else "Sell"
    pid = pending.new_id(c.env, sym, clock())
    order = {"category": "linear", "symbol": sym + "USDT", "side": side, "orderType": "Limit", "price": B.fmt(price), "qty": B.fmt(qty),
             "timeInForce": "PostOnly", "positionIdx": 0, "tpslMode": "Full", "stopLoss": B.fmt(sl_r), "takeProfit": B.fmt(tp_r),
             "slTriggerBy": "LastPrice", "tpTriggerBy": "LastPrice", "orderLinkId": pid}
    plan = {"exec": "limit", "pending_id": pid, "leverage": lev, "margin": margin, "qty": B.fmt(qty), "notional": round(float(qty) * px, 2),
            "limit_price": B.fmt(price), "snapped": snapped, "dist_pct": round((px / last - 1.0) * 100.0, 3), "ttl_min": round(ttl, 1), "ttl_clamped_to_session": clamped,
            "cancel_above": cab, "cancel_below": cbl, "order": order, "net_rr": g.get("net_rr"), "cost_win_pct": g.get("cost_win_pct"),
            "risk_usdt": g.get("risk_usdt"), "guard": g["ctx"]}
    if a.dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    try:
        c.call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym + "USDT", "buyLeverage": str(lev), "sellLeverage": str(lev)})
    except B.BybitError as e:
        if "110043" not in str(e):
            raise
    B.cancel_open_orders(c, sym)     # позиции и своих ждущих лимитов по символу нет: остатки старых ордеров снять
    now = clock()
    rec = {"id": pid, "env": c.env, "mode": mode_of(c), "sym": sym, "side": a.side, "order_id": None, "link_id": pid, "status": "placing",
           "price": px, "qty": B.fmt(qty), "sl": float(sl_r), "tp": float(tp_r), "lev": lev, "margin": margin, "notional": round(float(qty) * px, 2),
           "risk_usdt": g.get("risk_usdt"), "ts": now, "ts_iso": _iso(time.time()), "expires": now + ttl * 60.0, "ttl_min": round(ttl, 1),
           "cancel_above": cab, "cancel_below": cbl, "session": sess["id"] if sess else None, "stype": guard.session.geometry_type(sess, getattr(a, "horizon", None)) if sess else None,
           "idea": getattr(a, "idea", None), "thesis": a.thesis, "news": a.news or "", "regime": a.regime, "score": a.score,
           "ctx_place": tradectx.snapshot(sym, now=now)}      # снимок рынка при постановке; при заполнении journal.cmd_open снимет второй (ctx)
    pending.put(rec)                 # запись ДО create: при обрыве sync найдёт ордер по orderLinkId
    try:
        oid = c.call("POST", "/v5/order/create", order)["orderId"]
    except B.BybitError as e:
        if str(e).startswith("Bybit retCode="):       # биржа явно отказала: ордера нет
            pending.remove(pid)
            return {"ok": False, "stage": "exchange", "reasons": [str(e)]}
        raise B.BybitError("ВНИМАНИЕ %s: ответа на create нет (%s). Ордер мог создаться: заявка сохранена, ./tb bybit ... sync найдёт её по orderLinkId" % (pid, e))
    except Exception as e:
        raise B.BybitError("ВНИМАНИЕ %s: сбой при create (%s). Ордер мог создаться: заявка сохранена, ./tb bybit ... sync найдёт её по orderLinkId" % (pid, e))
    rec = pending.update(pid, order_id=oid, status="open") or rec
    sleep(0.6)
    out = {"ok": True, "pending": True, "pending_id": pid, "order_id": oid, "plan": plan}
    try:
        res = process_one(c, rec, clock(), sleep=sleep)       
    except B.BybitError as e:
        res = {"action": "error", "error": str(e)}
    out["state"] = res
    if res.get("action") in ("cancelled", "dropped"):
        out.update({"ok": False, "stage": "post_only_rejected", "reasons": ["ордер сразу снят биржей (%s): сделки нет" % res.get("reason")]})
    elif res.get("action") == "waiting" and getattr(a, "idea", None):
        _mark_idea(a.idea, "watching", "лимит %s %s @%s, ttl %.0f мин (%s)" % (a.side, sym, B.fmt(price), ttl, pid))
    return out


def _mark_idea(iid, status, note, trade=None):
    try:
        from tb.core import ideas
        ideas.mark_idea(iid, status, note, trade)
    except Exception:
        pass          # связь с идеей вспомогательная: сбой не должен ломать торговлю


# ------------------------------------------------------------------ состояние ордера и обработка
def lookup(c, rec):
    """Состояние ордера заявки по orderId (или orderLinkId, пока orderId неизвестен): realtime, затем история. None, если биржа ордера не знает."""
    params = {"category": "linear", "symbol": rec["sym"] + "USDT"}
    params["orderId" if rec.get("order_id") else "orderLinkId"] = rec.get("order_id") or rec["link_id"]
    lst = c.call("GET", "/v5/order/realtime", params).get("list") or []
    if not lst:
        lst = c.call("GET", "/v5/order/history", params).get("list") or []
    if not lst:
        return None
    o = lst[0]
    return {"status": o.get("orderStatus"), "qty": B._f(o.get("cumExecQty")), "avg": B._f(o.get("avgPrice")), "fee": B._f(o.get("cumExecFee")),
            "order_id": o.get("orderId"), "updated": B._f(o.get("updatedTime")) / 1000.0 or None}


def _session_ended(rec):
    if not rec.get("session"):
        return False
    sess, _ = guard.session.load_all()
    s = sess.get(rec["session"])
    return bool(s and s.get("stopped"))


def _cancel(c, rec, sleep):
    body = {"category": "linear", "symbol": rec["sym"] + "USDT"}
    body["orderId" if rec.get("order_id") else "orderLinkId"] = rec.get("order_id") or rec["link_id"]
    try:
        c.call("POST", "/v5/order/cancel", body)
    except B.BybitError:
        pass                      # гонка: ордер мог исполниться или уже отмениться; решает повторное чтение статуса
    st = None
    for _ in range(4):
        sleep(0.6)
        st = lookup(c, rec)
        if st is None or st["status"] not in OPEN_STATUSES:
            break
    return st


def process_one(c, rec, now, sleep=time.sleep, force=None):
    """Одна заявка: заполнение -> журнал; ttl/инвалидация/частичное/force -> отмена. -> dict {"id","action": waiting|filled|cancelled|dropped|error, ...}."""
    base = {"id": rec["id"], "sym": rec["sym"], "side": rec["side"]}
    st = lookup(c, rec)
    if st is None:
        if rec.get("order_id") is None and now - rec.get("ts", now) > PLACING_GRACE_S:
            pending.remove(rec["id"])
            return dict(base, action="dropped", reason="ордер на бирже не найден (create не дошёл)")
        if rec.get("order_id") is not None:
            return dict(base, action="error", error="биржа не знает ордер %s (нет ни в realtime, ни в history): проверь вручную" % rec["order_id"])
        return dict(base, action="waiting", note="ордер ещё не виден на бирже")
    if rec.get("order_id") is None and st.get("order_id"):
        rec = pending.update(rec["id"], order_id=st["order_id"], status="open") or rec
    why = None
    if st["status"] in OPEN_STATUSES:
        px = None
        if force:
            why = force
        elif st["qty"] > 0:
            why = "partial_fill"
        elif now >= rec.get("expires", now + 1):
            why = "ttl"
        elif _session_ended(rec):
            why = "session_ended"
        elif rec.get("cancel_above") is not None or rec.get("cancel_below") is not None:
            px = B.last_price(c, rec["sym"])
            if rec.get("cancel_above") is not None and px >= rec["cancel_above"]:
                why = "invalidated_above %s (цена %s)" % (rec["cancel_above"], px)
            elif rec.get("cancel_below") is not None and px <= rec["cancel_below"]:
                why = "invalidated_below %s (цена %s)" % (rec["cancel_below"], px)
        if not why:
            return dict(base, action="waiting")
        st = _cancel(c, rec, sleep)
        if st is not None and st["status"] in OPEN_STATUSES:
            return dict(base, action="error", error="ордер %s не отменился (%s): ./tb bybit ... cancel повторно" % (rec.get("order_id"), st["status"]))
        if st is None:
            return dict(base, action="error", error="после отмены ордер %s не виден: проверь вручную" % rec.get("order_id"))
    if st["qty"] > 0:
        return settle_fill(c, rec, st, why)
    pending.remove(rec["id"])
    if rec.get("idea") and why and not why.startswith("manual"):
        _mark_idea(rec["idea"], "invalidated" if why.startswith("invalidated") else "expired", "лимит не заполнен: %s (%s)" % (why, rec["id"]))
    return dict(base, action="cancelled", reason=why or ("статус биржи %s" % st["status"]))


def settle_fill(c, rec, st, why=None):
    """Исполнение (полное или частичное, остаток уже снят): запись open в журнал (один раз), проверка защиты позиции, удаление заявки."""
    sym = rec["sym"]
    qty, avg = Decimal(str(st["qty"])), (st["avg"] or rec["price"])
    planned = Decimal(str(rec["qty"]))
    partial = qty < planned
    tr, ids = journal._load()
    jid = next((i for i in ids if tr[i].get("pending_id") == rec["id"]), None)
    if jid is None:
        ns_j = argparse.Namespace(sym=sym, side=rec["side"], entry=avg, lev=float(rec["lev"]), margin=float(qty) * avg / rec["lev"], sl=rec["sl"], tp=rec["tp"],
                                  thesis=rec.get("thesis") or "", news=rec.get("news") or "", regime=rec.get("regime") or "unknown",
                                  tags="bybit,%s,limit_entry%s" % (c.env, ",partial" if partial else ""), mode=rec["mode"], id=B.journal_id(c, sym),
                                  session=rec.get("session"), stype=rec.get("stype"), venue="bybit", fee_open=st["fee"],
                                  ts_open=_iso(st["updated"]) if st.get("updated") else None, idea=rec.get("idea"), pending_id=rec["id"], ctx_place=rec.get("ctx_place"))
        journal.cmd_open(ns_j)
        jid = ns_j.id
    if rec.get("idea"):
        _mark_idea(rec["idea"], "taken", "лимит заполнен %s @%s (%s)" % (B.fmt(qty), avg, rec["id"]), trade=jid)
    out = {"id": rec["id"], "sym": sym, "side": rec["side"], "action": "filled", "qty": B.fmt(qty), "planned_qty": rec["qty"], "partial": partial,
           "avg": avg, "fee": st["fee"], "journal_id": jid}
    if why:
        out["remainder_cancelled"] = why
    warn = verify_protection(c, rec)
    if warn:
        out["warning"] = warn
    pending.remove(rec["id"])
    return out


def verify_protection(c, rec, sleep=time.sleep):
    """Позиция после заполнения должна иметь стоп (прикреплён к ордеру). Нет стопа -> немедленно закрыть по рынку reduce-only. Нет тейка -> предупреждение."""
    try:
        pos = B.get_position(c, rec["sym"])
        if not pos:
            return None              # уже закрылась (стоп/тейк сработали): журнал закроет основной цикл sync
        if B._f(pos.get("stopLoss")) <= 0:
            side = "Sell" if pos["side"] == "Buy" else "Buy"
            c.call("POST", "/v5/order/create", {"category": "linear", "symbol": rec["sym"] + "USDT", "side": side, "orderType": "Market",
                                                "qty": pos["size"], "reduceOnly": True, "positionIdx": 0})
            return "ВНИМАНИЕ: у позиции %s не оказалось стопа: она закрыта по рынку (reduce-only). Проверь ./tb bybit ... positions и журнал" % rec["sym"]
        if B._f(pos.get("takeProfit")) <= 0:
            return "ВНИМАНИЕ: у позиции %s нет тейка (стоп стоит): поставь тейк вручную или закрой" % rec["sym"]
    except Exception as e:
        return "ВНИМАНИЕ: не удалось проверить защиту позиции %s (%s): проверь стоп вручную" % (rec["sym"], e)
    return None


# ------------------------------------------------------------------ sync, cancel, pending
def sync_pending(c, now=None, sleep=time.sleep, force=None, ident=None, only_sym=None):
    """Обработать ждущие лимиты площадки клиента. -> None, если заявок нет (ни одного запроса к бирже), иначе dict со списками.
    ident: id заявки или orderId (для cancel), force: причина принудительной отмены."""
    if not pending.orders(env=c.env):
        return None
    now = time.time() if now is None else now
    res = {"filled": [], "cancelled": [], "waiting": [], "errors": []}
    try:
        with pending.lock():
            for rec in pending.orders(env=c.env, sym=only_sym):     # перечитано под блокировкой
                if ident and ident not in (rec["id"], rec.get("order_id")):
                    continue
                try:
                    r = process_one(c, rec, now, sleep=sleep, force=force)
                except B.BybitError as e:
                    r = {"id": rec["id"], "sym": rec["sym"], "side": rec["side"], "action": "error", "error": str(e)}
                {"filled": res["filled"], "cancelled": res["cancelled"], "dropped": res["cancelled"], "waiting": res["waiting"]}.get(r["action"], res["errors"]).append(r)
    except TimeoutError:
        return {"skipped": "заявки обрабатывает другой процесс (блокировка pending_orders.json); повтори позже"}
    return res


def cmd_cancel(c, a, sleep=time.sleep):
    # отмена снижает риск, поэтому --mainnet-confirm не требуется
    if not getattr(a, "all_pending", False) and not getattr(a, "id", None):
        return {"ok": False, "error": "укажи ID заявки или --all-pending"}
    ident = None if getattr(a, "all_pending", False) else a.id
    rows = [r for r in pending.orders(env=c.env) if ident is None or ident in (r["id"], r.get("order_id"))]
    if not rows:
        return {"ok": False, "error": "нет ждущих лимитов%s" % (" с id %s" % ident if ident else "")}
    res = sync_pending(c, sleep=sleep, force="manual", ident=ident)
    res["ok"] = not res.get("errors") and "skipped" not in res
    return res


def list_pending(c, now=None):
    """Для команды pending: заявки площадки с расстоянием до рынка, возрастом и остатком ttl. Только чтение."""
    now = time.time() if now is None else now
    items = []
    for r in pending.orders(env=c.env):
        it = {"id": r["id"], "sym": r["sym"], "side": r["side"], "price": r["price"], "qty": r["qty"], "sl": r["sl"], "tp": r["tp"],
              "age_min": round((now - r["ts"]) / 60.0, 1), "ttl_min": r.get("ttl_min"), "left_min": round((r["expires"] - now) / 60.0, 1),
              "cancel_above": r.get("cancel_above"), "cancel_below": r.get("cancel_below"), "order_id": r.get("order_id"), "status": r.get("status"),
              "risk_usdt": r.get("risk_usdt"), "idea": r.get("idea"), "session": r.get("session")}
        try:
            last = B.last_price(c, r["sym"])
            it["last"], it["dist_pct"] = last, round((r["price"] / last - 1.0) * 100.0, 3)
        except Exception as e:
            it["last"], it["dist_pct"], it["price_error"] = None, None, str(e)[:60]
        try:
            st = lookup(c, r)
            it["exchange_status"] = st["status"] if st else None
            it["filled_qty"] = st["qty"] if st else None
        except Exception as e:
            it["exchange_status"] = "?"
            it["status_error"] = str(e)[:60]
        items.append(it)
    return {"ok": True, "env": c.env, "count": len(items), "pending": items}
