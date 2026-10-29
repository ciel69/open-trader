"""Файловая блокировка с одним писателем (06 п. 6.6). Владелец: WP4.

Обобщение `pending.lock` (O_EXCL, pid внутри, устаревание 120 с, реентерабельность); `pending.py` не меняется.
Без fcntl (одинаково на POSIX и Windows). Отличия от pending.lock:
  - реентерабельность по (путь, поток): другой поток того же процесса ждёт, а не проходит мимо;
  - в файле pid и случайный токен: снимаем только свою блокировку (если чужую сочли устаревшей и перехватили, не удаляем её);
  - устаревшей считается блокировка умершего процесса (POSIX: os.kill(pid, 0); Windows: OpenProcess через ctypes) или старше `stale_s`;
  - перехват устаревшей атомарный (rename в уникальное имя), чтобы двое ждущих не снесли новую блокировку друг друга.
Там же `atomic_write` (tmp + fsutil.replace) для остальных файлов хранилища.
"""
import json
import os
import threading
import time
import uuid

from tb import fsutil

DEFAULT_WAIT_S = 10.0
DEFAULT_STALE_S = 120.0

_tls = threading.local()


def _held():
    d = getattr(_tls, "held", None)
    if d is None:
        d = _tls.held = {}
    return d


def pid_alive(pid):
    """True, если процесс жив (или нельзя определить: тогда считаем живым)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    try:  # Windows
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() != 87      # 87 = ERROR_INVALID_PARAMETER: процесса нет; иначе (доступ) считаем живым
        try:
            code = ctypes.c_ulong(0)
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259         # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    except Exception:
        return True


def _read_lock(lp):
    """-> (pid, token) или (0, "") если файл пуст/битый/исчез."""
    try:
        with open(lp, "r") as f:
            parts = f.read().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return 0, ""


class FileLock(object):
    """Контекстный менеджер `with FileLock(path): ...`; TimeoutError, если за `wait` секунд блокировку взять не удалось."""

    def __init__(self, path, wait=DEFAULT_WAIT_S, stale_s=DEFAULT_STALE_S):
        self.path = path
        self.wait = wait
        self.stale_s = stale_s
        self._token = None

    def _key(self):
        return os.path.abspath(self.path)

    def _is_stale(self, lp):
        try:
            age = time.time() - os.path.getmtime(lp)
        except OSError:
            return False
        if age > self.stale_s:
            return True
        pid, _tok = _read_lock(lp)
        return pid > 0 and not pid_alive(pid)

    def _break_stale(self, lp):
        """Снять устаревшую блокировку атомарно; если по ошибке выхватили свежую чужую, вернуть её на место."""
        dead = "%s.dead.%s" % (lp, uuid.uuid4().hex[:8])
        try:
            os.rename(lp, dead)
        except OSError:
            return
        try:
            if self._is_stale(dead):
                os.remove(dead)
                return
            try:
                os.link(dead, lp)        # не устарела: вернуть владельцу (если место уже занято, link не сработает)
            except OSError:
                pass
            os.remove(dead)
        except OSError:
            pass

    def touch(self):
        """Обновить mtime (долгие операции удерживающего), чтобы блокировку не сочли устаревшей."""
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def __enter__(self):
        held = _held()
        key = self._key()
        if key in held:
            held[key][1] += 1
            return self
        lp = self.path
        d = os.path.dirname(os.path.abspath(lp))
        os.makedirs(d, exist_ok=True)
        deadline = time.time() + self.wait
        delay = 0.002
        token = uuid.uuid4().hex[:12]
        while True:
            try:
                fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale(lp):
                    self._break_stale(lp)
                    continue
                if time.time() >= deadline:
                    raise TimeoutError("блокировка %s занята другим процессом" % os.path.basename(lp))
                time.sleep(delay)
                delay = min(delay * 1.5, 0.05)
                continue
            try:
                os.write(fd, ("%d %s" % (os.getpid(), token)).encode())
            finally:
                os.close(fd)
            break
        self._token = token
        held[key] = [token, 1]
        return self

    def __exit__(self, *exc):
        held = _held()
        key = self._key()
        ent = held.get(key)
        if ent is None:
            return False
        ent[1] -= 1
        if ent[1] > 0:
            return False
        del held[key]
        pid, tok = _read_lock(self.path)
        if tok == ent[0] or (pid == os.getpid() and not tok):
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def atomic_write(path, text, sync=False):
    """Запись файла целиком: уникальный tmp рядом + fsutil.replace. Прерванная запись оставляет только tmp, целевой файл цел."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d.%d.%s" % (path, os.getpid(), threading.get_ident(), uuid.uuid4().hex[:6])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            if sync:
                os.fsync(f.fileno())
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, sync=False, indent=1):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True), sync=sync)
