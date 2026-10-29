"""Общий интерфейс исполнения для площадок. Новая биржа = класс-наследник Venue в venues/<имя>.py.

Все методы возвращают JSON-совместимый dict. Размер и плечо площадка НЕ придумывает: их даёт core.guard.
"""


class Venue:
    name = "base"

    def open(self, sym, side, sl, tp, thesis, **kw):
        """Открыть позицию (стоп и тейк вместе с ордером). -> {"ok": bool, ...}"""
        raise NotImplementedError

    def close(self, sym, reason="manual", **kw):
        """Закрыть позицию по символу. -> {"ok": bool, ...}"""
        raise NotImplementedError

    def positions(self):
        """Открытые позиции площадки."""
        raise NotImplementedError

    def balance(self):
        """-> {"equity": float, "free": float, ...}"""
        raise NotImplementedError
