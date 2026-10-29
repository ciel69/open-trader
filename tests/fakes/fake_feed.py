"""FakeFeed: скриптованный MarketFeed для тестов (06 п. 6.3, 6.12). Заготовка WP0; WP5 расширяет."""
from tb.executor.ports import MarketFeed


class FakeFeed(MarketFeed):
    def __init__(self):
        self.on_ticker = None
        self.on_kline = None
        self.on_private = None
        self.on_state = None
        self.subs = {}          # sym -> set(tf)

    def subscribe(self, symbols, tfs):
        for s in symbols:
            self.subs.setdefault(s, set()).update(tfs)

    def unsubscribe(self, symbols):
        for s in symbols:
            self.subs.pop(s, None)

    def emit_ticker(self, sym, last, bid=None, ask=None, mark=None, funding=0.0, ts=0.0):
        if self.on_ticker:
            self.on_ticker(sym, last, bid if bid is not None else last, ask if ask is not None else last, mark if mark is not None else last, funding, ts)

    def emit_kline(self, sym, tf_min, start_ms, o, h, l, c, vol=0.0, turnover=0.0, confirm=False):
        if self.on_kline:
            self.on_kline(sym, tf_min, start_ms, o, h, l, c, vol, turnover, confirm)

    def emit_private(self, ev):
        if self.on_private:
            self.on_private(ev)

    def set_state(self, channel, up, down_s=0.0):
        if self.on_state:
            self.on_state(channel, up, down_s)
