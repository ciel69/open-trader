"""Сервис `ctl` под супервизором: `python -m tb.control.service run` (через `./tb control run`). Владелец: WP12.

Цикл 0.5 с: читает `state/control/_inbox`, исполняет команды (handlers.Ctl), следит за KILL и авто-понижением live, пишет `state/control/status.json` (каждый цикл,
не реже раза в 5 с при простое) и heartbeat `runtime/platform/ctl.hb.json`. Второй экземпляр на одном корне отказан блокировкой (код 4).
Падение ctl не останавливает торговые сервисы и не мешает аварийной остановке из дашборда (KILL создаёт сам дашборд).
"""
import argparse
import os
import sys
import time

from tb import paths
from tb.control import ctlstate, handlers
from tb.core.filelock import FileLock
from tb.memory import tick as memtick
from tb.platform import services as S

EXIT_ALREADY = 4
LOOP_S = 0.5


def _start_notify(root, clock):
    """Поток подписчика шины для уведомлений ОС (tb.lifecycle.notifyd); нет модуля или сбой -> без уведомлений, ctl работает."""
    try:
        from tb.lifecycle import notifyd
        return notifyd.NotifyThread(root, clock=clock).start()
    except Exception:
        return None


def run(root=None, max_seconds=None, sleep=time.sleep, clock=time.time):
    root = root or paths.root()
    os.makedirs(paths.control_dir(root=root), exist_ok=True)
    try:
        lock = FileLock(paths.control_dir("ctl.lock", root=root), wait=0.2)
        lock.__enter__()
    except TimeoutError:
        print("ctl уже запущен на этом корне", file=sys.stderr)
        return EXIT_ALREADY
    ctl = handlers.Ctl(root, clock=clock, sleep=sleep)
    t0, last_clean = clock(), 0.0
    nth = _start_notify(root, clock)
    try:
        while max_seconds is None or clock() - t0 < max_seconds:
            lock.touch()
            try:
                ctl.poll_once()
                ctl.watch()
                ctl.write_status()
                memtick.tick(root, clock())      # писатель памяти случаев (WP15b): закрытия, пропуски, итоги идей; раз в tick.interval_s, сбой проглатывается
            except Exception as e:
                print("ctl: сбой цикла: %s" % type(e).__name__, file=sys.stderr, flush=True)
            S.write_hb("ctl", {"state": ctl.platform()["state"]}, root=root, now=clock())
            if clock() - last_clean > 3600:
                last_clean = clock()
                ctl.cleanup_outbox()
            sleep(LOOP_S)
    except KeyboardInterrupt:
        pass
    finally:
        if nth is not None:
            nth.stop()
        lock.__exit__(None, None, None)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb control run", description="сервис управления (под супервизором)")
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--max-seconds", type=float, default=None, dest="max_seconds")
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    return run(paths.root(), a.max_seconds)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
