"""Кроссплатформенные операции с процессами: запуск в своей группе, остановка, чтение вывода. Владелец: WP11-B.

POSIX (macOS/Linux): дочерний процесс запускается в новой сессии (start_new_session), группа процессов = pid; остановка killpg SIGTERM -> SIGKILL.
Windows: CREATE_NEW_PROCESS_GROUP; остановка CTRL_BREAK_EVENT -> terminate() -> kill(). Без os.fork и без сигнальных API вне этого модуля.
Чистые функции `popen_kwargs(system)` и `stop_plan(system)` проверяются юнит-тестами для обеих ветвей.
"""
import os
import signal
import subprocess
import sys
import threading
import time

from tb.core.filelock import pid_alive

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
WIN_SIGNAL_WAIT_S = 3.0      # Windows: сколько ждать выхода после CTRL_BREAK, прежде чем terminate()
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_windows(system=None):
    return (system or sys.platform).startswith("win")


def popen_kwargs(system=None, detached=False):
    """Параметры Popen для отдельной группы процессов."""
    if is_windows(system):
        # CREATE_NO_WINDOW всегда: супервизор без консоли (DETACHED), и каждый его дочерний python.exe иначе получает своё окно консоли
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | (DETACHED_PROCESS if detached else 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def stop_plan(system=None):
    """Шаги остановки: список ('signal', имя) / ('terminate',) / ('kill',). Для тестов и для документа."""
    if is_windows(system):
        return [("signal", "CTRL_BREAK_EVENT"), ("terminate",), ("kill",)]
    return [("signal", "SIGTERM"), ("kill", "SIGKILL")]


def spawn(cmd, env, cwd, capture=True, detached=False, system=None):
    """Запуск дочернего процесса. capture=True: stdout+stderr в pipe (читает LineReader), stdin закрыт."""
    kw = popen_kwargs(system, detached)
    return subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.STDOUT if capture else subprocess.DEVNULL, close_fds=True, **kw)


class LineReader(threading.Thread):
    """Читает вывод процесса построчно и отдаёт в log.write (поток-демон)."""

    def __init__(self, proc, log):
        threading.Thread.__init__(self, name="log-%s" % proc.pid)
        self.daemon = True
        self.proc, self.log = proc, log
        self.start()

    def run(self):
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self.log.write(line.rstrip(b"\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.proc.stdout.close()
            except Exception:
                pass


def _wait(proc, timeout):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def terminate(proc, grace=8.0, system=None):
    """Мягкая остановка с эскалацией до kill. -> код возврата процесса. Группа добивается, даже если лидер уже вышел (дети)."""
    if proc is None:
        return None
    if proc.poll() is None:
        if is_windows(system):
            graceful = True
            try:
                proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            except (OSError, ValueError):
                graceful = False                  # у супервизора без консоли CTRL_BREAK не доходит: ждать ответа бессмысленно
            # без общей консоли сигнал может «уйти» и не дойти: ждём недолго, иначе остановка сервисов по очереди растягивается на минуты
            if not (graceful and _wait(proc, min(grace / 2.0, WIN_SIGNAL_WAIT_S))):
                try:
                    proc.terminate()
                except OSError:
                    pass
                if not _wait(proc, grace / 2.0):
                    try:
                        proc.kill()
                    except OSError:
                        pass
        else:
            _killpg(proc.pid, signal.SIGTERM, proc)
            if not _wait(proc, grace):
                _killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM), proc)
        _wait(proc, 5.0)
    elif not is_windows(system):
        _killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM), proc, quiet=True)     # лидер вышел, потомки группы нет
    return proc.poll()


def _killpg(pgid, sig, proc=None, quiet=False):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        if proc is not None and not quiet:
            try:
                proc.send_signal(sig)
            except OSError:
                pass


def cmdline_of(pid):
    """Командная строка процесса (POSIX через ps, Windows через PowerShell/CIM) или None."""
    if is_windows():
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, creationflags=CREATE_NO_WINDOW)
            s = out.stdout.decode("utf-8", "replace").strip()
            return s or None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        s = out.stdout.decode("utf-8", "replace").strip()
        return s or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def kill_pid(pid, grace=5.0):
    """Остановить процесс по pid (сирота или супервизор). POSIX: группа, затем процесс; Windows: taskkill /T. -> True, если процесса больше нет."""
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        return False
    if is_windows():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return not pid_alive(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            pg = os.getpgid(pid)
        except OSError:
            return True
        try:
            if pg == pid:
                os.killpg(pg, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        end = time.time() + grace
        while time.time() < end:
            if not pid_alive(pid):
                return True
            time.sleep(0.1)
    return not pid_alive(pid)
