"""`./tb control <команда>`: управление плоскостью управления из терминала владельца. Владелец: WP12.

Ключ подписи и токен создаются САМИ при первом `./tb start`/`./tb up` (токен печатается один раз); вручную ничего делать не нужно.
Токен нужен только для опасных действий (live, риск вверх): дашборд спросит его один раз и запомнит браузер (docs/CONTROL.md).
  init-key               (необязательно) создать ключ подписи, если его нет (существующий НЕ перезаписывается; значение не печатается)
  token new|show-once    новый токен: печатается ОДИН раз, в файле только хеш; старые токен и запомненные браузеры перестают действовать
  status                 есть ли ключ/токен/права файла; статус сервиса ctl и платформы
  gates [--mode live]    жёсткие гейты H1-H8 и мягкие предупреждения W1-W9 сейчас (то же, что видит кнопка «Старт»)
  audit [-n 20] [--verify]   хвост журнала аудита и проверка хеш-цепочки
  cmd ACTION [--args JSON] [--nonce N] [--phrase "..."]...   подписать и отправить команду (src=cli); опасные действия: сначала `cmd challenge --args '{"action":..,"args":{..}}' --wait`
  run                    запустить сервис ctl (обычно его поднимает супервизор)
Снять KILL здесь нельзя: `./tb exec resume`.
"""
import argparse
import json
import os
import subprocess
import sys

from tb import paths
from tb.control import audit, auth, client, protocol

_HELP = __doc__


def harden(path):
    """Windows: оставить файл только текущему пользователю (icacls). POSIX: chmod уже сделан при записи."""
    if not sys.platform.startswith("win"):
        return True
    try:
        user = os.environ.get("USERNAME") or ""
        r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _warn_perms():
    st = auth.status()
    if not harden(auth.control_file()):
        print("предупреждение: права на файл ключа не удалось ограничить автоматически; проверьте вручную (только ваш пользователь)", file=sys.stderr)
    return st


def ensure_auth(out=print):
    """Первый запуск платформы: ключ подписи и токен создаются сами, если их нет (существующие не трогаются). Токен печатается ОДИН раз. Вызывает `./tb up|start`."""
    try:
        r = auth.ensure()
        if r["key_created"] or r["token"]:
            harden(auth.control_file())
        if r["key_created"]:
            out("создан ключ подписи команд управления (значение не показывается)")
        if r["token"]:
            out("ТОКЕН ДЛЯ ОПАСНЫХ ДЕЙСТВИЙ (live, повышение риска): показан один раз, в файле только хеш. Сохраните в менеджере паролей.")
            out(r["token"])
            out("Для paper вход в дашборд без токена; токен спросят один раз при первом опасном действии. Новый токен: ./tb control token new")
        return r
    except OSError as e:
        print("предупреждение: не удалось создать ключ управления (%s); дашборд будет только для просмотра" % type(e).__name__, file=sys.stderr)
        return {"key_created": False, "token": None}


def cmd_init_key(a):
    created = auth.init_key()
    _warn_perms()
    print("ключ подписи создан" if created else "ключ подписи уже есть, не изменён")
    print("токен: ./tb control token new (при первом ./tb start создаётся сам)")
    return 0


def cmd_token(a):
    if a.rest[:1] not in (["new"], ["show-once"]):
        print("использование: ./tb control token new|show-once", file=sys.stderr)
        return 2
    if a.rest[:1] == ["show-once"]:       # токен в открытом виде не хранится: «показать» = выпустить новый и показать один раз
        auth.init_key()
    tok = auth.new_token()
    if tok is None:
        print("нет ключа подписи: ./tb control init-key", file=sys.stderr)
        return 1
    _warn_perms()
    print("ТОКЕН ДЛЯ ОПАСНЫХ ДЕЙСТВИЙ (показан один раз, сохраните в менеджере паролей; в файле только хеш):")
    print(tok)
    print("Дашборд спросит его один раз при первом опасном действии (live, риск вверх) и запомнит браузер. Прежний токен и запомненные браузеры больше не действуют.")
    return 0


def cmd_status(a):
    st = auth.status()
    print("ключ подписи: %s; токен: %s; права файла: %s" % ("есть" if st["key"] else "НЕТ (создастся при ./tb start)", "есть" if st["token"] else "НЕТ (создастся при ./tb start)",
                                                         {True: "ok", False: "СЛИШКОМ ОТКРЫТЫ", None: "не проверялись"}[st["perms_ok"]]))
    s = client.read_status(None)
    p = s.get("platform") or {}
    print("ctl: %s; платформа: %s, режим %s, день %s" % ("не отвечает" if s.get("stale") else "жив", p.get("state"), p.get("mode"), p.get("day_id")))
    return 0


def cmd_gates(a):
    from tb.control import ctlstate, gates
    import time
    ev = gates.evaluate(paths.root(), {"mode": a.mode, "session": a.session}, time.time(), ctlstate.load_cfg(paths.root()))
    for g in ev["gates"]:
        print("%s %-4s %s" % ("OK  " if g["ok"] else "СТОП", g["id"], g["msg"]))
    for w in ev["warnings"]:
        print("ПРЕД %-4s %s" % (w["id"], w["msg"]))
    print("итог: %s" % ("жёсткие гейты пройдены" if ev["ok"] else "ЖЁСТКИЕ ГЕЙТЫ НЕ ПРОЙДЕНЫ"))
    return 0 if ev["ok"] else 1


def cmd_audit(a):
    for r in audit.tail(None, a.n):
        print("%s %-26s %-6s %-14s %-8s %s" % (r.get("ts"), r.get("id"), r.get("src"), r.get("action"), r.get("code"), r.get("result")))
    if a.verify:
        v = audit.verify(None)
        print("цепочка: %s (записей %d%s)" % ("цела" if v["ok"] else "НАРУШЕНА", v["n"], "" if v["ok"] else ", сбой на seq %s" % v["bad_seq"]))
        return 0 if v["ok"] else 1
    return 0


def cmd_cmd(a):
    if not a.rest or a.rest[0] not in protocol.ACTIONS:
        print("действие: " + " | ".join(protocol.ACTIONS), file=sys.stderr)
        return 2
    try:
        args = json.loads(a.args or "{}")
    except ValueError:
        print("--args: некорректный JSON", file=sys.stderr)
        return 2
    ok, msg, args = protocol.validate_args(a.rest[0], args)
    if not ok:
        print("параметры: " + msg, file=sys.stderr)
        return 2
    cmd = client.build(a.rest[0], args, src="cli", sess="s-%s" % os.urandom(3).hex(), nonce=a.nonce, phrases=a.phrase or None)
    if cmd is None:
        print("нет ключа подписи: ./tb control init-key", file=sys.stderr)
        return 1
    client.submit(cmd)
    print("отправлено: %s" % cmd["id"])
    if a.wait:
        r = client.wait_result(cmd["id"], timeout=a.timeout)
        print(json.dumps(r, ensure_ascii=False, indent=1) if r else "результата нет (ctl не запущен?)")
        return 0 if r and r.get("code") == "OK" else 1
    return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0 if argv else 2
    if argv[0] == "run":
        from tb.control import service
        return service.main(["run"] + argv[1:])
    ap = argparse.ArgumentParser(prog="tb control")
    ap.add_argument("cmd", choices=["init-key", "token", "status", "gates", "audit", "cmd"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--mode", default="live")
    ap.add_argument("--session", default="intraday")
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--args", default=None)
    ap.add_argument("--nonce", default=None)
    ap.add_argument("--phrase", action="append")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--timeout", type=float, default=8.0)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    return {"init-key": cmd_init_key, "token": cmd_token, "status": cmd_status, "gates": cmd_gates, "audit": cmd_audit, "cmd": cmd_cmd}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
