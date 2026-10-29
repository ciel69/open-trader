"""Интерфейсы биржи для исполнителя: VenuePort (REST-операции), MarketFeed (WS-потоки), PortError (06 п. 6.2-6.3).

Владелец: WP5 (создал WP0). Реализации: executor/live_port.py::LivePort (WP1), executor/paper_port.py::PaperPort (WP7),
tests/fakes/fake_port.py::FakePort (WP5). Общий контрактный тест: tests/test_port_contract.py (принимает любой VenuePort).

Все записи ЛОГИРУЕТ вызывающий через WAL; порт сам ничего не повторяет. Порты не потокобезопасны сами по себе: исполнитель вызывает
записи из одной торговой очереди. Сторона позиции: "long"|"short". Стоп только строже (long: вверх, short: вниз), снять нельзя.
"""

ENV_MAINNET, ENV_TESTNET, ENV_PAPER = "mainnet", "testnet", "paper"
ENVS = (ENV_MAINNET, ENV_TESTNET, ENV_PAPER)

# PortError.kind
KIND_REJECTED = "rejected"            # биржа/порт отказали (retCode != 0, нарушен инвариант порта)
KIND_UNKNOWN = "unknown"              # исход неизвестен: таймаут, обрыв после отправки (вызывающий сверяет по link_id)
KIND_RATE = "rate"                    # лимит частоты (10006/10018)
KIND_POSITION_GONE = "position_gone"  # позиции уже нет (110017, 110025)
KIND_NETWORK = "network"              # нет соединения, запрос не отправлен
ERROR_KINDS = (KIND_REJECTED, KIND_UNKNOWN, KIND_RATE, KIND_POSITION_GONE, KIND_NETWORK)

# PortError.code (строковые коды порта; числовые retCode Bybit кладутся в code как есть)
CODE_NO_STOP = "NO_STOP"                        # вход без стопа запрещён на уровне порта
CODE_STOP_NOT_TIGHTER = "STOP_NOT_TIGHTER"      # новый стоп не строже текущего (биржа не вызывалась)
CODE_STOP_ON_WRONG_SIDE = "STOP_ON_WRONG_SIDE"  # стоп сработал бы мгновенно / не с той стороны от last/mark
CODE_STOP_REMOVE = "STOP_REMOVE_FORBIDDEN"      # попытка снять стоп (<= 0)
PORT_CODES = (CODE_NO_STOP, CODE_STOP_NOT_TIGHTER, CODE_STOP_ON_WRONG_SIDE, CODE_STOP_REMOVE)

SIDES = ("long", "short")
ORDER_TYPES = ("market", "limit")
TRIGGER_BY = ("last", "mark")
KLINE_TFS = (1, 5, 15, 60)


class PortError(Exception):
    """Ошибка порта. kind: rejected|unknown|rate|position_gone|network; code: retCode Bybit или строковый код порта."""

    def __init__(self, kind, msg="", code=None):
        Exception.__init__(self, "%s%s: %s" % (kind, " [%s]" % code if code is not None else "", msg))
        self.kind = kind
        self.code = code
        self.msg = msg


class VenuePort(object):
    """Абстрактный порт биржи. Базовые методы поднимают NotImplementedError; env переопределяется реализацией."""

    env = None  # "mainnet|testnet|paper"

    # ---- чтение (ничего не меняют) ----
    def account(self):
        """-> {"equity":float,"free":float,"ts":epoch}"""
        raise NotImplementedError

    def positions(self):
        """-> [{"sym","side":"long|short","size","avg","mark","sl","tp","liq","upnl","lev","margin","ts"}]; sl/tp=0.0, если нет"""
        raise NotImplementedError

    def open_orders(self, sym=None):
        """-> [{"sym","link_id","order_id","side":"Buy|Sell","type":"limit|market","price","qty","filled","reduce_only","status","ts"}]"""
        raise NotImplementedError

    def order_by_link(self, sym, link_id):
        """realtime, затем history -> словарь как у open_orders или None"""
        raise NotImplementedError

    def executions(self, since_ms, sym=None):
        """-> [{"exec_id","sym","side","price","qty","fee","is_maker","link_id","order_id","stop_type","exec_type","closed_size","pnl","ts_ms"}] по возрастанию времени"""
        raise NotImplementedError

    def closed_pnl(self, sym, since_ms):
        """-> [{"sym","side","qty","entry","exit","pnl","open_fee","close_fee","ts_ms"}]"""
        raise NotImplementedError

    def instrument(self, sym):
        """-> {"tick":str,"qty_step":str,"min_qty":str,"min_notional":str,"max_leverage":float} (кэш 1 ч)"""
        raise NotImplementedError

    def quote(self, sym):
        """-> (bid, ask, last)"""
        raise NotImplementedError

    def klines(self, sym, tf_min, limit=200):
        """-> [(start_ms,o,h,l,c,vol,turnover)] по возрастанию времени; последняя может быть незакрытой"""
        raise NotImplementedError

    # ---- запись (каждая операция логируется вызывающим через WAL; сама ничего не повторяет) ----
    def set_leverage(self, sym, lev):
        """110043 «not modified» = успех"""
        raise NotImplementedError

    def place_entry(self, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last"):
        """side "long|short"; order_type "market|limit"; sl ОБЯЗАТЕЛЕН: без него PortError(rejected, code="NO_STOP"); tpslMode Full"""
        raise NotImplementedError

    def place_reduce_only(self, sym, side, qty, link_id, price=None, post_only=False):
        """side = сторона ЗАКРЫТИЯ ("sell" для long); price=None -> market; всегда reduceOnly=True"""
        raise NotImplementedError

    def amend_order(self, sym, link_id, price=None, qty=None):
        raise NotImplementedError

    def cancel_order(self, sym, link_id):
        raise NotImplementedError

    def cancel_all(self, sym, reduce_only_only=False):
        raise NotImplementedError

    def set_stop(self, sym, side, stop_price, trigger_by="last"):
        """side позиции. Читает ТЕКУЩИЙ стоп; не строже (long: new < cur, short: new > cur) -> PortError(rejected, code="STOP_NOT_TIGHTER")
        БЕЗ вызова биржи; снять (<=0) нельзя; не с той стороны от mark/last -> PortError(rejected, "STOP_ON_WRONG_SIDE").
        -> {"ok":True,"stop":float,"unchanged":bool}"""
        raise NotImplementedError

    def close_market(self, sym, side, qty, link_id):
        """reduce-only market; qty=None -> вся позиция"""
        raise NotImplementedError


class MarketFeed(object):
    """Абстрактный поток рыночных данных. Обработчики регистрируются заранее (присваиванием атрибутов)."""

    on_ticker = None   # fn(sym, last, bid, ask, mark, funding, ts)
    on_kline = None    # fn(sym, tf_min, start_ms, o, h, l, c, vol, turnover, confirm)
    on_private = None  # fn(ev) ev: {"kind":"execution"|"order"|"position", ...поля порта..., "link_id"}
    on_state = None    # fn(channel "public|private", up: bool, down_s: float)

    def subscribe(self, symbols, tfs):
        """Динамически; tfs из {1,5,15,60}."""
        raise NotImplementedError

    def unsubscribe(self, symbols):
        raise NotImplementedError
