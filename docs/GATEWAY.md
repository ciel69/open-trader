# Шлюз инструментов и MCP (WP11-A)

Один фиксированный каталог инструментов для любого агента: Claude в чате Claude Code (через MCP) и демон на API (in-process). Код: `src/tb/gateway/`
(`catalog.py` инструменты, `registry.py` агенты и токены, `audit.py` журнал, `rpc.py` диспетчер, `cli.py` `./tb gateway`) и `src/tb/mcp/server.py`.
Агент через шлюз НЕ получает: ключи, файловую систему, shell, сеть, биржевой код, прямые ордера, правку `config/risk_profile.json` и `exec_limits.json`,
снятие KILL, смену режима исполнителя (kill/resume только человек). Шлюз вызывает только функции пакетов мозга и ядра (brief, events_cli, plan_cli, plan_schema,
bus, decisions, ideas, expstats, journal); слои проверяет `tests/test_layering.py` (нет импортов venues, executor, subprocess, socket, http, urllib, launcher).

## Каталог (22 инструмента)
Колонки: имя, право, вид, аргументы (`*` обязательный). Описания для модели (английский, с единицами) лежат в `catalog.py`; `./tb gateway tools` печатает их.

| Инструмент | Право | Вид | Аргументы |
|---|---|---|---|
| `get_status` | read | чтение | - |
| `get_brief` | read | чтение | scope, since, budget_tokens |
| `get_events` | read | чтение | after, limit, wake_only |
| `wait_events` | read | чтение | timeout_s, after, limit, wake_only |
| `ack_events` | events.ack | запись | *seq |
| `list_plans` | read | чтение | status, mode, limit |
| `show_plan` | read | чтение | *id, rev, events, decisions |
| `submit_plan` | plan.submit | запись | *plan, reason, wait_s, event_seq, brief_id |
| `amend_plan` | plan.manage | запись | *id, *rev, *patch, *reason, wait_s, event_seq, brief_id |
| `stop_plan` | plan.manage | запись | *id, *rev, *to, *reason, wait_s, event_seq, brief_id |
| `close_plan` | plan.manage | запись | *id, frac, *reason, wait_s, event_seq, brief_id |
| `cancel_plan` | plan.manage | запись | *id, *reason, wait_s, event_seq, brief_id |
| `hold_plan` | plan.manage | запись | *id, *reason, wait_s, event_seq, brief_id |
| `release_plan` | plan.manage | запись | *id, *reason, wait_s, event_seq, brief_id |
| `add_idea` | idea.write | запись | *sym, *side, *note, level, stop, tp, tag |
| `list_ideas` | read | чтение | status, limit |
| `post_mortem` | journal.post | запись | *trade_id, err, rule_followed, lesson |
| `get_stats` | read | чтение | by, mode, min_n |
| `get_decisions` | read | чтение | plan, after, limit |
| `get_similar_cases` | memory.read | чтение | sym, side, setup, k, scope, features |
| `list_lessons` | memory.read | чтение | status, limit |
| `add_case_review` | memory.write | запись | *case_id, *review |

Память (WP15b, `docs/spec_v2/08_case_memory.md`): `get_similar_cases` и `list_lessons` отдают только числа, enum и идентификаторы (без текстов решений), ответ <= 2 КБ, не чаще 30 вызовов/час на агента, случаи строго до «сейчас» (`as_of`); live скрыт, пока в `config/memory.json` нет `show_in_live: true` (ответ `warn: MEMORY_HIDDEN_LIVE`); `agents_off`/`enabled` выключают память агенту (`E_MEMORY_OFF`). `add_case_review` пишет вывод `tb.casereview/1` только в вызове роли `reviewer` (раннер ставит `meta.role`; иначе `E_NOT_REVIEWER`), `case_id` обязан совпасть с заданием (`E_CASE_MISMATCH`), один вывод на случай (`E_REVIEW_EXISTS`), лишние поля и ложные `known_at_decision` отклоняются (`E_REVIEW`), текст `alternative.detail` очищается (суммы USDT, ключи, URL, инъекции). Шлюз ставит план-тег `mem.on|off`. Инструментов чтения файлов и сырого текста нет.

Планы: единственный путь к торговле. `submit/amend/stop/close/cancel/hold/release` кладут команду в `state/plans/_inbox` (через `plan_cli.send`), исполнитель проверяет guard
и пишет ответ в `_outbox`. Ответ `ok:null, status:queued` = команда в очереди, повторно не слать. Размер, плечо и маржу агент не задаёт никогда.
`wait_events` ждёт до 60 с wake-события (или `exec_down`, не чаще раза в 5 минут); `get_events` читает без ожидания; обработку подтверждает `ack_events`.
Выход каждого инструмента проходит белый список полей верхнего уровня и `scrub` (управляющие и невидимые символы, длина <= 300, ключи только идентификаторы, конечные числа,
глубина <= 8); текст третьих лиц в данные не попадает (brief/events и так отдают только числа и идентификаторы).

## Модель прав и ограничений
`config/agents.json` (пишет владелец; агентов правят только он или `./tb gateway agent add`):
- `auth`: `token` (по умолчанию; для процессов за границей доверия, токен обязателен при старте MCP) или `local` (запуск самим владельцем, токен не нужен; так заведён `chat`).
- `tools`: имена или `@право` (`@read`, `@events.ack`, `@plan.submit`, `@plan.manage`, `@idea.write`, `@journal.post`). Опечатка отключает агента целиком (fail closed).
- `limits` (только СУЖАЮТ guard и exec_limits; ослабить риск-профиль шлюз не может): `max_risk_usdt`, `max_risk_pct` (риск плана = guard_preview.risk_usdt * size_mult, в % капитала;
  не удалось посчитать = отказ), `symbols` (список или `["*"]`, пусто = ничего), `modes` (`paper`/`live`; новый агент только paper; план без `mode` у paper-агента становится paper),
  `max_size_mult`, `allow_add` (добор/пирамидинг, по умолчанию запрещён; также в `shadow_variants`), `calls_per_min` (в памяти процесса), `plans_per_hour` (по аудиту, переживает перезапуск).
- Дополнительно: `how.type=adopt` через submit запрещён; при активном KILL submit отклоняется сразу; `stop_plan` принимает только более строгий стоп; `amend_plan` сначала прогоняется
  офлайн через `plan_schema.amend` (расширение стопа в позиции, рост риска, смена символа, устаревший rev дают отказ до очереди); операции над чужим символом или режимом отклоняются.
- Порядок проверок: агент известен и включён -> инструмент существует -> право -> лимит частоты -> схема входа (лишние поля отбрасываются и считаются в `dropped_fields`, длинные строки и
  неверные форматы отклоняются) -> ограничения агента -> обработчик -> фильтр выхода -> аудит. Коды отказа: `E_AGENT_UNKNOWN`, `E_AGENT_DISABLED`, `E_TOOL_UNKNOWN`, `E_TOOL_DENIED`,
  `E_RATE_LIMIT`, `E_ARGS`, `E_SYMBOL_FORBIDDEN`, `E_MODE_FORBIDDEN`, `E_SIZE_MULT`, `E_RISK_LIMIT`, `E_RISK_PCT`, `E_RISK_UNKNOWN`, `E_ADD_FORBIDDEN`, `E_RATE_PLANS`, `E_KILL`,
  `E_STOP_WIDEN`, `E_NOT_FOUND`, `E_INTERNAL` (текст исключения наружу не отдаётся).
- Кто решил: команды плана пишутся с `actor = brain:<id агента>` (id <= 10 символов); `core/decisions.py` принимает такой actor (`valid_actor`), исполнитель кладёт его в журнал решений,
  дашборд группирует по префиксу `brain`. В `created_by` плана остаётся значение из enum схемы (`brain:chat` или `brain:api`).
- Аудит `state/gateway_audit.jsonl` (append-only, под FileLock): время, агент, инструмент, хэш аргументов (16 hex), ok/код, мс, транспорт. Аргументов, результатов и токенов нет.
  `./tb gateway audit --tail 20`. Ротации нет (строка ~170 байт).

## Как добавить агента
1. `./tb gateway agent add scout --role analyst --tools @read,@idea.write` (auth=token, режим paper, символов нет, лимиты по умолчанию).
2. Отредактировать `config/agents.json`: `limits.symbols`, риск и т.д. (только вручную владельцем). `./tb gateway check` покажет ошибки записей.
3. `./tb gateway token new scout` печатает токен ОДИН раз и пишет в файл только `sha256`-хэш. Токен положить в `~/.config/traider-bot/agents/scout.token` (0600; или флаг `--write-file`,
   Windows: `%USERPROFILE%\\.config\\traider-bot\\agents\\`) либо передать процессу агента в переменной `TB_AGENT_TOKEN`. Не в аргументах командной строки.
4. Живой режим для агента владелец включает сам: `"modes": ["paper","live"]`.

## Подключение
**MCP в Claude Code (агент `chat`)** — не применено, фрагмент для владельца. Версии протокола: 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05 (клиентскую версию эхом, иначе 2025-11-25);
в поиске 2026-07-28 значится новой ревизией, но её статус не подтверждён и убирает сессии протокола; для stdio это не должно мешать, `tools/list` и `tools/call` сервер отдаёт и без `initialize`.
Команда (из корня проекта, область проекта; `/ABS/PATH/TO/traider-bot` замените на абсолютный путь к вашей копии репозитория):
```
claude mcp add --transport stdio --scope project tb-gateway -- /ABS/PATH/TO/traider-bot/tb mcp serve --agent chat
```
или `.mcp.json` в корне проекта:
```json
{"mcpServers": {"tb-gateway": {"type": "stdio", "command": "/ABS/PATH/TO/traider-bot/tb", "args": ["mcp", "serve", "--agent", "chat"], "env": {}}}}
```
Windows: `"command": "C:\\path\\traider-bot\\tb.cmd"`. Для агента с `auth=token` токен берётся из `TB_AGENT_TOKEN` или файла (в `env` конфига клиента класть токен не рекомендуется).
Инструменты появятся как `mcp__tb-gateway__get_status` и т.д.; ресурсы `tb://status`, `tb://brief`, `tb://events` (их читает тот же шлюз с теми же правами). После подключения права чата на
`./tb plan ...` в `PERMISSIONS.md` можно сузить: торговые решения идут через шлюз.
Сервер: `./tb mcp serve --agent ID` (подпроцесс клиента, stdio, одна JSON-строка на сообщение). Долгие вызовы (`wait_events`) идут в отдельном потоке, `notifications/cancelled` их прерывает.

**API-демон (WP11-C, in-process):** `from tb import gateway; gateway.call("api", "get_brief", {"scope": "event"})` -> dict; `gateway.tools_for("api")` даёт объекты инструментов
(`.name`, `.desc`, `.input_schema()`) для списка tools в Messages API. Агент `api` по умолчанию только paper. Токен для in-process вызова не проверяется (граница доверия = процесс демона);
если демон работает отдельным процессом, проверять `registry.authenticate(agent, token)` при старте.

## Ограничения MCP для чата (честно)
- MCP-сервер не может разбудить простаивающий чат: он только отвечает на запросы клиента, инициировать ход модели нельзя. `wait_events` (до 60 с) работает, пока у модели идёт ход;
  когда пользователь молчит и ход закончен, события копятся в шине и будут прочитаны при следующем обращении (`get_status` показывает backlog).
  Для пробуждения остаётся прежний путь (`./tb events --follow` под Monitor) или API-демон. Возможные push-механизмы клиента (каналы Claude Code) не проверены и не используются.
- Один процесс сервера на сессию клиента; `calls_per_min` считается в памяти этого процесса, `plans_per_hour` по общему аудиту.

## Модель безопасности и пределы
- Граница: инструменты и схемы, не изоляция ОС. Шлюз и исполнитель работают под одним пользователем ОС; изоляция процессом (у MCP-процесса нет ключей: BYBIT_*/ANTHROPIC_*/токен удаляются из его
  окружения при старте), а не контейнером. Агент с собственным shell (как Claude Code в чате) может обойти шлюз, написав в `_inbox` или вызвав `./tb`; поэтому шлюз надёжен только для агентов,
  у которых НЕТ shell и файлов (API-демон, другие MCP-клиенты без инструментов ОС), а для чата это дисциплина и аудит, а не барьер. Барьер от ордеров даёт guard и исполнитель (отдельный держатель ключа).
- Токены хранятся хэшем (SHA-256, токен 256 бит случайных), сравнение constant-time; в аудит, ответы и сообщения об ошибках не попадают (тесты). Файл токена читает любой процесс того же пользователя.
- `config/agents.json` и `state/gateway_audit.jsonl` доступны владельцу на запись; агентам через шлюз ни тем ни другим писать нельзя (нет такого инструмента).
- Тексты самого агента (`thesis`, `reason`, `note`, `lesson`) попадают в журналы как есть после очистки и усечения; их читает следующий сеанс мозга, поэтому это канал само-инъекции, а не внешней.
- Не проверено на живой сессии Claude Code (подключение .mcp.json не применялось); проверено ручным подпроцессом и тестами `test_gateway_core.py`, `test_mcp_server.py`.

## Провайдеры и роли (WP-X, кратко)
Шлюз не знает про провайдеров: роль берёт профиль из цепочки назначений (`state/brain/active.json`). Добавление провайдеров и профилей и смена цепочек ролей без правки кода описаны в `docs/BRAIN.md`
(«Провайдеры, профили и роли без правки кода») и `docs/spec_v2/SETTINGS_API.md`; шлюз и MCP по-прежнему не импортируют ctl, биржи и файлы ключей.

## Дополнения WP11-C2 (рантайм мозга)
- Торговый флаг дня: `submit_plan` с режимом не paper, а также `amend_plan`, ослабляющий риск, проходят только при `state/control/trading.json` (`enabled`, `mode` live|testnet, `agent`, `until_ts`) и совпадении `day_id` с `state/control/platform.json`;
  нечитаемый/отсутствующий файл = отказ (`E_TRADING_FLAG|OFF|MODE|EXPIRED|DAY|AGENT`). Paper, стоп, закрытие, отмена, hold/release доступны всегда (`registry.trading_allowed`).
- `rpc.call(..., meta={...})`: контекст вызова от раннера мозга (профиль, задание, роль). Из него шлюз сам ставит теги `agent.<id>` и `model.<профиль>` в `tags.extra` плана (чужие `agent.`/`model.` убираются), они доходят до журнала сделок.
- Добор в плане требует `allow_add` агента И `risklevels.pyramid_policy` (уровень риска, exec_limits); сетап агрессивнее уровня риска отклоняется `E_SETUP_AGGR`.
