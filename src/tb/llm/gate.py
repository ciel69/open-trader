"""Приоритетные «ворота» к провайдеру LLM: не больше `capacity` одновременных вызовов, очередь ждущих по приоритету (меньше число = раньше), внутри приоритета по порядку прихода.

Зачем: локальная модель (LM Studio) обслуживает запросы по очереди, и 11-секундный поиск входа (entry_scout, класс 3) задерживал бы ответ по открытой позиции (position_manager, класс 1-2).
Ворота не вытесняют уже идущий вызов, но следующим на модель всегда идёт самое срочное ожидающее задание. capacity <= 0 = без ограничения. Ожидание ограничено по времени:
не получили слот, значит вызов идёт без него (ворота ускоряют порядок, но не могут повесить мозг).
"""
import heapq
import itertools
import threading
import time


class PriorityGate(object):
    def __init__(self, capacity):
        self.capacity = int(capacity or 0)
        self._lock = threading.Lock()
        self._n = 0
        self._wait = []                 # куча (приоритет, порядковый номер, событие)
        self._seq = itertools.count()
        self.waited_s = 0.0

    def acquire(self, prio=5, timeout=120.0):
        """-> True, если слот получен (его нужно вернуть release), False по таймауту (слот не занят, release не нужен)."""
        if self.capacity <= 0:
            return True
        with self._lock:
            if self._n < self.capacity and not self._wait:
                self._n += 1
                return True
            ev = threading.Event()
            item = (prio, next(self._seq), ev)
            heapq.heappush(self._wait, item)
        t0 = time.monotonic()
        got = ev.wait(timeout)
        self.waited_s += time.monotonic() - t0
        if got:
            return True
        with self._lock:
            if ev.is_set():             # слот отдали в момент таймаута
                return True
            try:
                self._wait.remove(item)
                heapq.heapify(self._wait)
            except ValueError:
                pass
        return False

    def release(self):
        if self.capacity <= 0:
            return
        with self._lock:
            if self._wait:
                _p, _s, ev = heapq.heappop(self._wait)
                ev.set()                # слот передаётся самому срочному ожидающему без освобождения
            else:
                self._n = max(0, self._n - 1)

    def slot(self, prio=5, timeout=120.0):
        return _Slot(self, prio, timeout)

    def snapshot(self):
        with self._lock:
            return {"capacity": self.capacity, "busy": self._n, "waiting": len(self._wait)}


class _Slot(object):
    def __init__(self, gate, prio, timeout):
        self.gate, self.prio, self.timeout, self.held = gate, prio, timeout, False

    def __enter__(self):
        self.held = self.gate.acquire(self.prio, self.timeout)
        return self

    def __exit__(self, *exc):
        if self.held:
            self.gate.release()
        return False
