# Плоскость управления (WP12): кнопки на дашборде безопасно

Спецификация: `docs/spec_v2/07_control_lifecycle_news_roles.md` п. 1. Код: `src/tb/control/`, `src/tb/dash/{ctlweb.py,server.py,static/v-control.js}`, настройки: `config/control.json`.

## Идея в двух строках
Дашборд «тупой»: проверяет вход, подписывает команду ключом и кладёт её файлом в `state/control/_inbox`. Исполняет её отдельный сервис `ctl` под супервизором.
Дашборд не импортирует биржу, не запускает процессы, не видит ключей Bybit и LLM. Единственное, что он делает сам: создаёт `state/KILL` (аварийная остановка работает при мёртвом `ctl`).

```
браузер --POST /api/ctl/*--> dash (127.0.0.1) --подписанный файл--> state/control/_inbox/K-*.json --> ctl --> _outbox/K-*.json, audit.jsonl, status.json
                                   \--KILL: state/KILL напрямую--> исполнитель видит KILL
```

## Первый запуск и вход
```
./tb start                 # платформа в фоне; ключ подписи и токен создаются САМИ при первом запуске, токен печатается один раз
открыть http://127.0.0.1:8765/#control   # paper: дашборд сразу работает, вводить ничего не нужно
```
Ручных `init-key` и `token new` больше нет (команды остались для замены токена). `./tb setup` необязателен: он нужен только для ключей провайдеров и бирж.
Токен нужен ТОЛЬКО для опасных действий (live, повышение риска, смена live-агента и модели, выключение платформы). При первом таком действии дашборд просит токен один раз и запоминает браузер.
Токен потерян или не показался (автозапуск ОС): `./tb control token new` (или `token show-once`): токен в открытом виде не хранится, поэтому «показать ещё раз» = выпустить новый; прежний и все запомненные браузеры перестают действовать.
Холодный старт платформы кнопкой невозможен (кнопка живёт внутри платформы): `./tb start`, ярлык `tb-start.command` (macOS) / `tb-start.cmd` (Windows) или автозапуск ОС (`./tb up --print-launchd`, `--print-windows`).
Кнопки выключения процессов нет (`control.allow_shutdown=false`): `./tb stop` из терминала. Кнопки «снять KILL» нет: только `./tb exec resume`.

## Модель угроз (что защищает и от чего)
1. Веб-атаки из браузера на localhost (чужая страница шлёт запрос на 127.0.0.1, DNS rebinding, подделка формы): закрывают проверки `Host`, `Origin`, `Sec-Fetch-Site`, `Content-Type`, привязка сессии cookie `SameSite=Strict` и CSRF-токен в заголовке. Они включены ВСЕГДА, в любом режиме входа, и отключить их настройкой нельзя.
2. Токен входа и ключ подписи против локального процесса того же пользователя слабы: оба файла читает тот же пользователь, поэтому «токен как барьер» для программы на вашем ПК условность. Их ценность: агенты и процессы без доступа к `~/.config`, случайные нажатия и скрипты, подписанный и аудируемый след каждой команды.
3. Реальные деньги защищают гейты live (H1-H8), фразы подтверждения, nonce, кулдауны и лимиты ctl и биржи, а не токен. Токен лишь добавляет «осознанный» вход на опасных действиях.

## Режимы входа (`config/control.json`, `auth`)
```
"auth": {"mode": "open_local", "remember_days": 30}
```
- `open_local` (по умолчанию): с 127.0.0.1 при выполненных проверках п. 1 дашборд сам выдаёт сессию «open» (cookie `tb_sess`, HttpOnly, SameSite=Strict, 60 минут с продлением активностью). Она разрешает безопасное: старт и стоп paper/testnet/observe, `mode.set` не в live, пауза, уведомления, обновление intel, переключение моделей и ролей, пока платформа не в live, KILL (в том числе flatten с подтверждением).
  Опасное (`day.start` и `mode.set` в live, любой `risk.set`, `brain.*` в live или для live-агента/команды, `carry.decide carry` в live, `platform.shutdown`, любое новое или неизвестное действие, любой ответ ctl `danger`, любая команда с nonce кроме KILL) отвечает `E_ELEVATE`: окно ввода токена появляется только тогда, не при открытии дашборда.
  После верного токена выдаётся cookie «запомнить» на `remember_days` суток (по умолчанию 30): подписана ключом (`rnd.срок.mac`, mac привязан к метке токена), состояния на диске нет, переживает перезапуск платформы. CSRF-токен выводится из неё тем же ключом. Новый токен (`token new`) отзывает все запомненные браузеры; «Выйти» стирает cookie в этом браузере.
  Запомнить браузер = подтверждение права на опасные действия, но фразы, nonce, гейты и подпись остаются: токен их не заменяет.
- `token`: как раньше. Сессии нет, пока не введён токен (любые действия, включая paper); сессия в памяти дашборда, час, перезапуск = перелогин; cookie «запомнить» не используется. Включить: `"auth": {"mode": "token"}`. Неизвестное значение трактуется как `token`.
Ограничения входа по токену те же: 5 неверных за 5 минут = блок на 10 минут. Что запоминается: только cookie в браузере и аудит входа `login`; токен нигде в открытом виде не хранится.

## Защита
- Только 127.0.0.1; для POST обязательны `Host` из {127.0.0.1, localhost, ::1} с портом сервера, `Origin` = `http://<тот же Host>`, `Sec-Fetch-Site: same-origin` (если есть), `Content-Type: application/json`, cookie сессии, заголовок `X-TB-CSRF`. Сессию «open» выдают только запросам с `Sec-Fetch-Site` same-origin/none или без заголовка. CORS-заголовков нет. POST только под `/api/ctl/`, на остальных путях 405.
- Токен (для опасного в open_local, для всего в token): 5 неверных за 5 минут = блок входа на 10 минут. Не более 30 POST в минуту на сессию.
- Подпись команды: `HMAC-SHA256(hmac_key, canonical_json(команда без mac))`; `ctl` проверяет подпись, срок (`ts` в пределах 120 с), повтор `id` (журнал 500 последних, переживает рестарт).
- Опасные действия (live-старт, повышение уровня риска, KILL с flatten, добавление в перенос в live, смена live-команды): `challenge` -> одноразовый nonce (подписан ключом, привязан к сессии и аргументам, действует 60 с, не раньше чем через 2 с) -> команда с nonce и набранными вручную фразами (вставка отключена, не быстрее 4 с).
- Аудит `state/control/audit.jsonl` (хеш-цепочка, проверка `./tb control audit --verify`): КАЖДАЯ команда, включая отказы на входе и неверные подписи; для live поле `warnings_accepted` с цифрами. Событие шины `control_cmd` (wake для live-действий).
- Ответы об ошибках только из справочника кодов (без путей и исключений). Значений ключа и токена нет нигде, кроме файла ключа владельца.
- Чат Claude Code не должен читать `~/.config/**` и писать в `state/control/**` (см. «Предложение для PERMISSIONS»). Без ключа команду не подписать, поэтому положить файл в `_inbox` мало.

## Команды `ctl` (поле `action`)
`day.start {mode, session, minutes, team?, accepted?}` (`session`: `day` с дашборда = торговый день, горизонт каждой сделки выбирает агент; `scalp|intraday|swing|position` = сессия одного горизонта, как из CLI), `day.stop {mode:"soft"}`, `kill.set | stop.emergency {flatten, reason}`, `mode.set {mode}` (только к более безопасному), `risk.set {level, accepted?}` (через `tb.core.risklevels.set_level`, WP16),
`carry.decide`, `report.ack`, `notify.test` (интерфейс WP14, пока E_UNAVAILABLE), `brain.use | brain.rollback | brain.reload` (WP11-C2, `tb.brain.swap`: уровни T0/T1/T2, фраза и предупреждения смены, `docs/BRAIN.md`), `intel.refresh` (кладёт `runtime/intel/refresh.req`),
`platform.shutdown` (выключено), `challenge {action, args}` (проверка условий и выдача nonce).

Команда (`state/control/_inbox/K-<UTC>-<6 hex>.json`): `{"schema":"tb.ctl/1","id","ts","src":"dash|cli","action","args","nonce","phrase_hash","sess","mac"}`.
`phrase_hash` = sha256 набранных фраз, склеенных переводом строки. Результат (`_outbox/<id>.json`, хранится 7 суток): `{"id","status":"running|done|rejected|failed|expired","code","msg","ts_done","data"}`.
Коды: `OK, E_MAC, E_EXPIRED, E_DUP, E_NONCE, E_PHRASE, E_STATE, E_GATE, E_WARN, E_WARN_CHANGED, E_LIMITS, E_COOLDOWN, E_ARGS, E_UNKNOWN, E_DISABLED, E_UNAVAILABLE, E_INTERNAL` (тексты: `tb.control.protocol.CODES`).
`status.json` (пишет `ctl` каждый цикл): состояние платформы (`stopped|starting|running|closing|halted`), режим, `checks` (H1-H8 и W1-W9 для live), KILL, команда мозга, уровень риска (+список уровней), `last`, `banner`. Старше 15 с = «ctl не отвечает», кнопки заблокированы, кроме KILL.

## Настройки: провайдеры LLM, профили, роли, биржи, ввод ключа (WP-X)
Действия ctl (аргументы и ответы: `docs/spec_v2/SETTINGS_API.md`): `llm.provider {op,id,...}` (add, update, remove, enable, disable, model_set, model_remove, check, key_clear), `brain.profile {op:set|delete,...}`,
`brain.role {op:chain|opts|opts_clear,...}`, `exchange.set {op,...}`, а также `brain.use {profile, all:true}` («профиль для всех ролей»). Все идут обычным конвейером ctl (подпись, срок, аудит каждой команды,
`challenge`/nonce/фразы там, где есть риск). Права: в режиме `open_local` эти действия требуют токена (сессия «full», как все «новые и неизвестные»). Live-роли по прежним гейтам `brain.use`;
живая биржа: фраза `ВКЛЮЧАЮ LIVE НА БИРЖЕ <ID>` и остановленная платформа. Новые коды: `E_EXISTS, E_NO_KEY, E_IN_USE, E_LIVE_IN_USE, E_PROVIDER_OFF, E_CHECK`.

### Ввод ключа LLM-провайдера из дашборда (write-only) и его риск
Маршрут `POST /api/ctl/llmkey {"provider","value"}` (`dash/keyweb.py`) принимает ТОЛЬКО ключи LLM-провайдеров. Секрет идёт в обход очереди ctl: тело запроса (loopback, Origin/Host/CSRF, сессия с токеном) ->
`tb.core.secretfile.write_key` -> файл ключа 0600 в каталоге ключей (`<провайдер>.env`, имя и переменная берутся из реестра провайдеров, не из запроса) -> значение сразу отбрасывается.
Его нет в `_inbox`/`_outbox`, в аудите (там `llm.key.set`, провайдер и результат), в логах сервера (пишется только строка запроса), в ответе (только `present` и маска `…1d1c`); читать ключ обратно не умеет ни один
маршрут. Имена файлов и переменных бирж (`bybit*`, `okx*`, `binance*`, `control*`, `agent*`, `token*`, `BYBIT_*`, `TB_*`...) отклоняются, а реестр провайдеров не принимает ссылку на них
(иначе торговый ключ мог бы уйти на чужой `base_url`). Торговые ключи бирж через дашборд не принимаются никогда: только `./tb setup` и ручные файлы.
Остаточные риски: (1) ключ проходит через память процесса дашборда и тело HTTP-запроса на 127.0.0.1 (локальный перехват процессами того же пользователя возможен; шифрования нет, как и у самого файла ключа);
(2) страница с XSS могла бы подсмотреть значение при вводе: защищает CSP `default-src 'self'`, нет внешних скриптов; (3) браузер может запомнить введённое: поле `type=password` с `autocomplete=new-password`;
(4) сессия с токеном может перезаписать ключ провайдера (но не прочитать). Терминальный путь: `./tb llm key set ПРОВАЙДЕР` (getpass, нужен TTY, значение не печатается).

## Допуск в live
Жёсткие гейты (обойти нельзя, любой провал = `E_GATE` с причинами и цифрами): H1 чистый KILL; H2 стоп на бирже у каждой позиции; H3 связь (исполнитель жив, hb < 10 с, приватный WS, REST, сверка < 60 с, нет hold private_ws/blind/bad_data);
H4 сверка портфеля (нет equity_mismatch, diffs = 0, equity в допуске); H5 суточный лимит убытка и drawdown_halt; H6 мозг доступен (heartbeat сервиса `brain`, бюджет LLM, агент включён); H7 конфигурации читаются и не ослабляют защиту
(`risk_profile`, `exec_limits`, `risk_levels`, подпись уровня риска, у агента `modes` с live, конечные лимиты, `max_size_mult<=1`, `allow_add=false`); H8 не более 3 запусков live в сутки UTC и пауза 10 минут после остановки live.
Пока исполнитель в другом профиле (идёт переключение на live), H2-H4 помечены `deferred` и ПЕРЕПРОВЕРЯЮТСЯ жёстко после переключения; провал = откат профиля и `E_GATE` (`phase: after_switch`).
Мягкие предупреждения (принимаются владельцем): W1 мало paper-истории (рекомендация 30 сделок и 7 дней); W2 нет `live_ok`/старый eval; W3 фактура устарела, источники недоступны, календарь не проверен; W4 недавняя смена модели; W5 уровень риска 4+; W6 деградация второстепенных сервисов;
W7 перенос через сутки / перенесённые позиции; W8 окно новостей; W9 мало сделок по типу сессии. Для запуска live: отметить КАЖДОЕ предупреждение и набрать `ТОРГУЮ LIVE <UTC-дата> УРОВЕНЬ <n>` и `я принимаю риск`. Набор предупреждений изменился между проверкой и запуском: `E_WARN_CHANGED`.
Принятые предупреждения пишутся в аудит, `state/days/<id>/day.json` (`live_waivers`), событие шины. Авто-понижение: пока идёт live, `ctl` раз в 5 с проверяет H1-H6; нарушение = `observe` (торговый флаг выключается, позиции не закрываются), баннер alarm, события `control_cmd` (wake) и `limit_hit` для H5.

## Интерфейсы для следующих пакетов
- WP14 (`tb.lifecycle.api`): `start_day, stop_day, emergency_summary, carry_decide, report_ack, notify_test (root, args, now) -> {"ok","code","msg","data"}`. Пока есть заглушка `tb.control.lifecycle` (упрощённый `day.json`, торговый флаг `state/control/trading.json`, `closing_audit` -> `{"stub": true}`).
  Шлюз отклоняет `submit_plan` (и ослабляющий `amend`) не-paper, если `trading.json.enabled=false`, флаг нечитаем или `day_id` не совпадает с `platform.json` (`gateway.registry.trading_allowed(root, mode, now, agent_id)`, WP11-C2, fail closed).
- WP11-C2 (`tb.brain.swap`, подключено): `status(root)`, `probe(root, now)`, `classify(root, args, now, action)` (опасность, фразы, мягкие предупреждения, жёсткие условия для challenge), `use|rollback|reload(root, args, now, by, phrase_ok)`.
  Args смены: `role` (`A.position_manager`|`shared.news_reader`|`position_manager`|`live`), `profile` (или `team` для `live`), `accepted` (id принятых предупреждений `W_SWAP_*`), для отката `to`, для reload `from_config`. Коды: `E_SWAP_*` (`protocol.CODES`), `E_WARN`, `E_WARN_CHANGED`.
- Расширения дашборда: `EXT_API` в `dash/server.py` (модуль `tb.<пакет>.dashapi`, функция `api(path, query, root, now)`, только чтение) и `EXT_SCRIPTS` (+ автообнаружение `static/x-*.js`); скрипт вкладки вызывает `registerTab(id, title, iconPath, render)`.

## Предложение для `docs/PERMISSIONS.md` (файл настроек не менялся)
`deny`: `Read(~/.config/**)`, `Write(state/control/**)`, `Bash(./tb control *)`, `Bash(./tb risk level set *)`, `Bash(./tb brain use *)`; `allow`: `./tb status`, `./tb control status`, `./tb control audit *`, `./tb risk level show`.
