"""Ключи провайдеров. ЕДИНСТВЕННЫЙ модуль слоя llm, знающий про каталог ключей пользователя.

Ключ читается из файла провайдера (`key_file` в providers.json) в каталоге ключей (TB_KEYS_DIR или ~/.config/traider-bot), а если файла нет,
из переменной окружения `key_env` (её мог положить launcher). Ключ НИКОГДА не кладётся в os.environ, аргументы, логи, исключения и ответы:
наружу он выходит только как Secret (repr/str с маской) и только в заголовок запроса внутри адаптера.
"""
import os
import re
import sys

from tb.llm.types import LLMError

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
# грубая маска для текстов, пришедших от провайдера (эхо ключа в тексте ошибки)
_RE_LOOKS_LIKE_KEY = re.compile(r"(sk|key|tok|pk|api)[-_][A-Za-z0-9_\-]{8,}", re.I)


class Secret(object):
    __slots__ = ("_v",)

    def __init__(self, value):
        self._v = value

    def reveal(self):
        return self._v

    def mask(self):
        v = self._v
        return "…" + v[-4:] if len(v) >= 12 else "…"

    def __repr__(self):
        return "Secret(%s)" % self.mask()

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Secret не сериализуется")


def key_dirs(environ=None, system=None):
    environ = os.environ if environ is None else environ
    if environ.get("TB_KEYS_DIR"):
        return [environ["TB_KEYS_DIR"]]
    win = (system or sys.platform).startswith("win")
    home = (environ.get("USERPROFILE") if win else None) or environ.get("HOME") or os.path.expanduser("~")
    dirs = [os.path.join(home, ".config", "traider-bot")]
    if win and environ.get("APPDATA"):
        dirs.append(os.path.join(environ["APPDATA"], "traider-bot"))
    return dirs


def keys_dir_hint():
    """Каталог ключей для сообщений владельцу (не секрет)."""
    return key_dirs()[0]


def _parse_env(text):
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
            m = re.search(r"\s#", v)
            if m:
                v = v[:m.start()]
            v = v.strip()
        out[k] = v
    return out


def load(key_file, key_env, environ=None, system=None):
    """-> Secret или None (провайдеру ключ не нужен: key_file и key_env пусты). Нет ключа -> LLMError('no_key') без пути и значений."""
    if not key_file and not key_env:
        return None
    env = os.environ if environ is None else environ
    val = None
    if key_file and _RE_FILE.match(key_file):
        for d in key_dirs(env, system):
            p = os.path.join(d, key_file)
            try:
                with open(p, encoding="utf-8-sig") as f:
                    kv = _parse_env(f.read())
            except OSError:
                continue
            val = kv.get(key_env) if key_env else (next(iter(kv.values()), None))
            if val:
                break
    if not val and key_env:
        val = env.get(key_env)
    if not val:
        raise LLMError("no_key", "ключ провайдера не найден (файл %s в каталоге ключей или переменная %s)" % (key_file or "-", key_env or "-"))
    return Secret(val.strip())


def present(key_file, key_env, environ=None):
    """-> (есть_ключ, маска|None) для ./tb llm check (значение не раскрывается)."""
    try:
        s = load(key_file, key_env, environ)
    except LLMError:
        return False, None
    return (True, s.mask()) if s is not None else (True, None)


def redact(text, secret=None, limit=300):
    """Убирает ключ и похожее на ключ из текста, пришедшего извне; режет длину и управляющие символы."""
    t = text if isinstance(text, str) else ""
    if secret is not None:
        v = secret.reveal() if isinstance(secret, Secret) else str(secret)
        if v:
            t = t.replace(v, "***")
    t = _RE_LOOKS_LIKE_KEY.sub("***", t)
    t = "".join(ch if ch.isprintable() else " " for ch in t)
    return re.sub(r"\s+", " ", t).strip()[:limit]
