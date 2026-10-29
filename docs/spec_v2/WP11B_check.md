# WP11-B: проверка платформы на paper (2026-10-28)

Стенд: отдельный корень `TB_ROOT` в scratchpad (копия `config/`, пустые `state/` и `runtime/`), профиль `paper`, реальные ПУБЛИЧНЫЕ данные Bybit mainnet (WebSocket фида исполнителя и REST сканера),
ключи не использовались, `trade` и `./tb bybit trade` не запускались. В `config/platform.json` стенда: интервал скана 5 с, `range_pct` 0.05, `wake_score` 0 (чтобы событие появилось за минуты); реальный конфиг проекта не менялся.

## Прогон 1: `./tb up --profile paper --services exec,scan,dash,dispatch --max-seconds 330` (без sandbox)
- Все четыре сервиса подняты (pid-файлы, heartbeat); `./tb status`: зелёный, исполнитель жив (режим paper), шина, `hot.json` свежий, дашборд `http://127.0.0.1:8791/`.
- Автоперезапуск: `kill -9` процесса сканера -> `упал (exit_-9), перезапуск через 2.0 с (попытка 1/5)` -> через 2 с снова `running`, `рст=1`, последняя ошибка `упал, код -9`.
- По `--max-seconds` платформа сама остановилась: порядок dash, dispatch, scan, exec; коды 0 (dash -15 от SIGTERM); осиротевших процессов нет (`ps`).
- События шины: `exec_started`, `connectivity`, `exec_stopped` попали в очередь `chat` (диспетчер, `./tb dispatch queue chat`). Сканер событий не дал: после перезапуска окна 5 мин (нужно >= 75% окна) не успели накопиться (ожидаемо, не дефект).

## Прогон 2: `./tb up ... --detach --max-seconds 420`
- Фоновый запуск вернул управление (`платформа в фоне, pid ...`); повторный `./tb up` на том же корне: `вторая копия отказана`, код 4.
- Через ~4 минуты сканер опубликовал в шину: `seq 6 scanner_signal pri=wake sym=LINK d={kind:range_5m, px, score, dir, range_5m_pct, ret_5m_pct, turn_surge_5m, spread_bps, funding_pct}` (только поля белого списка, без reasons/note).
  Диспетчер доставил его в очередь `chat` (журнал: `res=ok cls=3`), `./tb status --line`: `TB paper OK dash:ok dispatch:ok exec:ok scan:ok chat:6!`.
- `/api/platform` дашборда: HTTP 200, тот же словарь, что `./tb status --json`.
- `./tb down`: «платформа остановлена»; после него `ps` без процессов tb.launcher / tb.platform, порт 8791 свободен, `./tb status`: «ВЫКЛЮЧЕНА» (код 1).

## Тесты
`./tb test`: 1797 тестов зелёные (в т.ч. 90 новых `test_platform_{procs,supervisor,dispatcher,scansvc}.py`). Тесты работают во временных корнях, без сети и живых демонов проекта.
Реальные `state/`, `config/risk_profile.json`, ключи не затрагивались.

## Не проверено
Windows (ветки CREATE_NEW_PROCESS_GROUP / CTRL_BREAK_EVENT / taskkill проверены только юнит-тестами параметров), launchd/Планировщик (только сгенерированный текст, plist разобран `plistlib`),
профили testnet и trade (в живую не запускались; trade проверен только отказами и `--dry-run`).
