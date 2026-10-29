"""Сервис context платформы: `python -m tb.platform.contextsvc run` (запускает супервизор). Владелец: WP11-B.

Обёртка над marketdata/context.py БЕЗ изменения его логики: деривативы (funding, open interest, long/short), DVOL (Deribit), индекс страха и жадности,
глобальные данные CoinGecko и макро-блэкаут по локальному календарю. Результат пишет сам poll_cycle в runtime/context.json (свежесть этого файла
проверяет preflight P10). Публичные источники, ключей нет. Сбой одного источника = ok:false в файле (degraded_sources), сбой всего цикла (сеть,
диск) логируется и не роняет сервис: следующий цикл через poll_s. Частота цикла: config/platform.json -> "context".poll_s (источники опрашиваются
по своим interval_s из config/context_config.json). Heartbeat: runtime/platform/context.hb.json на каждом цикле.
"""
import argparse
import os
import signal
import sys
import time

from tb import paths
from tb.marketdata import context as C
from tb.platform import services as S


def cycle(cfg, root, cache, now=None):
    """Один цикл с защитой: -> (контекст | None, текст ошибки | None)."""
    try:
        return C.poll_cycle(cfg, root, cache, now), None
    except Exception as e:  # noqa: BLE001 — сервис не падает из-за источника или диска
        return None, type(e).__name__


def run(root, stop=lambda: False, max_seconds=0.0, clock=time.time, sleep=time.sleep, poll_s=None, log=None):
    log = log or (lambda m: print(m, flush=True))
    pcfg = S.load_cfg(root)
    period = float(poll_s if poll_s is not None else (pcfg.get("context") or {}).get("poll_s") or 15.0)
    try:
        cfg = C.load_config(paths.config("context_config.json", root=root))
    except (OSError, ValueError) as e:
        log("context: config/context_config.json не прочитан (%s): сервис ждёт и повторяет" % type(e).__name__)
        cfg = None
    cache = {}
    t_end = clock() + max_seconds if max_seconds else None
    last_err, n = None, 0
    while not stop():
        if cfg is None:
            try:
                cfg = C.load_config(paths.config("context_config.json", root=root))
            except (OSError, ValueError):
                cfg = None
        ctx, err = (None, "config") if cfg is None else cycle(cfg, root, cache, clock())
        n += 1
        if err != last_err:
            log("context: %s" % ("цикл не удался (%s)" % err if err else "цикл в норме"))
            last_err = err
        S.write_hb("context", {"cycles": n, "ok": err is None, "error": err, "risk_off": bool(ctx and ctx.get("risk_off")),
                               "degraded": list((ctx or {}).get("degraded_sources") or [])[:8]}, root=root, now=clock())
        if t_end is not None and clock() >= t_end:
            break
        sleep(period)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb.platform.contextsvc")
    ap.add_argument("cmd", choices=("run",))
    ap.add_argument("--max-seconds", type=float, default=0.0, dest="max_seconds")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    flag = {"stop": False}

    def _h(*_):
        flag["stop"] = True
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _h)
            except (ValueError, OSError):
                pass
    print("context: старт", flush=True)
    return run(paths.root(), stop=lambda: flag["stop"], max_seconds=a.max_seconds)


if __name__ == "__main__":
    sys.exit(main())
