"""Уведомления об итогах дня: macOS (osascript) и Windows (PowerShell), НЕОБЯЗАТЕЛЬНО (`config/lifecycle.json: notify.os`, по умолчанию выключено). WP14.

Без shell. Текст только из шаблона с числами (чужого текста нет); кавычки, обратные слэши и управляющие символы вырезаются. На Windows текст идёт через
переменную окружения TB_NOTIFY_TEXT, а не интерполяцией в скрипт. Отказ уведомления не ошибка (возвращается ok=False).
"""
import os
import platform
import re
import subprocess
import sys

TITLE = "traider-bot"
PS_SCRIPT = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
             "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
             "$n.ShowBalloonTip(5000, 'traider-bot', $env:TB_NOTIFY_TEXT, [System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Seconds 6; $n.Dispose()")
_BAD = re.compile(r"[\"'\\`$;&|<>\x00-\x1f\x7f]")


def clean(text, n=160):
    return " ".join(_BAD.sub(" ", str(text or "")).split())[:n]


def template(kind, rep):
    """Текст уведомления только из чисел отчёта."""
    net = ((rep.get("pnl") or {}).get("net")) or 0.0
    if kind == "start":
        return "День %s запущен (%s)" % (rep.get("day_id"), rep.get("mode"))
    if kind == "emergency":
        return "Аварийная остановка дня %s, итог %+.2f USDT" % (rep.get("day_id"), net)
    return "Итог дня %s: %+.2f USDT, перенесено %d, вопросов %d, оценка %s" % (rep.get("day_id"), net, len(rep.get("carried") or []), len(rep.get("open_questions") or []),
                                                                     rep.get("verdict"))


def send_os(text, cfg, runner=None, system=None, environ=None, force=False):
    """-> (ok, how). Выключено настройкой (notify.os=false) -> (False, "disabled"), если не force."""
    if not force and not ((cfg or {}).get("notify") or {}).get("os"):
        return False, "disabled"
    if runner is None and "unittest" in sys.modules:
        return False, "test"  # тесты не должны слать настоящие уведомления ОС
    runner = runner or subprocess.run
    sysname = system or platform.system()
    t = clean(text)
    try:
        if sysname == "Darwin":
            runner(["osascript", "-e", 'display notification "%s" with title "%s"' % (t, TITLE)], shell=False, timeout=10, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "osascript"
        if sysname == "Windows":
            env = dict(environ if environ is not None else os.environ)
            env["TB_NOTIFY_TEXT"] = t
            runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT], shell=False, timeout=20, check=False, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "powershell"
    except Exception:
        return False, "failed"
    return False, "unsupported"
