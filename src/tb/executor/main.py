"""CLI исполнителя: `./tb exec run|status|halt|resume|reconcile|verify|replay` (02 п. 2.1; 05 WP5). Владелец: WP5.

run --profile trade|testnet|paper [--observe] [--max-seconds N] [--keep-entries] [--selftest]
  trade -> mainnet с торговым ключом, testnet -> testnet, paper -> бумажный порт на публичных потоках (ключ не нужен). Ключ и снятие прокси
  выполняет tb.launcher ДО диспетчера. Без --profile отказ с кодом 2. Второй экземпляр на одном корне: код 4 (runtime/exec.lock).
status: heartbeat + счётчики (код 0 жив, 1 нет). halt [--flatten] [--reason]: state/KILL + событие kill. resume: снять KILL (только владелец).
reconcile: запросить внеочередную реконсиляцию у работающего демона. verify: проверка хеш-цепочки журнала решений. replay [PLAN]: хронология решений.
Коды: 0 успех, 1 сбой/не жив, 2 использование, 4 второй экземпляр.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from tb import paths
from tb.core import bus, decisions, exec_cfg
from tb.core.filelock import atomic_write_json, pid_alive

SUBCOMMANDS = ("status", "halt", "resume", "reconcile", "verify", "replay", "run")
PROFILES = ("trade", "testnet", "paper")
EXIT_NOT_IMPLEMENTED = 2
EXIT_ALREADY_RUNNING = 4
LOCK_STALE_S = 30.0
ENV_OF = {"trade": "mainnet", "testnet": "testnet", "paper": "paper"}


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def acquire_lock(root=None, now=None, venue=None):
    """runtime/exec.lock (O_EXCL, pid и время старта). Устаревшая (мёртвый pid; либо heartbeat и файл старше 30 с) снимается. -> (ok, info)."""
    path = paths.exec_lock_file(root=root, venue=venue)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = time.time() if now is None else now
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = {}
            try:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                pass
            pid = int(info.get("pid") or 0)
            hb = None
            try:
                hb = now - os.path.getmtime(paths.exec_health_file(root=root, venue=venue))
            except OSError:
                pass
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            dead = pid > 0 and pid != os.getpid() and not pid_alive(pid)
            if dead or (age > LOCK_STALE_S and (hb is None or hb > LOCK_STALE_S)):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            return False, info
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": round(now, 3)}, f)
        return True, {"pid": os.getpid()}
    return False, {}


def release_lock(root=None, venue=None):
    try:
        with open(paths.exec_lock_file(root=root, venue=venue), encoding="utf-8") as f:
            if int(json.load(f).get("pid") or 0) != os.getpid():
                return
        os.remove(paths.exec_lock_file(root=root, venue=venue))
    except (OSError, ValueError):
        pass


def build(profile, observe, keep_entries, selftest, root=None, exchange=None):
    """-> (Executor, feed, port_closer). Порт и поток создаёт реестр бирж (tb.venues.registry) по id биржи (по умолчанию bybit). Импорты сетевых модулей только там."""
    from tb.executor.core import Executor
    from tb.venues import registry
    exchange = exchange or "bybit"
    b = registry.make_bundle(exchange, profile, root, selftest)
    ex = Executor(b.port, root=root, profile=profile, observe=observe, keep_entries=keep_entries, venue_id=exchange)
    return ex, b.feed, b.saver


def cmd_run(argv):
    ap = argparse.ArgumentParser(prog="tb exec run")
    ap.add_argument("--profile", choices=PROFILES)
    ap.add_argument("--exchange", default=None, help="id биржи из config/exchanges.json (по умолчанию bybit); по одному исполнителю на биржу")
    ap.add_argument("--observe", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=None, dest="max_seconds")
    ap.add_argument("--keep-entries", action="store_true", dest="keep_entries")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not a.profile:
        print("exec run: нужен --profile trade|testnet|paper", file=sys.stderr)
        return 2
    if a.selftest and a.profile != "paper":
        print("--selftest только для --profile paper", file=sys.stderr)
        return 2
    root = a.root
    exch = a.exchange or "bybit"
    if a.exchange:
        from tb.core import exchcfg
        if not exchcfg.RE_ID.match(exch):
            print("exec run: --exchange: id биржи (строчные буквы и цифры)", file=sys.stderr)
            return 2
        from tb.venues import registry
        try:
            registry.adapter_of(exch, root)
            if registry.adapter_of(exch, root) not in registry.adapters():
                raise registry.ExchangeNotImplemented("биржа %s не реализована: см. docs/EXCHANGES.md" % exch)
        except registry.ExchangeNotImplemented as e:
            print("exec run: %s" % e, file=sys.stderr)
            return 2
    ok, info = acquire_lock(root, venue=exch)
    if not ok:
        print("исполнитель уже запущен (pid %s): второй экземпляр отказан" % info.get("pid"), file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    try:
        ex, feed, saver = build(a.profile, a.observe, a.keep_entries, a.selftest, root, exch)
        from tb.executor.daemon import Daemon
        d = Daemon(ex, feed, max_seconds=a.max_seconds, log=lambda m: print(m, file=sys.stderr), paper_save=saver)
        print("exec run: exchange=%s profile=%s env=%s observe=%s" % (exch, a.profile, ex.port.env, a.observe), file=sys.stderr)
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(d.run())
            finally:
                loop.close()
        except KeyboardInterrupt:
            ex.stop()
        return 0
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 2
        return e.code or 0
    finally:
        release_lock(root, exch)


def cmd_status(argv):
    from tb.brain import common
    h = common.exec_health(root=None)
    d = h["data"]
    out = {"alive": h["alive"], "age_s": h["age_s"], "env": d.get("env"), "mode": d.get("mode"), "hold": d.get("hold"), "plans": d.get("plans"),
           "positions": d.get("positions"), "kill": exec_cfg.kill_state() is not None, "last_reconcile_ts": d.get("last_reconcile_ts")}
    _out(out)
    return 0 if h["alive"] else 1


def _executor_alive():
    try:
        from tb.brain import common
        return bool(common.exec_health(root=None)["alive"])
    except Exception:
        return False


def cmd_halt(argv):
    ap = argparse.ArgumentParser(prog="tb exec halt")
    ap.add_argument("--flatten", action="store_true")
    ap.add_argument("--reason", default="exec halt")
    a = ap.parse_args(argv)
    rec = exec_cfg.set_kill(flatten=a.flatten, reason=a.reason)
    if not _executor_alive():       # живой исполнитель сам пишет событие kill при обнаружении файла (ровно один раз); CLI пишет только за мёртвого
        try:
            bus.emit("kill", {"state": "on", "flatten": bool(rec["flatten"])})
        except Exception:
            pass
    _out({"ok": True, "kill": rec})
    return 0


def cmd_resume(argv):
    was = exec_cfg.clear_kill()
    if not _executor_alive():
        try:
            bus.emit("kill", {"state": "off", "flatten": False})
        except Exception:
            pass
    _out({"ok": True, "was_killed": was})
    return 0


def cmd_reconcile(argv):
    p = paths.runtime("exec_reconcile.req")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    atomic_write_json(p, {"ts": time.time()})
    _out({"ok": True, "requested": True})
    return 0


def cmd_verify(argv):
    r = decisions.verify()
    _out(r)
    return 0 if r.get("ok") else 1


def cmd_replay(argv):
    ap = argparse.ArgumentParser(prog="tb exec replay")
    ap.add_argument("plan", nargs="?")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args(argv)
    for r in decisions.read(0, plan=a.plan, limit=a.limit):
        print("%s %-6s %-16s %s %s %s" % (r["ts"], r["kind"], r["what"], r.get("plan", ""), json.dumps(r.get("args") or {}, ensure_ascii=False),
                                          r.get("why", "")))
    return 0


COMMANDS = {"run": cmd_run, "status": cmd_status, "halt": cmd_halt, "resume": cmd_resume, "reconcile": cmd_reconcile, "verify": cmd_verify, "replay": cmd_replay}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS:
        print("использование: ./tb exec <%s>" % "|".join(SUBCOMMANDS), file=sys.stderr)
        return 2
    return int(COMMANDS[argv[0]](argv[1:]) or 0)


if __name__ == "__main__":
    sys.exit(main())
