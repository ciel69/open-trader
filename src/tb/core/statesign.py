"""Подпись файлов состояния, которые пишет только владелец (WP11-C2: `state/brain/active.json`). Тот же ключ и тот же HMAC, что у файла уровня риска
(`risklevels`): ключ читает ровно одна функция ядра, здесь только тонкая обёртка без собственного обращения к каталогу ключей.

    key = statesign.key()                      # bytes | None (нет ключа -> подписать нельзя, проверить нельзя)
    obj = statesign.sign(body, key)            # -> копия с полем mac
    statesign.verify(obj, key)                 # -> bool
Слои brain/gateway/mcp/llm не упоминают модуль уровня риска: им нужна только подпись/проверка своего файла (тест `test_risklevels_cli.NoAgentPath`).
"""
from tb.core import risklevels


def key(environ=None):
    return risklevels._read_key(environ)


def sign(obj, key_bytes):
    body = {k: v for k, v in obj.items() if k != "mac"}
    body["mac"] = risklevels.sign_state(body, key_bytes)
    return body


def verify(obj, key_bytes):
    return risklevels.verify_state(obj, key_bytes)
