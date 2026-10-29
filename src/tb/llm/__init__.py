"""Слой LLM (WP11-C1): провайдеры, адаптеры, учёт, бюджет, проверка ответов, цикл инструментов.

Карта:
  types.py      нейтральные сообщения/запрос/ответ, LLMError (без имён провайдеров и моделей)
  keys.py       чтение ключа из файла провайдера (единственное место с обращением к каталогу ключей), Secret с маской
  transport.py  HTTP на stdlib (http.client): таймауты, keep-alive, повторы только по списку кодов
  providers.py  реестр config/providers.json (имена моделей и цены ТОЛЬКО оттуда) и проверка конфигурации
  adapters/     openai_compat (база), deepseek, anthropic: нейтральный формат <-> формат провайдера
  usage.py      цена вызова, state/llm_calls.jsonl (без текста промптов), агрегаты
  budget.py     потолки токенов и долларов (сутки/час/задание), событие limit_hit(brain_budget)
  schema.py     строгая проверка JSON-ответа по схеме, повтор-подсказка
  toolloop.py   цикл вызовов инструментов шлюза
  client.py     LLMClient: бюджет -> адаптер -> учёт (то, чем пользуется runtime мозга, WP11-C2)
  cli.py        ./tb llm providers|check|usage

Слой не импортирует tb.venues, tb.executor, tb.launcher, tb.marketdata.
"""
