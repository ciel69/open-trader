# SPEC V2 / 01. План (Plan JSON): схема, состояния, валидация, примеры

Часть `docs/SPEC_V2.md`. Здесь всё про документ «план»: что в нём может быть, как он живёт и как проверяется. Код: `core/plan_schema.py` (WP2, чистые функции),
хранение `core/plan_store.py` (WP4), вычисление условий и правил `logic/*` (WP3). Все числа в плане это цены и доли; размера, плеча и маржи в плане НЕТ (их считает guard).

## 1.1 Принципы документа
- План это декларативное намерение «если A, то войти так; пока в сделке, делай B при условии C». Условий и действий конечный словарь (ниже), произвольного кода и выражений нет.
- Мозг пишет план один раз и правит его через `plan amend` на событиях. Исполнитель вычисляет условия по WS-ценам и свечам без LLM.
- Спецификация плана (`spec.json`) и исполнительское состояние (`runtime.json`) лежат в разных файлах. Единственный писатель обоих исполнитель (мозг только кладёт команды в `_inbox`).
- Версия схемы `tb.plan/1`. Неизвестное поле = ошибка (не молчаливый игнор): опечатка в условии не должна стать «план без защиты». Новые виды условий появляются только с новой минорной версией схемы (`tb.plan/1` -> `tb.plan/2`), исполнитель читает все версии, которые знает.
- Лимиты размера: JSON плана не больше 16 КБ, строки не длиннее 280 символов, массивы ограничены (targets <= 4, manage <= 12, invalidation <= 4, shadow_variants <= 3). Числа конечные (без NaN/inf), цены > 0.

## 1.2 Структура плана (поля, типы, умолчания)
`*` обязательно. `(E)` поле выставляет исполнитель, в `submit` не передаётся (передано = игнор с предупреждением `W_FIELD_IGNORED`, а не ошибка, чтобы мозг мог отправлять назад полученный `plan show`).

| Поле | Тип / диапазон | Умолчание | Смысл |
|---|---|---|---|
| `schema` * | `"tb.plan/1"` | | версия |
| `id` (E) | `PL-MMDD-SYM-NN`, regex `^PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3}$` | назначается | в `submit` необязателен |
| `rev` (E) | int >= 1 | 1 | растёт на каждый `amend`; история в `revs.jsonl` |
| `symbol` * | `^[A-Z0-9]{2,20}$` без `USDT` | | должен быть в `instruments` биржи (проверка исполнителя) |
| `side` * | `long` / `short` | | неизменяем |
| `mode` | `live` / `paper` | `live` | `paper` = теневой план (раздел 04), сделки в журнале `mode=paper` |
| `session_type` * для live | `scalp/intraday/swing/position` | для paper `intraday` | горизонт сделки: определяет `max_stop_pct`, `min_rr`, плечо, срок удержания и закрытие в конце дня; в сессии-дне (`day`, старт с дашборда) любой, в сессии одного горизонта (CLI `./tb session start --type ...`) должен совпасть с типом активной сессии (`core/session.py`) |
| `source` * | объект | | `kind`: `idea/scanner/brain/manual/shadow/adopt`; `idea_id` (`I\d+`), `event_seq` (int, seq события шины), `parent_plan` (для shadow) |
| `thesis` * | строка 10..280 | | одно-два предложения: почему; идёт в журнал сделки (`thesis`) |
| `tags` | объект | `{}` | `experiment` (`^[a-z0-9._-]{2,40}$`), `variant` (то же), `setup` (то же), `hypothesis` (то же), `extra` (<= 5 тегов). Используется статистикой (раздел 04) |
| `entry` * | объект | | см. 1.3 |
| `stop` * | `{price, trigger_by}` | `trigger_by: "last"` | `price` > 0; `trigger_by`: `last`/`mark`. Только фиксированная цена: guard считает размер от стопа |
| `targets` * | массив 1..4 | | лесенка тейков, см. 1.4 |
| `risk` | `{score, size_mult, confirm_waiver}` | `0.5, 1.0, false` | `score` 0..1 (как `--score` guard); `size_mult` в (0, 1], можно только уменьшать риск; `confirm_waiver:true` обязательно, если нет условия подтверждения (см. E_NO_CONFIRM) |
| `manage` | массив <= 12 | `[]` | правила сопровождения, 1.5 |
| `invalidation` | массив <= 4 условий | `[]` | условия, при которых тезис мёртв: до входа план -> `invalidated`; в позиции исполнитель закрывает (`close_reason: invalidation`) |
| `lifecycle` | объект | см. ниже | `ttl_min` (1..1440, срок жизни ДО входа; не больше остатка сессии, иначе обрезается с `W_TTL_CLAMPED`), `max_hold_min` (1..43200, срок позиции; по истечении закрытие `timeout`), `flatten_at_session_end` (bool, умолчание false; для scalp рекомендуется true), `not_before_ts` (epoch, не раньше) |
| `shadow_variants` | массив <= 3 | `[]` | `{name, patch}`: теневые копии с изменённым входом/тейками/правилами (раздел 04) |
| `created_ts`, `created_by`, `armed_ts`, `guard_preview`, `prereg_hash` (E) | | | `created_by`: `brain:chat`, `brain:api`, `owner`; `guard_preview`: результат `guard.evaluate` при приёме; `prereg_hash`: sha256 от канонического JSON полей `symbol, side, entry, stop, targets, manage, invalidation, risk` на момент `armed` (предрегистрация, нужна для честной оценки гипотез) |

`ref_price` (в `entry`) подсказка мозга об ожидаемой цене входа (для предварительной проверки guard и дистанций). Реальная проверка guard идёт в момент срабатывания по живой цене.

## 1.3 Вход (`entry`)
```
"entry": {
  "when":  <условие>            // необязательно. Нет `when`: для how=limit лимит ставится сразу при armed; для how=market вход немедленно (разрешено, но см. E_NO_CONFIRM)
  "how":   {"type":"market"|"limit"|"adopt", ...}
  "ref_price": 142.3,           // необязательно
  "valid": {"ttl_min": 12, "cancel_above": 143.4, "cancel_below": 140.0, "max_chase_bps": 12}
}
```
- `how.type = market`: рыночный ордер с встроенным `stopLoss` (атомарно). `max_chase_bps` (умолчание 15, потолок из `exec_limits.entry.max_chase_bps`): если живая цена на момент срабатывания дальше от опорной (`ref_price`, иначе уровень условия) больше чем на столько бп в невыгодную сторону, входа нет (`entry.state=blocked_chase`), исполнитель повторит проверку на следующих тиках до `ttl`, не более `blocked_attempts_max` раз.
- `how.type = limit`: `{"type":"limit","price":2312.0,"post_only":true,"snap":false}`. Post-only лимит со встроенным `stopLoss` (как `bybit_limit.py`: стоп атомарно с заполнением). Если `when` задан, лимит ставится только после его выполнения. Частичное исполнение: остаток отменяется при первой обработке (усреднения нет, как сейчас).
- `how.type = adopt`: взять под управление УЖЕ открытую позицию (миграция и старые команды): исполнитель проверяет, что позиция есть, стоп на бирже есть, в журнале есть открытая запись; добавляет `plan_id` в runtime; `entry.when` запрещён; `stop.price` должен совпасть с биржевым стопом или быть строже. Размер не меняется.
- `valid.cancel_above/below`: уровни по ту сторону рынка, куда цена ещё не ушла (как `--cancel-above/--cancel-below`): дошла без входа -> `invalidated` (до входа).
- Если `valid.ttl_min` не задан, берётся `lifecycle.ttl_min`.

### Словарь условий (конечный; используется в `entry.when`, `invalidation`, `manage[].when`)
Лист (`kind`):
| kind | поля | Смысл (семантика) | где можно |
|---|---|---|---|
| `price_beyond` | `dir: above/below`, `level`, `allow_already` (false) | последняя цена >= level (above) или <= level (below). С `allow_already:false` условие срабатывает только при ПЕРЕСЕЧЕНИИ после взвода: если на момент `armed` уже за уровнем, нужно сначала вернуться (как `already` в `watch`) | везде |
| `candle_close` | `tf: 1m/3m/5m/15m/1h`, `dir`, `level`, `n` (1..5, умолч. 1) | последние n ПОДТВЕРЖДЁННЫХ свечей tf закрылись по ту сторону уровня. Считаются только свечи, начавшиеся ПОСЛЕ взвода плана (вход) / входа в сделку (manage): история не срабатывает | везде |
| `candle_reject` | `tf`, `level`, `dir: short/long`, `wick_min_pct` (0) | последняя подтверждённая свеча: для short `high >= level*(1+w/100)` и `close < level`; для long `low <= level*(1-w/100)` и `close > level`. Это формализация «прокол и закрытие обратно» из TRADING.md | entry, invalidation, manage |
| `candle_body` | `tf`, `dir: up/down`, `min_body_pct`, `min_close_pos` (0..1, доля диапазона, на которой закрылась) | последняя подтверждённая свеча направленная и с телом не меньше порога | entry, manage |
| `volume_spike` | `tf`, `mult`, `lookback` (5..100, умолч. 20), `on: close/live`, `dir: any/up/down` | объём свечи >= mult x медиана объёма предыдущих lookback закрытых свечей. `on=close`: подтверждённая свеча; `on=live`: формирующаяся (по последнему пуш-обновлению kline, объём не пропорционируется, это консервативно) | entry, manage |
| `price_in_zone` | `low`, `high` | цена внутри зоны | везде |
| `spread_bps` | `lte` | спред (ask-bid)/mid в бп <= порога | entry (защита от плохих входов) |
| `pnl_r` | один из `gte/gt/lte/lt` | нереализованный результат в R: `side * (last - avg_entry) / r_unit`, `r_unit = |avg_entry_первого_входа - начальный_стоп|`, без комиссий | manage, invalidation (в позиции) |
| `elapsed` | `min` | минут с момента входа (manage) или с `armed_ts` (entry/lifecycle) | entry, manage |
| `target_filled` | `target` (id) | цель исполнена полностью | manage |
| `near_target` | `target`, `frac` (0..1) | цена прошла долю `frac` пути от входа до цели | manage |
Составные: `{"all":[...]}`, `{"any":[...]}`, `{"not": <лист>}` (глубина <= 3, листьев <= 8). Только в `entry.when`: `{"kind":"seq","steps":[лист,...],"within_min":N}`: шаги выполняются строго по порядку, выполненный шаг защёлкивается, весь `seq` сбрасывается, если после первого шага прошло больше `within_min` (модель «зашёл в зону, потом подтвердил закрытием»).
Правила вычисления (общие):
- Условия по свечам смотрят только ПОДТВЕРЖДЁННЫЕ свечи (`confirm=true`), кроме `volume_spike on=live`. Цена для `price_*` = последняя цена из WS тикера; нет свежей цены (старше `exec_limits.connectivity.public_price_stale_s`) = условие не выполняется и вход блокирован (`hold: stale_price`).
- Уровень `level` и `price` обязаны быть в пределах `exec_limits.entry.max_level_distance_pct` (умолч. 25%) от текущей цены на момент приёма (защита от опечаток на порядок).
- `all/any/not` вычисляются на каждом тике и на каждом подтверждении свечи; результат не защёлкивается, КРОМЕ шагов `seq`. Входной `when` по умолчанию «одноразовый»: когда выполнился, исполнитель немедленно пробует войти; блокировка guard или chase переводит вход в `blocked_*`, повторы ограничены.

## 1.4 Тейки (`targets`)
```
"targets": [ {"id":"t1","price":141.2,"frac":0.5,"exec":"resting"}, {"id":"t2","price":140.2,"frac":0.5,"exec":"resting"} ]
```
- `id` `^t[1-4]$` по порядку от ближнего к дальнему (для long цены возрастают, для short убывают; иначе `E_TARGET_ORDER`). `frac` в (0,1], сумма <= 1. Остаток (1 - sum) это «бегун»: его закрывает трейл, `manage`, `invalidation`, `max_hold_min` или стоп.
- `exec: resting` (умолч.): после входа исполнитель ставит reduce-only лимит на `frac` исходного количества (maker 0.036%). `exec: touch`: ордера нет, исполнитель закрывает reduce-only по рынку, когда цена коснулась уровня (taker; для малых счётов, где лимиты не делятся на лоты).
- Количество каждой части округляется вниз по `qtyStep`; последняя часть берёт остаток; часть меньше `minOrderQty` (или номинала < `minNotional`) сливается со следующей. Если всё сливается в одну часть: предупреждение `W_LADDER_COLLAPSE` при приёме (на счёте около 39 USDT лесенка часто вырождается в 1-2 части, мозг должен это видеть сразу).
- Для проверки RR guard использует средневзвешенную цену тейков по `frac`; доля бегуна считается на цене ПОСЛЕДНЕГО тейка (консервативно, без «мечты о луне»).
- Тейки можно придвигать свободно; отодвигать (увеличивать цель) допустимо только если стоп уже в безубытке или лучше (иначе `E_TARGET_FARTHER_NOT_RISKFREE`).

## 1.5 Правила сопровождения (`manage`)
Каждое правило: `{"id","when","do","once","max_times","cooldown_s"}`. `id` `^[a-z0-9_]{2,16}$` уникален; `once:true` = выполнить один раз; `max_times` (умолч. 1 при once, иначе без ограничения); `cooldown_s` (умолч. 0) пауза между срабатываниями. Правила вычисляются только в состоянии `managing`, в порядке массива; побочные эффекты применяются после проверки Gate (раздел 02, 2.5).
Действия `do.action`:
| action | параметры | Семантика и ограничения |
|---|---|---|
| `move_stop` | `to`: `breakeven` / `price` (+`price`) / `lock_r` (+`r`) / `entry_pct` (+`pct`); `offset_bps` для breakeven (умолч.: комиссии круга по факту плюс 2 бп) | Целевой стоп считается и сравнивается с ТЕКУЩИМ биржевым: хуже или равен = no-op `NOT_TIGHTER` (не ошибка). Только подтягивание |
| `trail` | `mode`: `candle_extreme` (`tf`,`n`), `atr` (`tf`,`n`,`mult`), `percent` (`pct` от лучшей цены), `step_r` (`step_r`); `buffer_bps`; `min_step_ticks` | Непрерывное правило: пересчёт на каждой подтверждённой свече tf (percent/step_r на тике, не чаще `exec_limits.stop_moves.min_interval_s`). Кандидат стопа применяется, только если он тверже текущего и не ближе `min_gap_bps_from_price` к цене |
| `partial_close` | `frac_of_remaining` или `frac_of_initial`, `exec: market` | reduce-only по рынку; уменьшение риска, guard не нужен |
| `close` | `frac` (0..1, умолч. 1), `reason`: `timeout/invalidation/rule/risk` | закрытие целиком или частично, reduce-only рынок |
| `move_target` | `target`, `price` | см. правило придвигания/отодвигания в 1.4 |
| `add` | `risk_frac` (0..1, доля исходного риска-бюджета R0), `how`: `market` | Пирамидинг (раздел 1.7 `SPEC_V2.md`, ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ ВЛАДЕЛЬЦА, умолч. выключено). Gate проверяет прибыль, безубыток стопа и суммарный риск плана |
| `notify` | `code`: из закрытого списка (`R_PLUS`, `R_MINUS`, `NEAR_TARGET`, `CHECK_PLAN`, `TIME`) | событие `brain_wake_request` в шину; ничего не торгует |
Статические запреты валидатора: правило `add` обязано содержать в `when` конъюнкт `pnl_r gte x` с `x >= exec_limits.pyramid.min_r` (иначе `E_ADD_NOT_PROFIT_GATED`); `add` вместе с `exec_limits.pyramid.enabled=false` принимается как предупреждение `W_ADD_DISABLED` (правило будет пропускаться со `skip: ADD_DISABLED`); `move_stop.to=price` с ценой не на защитной стороне относительно `stop.price` (шире начального стопа) = `E_STOP_WIDEN`.

## 1.6 Машина состояний
Статусы: `draft` (принят CLI и лежит в `_inbox`/ещё не принят исполнителем), `armed` (принят, ждёт входа), `entered` (вход исполнен, идёт проверка защиты и постановка лесенки; секунды), `managing` (позиция под правилами), `closed`, `cancelled`, `expired`, `invalidated`, плюс служебный терминал `rejected` (исполнитель отказал при приёме: валидатор или guard; в таком плане нет позиции и ордеров). Ортогональный флаг `hold` (+`hold_reason`): не статус; пока `hold`, запрещены вход и добор, разрешены защитные действия (подтянуть стоп, закрыть, снять ордера).
```
draft --accept--> armed                        (исполнитель: валидатор + guard preview прошли; событие plan_armed)
draft --reject--> rejected                     (событие plan_rejected с кодами)
armed --entry filled--> entered --protection ok--> managing
armed --cancel (brain/owner/kill)--> cancelled        armed --ttl--> expired        armed --invalidation/cancel_above|below--> invalidated
armed --blocked_attempts exhausted--> invalidated (reason ENTRY_BLOCKED)
entered|managing --position gone/closed fully--> closed   (close_reason: tp|sl|manual|timeout|invalidation|rule|kill|session_end|risk|liquidation)
managing --brain: close--> (действие) --> closed
```
Допустимые переходы (всё остальное `E_TRANSITION`): `draft->armed|rejected|cancelled`, `armed->entered|cancelled|expired|invalidated`, `entered->managing|closed`, `managing->closed`. Из терминалов (`closed, cancelled, expired, invalidated, rejected`) выхода нет; новая идея это новый план. `entered->closed` возможен, если защита не подтвердилась и позицию аварийно закрыли.
Кто инициирует: `draft->armed|rejected`, `armed->entered|expired|invalidated`, `entered->managing`, `*->closed` по факту биржи/правил: исполнитель. `cancel`, `close` по команде: мозг или владелец (исполнитель выполняет). Частичное исполнение входа: `armed->entered` с `partial:true`, остаток снят.

## 1.7 Что можно менять (`amend`) по статусам
`plan amend` это JSON Merge Patch (RFC 7386, массивы заменяются целиком) поверх spec; применяется атомарно целиком или отклоняется целиком. Обязателен `rev_expected` (оптимистичная блокировка; устаревший вид мозга = `E_REV_STALE`).
| Поле | draft / armed (позиции нет) | entered / managing |
|---|---|---|
| `symbol`, `side`, `mode`, `source`, `session_type` | нельзя (`E_IMMUTABLE`) | нельзя |
| `thesis`, `tags` | можно | можно (`tags.experiment` нельзя после armed: предрегистрация) |
| `entry.*` | можно; ордер снимается и ставится заново (новая ревизия, новый `link_id`), полный повтор проверки guard | нельзя |
| `stop.price` | можно (размер пересчитывается guard; стоп шире ослабляет размер, не риск) | ТОЛЬКО в сторону уменьшения риска относительно ТЕКУЩЕГО БИРЖЕВОГО стопа (`E_STOP_WIDEN` иначе); снять стоп нельзя |
| `targets` | можно | придвинуть можно; отодвинуть только при стопе в безубытке или лучше; убрать цель = снять ордер, допустимо (уменьшает, не увеличивает риск); `frac` исполненных целей неизменны |
| `risk.size_mult` | можно только уменьшить, если уже есть заявка; до отправки любой в (0,1] | нельзя |
| `risk.score`, `risk.confirm_waiver` | можно | нельзя |
| `manage` | можно | можно (изменение или удаление правил; новое `add` проходит те же проверки) |
| `invalidation` | можно | можно |
| `lifecycle.ttl_min` | можно | `max_hold_min` можно уменьшать; увеличивать нельзя без `E_HOLD_EXTEND` (продление держит риск дольше); исключение: стоп уже в безубытке или лучше |
Отдельные команды без патча: `plan stop ID --to PRICE|be --rev N` (синтаксический сахар для подтягивания стопа), `plan close ID [--frac F]`, `plan cancel ID`.

## 1.8 Коды валидации
Каждая проблема: `{"code","severity":"error|warn","path":"entry.when.steps[1].level","msg":"..."}`. `submit` отклоняется при любой `error`. Предупреждения приходят в ответе.
- Структура: `E_SCHEMA`, `E_UNKNOWN_FIELD`, `E_TYPE`, `E_RANGE`, `E_TOO_LARGE`, `E_ID`, `E_DUP_ID` (повтор id правил/целей), `E_COND_DEPTH`, `E_COND_KIND_FORBIDDEN` (вид условия не разрешён в этом месте), `E_NONFINITE`.
- Смысл: `E_SYMBOL_UNKNOWN`, `E_STOP_SIDE` (стоп не с той стороны от опорной цены), `E_TARGET_SIDE`, `E_TARGET_ORDER`, `E_FRAC_SUM`, `E_LEVEL_DISTANCE` (уровень дальше `max_level_distance_pct`), `E_CANCEL_LEVELS` (cancel_above ниже цены и т. п.), `E_TTL`, `E_ADOPT_NO_POSITION`, `E_ADOPT_NO_STOP`.
- Риск: `E_GUARD` (в `msg` причины guard: `daily_loss_limit_reached`, `max_open_positions`, `net_rr`, `stop > max`, ...), `E_STOP_PCT`, `E_RR`, `E_SESSION_MISMATCH`/`E_NO_SESSION`, `E_NO_CONFIRM` (вход `market` или `limit` без условия подтверждения и без `risk.confirm_waiver:true` и `size_mult <= exec_limits.entry.no_confirm_max_size_mult` (0.5): правило TRADING.md «риск вдвое меньше и тег no-confirm»; исполнитель сам добавляет тег `no-confirm` в журнал), `E_COOLDOWN` (монета/общая пауза после стопов, TRADING.md п. 3), `E_LIMIT_PLANS` (слишком много активных планов, частота `submit`), `E_SYMBOL_BUSY` (по монете открыта позиция, ждущий лимит или другой активный план: усреднения нет).
- Изменения (`amend`): `E_STOP_WIDEN`, `E_RISK_UP`, `E_IMMUTABLE`, `E_STATUS_FORBIDS`, `E_REV_STALE`, `E_TARGET_FARTHER_NOT_RISKFREE`, `E_HOLD_EXTEND`, `E_TRANSITION`.
- Пирамидинг: `E_ADD_NOT_PROFIT_GATED`, `E_ADD_RISK` (`risk_frac` > 1 или > `exec_limits.pyramid.max_add_risk_frac`).
- Предупреждения: `W_FIELD_IGNORED`, `W_LADDER_COLLAPSE`, `W_TTL_CLAMPED`, `W_NO_INVALIDATION` (нет условий инвалидации), `W_ADD_DISABLED`, `W_NO_RUNNER_RULE` (бегун без трейла и без max_hold), `W_PRICE_STALE_AT_ACCEPT`.
Валидатор многоуровневый: (1) чистая структура и типы (`plan_schema.normalize/validate_static`, без сети и файлов), (2) контекстная (`validate_context`: сессия, лимиты `exec_limits`, инструмент, цена, guard preview; контекст передаётся аргументом), (3) инварианты изменения (`validate_amend(old, new, status, live_state)` сравнивает с текущим состоянием исполнителя, а не только со spec).

## 1.9 Пример: скальп (short от сопротивления, подтверждение «прокол и закрытие обратно» + объём)
Опорные числа: вход около 142.3, стоп 143.0 (0.49%), цели 141.2 и 140.2 (средневзвешенная 140.7 = 1.12%), издержки taker-круга 0.24% => net RR = (1.12-0.24)/(0.49+0.24) = 1.20 >= `min_rr` scalp 1.0.
```json
{
  "schema": "tb.plan/1",
  "symbol": "SOL", "side": "short", "mode": "live", "session_type": "scalp",
  "source": {"kind": "idea", "idea_id": "I7", "event_seq": 1842},
  "thesis": "Прокол сопротивления 142.5 и закрытие 5м обратно под уровень на повышенном объёме; цель откат к 140.2",
  "tags": {"experiment": "sweep-reject-v1", "variant": "A", "setup": "level_reject", "hypothesis": "H3"},
  "entry": {
    "when": {"all": [
      {"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.05},
      {"kind": "volume_spike", "tf": "5m", "mult": 1.8, "lookback": 20, "on": "close", "dir": "any"},
      {"kind": "spread_bps", "lte": 6}
    ]},
    "how": {"type": "market"},
    "ref_price": 142.3,
    "valid": {"ttl_min": 12, "cancel_above": 143.4, "cancel_below": 140.0, "max_chase_bps": 12}
  },
  "stop": {"price": 143.0, "trigger_by": "last"},
  "targets": [
    {"id": "t1", "price": 141.2, "frac": 0.5, "exec": "resting"},
    {"id": "t2", "price": 140.2, "frac": 0.5, "exec": "resting"}
  ],
  "risk": {"score": 0.7, "size_mult": 1.0},
  "manage": [
    {"id": "be_1r",   "when": {"kind": "pnl_r", "gte": 1.0},              "do": {"action": "move_stop", "to": "breakeven"}, "once": true},
    {"id": "be_t1",   "when": {"kind": "target_filled", "target": "t1"},  "do": {"action": "move_stop", "to": "lock_r", "r": 0.3}, "once": true},
    {"id": "trail",   "when": {"kind": "pnl_r", "gte": 1.3},              "do": {"action": "trail", "mode": "candle_extreme", "tf": "1m", "n": 3, "buffer_bps": 4}},
    {"id": "stale",   "when": {"all": [{"kind": "elapsed", "min": 10}, {"kind": "pnl_r", "lt": 0.3}]}, "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": true},
    {"id": "wake_m05","when": {"kind": "pnl_r", "lte": -0.5},             "do": {"action": "notify", "code": "R_MINUS"}, "once": true}
  ],
  "invalidation": [ {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 143.3, "n": 1} ],
  "lifecycle": {"ttl_min": 12, "max_hold_min": 15, "flatten_at_session_end": true}
}
```
Что сделает исполнитель: взведёт план (`armed`), на каждом пуше kline 5м SOL проверит `all`; при выполнении немедленно вызовет `guard.evaluate` по живой цене (получит `leverage`, `margin`), отправит market-sell со встроенным стопом 143.0, поставит reduce-only лимиты на 50%/50% количества, запишет `open` в журнал с тегами `plan:PL-..`, `exp:sweep-reject-v1`, `var:A`, затем будет подтягивать стоп по `manage`. Если за 12 минут условие не выполнилось, цена дошла до 143.4 или 5м закрылась выше 143.3, план `expired`/`invalidated` и идея `I7` помечается соответственно.

## 1.10 Пример: интрадей (long, «проход под уровень и возврат»), с лесенкой, трейлом и пирамидингом
Опорные числа: SOL 145.6, зона проверки 144.0, подтверждение 5м закрытием выше 145.0, стоп 143.2 (за экстремум отката), цели 148.0 (50%) и 151.5 (50%): средневзвешенная 149.75, до неё 2.85%, стоп 1.65% => net RR = (2.85-0.24)/(1.65+0.24) = 1.38 < 1.5 (intraday): ПЛАН БЫЛ БЫ ОТКЛОНЁН. Поэтому цели подняты до 148.6 и 153.0 (взвешенная 150.8 = 3.57%): net RR = (3.57-0.24)/(1.89) = 1.76 >= 1.5. (Пример намеренно показывает, что валидатор считает RR после издержек; `guard_preview` в ответе содержит `net_rr`.)
```json
{
  "schema": "tb.plan/1",
  "symbol": "SOL", "side": "long", "mode": "live", "session_type": "intraday",
  "source": {"kind": "brain"},
  "thesis": "Снятие стопов под 144 и возврат выше 145 на 5м после дневного восходящего тренда; держим до 153",
  "tags": {"experiment": "sweep-reclaim-v1", "variant": "B", "setup": "reclaim", "hypothesis": "H5", "extra": ["trend-up-1h"]},
  "entry": {
    "when": {"kind": "seq", "within_min": 90, "steps": [
      {"kind": "price_beyond", "dir": "below", "level": 144.0},
      {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 145.0, "n": 1}
    ]},
    "how": {"type": "market"},
    "ref_price": 145.6,
    "valid": {"ttl_min": 180, "cancel_above": 150.0, "cancel_below": 142.0, "max_chase_bps": 20}
  },
  "stop": {"price": 143.2, "trigger_by": "last"},
  "targets": [
    {"id": "t1", "price": 148.6, "frac": 0.5, "exec": "resting"},
    {"id": "t2", "price": 153.0, "frac": 0.5, "exec": "resting"}
  ],
  "risk": {"score": 0.6, "size_mult": 1.0},
  "manage": [
    {"id": "be_1r",   "when": {"kind": "pnl_r", "gte": 1.0},             "do": {"action": "move_stop", "to": "breakeven"}, "once": true},
    {"id": "trail",   "when": {"kind": "target_filled", "target": "t1"}, "do": {"action": "trail", "mode": "atr", "tf": "15m", "n": 14, "mult": 2.0, "buffer_bps": 5}},
    {"id": "pyr",     "when": {"all": [{"kind": "pnl_r", "gte": 1.5}, {"kind": "candle_close", "tf": "15m", "dir": "above", "level": 148.0, "n": 1}]},
                      "do": {"action": "add", "risk_frac": 0.5, "how": "market"}, "once": true},
    {"id": "tmax",    "when": {"kind": "elapsed", "min": 600},           "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": true},
    {"id": "wake_t1", "when": {"kind": "near_target", "target": "t1", "frac": 0.8}, "do": {"action": "notify", "code": "NEAR_TARGET"}, "once": true}
  ],
  "invalidation": [
    {"kind": "candle_close", "tf": "15m", "dir": "below", "level": 144.6, "n": 1}
  ],
  "lifecycle": {"ttl_min": 180, "max_hold_min": 720, "flatten_at_session_end": false}
}
```
Замечания к примеру: (1) правило `pyr` работает только при `exec_limits.pyramid.enabled=true` и выполнении всех условий пирамидинга; пока выключено, валидатор принимает с `W_ADD_DISABLED`; (2) `invalidation` «15м закрылась ниже 144.6» в позиции означает «возврат под зону проверки, тезис мёртв раньше стопа 143.2»: исполнитель закрывает reduce-only по рынку (убыток обычно меньше стопа, ценой лишних комиссий и риска выбивания шумом; мозг выбирает осознанно); (3) на счёте около 39 USDT количество часто 0.2-0.4 SOL, поэтому лесенка 50/50 даёт две части по шагу 0.1 или вырождается (`W_LADDER_COLLAPSE`).

## 1.11 Команды-конверты (inbox/outbox)
`./tb plan *` не пишет планы сам. Он кладёт атомарно (tmp + rename) файл `state/plans/_inbox/C-YYYYMMDDTHHMMSS-xxxxxx.json`:
```
{"cmd_id":"C-20261028T101502-a1b2c3","ts":"...","actor":"brain:chat","op":"submit|amend|cancel|close|stop|hold|release|adopt",
 "plan_id":"PL-1028-SOL-01","rev_expected":3,"payload":{...patch|plan|{"to":..,"frac":..}},"reason":"<=200 символов (почему)",
 "event_seq":1842,"brief_id":"B-20261028-1015-02"}
```
Исполнитель обрабатывает по порядку имён, пишет результат в `_outbox/<cmd_id>.json` (`ok, status, plan_id, rev, errors[], warnings[], guard_preview`) и в журнал решений, удаляет команду из `_inbox`. CLI с `--wait S` (умолч. 10) опрашивает `_outbox`. Исполнителя нет (устарел heartbeat): CLI честно отвечает `queued, executor_down`, команда остаётся в очереди. Повторная доставка команды (тот же `cmd_id`) идемпотентна: исполнитель хранит последние 500 `cmd_id`.
Команда `hold` / `release` (мозг может поставить hold, снять его может исполнитель по условию или владелец) и `adopt` (создать план для существующей позиции) см. `02_executor.md`.
