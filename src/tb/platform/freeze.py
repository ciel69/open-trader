"""Защита от «замороженной» платформы: процесс супервизора или сервиса приостановлен (Ctrl+Z, состояние T) и не отвечает на stop.req.

Трогаем ТОЛЬКО свои процессы: pid из runtime/platform/supervisor.lock и *.pid, и только если командная строка похожа на наш модуль (`-m tb.`).
Сигналы: SIGCONT (разморозка) и SIGTERM (мягкая остановка). SIGKILL здесь не используется. Windows: состояние процесса не определяется, только предупреждение.
Состояние и сигналы подменяются в тестах (state_fn, cont_fn, term_fn, cmd_fn).
"""
import os
import signal
import subprocess
import time

from tb.core.filelock import pid_alive
from tb.platform import procs
from tb.platform import services as S


def proc_state(pid):
    """-> первая буква состояния процесса из ps (R, S, T, Z ...) или None (процесса нет / Windows / не определить)."""
    if procs.is_windows():
        return None
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        s = out.stdout.decode("utf-8", "replace").strip()
        return s[:1] or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_ours(pid, cmd_fn=None):
    """Командная строка принадлежит модулю проекта (`python -m tb.…`). Не удалось узнать -> False (не трогаем)."""
    cmd = (cmd_fn or procs.cmdline_of)(pid)
    return bool(cmd and "-m tb." in cmd)


def platform_pids(root):
    """-> [(имя, pid)] живых процессов платформы по lock и pid-файлам (без повторов, без нашего pid)."""
    found, seen = [], set()

    def add(name, pid):
        try:
            pid = int(pid or 0)
        except (TypeError, ValueError):
            return
        if pid > 0 and pid != os.getpid() and pid not in seen and pid_alive(pid):
            seen.add(pid)
            found.append((name, pid))

    d = S.read_json(S.lock_file(root))
    if isinstance(d, dict):
        add("supervisor", d.get("pid"))
    try:
        names = sorted(n for n in os.listdir(S.pdir(root)) if n.endswith(".pid"))
    except OSError:
        names = []
    for fn in names:
        d = S.read_json(os.path.join(S.pdir(root), fn))
        if isinstance(d, dict):
            add(fn[:-4], d.get("pid"))
    return found


def frozen(root, state_fn=None, cmd_fn=None, pids=None):
    """-> [(имя, pid)] приостановленных (T) процессов платформы, которые точно наши."""
    st = state_fn or proc_state
    return [(n, p) for n, p in (platform_pids(root) if pids is None else pids) if st(p) == "T" and is_ours(p, cmd_fn)]


def thaw(root, out=print, state_fn=None, cont_fn=None, cmd_fn=None, wait_s=3.0, sleep=time.sleep):
    """Размораживает приостановленные процессы платформы (SIGCONT). -> (число размороженных, [оставшиеся замороженными (имя, pid)])."""
    st = state_fn or proc_state
    cont = cont_fn or (lambda pid: os.kill(pid, getattr(signal, "SIGCONT", signal.SIGTERM)))
    fz = frozen(root, st, cmd_fn)
    for name, pid in fz:
        out("%s (pid %s) приостановлен (Ctrl+Z?): отправляю SIGCONT" % (name, pid))
        try:
            cont(pid)
        except OSError:
            pass
    end = time.time() + (wait_s if fz else 0)
    left = fz
    while True:
        left = [(n, p) for n, p in fz if pid_alive(p) and st(p) == "T"]
        if not left or time.time() >= end:
            break
        sleep(0.1)
    return len(fz) - len(left), left


def manual_hint(left):
    pids = " ".join(str(p) for _, p in left)
    if procs.is_windows():
        return "Не смог разморозить процессы (%s). Закрой их в Диспетчере задач (завершить дерево процессов Python проекта) и повтори." % pids
    return ("Не смог разморозить процессы (pid %s). Вручную: `kill -CONT %s`; если не помогло: `kill -TERM %s`, затем ./tb start. "
            "Если процесс в фоне терминала, попробуй `fg` в том окне." % (pids, pids, pids))


def term(pid, cmd_fn=None):
    """Мягко просит свой процесс завершиться (SIGTERM, группе, если pid её лидер). -> True, если сигнал отправлен."""
    if not is_ours(pid, cmd_fn):
        return False
    try:
        if procs.is_windows():
            os.kill(pid, signal.SIGTERM)
        elif os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False
