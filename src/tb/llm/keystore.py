"""Запись ключей LLM-провайдеров (WRITE-ONLY) для терминала и сервисов: тонкая обёртка над tb.core.secretfile с каталогом ключей из llm/keys.py.

Читает ключи только keys.py; здесь ключ только записывается, значение наружу не возвращается (результат: «есть/нет» и маска `…1d1c`). Принимаются ТОЛЬКО ключи
LLM-провайдеров (`check_spec`: торговые ключи бирж и служебные файлы недоступны). Ключ не логируется, не попадает в аудит, очередь команд и ответы.
"""
from tb.core import secretfile
from tb.core.secretfile import SecretError, check_spec          # noqa: F401  (реэкспорт для вызывающих)
from tb.llm import keys


def keys_dir():
    return keys.key_dirs()[0]


def status(key_file, key_env):
    return secretfile.status(keys_dir(), key_file, key_env)


def set_key(key_file, key_env, value):
    return secretfile.write_key(keys_dir(), key_file, key_env, value)


def clear_key(key_file, key_env):
    return secretfile.clear_key(keys_dir(), key_file, key_env)
