"""Генерация файлов автозапуска ОС (только печать: ничего не устанавливается и не запускается). Владелец: WP11-B.

macOS: launchd plist (`~/Library/LaunchAgents/com.traider-bot.platform.plist`, ставит владелец: `launchctl load`).
Windows: PowerShell-скрипт, регистрирующий задачу Планировщика (запускает владелец сам, один раз).
Инструкция: docs/RUNBOOK.md.
"""
import os
import posixpath
from xml.sax.saxutils import escape

LABEL = "com.traider-bot.platform"
TASK = "TraiderBotPlatform"


def _args(passthru):
    return [a for a in passthru if a not in ("--detach", "--dry-run")]


def launchd_plist(root, passthru=()):
    """plist: RunAtLoad + перезапуск при НЕнулевом выходе (`./tb down` завершает платформу кодом 0: launchd её не поднимает обратно)."""
    root = str(root).replace("\\", "/")            # plist читает macOS: пути всегда с «/», даже если файл напечатан на Windows
    prog = [posixpath.join(root, "tb"), "up"] + _args(passthru)
    items = "\n".join("        <string>%s</string>" % escape(x) for x in prog)
    out = posixpath.join(root, "runtime", "logs")
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Сгенерировано командой ./tb up с флагом печати launchd. Не установлено. Установка (владелец): сохранить как ~/Library/LaunchAgents/%(label)s.plist,
     затем `launchctl load ~/Library/LaunchAgents/%(label)s.plist`. Снять: `launchctl unload ...`. Подробности: docs/RUNBOOK.md -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%(label)s</string>
    <key>ProgramArguments</key>
    <array>
%(items)s
    </array>
    <key>WorkingDirectory</key>
    <string>%(root)s</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>%(out)s/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>%(out)s/launchd.err.log</string>
</dict>
</plist>
""" % {"label": LABEL, "items": items, "root": escape(root), "out": escape(out)}


def windows_script(root, passthru=()):
    args = " ".join(_args(passthru))
    cmd = os.path.join(root, "tb.cmd")
    return """# Сгенерировано `./tb up --print-windows`. НЕ запущено. Запуск (владелец, один раз, PowerShell):
#   сохранить как register-tb-platform.ps1 и выполнить; снять: Unregister-ScheduledTask -TaskName %(task)s -Confirm:$false
# Платформа стартует при входе пользователя; при сбое Планировщик повторяет запуск (до 5 раз с интервалом 1 минута).
$root = "%(root)s"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument '/c ""%(cmd)s"" up %(args)s' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "%(task)s" -Action $action -Trigger $trigger -Settings $settings -Description "traider-bot: платформа (./tb up)"
""" % {"task": TASK, "root": root, "cmd": cmd, "args": args}
