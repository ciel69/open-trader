"""Ждущие лимитные входы (post-only лимит на заданной цене, живёт до заполнения/отмены/ttl): хранилище state/pending_orders.json.

Только локальный файл, на биржу не ходит. Нужен, чтобы после ожидания или рестарта `./tb bybit trade sync` смог найти ордер,
записать заполнение в журнал и снять просроченное, а guard/сессия учитывали риск ещё не заполненных заявок.

Формат: {"orders": {ID: {...}}}. Запись ID:
  id, env (testnet|mainnet), mode (live|testnet, как в журнале), sym, side, order_id (None, пока ордер не создан), link_id (orderLinkId = id),
  status (placing: запись есть, ответа биржи ещё нет; open: ордер ждёт), price, qty, sl, tp, lev, margin, notional, risk_usdt,
  ts (epoch постановки), expires (epoch), ttl_min, cancel_above, cancel_below, session, stype, idea, thesis, news, regime, score.
Запись файла атомарная (tmp + fsutil.replace); изменения под файловой блокировкой (параллельные sync по cron и вручную не задвоят журнал).
"""
import contextlib
import json
import os
import re
import time

from tb import fsutil
from tb import paths

PATH = paths.state("pending_orders.json")
LOCK_STALE_S = 120.0     # блокировка старше этого считается брошенной (упавший процесс)
_held = [0]              # глубина вложенных lock() в этом процессе (блокировка реентерабельна)


def load():
    """-> {"orders": {...}}; нет файла или он битый -> пусто (битый не затирается, пока кто-то не запишет)."""
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"orders": {}}
    if not isinstance(d, dict) or not isinstance(d.get("orders"), dict):
        return {"orders": {}}
    return d


def save(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    fsutil.replace(tmp, PATH)


def _owner_dead(lp):
    """POSIX: процесс-владелец блокировки (pid в файле) уже не существует -> блокировка брошена. На Windows os.kill(pid, 0) небезопасен: только по возрасту файла."""
    if os.name != "posix":
        return False
    try:
        with open(lp) as f:
            pid = int(f.read().strip() or 0)
        if pid <= 0 or pid == os.getpid():
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    return False


@contextlib.contextmanager
def lock(wait=10.0, sleep=time.sleep, clock=time.time):
    """Эксклюзивная блокировка через O_EXCL-файл (одинаково на POSIX и Windows). Не взяли за wait секунд -> TimeoutError."""
    if _held[0]:
        _held[0] += 1
        try:
            yield
        finally:
            _held[0] -= 1
        return
    lp = PATH + ".lock"
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    deadline = clock() + wait
    while True:
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lp) > LOCK_STALE_S or _owner_dead(lp):
                    os.remove(lp)
                    continue
            except OSError:
                continue
            if clock() >= deadline:
                raise TimeoutError("pending_orders.json занят другим процессом")
            sleep(0.2)
    _held[0] = 1
    try:
        yield
    finally:
        _held[0] = 0
        try:
            os.remove(lp)
        except OSError:
            pass


def put(rec):
    with lock():
        d = load()
        d["orders"][rec["id"]] = rec
        save(d)


def update(pid, **kw):
    with lock():
        d = load()
        if pid in d["orders"]:
            d["orders"][pid].update(kw)
            save(d)
        return d["orders"].get(pid)


def remove(pid):
    with lock():
        d = load()
        r = d["orders"].pop(pid, None)
        if r is not None:
            save(d)
        return r


def orders(env=None, mode=None, sym=None):
    """Список записей в порядке постановки, с фильтрами по площадке/режиму журнала/символу."""
    out = [r for r in load()["orders"].values() if isinstance(r, dict) and r.get("id")]
    if env:
        out = [r for r in out if r.get("env") == env]
    if mode:
        out = [r for r in out if r.get("mode") == mode]
    if sym:
        out = [r for r in out if r.get("sym") == sym]
    return sorted(out, key=lambda r: r.get("ts") or 0.0)


def risk(rows):
    return sum(float(r.get("risk_usdt") or 0.0) for r in rows)


def new_id(env, sym, now=None):
    """Уникальный id заявки; он же orderLinkId на бирже (до 36 символов)."""
    base = "P%s-%s-%s" % (env[:1].upper(), sym, time.strftime("%m%d-%H%M%S", time.gmtime(now)))
    ids, n, pid = load()["orders"], 1, base
    while pid in ids:
        n += 1
        pid = "%s-%d" % (base, n)
    return pid[:36]


LINK_RE = re.compile(r"^P[TM]-[A-Z0-9]+-\d{4}-\d{6}(-\d+)?$")


def link_id(x):
    """orderLinkId ждущей заявки (PT-/PM-...) -> он же как id заявки, иначе None (стоп/тейк и чужие ордера ссылок такого вида не имеют)."""
    return x if isinstance(x, str) and LINK_RE.match(x) else None
