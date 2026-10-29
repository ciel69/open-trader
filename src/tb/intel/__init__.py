"""Фактура и новости (WP13a, сборщик БЕЗ LLM). Владелец: WP13.

Сервис `intel` (`python -m tb.intel.service run`, под `./tb up --services ...,intel`) по расписанию тянет публичные источники (без ключей),
разбирает ответы и пишет:
  runtime/intel/facts.json      структурная фактура для brief: только числа, enum, идентификаторы (текст третьих лиц сюда не попадает);
  runtime/intel/blackouts.json  объединение окон blackout (watch_config.blackouts + календарь событий);
  runtime/intel/items.jsonl     очищенные ЗАГОЛОВКИ новостей: только для показа владельцу и будущего карантинного читателя (WP13b).
Правило слоёв: модуль `tb.intel.items` не импортируют brain, gateway, mcp, memory, core, executor (тест tests/test_intel_layers.py).
Модули: sources (описания источников и парсеры), fetch (загрузка, кэш, backoff), items (очистка и хранение заголовков), calendar (события,
blackout, рынок США), store (пути и сборка facts.json), service (цикл), verify (проверка живых источников владельцем), cli (`./tb intel ...`).
Только stdlib, Python 3.9. Сетевые вызовы: ТОЛЬКО fetch.http_get_text (в тестах подменяется).
"""
