# RUNBOOK: платформа v2 (WP11-B)

Платформа = супервизор + сервисы под ним. Запускается одной командой, живёт без чата и без агента Claude. Сервисы: `exec` (исполнитель), `scan` (сканер как сервис),
`dispatch` (диспетчер подписок), `ctl` (сервис управления, WP12: `docs/CONTROL.md`), `dash` (дашборд), `mcp` и `brain` (резерв: см. ниже). Код: `src/tb/platform/`, настройки: `config/platform.json`, `config/subscriptions.json`.

## Команды
Просто: `./tb start` (всё в фоне, профиль из `config/local/platform.json`, иначе paper; мозг входит в набор и стартует, если есть условия, иначе одна строка «мозг выключен: ...»), `./tb stop`, `./tb restart`, `./tb status`.
`start` не включает trade. Приостановленный (Ctrl+Z, состояние T) супервизор/сервис платформы `start|stop|restart|down` размораживают SIGCONT (только свои pid из `runtime/platform/*.pid` и `supervisor.lock`; KILL -9 не используется); не вышло: печатается ручная инструкция. Код: `src/tb/platform/quick.py`, `freeze.py`.
Расширенные параметры:
```
./tb up [--profile paper|testnet|trade] [--observe] [--services exec,scan,feed,context,intel,sync,dispatch,ctl,dash] [--detach] [--max-seconds N] [--dry-run]
./tb status [--json|--line]       сводка (код 0 платформа запущена, 1 нет); --line одна строка для статусной строки
./tb logs [platform|exec|scan|dispatch|dash] [-n 50] [-f]
./tb down [--timeout 45]          остановка всех сервисов (исполнитель последним)
./tb dispatch <check|once|queue ID [--ack N]|stats|log>
./tb up --print-launchd | --print-windows      напечатать файл автозапуска ОС (ничего не устанавливается)
```
По умолчанию профиль `paper` и сервисы `exec, scan, feed, context, intel, sync, dispatch, ctl, dash, brain` (brain отсеивается, если нет условий). `up` печатает профиль и список сервисов. Ctrl+C в окне `up` останавливает всё так же, как `down`.
Вторая копия на том же корне отказана блокировкой (`runtime/platform/supervisor.lock`, код 4). Корень можно задать `TB_ROOT` или `--root`.

## Запуск, статус, остановка
1. Рядом с терминалом: `./tb up` (окно занято; Ctrl+C останавливает) либо в фон: `./tb up --detach` (платформа живёт без терминала и без чата).
2. `./tb status`: цвет (зелёный, жёлтый, красный, выключена), таблица сервисов (состояние, pid, аптайм, heartbeat, перезапуски, последняя ошибка), исполнитель (жив, режим, позиции, hold, KILL),
   возраст шины и сканера, непрочитанное по очередям, ссылка на дашборд. То же в JSON: `./tb status --json`; в дашборде `/api/platform`; файл `runtime/platform/status.json` (блок сервисов).
3. `./tb down`. Если открыты позиции, будет напоминание: исполнитель остановлен, позиции держит биржевой стоп.

## Профили и безопасность
- `paper`: исполнитель на бумажном порту, ключи не нужны.
- `testnet`: ключ testnet грузит `tb.launcher` внутрь процесса исполнителя.
- `trade` (МАЙНЕТ, реальные деньги): только `./tb up --profile trade --i-understand-live` и только когда `state/KILL` отсутствует. Иначе отказ с кодом 2. Перед стартом печатается профиль и состав сервисов.
- Ключи Bybit и Anthropic есть ТОЛЬКО в процессе исполнителя (его launcher). Все остальные сервисы запускаются с окружением без `BYBIT_*`, `ANTHROPIC_*` и без прокси (тест `test_platform_supervisor`).
- `--observe`: исполнитель только наблюдает, ордеров не отправляет.

## Автоперезапуск, heartbeat, деградации
- Упавший сервис перезапускается с паузой `base_s * factor^(n-1)` (2, 4, 8, ... до 60 с). После `max_tries` (5) сбоев подряд сервис помечается `failed`, в шину пишется событие `service_down`
  (приоритет wake, не чаще раза в час на сервис), статус красный. Сдавшийся сервис пробуется заново раз в `retry_after_giveup_s` (15 мин); коды выхода 2 и 4 в первые секунды (ошибка запуска, второй экземпляр) не перезапускаются.
- Сервис проработал дольше `stable_s` (120 с): счётчик сбоев обнуляется.
- Heartbeat: `exec` = `runtime/exec_health.json` (60 с), `scan` и `dispatch` пишут `runtime/platform/<сервис>.hb.json`. Живой процесс с устаревшим heartbeat после льготного срока убивается и перезапускается.
- Исполнитель упал при открытых позициях: красный статус и напоминание, что позиции защищены биржевым стопом, но не ведутся. Что делать: `./tb status`, `./tb logs exec`, при необходимости `./tb bybit read positions`,
  закрыть вручную; после починки `./tb up`.

## Если что-то не так
| Симптом в `./tb status` | Причина | Действие |
|---|---|---|
| сервис `failed`, код 4 (exec) | исполнитель уже запущен отдельно (`exec run` в другом окне) | остановить его (Ctrl+C) и `./tb up` |
| сервис `failed`, код 2 | неверный запуск (`mcp` без параметров) | `./tb logs <сервис>`, убрать из `--services` |
| `backoff` | идёт перезапуск | подождать; смотреть `./tb logs <сервис>` |
| сканер не обновляет hot.json | нет доступа к REST Bybit (прокси, блокировка) | `./tb bybit read scan` из терминала; платформа запускает сканер без прокси |
| «платформа не запущена» при живых процессах | супервизор убит жёстко | `./tb down` (зачистит сирот по pid-файлам), затем `./tb up` |
| `status.json не обновляется` | супервизор завис | `./tb down`, `./tb up` |
| красный: мозг в «только защите» | бюджет LLM исчерпан или профили недоступны (ключ, сеть, 5xx) | `./tb brain status`, `./tb llm usage`, `./tb llm check --live`; позиции ведёт исполнитель, вручную при необходимости `./tb exec halt --flatten` |

## Ночной прогон (observe)
`./tb up --profile paper --observe --detach` (или testnet). Утром: `./tb status`, `./tb events stats`, `./tb dispatch stats`, `./tb logs platform -n 100`. Остановка: `./tb down`.
Во время прогона чат не нужен: события копятся в шине и очередях подписчиков, API-мозг (если включён) получает задания.

## Автозапуск ОС (ставит владелец)
Платформа ничего не устанавливает сама. `./tb up --profile paper --print-launchd > ~/Library/LaunchAgents/com.traider-bot.platform.plist`, затем `launchctl load ~/Library/LaunchAgents/com.traider-bot.platform.plist`
(снять: `launchctl unload ...`). Плист перезапускает платформу при НЕнулевом выходе; `./tb down` завершает её кодом 0 и launchd её не поднимает.
Windows: `./tb up --print-windows` печатает PowerShell-скрипт с `Register-ScheduledTask` (старт при входе, повтор при сбое); сохранить в файл и один раз выполнить. Снять: `Unregister-ScheduledTask -TaskName TraiderBotPlatform`.
Профиль `trade` в автозапуск вносить осознанно: нужны флаги `--i-understand-live` в аргументах, KILL должен быть чист.

## Чек-лист перед live
1. `./tb test` зелёный; `./tb exec verify` без ошибок.
2. Был прогон `paper` и `testnet` с `--observe` не меньше суток без `service_down`.
3. `config/risk_profile.json`, `config/exec_limits.json` не менялись без указания владельца; `state/KILL` отсутствует осознанно (`./tb exec resume`).
4. Ключ `bybit_mainnet_trade.env` на месте (проверяет сам исполнитель; содержимое не печатать).
5. `./tb up --profile trade --i-understand-live --dry-run` показывает ожидаемые сервисы; в `config/subscriptions.json` только нужные подписчики.
6. Дашборд открыт, `./tb status` зелёный, `./tb dispatch stats` без замечаний.
7. Известен порядок остановки: `./tb exec halt --flatten` (аварийно), затем `./tb down`.

## config/platform.json
Недостающие ключи берутся из `tb.platform.services.DEFAULTS`. Разделы:
- `profile`, `services.<профиль>`: список сервисов по умолчанию.
- `supervisor`: `poll_s`, `status_s`, `stop_grace_s`, `exec_stop_grace_s`, `stable_s`, `restart{base_s,factor,max_s,max_tries}`, `retry_after_giveup_s`, `fatal_codes`, `fatal_within_s`, `down_event_repeat_s`, `log{max_bytes,keep}`.
- `svc.<сервис>`: `hb` (`exec`|`file`|`none`), `hb_stale_s`, `grace_s`, для `dash` `port`, для `mcp`/`brain` `module` и `args`.
- `scan`: пороги прежние (hotscan): `interval, top, min_turnover, max_spread_bps, max_lot, range_pct, surge_min, cooldown, wake_score, max_per_hour, idea_zone_pct, outcome_min`; `env` (рынок, по умолчанию mainnet:
  публичные данные), `publish_info` (тихие события тоже в шину как info), `dedup_s` (окна дедупликации по виду события).
- `dispatch`: `poll_s`, `retry_*`, `max_attempts` (webhook), `api{jobs_per_hour, critical_per_hour, coalesce_s, settle_s, max_seqs}`, `queue_keep`, `classes` (тип события -> класс приоритета).

## config/subscriptions.json (реестр подписчиков)
Поставляемый файл держит `api` выключенным. Личные настройки владельца лежат в `config/local/<имя>.json` (каталог в `.gitignore`): глубокое слияние поверх `config/<имя>.json`, списки с `id` (подписчики) сливаются по id, прочие значения заменяются; битый локальный файл игнорируется с предупреждением в stderr. Подключено для `subscriptions`, `intel` (`sources.<id>.verified`, `reader.enabled`), `platform`, `providers`, `brain`. Включить мозг: `./tb brain enable` (пишет `config/local/subscriptions.json` = `{"subscribers": [{"id": "api", "enabled": true}]}`) (диспетчер подхватывает на лету, остальные сервисы при перезапуске). Код: `src/tb/core/localcfg.py`.
```json
{"subscribers": [
  {"id": "chat", "enabled": true, "delivery": "queue", "filter": {"pri": "wake"}, "rate": {"max_per_hour": 120}},
  {"id": "api", "enabled": false, "delivery": "api_agent", "filter": {"pri": "wake", "max_class": 3}, "rate": {"jobs_per_hour": 12, "coalesce_s": 60}},
  {"id": "hook", "enabled": false, "delivery": "webhook", "url": "http://127.0.0.1:9099/events", "filter": {"pri": "wake"}},
  {"id": "cmd", "enabled": false, "delivery": "exec_cmd"}]}
```
`id`: `a-z0-9_-` до 20 символов. `delivery`: `queue` | `api_agent` | `webhook` (только localhost/127.0.0.1/[::1], иначе подписчик отвергается) | `exec_cmd` (не поддерживается: запись «unsupported» в журнале, команда не запускается).
`filter`: `types` (список или `["*"]`), `exclude_types`, `kinds` (виды `scanner_signal`), `pri` (`wake` = только wake), `max_class` (0..4), `syms`. `rate`: `max_per_hour`, `min_gap_s` (события класса 0 не режутся).
`start_at`: `end` (по умолчанию: новый подписчик не получает историю) или `begin`. Включить можно только `"enabled": true`.
Классы приоритета: 0 связь, kill, лимиты, ошибки, `service_down`; 1 исполнение; 2 вехи позиции; 3 сигналы сканера; 4 идеи (`idea_*`).

## Диспетчер: как агент забирает события
Диспетчер читает шину своим курсором на каждого подписчика (`state/dispatch/subs/<id>.cursor.json`), доставка at-least-once и идемпотентна по `seq`. Журнал: `state/dispatch/deliveries.jsonl`
(`./tb dispatch log`): ok, dup, coalesced, rate, cap, retry, failed, unsupported.
- `queue`: файл `state/dispatch/queues/<id>.jsonl`, записи `{"q","seq","ts","type","pri","cls","plan"?,"sym"?,"d"}`; забрать `./tb dispatch queue <id> [--after Q] [--limit N] [--wake-only]`, подтвердить `--ack Q`.
  Библиотека для MCP (WP11-A): `from tb.platform.dispatcher import Queue; q = Queue(root, "chat"); q.read(); q.ack(n); q.stats()`.
  Счётчик непрочитанного: `runtime/platform/unread.json` (`{"chat":{"unread","wake_unread","oldest_wake_age_s",...}}`), он же в `./tb status` и в `build_status()["unread"]`.
- `api_agent` (для API-мозга, WP11-C): задания `state/brain/jobs/J-*.json` со `status:"new"`. Поля: `id, sub, status, prio (лучший класс), key (plan:ID | sym:СИМВОЛ | type:ТИП), plan, sym, ts_first, ts_last, not_before,
  count, seqs, types, kinds, dropped_before`. Серия событий по одному плану/символу в окне `coalesce_s` сворачивается в одно задание (`count` растёт, приоритет повышается, критичное снимает `not_before`).
  Потолок: `jobs_per_hour` обычных и `critical_per_hour` критичных (класс 0) в час; сверх потолка события в задания не идут (счётчик `dropped_before` следующего задания). Мозг берёт только задания с `not_before <= now`,
  меняет `status` `new -> taken -> done` (запись файла целиком; диспетчер коалесцирует только в `new`). Тексты третьих лиц в заданиях невозможны: только поля событий шины.
- Чат Claude Code диспетчер НЕ будит: это невозможно. События лежат в очереди `chat`, счётчик виден в `get_status`/`./tb status --line`; чат читает их при следующем обращении (`get_events`, `wait_events`).

## Сканер как сервис
`python -m tb.platform.scansvc run` (запускает супервизор) гоняет тот же `hotscan` (пороги из `config/platform.json`, раздел `scan`) и публикует в шину `scanner_signal` (kind: `range_5m`, `turnover_surge`, `hot_enter`,
`idea_near`, `idea_level`, `idea_invalidated`, `idea_tp`, `idea_expired`) с белым списком полей и дедупликацией. Мост исполнителя при этом выключается (`TB_SCAN_BRIDGE=0`), иначе были бы дубли.
Старый `./tb bybit read scan --watch` (Monitor) работает как прежде; одновременно со службой его запускать не нужно.

## MCP и мозг как сервисы
`mcp serve` (WP11-A) по умолчанию stdio и запускается клиентом агента, поэтому в наборе по умолчанию его нет. Если появится режим демона, задать в `svc.mcp.args` и добавить `mcp` в `--services`.
`brain run` (WP11-C2, `docs/BRAIN.md`) выключен по умолчанию: включается `--services exec,scan,dispatch,ctl,dash,brain` и подписчиком `api` (`enabled:true`) в `config/subscriptions.json`; нужен ключ провайдера (`./tb llm check --live`).
Heartbeat `brain.hb.json` окрашивает `./tb status`: красный = «только защита» (бюджет исчерпан или все профили недоступны: новых входов нет, позиции ведёт исполнитель), жёлтый = запасной профиль/ошибки конфигурации.
Команды: `./tb brain status|check|jobs|probe --all --live|use ЦЕЛЬ ПРОФИЛЬ|rollback|reload|verify`. Модуль не найден -> сервис «недоступен», не ошибка.

## Фактура и новости (intel, WP13a)
Сервис `intel` (`python -m tb.intel.service run`) входит в наборы по умолчанию (источники `verified:false` до `./tb intel verify`). Делает публичные GET (без ключей, без прокси), пишет `runtime/intel/{facts.json,blackouts.json,items.jsonl,cache/}`,
heartbeat `runtime/platform/intel.hb.json`. Источник не отвечает -> `degraded` в facts, сервис не падает. Все источники `verified:false`, пока владелец не запустил в Terminal.app `./tb intel verify [--write]`
(список и результаты: `docs/spec_v2/WP13_sources.md`). Команды: `./tb intel status|fetch|show|sources|calendar|verify`; `show items` (тексты заголовков) работает только в интерактивном терминале.
Даты FOMC/CPI/NFP в `config/intel.json` по памяти (v:false): подтверждать в `config/macro_events.json`. Событие `macro_blackout_soon` (за 60 и 15 мин) приходит в шину; brief получает блок `intel` без текста новостей.

## Файлы
`runtime/platform/` (`supervisor.lock`, `status.json`, `<сервис>.pid`, `<сервис>.hb.json`, `unread.json`, `stop.req`), `runtime/logs/<сервис>.log` (ротация 1 МБ x 3), `state/dispatch/`, `state/brain/jobs/`.
Коды выхода `up`: 0 штатно, 2 использование/отказ, 4 вторая копия.

## Управление с дашборда (WP12)
Кнопки «Старт», «Стоп», «Аварийная остановка», смена режима и уровня риска живут на вкладке «Управление» (вход по токену: `./tb control init-key`, `./tb control token new`). Подробности и защита: `docs/CONTROL.md`.
Холодный старт кнопкой невозможен: `./tb up --detach`, ярлык `tb-start.command` (macOS) / `tb-start.cmd` (Windows) или автозапуск ОС. Смена профиля исполнителя по команде `ctl` идёт через `runtime/platform/reconf.req` -> `reconf.ack`
(супервизор перезапускает только `exec`; профиль `trade` при активном `state/KILL` не поднимается). Снять KILL можно только `./tb exec resume`.

## День: старт, закрытие, перенос, итог (WP14)
Кнопки «Старт»/«Стоп» дня запускают шаги S1-S9 / C1-C8 (`docs/LIFECYCLE.md`). Состояние: вкладка «День» или `./tb day status` (`list`, `report [ID]`, `preflight [--mode M]`, `checklist` = сухой прогон того, что сделала бы остановка; всё только чтение).
Остановка может висеть в `closing`: ждёт ответ владельца о переносе (15 минут, без ответа позиция закрывается), ответ мозга (90 с) или подтверждение закрытия исполнителем (120 с). Если закрытие не подтверждено, день остаётся `closing` с красным баннером: проверить позиции на бирже,
затем кнопкой «Прочитал» (`report.ack`) с пометкой «разобран вручную». Итог дня (`state/days/<D-id>/report.md`) приходит баннером и событием `day_report`; уведомления ОС включает владелец сам: вкладка «Уведомления» дашборда или `./tb notify status|on|off|set|test` (приоритет у `state/control/notify.json`, без него действует `config/lifecycle.json: notify.os`; категории, тихие часы, антиспам: `docs/NOTIFICATIONS.md`).
Перенос через сутки: политика уровня риска (1 никогда, 2-3 подтверждает владелец, 4-5 решает агент), жёсткие условия K1-K8 проверяет код; без доступного мозга перенос невозможен (K8). После перезапуска платформы при идущем дне торговый флаг выключен до подтверждения: вкладка «День», кнопка «Возобновить день» (`day.resume`, только paper/testnet; `./tb control cmd day.resume --wait`).

## Интеграция: первый сквозной запуск (paper)
Пошагово: `docs/QUICKSTART.md`, подключение провайдера LLM: `docs/PROVIDERS.ru.md`; помощник настройки: `./tb setup [--check]`, ключи биржи: `./tb setup bybit --profile read|trade|testnet`; сквозной прогон без вашего счёта: `./tb smoke` (результат: `docs/spec_v2/SMOKE_RESULT.md`).
- Наборы по умолчанию (paper, testnet, trade): `exec, scan, feed, context, intel, sync, dispatch, ctl, dash`. `context` (`tb.platform.contextsvc`, обёртка над `marketdata/context.py`: деривативы, DVOL, индекс страха, макро-блэкаут; публичные источники, без ключей, сбой источника = degraded, не падение) пишет `runtime/context.json` и heartbeat каждые `config/platform.json: context.poll_s` (15 с); `sync` (`tb.platform.syncsvc`) раз в `sync.interval_min` (5) минут читает баланс ТОЛЬКО ключом `read` (launcher грузит его в процесс сервиса) и обновляет `state/portfolio.json`: при ошибке или без ключа портфель не трогается и помечается «устарело» (`runtime/platform/sync.state.json`); расхождение больше `sync.max_dev_frac` (10%) не применяется, уходит `reconcile_alert equity_mismatch`, принять значение: `./tb sync confirm`; высшая отметка растёт только после `sync.hwm_confirm_reads` (2) подтверждающих чтений подряд. P10: `levels.json` нужен, только когда есть планы или позиции. Ранее набор paper был `exec, scan, feed, dispatch, ctl, dash`: сервис `feed` (рыночный фид BTC/ETH/SOL, публичный WS Bybit) пишет `runtime/market.json`; без него в досье нет блока `market`, а план дня мозга отклоняется как `NO_DATA_PLANS`.
- Мозг отвечает на задания жизненного цикла: файлы `state/brain/jobs/L-*.json` (kind=lifecycle) берёт тот же демон `brain run` (`brain/lifejobs.py`), вызывает `Runner.ask_role` и пишет `L-*.result.json`; захват `L-*.claim` снимается при старте демона.
- Досье мозга получило блоки `limits`, `risk_level`, `paper_history` (только числа); `./tb exp stats --by agent|model` режет сделки по тегам шлюза; события `brain_swap` и `limit_hit brain_down` эмитятся.
- `./tb plan submit` (прямой путь чата) пишет в аудит шлюза запись `agent=cli` и добавляет предупреждение `W_CLI_BYPASS`; пока платформа ведёт день, не-paper план без торгового флага отклоняется `E_TRADING_*`.
- `./tb test` ограничивает каждый тест по времени (`TB_TEST_TIMEOUT`, 180 с), пропускает тесты с локальным сервером, если localhost-сокет недоступен (песочница), и сверяет реальные `state/`, `config/`, `runtime/` до и после прогона (`TB_TEST_NOGUARD=1` отключает сверку).
