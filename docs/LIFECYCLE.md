# Жизненный цикл дня (WP14)

Спецификация: `docs/spec_v2/07_control_lifecycle_news_roles.md` п. 2, роли `docs/spec_v2/07b_roles_providers.md` п. 5.3, 5.6-5.7. Код: `src/tb/lifecycle/`, настройки: `config/lifecycle.json`, вкладка «День» на дашборде, CLI `./tb day ...` (только чтение).
Управляет днём сервис `ctl` (WP12) через модуль `tb.lifecycle.api`; заглушка `tb.control.lifecycle` остаётся на случай отсутствия модуля.

## Идея в трёх строках
День = старт (S1-S9) -> работа -> мягкая остановка (C1-C8) -> итог дня. Всё, что можно сделать без LLM, делается без LLM (preflight, чек-лист закрытия, отчёт, уведомления); LLM (`morning_analyst`, `day_auditor`) только предлагает, жёсткие условия переноса K1-K8 проверяет код.
Слой не знает биржи: исполнителем управляет только очередью команд плана (`close`, `cancel`, `adopt` от актора `system`), подтверждение берёт из `runtime/exec_snapshot.json` и файлов планов. `venues` и `executor` не импортируются (тест слоёв).

```
ctl --day.start--> starter S1..S9 --(порт мозга: morning_analyst)--> dayplan.json, day_limits.json, session, trading.json
ctl --day.stop---> audit C1..C8 --(порт мозга: day_auditor)--> carry.json, checklist.json, report.json/md
ctl.watch() -----> api.tick(): возобновление, автоостановка, продолжение закрытия, синхронизация platform.json
```

## Состояния и файлы дня
`stopped -> starting -> running -> closing -> stopped`, из любого активного `-> halted` (KILL), `halted -> stopped` после снятия KILL. Параллельный день невозможен (`E_STATE`). «Стоп» дня процессы не останавливает.
Файлы `state/days/<D-YYYYMMDD-N>/`: `day.json` (главный), `preflight.json`, `dayplan.json`, `dayplan_raw.json`, `start.md`, `audit_input.json`, `checklist.json`, `audit.json`, `carry.json`, `report.json`, `report.md`.
Вне папки дня: `state/control/trading.json` (торговый флаг для шлюза), `state/control/day_limits.json` (суженные лимиты дня), `state/control/day_banner.json` (баннер).

`day.json` (`tb.day/1`): `day_id, state, phase (start|run|stop), mode, agent, team, session{type,minutes,id}, live_waivers[], args{...}, started, stopped, stop_source (owner|auto|abort|emergency), abort{step,code,detail}|null,
steps[]{name,phase,ok (true|false|null=идёт),ts,dur_s,detail,code,tries}, runner{pid,hb}, brief_id, facts{status,age_s,degraded[]}, preflight{red[],amber[]}, dayplan{...кратко}, carry_in[], owner{questions[]{id,item,plan,text,options,answer,answered_by,answered_ts},deadline_ts},
closing{started,source,auto_reason,cancel_cmds,close_cmds,final[],failed{items,manual,reason}|null,manual[],emergency,job}, report{ready,ts,verdict,acked,net,carried_n,questions_n,ack_note}, needs_resume`.
Каждый шаг пишется ДО выполнения (`ok:null`) и ПОСЛЕ; шаги идемпотентны, при возобновлении выполняются те, у которых нет `ok=true`.

## Старт: S1-S9
| шаг | что | при провале |
|---|---|---|
| S1 preflight | P1-P16 (`preflight.json`); красные блокируют; жёлтые блокируют, если не `allow_amber`, кроме live (там это уже принятые W-предупреждения) | откат, `E_GATE` |
| S2 facts | `runtime/intel/refresh.req`, ждёт свежий `facts.json` до 60 с (только если жив сервис `intel`), иначе amber | всегда ok (amber в detail) |
| S3 sync | testnet/live: equity биржи vs портфель; инвентаризация; `carry.json` ПРЕДЫДУЩЕГО дня -> `day.json:carry_in` | `E_GATE` |
| S4 brief | `brief --scope start`, `brief_id` дня | откат |
| S5 briefing | задание `morning_analyst`: 2 попытки, на повтор уходят только КОДЫ ошибок формата | live без мозга: `E_GATE`; иначе запасной план |
| S6 dayplan | валидатор `tb.dayplan/1`: лимиты = min(agents.json, уровень риска, risk_profile); в торговом дне (`session=day`, дашборд) `session.type` плана дня = потолок горизонта сделок на сегодня (`day_limits.json:max_horizon`), минуты не больше заданных владельцем; в сессии одного горизонта (CLI) тип не выше выбранного владельцем; blackout в ближайший час даёт `max_new_plans=0` (в paper при `paper.blackout_mode=warn` планы остаются, `size_mult<=0.5`, код риска `blackout_warn`; P14 жёлтая с пометкой; live/testnet: всегда запрет) | невалидный: консервативный план (amber) |
| S7 session | `session.start(type, thesis, ...)`: с дашборда тип `day` (контейнер дня: бюджет, лимит сделок и срок как у intraday или `risk_profile.day_session`; горизонт каждой сделки scalp/intraday/swing/position выбирает агент в плане, guard берёт стоп/RR/плечо по горизонту плана); из CLI тип-горизонт как раньше; бюджет только из risk_profile; в `observe` пропускается | `E_LIMITS`, откат |
| S8 enable | `trading.json {day_id, enabled, mode, agent, until_ts}` (в `observe` enabled=false) | откат |
| S9 report | `start.md`, баннер, событие `day_report kind=start` | не критичен |
Откат: сессия остановлена с пометкой `aborted: <шаг>`, торговый флаг выключен, `state=stopped`, причина в `day.json:abort` и на баннере.
Preflight P1-P16: супервизор/ctl/здоровье; исполнитель (профиль, hb, hold, сверка, diffs); фид; сканер и диспетчер (A); KILL; ключи (только наличие: `launcher.key_status`, если есть; ключ провайдера через `llm.keys.present`); баланс и сверка; нет позиций без плана; конфиги читаются и `exec_cfg.warnings()` пуст; свежесть market/context/levels (A); бюджет LLM и проба (R в live); диск и логи (A); дневной лимит убытка (live); blackout в первые 30 минут (A); фактура (A); нет незавершённого предыдущего дня.
`./tb day preflight [--mode M]` показывает таблицу без запуска дня.

## План дня `tb.dayplan/1`
Поля как в 07 п. 2.3 (`regime, bias, session, watch<=6, allowed_setups, avoid, limits{max_new_plans, max_risk_usdt, symbols, size_mult}, carry{allow,max_positions}, carried_review, risks, questions<=3, why<=200`) плюс `meta{source: agent|fallback, warnings[], errors[]}`.
Любая ошибка схемы/enum = весь ответ отвергнут (повтор, затем консервативный план); превышение лимитов = обрезка с предупреждением `CLAMP:*`; текст очищается от кавычек, обратных слэшей, управляющих символов.

## Остановка: C1-C8 (шаги не блокируют ctl)
C1 отмена ждущих входов (кроме A1-исключения) -> C2 снимок (`audit_input.json`; при `diffs>0` ждёт сверку до 30 с) -> C3 чек-лист A0-A9 (`checklist.json`) -> C4 `day_auditor` (по кандидатам; нет мозга/ошибка/таймаут 90 с = чек-лист остаётся) -> C5 вопросы владельцу (ждёт `owner_wait_min`=15, без ответа close) ->
C6 исполнение: `close`/`adopt`+`close` в очередь, подтверждение по снимку, одна повторная попытка через 20 с, `exec_wait_s`=120 с, провал = `error DAY_CLOSE_FAILED` (wake), день остаётся `closing` с баннером alarm -> C7 `session stop --note "eod; carry=[..]"` -> C8 `report.json/md`, событие `day_report`, баннер, уведомление, `stopped`.
Пока шаг ждёт (мозг, владелец, исполнитель), `stop_day` возвращает `state: closing`, дальше ведёт `tick` (раз в 2 с). Торговый флаг выключается в момент остановки.

### Чек-лист и K-условия
A0 позиция без плана / план в hold / расхождение -> close; A1 ждущий вход -> cancel (keep: swing/position, ttl дольше времени до следующего дня, нет K4, политика не `never`); A2 scalp -> close (никогда не переносится); A3 intraday, A4 swing/position -> кандидат на перенос; A6 истёк `max_hold` -> close; A7 любой K провален -> close; A8 сессия stop; A9 идеи остаются.
K1 стоп на бирже, подтверждён сверкой, не шире `max_stop_pct` типа, на защитной стороне; K2 риск до стопа (с комиссиями) <= `max_risk_frac_of_trade` x R0 либо стоп в безубытке; K3 число и суммарный риск переносов в рамках уровня (кумулятивно, меньший риск первым); K4 нет события календаря impact>=3 / blackout / события по символу / листинга-делистинга / техработ в `event_hours`; K5 funding до следующего старта <= `max_funding_frac_of_risk` x риск (неизвестный funding = отказ); K6 до ликвидации не меньше `liq_factor` x стоп; K7 у позиции есть план, нет hold/diffs/`reconcile_alert`; K8 нет hold, приватный WS жив, мозг доступен, intel и провайдер отвечали последние 30 минут.
Без мозга K8 не проходит: **перенос без доступного мозга невозможен**.
Политика уровня риска (`risklevels.carry_policy`): `never` (1) всё закрывается; `owner_confirm` (2-3) агент предлагает, владелец подтверждает кнопкой; `agent` (4-5) решает агент в рамках K и лимитов уровня. Без решения агента: swing/position переносятся только при `agent`, K1-K8 и наличии трейла или `max_hold` в плане; intraday закрывается.
Решения владельца и агента перепроверяются K-условиями перед исполнением (C6): `K_FAILED:<код>` -> close.

### Блэкаут в Gate (жёсткий отказ)
Gate (`tb.executor.gate`, функция `tb.core.blackout.check`, единая для Gate, preflight и валидатора) отклоняет RISK_UP (вход, лимитный вход, добор пирамидинга) кодом `E_BLACKOUT`, если сейчас внутри окна из `runtime/intel/blackouts.json` (календарь intel + `watch_config.json: blackouts`, границы включительно) или за `blackout.pre_min` минут до его начала (`exec_limits.json`, умолчание 0: окно календаря уже включает «до» из `intel.json: calendar.windows`, например FOMC [30, 90]). В `reasons` событие и минуты до конца окна. Действует в live и testnet всегда и в paper при `paper.blackout_mode=block`; paper при `warn` не меняется (предупреждение, тег `blackout`, size_mult <= 0.5). Непроверенные кандидаты (`v:false`) считаются окном.
Поведение: отказ пишется в журнал решений (`skip place_entry DENY:E_BLACKOUT ...`, раз в 30 с на план) и шлёт `action_rejected` (wake); это НЕ попытка входа (код в `TRANSIENT`), ENTRY_BLOCKED не копится, план остаётся armed и входит после окна, если условие входа ещё действует и ttl не истёк, иначе истекает по ttl. Защитные действия (подтяжка стопа, закрытие, частичное закрытие, отмена, reduce-only, лесенка, flatten, hold/release) Gate по блэкауту не проверяет никогда.
Нет данных об окнах (файла нет, битый, нет ключа `windows`, поле `ts` старше 48 ч): live и testnet отказывают всегда (`blackout.on_missing` там игнорируется, `allow` не ослабляет), paper по `blackout.on_missing` (`auto` = allow, `block`).

### Схема `tb.carry/1` (ответ `day_auditor`, массив `carry` в `tb.audit/1`)
`{"schema":"tb.carry/1","item":"pos:PL-..","decision":"carry|close","basis":[trend_intact,level_held,volume_supportive,news_neutral,event_far,funding_ok,stop_at_be,thesis_valid],"against":[news_risk,volume_fading,level_lost,event_near,funding_high,range_expanding],"view":{"risk":"low|mid|high","horizon_h":1..168,"recheck_min":5..1440},"why":"<=200","conf":0..1}`.
Агент может закрыть любую запись; `carry` принимается, только если запись кандидат (K пройдены), `basis` не пуст, политика разрешает, `dayplan.carry.allow` не запрещает, K3 по принятым. Иначе close с кодом в `audit.json:rejected`.
`carry.json` (`tb.carryfile/1`): `{day_id, ts, next_day_start, items[]{plan,sym,side,stype,size,avg,stop,risk_usdt,why,codes,basis,against,view,decided_by (agent|owner|default),rule,K{K1..K8:bool},carried_at}}`. Следующий день читает его на S3 (только непосредственно предыдущий день) и передаёт аналитику как `carried_in`.

## Итог дня
`report.json` (`tb.dayreport/1`): `day_id, kind (stop|emergency), mode, agent, team, profiles, started, stopped, stop_source, session[]{id,type,pnl,trades,budget_usdt,budget_used}, pnl{realized_gross,fees,funding,net,r_sum}, trades[], trades_n, err_tags{},
cancelled[], carried[], closed_eod[], errors{exec,rejected_actions,brain_format,fallbacks,conn}, cost{usd,tokens_in,tokens_out,calls,by_profile,by_role}, intel{degraded,events_used,status}, open_questions[]{id,text,options}, checklist{rows}, agent_audit{llm,decisions,rejected}, narrative, waivers[], close_failed, llm_unavailable, verdict (ok|attention|alarm)`.
`report.md` строится шаблоном (golden-тест `tests/fixtures_days/report_golden.md`). Оценка: alarm (закрытие не подтверждено, позиция без плана не закрыта, аварийный день), attention (замечания старта, вопросы, ошибки, теги разбора, мозг недоступен), иначе ok.
Расход берётся из `state/llm_calls.jsonl` за окно дня. Событие шины `day_report` (wake): `kind, day, net, carried_n, questions_n, verdict`; `day_state` (info): `state, step, code`.
Баннер `state/control/day_banner.json` виден в `status.json` ctl и на вкладке «День» до `report.ack`.
Уведомления ОС (`notify.os=true`, по умолчанию выключено): macOS `osascript -e 'display notification ...'`, Windows `powershell -NoProfile -NonInteractive -Command` с текстом в переменной окружения `TB_NOTIFY_TEXT`; без shell, текст только из шаблона с числами; отказ не ошибка.

## Что делают действия ctl
- `day.start`: `start_day` (S1-S9); `day.stop`: `stop_day`; KILL: `emergency_summary` (день -> `halted`, краткий отчёт без LLM, шаги закрытия не идут: позиции ведут исполнитель и биржевые стопы; после снятия KILL день -> `stopped`).
- `carry.decide {plan, decision}`: отвечает на вопрос C5; без ожидающего вопроса `E_STATE` (владелец не может перенести то, что код не предложил/не разрешил); `report.ack {day_id, note?}`: убирает баннер; для дня, застрявшего в `closing` после `day_close_failed`, нужна пометка (`note`), тогда день `stopped` («разобран вручную»); `notify.test`: `E_DISABLED`, пока `notify.os=false`.

## Краевые случаи
Падение ctl на старте: `starting` с устаревшим пульсом (45 с) возобновляется с первого шага без `ok=true` (в потоке), старше 10 минут = откат `aborted_start`. Падение посреди остановки: `tick` продолжает с шага без `ok`; команды не дублируются (id команд хранятся в `closing.cancel_cmds/close_cmds`).
Расхождение позиции с планом: A0, перед чек-листом ждём сверку до 30 с. Несколько сессий за день: отчёт перечисляет все, останавливается активная. Позиция без плана: `adopt` затем `close`; отказ исполнителя = вопрос `M<n>` владельцу и оценка alarm.
Авто-стоп: `max_day_hours`, `day_end_utc`, `stop_day_on_session_end`. Перезапуск платформы при `running` дне: торговый флаг выключается до подтверждения (`day.json:needs_resume`; API `resume_day` готов, кнопки в ctl пока нет).
Нет LLM: C4 пропущен, отчёт помечен `llm_unavailable`.

## Порты (для интеграции WP11-C2/C3)
- Мозг (`tb.lifecycle.ports.LifecycleBrain`): `available(root, now) -> (bool, msg)`, `submit(role, payload, timeout_s, now, meta) -> job_id`, `poll(job_id, now) -> None | {"ok","output","err"}`, `cancel(job_id)`. Реализация по умолчанию `FileJobBrain`: задание `state/brain/jobs/L-*.json` (`tb.brainjob/1`: `id, kind="lifecycle", role, ts, deadline_ts, meta, payload`; префикс `L-` нужен, чтобы не пересекаться с заданиями диспетчера `J-*.json`), ответ раннера `state/brain/jobs/L-*.result.json` `{"id","ok","output","err"}` (запись атомарная). Готовность: `control.brain_iface.probe`.
  Раннеру C2/C3 достаточно подобрать `kind=lifecycle`, вызвать `Runner.ask_role(role, text)` (уже есть) и записать результат. Входы: `payload.kind` = `morning` (brief, facts, owner_choice, caps, symbols, carried_in) или `audit` (items, rows, policy, facts, dayplan). Тексты третьих лиц во входах невозможны (только структурные поля).
- Исполнитель (`ExecPort`): `snapshot, health, plans, send(op, plan_id, reason, payload, now, tag), result(cmd_id)`; `FileExecPort` пишет в `state/plans/_inbox` через `PlanStore.inbox_put` (актор `system`).
- Тесты подменяют порты через `tb.lifecycle.runtime.configure(clock, sleep, brain, execport, sync)` (`tests/fakes/fake_lifecycle.py`, стенд `tests/_lc.py`).
