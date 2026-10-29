# Контракт API раздела «Настройки» (провайдеры LLM, профили, роли, биржи, расход)

Это главный документ для вёрстки вкладки «Настройки». UI ничего не знает о файлах: он читает GET-эндпоинты и пишет ctl-действиями через уже существующие
`POST /api/ctl/challenge` и `POST /api/ctl/command` (подпись, CSRF, Origin, права, nonce и фразы делает ctlweb, как для остальных кнопок). Ключ LLM вводится отдельным
write-only маршрутом `POST /api/ctl/llmkey`. Торговые ключи бирж через дашборд не принимаются никогда.

Статус: реализовано и покрыто тестами `tests/test_settings_llm.py`, `test_settings_ctl.py`, `test_exchanges.py` (без сети, ключи поддельные). Шаблоны провайдеров НЕ проверены живым ключом.

## 0. Общие правила

* Чтение: `GET /api/settings/<имя>` (расширение EXT_API, модуль `tb.llm.dashapi`). Ответ `200 {…}`; неизвестное имя `404`; сбой `500 {"error":"settings_failed","kind":"…"}`.
  Данные берутся из конфигурации и файлов состояния; это снимок, обновляйте после каждой записи и раз в 10-15 с.
* Запись: `POST /api/ctl/command {"action": A, "args": {...}, "nonce"?, "phrases"?}`; перед опасным вызовом `POST /api/ctl/challenge` с теми же `action` и `args`
  (ответ содержит `danger`, `phrases`, `gates`, `warnings`, `blocked`, `nonce`); результат команды: `GET /api/ctl/result?id=K-…` (`status`: `done|rejected|failed|expired`,
  `code`, `msg`, `data`). Подробность отказа в `data.detail` (текст по-русски, без путей и ключей).
* Права: сессия нужна всегда. В режиме `open_local` сессия «open» (вход без токена) НЕ может ни одно из действий этого раздела и ввод ключа: нужен токен (сессия «full»,
  сервер вернёт `401 E_ELEVATE`: покажите окно ввода токена один раз). В режиме `token` все сессии «full». Чтение (`GET /api/settings/*`) токена не требует.
* **Переименованные поля.** Все GET-ответы дашборда проходят `scrub`, который вырезает поля со словами `key`, `token`, `secret`. Поэтому в GET-ответах:
  `key` -> `cred`, `key_file` -> `cred_file`, `key_env` -> `cred_env`, `max_tokens` -> `max_out_tok`, `day_tokens` -> `day_toks`, `hour_tokens` -> `hour_toks`,
  `job_tokens` -> `job_toks`, `max_tokens_field` -> `max_out_field`, `tokens` -> `toks`. В аргументах ctl-действий (запись) используются настоящие имена
  (`max_tokens`, `day_tokens` и так далее).
* Деньги в USD (числа), цены моделей за 1M токенов, время в epoch-секундах UTC, идентификаторы строчные латиницей (`[a-z0-9][a-z0-9_-]{0,29}`).
* Значение ключа провайдера не возвращается НИ ОДНИМ маршрутом: только `present` (есть/нет) и `mask` (`…1d1c`, последние 4 символа).

## 1. Провайдеры

`GET /api/settings/providers`

```json
{"providers": [{
   "id": "openai", "name": "OpenAI (ChatGPT API)", "adapter": "openai-compatible", "enabled": false, "error": null,
   "base_url": "https://api.openai.com/v1", "verified": false, "note": "НЕ ПРОВЕРЕНО живым ключом: …",
   "cred": {"needed": true, "present": false, "mask": null, "file": "openai.env", "env": "OPENAI_API_KEY", "perms_ok": null},
   "timeouts": {"connect_s": 10, "read_s": 120, "total_s": 300}, "retry": {"max": 3, "base_s": 1, "max_s": 8, "on": [429, 500, 503]},
   "limits": {"concurrent": 2, "rpm": 60, "day_usd": 3.0, "hour_usd": 1.0, "day_toks": 2000000},
   "models": [{"id": "mini", "name": "gpt-4.1-mini", "ctx": 1000000, "max_out": 8192, "tools": true, "strict": false, "json_mode": true, "thinking": "none",
               "cache": "auto", "price": {"in": 0.4, "in_hit": 0.1, "out": 1.6}, "tags": ["cheap"], "max_out_field": "max_completion_tokens", "no_temperature": false}],
   "local": true, "builtin": false, "profiles": ["oai-mini"]}],
 "errors": [], "budget": {"day_usd": 2.0, "hour_usd": 0.5, "day_toks": 4000000, "hour_toks": 1000000, "job_usd": 0.05, "warn_frac": 0.8}}
```

* `adapter`: `deepseek | anthropic | openai-compatible` (в конфигурации допустим также псевдоним `openai_compat`). Через `openai-compatible` работают OpenAI/ChatGPT,
  Gemini (OpenAI-совместимая точка Google), OpenRouter, Mistral, Groq, Ollama, LM Studio: только конфигом, без правки кода.
* `cred.needed=false` у локальных серверов. `cred.present` и `cred.mask` показывать как «ключ есть (…1d1c)» / «нет». `perms_ok=false` означает, что файл ключа читаем другим пользователям (предупредить).
* `verified=false` у всех добавленных шаблонов: показывать бейдж «не проверено живым ключом»; `verified` ставится вручную владельцем (`update` с `verified:true`) после `./tb llm check --live`.
* `builtin=true`: провайдер из поставляемого `config/providers.json` (удалить нельзя, выключить можно). `local=true`: есть запись в `config/local/providers.json`.
* `profiles`: профили, которые на него ссылаются (удаление/смена блокируются).

`GET /api/settings/presets`: `{"presets": [{"id","name","adapter","base_url","verified":false,"note","models":["mini","full"],"cred_file","cred_env","added":bool}]}`.
Шаблоны: openai, gemini, openrouter, anthropic, deepseek, groq, mistral, local (Ollama), lmstudio. Все с `verified:false`.

### Действие `llm.provider` (запись)

`args = {"op": …, "id": "<провайдер>", …}`. Все операции только пишут слой `config/local/providers.json` (поставляемый файл не меняется) и просят демон мозга перечитать конфиги.

| op | доп. поля args | что делает | типичные отказы (`code` / `data.detail`) |
|---|---|---|---|
| `add` | `preset` и/или `spec` | добавить провайдера ВЫКЛЮЧЕННЫМ: из шаблона и/или своей записи (`spec` поверх шаблона; ≤ 2000 байт) | `E_EXISTS`, `E_ARGS` (проверка адаптера, URL, моделей, цен) |
| `update` | `patch` (поля: name, adapter, base_url, beta_url, key_file, key_env, timeouts, retry, limits, note, verified, version_header) | изменить настройки | `E_ARGS` |
| `model_set` | `model` (ключ), `spec` (name, ctx, max_out, tools, strict, json_mode, thinking, cache, price, tags, max_tokens_field, no_temperature) | добавить/изменить модель; `name` = имя у провайдера (из `GET /models` или документации) | `E_ARGS` |
| `model_remove` | `model` | удалить добавленную модель | `E_IN_USE` (на неё ссылается профиль), `E_STATE` (поставляемая или последняя) |
| `enable` | — | включить провайдера | `E_NO_KEY` (ключа нет), `E_ARGS` (ошибка конфигурации) |
| `disable` | — | выключить | `E_LIVE_IN_USE` (провайдер в цепочке live-роли) |
| `remove` | — | удалить добавленного провайдера | `E_IN_USE` (есть профили), `E_STATE` (поставляемый) |
| `check` | `model?` | проверка БЕЗ сети: конфигурация и наличие ключа; результат `data.ok`, `data.checked[]` (`config_error`, `cred`, `note`) | отчёт, не ошибка команды |
| `key_clear` | — | удалить файл ключа | `E_IN_USE` (провайдер включён и используется) |

Живая проверка (GET /models, пробный запрос) делается только в терминале: `./tb llm check --provider ID --live` (тратит деньги и использует ключ владельца). Рекомендуйте её текстом после добавления.

### Ввод ключа: `POST /api/ctl/llmkey` (write-only)

Тело: `{"provider": "openai", "value": "<ключ>"}` с заголовками как у любого POST ctl (`Origin`, `Content-Type: application/json`, `X-TB-CSRF`, cookie сессии «full»).
Ответ `200 {"ok":true,"provider":"openai","cred":{"present":true,"mask":"…1d1c","perms_ok":true}}`. Значение нигде не возвращается. Ошибки: `400 E_ARGS|E_KEY` (ключ не принят:
длина 8-400, печатный ASCII без пробелов; имя файла зарезервировано), `404 E_UNKNOWN` (нет такого провайдера или ему не нужен ключ), `401 E_AUTH|E_ELEVATE`, `403 E_CSRF|E_ORIGIN`, `429 E_RATE`.
Правила для вёрстки: поле `type="password"`, `autocomplete="new-password"`, `spellcheck=false`; после отправки очистить поле и значение в состоянии страницы; не класть ключ в URL, `localStorage`, логи консоли;
после успеха перечитать `GET /api/settings/providers`. Кнопки «показать ключ» быть не может: возврата нет.

## 2. Модели и профили

Профиль = (провайдер, модель, параметры вызова). `GET /api/settings/profiles`:

```json
{"profiles": [{"id": "ds-pro", "provider": "deepseek", "model": "pro", "model_name": "deepseek-v4-pro",
               "params": {"temperature": 0.1, "max_out_tok": 2500, "thinking": "low"}, "live_ok": false, "eval_ts": null,
               "provider_enabled": true, "used_by": ["A.position_manager", "shared.reviewer"], "builtin": true}],
 "errors": [], "rev": 3, "source": "active"}
```

`live_ok`: метку ставит ТОЛЬКО живой eval владельца в терминале (`./tb brain eval --live --confirm`); из UI поставить нельзя. Смена модели или параметров профиля делает метку недействительной.
`used_by`: роли, в чьих цепочках стоит профиль (`<команда>.<роль>` или `shared.<роль>`). `builtin=true`: профиль из `config/brain.json` (удалить нельзя, менять можно слоем local).

### Действие `brain.profile`

| op | args | что делает | отказы |
|---|---|---|---|
| `set` | `id`, `provider?`, `model?`, `params?` (`temperature` 0..2, `max_tokens` 16..65536, `thinking` off\|low\|medium\|high) | создать профиль или изменить поля существующего | `E_ARGS` (нет такой модели), `E_PROVIDER_OFF` (провайдер выключен), `E_LIVE_IN_USE` (профиль используют live-роли в режиме live) |
| `delete` | `id` | удалить добавленный владельцем профиль | `E_IN_USE`/`E_LIVE_IN_USE` (стоит в цепочке роли), `E_STATE` (поставляемый) |

«Использовать профиль X для всех ролей одной командой»: существующее действие `brain.use` с `{"profile": "X", "all": true}` (роли live-команды и общие роли одной ревизией).
В режиме live оно идёт по прежним гейтам и фразам (challenge вернёт `danger`, `phrases`, `warnings`).

## 3. Роли

`GET /api/settings/roles`: таблица «роль -> основной/запасные/параметры/live_ok/стоимость за сутки».

```json
{"roles": [{"role": "position_manager", "scope": "team", "team": "A", "live_risk": true, "io": "tools", "known": true,
            "primary": {"id": "ds-pro", "provider": "deepseek", "model": "pro", "model_name": "deepseek-v4-pro", "provider_enabled": true, "live_ok": false,
                        "params": {"temperature": 0.1, "max_out_tok": 2500, "thinking": "low"}},
            "fallback": [{"id": "an-sonnet", "provider": "anthropic", "…": "…"}],
            "live_ok": false, "params_override": {"temperature": 0.7}, "params_effective": {"temperature": 0.7, "max_out_tok": 2500, "thinking": "low"},
            "budget": {"day_usd": 0.25},
            "today": {"usd": 0.0123, "calls": 14, "errors": 0, "budget_left_usd": 0.2377}}],
 "live_team": "A", "rev": 3, "source": "active", "mode": "paper", "errors": []}
```

`mode`: режим платформы (`observe|paper|testnet|live`); при `live` строки с `live_risk=true` менять почти нельзя (см. ниже), показывайте замок. `source`: `active` (подписанный
`state/brain/active.json`), `config` (файл `config/brain.json`) или `untrusted` (подпись не прошла: действует конфиг, показать предупреждение). `today.budget_left_usd=null`, если бюджет роли не задан.

### Действие `brain.role`

| op | args | что делает |
|---|---|---|
| `chain` | `role` (`A.position_manager`, `shared.reviewer` или `news_reader`), `primary?`, `fallback?` (список до 4 профилей без повторов), `accepted?` | основной профиль и/или цепочка запасных (состав и порядок) новой подписанной ревизией; откат: `brain.rollback` |
| `opts` | `role`, `params?` (temperature, max_tokens, thinking), `budget?` (`{"day_usd": число}`) | переопределение параметров на уровне роли (побеждает профиль) и суточный бюджет роли; значение `null` снимает поле |
| `opts_clear` | `role` | снять все переопределения роли |

Правила для live: для live-роли live-команды в режиме live можно менять только основной профиль (`chain` с `primary`), и это то же, что `brain.use`: challenge вернёт жёсткие условия
(`E_SWAP_NO_LIVE_RIGHTS`, `E_SWAP_PROBE`, `E_SWAP_AMBIGUOUS_STATE`, `E_SWAP_LIMIT_DAY`), мягкие предупреждения (`accepted`), фразу и nonce. Состав и порядок запасных и `opts` live-роли в live не меняются
(`E_STATE`/`E_LIVE_IN_USE`: остановите день). Для paper/observe и общих ролей изменения применяются сразу. Каждое действие попадает в журнал аудита ctl; смены цепочек ещё и в `state/brain/swaps.jsonl` (хеш-цепочка).

`GET /api/settings/revisions`: `{"rev","source","warn","revisions":[номера для отката],"swaps":[{seq,ts,rev,prev_rev,kind,tier,target,from,to,by}],"chain_ok":bool}`.
Откат: существующее `brain.rollback` (`{"to": N}` необязателен).

## 4. Расход по моделям

`GET /api/settings/usage?days=7&by=model|provider|profile|role|team`

```json
{"days": 7, "by": "model", "total": {"usd": 0.31, "toks": 120345, "calls": 211}, "today": {"usd": 0.04, "toks": 9000, "calls": 17},
 "groups": [{"id": "flash", "calls": 180, "in": 90000, "in_hit": 40000, "out": 12000, "reasoning": 0, "usd": 0.11, "errors": 1, "fmt_errors": 0, "fallbacks": 2,
             "lat_p50_ms": 900, "lat_p95_ms": 2400, "err_rate": 0.0055, "fmt_err_rate": 0.0, "cache_hit_rate": 0.44}],
 "budget": {"state": "ok", "scope": "all", "key": "day_usd", "used": 0.04, "cap": 2.0, "until_ts": 1793145600.0, "items": []},
 "providers": [{"id": "deepseek", "state": "ok"}]}
```

`budget.state`: `ok|warn|exhausted`. Диаграммы: расход по группам, доля ошибок, кэш. Учёт ведётся по ключу модели (`flash`), а не по имени у провайдера.

## 5. Биржи

`GET /api/settings/exchanges`

```json
{"exchanges": [{"id": "bybit", "name": "Bybit (USDT-перпетуалы)", "adapter": "bybit", "enabled": true, "mode": "paper", "implemented": true, "error": null,
                "cred_files": {"read": "bybit_mainnet.env", "trade": "bybit_mainnet_trade.env", "testnet": "bybit_testnet.env"},
                "limits": {"max_open_positions": null}, "symbols": {"allow": [], "deny": []}, "note": "…",
                "active": true, "allowed": true, "exec": {"present": true, "age_s": 3.1, "alive": true}},
               {"id": "okx", "implemented": false, "enabled": false, "note": "не реализовано: заготовка", "…": "…"}],
 "active": "bybit", "allowed": ["bybit"], "errors": [], "platform": {"state": "stopped", "mode": "observe"}, "editable": true}
```

* `implemented=false`: заготовка; включить и сделать активной нельзя (сервер откажет `E_ARGS` с `data.detail`), в UI показывать серым с пометкой «не реализовано».
* `active`: одна биржа, на которую по умолчанию идут новые планы; `allowed`: биржи, для которых запускаются исполнители (параллельный запуск: по одному исполнителю на биржу).
* `mode`: режим исполнителя этой биржи по умолчанию (`paper|testnet|live`). `exec`: heartbeat исполнителя биржи (`alive` если моложе 60 с).
* `cred_files`: только имена файлов ключей для справки; ключи бирж вводятся в терминале (`./tb setup`), через дашборд не принимаются.
* `editable=false`: платформа запущена, изменения бирж запрещены (`E_GATE`): показать кнопки неактивными.

### Действие `exchange.set`

| op | args | что делает |
|---|---|---|
| `activate` | `id` | сделать биржу активной (она должна быть включена и разрешена) |
| `enable` / `disable` | `id` | включить/выключить (выключить активную нельзя) |
| `mode` | `id`, `mode` | режим исполнителя биржи |
| `allowed` | `ids` | список разрешённых бирж (активная обязана остаться) |
| `limits` | `id`, `max_open_positions` (0..50 или `null`) | лимит открытых позиций ИМЕННО на этой бирже (общий лимит профиля считает guard по всем биржам) |

Гейты: любое изменение только при остановленной платформе (`E_GATE`, `gates[0].id="E_STATE"`); включение live (`mode: live`, а также `activate`/`enable` биржи с `mode: live`) требует nonce и фразу
`ВКЛЮЧАЮ LIVE НА БИРЖЕ <ID>` (из challenge). Изменения вступают в силу при следующем старте платформы.

## 6. Сводка

`GET /api/settings/overview`: `{"exchange":{"active","allowed"},"mode","rev","source","live_team","providers":{"total","enabled","no_cred":[id включённых без ключа]},"profiles":n,"roles":n,"usage_today":{usd,toks,calls},"errors":[…]}`. Подходит для заголовка вкладки и индикаторов.

## 7. Коды результатов настроек (добавлены в `protocol.CODES`)

`E_EXISTS` запись уже есть; `E_NO_KEY` нет ключа провайдера; `E_IN_USE` используется профилями/ролями; `E_LIVE_IN_USE` используется live-ролью; `E_PROVIDER_OFF` провайдер выключен; `E_CHECK` проверка нашла проблемы;
плюс прежние `E_ARGS`, `E_STATE`, `E_GATE`, `E_NONCE`, `E_PHRASE`, `E_WARN`, `E_WARN_CHANGED`, `E_SWAP_*`, `E_AUTH`, `E_ELEVATE`.

## 8. Порядок экранов (подсказка вёрстке)

1. «Провайдеры»: таблица (имя, адаптер, включён, ключ есть/нет + кнопка «ввести ключ», модели, бейдж «не проверено»), кнопки «добавить из шаблона» (`presets` -> `add`), «проверить» (`check`), «включить», «выключить», «удалить».
2. «Модели и профили»: модели провайдера (`model_set`) и профили (`brain.profile`), кнопка «этот профиль для всех ролей» (`brain.use all`).
3. «Роли»: таблица `roles`; редактор цепочки (основной, запасные, порядок), параметры роли, бюджет, расход за сутки; замок на live-ролях в режиме live.
4. «Биржи»: таблица `exchanges`, выбор активной, режимы, лимиты; заготовки серым.
5. «Расход»: `usage` по моделям/провайдерам/ролям.
6. «Ревизии»: `revisions` + кнопка отката.

## 9. Что НЕ входит

Нативные адаптеры (Gemini REST, Cohere и т.д.), живая проверка из UI, ввод/чтение торговых ключей бирж в дашборде, реальные вторые биржи (okx, binance): только заготовки.
