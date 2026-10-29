"""Низкоуровневые операции Bybit поверх `Client.call` для исполнителя v2 (05 WP1, фаза A). Владелец: WP1A.

Функции принимают клиент `c` (venues/bybit.py::Client) и ИМЯ МОНЕТЫ БЕЗ КВОТЫ (`SOL`, как везде в проекте; `SOLUSDT` тоже принимается).
Возвращают словари в форматах VenuePort (06 п. 6.2). Ничего не повторяют сами: повтор решает вызывающий (WAL исполнителя).

Чтение: account, positions_norm, get_stop, open_orders, order_by_link, executions, closed_pnl, klines, ticker, quote, instrument.
Запись: set_leverage, place_entry, place_reduce_only, amend_order, cancel_by_link, cancel_all, set_trading_stop, tighten_stop, close_market.

Инварианты безопасности (проверяются ЗДЕСЬ, до любого обращения к бирже):
  * вход без стопа (`sl` пуст или <= 0) запрещён: PortError(rejected, NO_STOP);
  * стоп только строже: `tighten_stop` читает текущий стоп и отказывает с STOP_NOT_TIGHTER без вызова записи; снять стоп (<= 0) нельзя;
  * все пути берутся из белого списка `bybit.ALLOWED` (его проверяет Client.call); эндпоинтов вывода/перевода/ключей нет вообще.
Ключ читает только venues/bybit.py (Client); этот модуль строк с секретами не содержит, ничего не печатает и не логирует.

Округление (Decimal, через step_floor/step_ceil из bybit.py):
  * количество всегда вниз к qtyStep (никогда не больше запрошенного);
  * цена лимитного входа пассивная: покупка вниз, продажа вверх; цена reduce-only лимита (тейк) не хуже запрошенной: продажа вверх, покупка вниз;
  * НАЧАЛЬНЫЙ стоп входа отодвигается от входа (long: вниз, short: вверх), как в bybit_limit: риск, посчитанный guard-ом, не растёт;
  * ПОДТЯГИВАЕМЫЙ стоп округляется в сторону ужесточения (long: вверх, short: вниз), результат сравнивается с текущим стопом уже ПОСЛЕ округления.
"""
import time
from decimal import ROUND_HALF_UP, Decimal

from tb.executor.ports import (CODE_NO_STOP, CODE_STOP_NOT_TIGHTER, CODE_STOP_ON_WRONG_SIDE, CODE_STOP_REMOVE, KIND_NETWORK, KIND_POSITION_GONE,
                               KIND_RATE, KIND_REJECTED, KIND_UNKNOWN, PortError)
from tb.marketdata.klines import parse_kline
from tb.venues import bybit as B

# локальные строковые коды порта (дополняют ports.PORT_CODES; числовые retCode Bybit кладутся в code как есть)
CODE_BAD_ARGS = "BAD_ARGS"              # неверные аргументы вызова (сторона, тип, пустой link_id): биржа не вызывалась
CODE_MIN_QTY = "MIN_QTY"                # после округления вниз к qtyStep количество меньше минимального
CODE_SIDE_MISMATCH = "SIDE_MISMATCH"    # сторона позиции на бирже не совпала с заявленной

TIMEFRAMES = (1, 3, 5, 15, 30, 60, 120, 240, 360, 720)
EXEC_WINDOW_MS = 7 * 24 * 3600 * 1000 - 60000    # окно startTime..endTime у execution/list и closed-pnl не больше 7 суток
PAGE_LIMIT = 100
MAX_PAGES = 50

# retCode -> класс ошибки (kind). Подтверждено/уточнено на testnet: docs/spec_v2/WP1A_testnet.md.
RATE_CODES = (10006, 10018, 10014)                  # лимит частоты / IP-лимит
UNKNOWN_CODES = (10000, 10016, 10019, 20003)        # сбой на стороне биржи: запрос мог быть обработан
GONE_CODES = (110017, 110025)                       # позиции нет / reduce-only нечего закрывать (для reduce-only операций; порт перепроверяет позиции)
ORDER_NOT_FOUND = (110001, 20001)                   # ордера нет (cancel: уже снят/исполнен)
NOT_MODIFIED = 34040                                # trading-stop: значение не изменилось
LEV_NOT_MODIFIED = 110043                           # set-leverage: плечо то же


# ------------------------------------------------------------------ вспомогательное
def sym_of(base):
    """SOL -> SOLUSDT (SOLUSDT остаётся как есть)."""
    s = str(base).strip().upper()
    return s if s.endswith("USDT") else s + "USDT"


def base_of(symbol):
    """SOLUSDT -> SOL."""
    s = str(symbol or "").strip().upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(x):
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def _dec(x):
    return Decimal(str(x))


def _fmt(d):
    return B.fmt(d if isinstance(d, Decimal) else _dec(d))


def rejected(msg, code=None):
    return PortError(KIND_REJECTED, msg, code=code)


def side_word(side):
    """long|buy -> 'Buy'; short|sell -> 'Sell' (регистр не важен)."""
    s = str(side or "").strip().lower()
    if s in ("long", "buy"):
        return "Buy"
    if s in ("short", "sell"):
        return "Sell"
    raise rejected("неверная сторона %r" % (side,), CODE_BAD_ARGS)


def pos_side(side_word_):
    """'Buy' -> 'long', 'Sell' -> 'short', иначе ''."""
    return {"Buy": "long", "Sell": "short"}.get(side_word_, "")


def trigger_word(trigger_by):
    t = str(trigger_by or "last").strip().lower()
    if t in ("last", "lastprice"):
        return "LastPrice"
    if t in ("mark", "markprice"):
        return "MarkPrice"
    raise rejected("неверный trigger_by %r" % (trigger_by,), CODE_BAD_ARGS)


def check_link(link_id):
    if not link_id or not isinstance(link_id, str) or len(link_id) > 36:
        raise rejected("link_id пуст или длиннее 36 символов: %r" % (link_id,), CODE_BAD_ARGS)
    return link_id


def to_port_error(exc, write=True):
    """Исключение клиента/транспорта -> PortError (таблица классов в live_port.py).

    BybitError с кодом биржи: запрос дошёл и обработан -> rejected (rate для 10006/10018, unknown для сбоев биржи 10000/10016/10019).
    HTTP 429 -> rate; HTTP 401/403 (прокси, блокировка IP: запрос не обработан) -> network; прочий HTTP -> unknown для записи, network для чтения.
    Остальные исключения (таймаут, обрыв, мусор в ответе): запись -> unknown (ордер мог создаться), чтение -> network.
    """
    if isinstance(exc, PortError):
        return exc
    if isinstance(exc, B.BybitError):
        code, http = getattr(exc, "code", None), getattr(exc, "http", None)
        if code is not None:
            low = str(exc).lower()
            if "zero position" in low or "position is zero" in low:      # 10001 «can not set tp/sl/ts for zero position», 110017 «current position is zero»
                return PortError(KIND_POSITION_GONE, str(exc), code=code)
            if code in RATE_CODES:
                return PortError(KIND_RATE, str(exc), code=code)
            if code in UNKNOWN_CODES:
                return PortError(KIND_UNKNOWN if write else KIND_NETWORK, str(exc), code=code)
            return PortError(KIND_REJECTED, str(exc), code=code)
        if http is not None:
            if http == 429:
                return PortError(KIND_RATE, str(exc), code="HTTP_429")
            if http in (401, 403):
                return PortError(KIND_NETWORK, str(exc), code="HTTP_%s" % http)
            return PortError(KIND_UNKNOWN if write else KIND_NETWORK, str(exc), code="HTTP_%s" % http)
        return PortError(KIND_REJECTED, str(exc))       # локальный отказ клиента (белый список, нет ключа)
    text = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    reason = getattr(exc, "reason", exc)
    refused = isinstance(reason, ConnectionRefusedError) or type(reason).__name__ == "gaierror"
    if refused or not write:
        return PortError(KIND_NETWORK, text)            # запрос не ушёл (DNS, отказ соединения) либо чтение
    return PortError(KIND_UNKNOWN, text)


def _get_list(c, path, params, cursor_pages=1):
    """GET со списком в result.list; идёт по nextPageCursor до cursor_pages страниц. -> [dict]."""
    out, params, cur = [], dict(params), None
    for _ in range(max(1, cursor_pages)):
        if cur:
            params["cursor"] = cur
        r = c.call("GET", path, params)
        out += r.get("list") or []
        cur = r.get("nextPageCursor")
        if not cur:
            break
    return out


# ------------------------------------------------------------------ чтение
def instrument(c, sym):
    """tick, qty_step, min_qty, min_notional, max_leverage (строки, кроме плеча)."""
    return B.instrument(c, base_of(sym))


def ticker(c, sym):
    """-> {"bid","ask","last","mark","funding"} из GET /v5/market/tickers (публичный)."""
    it = c.call("GET", "/v5/market/tickers", {"category": "linear", "symbol": sym_of(sym)}, auth=False)["list"][0]
    return {"bid": _f(it.get("bid1Price")), "ask": _f(it.get("ask1Price")), "last": _f(it.get("lastPrice")),
            "mark": _f(it.get("markPrice")), "funding": _f(it.get("fundingRate"))}


def quote(c, sym):
    t = ticker(c, sym)
    return t["bid"], t["ask"], t["last"]


def account(c, clock=time.time):
    """-> {"equity","free","ts"} (UNIFIED; free как в bybit.wallet)."""
    w = B.wallet(c)
    return {"equity": w["equity"], "free": w["free"], "ts": clock()}


def _norm_position(p, clock=time.time):
    side = pos_side(p.get("side"))
    upd = _f(p.get("updatedTime")) / 1000.0
    return {"sym": base_of(p.get("symbol")), "side": side, "size": _f(p.get("size")), "avg": _f(p.get("avgPrice") or p.get("entryPrice")),
            "mark": _f(p.get("markPrice")), "sl": _f(p.get("stopLoss")), "tp": _f(p.get("takeProfit")), "liq": _f(p.get("liqPrice")),
            "upnl": _f(p.get("unrealisedPnl")), "lev": _f(p.get("leverage")),
            "margin": _f(p.get("positionIM") or p.get("positionBalance")), "ts": upd or clock(),
            "position_idx": _i(p.get("positionIdx"))}


def positions_norm(c, sym=None, clock=time.time):
    """Открытые позиции (size > 0) в формате VenuePort.positions; sym=None -> все USDT-перпы. Односторонний режим (positionIdx 0)."""
    params = {"category": "linear"}
    params.update({"symbol": sym_of(sym)} if sym else {"settleCoin": "USDT", "limit": "200"})
    rows = _get_list(c, "/v5/position/list", params, cursor_pages=5 if not sym else 1)
    return [_norm_position(p, clock) for p in rows if _f(p.get("size")) > 0]


def get_stop(c, sym):
    """Текущая защита позиции: {"side","size","sl","tp"} или None, если позиции нет (sl/tp = 0.0, когда не заданы)."""
    ps = positions_norm(c, sym)
    if not ps:
        return None
    p = ps[0]
    return {"side": p["side"], "size": p["size"], "sl": p["sl"], "tp": p["tp"], "avg": p["avg"]}


def norm_order(o):
    """Строка order/realtime|history -> формат VenuePort.open_orders (+ avg, fee, stop_type, updated_ms, reject_reason, cancel_type как дополнительные поля)."""
    return {"sym": base_of(o.get("symbol")), "link_id": o.get("orderLinkId") or "", "order_id": o.get("orderId") or "",
            "side": o.get("side") or "", "type": (o.get("orderType") or "").lower(), "price": _f(o.get("price")), "qty": _f(o.get("qty")),
            "filled": _f(o.get("cumExecQty")), "reduce_only": bool(o.get("reduceOnly")), "status": o.get("orderStatus") or "",
            "ts": _f(o.get("createdTime")) / 1000.0, "avg": _f(o.get("avgPrice")), "fee": _f(o.get("cumExecFee")),
            "stop_type": _stop_type(o.get("stopOrderType")), "updated_ms": _i(o.get("updatedTime")),
            "reject_reason": _reason(o.get("rejectReason")), "cancel_type": _reason(o.get("cancelType"))}


def _reason(x):
    return "" if x in (None, "", "EC_NoError", "UNKNOWN") else str(x)


def _stop_type(x):
    x = x or ""
    return "" if x == "UNKNOWN" else x


# встроенные в позицию стоп/тейк биржа показывает в order/realtime как «Untriggered» reduce-only market (проверено на testnet): это не ордера порта
POSITION_STOP_TYPES = ("StopLoss", "TakeProfit", "PartialStopLoss", "PartialTakeProfit", "TrailingStop", "MmRateClose")


def open_orders(c, sym=None, include_stops=False):
    """Активные ордера (New, PartiallyFilled...). sym=None -> все USDT-перпы. Стоп/тейк уровня позиции (stopOrderType StopLoss/TakeProfit...)
    по умолчанию НЕ включаются (они видны в positions(): sl/tp); include_stops=True вернёт и их."""
    params = {"category": "linear", "limit": "50"}
    params.update({"symbol": sym_of(sym)} if sym else {"settleCoin": "USDT"})
    out = [norm_order(o) for o in _get_list(c, "/v5/order/realtime", params, cursor_pages=5)]
    return out if include_stops else [o for o in out if o["stop_type"] not in POSITION_STOP_TYPES]


def order_by_link(c, sym, link_id):
    """Ордер по orderLinkId: realtime, затем history (как bybit_limit.lookup). None, если биржа не знает такого."""
    params = {"category": "linear", "symbol": sym_of(sym), "orderLinkId": check_link(link_id)}
    lst = c.call("GET", "/v5/order/realtime", params).get("list") or []
    if not lst:
        lst = c.call("GET", "/v5/order/history", params).get("list") or []
    return norm_order(lst[0]) if lst else None


def _windows(since_ms, now_ms):
    """Разбить [since, now] на окна по <= 7 суток (ограничение биржи на startTime..endTime), от старых к новым."""
    out, a = [], int(since_ms)
    while True:
        b = min(a + EXEC_WINDOW_MS, int(now_ms))
        out.append((a, b))
        if b >= now_ms:
            return out
        a = b + 1


def norm_execution(e):
    return {"exec_id": e.get("execId") or "", "sym": base_of(e.get("symbol")), "side": e.get("side") or "", "price": _f(e.get("execPrice")),
            "qty": _f(e.get("execQty")), "fee": _f(e.get("execFee")), "is_maker": bool(e.get("isMaker")), "link_id": e.get("orderLinkId") or "",
            "order_id": e.get("orderId") or "", "stop_type": _stop_type(e.get("stopOrderType")), "exec_type": e.get("execType") or "",
            "closed_size": _f(e.get("closedSize")), "pnl": _f(e.get("execPnl")), "ts_ms": _i(e.get("execTime"))}


def executions(c, since_ms, sym=None, clock=time.time):
    """Исполнения с since_ms (включительно) по возрастанию времени. Окна <= 7 суток, пагинация по курсору (limit 100), дубли по execId убраны,
    funding-записи (execType=Funding) пропущены."""
    now_ms = int(clock() * 1000) + 1000
    seen, out = set(), []
    for a, b in _windows(max(0, int(since_ms)), now_ms):
        params = {"category": "linear", "startTime": str(a), "endTime": str(b), "limit": str(PAGE_LIMIT)}
        if sym:
            params["symbol"] = sym_of(sym)
        for e in _get_list(c, "/v5/execution/list", params, cursor_pages=MAX_PAGES):
            ex = norm_execution(e)
            if ex["exec_type"] == "Funding" or not ex["exec_id"] or ex["exec_id"] in seen:
                continue
            seen.add(ex["exec_id"])
            out.append(ex)
    out.sort(key=lambda x: (x["ts_ms"], x["exec_id"]))
    return out


def closed_pnl(c, sym, since_ms, clock=time.time):
    """Закрытые позиции с since_ms по возрастанию времени. side = сторона ПОЗИЦИИ (long|short); у биржи в этом поле сторона закрывающего ордера."""
    now_ms = int(clock() * 1000) + 1000
    out, seen = [], set()
    for a, b in _windows(max(0, int(since_ms)), now_ms):
        params = {"category": "linear", "startTime": str(a), "endTime": str(b), "limit": str(PAGE_LIMIT)}
        if sym:
            params["symbol"] = sym_of(sym)
        for r in _get_list(c, "/v5/position/closed-pnl", params, cursor_pages=MAX_PAGES):
            key = (r.get("orderId"), r.get("updatedTime"))
            if key in seen:
                continue
            seen.add(key)
            closing = r.get("side")
            out.append({"sym": base_of(r.get("symbol")), "side": {"Sell": "long", "Buy": "short"}.get(closing, ""), "qty": _f(r.get("closedSize") or r.get("qty")),
                        "entry": _f(r.get("avgEntryPrice")), "exit": _f(r.get("avgExitPrice")), "pnl": _f(r.get("closedPnl")),
                        "open_fee": _f(r.get("openFee")), "close_fee": _f(r.get("closeFee")), "ts_ms": _i(r.get("updatedTime") or r.get("createdTime")),
                        "order_id": r.get("orderId") or ""})
    out.sort(key=lambda x: x["ts_ms"])
    return out


def klines(c, sym, tf_min, limit=200):
    """-> [(start_ms, o, h, l, c, vol, turnover)] по возрастанию времени (числа float). Последняя свеча может быть незакрытой."""
    tf = int(tf_min)
    if tf not in TIMEFRAMES:
        raise rejected("неподдерживаемый таймфрейм %r" % (tf_min,), CODE_BAD_ARGS)
    r = c.call("GET", "/v5/market/kline", {"category": "linear", "symbol": sym_of(sym), "interval": str(tf), "limit": str(max(1, min(int(limit), 1000)))},
               auth=False)
    rows, _ = parse_kline(r)
    out = [(s * 1000,) + tuple(float(v) for v in vals) for s, *vals in rows]
    out.sort(key=lambda x: x[0])
    return out


# ------------------------------------------------------------------ округление
def round_qty(qty, ins):
    """Количество вниз к qtyStep; меньше minOrderQty -> PortError(rejected, MIN_QTY)."""
    if qty is None or _f(qty) <= 0:
        raise rejected("количество должно быть больше нуля: %r" % (qty,), CODE_BAD_ARGS)
    q = B.step_floor(qty, ins["qty_step"])
    if q < _dec(ins["min_qty"]):
        raise rejected("количество %s после округления к шагу %s меньше минимума %s" % (_fmt(q), ins["qty_step"], ins["min_qty"]), CODE_MIN_QTY)
    return q


def round_price_nearest(px, ins):
    """Цена к ближайшему тику (для amend, где сторона ордера неизвестна)."""
    tick = _dec(ins["tick"])
    return (_dec(px) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


# ------------------------------------------------------------------ запись
def set_leverage(c, sym, lev):
    """Плечо (одинаковое для обеих сторон). 110043 «not modified» = успех."""
    lv = _f(lev)
    if lv < 1:
        raise rejected("плечо должно быть >= 1: %r" % (lev,), CODE_BAD_ARGS)
    s = _fmt(_dec(lv))
    try:
        c.call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym_of(sym), "buyLeverage": s, "sellLeverage": s})
    except B.BybitError as e:
        if getattr(e, "code", None) != LEV_NOT_MODIFIED:
            raise
        return {"ok": True, "unchanged": True}
    return {"ok": True, "unchanged": False}


def place_entry(c, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last", ins=None):
    """Вход (market либо лимит) СО ВСТРОЕННЫМ СТОПОМ (tpslMode=Full, positionIdx=0). Без стопа: PortError(rejected, NO_STOP) до обращения к бирже.

    Лимит: post_only=True -> timeInForce=PostOnly, иначе GTC; цена пассивно (покупка вниз, продажа вверх). Стоп отодвигается от входа
    (long: вниз, short: вверх). -> {"ok","order_id","link_id","qty","price","stop"}. ins: instrument(); если None, запрашивается."""
    if sl is None or _f(sl) <= 0:
        raise rejected("вход без стопа запрещён", CODE_NO_STOP)
    sd = side_word(side)
    if str(side).lower() not in ("long", "short"):
        raise rejected("сторона входа должна быть long|short, а не %r" % (side,), CODE_BAD_ARGS)
    long_ = sd == "Buy"
    ot = str(order_type or "").lower()
    if ot not in ("market", "limit"):
        raise rejected("order_type должен быть market|limit, а не %r" % (order_type,), CODE_BAD_ARGS)
    check_link(link_id)
    ins = ins or instrument(c, sym)
    q = round_qty(qty, ins)
    tick = ins["tick"]
    sl_r = B.step_floor(sl, tick) if long_ else B.step_ceil(sl, tick)
    if sl_r <= 0:
        raise rejected("стоп после округления <= 0", CODE_NO_STOP)
    body = {"category": "linear", "symbol": sym_of(sym), "side": sd, "orderType": "Market" if ot == "market" else "Limit", "qty": _fmt(q),
            "positionIdx": 0, "tpslMode": "Full", "stopLoss": _fmt(sl_r), "slTriggerBy": trigger_word(sl_trigger), "orderLinkId": link_id}
    px = None
    if ot == "limit":
        if price is None or _f(price) <= 0:
            raise rejected("для лимита нужна цена", CODE_BAD_ARGS)
        px = B.step_floor(price, tick) if long_ else B.step_ceil(price, tick)
        body["price"] = _fmt(px)
        body["timeInForce"] = "PostOnly" if post_only else "GTC"
    r = c.call("POST", "/v5/order/create", body)
    return {"ok": True, "order_id": r["orderId"], "link_id": link_id, "qty": float(q), "price": float(px) if px is not None else None, "stop": float(sl_r)}


def place_reduce_only(c, sym, side, qty, link_id, price=None, post_only=False, ins=None):
    """Закрывающий ордер reduceOnly=True (positionIdx=0). side = сторона ЗАКРЫТИЯ (sell для long). price=None -> market.
    Лимит: цена не хуже запрошенной (продажа вверх, покупка вниз); post_only -> PostOnly, иначе GTC."""
    sd = side_word(side)
    check_link(link_id)
    ins = ins or instrument(c, sym)
    q = round_qty(qty, ins)
    body = {"category": "linear", "symbol": sym_of(sym), "side": sd, "orderType": "Market" if price is None else "Limit", "qty": _fmt(q),
            "reduceOnly": True, "positionIdx": 0, "orderLinkId": link_id}
    px = None
    if price is not None:
        if _f(price) <= 0:
            raise rejected("цена должна быть больше нуля", CODE_BAD_ARGS)
        px = B.step_ceil(price, ins["tick"]) if sd == "Sell" else B.step_floor(price, ins["tick"])
        body["price"] = _fmt(px)
        body["timeInForce"] = "PostOnly" if post_only else "GTC"
    r = c.call("POST", "/v5/order/create", body)
    return {"ok": True, "order_id": r["orderId"], "link_id": link_id, "qty": float(q), "price": float(px) if px is not None else None}


def amend_order(c, sym, link_id, price=None, qty=None, ins=None):
    """Изменить цену и/или количество живого ордера по orderLinkId (POST /v5/order/amend). Цена к ближайшему тику, количество вниз к шагу."""
    if price is None and qty is None:
        raise rejected("amend без изменений: нужна price или qty", CODE_BAD_ARGS)
    check_link(link_id)
    ins = ins or instrument(c, sym)
    body = {"category": "linear", "symbol": sym_of(sym), "orderLinkId": link_id}
    if price is not None:
        if _f(price) <= 0:
            raise rejected("цена должна быть больше нуля", CODE_BAD_ARGS)
        body["price"] = _fmt(round_price_nearest(price, ins))
    if qty is not None:
        body["qty"] = _fmt(round_qty(qty, ins))
    try:
        r = c.call("POST", "/v5/order/amend", body)
    except B.BybitError as e:
        if getattr(e, "code", None) == 10001 and "not modified" in str(e):      # те же цена и количество (проверено на testnet: 10001 «order not modified»)
            return {"ok": True, "unchanged": True, "order_id": None, "link_id": link_id}
        raise
    return {"ok": True, "unchanged": False, "order_id": r.get("orderId"), "link_id": link_id}


def cancel_by_link(c, sym, link_id):
    """Отмена по orderLinkId. Ордера уже нет (110001: исполнен/снят) = {"ok":True,"gone":True} (отмена идемпотентна)."""
    check_link(link_id)
    try:
        r = c.call("POST", "/v5/order/cancel", {"category": "linear", "symbol": sym_of(sym), "orderLinkId": link_id})
    except B.BybitError as e:
        if getattr(e, "code", None) in ORDER_NOT_FOUND:
            return {"ok": True, "gone": True, "link_id": link_id}
        raise
    return {"ok": True, "gone": False, "link_id": link_id, "order_id": r.get("orderId")}


def cancel_all(c, sym, reduce_only_only=False):
    """Снять ордера по символу. reduce_only_only=False -> POST cancel-all с orderFilter=Order (обычные активные ордера). ВАЖНО: без orderFilter
    (и с StopOrder) Bybit снимает и СТОП/ТЕЙК УРОВНЯ ПОЗИЦИИ (проверено на testnet): позиция остаётся без защиты. True -> только reduce-only
    обычные ордера (по одному, orderId). -> {"ok","cancelled":[order_id]}."""
    if not reduce_only_only:
        r = c.call("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": sym_of(sym), "orderFilter": "Order"})
        return {"ok": True, "cancelled": [x.get("orderId") for x in (r.get("list") or [])]}
    done = []
    for o in open_orders(c, sym):
        if not o["reduce_only"]:
            continue
        try:
            c.call("POST", "/v5/order/cancel", {"category": "linear", "symbol": sym_of(sym), "orderId": o["order_id"]})
            done.append(o["order_id"])
        except B.BybitError as e:
            if getattr(e, "code", None) not in ORDER_NOT_FOUND:
                raise
    return {"ok": True, "cancelled": done}


def set_trading_stop(c, sym, sl=None, tp=None, trigger_by="LastPrice"):
    """Сырой POST /v5/position/trading-stop (tpslMode=Full, positionIdx=0) БЕЗ проверки монотонности: для стопа используй tighten_stop.
    Снятие (<= 0) запрещено. 34040 «not modified» -> {"ok":True,"unchanged":True}. Хотя бы одно из sl/tp обязательно."""
    if sl is None and tp is None:
        raise rejected("trading-stop без значений", CODE_BAD_ARGS)
    body = {"category": "linear", "symbol": sym_of(sym), "tpslMode": "Full", "positionIdx": 0}
    trig = trigger_word(trigger_by)
    if sl is not None:
        if _f(sl) <= 0:
            raise rejected("снять стоп нельзя", CODE_STOP_REMOVE)
        body["stopLoss"], body["slTriggerBy"] = _fmt(sl), trig
    if tp is not None:
        if _f(tp) <= 0:
            raise rejected("снять тейк через trading-stop нельзя", CODE_STOP_REMOVE)
        body["takeProfit"], body["tpTriggerBy"] = _fmt(tp), trig
    try:
        c.call("POST", "/v5/position/trading-stop", body)
    except B.BybitError as e:
        if getattr(e, "code", None) == NOT_MODIFIED:
            return {"ok": True, "unchanged": True}
        raise
    return {"ok": True, "unchanged": False}


def tighten_stop(c, sym, side, stop_price, trigger_by="last", ins=None, tk=None):
    """Подтянуть стоп позиции. side = сторона ПОЗИЦИИ (long|short). Порядок проверок (до записи):
      1. stop_price None/<=0 -> rejected STOP_REMOVE_FORBIDDEN;
      2. позиции нет -> PortError(position_gone); сторона не совпала -> rejected SIDE_MISMATCH;
      3. округление в сторону ужесточения (long вверх, short вниз);
      4. текущий стоп на бирже cur > 0: новый строго слабее -> rejected STOP_NOT_TIGHTER (запись НЕ вызывается); равный -> unchanged без записи;
      5. новый стоп обязан быть по правильную сторону от цены триггера (long: ниже last/mark, short: выше), иначе rejected STOP_ON_WRONG_SIDE;
      6. запись trading-stop; 34040 -> unchanged.
    -> {"ok":True,"stop":float,"unchanged":bool}. tk: заранее полученный ticker() (для тестов)."""
    if stop_price is None or _f(stop_price) <= 0:
        raise rejected("снять стоп нельзя", CODE_STOP_REMOVE)
    ps = str(side).lower()
    if ps not in ("long", "short"):
        raise rejected("side позиции должен быть long|short, а не %r" % (side,), CODE_BAD_ARGS)
    long_ = ps == "long"
    cur_pos = get_stop(c, sym)
    if cur_pos is None:
        raise PortError(KIND_POSITION_GONE, "позиции по %s нет" % sym)
    if cur_pos["side"] != ps:
        raise rejected("сторона позиции на бирже %s, заявлена %s" % (cur_pos["side"], ps), CODE_SIDE_MISMATCH)
    ins = ins or instrument(c, sym)
    new = B.step_ceil(stop_price, ins["tick"]) if long_ else B.step_floor(stop_price, ins["tick"])
    if new <= 0:
        raise rejected("стоп после округления <= 0", CODE_STOP_REMOVE)
    cur = _dec(cur_pos["sl"])
    if cur > 0:
        if (long_ and new < cur) or (not long_ and new > cur):
            raise rejected("новый стоп %s не строже текущего %s" % (_fmt(new), _fmt(cur)), CODE_STOP_NOT_TIGHTER)
        if new == cur:
            return {"ok": True, "stop": float(new), "unchanged": True}
    trig = trigger_word(trigger_by)
    tk = tk or ticker(c, sym)
    ref = tk["mark"] if trig == "MarkPrice" else tk["last"]
    if ref <= 0:
        raise PortError(KIND_NETWORK, "нет цены %s для проверки стопа" % trig)
    if (long_ and float(new) >= ref) or (not long_ and float(new) <= ref):
        raise rejected("стоп %s не с той стороны от %s %s: сработал бы мгновенно" % (_fmt(new), trig, ref), CODE_STOP_ON_WRONG_SIDE)
    r = set_trading_stop(c, sym, sl=new, trigger_by=trig)
    return {"ok": True, "stop": float(new), "unchanged": bool(r.get("unchanged"))}


def close_market(c, sym, side, qty, link_id, ins=None):
    """reduce-only market. side = сторона закрытия. qty=None -> вся позиция (читает позицию; нет позиции -> PortError(position_gone))."""
    if qty is None:
        ps = positions_norm(c, sym)
        if not ps:
            raise PortError(KIND_POSITION_GONE, "позиции по %s нет" % sym)
        qty = ps[0]["size"]
    return place_reduce_only(c, sym, side, qty, link_id, price=None, ins=ins)
