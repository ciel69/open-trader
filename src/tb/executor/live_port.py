"""LivePort: VenuePort поверх venues/bybit_exec.py (06 п. 6.2). Владелец: WP1A.

LivePort(client): client = venues/bybit.py::Client (testnet или mainnet). Порт ничего не повторяет сам, ничего не пишет в файлы и не печатает;
ключ в нём не используется напрямую (подпись внутри Client). Символы без квоты (`SOL`).

Таблица классификации ошибок (PortError.kind; code = retCode Bybit либо строковый код порта). Подтверждено/уточнено на testnet: docs/spec_v2/WP1A_testnet.md.
  ответ биржи, retCode != 0 (запрос обработан, эффекта нет)                       rejected, code=retCode
  10006, 10018, 10014 (лимит частоты), HTTP 429                                    rate
  10000, 10016, 10019, 20003 (сбой биржи; запрос мог пройти)                       unknown (для записи), network (для чтения)
  110017, 110025 в place_reduce_only/close_market и позиция по символу не найдена  position_gone (если позиция есть, то rejected, code=retCode)
  110043 при set_leverage                                                          успех (плечо не менялось)
  34040 при trading-stop                                                           успех, unchanged=True
  110001/20001 при cancel                                                          успех, gone=True (ордера уже нет)
  110001 при amend                                                                 rejected (ордера нет: сверить по link_id)
  110072 (дубль orderLinkId) при place_*                                           rejected (ордер с таким link_id уже есть: сверить order_by_link)
  таймаут, обрыв, мусор в ответе после отправки записи                            unknown (ордер МОГ создаться: сверять по link_id)
  DNS/отказ соединения (запрос не ушёл), любой сбой транспорта при чтении          network
  HTTP 401/403 (прокси/блокировка IP: запрос не обработан)                         network, code="HTTP_403"
  локальные отказы без вызова биржи                                                rejected: NO_STOP, STOP_NOT_TIGHTER, STOP_ON_WRONG_SIDE,
                                                                                   STOP_REMOVE_FORBIDDEN, MIN_QTY, BAD_ARGS, SIDE_MISMATCH
`unknown` для записи значит: вызывающий обязан проверить результат по link_id (order_by_link) или состоянию позиции, слепых повторов нет.
"""
import time

from tb.executor import ports
from tb.executor.ports import KIND_POSITION_GONE, KIND_REJECTED, PortError, VenuePort
from tb.venues import bybit_exec as X

INSTRUMENT_TTL_S = 3600.0


class LivePort(VenuePort):
    """Реальный порт Bybit. env: mainnet|testnet."""

    def __init__(self, client, clock=time.time):
        self.client = client
        self.env = getattr(client, "env", None)
        self.clock = clock
        self._ins = {}      # sym -> (ts, instrument)

    # ---- служебное ----
    def _run(self, fn, *args, **kw):
        write = kw.pop("_write", False)
        try:
            return fn(self.client, *args, **kw)
        except PortError:
            raise
        except Exception as e:
            raise X.to_port_error(e, write=write)

    def _gone_check(self, sym, err):
        """110017/110025 для reduce-only: позиции нет -> position_gone, иначе исходный отказ."""
        if err.code in X.GONE_CODES and err.kind == KIND_REJECTED:
            try:
                alive = X.positions_norm(self.client, sym, clock=self.clock)
            except Exception:
                return err
            if not alive:
                return PortError(KIND_POSITION_GONE, "позиции по %s нет (%s)" % (sym, err.msg), code=err.code)
        return err

    # ---- чтение ----
    def account(self):
        return self._run(X.account, clock=self.clock)

    def positions(self):
        return self._run(X.positions_norm, None, clock=self.clock)

    def open_orders(self, sym=None):
        return self._run(X.open_orders, sym)

    def order_by_link(self, sym, link_id):
        return self._run(X.order_by_link, sym, link_id)

    def executions(self, since_ms, sym=None):
        return self._run(X.executions, since_ms, sym, clock=self.clock)

    def closed_pnl(self, sym, since_ms):
        return self._run(X.closed_pnl, sym, since_ms, clock=self.clock)

    def instrument(self, sym):
        """Кэш 1 час. -> {"tick","qty_step","min_qty","min_notional","max_leverage"}"""
        key = X.base_of(sym)
        hit = self._ins.get(key)
        now = self.clock()
        if hit and now - hit[0] < INSTRUMENT_TTL_S:
            return dict(hit[1])
        ins = self._run(X.instrument, key)
        self._ins[key] = (now, ins)
        return dict(ins)

    def quote(self, sym):
        return self._run(X.quote, sym)

    def ticker(self, sym):
        """Дополнительно к контракту: {"bid","ask","last","mark","funding"} (REST-подстраховка WS-цены)."""
        return self._run(X.ticker, sym)

    def klines(self, sym, tf_min, limit=200):
        return self._run(X.klines, sym, tf_min, limit)

    # ---- запись ----
    def set_leverage(self, sym, lev):
        return self._run(X.set_leverage, sym, lev, _write=True)

    def place_entry(self, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last"):
        if sl is None or X._f(sl) <= 0:        # до любых сетевых вызовов (в т. ч. instrument)
            raise PortError(KIND_REJECTED, "вход без стопа запрещён", code=ports.CODE_NO_STOP)
        return self._run(X.place_entry, sym, side, qty, link_id, order_type, price=price, post_only=post_only, sl=sl, sl_trigger=sl_trigger,
                         ins=self._ins_for(sym), _write=True)

    def place_reduce_only(self, sym, side, qty, link_id, price=None, post_only=False):
        try:
            return self._run(X.place_reduce_only, sym, side, qty, link_id, price=price, post_only=post_only, ins=self._ins_for(sym), _write=True)
        except PortError as e:
            raise self._gone_check(sym, e)

    def amend_order(self, sym, link_id, price=None, qty=None):
        return self._run(X.amend_order, sym, link_id, price=price, qty=qty, ins=self._ins_for(sym), _write=True)

    def cancel_order(self, sym, link_id):
        return self._run(X.cancel_by_link, sym, link_id, _write=True)

    def cancel_all(self, sym, reduce_only_only=False):
        return self._run(X.cancel_all, sym, reduce_only_only, _write=True)

    def set_stop(self, sym, side, stop_price, trigger_by="last"):
        """Только строже; см. bybit_exec.tighten_stop. -> {"ok":True,"stop":float,"unchanged":bool}"""
        if stop_price is None or X._f(stop_price) <= 0:
            raise PortError(KIND_REJECTED, "снять стоп нельзя", code=ports.CODE_STOP_REMOVE)
        return self._run(X.tighten_stop, sym, side, stop_price, trigger_by=trigger_by, ins=self._ins_for(sym), _write=True)

    def close_market(self, sym, side, qty, link_id):
        try:
            return self._run(X.close_market, sym, side, qty, link_id, ins=self._ins_for(sym), _write=True)
        except PortError as e:
            raise self._gone_check(sym, e)

    def _ins_for(self, sym):
        """Инструмент из кэша (запрос при промахе); ошибка запроса -> PortError."""
        return self.instrument(sym)
