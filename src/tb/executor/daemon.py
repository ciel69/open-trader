"""Тонкая asyncio-оболочка исполнителя (02 п. 2.3). Владелец: WP5.

Один рабочий поток исполняет Executor строго последовательно (очередь событий): записи на биржу сериализованы, нет гонок «два потока двигают стоп».
Страж (WP1B): отдельный малый поток; если рабочий поток занят одним событием дольше stuck_s (завис в REST), а владелец потребовал KILL flatten, страж сам шлёт
reduce-only закрытия (Executor.emergency_flatten) по своим соединениям транспорта. Остальную защиту (стопы на бирже) держит биржа.
Цикл событий asyncio только принимает данные фида (WS) и таймеры и кладёт их в очередь; чтение рынка для подкачки свечей идёт отдельным малым пулом
потоков, поэтому зависший REST читающего пула не блокирует защиту. Завершение: stop() (Ctrl+C / SIGTERM / --max-seconds).
"""
import asyncio
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tb import paths
from tb.executor import mdsvc
from tb.executor.ports import MarketFeed
from tb.executor.scanbridge import ScanBridge


class NullFeed(MarketFeed):
    """Фид без сети (selftest): подписки только запоминаются."""

    def __init__(self):
        self.on_ticker = self.on_kline = self.on_private = self.on_state = None
        self.subs = {}

    def subscribe(self, symbols, tfs):
        for s in symbols:
            self.subs.setdefault(s, set()).update(tfs)

    def unsubscribe(self, symbols):
        for s in symbols:
            self.subs.pop(s, None)

    async def run(self):
        while True:
            await asyncio.sleep(3600)

    async def stop(self):
        pass


class Daemon(object):
    def __init__(self, ex, feed, max_seconds=None, log=None, timer_s=1.0, inbox_s=0.5, paper_save=None, stuck_s=3.0, guard_s=1.0, feed_stop_s=3.0, join_s=8.0):
        self.ex = ex
        self.feed = feed
        self.max_seconds = max_seconds
        self.log = log or (lambda m: None)
        self.timer_s = timer_s
        self.inbox_s = inbox_s
        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.subscribed = {}
        self.bridge = ScanBridge(ex.root, ex.clock)
        self.paper_save = paper_save
        self._stop = None
        self._worker = None
        self._seeding = set()
        self.lag_ms = 0.0
        self.stuck_s = stuck_s
        self.guard_s = guard_s
        self.feed_stop_s = feed_stop_s
        self.join_s = join_s
        self._busy = None             # time.monotonic() начала обработки текущего события или None
        self._guardian = None
        self._guard_stop = threading.Event()
        self._last_emerg = None

    # ---- обработчики фида (вызываются в потоке цикла; только кладут в очередь)
    def _wire(self):
        f = self.feed
        f.on_ticker = lambda *a: self.q.put(("ticker", a))
        f.on_kline = lambda *a: self.q.put(("kline", a))
        f.on_private = lambda ev: self.q.put(("private", ev))
        f.on_state = lambda ch, up, down: self.q.put(("state", (ch, up, down)))
        if hasattr(f, "on_gap"):
            f.on_gap = lambda *a: self.q.put(("gap", a))
        if hasattr(self.ex.port, "on_private"):
            self.ex.port.on_private = lambda ev: self.q.put(("private", ev))   # бумажный порт: события исполнения как у приватного WS

    def _work(self):
        ex = self.ex
        while True:
            item = self.q.get()
            if item is None:
                return
            kind, a = item
            t0 = time.time()
            self._busy = time.monotonic()
            try:
                if kind == "ticker":
                    ex.on_ticker(*a)
                elif kind == "kline":
                    ex.on_kline(*a)
                elif kind == "private":
                    ex.on_private(a)
                elif kind == "state":
                    ex.on_state(*a)
                elif kind == "gap":
                    ex.on_gap(*a)
                elif kind == "seed":
                    ex.on_seed(*a)
                elif kind == "timer":
                    self._timer()
                elif kind == "inbox":
                    ex.poll_inbox()
            except Exception as e:      # защитный пояс: исключение не останавливает исполнителя
                ex.error("work_" + kind, type(e).__name__)
                self.log("ошибка обработчика %s: %s: %s" % (kind, type(e).__name__, e))
            finally:
                self._busy = None
            self.lag_ms = max(0.0, (time.time() - t0) * 1000.0) if kind != "timer" else self.lag_ms

    def stuck_for(self):
        """Сколько секунд рабочий поток занят одним событием (0, если свободен)."""
        b = self._busy
        return 0.0 if b is None else max(0.0, time.monotonic() - b)

    def guard_once(self):
        """Один проход стража: -> число отправленных аварийных закрытий. Не чаще раза в 5 с."""
        if self.stuck_for() < self.stuck_s or (self._last_emerg is not None and time.monotonic() - self._last_emerg < 5.0):
            return 0
        from tb.core import exec_cfg
        k = exec_cfg.kill_state(self.ex.root)
        if k is None or not k.get("flatten"):
            return 0
        self._last_emerg = time.monotonic()
        try:
            n = self.ex.emergency_flatten()
        except Exception as e:
            self.log("страж: ошибка аварийного закрытия %s" % type(e).__name__)
            return 0
        self.log("страж: рабочий поток завис %.0f с, KILL flatten выполнен стражем (%d)" % (self.stuck_for(), n))
        return n

    def _guard_loop(self):
        while not self._guard_stop.wait(self.guard_s):
            try:
                self.guard_once()
            except Exception:
                pass

    def _timer(self):
        ex = self.ex
        ex.on_timer()
        self._sync_subs()
        self._seed_pull()
        try:
            self.bridge.poll()
        except Exception:
            pass
        req = paths.exec_reconcile_req_file(ex.root, ex.venue_id)
        if os.path.exists(req):
            try:
                os.remove(req)
            except OSError:
                pass
            ex.need_reconcile = True
        if self.paper_save and time.time() - getattr(self, "_ps", 0) > 60:
            self._ps = time.time()
            self.paper_save()

    def _sync_subs(self):
        want = self.ex.desired()
        for sym, tfs in want.items():
            old = self.subscribed.get(sym, set())
            if tfs - old:
                self.feed.subscribe([sym], sorted(tfs - old))
                self.subscribed[sym] = old | tfs
        gone = [s for s in self.subscribed if s not in want]
        if gone:
            self.feed.unsubscribe(gone)
            for s in gone:
                self.subscribed.pop(s, None)

    def _seed_pull(self):
        """Подкачка свечей отдельным пулом: результат возвращается событием в очередь."""
        for sym, tf in self.ex.seed_requests():
            if (sym, tf) in self._seeding:
                continue
            self._seeding.add((sym, tf))
            self.pool.submit(self._fetch, sym, tf)

    def _fetch(self, sym, tf):
        try:
            rows = self.ex.port.klines(sym, tf, 200)
            self.q.put(("seed", (sym, tf, rows)))
        except Exception:
            pass
        finally:
            self._seeding.discard((sym, tf))

    async def _ticks(self):
        n = 0
        t_in = 0.0
        while not self._stop.is_set():
            await asyncio.sleep(self.inbox_s)
            self.q.put(("inbox", None))
            t_in += self.inbox_s
            if t_in >= self.timer_s:
                t_in = 0.0
                self.q.put(("timer", None))
            n += 1

    async def run(self):
        loop = asyncio.get_event_loop()
        self._stop = asyncio.Event()
        self._wire()
        self.ex.start()
        self._worker = threading.Thread(target=self._work, name="exec-worker", daemon=True)
        self._worker.start()
        self._guardian = threading.Thread(target=self._guard_loop, name="exec-guardian", daemon=True)
        self._guardian.start()
        self.q.put(("timer", None))
        tasks = [asyncio.ensure_future(self._ticks()), asyncio.ensure_future(self.feed.run())]
        try:
            import signal
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self._stop.set)
                except (NotImplementedError, ValueError, RuntimeError):
                    pass      # Windows: KeyboardInterrupt обрабатывает main
        except Exception:
            pass
        try:
            if self.max_seconds:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=float(self.max_seconds))
                except asyncio.TimeoutError:
                    pass
            else:
                await self._stop.wait()
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks):
        """Быстрое завершение (цель до ~10 с): фид останавливается с лимитом feed_stop_s, невыбранные события очереди отбрасываются (все они пересоздаются
        таймером/реконсиляцией при следующем старте), рабочему потоку даётся join_s на текущее событие."""
        self._guard_stop.set()
        try:
            await asyncio.wait_for(self.feed.stop(), timeout=self.feed_stop_s)
        except Exception:
            pass
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self.feed_stop_s)
        except Exception:
            pass
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.q.put(None)
        if self._worker is not None:
            self._worker.join(timeout=self.join_s)
        self.pool.shutdown(wait=False)
        try:
            self.ex.stop()
            if self.paper_save:
                self.paper_save()
        except Exception as e:
            self.log("ошибка завершения: %s" % type(e).__name__)
