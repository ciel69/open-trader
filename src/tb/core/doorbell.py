"""Звонок: мгновенное пробуждение процессов платформы вместо опроса файлов.

Раньше связку «событие -> диспетчер -> задание мозга -> команда -> исполнитель» держали опросы по 0.5-1 с на каждом звене (в среднем 1-2.5 с простоя на каждое решение).
Теперь тот, кто положил событие, задание или команду, «звонит» адресату коротким UDP-пакетом на 127.0.0.1; адресат просыпается за миллисекунды.
Файлы остаются единственным источником правды (звонок только будит): потеряли пакет, нет получателя, чужой порт, ошибка сокета, значит всё работает как раньше по таймауту опроса.
Порт получателя лежит в runtime/doorbell/<имя>.port (атомарная запись). Ничего, кроме одного байта, по сокету не передаётся, поэтому подделать звонком нечего: он только ускоряет опрос.
"""
import os
import socket
import threading
import time

from tb import paths

NAMES = ("dispatch", "brain", "exec", "outbox")
_MAX_PENDING = 8
_NEG_TTL_S = 1.0               # нет файла порта: повторно искать не чаще раза в секунду
_cache = {}                    # путь файла порта -> (mtime_ns, port, проверено_в)
_tx = None
_tx_lock = threading.Lock()


def _port_file(name, root=None):
    return paths.runtime("doorbell", name + ".port", root=root)


def _sender():
    global _tx
    with _tx_lock:
        if _tx is None:
            _tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _tx.setblocking(False)
        return _tx


def ring(name, root=None):
    """Разбудить получателя `name` (если он запущен). Никогда не бросает исключений и не блокируется."""
    try:
        pf = _port_file(name, root)
        now = time.time()
        hit = _cache.get(pf)
        if hit is not None and hit[1] is None and now - hit[2] < _NEG_TTL_S:
            return False
        try:
            st = os.stat(pf)
        except OSError:
            _cache[pf] = (None, None, now)
            return False
        if hit is None or hit[0] != st.st_mtime_ns or hit[1] is None:
            with open(pf, "r") as f:
                port = int(f.read().strip())
            _cache[pf] = (st.st_mtime_ns, port, now)
        else:
            port = hit[1]
        _sender().sendto(b"1", ("127.0.0.1", port))
        return True
    except (OSError, ValueError):
        return False


class Bell(object):
    """Получатель звонков. wait(t) возвращает True, если разбудили звонком, False по таймауту. callback (необязательно) вызывается из потока приёма на каждый звонок."""

    def __init__(self, name, root=None, callback=None):
        self.name, self.root, self.callback = name, root, callback
        self._pf = _port_file(name, root)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._cv = threading.Condition()
        self._pending = 0
        self._stop = False
        os.makedirs(os.path.dirname(self._pf), exist_ok=True)
        tmp = "%s.%d.tmp" % (self._pf, os.getpid())
        with open(tmp, "w") as f:
            f.write(str(self.port))
        os.replace(tmp, self._pf)
        self._th = threading.Thread(target=self._loop, name="bell-%s" % name, daemon=True)
        self._th.start()

    def _loop(self):
        while not self._stop:
            try:
                self._sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                if self._stop:
                    break
                time.sleep(0.05)          # например, ConnectionResetError на Windows: сокет жив, ждём дальше
                continue
            with self._cv:
                self._pending = min(self._pending + 1, _MAX_PENDING)
                self._cv.notify_all()
            if self.callback is not None:
                try:
                    self.callback()
                except Exception:
                    pass

    def wait(self, timeout):
        with self._cv:
            if self._pending <= 0:
                self._cv.wait(timeout)
            if self._pending > 0:
                self._pending -= 1
                return True
        return False

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            with open(self._pf, "r") as f:
                mine = f.read().strip() == str(self.port)
            if mine:
                os.remove(self._pf)
        except OSError:
            pass
        with self._cv:
            self._cv.notify_all()
