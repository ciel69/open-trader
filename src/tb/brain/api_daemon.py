"""`./tb brain run`: демон API-мозга (WP11-C2; сервис `brain` супервизора, по умолчанию выключен).

Цикл: воркеры берут задания диспетчера (`state/brain/jobs`, приоритеты, слияние дублей), раннер (runner.py) собирает досье, вызывает роль через слой llm и исполняет решение
инструментами шлюза. Главный поток: heartbeat `runtime/platform/brain.hb.json` (с полями protect_only/health для `./tb status`), сигнал `state/brain/reload.req`
(перечитать providers/roles/brain.json), проба восстановления из «только защиты», чистка старых заданий, возврат «осиротевших» заданий в очередь.
Остальные подкоманды (use|rollback|reload|status|jobs|probe|check|verify) — brain_cli.py. Рестарт демона очередь не теряет: задания лежат файлами, взятые и не завершённые
возвращаются в `new`, события подтверждаются только после завершения заданий.
Включение: `./tb up --services exec,scan,dispatch,ctl,dash,brain` (или сервис brain в services.<профиль> config/platform.json) и подписчик `api` в config/subscriptions.json (enabled=true).
"""
import json
import os
import signal
import sys
import threading
import time

from tb import paths
from tb.brain import jobs, lifejobs, swap
from tb.brain.runner import Runner

HB_S = 5.0


def _worker(runner, poll_s):
    while not runner.stop_event.is_set():
        try:
            did = lifejobs.step(runner) or runner.step()      # задания жизненного цикла L-* (WP14) раньше событийных
        except Exception:        # воркер не умирает от одного плохого задания: ошибка уже учтена в process
            did = False
        if not did:
            runner.stop_event.wait(poll_s)


def _seed_rates(runner):
    now = runner.clock()
    for j in jobs.list_jobs(runner.root):
        ts = j.get("taken_ts") or j.get("done_ts")
        if ts and now - ts < 3600:
            runner.rate.seed(j.get("sub"), j.get("prio") if isinstance(j.get("prio"), int) else 9, ts, now)


def heartbeat(runner):
    from tb.platform import services as S
    S.write_hb("brain", runner.snapshot(), root=runner.root, now=runner.clock())


def run_daemon(runner=None, root=None, max_seconds=None, poll_s=None, join_s=8.0):
    """-> код выхода. Блокирует до runner.stop_event / max_seconds."""
    root = root or paths.root()
    r = runner or Runner(root)
    rt = r.conf.runtime
    poll = poll_s if poll_s is not None else float(rt.get("poll_s") or 1.0)
    n = jobs.requeue_stale(root, r.clock(), rt["stale_taken_s"], rt["max_attempts"])
    lifejobs.reset_claims(root)
    _seed_rates(r)
    if r.errors:
        for e in r.errors[:10]:
            print("brain: замечание: %s" % e, flush=True)
    th = [threading.Thread(target=_worker, args=(r, poll), name="brain-w%d" % i, daemon=True) for i in range(int(rt["workers"]))]
    for t in th:
        t.start()
    t0 = time.time()
    last_hb = last_prune = last_requeue = 0.0
    print("brain: воркеров %d, возвращено в очередь %d, ревизия %s" % (len(th), n, r.cache.get(r.conf).rev), flush=True)
    try:
        while not r.stop_event.is_set():
            now = time.time()
            if max_seconds is not None and now - t0 >= max_seconds:
                break
            if os.path.exists(swap.reload_req_file(root)):
                try:
                    os.remove(swap.reload_req_file(root))
                except OSError:
                    pass
                r.reload()
                r.cache.get(r.conf, force=True)
            r.recovery_tick()
            if now - last_hb >= HB_S:
                heartbeat(r)
                last_hb = now
            if now - last_prune >= 3600:
                jobs.prune(root, r.clock(), rt["keep_jobs_s"])
                lifejobs.prune(root, time.time(), rt["keep_jobs_s"])
                last_prune = now
            if now - last_requeue >= 60:
                with r._lk:
                    inflight = set(r.inflight)
                jobs.requeue_stale(root, r.clock(), rt["stale_taken_s"], rt["max_attempts"], inflight=inflight, own_dead=False)
                last_requeue = now
            r.stop_event.wait(0.5)
    finally:
        r.stop_event.set()
        for t in th:
            t.join(join_s)
        with r._lk:
            left = list(r.inflight)
        for jid in left:           # воркер не успел: задание возвращается в очередь, событие остаётся неподтверждённым
            j = next((x for x in jobs.list_jobs(root, "taken") if x["id"] == jid), None)
            if j:
                jobs.finish(root, j, "new", {"outcome": "aborted", "code": "SHUTDOWN", "defer": True, "retry_in_s": 0}, r.clock())
        heartbeat(r)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        max_s = None
        if "--max-seconds" in argv:
            try:
                max_s = float(argv[argv.index("--max-seconds") + 1])
            except (IndexError, ValueError):
                print("использование: brain run [--max-seconds N]", file=sys.stderr)
                return 2
        r = Runner(paths.root())

        def _h(*_):
            r.stop_event.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _h)
                except (ValueError, OSError):
                    pass
        return run_daemon(r, paths.root(), max_seconds=max_s)
    from tb.brain import brain_cli
    return brain_cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
