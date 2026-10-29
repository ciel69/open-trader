"""`./tb events [--follow|ack|stats]`: чтение очереди событий и подтверждение обработки (03 п. 3.3). Владелец: WP6.

  events [--consumer chat] [--after SEQ] [--limit 50] [--wake-only] [--json]   разовое чтение (по умолчанию после acked)
  events --follow [--consumer chat] [--poll 0.5] [--no-redeliver]               поток для Monitor: одна JSON-строка на wake-событие
  events ack SEQ [--consumer chat]                                              подтвердить обработку
  events stats [--consumer chat]                                                backlog, последний seq, возраст

Вывод: только числа и белые списки полей (события перепроверяются `bus.clean_out` при чтении). Чтение не пишет в шину; единственное,
что пишется: курсоры `state/bus/cursors/`. Синтетическое `exec_down` (исполнитель не отвечает) в шину не пишется.
"""
import argparse
import sys
import time

from tb.brain import common
from tb.core import bus

EXEC_DOWN_REPEAT_S = 300.0     # синтетика exec_down не чаще раза в 5 минут, пока исполнитель молчит
DEFAULT_POLL = 0.5
READ_BATCH = 200


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _consumer_arg(p):
    p.add_argument("--consumer", default=bus.DEFAULT_CONSUMER)


def _line(ev, redelivery=False):
    """Событие -> словарь для печати (только поля конверта); None, если не проходит белый список."""
    c = bus.clean_out(ev)
    if c is None:
        return None
    if redelivery:
        c["redelivery"] = True
    return c


def read_events(consumer, after, limit, wake_only, root=None):
    """-> [очищенные события] после seq `after` (None = после acked потребителя)."""
    if after is None:
        after = bus.cursor_get(consumer, root=root)["acked"]
    evs = bus.read(after_seq=after, limit=limit, wake_only=wake_only, root=root)
    return [c for c in (_line(e) for e in evs) if c]


def exec_down_event(root=None, now=None):
    """Синтетическое событие exec_down, если heartbeat есть, но старше порога; иначе None. Файла нет вовсе (исполнитель не запускался) = тишина."""
    h = common.exec_health(root=root, now=now)
    if not h["present"] or h["alive"]:
        return None
    age = h["age_s"] if h["age_s"] is not None else 0.0
    return {"type": "exec_down", "pri": bus.PRI_WAKE, "ts": common.iso(time.time() if now is None else now), "synthetic": True, "d": {"age_s": int(age)}}


def follow(consumer, poll=DEFAULT_POLL, redeliver=True, root=None, out=None, duration=None, sleep=time.sleep, clock=time.time):
    """Основной цикл `--follow`. duration (сек) только для тестов. -> 0."""
    out = out or sys.stdout

    def emit_line(obj):
        out.write(common.dumps(obj) + "\n")
        out.flush()
    cur = bus.cursor_init(consumer, root=root)
    last = bus.last_seq(root)
    if cur["delivered"] > last:                 # шину очистили: курсор не может быть впереди
        cur = bus.cursor_reset(consumer, last, root=root)
    if redeliver and cur["acked"] < cur["delivered"]:
        # события могли потеряться вместе с упавшей сессией: один раз переотдаём acked < seq <= delivered
        for ev in bus.read(after_seq=cur["acked"], limit=0, wake_only=True, root=root):
            if ev["seq"] > cur["delivered"]:
                break
            c = _line(ev, redelivery=True)
            if c:
                emit_line(c)
    pos = cur["delivered"]
    started = clock()
    sig, down_at = None, None
    try:
        while True:
            now = clock()
            s = bus.signature(root)
            if s != sig:
                sig = s
                while True:
                    evs = bus.read(after_seq=pos, limit=READ_BATCH, wake_only=False, root=root)
                    if not evs:
                        break
                    for ev in evs:
                        pos = max(pos, ev["seq"])
                        if ev.get("pri") == bus.PRI_WAKE:
                            c = _line(ev)
                            if c:
                                emit_line(c)
                    bus.cursor_delivered(consumer, pos, root=root)
                    if len(evs) < READ_BATCH:
                        break
            else:
                bus.cursor_flush(consumer, root=root)
            down = exec_down_event(root=root, now=now)
            if down is None:
                down_at = None
            elif down_at is None or now - down_at >= EXEC_DOWN_REPEAT_S:
                down_at = now
                emit_line(down)
            if duration is not None and now - started >= duration:
                break
            sleep(poll)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        try:
            bus.cursor_flush(consumer, root=root)
        except (OSError, TimeoutError):
            pass
    return common.EXIT_OK


def main(argv=None):
    common.setup_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "ack":
            return _cmd_ack(argv[1:])
        if argv and argv[0] == "stats":
            return _cmd_stats(argv[1:])
        return _cmd_read(argv)
    except ValueError as e:
        return common.fail(str(e))
    except (OSError, TimeoutError) as e:
        common.fail("шина недоступна: %s" % e.__class__.__name__, code="E_BUS")
        return common.EXIT_FAIL


def _cmd_read(argv):
    p = _Parser(prog="tb events", add_help=True)
    _consumer_arg(p)
    p.add_argument("--after", type=int, default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--wake-only", action="store_true")
    p.add_argument("--json", action="store_true", help="один объект {ok,events,last_seq} вместо строки на событие")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--redeliver", action="store_true", help="переотдать неподтверждённое (по умолчанию уже включено)")
    p.add_argument("--no-redeliver", action="store_true")
    p.add_argument("--poll", type=float, default=DEFAULT_POLL)
    p.add_argument("--duration", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    bus.check_consumer(a.consumer)
    if a.poll < 0.05 or a.limit < 0:
        raise ValueError("--poll >= 0.05, --limit >= 0")
    if a.follow:
        try:
            import signal
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))   # остановка Monitor: дописать курсор
        except (ValueError, OSError, AttributeError):
            pass
        return follow(a.consumer, poll=a.poll, redeliver=not a.no_redeliver, root=a.root, duration=a.duration)
    evs = read_events(a.consumer, a.after, a.limit, a.wake_only, root=a.root)
    if a.json:
        common.out({"ok": True, "events": evs, "last_seq": bus.last_seq(a.root)})
    else:
        for e in evs:
            common.out(e)
    return common.EXIT_OK


def _cmd_ack(argv):
    p = _Parser(prog="tb events ack")
    p.add_argument("seq", type=int)
    _consumer_arg(p)
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    try:
        cur = bus.ack(a.consumer, a.seq, root=a.root)
    except ValueError as e:
        common.out({"ok": False, "errors": [{"code": "E_ACK", "path": "seq", "msg": str(e)}]})
        return common.EXIT_FAIL
    common.out({"ok": True, "consumer": a.consumer, "acked": cur["acked"], "delivered": cur["delivered"]})
    return common.EXIT_OK


def _cmd_stats(argv):
    p = _Parser(prog="tb events stats")
    _consumer_arg(p)
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    st = bus.stats(a.consumer, root=a.root)
    st["ok"] = True
    common.out(st)
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
