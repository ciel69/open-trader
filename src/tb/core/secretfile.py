"""Запись и проверка файлов с ключами LLM-провайдеров (write-only). Чистая библиотека: пути каталога ключей сюда ПЕРЕДАЮТ (знают только llm/keys.py и control/auth.py).

Файл ключа: `<каталог>/<key_file>`, одна строка `KEY_ENV=значение`, права 0600, запись атомарная (tmp + replace). Принимаются только ключи LLM-провайдеров:
имена файлов и переменных бирж и служебных файлов (файл ключа подписи управления, токены агентов) запрещены `check_spec`, чтобы ни записать, ни подменить, ни использовать торговый
ключ как «ключ провайдера» (иначе он ушёл бы на чужой base_url). Значение ключа наружу не возвращается ни одной функцией: только «есть/нет» и маска `…1d1c`.
Ключ не попадает в исключения (тексты ошибок фиксированные).
"""
import os
import re
import sys

MIN_LEN, MAX_LEN = 8, 400
RE_VALUE = re.compile(r"^[\x21-\x7e]+$")                                  # печатный ASCII без пробелов и переводов строк (нельзя внедрить вторую строку файла)
RE_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}\.env$")
RE_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,60}$")
FORBIDDEN_FILE_PREFIX = ("bybit", "okx", "binance", "bitget", "kucoin", "gate", "exchange", "control", "agent", "token")
FORBIDDEN_ENV_PREFIX = ("BYBIT_", "OKX_", "BINANCE_", "BITGET_", "KUCOIN_", "GATE_", "EXCHANGE_", "TB_")


class SecretError(Exception):
    """Ошибка записи ключа; текст без значения ключа."""


def check_spec(key_file, key_env):
    """-> текст ошибки или None: имя файла и переменная годятся для ключа LLM-провайдера."""
    if not isinstance(key_file, str) or not RE_FILE.match(key_file):
        return "key_file: строчные буквы, цифры, _ и -, суффикс .env"
    if key_file.startswith(FORBIDDEN_FILE_PREFIX):
        return "key_file: имя зарезервировано для ключей бирж и служебных файлов"
    if not isinstance(key_env, str) or not RE_ENV.match(key_env):
        return "key_env: ЗАГЛАВНЫЕ буквы, цифры и _"
    if key_env.startswith(FORBIDDEN_ENV_PREFIX):
        return "key_env: префикс зарезервирован для ключей бирж"
    return None


def mask(value):
    return "…" + value[-4:] if isinstance(value, str) and len(value) >= 12 else "…"


def restrict(path, is_dir=False):
    """POSIX chmod 0600/0700; Windows icacls (только текущему пользователю). -> bool."""
    if sys.platform.startswith("win"):
        try:
            import subprocess
            r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % (os.environ.get("USERNAME") or "")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            return r.returncode == 0
        except Exception:
            return False
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
        return True
    except OSError:
        return False


def _find_value(text, key_env):
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        if sep and k.strip() == key_env:
            v = v.strip()
            if v[:1] in ("'", '"') and len(v) > 1:
                end = v.find(v[0], 1)
                v = v[1:end] if end > 0 else v[1:]
            return v.strip()
    return ""


def status(dirpath, key_file, key_env, environ=None):
    """-> {"present","mask","perms_ok"}: ключ в файле (или в переменной окружения key_env). Значение не возвращается."""
    val = ""
    if isinstance(key_file, str) and RE_FILE.match(key_file):
        try:
            with open(os.path.join(dirpath, key_file), encoding="utf-8-sig") as f:
                val = _find_value(f.read(), key_env)
        except OSError:
            val = ""
    if not val and key_env:
        val = ((os.environ if environ is None else environ).get(key_env) or "").strip()
    perms = None
    if val and os.name == "posix" and key_file:
        try:
            perms = (os.stat(os.path.join(dirpath, key_file)).st_mode & 0o077) == 0
        except OSError:
            perms = None
    return {"present": bool(val), "mask": mask(val) if val else None, "perms_ok": perms}


def write_key(dirpath, key_file, key_env, value):
    """Атомарно пишет ключ в файл 0600. -> {"present":True,"mask":...}. Значение не возвращается."""
    bad = check_spec(key_file, key_env)
    if bad:
        raise SecretError(bad)
    if not isinstance(value, str):
        raise SecretError("ключ должен быть строкой")
    v = value.strip()
    if not (MIN_LEN <= len(v) <= MAX_LEN) or not RE_VALUE.match(v):
        raise SecretError("ключ: от %d до %d печатных символов ASCII без пробелов" % (MIN_LEN, MAX_LEN))
    new_dir = not os.path.isdir(dirpath)
    os.makedirs(dirpath, exist_ok=True)
    if new_dir:
        restrict(dirpath, is_dir=True)
    path = os.path.join(dirpath, key_file)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key_env, v))
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    restrict(path)
    return {"present": True, "mask": mask(v), "perms_ok": (os.stat(path).st_mode & 0o077) == 0 if os.name == "posix" else None}


def clear_key(dirpath, key_file, key_env):
    """Удаляет файл ключа провайдера. -> {"present": False}."""
    bad = check_spec(key_file, key_env)
    if bad:
        raise SecretError(bad)
    try:
        os.remove(os.path.join(dirpath, key_file))
    except FileNotFoundError:
        pass
    except OSError:
        raise SecretError("файл ключа не удалён")
    return {"present": False, "mask": None, "perms_ok": None}
