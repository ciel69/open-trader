# Архитектура

Пакет `src/tb/`, единая точка запуска `./tb <группа> <команда> [аргументы]` (`./tb help`); на Windows `tb.cmd` / `tb.ps1` (см. `WINDOWS.md`). Только stdlib, Python 3.9.

## Слои (зависимости только сверху вниз)
```
tb (sh) | tb.cmd | tb.ps1  ->  tb/launcher.py (ключ Bybit, прокси, UTF-8)  ->  tb/__main__.py (диспетчер групп)
core/          journal, session, portfolio, guard, pending (ждущие лимиты, state/pending_orders.json), tradectx (снимок рынка на входе, теги ошибок), tradepath (MAE/MFE от сканера, runtime/trade_paths.json) (профиль риска): решения «можно ли входить и каким размером»
venues/        base.Venue (open/close/positions/balance) и реализации: bybit.py (BybitVenue + CLI), bybit_limit.py (ждущие лимитные входы), bybit_stream.py (watch)
marketdata/    feed, wsclient, features, history, funding_history, watch, context, data, collect, patterns (чистые функции фигур/уровней), patterns_scan (`./tb patterns scan`), hotscan (сканер горячих монет по всему рынку Bybit, `./tb bybit read scan`; вывод runtime/hot.json и runtime/prices.json)
research/      research_*.py (в т.ч. research_patterns: `./tb research patterns`), compare_venues, backtest, paper_donchian (не торгуют)
dash/          локальный дашборд (`./tb dash`): model.py (чтение файлов и расчёты), server.py (http.server), static/ (HTML+CSS+JS по частям)
legacy_engine/ signals, risk, state, tick, sizing: старый автоматический движок, живой торговлей не используется
paths.py       ЕДИНСТВЕННОЕ место с путями: корень от расположения файла или TB_ROOT
launcher.py    платформенная логика входа: env-файл ключа Bybit, снятие прокси, UTF-8 консоль; fsutil.py: os.replace с повторами для Windows
```
`core` не знает про биржи; `venues` зовут `core.guard` и `core.journal`; `marketdata` не знает про торговлю.

## Где что лежит (корень проекта)
| Папка | Что |
|---|---|
| `config/` | risk_profile, config, feed_config, watch_config, context_config, macro_events, patterns_config (только настройки) |
| `state/` | trades.jsonl, sessions.jsonl, portfolio.json, pending_orders.json (ждущие лимиты), state.json/journal.jsonl (старый движок), `plans/` |
| `data/` | CSV свечей, тиков и funding, history_report_*.json |
| `runtime/` | выходы фида и наблюдателей (market.json, events.jsonl, ...), создаются демонами |
| `docs/` | TRADING, HARNESS, RESEARCH, SPEC*, PERMISSIONS, ARCHITECTURE |
| `tests/` | unittest; `_bootstrap.py` добавляет `src/` в путь |

Пути берутся только так: `from tb import paths; paths.state("trades.jsonl")`, `paths.config(...)`, `paths.data(...)`, `paths.runtime(...)`.
Каждая функция принимает `root=` (для `--root` и тестов); `TB_ROOT=/другая/папка` меняет корень для всего процесса.
Руками `os.path.join(HERE, ...)` не писать.

## Как добавить
- Новую биржу: `venues/<имя>.py`, класс-наследник `venues.base.Venue` (open/close/positions/balance, размер и плечо только из `core.guard.evaluate`,
  журнал через `core.journal` с `venue=<имя>`); ключ-профили в `./tb` (ветка группы, если нужен свой env-файл); группа в `SIMPLE` в `__main__.py`;
  тест с поддельным транспортом (как `tests/test_bybit_exec.py`); строка про права в `docs/PERMISSIONS.md`.
- Новую стратегию: исследование в `research/<имя>.py` (+ запись в `RESEARCH.md`), запуск через словарь `RESEARCH` в `__main__.py`.
  В торговлю стратегия попадает только как тезис агента, проверенный `guard`; свои лимиты и формулы риска не заводить.
- Новый тип сессии: только в `config/risk_profile.json:session_types` (max_minutes, budget_frac, лимит сделок). Код не менять;
  описать в `docs/HARNESS.md`.
- Новый источник данных: модуль в `marketdata/`, пути через `paths`, адреса в `config/*.json`.

## v2: план + исполнитель (в разработке, карта модулей)
Спецификация `docs/SPEC_V2.md` и `docs/spec_v2/01..06`, статус пакетов `docs/spec_v2/STATUS.md`. Каркас (WP0) создан, реализации пакетов подключаются позже; пока новые команды печатают «не реализовано» (код 2).
```
core/    plan_schema.py linkid.py (WP2)  filelock.py plan_store.py decisions.py exec_cfg.py (WP4)  bus.py (WP6)  expstats.py (WP7)
logic/   candles.py conds.py rules.py pyramid.py levels.py (WP3)  paper.py (WP7)        чистая логика, без ввода-вывода
executor/ ports.py (VenuePort, MarketFeed, PortError)  core gate actions reconcile protect daemon scanbridge mdsvc main (WP5)  live_port (WP1)  paper_port (WP7)
brain/   plan_cli brief events_cli common (WP6)  exp_cli (WP7)  api_daemon (WP11, отложено)   мозг: без venues/executor и без ключей
venues/  bybit_exec.py bybit_wsx.py (WP1)                                              операции и WS-фид исполнителя
config/  exec_limits.json (только сужающие лимиты)  brief_checklist.json
state/   plans/PL-*/{spec,runtime,revs}  plans/_inbox _outbox _archive  decisions.jsonl  bus/  briefs/  KILL
```
CLI-группы `plan`, `brief`, `events`, `exec`, `exp`, `brain` диспетчер вызывает как `main(argv) -> int` (`tb.__main__.V2`). Для `exec run --profile trade|testnet` launcher грузит ключ и снимает прокси
так же, как для `bybit`; `--profile paper` ключа не требует. Слои проверяет `tests/test_layering.py`: `brain/logic/core/dash` не импортируют `venues` и `executor`, `logic` чист (без `os`, сети, часов, `open`),
`BYBIT_API_SECRET` только в `launcher.py` и модулях `venues/bybit*.py`, `brain` не читает каталог ключей. Контракт порта биржи: `tests/test_port_contract.py` (`PortContract`).

## Тесты
```
./tb test                                   # все тесты
python3 -m unittest discover -s tests       # то же из корня проекта
python3 -m unittest discover -s tests -p "test_guard.py"
```
Тесты не должны трогать реальные `state/` и `runtime/`: подменяй `journal.PATH`, `portfolio.PATH`, `session.PATH` или передавай `root=` во временную папку.

## Launcher (кроссплатформенный вход)
Обёртки `tb`, `tb.cmd`, `tb.ps1` только выставляют `PYTHONPATH=src` и зовут `python -m tb.launcher "$@"`; `python -m tb ...` тоже идёт через launcher.
Для `bybit <read|trade|testnet> ...` launcher находит env-файл (`~/.config/traider-bot/` на macOS/Linux; `%USERPROFILE%\.config\traider-bot\`, затем `%APPDATA%\traider-bot\`
на Windows; `TB_KEYS_DIR` переопределяет каталог), кладёт пары KEY=VALUE в окружение ЭТОГО процесса, снимает `HTTP(S)_PROXY`/`ALL_PROXY` любого регистра
и отключает системные прокси urllib (на Windows/macOS они берутся не только из env). Содержимое файла не печатается. Остальные группы окружение не трогают.
Правила для нового кода: пути через `paths`/`os.path`, `open(..., encoding="utf-8")`, замена файла через `fsutil.replace`, без `fcntl`/`os.fork`/`sh`.

## Дашборд (`./tb dash [--port 8765] [--open] [--max-seconds N]`)
Только чтение. `http.server` на `127.0.0.1` (адрес не настраивается; чужой заголовок Host даёт 403, любой не-GET даёт 405), страница без CDN и сборки (`dash/static/`:
index.html, app.css, app.js), опрос `/api/state` раз в 3 с. Данные ТОЛЬКО из файлов проекта: `state/{trades,sessions}.jsonl`, `portfolio.json`, `learning.json`,
`runtime/{market,context,hot,health}.json`, `events.jsonl`, `config/risk_profile.json`. Биржу и ключи не трогает (тест `test_dash.py` проверяет исходники и ответы на секреты;
`model.scrub` вырезает поля `*key*/*secret*/*token*/*passw*`).
- Проценты сделки (`model.trade_pcts`, `live_calc`): `move_pct` = движение цены монеты с учётом стороны (long: цена/вход-1, short: 1-цена/вход), без плеча и комиссий; `pnl_pct_margin` = PnL после комиссий / маржа (у открытой: после комиссии входа, плюс `pnl_pct_margin_est` с оценкой комиссии выхода taker). В истории колонки «Цена %», «К марже %», «PnL USDT»; сводка `metrics`: `avg_move_pct`, `avg_margin_pct`, `best/worst_margin_pct`. Нет выхода или маржи -> `null`, на странице «—».
- API: `/api/state?mode=live|paper|testnet|all&venue=all|platform|bybit` (всё сразу), `/api/market`, `/api/hot`, `/api/health`, `/api/trades`, `/api/ping`. По умолчанию mode=live.
- Журнал разбирается устойчиво: повторный open при открытой записи заменяет её, open после close = новая запись, повторный close правит запись (как поправки по факту биржи),
  close без open и битые строки пропускаются и считаются в предупреждениях, старые записи без полей достраиваются.
- Нереализованный PnL = знак * (цена/вход - 1) * номинал - fee_open (если fee_open нет, оценка 0.09% номинала, помечается). Цена (`model.resolve_price`, для
  сделок, ждущих лимитов и идей): `market.json` (для bybit `prices_by_src.bybit`) -> `runtime/prices.json` (lastPrice ВСЕХ ~820 монет, пишет hotscan каждый скан без новых запросов,
  формат `{"ts_epoch", "prices": {МОНЕТА: цена}, "marks": {...}}`) -> `hot.json`; для площадок кроме bybit запасные источники не берутся. Берётся первая свежая
  (market <= 60 с, prices/hot <= 150 с), иначе самая свежая с пометкой «устарела»; в API `price_src`, `price_age_s`, `price_feed` (сканер жив или нет, подсказка `scan --watch`). Нет цены нигде: «нет цены», PnL не считается.
  Формат цен: значащие цифры по величине (`model.fmt_price` = `price()` в app.js, держать одинаковыми). R сейчас = PnL / risk_usdt. Ликвидация показана ОЦЕНКОЙ
  (`entry * (1 -/+ (1/плечо - mm))`, mm из `maintenance_margin_pct`), реальная цена ликвидации биржи в журнал не пишется.
- Фронт (`dash/static/`, без сборки, список файлов `server.SCRIPTS`): `theme.js` (тема до отрисовки), `core.js` (DOM, форматтеры, подсказки MAE/MFE/R/PF, «тикающие» возрасты, хэш URL + localStorage, `syncList` обновление по ключам, раскрываемые строки `rowGroup`), `charts.js` (кривая PnL, столбики, распределения), `v-overview|trades|ideas|market|system.js` (по вкладке), `app.js` (вкладки, опрос раз в 4 с, пауза при скрытой вкладке, индикатор состояния, настройки/тема).
  Вкладки: Обзор (капитал, PnL сегодня/всего, просадка, позиции, ждущие лимиты, кривая, 5 последних сделок), Сделки (сводка, графики, причины выхода и теги ошибок, история с раскрытием, разрезы), Идеи (Взято/Наблюдаем/Идеи/Пропущено/Авто-история), Рынок (горячие и отслеживаемые монеты), Система (узлы, предупреждения, сессия и лимиты, источники, события). Вкладка и фильтры: `#trades?res=loss&bd=tag`, плюс `tbdash.ui` в localStorage. Режим/площадка/тема в окне настроек. Новые агрегаты API: `summary` (PnL сегодня по локальным суткам и всего), `status` (единый индикатор: сканер, фид, контекст, сессия), `breakdown.err` (теги ошибок).
- Графики: SVG чистым JS; текст из данных вставляется через `textContent`. Тесты: `tests/test_dash.py`, `tests/test_launcher.py`.

## Шлюз инструментов и MCP (WP11-A, `docs/GATEWAY.md`)
`src/tb/gateway/` (каталог из 22 инструментов поверх brain/core, реестр агентов `config/agents.json` с хэшами токенов, аудит `state/gateway_audit.jsonl`, `gateway.call(agent, tool, args)`) и
`src/tb/mcp/server.py` (`./tb mcp serve --agent ID`, stdio JSON-RPC). Агент видит только каталог: ни ключей, ни файлов, ни shell, ни ордеров; команды планов идут в `_inbox` с `actor=brain:<id>`.
Слои: gateway и mcp не импортируют venues, executor, subprocess, socket, http (`test_layering.py`).

## Платформа: супервизор, диспетчер, сканер-сервис (WP11-B, `docs/RUNBOOK.md`)
`src/tb/platform/`: `supervisor.py` (`./tb up|down|status|logs`: дочерние процессы, pid-файлы, heartbeat, автоперезапуск с экспоненциальной паузой и потолком, блокировка второй копии, `service_down` в шину), `services.py` (конфиг
`config/platform.json`, команды и ЧИСТОЕ окружение сервисов: ключи только у исполнителя), `procs.py` (группы процессов POSIX / CREATE_NEW_PROCESS_GROUP Windows), `logs.py` (ротация), `status.py` (`build_status`: один словарь для
`./tb status`, дашборда `/api/platform`, MCP), `autostart.py` (печать launchd/Windows, не установка), `scansvc.py` (hotscan + ideawatch -> шина `scanner_signal`, белый список, дедупликация), `dispatcher.py` (шина -> подписчики по
`config/subscriptions.json`: очередь, задания API-мозгу, localhost-webhook; коалесцирование, классы приоритета, потолки, курсоры). Платформа не импортирует `tb.executor`/`tb.venues` в слоях brain/core/dash (кроме запуска клиента публичных данных в `scansvc`).

## Уровни риска (WP16, `docs/RISK_LEVELS.md`)
`core/risklevels.py` (чистая stdlib: загрузка `config/risk_levels.json`, `effective(prof, root)`, подпись HMAC файла `state/control/risk_level.json`, `set_level`, `auto_downgrade`, запросы политик) и CLI `control/risk_cmd.py` (`./tb risk ...`). Подключено в `core/guard.py` (`evaluate*`), `core/session.py` (бюджет), `core/exec_cfg.py` (`warnings()`); событие шины `risk_level_changed`. Шлюз, MCP, brain и llm уровень не меняют (тесты `NoAgentPath`).

## Память случаев (WP15a, `docs/spec_v2/08_case_memory.md`)
`src/tb/memory/`: `cases.py` (схема `tb.case/1`, валидатор, `state/cases.jsonl` append-only с хеш-цепочкой `chain.py`, update-строки того же `case_id`; `situation`/`decision` после записи не меняются), `features.py` (признаки из ctx/тегов/идей, неизвестное = null + `missing`, ретроспективные поля в `retro`), `similar.py` + `agg.py` (ЧИСТАЯ логика: сходство Gower с весами и шкалами из `config/memory.json`, жёсткие фильтры режим/версия правил/сторона/возраст/`as_of`, top-k, агрегат n, mean R ± se, win, база, предупреждения INSUFFICIENT n<5 / LOW_N n<30), `lessons.py` (`state/lessons.jsonl`: уроки-гипотезы из шаблонов cond+claim, статусы hyp/confirmed/rejected/expired/merged, затухание, срок годности, противоречия; в guard автоматически НЕ превращаются), `review.py` (`tb.casereview/1`, валидатор `known_at_decision` против самой ситуации), `privacy.py` (очистка текстов, `privacy_scan`), `migrate.py` (миграция и sync из trades/ideas/hot_outcomes/decisions, идемпотентно), `query.py` (сервисный слой чтения для 15b: `find_similar`, `stats`), `cli.py` (`./tb memory similar|stats|add|show|sync|migrate|verify|review-queue|review|lesson|backtest|compare`). Память ничего не разрешает и не запрещает; пакет не импортирует `tb.intel`, биржевой код, executor, gateway (`tests/test_memory_cli.py::TestLayers`). Дашборд («Память») и A/B: позже.
WP15b (память внутри мозга): `blocks.py` (блок `memory` досье <= 450 токенов с порядком усечения, видимость paper/testnet/live по `show_in_live`, полезная нагрузка инструментов шлюза `get_similar_cases`/`list_lessons`, метка `mem.on|off`), `tick.py` (периодический писатель: `migrate(final_only=True)` + пересчёт уроков; вызывается сервисом ctl раз в `tick.interval_s` и раннером перед разбором закрытия), `backtest.py` (`./tb memory backtest|compare`: ретро-проверка «совета похожих» walk-forward и старые/новые, сравнение периодов до/после с разрезом mem), `review.normalize_hint` (шов словарей роли и памяти). Подключения вне пакета: `gateway/catalog.py` (инструменты `get_similar_cases`, `list_lessons`, `add_case_review`; права `memory.read`/`memory.write`; плановый тег `mem.*`), `brain/brief.py` (`memory_block`), `brain/runner.py` (вход ревьюера из памяти, `case_id` по `position_closed`, запись через шлюз), `control/service.py` (тик писателя), `core/expstats.py` (разрез `mem`), `config/agents.json` (права), `config/roles/morning_analyst.json` (`tools_optional`).

## Фигуры и уровни (опционально)
`marketdata/patterns.py` (чистые функции) -> `patterns_scan.py` (`./tb patterns scan --sym ETH [--tf 1h|4h|1d]`, только чтение `data/` и `runtime/market.json`).
События для наблюдателя: `config/watch_config.json` ключ `"patterns_events": true` (по умолчанию false, ключа в файле нет; пороги `config/patterns_config.json`). События `pattern` помечены `unvalidated`: фигуры не прошли проверку (`RESEARCH.md`).
