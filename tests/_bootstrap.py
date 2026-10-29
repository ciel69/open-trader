"""Делает пакет tb импортируемым при `python3 -m unittest discover -s tests` из корня проекта."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Тесты не должны читать рабочий state/pending_orders.json проекта (ждущие лимиты влияют на guard и сессию): подменяем путь на временный.
import tempfile  # noqa: E402
from tb.core import pending as _pending  # noqa: E402
_pending.PATH = os.path.join(tempfile.mkdtemp(prefix="tbpend_"), "pending_orders.json")

# Тесты читают ПОСТАВЛЯЕМЫЕ умолчания config/*.json, а не личные переопределения владельца: каталог config/local подменяем пустым временным (слой tb.core.localcfg).
os.environ["TB_CONFIG_LOCAL"] = tempfile.mkdtemp(prefix="tbpend_cfglocal_")

# Герметичность (docs/CONTRIBUTING.md, «Герметичность тестов»): личное состояние владельца недоступно тестам.
#  state/control (уровень риска, подпись, platform.json, trading.json) и state/brain (active.json, swaps) -> пустые временные каталоги (хук tb.paths: TB_STATE_DIR_*),
#  ключи (подпись, токены, API) -> пустой временный каталог TB_KEYS_DIR, поэтому уровень риска в тестах всегда «по умолчанию (3)», ~/.config/traider-bot не читается.
# Тесты, которым нужен ключ/каталог, создают свои (setUp) и возвращают окружение (patch.dict).
os.environ["TB_STATE_DIR_CONTROL"] = tempfile.mkdtemp(prefix="tbpend_ctl_")
os.environ["TB_STATE_DIR_BRAIN"] = tempfile.mkdtemp(prefix="tbpend_brain_")
os.environ["TB_KEYS_DIR"] = tempfile.mkdtemp(prefix="tbpend_keys_")


# ---------------------------------------------------------------- устойчивость набора (WP-интеграция)
# 1. localhost-сокет: если в окружении (песочница чата) нельзя поднять и открыть сокет на 127.0.0.1, тесты с локальным сервером ПРОПУСКАЮТСЯ с понятным сообщением,
#    а не зависают. Настоящие ошибки (сервер поднялся, но ответ неверный) не маскируются.
# 2. Сторож зависаний: каждый тест ограничен по времени (SIGALRM на POSIX, TB_TEST_TIMEOUT, по умолчанию 180 с) и падает с понятным сообщением;
#    на всякий случай весь процесс прерывается faulthandler через TB_TEST_TOTAL_S (по умолчанию 3300 с) с дампом стеков.
# 3. Сторож реальных каталогов: снимок state/, config/, runtime/ проекта при импорте и сверка в tests/test_zz_realstate_guard.py (тесты не пишут в реальные данные).
import socket as _socket  # noqa: E402
import unittest as _unittest  # noqa: E402

os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(x for x in (os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost", "::1") if x)
_LOCAL_OK = []


def localhost_ok():
    """Можно ли поднять слушающий сокет на 127.0.0.1 и подключиться к нему (с таймаутом 2 с). Результат кэшируется."""
    if _LOCAL_OK:
        return _LOCAL_OK[0]
    ok, why = False, ""
    srv = cli = conn = None
    try:
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.settimeout(2.0)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        cli.settimeout(2.0)
        cli.connect(srv.getsockname())
        conn, _a = srv.accept()
        conn.settimeout(2.0)
        cli.sendall(b"x")
        ok = conn.recv(1) == b"x"
        why = "" if ok else "эхо не получено"
    except (OSError, _socket.timeout) as e:
        why = "%s: %s" % (e.__class__.__name__, e)
    finally:
        for s in (conn, cli, srv):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
    _LOCAL_OK.append((ok, why))
    return _LOCAL_OK[0]


def require_localhost():
    """Вызывать перед запуском локального сервера в тесте: при недоступном localhost-сокете -> SkipTest с причиной."""
    ok, why = localhost_ok()
    if not ok:
        raise _unittest.SkipTest("локальный сокет 127.0.0.1 недоступен в этом окружении (%s): тест с локальным сервером пропущен" % why)


TEST_TIMEOUT_S = float(os.environ.get("TB_TEST_TIMEOUT", "180"))


class TestHang(AssertionError):
    pass


def _install_watchdog():
    import signal
    import faulthandler
    try:
        faulthandler.dump_traceback_later(float(os.environ.get("TB_TEST_TOTAL_S", "3300")), exit=True)
    except (ValueError, RuntimeError, OSError):
        pass
    if not hasattr(signal, "SIGALRM") or getattr(_unittest.TestCase, "_tb_watchdog", False):
        return
    orig = _unittest.TestCase.run

    def run(self, result=None):
        import threading
        if threading.current_thread() is not threading.main_thread():
            return orig(self, result)

        def _h(signum, frame):
            raise TestHang("тест завис (> %d с): %s" % (TEST_TIMEOUT_S, self.id()))
        old = signal.signal(signal.SIGALRM, _h)
        signal.setitimer(signal.ITIMER_REAL, TEST_TIMEOUT_S)
        try:
            return orig(self, result)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    _unittest.TestCase.run = run
    _unittest.TestCase._tb_watchdog = True


_install_watchdog()


def _snapshot_real(root=ROOT):
    """{относительный путь: (размер, mtime_ns)} для state/, config/, runtime/ проекта (имена каталогов, не содержимое)."""
    out = {}
    for top in ("state", "config", "runtime"):
        base = os.path.join(root, top)
        for dp, dn, fn in os.walk(base):
            for n in fn:
                p = os.path.join(dp, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


REAL_SNAPSHOT = _snapshot_real()
