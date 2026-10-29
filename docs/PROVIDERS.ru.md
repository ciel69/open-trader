# Подключение LLM-провайдера

[English](PROVIDERS.md) | назад в [README](../README.ru.md)

Платформа работает и без LLM: исполнитель, сканер, дашборд и профиль paper живут, день стартует с консервативным планом. **Мозг** (роли LLM: утренний план, сопровождение позиций, разбор закрытых сделок) включается, только когда выполнены три условия:

1. включён хотя бы один **провайдер** и есть его **API-ключ**;
2. **профиль** (провайдер + модель + параметры) назначен на **роли** (назначения по умолчанию лежат в `config/brain.json`);
3. включён **подписчик `api`** (в поставляемом конфиге он выключен: свежий клон сам никогда не обращается к LLM).

> **Честный статус.** Все шаблоны провайдеров **не проверены живым ключом**: имена моделей, адреса и цены взяты из документации и памяти, их нужно сверить самому (`./tb llm check --provider ID --live`). Нативные адаптеры есть только у **Anthropic** и **DeepSeek**. Всё остальное (OpenAI, Gemini, Groq, Mistral, OpenRouter, Ollama, LM Studio, свой адрес) идёт через один общий **OpenAI-совместимый** адаптер (`/chat/completions`, tool calls, JSON-режим), проверенный только на локальном фейковом сервере. Живой мозг в деле не проверялся: промпты и форматы ответов тестировались на фейковом LLM. Начинайте с `paper`, дешёвой модели и малых бюджетов.

Все команды запускаются из корня репозитория. Ключи не попадают в репозиторий и нигде не печатаются: они лежат в `~/.config/traider-bot/` (Windows: `%USERPROFILE%\.config\traider-bot\`; другой каталог: `TB_KEYS_DIR`).

## 1. Выберите шаблон

```
./tb llm presets          # JSON: id, адаптер, base_url, модели, файл/переменная ключа, "added" (уже в реестре)
./tb llm providers        # что настроено сейчас: включён ли, есть ли ключ (только маска ...1d1c), лимиты, цены
```

| id | адаптер | адрес по умолчанию | ключи моделей | файл ключа / переменная | в реестре сразу |
|---|---|---|---|---|---|
| `deepseek` | нативный | `https://api.deepseek.com` | `flash`, `pro` | `deepseek.env` / `DEEPSEEK_API_KEY` | да, **включён** |
| `anthropic` | нативный | `https://api.anthropic.com` | `haiku`, `sonnet` | `anthropic.env` / `ANTHROPIC_API_KEY` | да, выключен |
| `local` (Ollama) | openai-compatible | `http://127.0.0.1:11434/v1` | `qwen` (встроен) / `main` (шаблон) | нет | да, выключен |
| `openai` | openai-compatible | `https://api.openai.com/v1` | `full`, `mini` | `openai.env` / `OPENAI_API_KEY` | нет |
| `gemini` | openai-compatible | `https://generativelanguage.googleapis.com/v1beta/openai` | `flash`, `pro` | `gemini.env` / `GEMINI_API_KEY` | нет |
| `groq` | openai-compatible | `https://api.groq.com/openai/v1` | `main` | `groq.env` / `GROQ_API_KEY` | нет |
| `mistral` | openai-compatible | `https://api.mistral.ai/v1` | `main` | `mistral.env` / `MISTRAL_API_KEY` | нет |
| `openrouter` | openai-compatible | `https://openrouter.ai/api/v1` | `auto` | `openrouter.env` / `OPENROUTER_API_KEY` | нет |
| `lmstudio` | openai-compatible | `http://127.0.0.1:1234/v1` | `main` | нет | нет |

«Ключ модели» это короткий локальный псевдоним; в API уходит настоящее имя `name` (шаг 3).

## 2. Добавьте провайдера (для DeepSeek, Anthropic и Ollama не нужно: они уже в реестре)

CLI:
```
./tb llm provider add gemini --preset gemini      # запись в config/local/providers.json, добавлен ВЫКЛЮЧЕННЫМ
```
Дашборд: **Система -> Настройки -> Провайдеры -> «Добавить провайдера из шаблона» -> «Добавить выключенным»** (нужен токен дашборда один раз: он печатается при первом `./tb start`, потерян: `./tb control token new`).

![Настройки: подключения ИИ](images/settings.png)

## 3. Ключ, имя модели, включение (порядок важен: без ключа провайдера не включить)

```
./tb llm key set gemini            # скрытый ввод (нужен настоящий терминал); пишет ~/.config/traider-bot/gemini.env с правами 0600
./tb llm model set gemini flash --name gemini-2.5-flash --price-in 0.3 --price-out 2.5 --tools --json-mode
./tb llm provider enable gemini
./tb llm check --provider gemini                  # без сети: конфиг верный, файл ключа есть
```
Другие способы дать ключ:
- **Файл**: `~/.config/traider-bot/<провайдер>.env`, одна строка `GEMINI_API_KEY=...` (без кавычек, `chmod 600`). Для DeepSeek это делает `./tb setup` со скрытым вводом.
- **Переменная окружения** с тем же именем, экспортированная в оболочке, где запускается `./tb start` (работает, если файла нет; надёжнее файл).
- **Дашборд**: карточка провайдера -> «Ввести ключ» (write-only, обратно не показывается; только для LLM-провайдеров). Ключи бирж дашборд **не принимает никогда**, только терминал (`./tb setup`).

Имена и цены поправьте под свой аккаунт: точный id модели берите из списка моделей провайдера (`GET /models`), цены из тарифа. Неверное имя модели самая частая причина первой живой ошибки. `--price-in/--price-out` в USD за 1M токенов; по ним считается бюджет, нулевая цена отключает защиту стоимостью для этой модели.

## 4. Профиль и роли

**Профиль** = провайдер + модель + параметры (temperature, max_tokens, thinking). У **роли** есть цепочка: `primary` и запасные `fallback` по порядку.

```
./tb brain profile set gem-flash --provider gemini --model flash --set temperature=0.1 --set max_tokens=2500
./tb brain profile list

./tb brain role list                                              # роли, цепочки, бюджеты, расход сегодня
./tb brain role chain position_manager --primary gem-flash --fallback ds-flash
./tb brain use all gem-flash                                      # или: один профиль сразу на ВСЕ роли
```

| роль | команда/общая | когда просыпается | что делает |
|---|---|---|---|
| `morning_analyst` | команда `A` | старт дня (задание), блэкаут макро, смена уровня риска | утренний брифинг, план дня |
| `position_manager` | команда `A` | вход исполнен, веха, конец сессии, связь | держать / подтянуть стоп / закрыть открытую позицию |
| `entry_scout` | команда `A` | сигнал сканера | предлагает (или пропускает) вход; только paper |
| `reviewer` | общая | позиция закрыта | разбор закрытой сделки |
| `day_auditor` | общая | закрытие дня (задание) | итог дня, решения о переносе |
| `news_reader` | общая | вызывается на заголовки новостей | карантинный читатель текстов третьих лиц (только строгий JSON) |

Имена ролей: `position_manager` значит роль live-команды, `A.position_manager` называет команду явно, `shared.news_reader` общую роль. Смена цепочек и профилей требует ключа подписи (создаётся сам при `./tb start`, или `./tb control init-key`). Тот же экран есть в дашборде (**Настройки -> Роли**, смена по токену):

![Настройки: роли](images/settings.png)

Смешивать можно и нужно: дешёвая модель для `news_reader`/`reviewer`/`entry_scout`, сильнее для `morning_analyst`/`position_manager`, второй провайдер как `--fallback`, чтобы 429/5xx одного вендора не ослепили мозг.

## 5. Проверка до того, как тратить больше копеек

```
./tb brain check                              # config/brain.json, роли, providers.json: структура (без сети)
./tb brain probe gem-flash                    # проверка без сети
./tb llm check --provider gemini --live       # ЖИВОЙ: GET /models (есть ли ваше имя модели) + один крошечный запрос. Доли цента.
./tb brain probe --all --live                 # ЖИВОЙ по профилям
./tb brain eval                               # регрессионный набор на ФЕЙКОВОМ LLM (без сети и денег)
```
`--live` использует ваш ключ и ваши деньги. `./tb brain eval ... --live --confirm` (только владелец, нужен терминал) гоняет полный набор на настоящей модели и единственный ставит профилю метку `live_ok`. Без `live_ok` профиль не должен вести live-позиции.

## 6. Включите мозг

Подписчик `api` в `config/subscriptions.json` выключен. Включается одной командой, без ручного JSON:

```
./tb brain enable        # пишет личный слой config/local/subscriptions.json (в .gitignore, сливается поверх config/); повторный запуск безопасен
```
Команда печатает, чего мозгу ещё не хватает (ключ провайдера, профиль/роли), и следующий шаг. Выключить обратно: `./tb brain disable`.

Дальше:
```
./tb start               # или ./tb restart, если платформа уже запущена
./tb brain status        # назначения, ревизия, расход сегодня, очередь
./tb logs brain
```
Если мозг остаётся выключенным, `./tb start` печатает одну строку: `мозг выключен: <причина> (как включить: ...)`. Причины в порядке проверки:

| причина в строке | что делать |
|---|---|
| `подписчик api выключен` | `./tb brain enable` (шаг 6) |
| `профиль/модель мозга не настроены` | в `config/brain.json` нет live-команды/ролей: `./tb brain reload --from-config` или `./tb brain role list` |
| `провайдер X неизвестен или с ошибкой` | `./tb llm check`; поправьте `config/local/providers.json` |
| `провайдер X выключен` | `./tb llm provider enable X` (сначала ключ) |
| `нет ключа провайдера X (файл x.env)` | шаг 3: файл ключа или переменная окружения |

Мозг стартует, как только в цепочках live-команды **один** профиль пригоден (провайдер включён, ключ есть). Роли, у которых цепочка ведёт на провайдера без ключа, переходят к следующему элементу; чтобы не было шума, назначьте всё на своего провайдера: `./tb brain use all <профиль>`.

После старта в дашборде **Мозг -> Решения** видна карточка «Мозг: работает» и лента решений (кто, что, почему, результат):

![Карточка мозга и лента решений](images/brain-decisions.png)

## 7. Свой OpenAI-совместимый адрес (прокси, vLLM, корпоративный шлюз, LiteLLM...)

Подойдёт любой сервер с `POST {base_url}/chat/completions`. Модели описываются в записи провайдера:

```
./tb llm provider add myproxy \
  --set 'adapter="openai_compat"' \
  --set 'base_url="https://llm.example.com/v1"' \
  --set 'key_file="myproxy.env"' --set 'key_env="MYPROXY_API_KEY"' \
  --set 'models={"main":{"name":"my-model","tools":true,"json_mode":true,"price":{"in":0.5,"out":1.5}}}'
./tb llm key set myproxy
./tb llm provider enable myproxy
./tb brain profile set my-main --provider myproxy --model main
./tb brain role chain position_manager --primary my-main
```
Заметки: `--set` принимает `поле=JSON` (строки в кавычках внутри). `adapter` принимает `openai_compat` (также пишется `openai-compatible`). Флаги возможностей важны: роли нужны tool calls (`tools:true`), мозг предпочитает `json_mode:true`; без них ждите пропусков `BAD_FORMAT`. Некоторым серверам нужен `max_completion_tokens` вместо `max_tokens` (поле модели `max_tokens_field`) или они отвергают `temperature` (`no_temperature`); все поля: `docs/spec_v2/SETTINGS_API.md`, раздел 1. `base_url` должен начинаться с `http://` или `https://`; всё, что не на вашей машине, держите на `https://`.

## 8. Локальная модель (Ollama, LM Studio)

Ключа и платы нет, но **качество ниже**: маленькие локальные модели часто не проходят строгую JSON-схему, в `./tb logs brain` это `BAD_FORMAT`. Начинайте с экспериментов на `news_reader`/`entry_scout`, не с сопровождения позиций.

```
ollama pull qwen2.5:14b          # в своём терминале; сервер Ollama должен работать
./tb llm model set local qwen --name qwen2.5:14b --tools --json-mode     # встроенный провайдер "local" -> http://127.0.0.1:11434/v1
./tb llm provider enable local
./tb brain profile set loc-qwen --provider local --model qwen
./tb brain role chain entry_scout --primary loc-qwen --fallback ds-flash
```
LM Studio: `./tb llm provider add lmstudio --preset lmstudio`, включите «Local Server» в приложении, имя модели возьмите из `GET http://127.0.0.1:1234/v1/models` и задайте `./tb llm model set lmstudio main --name ИМЯ`.

## Деньги и безопасность

- **Бюджеты** (жёсткие, USD): у провайдера в `limits` (`day_usd`, `hour_usd`, `rpm`, `concurrent`; по умолчанию $3 в день и $1 в час), общий в `config/brain.json` -> `budget` (`day_usd` 2.0, `job_usd` 0.05 на задание, `jobs_per_hour` 12, предупреждение на 80%), по ролям: `./tb brain role budget entry_scout --day-usd 0.3`. Когда бюджет исчерпан, задание пропускается или уходит на запасной профиль; мозг дальше потолка не тратит. Следить: `./tb llm usage --days 7 --by role` и дашборд (**Система**).
- **Дорогие модели**: «сильная» модель (`sonnet`, `pro`) на каждый сигнал сканера съест бюджет за часы. `entry_scout` держите на дешёвой модели, `jobs_per_hour` низким, задайте бюджеты ролей.
- **Сначала paper.** Агент `api` в `config/agents.json` только paper (`modes: ["paper"]`). Мозг не может открыть live-сделку, менять лимиты, плечо и ключи. Он предлагает; guard, уровни риска и KILL это код.
- **Live решает владелец** по `docs/CONTROL.md` и `docs/RUNBOOK.md`: гейты, токен, фразы подтверждения и `live_ok` после живого eval. Смена модели live-роли требует терминала и набранной фразы. Прибыль не доказана (`docs/RESEARCH.md`); модель получше не повод поднимать риск.
- Тексты третьих лиц (новости) читает только карантинный читатель; торговые роли их не видят.

## Типичные ошибки

| симптом | причина | что делать |
|---|---|---|
| `E_NO_KEY` на `provider enable` | ключ ещё не сохранён | `./tb llm key set ID` или файл ключа, потом включить |
| `E_PROVIDER_OFF` на `profile set` | провайдер выключен | сначала включить (нужен ключ) |
| `E_SWAP_NO_KEY` на `role chain` / `use` | нет ключа подписи | один раз `./tb start` (сам) или `./tb control init-key` |
| `E_SWAP_PROFILE ... неизвестен или отключён` | профиля нет / провайдер выключен | `./tb brain profile list`, создать профиль |
| `мозг выключен: ...` при `./tb start` | см. таблицу шага 6 | следовать подсказке «как включить» |
| `--live`: `auth` / 401 | неверный ключ или ключ от другого адреса | заново: `./tb llm key set ID` |
| `--live`: модели нет в списке / 404 | неверное `name` | взять из `GET /models`, `./tb llm model set ... --name` |
| `BAD_FORMAT` в журнале мозга, решения пропускаются | модель не держит JSON-схему или нет tools/JSON | модель сильнее, флаги `json_mode`/`tools`, ниже temperature, запасной профиль |
| `E_ELEVATE` / 401 в дашборде | для смены настроек нужен токен | ввести один раз (потерян: `./tb control token new`) |
| состояние бюджета `exhausted` | упёрлись в лимит дня/часа | `./tb llm usage`, осознанно поднять лимит в `config/local/providers.json` / `config/local/brain.json` или подождать |

Дальше: `docs/BRAIN.md` (рантайм, смена, запасные профили, файлы), `docs/ROLES.md` (промпты и схемы), `docs/spec_v2/07b_roles_providers.md` (дизайн), `docs/spec_v2/SETTINGS_API.md` (контракт API настроек).
