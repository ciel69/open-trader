# DeepSeek API: справка для адаптера (stdlib)

Актуальность: 2026-10-28. Метод: только WebSearch (WebFetch в среде отказал: "proxy refused the connection"), официальные страницы api-docs.deepseek.com напрямую НЕ прочитаны, только фрагменты в выдаче поиска. Остальное - сторонние блоги.
Уверенность: В = высокая (официальный источник или много согласных источников), С = средняя (согласованные сторонние), Н = низкая/противоречиво/не найдено. Всё важное проверить на живом ключе (раздел 8).

## 1. Базовое
- Base URL: https://api.deepseek.com (В); для beta-фич (strict tools, prefix completion) https://api.deepseek.com/beta (В). Эндпоинт: POST /chat/completions (в). Совместим с OpenAI chat-completions (В).
- Авторизация: `Authorization: Bearer <key>`, `Content-Type: application/json` (В).
- Модели (С, противоречия по именам!):
  - Поколение V4 (весна-лето 2026): `deepseek-v4-flash`, `deepseek-v4-pro`; контекст 1M, макс. выход до ~384K (С).
  - Старые алиасы `deepseek-chat` / `deepseek-reasoner` = flash в non-thinking/thinking; по одному источнику сняты 2026-07-24 (Н - проверить, могут уже не работать).
  - Октябрьские источники: актуальные имена `deepseek-flash` (V4.1 Flash, запуск ~2026-09-10) и `deepseek-v4-pro` (С/Н). Имена в адаптер вынести в конфиг; на живом ключе вызвать GET /models и взять оттуда.
- Chat vs reasoner: теперь не отдельные модели, а режим thinking, включается параметром запроса (thinking type enabled + reasoning_effort) (С; точные имена полей проверить по api-docs.deepseek.com/api/create-chat-completion/). Токены рассуждений тарифицируются как выход (С).

## 2. Инструменты, форматы, стриминг
- tools / tool_choice (auto|none|required|конкретная функция), OpenAI-формат (В). Параллельные tool_calls в одном ответе возможны (С).
- Strict mode: `"strict": true` внутри function, только на /beta base URL; схема: все свойства в required, additionalProperties:false у каждого объекта (В по офиц. описанию, детали С). Beta.
- JSON mode: `response_format={"type":"json_object"}` (С); в промпте обязательно слово "json" и пример формата, иначе возможен пустой/зацикленный вывод (С, по прежней документации). Полноценного json_schema вне strict-tools не найдено (Н).
- Thinking + tools: если запрос содержит tools, `reasoning_content` из ответа ассистента нужно возвращать в следующих запросах целиком (в message ассистента), иначе HTTP 400 (С, подтверждено багом opencode #24130). Без tools возвращать не нужно. В адаптере: хранить assistant-сообщение как есть (content, reasoning_content, tool_calls) и пересылать без изменений.
- Стриминг: stream:true, SSE, `data: [DONE]`; во время ожидания приходят `: keep-alive` (В). `stream_options.include_usage` для usage в стриме (С).
- Для автономного бота стриминг не нужен; non-stream проще, но учесть пустые строки keep-alive до тела ответа (В): http.client это переживает, json.loads от strip().

## 3. Цены (за 1M токенов, USD) - С, не сверено с офиц. страницей
- Схема с августа-сентября 2026: пик/непик, непик = 50% пика (С, несколько источников согласны).
- Пик: пн-пт 01:00-04:00 и 06:00-10:00 UTC, кроме праздников КНР; выходные непик весь день (С).
- deepseek-flash (V4.1 Flash): непик: кэш-попадание 0.003 / промах 0.15 / выход 0.60; пик: 0.006 / 0.30 / 1.20 (С).
- deepseek-v4-pro: непик вход(промах) 0.66 / выход 1.98; пик 1.32 / 3.96 (С); цена кэш-попадания Pro не найдена (Н; ранее 0.003625-0.0145 по старой схеме).
- Прежние цены (до ~16 авг 2026): flash 0.14/0.28, кэш-хит 0.0028 (С) - в источниках остались, не путать.
- Кэш: автоматическое префиксное кэширование контекста включено по умолчанию, настройки не нужны (В); совпадение считается по общему префиксу (кэш-единицы порядка 64 токенов, С). Следствие: стабильный системный промпт и инструкции в начале, меняющиеся данные в конце.
- Оценка для бота: даже 50K входных токенов на цикл на flash ~ $0.0075 при промахе (пик $0.015) - стоимость пренебрежимо мала относительно капитала 30 USDT.

## 4. Лимиты и перегрузка
- Фиксированных RPM/TPM нет, лимит динамический по нагрузке (В: страница rate_limit). Сторонние цифры (напр. 500 одновременных) не подтверждены (Н).
- Коды: 400 неверный формат, 401 ключ, 402 нет баланса, 422 параметры, 429 лимит скорости, 500 ошибка сервера, 503 перегрузка (В). 429/500/503 - повторять; 400/401/402/422 - нет.
- Если вывод не начался ~10 минут, сервер закрывает соединение (С/Н).
- Retry-After не документирован (С). Рекомендация: экспоненциальный backoff с джиттером (1,2,4,8 с, 4-5 попыток), общий таймаут запроса 120-600 с для thinking (reasoning долго), connect-таймаут 10 с.
- Стабильность в пик: исторически периодические 503/"server busy" в пик и заметные задержки (С, общее мнение). Для бота: fallback "не торговать/держать позицию" при недоступности LLM; открытые позиции обязаны иметь стоп на бирже.

## 5. Учёт расхода и баланс
- usage в ответе: prompt_tokens, completion_tokens, total_tokens, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `completion_tokens_details.reasoning_tokens` (В/С по OpenAI-совместимости и ранней документации).
- Баланс: GET /user/balance (Bearer) -> `is_available`, `balance_infos[{currency, total_balance, granted_balance, topped_up_balance}]` (В по прежней документации; сверить). Пополнение предоплатное, при нуле - 402.
- Расчёт: стоимость = hit*цена_hit + miss*цена_miss + completion*цена_out, цена зависит от часа UTC (учесть пик).

## 6. Данные и конфиденциальность
- Серверы и хранение: данные собираются, обрабатываются и хранятся в КНР (В/С, политика конфиденциальности). Срок хранения API-запросов не указан (С).
- Обучение на API-данных: источники противоречат (политика перечисляет обучение как цель, условия API молчат) (Н). Считать, что промпты могут использоваться.
- Передача третьим лицам/госорганам допускается политикой (С).
- Рекомендация: в промпты не отправлять ключи, адреса кошелька, точные суммы капитала, персональные данные; только рыночные данные и обезличенное состояние. Решение об отправке позиций/баланса - за владельцем.

## 7. Особенности для автономного агента (С, опыт сообщества; официального "рекомендованного" валидатора нет, кроме strict tools beta и json_object)
- Типовые проблемы: невалидный JSON/усечённые аргументы (особенно при малом max_tokens, в тестах одного источника аргументы обрезались на ~96 токенах в thinking+strict), лишний текст вокруг JSON, пропуск вызова инструмента при tool_choice=auto, зацикливание повторных вызовов, пустой content при json_object без слова "json".
- Меры: max_tokens с запасом (thinking съедает бюджет), temperature низкая (для tool use/JSON ~0-0.3; для V4 в thinking параметры temperature/top_p могут игнорироваться, С/Н), системный промпт короткий и жёсткий, явная схема и пример.
- Валидация на нашей стороне обязательна: json.loads -> проверка схемы руками (stdlib) -> при ошибке 1-2 повтора с сообщением об ошибке -> иначе действие "ничего не делать". Лимит итераций tool-цикла (напр. 8) и детектор повторов одинаковых вызовов. Любое торговое действие проходит `./tb guard`, LLM не обходит риск-лимиты.
- Параллельные вызовы: исполнять последовательно, ответ на каждый tool_call_id обязателен.

## 8. Проверить на живом ключе
1. GET /models: реальные имена моделей; работают ли deepseek-chat/deepseek-reasoner.
2. Точные поля включения thinking и reasoning_effort; поведение temperature в thinking.
3. Обязательность возврата reasoning_content при tools (воспроизвести 400) и без tools.
4. strict tools на /beta: схема принимается, ограничения типов.
5. response_format json_object; json_schema (скорее всего нет).
6. usage: наличие prompt_cache_hit_tokens/miss, reasoning_tokens; реальный кэш-хит на втором одинаковом префиксе.
7. GET /user/balance формат.
8. Фактическая цена: сверить списание с таблицей, пик/непик окна.
9. Латентность thinking-запроса, коды и частота 429/503, наличие keep-alive.
10. Условия API (ToS) по обучению и хранению - прочитать оригинал.

## Источники
- https://api-docs.deepseek.com/api/create-chat-completion/ (офиц., только фрагмент strict/beta)
- https://api-docs.deepseek.com/quick_start/rate_limit (офиц., keep-alive, динамический лимит)
- https://api-docs.deepseek.com/quick_start/pricing (офиц.; НЕ открыт)
- https://github.com/anomalyco/opencode/issues/24130 (reasoning_content passback, 400)
- https://aihubmix.com/blog/deepseek-v4-pro-0813-thinking-passback-3-api-matrix
- https://chat-deep.ai/docs/deepseek-tool-calls/ ; https://macaron.im/blog/deepseek-v4-tool-calling
- https://apidog.com/blog/deepseek-v4-1-flash-pricing/ ; https://www.yottalabs.ai/post/deepseek-v4-1-flash-pricing-specs-v4-pro-routing-2026 ; https://costgoat.com/pricing/deepseek-api ; https://benchlm.ai/deepseek/api-pricing ; https://www.nihardaily.com/posts/deepseek-v4-pricing-now-depends-on-what-time-you-run-it
- https://deepseekai.guide/api/deepseek-api-rate-limits/ ; https://fast.io/resources/deepseek-rate-limit/
- https://deepseekai.guide/guides/deepseek-privacy/ ; https://www.developersdigest.tech/models/data-retention/deepseek
