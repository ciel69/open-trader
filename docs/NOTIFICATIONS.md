# Уведомления ОС (WP-N)

Короткие уведомления на экран (macOS: `osascript`, Windows: PowerShell) о важном: итог дня, аварийная остановка, сбой сервиса и т. д. Всё **выключено по умолчанию**.
Владелец включает без правки конфига: вкладка «Уведомления» дашборда или `./tb notify`. Текст берётся только из шаблонов с числами и идентификаторами (чужого текста нет, кавычки и управляющие символы вырезаются).

## Как включить
- Дашборд: вкладка «Уведомления» (нужен вход на вкладке «Управление»; подтверждение фразами не требуется). Главный выключатель, категории, тихие часы, антиспам, «Сохранить», «Проверить».
- Терминал: `./tb notify status | on | off | set КАТЕГОРИЯ on|off | set quiet HH:MM-HH:MM|off | set bypass КАТЕГОРИЯ on|off | set limit per_hour|same_kind_min N | test`.
- «Проверить» / `./tb notify test` показывает ОДНО настоящее уведомление ОС (только по явной команде; мастер и категории для проверки не нужны).
- Если уведомление не появилось: системные настройки уведомлений (macOS: приложение «Редактор скриптов» должно быть разрешено, режим «Не беспокоить»; Windows: уведомления от PowerShell, «Фокусировка»). Наличие прав ОС программа не определяет.

## Приоритет настроек
1. Файл `state/control/notify.json` (создаётся при первом изменении; правят только ctl и CLI; подпись не нужна, запись атомарная).
2. Нет файла: действует `config/lifecycle.json: notify.os` (по умолчанию `false`) по-старому: только итог дня и старт дня напрямую из WP14.
Когда файл есть, уведомления рассылает подписчик шины, прямой путь WP14 молчит (дублей нет). Битый файл = всё выключено (+ предупреждение).

## Формат `state/control/notify.json`
```
{"version":1, "master":false,
 "categories":{"day_start":false,"day_report":false,"emergency":false,"service_down":false,"live_gate":false,
               "risk_level_changed":false,"brain_swap":false,"position_closed":false,"day_close_failed":false,"macro_blackout_soon":false},
 "quiet":{"enabled":false,"from":"23:00","to":"07:00","tz":"local","bypass":["emergency","service_down","day_close_failed"]},
 "limits":{"per_hour":6,"same_kind_min":10}, "updated":1793184685.2, "by":"cli|dash"}
```

## Категории (все по умолчанию выключены)
| Категория | Событие шины | Текст (шаблон) |
|---|---|---|
| `day_start` | `day_report kind=start` | День D запущен, перенесено N, вопросов M |
| `day_report` | `day_report kind=stop` | Итог дня D: ±X USDT, перенесено, вопросов, оценка |
| `emergency` (критичная) | `day_report kind=emergency`, `kill state=on` | Аварийная остановка дня D, итог; KILL включён |
| `service_down` (критичная) | `service_down`, `exec_down`, `connectivity down >= 20 с`, `reconcile_alert` | Сервис S не поднимается: попыток, код |
| `live_gate` | `control_cmd day.start mode=live` | Запущен live / запуск live отклонён: код |
| `risk_level_changed` | `risk_level_changed` | Уровень риска a -> b (кто) |
| `brain_swap` | `brain_swap`, `brain_fallback` | Мозг: вид from -> to, ревизия. Тип `brain_swap` попадёт в шину, когда его добавят в `core/bus.py` (до тех пор категория молчит) |
| `position_closed` | `position_closed reason=tp|sl|liquidation` | ETH закрыта по стопу: ±X USDT (±R) |
| `day_close_failed` (критичная) | `error where=lifecycle code=DAY_CLOSE_FAILED` | Закрытие дня не подтверждено исполнителем |
| `macro_blackout_soon` | `macro_blackout_soon` | Через N мин макро-событие KIND |

## Тихие часы и антиспам
- Тихие часы `from`-`to` (`local` = местное время машины, `utc`); диапазон может переходить через полночь (23:00-07:00); `from == to` = тишины нет. В это время события копятся (до 100, хранятся сутки) и после окончания выходят одним сводным уведомлением. Категории из `quiet.bypass` (по умолчанию `emergency`, `service_down`, `day_close_failed`) показываются сразу.
- Не больше `per_hour` (6) показов в час на категорию, лишнее считается в статистике, но не показывается.
- Один вид (категория + объект: сервис, план, день) не чаще раза в `same_kind_min` (10) минут; пропущенные повторы дописываются к следующему показу («+N повторов»).
- События старше 15 минут (ctl не работал) не показываются; история до первого запуска подписчика не пересылается.

## Устройство
- Источник: поток `tb-notify` внутри сервиса `ctl` (`tb.lifecycle.notifyd.NotifyThread`), отдельного процесса нет. Читает шину `state/bus`, курсор и счётчики в `state/control/notify_state.json`. Показ через `tb.lifecycle.notify.send_os(force=True)`; без shell, на Windows текст через переменную окружения.
- Действия плоскости управления `notify.set` (изменение настроек) и `notify.test` (проверка): не опасные, nonce и фразы не нужны, нужна сессия владельца; каждое пишется в `state/control/audit.jsonl` (поле `note` = список изменений). Правки из CLI тоже в аудите (`src=cli`).
- Дашборд читает `GET /api/notify/status`; пишет только через ctl.
- В режиме unittest реальные уведомления заблокированы (`send_os` возвращает `(False, "test")`); тесты `tests/test_notify.py` подставляют `sender`/`runner`.
