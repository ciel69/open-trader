"""Общая точка входа для обёрток `tb` (sh), `tb.cmd` и `tb.ps1` (Windows): вся платформенная логика здесь, обёртки тонкие.

Что делает перед диспетчером `tb.__main__`:
  1. настраивает консоль: вывод в UTF-8 (кириллица на консоли Windows);
  2. для `bybit <read|trade|testnet> ...`: находит env-файл ключа, читает пары KEY=VALUE в окружение ЭТОГО процесса и снимает
     прокси-переменные (HTTP(S)_PROXY, ALL_PROXY в любом регистре; на Windows ещё отключает прокси из реестра/системы).
     Для остальных групп окружение и прокси не трогаются. Значения из env-файла никуда не печатаются.
  3. для `exec run --profile trade|testnet` (исполнитель v2) то же самое, что для `bybit`: ключ профиля trade -> bybit_mainnet_trade.env,
     testnet -> bybit_testnet.env, прокси снимаются. `exec run --profile paper` и остальные `exec ...` ключа не читают и окружение не трогают.
  4. для `sync run|once` (автосинхронизация капитала): грузится ТОЛЬКО читающий ключ read (баланс), прокси снимаются; нет ключа -> сервис работает в деградации.
     (`brain run` позже будет подгружать anthropic.env; пока не реализовано.)

Где лежат ключи (первый найденный файл побеждает; можно задать каталог через TB_KEYS_DIR):
  macOS/Linux: ~/.config/traider-bot/
  Windows:     %USERPROFILE%\\.config\\traider-bot\\   затем запасной %APPDATA%\\traider-bot\\
Файлы: bybit_mainnet.env (read), bybit_mainnet_trade.env (trade), bybit_testnet.env (testnet).
Формат файла: строки KEY=VALUE, необязательный префикс `export `, значения можно брать в '...' или "...", `#` начинает комментарий.
"""
import os
import re
import sys

PROFILES = {"read": "bybit_mainnet.env", "trade": "bybit_mainnet_trade.env", "testnet": "bybit_testnet.env"}
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
USAGE_BYBIT = "использование: ./tb bybit <read|trade|testnet> <balance|positions|info|fees|open|close|sync|pending|cancel|watch|scan> ..."
EXEC_PROFILES = {"trade": "trade", "testnet": "testnet"}   # профиль exec -> профиль ключа (PROFILES); paper без ключа
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LauncherError(Exception):
    pass


def is_windows(system=None):
    return (system or sys.platform).startswith("win")


def key_dirs(environ=None, system=None):
    """Каталоги, где ищется env-файл ключа, по порядку приоритета."""
    environ = os.environ if environ is None else environ
    if environ.get("TB_KEYS_DIR"):
        return [environ["TB_KEYS_DIR"]]
    if is_windows(system):
        home = environ.get("USERPROFILE") or environ.get("HOME") or os.path.expanduser("~")
        dirs = [os.path.join(home, ".config", "traider-bot")]
        if environ.get("APPDATA"):
            dirs.append(os.path.join(environ["APPDATA"], "traider-bot"))
        return dirs
    home = environ.get("HOME") or os.path.expanduser("~")
    return [os.path.join(home, ".config", "traider-bot")]


def find_key_file(profile, environ=None, system=None):
    """-> путь к существующему файлу профиля или None."""
    for d in key_dirs(environ, system):
        p = os.path.join(d, PROFILES[profile])
        if os.path.isfile(p):
            return p
    return None


def parse_env_text(text):
    """Разбор содержимого env-файла -> dict. Не поддерживает подстановки ($VAR) и многострочные значения."""
    out = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not _KEY_RE.match(k):
            continue
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            m = re.search(r"\s#", v)  # как в sh: комментарий после пробела
            if m:
                v = v[:m.start()]
            v = v.strip()
        out[k] = v
    return out


def read_env_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return parse_env_text(f.read())


def strip_proxies(environ):
    """Удаляет прокси-переменные (любой регистр) из словаря environ на месте; возвращает список удалённых имён (без значений)."""
    removed = []
    for k in list(environ):
        if k.upper() in PROXY_VARS:
            del environ[k]
            removed.append(k)
    return removed


def split_bybit(argv):
    """['bybit', 'read', 'balance'] -> ('read', ['balance']); неверный профиль -> LauncherError."""
    if len(argv) < 2 or argv[1] not in PROFILES:
        raise LauncherError(USAGE_BYBIT)
    return argv[1], list(argv[2:])


def prepare_bybit(argv, environ=None, system=None):
    """Готовит окружение для команды bybit: -> (профиль, остаток аргументов). environ меняется на месте."""
    environ = os.environ if environ is None else environ
    profile, rest = split_bybit(argv)
    path = find_key_file(profile, environ, system)
    if not path:
        where = " или ".join(os.path.join(d, PROFILES[profile]) for d in key_dirs(environ, system))
        raise LauncherError("нет файла ключа для профиля %s (%s)" % (profile, where))
    try:
        values = read_env_file(path)
    except (OSError, UnicodeDecodeError) as e:
        raise LauncherError("не удалось прочитать файл ключа профиля %s: %s" % (profile, type(e).__name__))
    environ.update(values)
    strip_proxies(environ)
    return profile, rest


def exec_profile(argv):
    """['exec','run','--profile','trade',...] или '--profile=trade' -> 'trade'|'testnet'|'paper'|... (как написано) либо None, если не `exec run`/нет флага."""
    if len(argv) < 2 or argv[0] != "exec" or argv[1] != "run":
        return None
    rest = argv[2:]
    for i, a in enumerate(rest):
        if a == "--profile":
            return rest[i + 1] if i + 1 < len(rest) else None
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return None


def exec_exchange(argv):
    """['exec','run',...,'--exchange','okx'] -> 'okx' (как написано) либо None."""
    if len(argv) < 2 or argv[0] != "exec" or argv[1] != "run":
        return None
    rest = argv[2:]
    for i, a in enumerate(rest):
        if a == "--exchange":
            return rest[i + 1] if i + 1 < len(rest) else None
        if a.startswith("--exchange="):
            return a.split("=", 1)[1]
    return None


def prepare_exec(argv, environ=None, system=None):
    """Готовит окружение для `exec run`: для profile trade|testnet грузит ключ и снимает прокси; -> (профиль|None, argv без изменений).
    paper и всё остальное окружение не трогают. Ошибка (нет файла ключа) -> LauncherError без значений."""
    environ = os.environ if environ is None else environ
    prof = exec_profile(argv)
    key_profile = EXEC_PROFILES.get(prof)
    if not key_profile:
        return prof, list(argv)
    exch = exec_exchange(argv)
    if exch not in (None, "bybit"):       # ключ Bybit не отдаётся исполнителю другой биржи; ключи новой биржи launcher получит вместе с её адаптером (docs/EXCHANGES.md)
        raise LauncherError("для биржи %s launcher не знает файлов ключей (реализован только bybit)" % exch[:12])
    path = find_key_file(key_profile, environ, system)
    if not path:
        where = " или ".join(os.path.join(d, PROFILES[key_profile]) for d in key_dirs(environ, system))
        raise LauncherError("нет файла ключа для профиля %s (%s)" % (key_profile, where))
    try:
        values = read_env_file(path)
    except (OSError, UnicodeDecodeError) as e:
        raise LauncherError("не удалось прочитать файл ключа профиля %s: %s" % (key_profile, type(e).__name__))
    environ.update(values)
    strip_proxies(environ)
    return prof, list(argv)


def sync_command(argv):
    """['sync','run'|'once'] -> True: этим командам нужен читающий ключ read (баланс); status/confirm ключа не читают."""
    return len(argv) >= 2 and argv[0] == "sync" and argv[1] in ("run", "once")


def prepare_sync(argv, environ=None, system=None):
    """Окружение сервиса автосинхронизации капитала: грузит ТОЛЬКО читающий ключ read и снимает прокси. Нет файла/поля -> окружение без ключа (сервис сам деградирует и
    сообщает no_key, а не падает). BYBIT_ENV по умолчанию mainnet (файл читающего ключа mainnet). -> True, если ключ загружен."""
    environ = os.environ if environ is None else environ
    path = find_key_file("read", environ, system)
    if not path:
        return False
    try:
        values = read_env_file(path)
    except (OSError, UnicodeDecodeError):
        return False
    if not all(values.get(k) for k in ("BYBIT_API_KEY", "BYBIT_API_SECRET")):
        return False
    environ.update(values)
    environ.setdefault("BYBIT_ENV", "mainnet")
    strip_proxies(environ)
    return True


REQUIRED_FIELDS = ("BYBIT_API_KEY", "BYBIT_API_SECRET")


def key_status(profile, environ=None, system=None):
    """Есть ли ключ биржи профиля (read|trade|testnet; также paper -> не нужен). НИКОГДА не возвращает значения: только имя файла, наличие и имена недостающих полей.
    -> {"ok": bool, "profile", "file": имя|None, "missing": [имена полей], "reason": "paper|no_file|unreadable|fields|ok"}."""
    if profile == "paper":
        return {"ok": True, "profile": profile, "file": None, "missing": [], "reason": "paper"}
    if profile not in PROFILES:
        return {"ok": False, "profile": profile, "file": None, "missing": [], "reason": "no_profile"}
    path = find_key_file(profile, environ, system)
    if not path:
        return {"ok": False, "profile": profile, "file": PROFILES[profile], "missing": list(REQUIRED_FIELDS), "reason": "no_file"}
    try:
        values = read_env_file(path)
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "profile": profile, "file": PROFILES[profile], "missing": list(REQUIRED_FIELDS), "reason": "unreadable"}
    missing = [k for k in REQUIRED_FIELDS if not values.get(k)]
    return {"ok": not missing, "profile": profile, "file": PROFILES[profile], "missing": missing, "reason": "ok" if not missing else "fields"}


def disable_system_proxies():
    """urllib на Windows и macOS берёт прокси ещё и из системных настроек; для bybit отключаем их тоже."""
    import urllib.request
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def setup_stdio():
    """UTF-8 на консоли (особенно Windows с cp1251/cp866); при перенаправлении в файл/пайп тоже."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_stdio()
    if argv and argv[0] == "bybit":
        try:
            profile, rest = prepare_bybit(argv)
        except LauncherError as e:
            print(e, file=sys.stderr)
            return 2
        disable_system_proxies()
        argv = ["bybit"] + rest
    if sync_command(argv):
        prepare_sync(argv)
        disable_system_proxies()
    if exec_profile(argv) in EXEC_PROFILES:
        try:
            prepare_exec(argv)
        except LauncherError as e:
            print(e, file=sys.stderr)
            return 2
        disable_system_proxies()
    from tb.__main__ import main as dispatch
    return dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
