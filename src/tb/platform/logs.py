"""Логи сервисов с ротацией: `runtime/logs/<сервис>.log` (+ .1 .2 ...). Владелец: WP11-B.

Пишет один процесс (супервизор: поток-читатель на сервис). Строки получают метку времени UTC. Ротация по размеру: log -> log.1 -> log.2 ...,
старейший удаляется. Чтение (`./tb logs`) безопасно параллельно с записью.
"""
import os
import threading
import time

from tb import fsutil

DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_KEEP = 3


def stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() if now is None else now))


class RotatingLog(object):
    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES, keep=DEFAULT_KEEP, clock=None):
        self.path, self.max_bytes, self.keep = path, int(max_bytes), max(1, int(keep))
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0

    def _open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "ab")
        try:
            self._size = os.path.getsize(self.path)
        except OSError:
            self._size = 0

    def _rotate(self):
        if self._fh:
            self._fh.close()
            self._fh = None
        last = "%s.%d" % (self.path, self.keep)
        try:
            os.remove(last)
        except OSError:
            pass
        for i in range(self.keep - 1, 0, -1):
            src = "%s.%d" % (self.path, i)
            if os.path.exists(src):
                fsutil.replace(src, "%s.%d" % (self.path, i + 1))
        if os.path.exists(self.path):
            fsutil.replace(self.path, self.path + ".1")

    def write(self, text):
        """Добавляет строку (или несколько) с меткой времени; bytes принимаются как есть."""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        lines = text.splitlines() or [""]
        data = "".join("%s %s\n" % (stamp(self.clock()), ln.rstrip("\r")) for ln in lines).encode("utf-8", "replace")
        with self._lock:
            try:
                if self._fh is None:
                    self._open()
                if self._size > 0 and self._size + len(data) > self.max_bytes:
                    self._rotate()
                    self._open()
                self._fh.write(data)
                self._fh.flush()
                self._size += len(data)
            except OSError:
                pass

    def close(self):
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


def tail(path, n=50):
    """Последние n строк файла (читает хвост, не весь файл)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            block = 8192
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[-n:] if n > 0 else []


def follow(path, out, poll_s=0.5, stop=lambda: False, sleep=time.sleep):
    """tail -f: печатает новые строки в out до stop(); переживает ротацию (размер уменьшился -> читаем с начала)."""
    pos = os.path.getsize(path) if os.path.exists(path) else 0
    while not stop():
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size < pos:
            pos = 0
        if size > pos:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                pos += nl + 1
                out.write(chunk[:nl + 1].decode("utf-8", "replace"))
                out.flush()
        sleep(poll_s)
