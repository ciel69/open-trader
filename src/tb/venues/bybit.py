"""Исполнение на Bybit (USDT-перпы, API v5): только торговля. По умолчанию TESTNET. Ключи только из переменных окружения.

  export BYBIT_API_KEY=...   export BYBIT_API_SECRET=...        # ключ с правами только «Contract: Orders & Positions», БЕЗ вывода, с IP-привязкой
  ./tb bybit <read|trade|testnet> balance | positions | info --sym SOL
  ./tb bybit <read|trade|testnet> open  --sym SOL --side long --sl 95 --tp 110 --thesis "..." [--score 0.7] [--regime range] [--dry-run]
  ./tb bybit <read|trade|testnet> close --sym SOL --reason manual [--dry-run] [--err TAG[,TAG]] [--rule-followed yes|no|partial] [--lesson "..."]
  ./tb bybit <trade|testnet> open --sym SOL --side long --limit-price 140.5 --sl 139 --tp 144 --thesis "..." [--ttl 60] [--cancel-above X] [--cancel-below Y] [--snap] [--idea I7]
                                              ждущий post-only лимит на уровне (venues/bybit_limit.py), стоп и тейк встроены в ордер
  ./tb bybit <trade|testnet> open ... --via-plan             то же, но не прямым ордером: план (тег legacy) уходит в inbox исполнителя v2 (venues/planlink.py);
                                              исполнитель не запущен -> ошибка с подсказкой, на прямой ордер НЕ падает
  ./tb bybit <trade|testnet> pending | cancel ID | cancel --all-pending         ждущие лимиты: список и отмена
  ./tb bybit <trade|testnet> sync                                                 закрывает сделки по closed-pnl и обрабатывает ждущие лимиты (заполнение -> журнал, ttl -> отмена)
  ./tb bybit read klines [--top 80] [--interval 5] [--days 40] [--out data/klines]   публичные свечи с объёмом для исследований (marketdata/klines.py)
  ./tb bybit read levels [--symbols BTC,ETH] [--write]   структура рынка для досье мозга: режим, ATR, границы суток/недели, уровни (marketdata/structure.py)
  ./tb bybit read scan [--watch] [--top 10] [--min-turnover 5e6] [--range-pct 0.8] [--max-seconds S]   сканер горячих монет по всему рынку (marketdata/hotscan.py)
  ./tb bybit <read|trade|testnet> watch --symbols SOLUSDT [--above X] [--below Y] [--move-pct P] [--private] [--max-seconds S]   события WebSocket (venues/bybit_stream.py)
Площадка: BYBIT_ENV=testnet (по умолчанию) или mainnet; mainnet требует ещё и флаг --mainnet-confirm в команде.
Безопасность: клиент имеет белый список методов и путей; эндпоинтов вывода, перевода и управления ключами в нём нет вообще.
Размер и плечо дает guard.evaluate (сессия, портфель, лестница просадки, множитель по результатам); скрипт не придумывает свои.
Вход без позиции по символу (усреднения нет). Стоп и тейк ставятся вместе с ордером. Журнал: core/journal.py (mode=live на mainnet, testnet на тестовой сети).
Эндпоинты и формы ответов написаны по документации Bybit v5 из памяти, НЕ ПРОВЕРЕНО ВЖИВУЮ: сначала прогнать на testnet.
"""
import argparse
import calendar
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from tb.core import guard
from tb.core import journal
from tb.venues.base import Venue

BASES = {"testnet": "https://api-testnet.bybit.com", "mainnet": "https://api.bybit.com"}
TAKER_FEE = 0.001  # ФАКТ у пользователя (измерено 2026-10-27: круг 0.20% номинала), не публичные 0.055%; запасное значение, если биржа не вернула комиссию по исполнению
RECV_WINDOW = "10000"
ALLOWED = {
    ("GET", "/v5/market/time"), ("GET", "/v5/market/tickers"), ("GET", "/v5/market/kline"), ("GET", "/v5/market/instruments-info"), ("GET", "/v5/account/wallet-balance"),
    ("GET", "/v5/position/list"), ("GET", "/v5/order/realtime"), ("GET", "/v5/account/fee-rate"), ("GET", "/v5/position/closed-pnl"),
    ("GET", "/v5/order/history"),
    ("POST", "/v5/position/trading-stop"), ("POST", "/v5/order/amend"), ("GET", "/v5/execution/list"),   # WP1A: исполнитель v2 (venues/bybit_exec.py)
    ("POST", "/v5/position/set-leverage"), ("POST", "/v5/order/create"), ("POST", "/v5/order/cancel"), ("POST", "/v5/order/cancel-all"),
}


class BybitError(Exception):
    """Ошибка клиента. code: retCode ответа биржи (запрос дошёл и обработан); http: код HTTP, если ответ не 200; оба None = локальная ошибка."""

    def __init__(self, msg="", code=None, http=None):
        Exception.__init__(self, msg)
        self.code, self.http = code, http


def _http(method, url, headers, body):
    req = urllib.request.Request(url, data=body.encode() if body else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise BybitError("HTTP %s" % e.code, http=e.code)


class Client:
    def __init__(self, env="testnet", key=None, secret=None, transport=None, clock=time.time):
        if env not in BASES:
            raise BybitError("BYBIT_ENV должен быть testnet или mainnet")
        self.env, self.base = env, BASES[env]
        self.key, self.secret = key, secret
        self.transport, self.clock = transport or _http, clock
        self.offset_ms = None  # поправка к локальным часам по времени сервера Bybit

    def sync_time(self):
        resp = self.transport("GET", self.base + "/v5/market/time", {"User-Agent": "traider-bot/1"}, None)
        res = (resp or {}).get("result") or {}
        server_ms = int(res["timeNano"]) // 1000000 if res.get("timeNano") else int(res["timeSecond"]) * 1000
        self.offset_ms = server_ms - int(self.clock() * 1000)

    def sign(self, ts, payload):
        msg = "%s%s%s%s" % (ts, self.key, RECV_WINDOW, payload)
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def call(self, method, path, params=None, auth=True):
        if (method, path) not in ALLOWED:
            raise BybitError("метод %s %s не разрешён (белый список)" % (method, path))
        params = params or {}
        headers = {"Content-Type": "application/json", "User-Agent": "traider-bot/1"}
        url, body = self.base + path, None
        if method == "GET":
            qs = urllib.parse.urlencode(params)
            payload = qs
            if qs:
                url += "?" + qs
        else:
            body = json.dumps(params, separators=(",", ":"))
            payload = body
        if auth:
            if not self.key or not self.secret:
                raise BybitError("нет BYBIT_API_KEY / BYBIT_API_SECRET в окружении")
            if self.offset_ms is None:
                self.sync_time()
            ts = str(int(self.clock() * 1000) + self.offset_ms)
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                            "X-BAPI-SIGN": self.sign(ts, payload)})
        resp = self.transport(method, url, headers, body)
        if auth and isinstance(resp, dict) and resp.get("retCode") == 10002 and not getattr(self, "_retried", False):
            self._retried = True  # один повтор после пересинхронизации часов
            try:
                self.sync_time()
                return self.call(method, path, params, auth)
            finally:
                self._retried = False
        if not isinstance(resp, dict) or resp.get("retCode") != 0:
            code = resp.get("retCode") if isinstance(resp, dict) else "?"
            msg = resp.get("retMsg") if isinstance(resp, dict) else str(resp)[:80]
            raise BybitError("Bybit retCode=%s: %s" % (code, msg), code=code if isinstance(code, int) else None)
        return resp.get("result", {})


def step_floor(x, step):
    d = (Decimal(str(x)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step))
    return d


def step_ceil(x, step):
    d = (Decimal(str(x)) / Decimal(str(step))).to_integral_value(rounding=ROUND_UP) * Decimal(str(step))
    return d


def fmt(d):
    s = format(d.normalize(), "f")
    return s


def instrument(c, sym):
    r = c.call("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym + "USDT"}, auth=False)
    it = r["list"][0]
    lot, pf, lv = it["lotSizeFilter"], it["priceFilter"], it["leverageFilter"]
    return {"min_qty": lot["minOrderQty"], "qty_step": lot["qtyStep"], "min_notional": lot.get("minNotionalValue", "5"),
            "tick": pf["tickSize"], "max_leverage": float(lv["maxLeverage"])}


def last_price(c, sym):
    r = c.call("GET", "/v5/market/tickers", {"category": "linear", "symbol": sym + "USDT"}, auth=False)
    return float(r["list"][0]["lastPrice"])


def quote(c, sym):
    """(bid, ask, last) из тикера."""
    it = c.call("GET", "/v5/market/tickers", {"category": "linear", "symbol": sym + "USDT"}, auth=False)["list"][0]
    return _f(it.get("bid1Price")), _f(it.get("ask1Price")), _f(it.get("lastPrice"))


FINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


def order_state(c, sym, oid):
    r = c.call("GET", "/v5/order/realtime", {"category": "linear", "symbol": sym + "USDT", "orderId": oid})
    o = (r.get("list") or [{}])[0]
    return {"status": o.get("orderStatus"), "qty": _f(o.get("cumExecQty")), "avg": _f(o.get("avgPrice")), "fee": _f(o.get("cumExecFee"))}


def get_position(c, sym):
    r = c.call("GET", "/v5/position/list", {"category": "linear", "symbol": sym + "USDT"})
    for p in r.get("list", []):
        if float(p.get("size") or 0) > 0:
            return p
    return None


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def fee_rate(c, sym):
    """Фактический тариф аккаунта по символу (только чтение): доли за сторону. Формат ответа из документации Bybit v5, НЕ ПРОВЕРЕНО ВЖИВУЮ."""
    r = c.call("GET", "/v5/account/fee-rate", {"category": "linear", "symbol": sym + "USDT"})
    it = (r.get("list") or [{}])[0]
    taker, maker = _f(it.get("takerFeeRate")), _f(it.get("makerFeeRate"))
    return {"symbol": sym + "USDT", "taker": taker, "maker": maker, "taker_pct": round(100 * taker, 4),
            "maker_pct": round(100 * maker, 4), "env": c.env}


def wallet(c, account_type="UNIFIED"):
    r = c.call("GET", "/v5/account/wallet-balance", {"accountType": account_type})
    acc = r["list"][0] if r.get("list") else {}
    coins, usdt_free = {}, None
    for x in acc.get("coin") or []:
        try:
            if _f(x.get("walletBalance")) != 0:
                coins[x["coin"]] = _f(x["walletBalance"])
            if x.get("coin") == "USDT":
                # В режиме Isolated Margin totalAvailableBalance = 0: считаем по монете (баланс минус заблокированное и маржа позиций/ордеров)
                usdt_free = max(0.0, _f(x.get("walletBalance")) - _f(x.get("locked")) - _f(x.get("totalPositionIM")) - _f(x.get("totalOrderIM")))
        except (KeyError, TypeError):
            pass
    free = _f(acc.get("totalAvailableBalance"))
    if free <= 0 and usdt_free is not None:
        free = usdt_free
    return {"equity": _f(acc.get("totalEquity")), "free": free, "coins": coins}


def wallet_all(c):
    """Баланс по типам счёта: монеты могли попасть не в Unified."""
    out = {}
    for t in ("UNIFIED", "CONTRACT"):
        try:
            out[t] = wallet(c, t)
        except (BybitError, KeyError, IndexError) as e:
            out[t] = {"error": str(e)}
    return out


def fill_of(c, sym, order_id, fallback):
    """Средняя цена исполнения ордера; при неудаче цена последней сделки."""
    try:
        r = c.call("GET", "/v5/order/realtime", {"category": "linear", "symbol": sym + "USDT", "orderId": order_id})
        o = r["list"][0]
        px = float(o.get("avgPrice") or 0)
        return (px if px > 0 else fallback), float(o.get("cumExecFee") or 0)
    except (BybitError, KeyError, IndexError, ValueError):
        return fallback, None


def journal_id(c, sym):
    """Уникальный id записи журнала: с секундами; при совпадении суффикс (иначе новая запись затирает старую, в т. ч. закрытую)."""
    base = "BB-%s-%s-%s" % (c.env[:1].upper(), sym, time.strftime("%m%d-%H%M%S", time.gmtime()))
    ids, n, jid = journal._load()[0], 1, base
    while jid in ids:
        n += 1
        jid = "%s-%d" % (base, n)
    return jid


def refuse_if_pending(sym, env):
    """Вход по рынку или лимитом по символу, где ждёт лимит, превратился бы в усреднение при его заполнении."""
    from tb.core import pending
    if pending.orders(env=env, sym=sym):
        raise BybitError("по %s уже есть ждущий лимит: сначала ./tb bybit ... cancel (усреднение запрещено)" % sym)


def cmd_open(c, a, sleep=time.sleep, clock=time.time):
    if c.env == "mainnet" and not a.mainnet_confirm:
        raise BybitError("mainnet: нужен флаг --mainnet-confirm")
    if getattr(a, "via_plan", False):
        return open_via_plan(c, a)
    if getattr(a, "limit_price", None) is not None:
        from tb.venues import bybit_limit
        return bybit_limit.open_limit(c, a, sleep, clock)
    refuse_if_pending(a.sym.upper(), c.env)
    if getattr(a, "exec", "taker") == "maker":
        return open_maker(c, a, sleep, clock)
    sym = a.sym.upper()
    if get_position(c, sym):
        raise BybitError("по %s уже есть позиция: усреднение запрещено" % sym)
    px = last_price(c, sym)
    ins = instrument(c, sym)
    ns = argparse.Namespace(sym=sym, side=a.side, entry=px, sl=a.sl, tp=a.tp, equity=None, free=None, score=a.score,
                            regime=a.regime, min_quote=0.0, contract_max_lev=ins["max_leverage"], profile=None,
                            no_session=False)
    g = guard.evaluate(ns)
    if not g["ok"]:
        return {"ok": False, "stage": "guard", "reasons": g["reasons"], "ctx": g.get("ctx")}
    lev, margin = g["leverage"], g["margin"]
    qty = step_floor(Decimal(str(margin * lev / px)), ins["qty_step"])
    notional = float(qty) * px
    if qty < Decimal(ins["min_qty"]) or notional < float(ins["min_notional"]):
        need = max(float(ins["min_qty"]) * px, float(ins["min_notional"])) / lev
        return {"ok": False, "stage": "min_size", "reasons": ["qty %s < min %s (или номинал < %s): при плече %d нужна маржа около %.2f, guard разрешил %.2f"
                                                           % (fmt(qty), ins["min_qty"], ins["min_notional"], lev, need, margin)]}
    tick = ins["tick"]
    order = {"category": "linear", "symbol": sym + "USDT", "side": "Buy" if a.side == "long" else "Sell", "orderType": "Market",
             "qty": fmt(qty), "positionIdx": 0, "tpslMode": "Full", "stopLoss": fmt(step_floor(a.sl, tick)),
             "takeProfit": fmt(step_floor(a.tp, tick)), "slTriggerBy": "LastPrice", "tpTriggerBy": "LastPrice"}
    plan = {"leverage": lev, "margin": margin, "qty": fmt(qty), "notional": round(notional, 2), "order": order, "guard": g["ctx"]}
    if a.dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    try:
        c.call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym + "USDT",
                                                     "buyLeverage": str(lev), "sellLeverage": str(lev)})
    except BybitError as e:
        if "110043" not in str(e):  # «leverage not modified» не ошибка
            raise
    res = c.call("POST", "/v5/order/create", order)
    oid = res["orderId"]
    sleep(0.5)
    avg, fee_open = fill_of(c, sym, oid, px)
    sess = guard.session.active()
    ns_j = argparse.Namespace(sym=sym, side=a.side, entry=avg, lev=float(lev), margin=float(qty) * avg / lev, sl=a.sl, tp=a.tp,
                              thesis=a.thesis, news=a.news or "", regime=a.regime, tags="bybit,%s" % c.env,
                              mode="live" if c.env == "mainnet" else "testnet", id=journal_id(c, sym),
                              session=sess["id"] if sess else None, stype=guard.session.geometry_type(sess, getattr(a, "horizon", None)) if sess else None, venue="bybit", fee_open=fee_open)
    journal.cmd_open(ns_j)
    return {"ok": True, "order_id": oid, "avg_price": avg, "plan": plan, "journal_id": ns_j.id}


def open_via_plan(c, a, wait=15.0):
    """`open ... --via-plan`: собрать из аргументов план (тег legacy) и отправить исполнителю вместо прямого ордера. Размер, плечо, стоп и тейк на бирже,
    запись в журнал и управление дальше за исполнителем. Исполнитель не жив -> ошибка без отправки (молчаливого отката на прямой ордер нет).
    --dry-run: план и предпросмотр guard без отправки, исполнитель не нужен. Ответ как у `plan submit` плюс via_plan:true."""
    from tb.brain import plan_cli
    from tb.venues import planlink
    sym = a.sym.upper()
    st = planlink.executor_state(c.env)
    if not a.dry_run and not st["alive"]:
        return dict(planlink.not_running_error(c.env, st), via_plan=True)
    sess = guard.session.active()
    stype = guard.session.geometry_type(sess, getattr(a, "horizon", None))      # сессия-день: горизонт сделки задаёт --horizon (по умолчанию intraday)
    maker_px = ref = None
    try:
        if getattr(a, "limit_price", None) is None and getattr(a, "exec", "taker") == "maker":
            bid, ask, ref = quote(c, sym)
            maker_px = (bid, ask)
        else:
            ref = last_price(c, sym)
    except (BybitError, OSError, KeyError, IndexError, ValueError) as e:
        if maker_px is None and getattr(a, "limit_price", None) is None and getattr(a, "exec", "taker") == "maker":
            return {"ok": False, "via_plan": True, "stage": "quote", "error": "нет котировки для maker-входа: %s" % e}
        ref = None       # цена нужна только для ref_price/предпросмотра: исполнитель берёт свою
    sm = getattr(a, "size_mult", None)
    if sm is not None and not 0 < sm <= 1:
        return {"ok": False, "via_plan": True, "stage": "plan", "error": "--size-mult должен быть в (0, 1]"}
    cap = planlink.no_confirm_cap()
    plan = planlink.build_plan(a, ref, stype, maker_px, size_mult=min(sm, cap) if sm is not None else cap)
    _, errs, warns, _ = plan_cli.precheck(plan)
    items = lambda rows: [{k: r.get(k) for k in ("code", "path", "msg") if k in r} for r in rows]    # noqa: E731
    if errs:
        return {"ok": False, "via_plan": True, "stage": "plan", "errors": items(errs), "warnings": items(warns), "plan": plan}
    if a.dry_run:
        return {"ok": True, "via_plan": True, "dry_run": True, "plan": plan, "warnings": items(warns), "guard_preview": plan_cli.guard_preview(plan),
                "executor": "alive" if st["alive"] else "down"}
    res = planlink.submit_plan(plan, "legacy open --via-plan: %s" % a.thesis, wait)
    return dict(res, via_plan=True)


def open_maker(c, a, sleep=time.sleep, clock=time.time):
    """Лимитный post-only вход по лучшей цене стакана (maker), стоп прикреплён к позиции (по рынку), лимитный тейк reduce-only после исполнения.
    Не исполнилось за timeout -> отмена, сделки нет. Частичное исполнение -> берём исполненное. Эндпоинты по документации, НЕ ПРОВЕРЕНО ВЖИВУЮ."""
    if c.env == "mainnet" and not a.mainnet_confirm:
        raise BybitError("mainnet: нужен флаг --mainnet-confirm")
    sym = a.sym.upper()
    if get_position(c, sym):
        raise BybitError("по %s уже есть позиция: усреднение запрещено" % sym)
    bid, ask, last = quote(c, sym)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise BybitError("нет нормальной котировки (bid %s, ask %s)" % (bid, ask))
    ins = instrument(c, sym)
    tick, long_ = ins["tick"], a.side == "long"
    px = float(step_floor(bid, tick)) if long_ else float(step_ceil(ask, tick))   # пассивная цена: покупка по bid, продажа по ask
    ns = argparse.Namespace(sym=sym, side=a.side, entry=px, sl=a.sl, tp=a.tp, equity=None, free=None, score=a.score,
                            regime=a.regime, min_quote=0.0, contract_max_lev=ins["max_leverage"], profile=None,
                            no_session=False, exec="maker")
    g = guard.evaluate(ns)
    if not g["ok"]:
        return {"ok": False, "stage": "guard", "reasons": g["reasons"], "ctx": g.get("ctx")}
    lev, margin = g["leverage"], g["margin"]
    qty = step_floor(Decimal(str(margin * lev / px)), ins["qty_step"])
    if qty < Decimal(ins["min_qty"]) or float(qty) * px < float(ins["min_notional"]):
        need = max(float(ins["min_qty"]) * px, float(ins["min_notional"])) / lev
        return {"ok": False, "stage": "min_size", "reasons": ["qty %s < min %s: при плече %d нужна маржа около %.2f, guard разрешил %.2f"
                                                           % (fmt(qty), ins["min_qty"], lev, need, margin)]}
    side, opp = ("Buy", "Sell") if long_ else ("Sell", "Buy")
    entry = {"category": "linear", "symbol": sym + "USDT", "side": side, "orderType": "Limit", "price": fmt(Decimal(str(px))),
             "qty": fmt(qty), "timeInForce": "PostOnly", "positionIdx": 0, "tpslMode": "Full",
             "stopLoss": fmt(step_floor(a.sl, tick)), "slTriggerBy": "LastPrice"}
    tp_px = step_ceil(a.tp, tick) if long_ else step_floor(a.tp, tick)           # лимитный тейк не хуже заданного
    tp_order = {"category": "linear", "symbol": sym + "USDT", "side": opp, "orderType": "Limit", "price": fmt(tp_px),
                "timeInForce": "PostOnly", "reduceOnly": True, "positionIdx": 0}
    plan = {"exec": "maker", "leverage": lev, "margin": margin, "qty": fmt(qty), "notional": round(float(qty) * px, 2),
            "entry_order": entry, "tp_order": dict(tp_order, qty="<исполненное>"), "timeout_s": getattr(a, "timeout", 30),
            "cost_win_pct": g.get("cost_win_pct"), "net_rr": g.get("net_rr"), "guard": g["ctx"]}
    if a.dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    try:
        c.call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym + "USDT", "buyLeverage": str(lev), "sellLeverage": str(lev)})
    except BybitError as e:
        if "110043" not in str(e):
            raise
    cancel_open_orders(c, sym)   # позиции нет: старые висящие ордера (остатки прошлого тейка) снять до входа
    oid = c.call("POST", "/v5/order/create", entry)["orderId"]
    cancel_body = {"category": "linear", "symbol": sym + "USDT", "orderId": oid}
    try:
        deadline = clock() + float(getattr(a, "timeout", 30))
        while True:
            sleep(1.5)
            st = order_state(c, sym, oid)
            if st["status"] in FINAL_STATUSES:
                break
            if clock() >= deadline:
                try:
                    c.call("POST", "/v5/order/cancel", cancel_body)
                except BybitError:
                    pass                 # гонка: ордер мог исполниться между опросом и отменой; решает повторный опрос статуса
                for _ in range(3):
                    sleep(0.7)
                    st = order_state(c, sym, oid)
                    if st["status"] in FINAL_STATUSES:
                        break
                if st["status"] not in FINAL_STATUSES:
                    raise BybitError("ордер %s не отменился после таймаута (статус %s)" % (oid, st["status"]))
                break
    except Exception as e:               # сбой сети/ответа при живом ордере: снять ордер и громко сообщить (позиция могла открыться)
        try:
            c.call("POST", "/v5/order/cancel", cancel_body)
        except Exception:
            pass
        raise BybitError("ВНИМАНИЕ maker-вход %s: %s. Проверь вручную ./tb bybit ... positions: возможна позиция со стопом, но без тейка и журнала" % (oid, e))
    if st["qty"] <= 0:
        return {"ok": True, "filled": False, "order_id": oid, "status": st["status"],
                "reason": "post-only не исполнен за %ss (или отклонён биржей, статус %s): сделки нет" % (getattr(a, "timeout", 30), st["status"])}
    filled = Decimal(str(st["qty"]))
    avg = st["avg"] or px
    tp_err, tp_oid = None, None
    try:
        tp_oid = c.call("POST", "/v5/order/create", dict(tp_order, qty=fmt(filled)))["orderId"]
    except BybitError:
        try:   # post-only отклонён (цена уже за тейком): обычный reduce-only лимит исполнится сразу, это лучше, чем без тейка
            tp_oid = c.call("POST", "/v5/order/create", dict(tp_order, qty=fmt(filled), timeInForce="GTC"))["orderId"]
        except BybitError as e:
            tp_err = str(e)      # стоп уже стоит на позиции; тейк придётся поставить вручную или закрыть позицию
    sess = guard.session.active()
    ns_j = argparse.Namespace(sym=sym, side=a.side, entry=avg, lev=float(lev), margin=float(filled) * avg / lev, sl=a.sl, tp=a.tp,
                              thesis=a.thesis, news=a.news or "", regime=a.regime, tags="bybit,%s,maker%s" % (c.env, ",no_tp" if tp_err else ""),
                              mode="live" if c.env == "mainnet" else "testnet", id=journal_id(c, sym),
                              session=sess["id"] if sess else None, stype=guard.session.geometry_type(sess, getattr(a, "horizon", None)) if sess else None, venue="bybit", fee_open=st["fee"])
    journal.cmd_open(ns_j)
    out = {"ok": True, "filled": True, "order_id": oid, "avg_price": avg, "filled_qty": fmt(filled), "tp_order_id": tp_oid,
           "plan": plan, "journal_id": ns_j.id}
    if tp_err:
        out["warning"] = "ВНИМАНИЕ: ТЕЙК-ОРДЕР НЕ СОЗДАН (%s). Позиция %s %s стоит только со стопом: поставь тейк вручную или закрой (close)" % (tp_err, sym, fmt(filled))
    return out


def cancel_open_orders(c, sym):
    """Снять висящие ордера по символу (лимитный тейк и т. п.). Стоп позиции не затрагивается ТОЛЬКО с orderFilter=Order: без фильтра cancel-all
    на testnet снимал и стоп уровня позиции (проверено WP1A, docs/spec_v2/WP1A_testnet.md)."""
    try:
        c.call("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": sym + "USDT", "orderFilter": "Order"})
        return True
    except BybitError:
        return False


def cmd_sync(c, a=None, sleep=time.sleep, clock=time.time):
    """Сверка журнала с биржей: сначала ждущие лимитные входы (заполнение -> запись open в журнал, ttl/инвалидация -> отмена; bybit_limit.sync_pending),
    затем открытые в журнале сделки Bybit, по которым позиции уже нет, закрываются по данным closed-pnl (стоп или тейк сработали сами).
    Формат closed-pnl по документации, НЕ ПРОВЕРЕНО ВЖИВУЮ."""
    mode = "live" if c.env == "mainnet" else "testnet"
    from tb.venues import bybit_limit
    pend = bybit_limit.sync_pending(c, now=clock(), sleep=sleep)   # None, если ждущих заявок нет: ни одного лишнего запроса
    tr, order = journal._load()
    done, pending, plan_managed, warnings = [], [], [], []
    for jid in order:
        t = tr[jid]
        if t.get("venue") != "bybit" or t.get("ts_close") or t.get("mode") != mode:
            continue
        if get_position(c, t["sym"]):
            pending.append(jid)
            continue
        if t.get("plan_id"):      # сделка исполнителя v2: журнал закроет он сам (идемпотентно), ордера плана (лесенка) не трогаем
            from tb.venues import planlink
            st = planlink.executor_state(c.env)
            if st["alive"]:
                pending.append(jid)
                plan_managed.append({"id": jid, "plan_id": t["plan_id"], "note": "позиции на бирже нет: закрытие и запись close делает исполнитель"})
                continue
            warnings.append("%s: сделка плана %s, но исполнитель не запущен (%s): закрыто по closed-pnl как раньше (исполнитель при старте увидит закрытую запись журнала и не дублирует close)" % (jid, t["plan_id"], st["reason"]))
        cancel_open_orders(c, t["sym"])   # позиции нет (стоп/тейк сработал): снять оставшийся лимитный тейк
        opened_ms = int(calendar.timegm(time.strptime(t["ts_open"], "%Y-%m-%dT%H:%M:%SZ"))) * 1000
        rows = c.call("GET", "/v5/position/closed-pnl", {"category": "linear", "symbol": t["sym"] + "USDT", "limit": "20"}).get("list", [])
        rows = [r for r in rows if int(r.get("updatedTime") or r.get("createdTime") or 0) >= opened_ms - 15000]
        if not rows:
            pending.append(jid)
            continue
        r = max(rows, key=lambda x: int(x.get("updatedTime") or 0))
        exit_px = _f(r.get("avgExitPrice"))
        fees = _f(r.get("openFee")) + _f(r.get("closeFee"))
        reason, sg = "closed", (1 if t["side"] == "long" else -1)
        if t.get("tp") and sg * (exit_px - t["tp"]) >= -0.0007 * t["tp"]:
            reason = "tp"
        elif t.get("sl") and sg * (exit_px - t["sl"]) <= 0.002 * t["sl"]:   # включая проскальзывание за стоп
            reason = "sl"
        journal.cmd_close(argparse.Namespace(id=jid, exit=exit_px, reason=reason, funding=0.0, fee=TAKER_FEE,
                                             fees_usdt=fees if fees > 0 else None, note="bybit sync closed-pnl"))
        done.append({"id": jid, "exit": exit_px, "reason": reason, "fees": round(fees, 5)})
    out = {"ok": True, "closed": done, "still_open": pending}
    if pend is not None:
        out["pending"] = pend
    if plan_managed:
        out["plan_managed"] = plan_managed
    if warnings:
        out["warnings"] = warnings
    return out


def cmd_close(c, a, sleep=time.sleep):
    if c.env == "mainnet" and not a.mainnet_confirm:
        raise BybitError("mainnet: нужен флаг --mainnet-confirm")
    sym = a.sym.upper()
    plan_warn = None
    prec = _open_plan_record(c, sym)
    if prec is not None:
        from tb.venues import planlink
        st = planlink.executor_state(c.env)
        snap = planlink.snapshot() if st["alive"] else None
        if st["alive"] and (snap is None or planlink.owns_position(c.env, sym, prec[1]["plan_id"])):
            return close_via_plan(c, a, prec[0], prec[1])
        plan_warn = ("сделка плана %s, но %s: закрываю напрямую, как раньше (стоп на бирже остаётся до закрытия). Исполнитель на сверке увидит "
                     "закрытую запись журнала и не продублирует close" % (prec[1]["plan_id"], "исполнитель не запущен (%s)" % st["reason"] if not st["alive"]
                                                                    else "исполнитель не владеет этой позицией"))
    from tb.venues import bybit_limit
    if not a.dry_run:
        bybit_limit.sync_pending(c, sleep=sleep, only_sym=sym, force="close")   # заполненный лимит -> журнал; ещё ждущий -> отмена (иначе заполнится после закрытия)
    pos = get_position(c, sym)
    if not pos:
        return {"ok": False, "reasons": ["нет открытой позиции по %s" % sym]}
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    order = {"category": "linear", "symbol": sym + "USDT", "side": side, "orderType": "Market", "qty": pos["size"],
             "reduceOnly": True, "positionIdx": 0}
    if a.dry_run:
        return {"ok": True, "dry_run": True, "order": order}
    cancelled = cancel_open_orders(c, sym)       # лимитный тейк (maker-вход) снять ДО закрытия, иначе он останется висеть
    px = last_price(c, sym)
    oid = c.call("POST", "/v5/order/create", order)["orderId"]
    sleep(0.5)
    avg, fee_close = fill_of(c, sym, oid, px)
    tr, order_ids = journal._load()
    jid = next((i for i in reversed(order_ids) if tr[i].get("venue") == "bybit" and tr[i]["sym"] == sym and tr[i].get("mode") == ("live" if c.env == "mainnet" else "testnet") and not tr[i].get("ts_close")), None)
    if jid:
        fo = tr[jid].get("fee_open")
        fees_actual = (fo + fee_close) if (fo is not None and fee_close is not None) else None
        journal.cmd_close(argparse.Namespace(id=jid, exit=avg, reason=a.reason, funding=0.0, fee=TAKER_FEE,
                                             fees_usdt=fees_actual, note="bybit %s" % c.env, err=getattr(a, "err", None),
                                             rule_followed=getattr(a, "rule_followed", None), lesson=getattr(a, "lesson", None)))
    return {"ok": True, "order_id": oid, "avg_price": avg, "journal_id": jid, "orders_cancelled": cancelled,
            **({} if cancelled else {"warning": "ВНИМАНИЕ: cancel-all не прошёл, проверь висящие ордера по %s" % sym}),
            **({"plan_warning": plan_warn} if plan_warn else {})}


def _open_plan_record(c, sym):
    """Открытая запись журнала этой площадки по монете, если это сделка плана исполнителя (есть plan_id): (id, запись) или None."""
    mode = "live" if c.env == "mainnet" else "testnet"
    tr, ids = journal._load()
    jid = next((i for i in reversed(ids) if tr[i].get("venue") == "bybit" and tr[i]["sym"] == sym and tr[i].get("mode") == mode and not tr[i].get("ts_close")), None)
    return (jid, tr[jid]) if jid and tr[jid].get("plan_id") else None


def close_via_plan(c, a, jid, rec):
    """Закрытие сделки плана: команда `close` исполнителю (как `plan close`). Ордера плана не снимаем и позицию в обход исполнителя не закрываем;
    запись close в журнале пишет исполнитель сам (после финализации по исполнениям биржи)."""
    from tb.venues import planlink
    out = {"via": "executor", "plan_id": rec["plan_id"], "journal_id": jid}
    if a.dry_run:
        return dict(out, ok=True, dry_run=True, note="команда close ушла бы исполнителю (plan close %s)" % rec["plan_id"])
    res = planlink.close_plan(rec["plan_id"], "legacy close: %s" % (getattr(a, "reason", None) or "manual"))
    out.update(ok=res.get("ok"), result=res)
    if res.get("ok") is None:
        out["note"] = "ответа исполнителя пока нет: команда в его очереди и будет выполнена, повторно её не отправляй (проверь ./tb exec status и ./tb bybit ... sync)"
    if any(getattr(a, k, None) for k in ("err", "rule_followed", "lesson")):
        out["warning"] = "--err/--rule-followed/--lesson не переданы исполнителю: разбор допиши после закрытия через ./tb journal"
    return out


def make_client():
    env = os.environ.get("BYBIT_ENV", "testnet")
    return Client(env, os.environ.get("BYBIT_API_KEY"), os.environ.get("BYBIT_API_SECRET"))


def main(argv):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("balance")
    sp.add_parser("positions")
    i = sp.add_parser("info")
    i.add_argument("--sym", required=True)
    fz = sp.add_parser("fees")
    fz.add_argument("--sym", required=True)
    o = sp.add_parser("open")
    o.add_argument("--sym", required=True)
    o.add_argument("--side", required=True, choices=["long", "short"])
    o.add_argument("--sl", type=float, required=True)
    o.add_argument("--tp", type=float, required=True)
    o.add_argument("--thesis", required=True)
    o.add_argument("--news", default="")
    o.add_argument("--score", type=float, default=0.5)
    o.add_argument("--regime", default="unknown")
    o.add_argument("--exec", choices=["taker", "maker"], default="taker", help="maker: лимитный post-only вход и лимитный тейк")
    o.add_argument("--horizon", choices=["scalp", "intraday", "swing", "position"], default=None, help="горизонт сделки в сессии-дне (стоп/RR/плечо по risk_profile.session_types; по умолчанию intraday)")
    o.add_argument("--timeout", type=float, default=30.0, help="секунд ждать исполнения лимитного входа, потом отмена")
    o.add_argument("--limit-price", type=float, help="ждущий post-only лимит на этой цене (уровень/ретест), может быть далеко от рынка; --exec и --timeout игнорируются")
    o.add_argument("--ttl", type=float, default=None, help="минут ждать заполнения лимита (по умолчанию 60, не дольше остатка сессии)")
    o.add_argument("--cancel-above", type=float, help="лимит: если цена дошла до этого уровня сверху без заполнения, отменить")
    o.add_argument("--cancel-below", type=float, help="лимит: если цена дошла до этого уровня снизу без заполнения, отменить")
    o.add_argument("--snap", action="store_true", help="лимит: если цена пересекает стакан, сместить к лучшей цене (иначе отказ)")
    o.add_argument("--idea", help="id идеи (./tb ideas): при постановке лимита watching, при заполнении taken")
    o.add_argument("--via-plan", action="store_true", dest="via_plan",
                   help="не прямым ордером: собрать план (тег legacy) и отправить исполнителю v2; исполнитель не запущен -> ошибка (без отката на прямой путь)")
    o.add_argument("--size-mult", type=float, default=None, dest="size_mult",
                   help="только с --via-plan: доля размера guard (0..1]; по умолчанию и максимум = exec_limits.entry.no_confirm_max_size_mult (0.5), "
                        "потому что вход без условия подтверждения иначе запрещён")
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--mainnet-confirm", action="store_true")
    sp.add_parser("sync")
    sp.add_parser("pending", help="ждущие лимитные входы: цена, расстояние до рынка, возраст, ttl")
    cn = sp.add_parser("cancel", help="снять ждущий лимит по id заявки (или orderId) либо все")
    cn.add_argument("id", nargs="?")
    cn.add_argument("--all-pending", action="store_true")
    od = sp.add_parser("orders")
    od.add_argument("--sym", required=True)
    k = sp.add_parser("close")
    k.add_argument("--sym", required=True)
    k.add_argument("--reason", default="manual")
    k.add_argument("--dry-run", action="store_true")
    k.add_argument("--mainnet-confirm", action="store_true")
    journal.add_post_args(k)       # разбор сделки при ручном закрытии: теги ошибок валидируются ДО отправки ордера (argparse)
    w = sp.add_parser("watch", help="демон событий для Monitor: одна JSON-строка на событие")
    w.add_argument("--symbols", default="", help="через запятую: SOLUSDT,ETHUSDT (без --symbols с --private берутся символы открытых позиций)")
    w.add_argument("--above", action="append", default=[], help="уровень сверху: 150 или SOLUSDT=150 (можно несколько)")
    w.add_argument("--below", action="append", default=[], help="уровень снизу: 140 или SOLUSDT=140 (можно несколько)")
    w.add_argument("--move-pct", type=float, default=0.0, help="событие, если цена ушла больше этого %% за --move-window")
    w.add_argument("--move-window", type=float, default=60.0)
    w.add_argument("--private", action="store_true", help="приватный сокет: ордера, исполнения, позиции")
    w.add_argument("--pending", action="store_true", help="ждущие лимиты: символы и уровни (лимит, инвалидация) из state/pending_orders.json, события pending_filled/pending_cancelled, обработка заявок")
    w.add_argument("--pending-every", type=float, default=30.0, help="с --pending: раз в сколько секунд обрабатывать заявки (заполнение, ttl, инвалидация)")
    w.add_argument("--down-after", type=float, default=30.0, help="секунд без связи до события ws_down")
    w.add_argument("--max-seconds", type=float, default=0.0, help="самозавершение (0 = работать до остановки)")
    from tb.marketdata import hotscan
    hotscan.add_args(sp.add_parser("scan", help="сканер горячих монет по всему рынку USDT-перпов (только чтение, marketdata/hotscan.py)"))
    from tb.marketdata import klines
    klines.add_args(sp.add_parser("klines", help="загрузка свечей с объёмом для исследований (только чтение, marketdata/klines.py)"))
    from tb.marketdata import structure
    structure.add_args(sp.add_parser("levels", help="структура рынка по свечам 5м/1ч: режим, ATR, дневные/недельные границы, ближайшие уровни (только чтение, marketdata/structure.py)"))
    a = ap.parse_args(argv)
    c = make_client()
    if a.cmd == "levels":
        return structure.main_levels(c, a)
    if a.cmd == "klines":
        return klines.main_klines(c, a)
    if a.cmd == "scan":
        return hotscan.main_scan(c, a)
    if a.cmd == "watch":
        from tb.venues import bybit_stream
        return bybit_stream.main_watch(c, a)
    try:
        if a.cmd == "balance":
            accs = wallet_all(c)
            w = accs.get("UNIFIED", {})
            out = {"equity": w.get("equity", 0.0), "free": w.get("free", 0.0), "env": c.env, "accounts": accs}
        elif a.cmd == "positions":
            out = c.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        elif a.cmd == "info":
            out = dict(instrument(c, a.sym.upper()), last=last_price(c, a.sym.upper()))
        elif a.cmd == "orders":
            out = c.call("GET", "/v5/order/realtime", {"category": "linear", "symbol": a.sym.upper() + "USDT", "openOnly": "0", "limit": "10"})
        elif a.cmd == "sync":
            out = cmd_sync(c)
        elif a.cmd == "pending":
            from tb.venues import bybit_limit
            out = bybit_limit.list_pending(c)
        elif a.cmd == "cancel":
            from tb.venues import bybit_limit
            out = bybit_limit.cmd_cancel(c, a)
        elif a.cmd == "fees":
            out = fee_rate(c, a.sym.upper())
        elif a.cmd == "open":
            out = cmd_open(c, a)
        else:
            out = cmd_close(c, a)
    except BybitError as e:
        out = {"ok": False, "error": str(e)}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok", True) else 1


class BybitVenue(Venue):
    """Реализация интерфейса Venue поверх функций этого модуля."""
    name = "bybit"

    def __init__(self, client=None):
        self.c = client or make_client()

    def open(self, sym, side, sl, tp, thesis, **kw):
        a = argparse.Namespace(sym=sym, side=side, sl=sl, tp=tp, thesis=thesis, news=kw.get("news", ""),
                               score=kw.get("score", 0.5), regime=kw.get("regime", "unknown"),
                               dry_run=kw.get("dry_run", False), mainnet_confirm=kw.get("mainnet_confirm", False))
        return cmd_open(self.c, a)

    def close(self, sym, reason="manual", **kw):
        a = argparse.Namespace(sym=sym, reason=reason, dry_run=kw.get("dry_run", False),
                               mainnet_confirm=kw.get("mainnet_confirm", False))
        return cmd_close(self.c, a)

    def positions(self):
        return self.c.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})

    def balance(self):
        w = wallet_all(self.c).get("UNIFIED", {})
        return {"equity": w.get("equity", 0.0), "free": w.get("free", 0.0), "env": self.c.env}


if __name__ == "__main__":
    # `./tb` запускает модуль через runpy как __main__: без псевдонима `from tb.venues import bybit` в bybit_limit/bybit_stream загрузил бы вторую копию
    # с другим классом BybitError, и `except BybitError` там не ловил бы ошибки клиента этой копии.
    import tb.venues
    sys.modules["tb.venues.bybit"] = tb.venues.bybit = sys.modules[__name__]
    sys.exit(main(sys.argv[1:]))
