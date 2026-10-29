"""Дописывает тики в data/ticks_<SYM>.csv в корне проекта.

Использование: ./tb collect ETH=2313.67 BTC=79960.4
Время берётся из системных часов (UTC). Ничего не торгует и в сеть не ходит.
"""
import os
import sys
import time

from tb import paths
from tb.marketdata import data


def main(argv):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    written = []
    for arg in argv:
        sym, _, raw = arg.partition("=")
        price = float(raw)
        if not sym or price <= 0:
            raise ValueError("bad arg: " + arg)
        path = paths.data("ticks_%s.csv" % sym.upper())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data.append_tick(path, now, price)
        written.append("%s=%s" % (sym.upper(), price))
    print(now, " ".join(written))


if __name__ == "__main__":
    main(sys.argv[1:])
