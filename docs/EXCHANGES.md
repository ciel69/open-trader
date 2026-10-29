# Биржи: реестр, выбор и параллельный запуск (наработки)

Сейчас реально работает одна биржа: Bybit (USDT-перпетуалы). Остальные записи реестра (okx, binance) заготовки: `enabled:false`, `implemented:false`, включить нельзя. Реальных вторых
бирж в проекте нет; всё ниже проверено на ФЕЙКОВОЙ бирже `fake2` (`FakePort`), см. `tests/test_exchanges.py`.

## Подключение ключей Bybit

Ключ биржи нужен только для `testnet` и `live`; paper работает без него (по публичным ценам). Проверена и реализована ТОЛЬКО Bybit (paper, testnet, несколько небольших live-сделок владельца).

1. На Bybit создайте API-ключ. Право вывода (Withdraw) не давать никогда. `read`: только чтение. `trade`: торговля контрактами, по возможности привязка к вашему IP. `testnet`: отдельный аккаунт на testnet.bybit.com.
2. Сохраните ключ скриптом (запускает владелец в терминале; ввод скрыт, значения нигде не печатаются, файл пишется вне репозитория, права 0600 там, где ОС это поддерживает):

```
./tb setup bybit --profile read
./tb setup bybit --profile trade
./tb setup bybit --profile testnet
```

   Windows: `.\tb.cmd setup bybit --profile read`. Тот же шаг есть в общем помощнике `./tb setup` (шаг 5). Существующий файл не перезаписывается без слова `да`.
3. Файлы: `~/.config/traider-bot/bybit_mainnet.env` (read), `bybit_mainnet_trade.env` (trade), `bybit_testnet.env` (testnet); на Windows `%USERPROFILE%\.config\traider-bot\`; другой каталог: `TB_KEYS_DIR`. Формат: строки `BYBIT_ENV`, `BYBIT_API_KEY`, `BYBIT_API_SECRET` (скрипт создаёт их сам; руками тоже можно). Ключ читает только `launcher` и исполнитель; мозг и дашборд ключей не видят, через дашборд ключи бирж не вводятся.
4. Проверка: `./tb setup --check` (есть ли файл, без чтения значений), затем `./tb bybit read balance` или `./tb bybit testnet balance`.
5. Порядок включения: paper, затем testnet (`./tb up --profile testnet`), затем live только после paper-истории и с гейтами владельца (дашборд: «Начать торговлю» -> режим «Реальные деньги»).

Другие биржи (okx, binance) в реестре это заготовки без адаптера, включить их нельзя; как добавить новую: раздел «Как добавить биржу» ниже. Для новой биржи понадобится свой скрипт ключей по образцу `./tb setup bybit`.

## Реестр

* `config/exchanges.json` (+ личный слой `config/local/exchanges.json`): `active` (одна, по умолчанию bybit), `allowed` (для каких бирж запускать исполнители), `exchanges.<id>`:
  `name`, `adapter`, `enabled`, `mode` (`paper|testnet|live`: режим исполнителя биржи по умолчанию), `keys` (имена файлов ключей по профилям `read|trade|testnet`: только справка),
  `limits.max_open_positions` (лимит позиций на этой бирже, `null` = только общий), `symbols.allow|deny`, `note`.
* Читает и проверяет `src/tb/core/exchcfg.py` (без биржевого кода: его видят дашборд, ctl и guard). Поле `implemented` вычисляется: адаптер есть в `tb.venues.registry`.
  Запись, включённая вручную без адаптера, сама отключается (ошибка в `exchcfg.load()["errors"]`).
* Изменения: `./tb`-командой пока нет, дашборд: ctl-действие `exchange.set` (activate, enable, disable, mode, allowed, limits; только при остановленной платформе; live: фраза
  `ВКЛЮЧАЮ LIVE НА БИРЖЕ <ID>`). Контракт: `docs/spec_v2/SETTINGS_API.md`. Руками: правьте `config/local/exchanges.json`.
* Фабрика `src/tb/venues/registry.py`: `make_bundle(exchange_id, profile, root, selftest)` -> `Bundle(port, feed, saver, client)`; bybit = `LivePort` (trade/testnet) и `PaperPort` на публичных
  потоках (paper). Нереализованная биржа даёт `ExchangeNotImplemented` с понятным текстом. `make_client(exchange_id, env)` для сервисов чтения.

## Параллельный запуск: один исполнитель на биржу

* `./tb exec run --profile paper|testnet|trade [--exchange ID]` (по умолчанию bybit). Исполнитель биржи имеет свои файлы: у основной биржи (bybit) имена прежние, у остальных суффикс `@<id>`:
  `runtime/exec@ID.lock`, `exec_health@ID.json`, `exec_snapshot@ID.json`, `exec_state@ID.json`, `exec_reconcile@ID.req`, `levels@ID.json`, `instruments@ID.json`, `state/paper_port@ID.json`
  (`paths.venue_suffix`). Второй экземпляр той же биржи отказан (код 4), разные биржи друг другу не мешают.
* WAL: ждущие действия лежат в `runtime.json` КАЖДОГО плана; план ведёт только исполнитель своей биржи (`spec.venue`, нет поля = bybit), восстановление после рестарта идёт по его планам.
* Команды в общем `state/plans/_inbox`: исполнитель берёт только команды своей биржи (`payload.venue` при создании или биржа существующего плана), чужие оставляет. Если для биржи нет
  работающего исполнителя, её команды ждут в inbox (не теряются). Для бирж кроме bybit в плане нужно указывать `venue` явно (автоподстановки активной биржи в план пока нет: нет `venue` = bybit).
* `orderLinkId`: у bybit прежний `tbx-<план>-<действие>`, у остальных `tbx-<биржа>-<план>-<действие>` (`linkid.make(plan_n, action_n, venue)`, `linkid.venue_of`). Номера планов общие, поэтому
  коллизий нет и без префикса, префикс даёт читаемость и защиту от случайного перекрёстного совпадения.
* Журнал сделок: поле `venue` и тег биржи (`tags[0]`) на каждой сделке (`venue_id` исполнителя). `portfolio.json`: `venues.<id>` обновляет исполнитель своей биржи; эквити всего счёта
  = сумма по биржам (`portfolio.total`), высшая отметка тоже по сумме.
* Платформа: сервисы `exec` (bybit) и `exec@<id>` (`services.exec_name/exec_venue/exec_services`, `parse_services`, `stop_order`: исполнители останавливаются последними).
  `config/platform.json`: `"exec_per_exchange": true` заменяет `exec` на исполнителей всех разрешённых и реализованных бирж; по умолчанию `false`, как раньше один `exec`.
  Проверка heartbeat для `exec@<id>` в супервизоре пока не включена (`hb: none`): их жизнь видна в `GET /api/settings/exchanges` (`exec.alive`). НЕ проверено: запуск двух исполнителей
  супервизором на живой платформе (только функции имён и команд и два Executor в одном процессе на фейках).
* launcher: ключ Bybit никогда не загружается исполнителю другой биржи (`--exchange` не bybit при `trade|testnet` -> отказ); ключи новой биржи launcher получит вместе с её адаптером.

## Общие лимиты и риск (guard) по ВСЕМ биржам

`guard.day_state` считает по журналу live-сделок ВСЕХ бирж: суммарный открытый риск, число слотов (`max_open_positions`), коррелированный риск по группам, дневной убыток и серию стопов.
Следствия: капитал и дневной лимит убытка от суммарной эквити; одна монета на двух биржах считается дублем (`position_exists_on_symbol`, усреднение запрещено); BTC/ETH/SOL в одну сторону
на разных биржах считаются одной ставкой. Дополнительно лимит одной биржи `limits.max_open_positions` (`max_open_positions_on_exchange`). Разрез по биржам: `day_state()["by_venue"]`.
Paper-сделки по-прежнему идут облегчённой проверкой (как раньше).

Приоритеты и конфликты: исполнители независимы и не знают друг о друге; общий потолок обеспечивает guard при создании плана (предпросмотр на submit и перед входом). Если две биржи одновременно
проходят проверку на последний слот, гонка двух процессов возможна: общей блокировки на момент входа нет (риск описан в STATUS). KILL (`state/KILL`) общий: останавливает все исполнители.

## Как добавить биржу (чек-лист)

1. Записать в `config/exchanges.json` (или `config/local/exchanges.json`): `id` (строчные латиница и цифры, до 12 символов), `adapter`, `enabled:false`, `keys`, `limits`.
2. Реализовать `VenuePort` (`src/tb/executor/ports.py`): чтение (`account`, `positions`, `open_orders`, `order_by_link`, `executions`, `closed_pnl`, `instrument`, `quote`, `klines`),
   запись (`set_leverage`, `place_entry` со стопом обязательно, `place_reduce_only`, `amend_order`, `cancel_order`, `cancel_all`, `set_stop` только строже, `close_market`) и `MarketFeed`
   (`subscribe`, `unsubscribe`, события `on_ticker|on_kline|on_private|on_state`). Инварианты порта: вход без стопа запрещён, стоп только строже, `link_id` идемпотентен.
3. Фабрика `factory(profile, root, selftest, venue_id) -> registry.Bundle(port, feed, saver, client)` и `registry.register("<adapter>", factory)` (в `src/tb/venues/registry.py` или в модуле, импортируемом
   при его загрузке). Только после этого `exchcfg` считает адаптер реализованным.
4. Контрактный тест порта: в тестах `class TestMyPortContract(PortContract, unittest.TestCase): def make_port(self): ...` (`tests/test_port_contract.py`; порт с символом SOL по 100, bid 99.95/ask 100.05,
   без позиций). Пример подключения на фабрике: `TestPortContractFake2` в `tests/test_exchanges.py`. Плюс тесты клиента на фейковом транспорте (как `tests/test_bybit_exec.py`, `test_live_port.py`).
5. Ключи: файлы профилей и загрузка в окружение процесса только в `src/tb/launcher.py` (по образцу bybit); имена `ИМЯ_*` ключей биржи вносятся в список запретных для ключей LLM (`core/secretfile.py`), если префикс новый.
   Через дашборд ключи бирж не вводятся никогда. Литералы секретов вне `launcher` и модулей биржи запрещены тестом `test_layering`.
6. Комиссии/проскальзывание: `config/risk_profile.json -> costs.<id>` (иначе умолчания), символы и размер лота из `instrument()` порта.
7. Включить: `exchange.set enable` (дашборд) или `config/local/exchanges.json`, затем `./tb exec run --profile paper --exchange <id>`; после paper-прогона и контракта: testnet, потом live (live: фраза и гейты).
8. Запись в `docs/spec_v2/STATUS.md`: что проверено на живом ключе, что нет.

## Не сделано

Реальные okx/binance; единый планировщик между биржами (кому достаётся последний слот); сводный статус всех исполнителей в `./tb status`; heartbeat-проверка `exec@<id>` супервизором;
перенос позиции между биржами; выбор биржи на этапе задания сессии (`./tb session`).
