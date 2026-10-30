"""Реестр адаптеров бирж: фабрика портов (VenuePort), потоков (MarketFeed) и клиентов по id биржи (WP-X). Единственное место, где id биржи превращается в реализацию.

Настройки бирж (включена ли, режим, лимиты) лежат в `config/exchanges.json` (tb.core.exchcfg, без биржевого кода); здесь только то, что реально реализовано:
`register(adapter, factory)`; сейчас зарегистрирован `bybit` (LivePort для trade/testnet, PaperPort на публичных потоках для paper). Остальные записи реестра (okx, binance...) заготовки:
запрос порта для них даёт ExchangeNotImplemented с понятным текстом. Как добавить биржу: docs/EXCHANGES.md (интерфейс VenuePort + MarketFeed, контрактный тест PortContract).
factory(profile, root, selftest, venue_id) -> Bundle(port, feed, saver, client), где profile = trade|testnet|paper. Ключи читает launcher до запуска процесса, не фабрика.
"""
import os
import sys

from tb import paths
from tb.core import exchcfg

PROFILES = ("trade", "testnet", "paper")
ENV_OF = {"trade": "mainnet", "testnet": "testnet", "paper": "paper"}


class ExchangeNotImplemented(RuntimeError):
    pass


class Bundle(object):
    """Результат фабрики: порт (VenuePort), фид (MarketFeed), функция сохранения paper-состояния (или None), клиент биржи (или None)."""

    def __init__(self, port, feed, saver=None, client=None, venue_id=None):
        self.port, self.feed, self.saver, self.client, self.venue_id = port, feed, saver, client, venue_id


_FACTORIES = {}


def register(adapter, factory):
    _FACTORIES[adapter] = factory
    exchcfg.IMPLEMENTED.add(adapter)


def unregister(adapter):
    _FACTORIES.pop(adapter, None)
    if adapter != "bybit":
        exchcfg.IMPLEMENTED.discard(adapter)


def adapters():
    return sorted(_FACTORIES)


def adapter_of(exchange_id, root=None):
    """Адаптер биржи из реестра config/exchanges.json; неизвестная биржа -> ExchangeNotImplemented."""
    e = exchcfg.load(root)["exchanges"].get(exchange_id)
    if e is None:
        raise ExchangeNotImplemented("биржа %s не описана в config/exchanges.json" % str(exchange_id)[:20])
    return e["adapter"]


def make_bundle(exchange_id, profile, root=None, selftest=False):
    """-> Bundle для биржи exchange_id и профиля trade|testnet|paper. Нереализованная биржа или адаптер без фабрики -> ExchangeNotImplemented."""
    if profile not in PROFILES:
        raise ValueError("профиль: %s" % "|".join(PROFILES))
    adapter = adapter_of(exchange_id, root)
    fac = _FACTORIES.get(adapter)
    if fac is None:
        raise ExchangeNotImplemented("биржа %s (адаптер %s) не реализована: см. docs/EXCHANGES.md, как добавить" % (exchange_id, adapter))
    b = fac(profile, root, selftest, exchange_id)
    b.venue_id = exchange_id
    return b


def make_client(exchange_id, env="mainnet", root=None):
    """REST-клиент биржи (для сервисов чтения, например sync). Ключ грузит launcher в окружение процесса; здесь только объект клиента."""
    adapter = adapter_of(exchange_id, root)
    if adapter == "bybit":
        from tb.venues import bybit
        if env not in (None, "mainnet", "testnet"):
            raise ValueError("env: mainnet|testnet")
        if env:
            os.environ.setdefault("BYBIT_ENV", env)
        return bybit.make_client()
    raise ExchangeNotImplemented("клиент биржи %s не реализован" % exchange_id)


# ---------------------------------------------------------------- bybit
def _bybit(profile, root, selftest, venue_id):
    from tb.executor.live_port import LivePort
    from tb.executor.transport import make_transport
    from tb.venues import bybit, bybit_wsx
    if profile == "paper":
        from tb.core import portfolio
        from tb.executor.daemon import NullFeed
        from tb.executor.paper_port import PaperPort
        instr_fn, feed, client = None, NullFeed(), None
        if not selftest:
            client = bybit.Client("mainnet", transport=make_transport())
            lp = LivePort(client)
            instr_fn = lp.instrument
            feed = bybit_wsx.BybitFeed(client, private=False)
        eq = 0.0
        try:
            eq = portfolio.total(portfolio.load())
        except Exception:
            pass
        port = PaperPort.from_config(equity=eq if eq > 0 else 40.0, root=root, instrument_fn=instr_fn, venue=venue_id)
        sp = paths.paper_port_file(root, venue_id)
        if os.path.exists(sp):
            try:
                port.load(sp)
            except Exception:
                pass
        return Bundle(port, feed, (lambda: port.save(sp)), client)
    env = ENV_OF[profile]
    if os.environ.get("BYBIT_ENV") not in (None, "", env):
        raise SystemExit("BYBIT_ENV=%s не совпадает с профилем %s (%s): отказ" % (os.environ.get("BYBIT_ENV"), profile, env))
    os.environ["BYBIT_ENV"] = env
    client = bybit.make_client()
    client.transport = make_transport()
    if not client.key or not client.secret:
        raise SystemExit("нет ключа в окружении: запускайте через ./tb (launcher грузит профиль)")
    try:        # ордерные запросы по WebSocket Trade API, если включено (exec_limits.ws_trade.enabled); иначе всё идёт по REST keep-alive как раньше
        from tb.core import exec_cfg
        from tb.executor import ws_trade
        ws_trade.wrap_client(client, exec_cfg.load(root).get("ws_trade"), env)
    except Exception as e:      # WS необязателен: без него работает REST, но причину не прячем
        print("ws_trade: не подключён (%s: %s), ордера идут по REST" % (type(e).__name__, str(e)[:80]), file=sys.stderr)
    return Bundle(LivePort(client), bybit_wsx.BybitFeed(client), None, client)


register("bybit", _bybit)
