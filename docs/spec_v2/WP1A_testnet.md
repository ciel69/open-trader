# WP1A: результаты проверки на testnet (чек-лист T1-T10)

Дата: 2026-10-28. Площадка: Bybit **testnet** (UNIFIED, односторонний режим positionIdx=0, USDT-перпы), символ SOLUSDT (tick 0.01, qtyStep 0.1, minQty 0.1, minNotional 5). Код: `venues/bybit_exec.py`, `venues/bybit_wsx.py`, `executor/live_port.py`. Прогон: временные скрипты в scratchpad (ключ грузил только `launcher.prepare_bybit(["bybit","testnet"])`, прокси сняты, TB_ROOT во временной папке, состояние проекта не тронуто). Mainnet не вызывался.
Формат пунктов: **подтверждено** / **отличается (что)**. Тестовые позиции 0.5 SOL закрыты, висящих ордеров не осталось.

## Критичные находки (поправлены в коде до интеграции WP5)
1. **`POST /v5/order/cancel-all` без `orderFilter` (и с `orderFilter=StopOrder`) снимает СТОП/ТЕЙК УРОВНЯ ПОЗИЦИИ.** Позиция остаётся без защиты (`stopLoss` стал `0`). С `orderFilter=Order` снимаются только обычные ордера, стоп жив (проверено дважды, в том числе после частичного закрытия). Исправлено: `bybit_exec.cancel_all` и прежний `bybit.cancel_open_orders` (его вызывают `cmd_close`, `open_maker`, `open_limit`, `sync`; в `cmd_close` он шёл ДО закрытия позиции, то есть открывал окно без стопа) теперь передают `orderFilter=Order`. Комментарий в прежнем коде «стоп позиции не затрагивается» был неверен.
2. **Биржа сама разрешает ослабить стоп**: сырой `trading-stop` с более слабым значением прошёл (`retCode 0`, стоп 100.46 -> 98.93). Значит монотонность держится только на локальной проверке (`tighten_stop`, затем Gate); обходить её нельзя.
3. **`order/realtime` возвращает стоп/тейк уровня позиции как ордера** (`orderStatus=Untriggered`, `reduceOnly=true`, `orderType=Market`, `stopOrderType=StopLoss|TakeProfit`, пустой `orderLinkId`, `qty` = размер позиции). `bybit_exec.open_orders` по умолчанию их скрывает (`include_stops=True` вернёт); `cancel_all(reduce_only_only=True)` их не трогает.

## T1. `trading-stop` (tpslMode=Full)
- Подтянуть: **подтверждено**. `POST /v5/position/trading-stop {tpslMode:Full, positionIdx:0, stopLoss, slTriggerBy}` -> `retCode 0`, `{}`; стоп виден в `GET /v5/position/list` сразу (то же чтение, без задержки).
- Равное значение: **подтверждено** `retCode 34040 "not modified"`; порт в этом случае возвращает `unchanged=True` (и вообще не вызывает запись, если равенство видно локально после округления).
- Ослабить: блокируется локально (`STOP_NOT_TIGHTER`, записи нет). Биржа ослабление разрешает (см. находку 2).
- Снять (`<= 0`): локально `STOP_REMOVE_FORBIDDEN`.
- Не с той стороны от цены: локально `STOP_ON_WRONG_SIDE`; биржа для long со стопом выше last: `retCode 10001 "StopLoss:... set for Buy position should lower than base_price:...??LastPrice"` (запись не прошла, стоп прежний).
- Позиции нет: `retCode 10001 "can not set tp/sl/ts for zero position"` -> порт: `position_gone`.
- `takeProfit` отдельным вызовом: ставится, стоп не меняется (**подтверждено**).
- `slTriggerBy=MarkPrice`: принимается. Поле `slTriggerBy` в `position/list` НЕ возвращается (пустое), поэтому тип триггера по чтению позиции не проверить.

## T2. Рыночный вход со встроенным стопом
**Подтверждено.** `order/create` Market + `stopLoss` + `tpslMode=Full` + `slTriggerBy=LastPrice` + `orderLinkId`: первый же `position/list` после ответа показывает позицию с `stopLoss`. Исполнение одним `execution` (taker, `feeRate 0.001`, комиссия 0.1% номинала, `isMaker=false`, `stopOrderType=UNKNOWN`). Задержка вызова create ~0.9 с. Частичных исполнений на 0.5 SOL не было (на testnet ликвидности хватает); логика усреднения по нескольким `execution` лежит на WP5. `order/history` по `orderLinkId` для рыночного ордера: `orderStatus=Filled`, `stopLoss` продублирован в ордере, `price` у Market приходит завышенным служебным значением (201.94), порт его возвращает как `price` (не использовать).
Лимитный post-only вход, пересекающий стакан: `order/create` отвечает `retCode 0`, ордер сразу получает статус `Cancelled` (post-only). Вызывающий обязан читать статус по `link_id` или из WS `order`; `rejectReason`/`cancelType` теперь возвращаются полями `reject_reason`/`cancel_type`.
Вход со стопом по другую сторону цены (long, стоп выше цены): `10001 "StopLoss:... should lower than base_price"`, ордера нет.
Дубль `orderLinkId`: `110072 "OrderLinkedID is duplicate"` -> `rejected`.
Плечо: `set-leverage` повторно с тем же значением: `110043` -> успех (`unchanged`).

## T3. Reduce-only лимитный тейк на часть + стоп на всей позиции
**Подтверждено**: оба живут (лимит `reduceOnly` 0.2 из 0.5 в `open_orders`, стоп позиции в `position/list`). Уточнения:
- reduce-only больше свободного остатка (5.0 при 0.3 свободных): биржа **принимает и сама обрезает** `qty` до остатка, не занятого другими reduce-only (0.3). WP5: размеры лесенки сверять по `open_orders`, а не по ответу create.
- Закрытие позиции (рыночное reduce-only) **снимает** оставшийся reduce-only лимит: статус `Cancelled`, `open_orders` пуст. Закрытие позиции стоп-ордером: см. T7 (проверка на реальном срабатывании).
- Частичное закрытие рынком оставляет стоп и уменьшает `qty` стоп-ордера (0.5 -> 0.3); лимитный тейк на оставшуюся часть остаётся.
- reduce-only при отсутствии позиции: `110017 "current position is zero, cannot fix reduce-only order qty"` -> `position_gone`.

## T4. `amend`
**Подтверждено.** `POST /v5/order/amend {orderLinkId, price}` и `{qty}` по отдельности работают для reduce-only лимита (цена 105.07 -> 105.57; qty 0.2 -> 0.3), `updatedTime` меняется. Те же значения: `10001 "order not modified"` -> порт возвращает `unchanged=True`. Несуществующий: `110001 "order not exists or too late to replace"` -> `rejected`.
Отмена: `POST /v5/order/cancel` по `orderLinkId` работает; повторная отмена или несуществующий: `110001 "order not exists or too late to cancel"` -> порт возвращает `gone=True`.

## T8. `order/history` по `orderLinkId`
**Подтверждено.** `GET /v5/order/realtime?orderLinkId=` находит и исполненные, и отменённые ордера в течение первых секунд-минут; после этого и всегда — `GET /v5/order/history?orderLinkId=`. Для надёжности `order_by_link` сначала realtime, затем history (как `bybit_limit.lookup`). Поля: `orderStatus` (New/Filled/Cancelled/PartiallyFilled...), `cumExecQty`, `avgPrice`, `cumExecFee`, `reduceOnly`, `stopOrderType`, `rejectReason`, `cancelType`, `createdTime`, `updatedTime`.

## T9. `execution/list`
**Подтверждено.** Пагинация: `limit` + `cursor`/`nextPageCursor` (проверено `limit=2`: 3 страницы, 3 уникальных записи, последний курсор пустой). Новые первыми (порт сортирует по возрастанию времени). Окно `startTime..endTime` не более 7 суток: иначе `10001 "The time range between startTime and endTime cannot exceed 7 days."`; только `startTime` даёт окно +7 суток. Порт режет диапазон на окна по 7 суток (проверено на 20 сутках: 18 записей без ошибок). Комиссия: поле `execFee` в USDT (положительное = списание), `feeRate` 0.001 (taker). `execPnl` в REST приходит `null` на открывающих исполнениях и числом на закрывающих; `closedSize` > 0 только у закрывающих; `stopOrderType` при обычных ордерах `UNKNOWN` (порт нормализует в пустую строку). Funding-записи (`execType=Funding`) порт пропускает.
`position/closed-pnl`: по строке на каждое закрытие (частичное тоже), `side` = сторона закрывающего ордера (порт переводит в сторону позиции), `closedSize`, `avgEntryPrice`, `avgExitPrice`, `closedPnl`, `openFee`, `closeFee`, `updatedTime`.

## T5. WS kline
**Подтверждено.** Темы `kline.<tf>.SOLUSDT` (`kline.1`, `kline.5`), подписка `{"op":"subscribe","args":[...]}` -> `{"success":true,"op":"subscribe"}`. Сообщение: `data:[{start,end,interval,open,close,high,low,volume,turnover,confirm,timestamp}]`, `type` у ВСЕХ пушей `snapshot` (не delta), `start` в мс. Формирующаяся свеча приходит с `confirm:false` примерно **раз в 5 с** (медиана 5.0, максимум 7-9 с; на testnet, на mainnet может быть чаще); закрытая приходит одним сообщением `confirm:true` на границе минуты/5 минут. После подписки сразу приходит snapshot текущей формирующейся свечи.
- Обрыв и возврат: **подтверждено**. `WSClient` переподключается и фид заново подписывается на все темы (`on_connect`); первое после возврата сообщение это snapshot формирующейся свечи. Закрытые свечи, пропущенные за время обрыва, **не досылаются**: после обрыва на ~120 с первая подтверждённая свеча имела `start` на шаг дальше ожидаемого, фид выдал `on_gap(SOL, 1, from_ms, to_ms)` с границами пропуска (проверено на реальных данных). Дозапрос пропусков делает исполнитель через REST `klines`.
- Повтор `confirm:true` для той же свечи фид отбрасывает (по `start`); на testnet за прогоны повторов не было.
- Динамическая подписка/отписка на лету (`subscribe(["ETH"],[1])`, `unsubscribe`) работает; `op:unsubscribe` отвечает `success`.

## T6. WS tickers
**Подтверждено.** Тема `tickers.SOLUSDT`: первое сообщение `type:snapshot` с полным набором (`lastPrice, bid1Price, ask1Price, markPrice, indexPrice, fundingRate, nextFundingTime, openInterest...`), дальше `type:delta` с ТОЛЬКО изменившимися полями (например `{"symbol":"SOLUSDT","indexPrice":"102.083"}` без `lastPrice`/`bid1Price`): слияние в фиде обязательно, оно проверено в бою (за ~170 с 165 событий, ни одного нулевого `bid/ask/mark`). Новый snapshot приходит после каждой (пере)подписки и заменяет состояние. Периодичность на testnet: медиана 0.5 с, p90 1.7-2.1 с, максимум 6.5 с между сообщениями при живой связи (спецификация предполагала ~100 мс: для условий по цене закладывать обновления реже; `public_price_stale_s=10` с запасом достаточно). Прикладной ping `{"op":"ping"}` -> `{"success":true,"ret_msg":"pong","op":"ping"}` (фид его игнорирует как служебный).

## T7. WS execution (и order/position/wallet)
**Подтверждено** (приватный сокет: `op:auth` -> `success`, затем `subscribe` на `order, execution, position, wallet`; подпись как в `bybit_stream`). Один вызов создания рыночного ордера даёт в течение ~20 мс: `execution`, `order` (Filled; плюс `order` самого стопа позиции: `Untriggered`, `stopOrderType=StopLoss`, `createType=CreateByStopLoss`, пустой `orderLinkId`), `position`, `wallet`.
- `orderLinkId` в `execution`/`order` сохраняется (`tbx-...`). **Исключение: исполнение, вызванное стопом позиции, приходит с ПУСТЫМ `orderLinkId`** и новым `orderId`: распознавать по `stopOrderType`.
- `stopOrderType`: у обычных `UNKNOWN` (в `order` пустая строка); у сработавшего стопа `StopLoss` (подтверждено на реальном срабатывании: `closedSize=0.5`, `execPnl` в WS заполнен числом), `isMaker=false`. Фид нормализует `UNKNOWN` в пустую строку.
- `isMaker`: **подтверждено** `true` для post-only reduce-only лимита, заполненного пассивно (комиссия 0.036% против taker 0.1% номинала).
- Закрытие стопом **снимает** оставшиеся reduce-only лимиты: `order` со `status=Cancelled`, `cancelType=CancelByReduceOnly`, `rejectReason=EC_PerCancelRequest`; в `open_orders` пусто (проверено на реальном срабатывании стопа, T3).
- Отмена пользователем: `Cancelled`, `cancelType=CancelByUser`. Порядок событий WS относительно ответа REST не гарантирован: исполнитель обязан сверять по `link_id`/`exec_id`.
- Принудительный обрыв приватного сокета и возврат: ~3 с (1 с отступа + connect + auth + subscribe), фид уведомил `on_state("private", False, ...)`, затем `True`; после возврата события идут (дубликаты по `execId` отброшены, окно 5000).
- Схема `on_state`: падение сообщается сразу (`down_s=0`), потом раз в секунду с растущим `down_s`, при возврате `up=True` и общее `down_s`.

## T10. Лимиты частоты
- Лимиты в заголовках ответа: `X-Bapi-Limit` / `X-Bapi-Limit-Status` / `X-Bapi-Limit-Reset-Timestamp`. Для `GET position/list`: **50 в секунду**; для `POST order/cancel`: **10 в секунду** на аккаунт (по эндпоинту). Клиент заголовки не читает (транспорт возвращает только JSON).
- 10 запросов/с (параллельно, ровно по таймеру): `POST cancel` 40 из 40 без `10006`; `GET position/list` 39 из 40 (один `10002`, см. ниже). 15 запросов/с на `POST cancel`: 4 из 40 получили `retCode 10006` -> `PortError(kind="rate")` (запрос не обработан). Всплеск 30 параллельных `cancel`: 15 прошло, 15 `10006`. Всплеск 40 параллельных `position/list`: все прошли. Вывод: токен-бакет записей `rest_write_rps=4` из `exec_limits.json` с большим запасом, чтение `rest_read_rps=8` тоже.
- Последовательные вызовы (каждый на новом TLS-соединении): медиана ~0.55 с на запрос из этой среды, поэтому последовательно больше ~2 запросов/с не выжать.
- **Периодические задержки ~16 с на новых соединениях**: примерно 1 запрос из 40-100 (в разных прогонах, и без параллелизма) «зависает» на ~16 с, и уже подписанный запрос приходит на сервер просрочённым: `retCode 10002 "invalid request, please check your server timestamp or recv_window"` (recv_window 10 с; запрос НЕ обработан). `Client.call` один раз пересинхронизирует часы и повторяет. Причину (сеть, DNS, прокси CDN) установить не удалось: все 4 адреса `api-testnet.bybit.com` подключаются за 0.06-0.14 с. Рекомендации для WP5: (а) на `10002` писать в журнал решений и повторять только по новому решению (запрос не обработан), (б) общий дедлайн REST-вызова исполнителя заметно меньше 16 с, (в) позже рассмотреть постоянное keep-alive-соединение вместо нового на каждый запрос.

## Что не проверено
- Mainnet целиком (по условию), включая реальную периодичность kline/tickers и лимиты mainnet.
- Частичные исполнения рыночного входа (на 0.5 SOL всегда одно `execution`); логика нескольких `execution` проверена только на фиктивной бирже.
- Хедж-режим (`positionIdx` 1/2): порт работает ТОЛЬКО в одностороннем режиме (`positionIdx=0`).
- Срабатывание стопа с `slTriggerBy=MarkPrice` и поведение `trading-stop` при частично исполненном входе.
- `closed-pnl` при закрытии стопом (проверены только закрытия рынком).
- Лимит числа тем WS в одном соединении и поведение при 12+ символах.
