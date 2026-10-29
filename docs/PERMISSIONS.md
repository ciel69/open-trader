# Разрешения для ./tb (готовые правила для настроек Claude Code)

Файлы настроек я не менял. Правила нужно добавить самому в `permissions` одного из файлов:
`~/.claude/settings.json` (все проекты), `<проект>/.claude/settings.local.json` (только этот проект, не в git) или `<проект>/.claude/settings.json`.

## Синтаксис (по документации Claude Code, проверь у себя командой `/permissions`)
- Правило Bash: `Bash(<шаблон>)`. Шаблон сравнивается с текстом команды как набрано, поэтому вызов всегда должен быть
  в одной и той же форме `./tb ...` (не `tb`, не абсолютный путь, не `cd ... && ./tb`).
- `*` внутри шаблона заменяет любой текст. Пробел перед `*` (`./tb guard *`) означает границу слова: совпадёт `./tb guard --sym ETH`,
  но не `./tb guardian`. Старая форма с двоеточием (`Bash(./tb guard:*)`) устарела, но может ещё работать в старых версиях.
- Составные команды (`&&`, `;`, `|`) Claude Code разбирает по частям; правило на `./tb guard *` не разрешает `./tb guard ... ; rm -rf x`.
  Поэтому вызывать `./tb` нужно одной простой командой.
- Приоритет: `deny` > `ask` > `allow`.

НЕ УВЕРЕН: если карточки подтверждения появляются от инструмента вкладки терминала приложения (`run_in_terminal`), а не от инструмента `Bash`,
правила `Bash(...)` на них могут не действовать (правила для MCP-инструментов задаются по имени инструмента, без маски на аргументы).
Проверка: добавь правило, запусти `./tb journal review` тем же способом. Если карточка осталась: (а) в самой карточке выбери «всегда разрешать» для этой команды, если такая кнопка есть;
(б) либо проси запускать через инструмент Bash, на который эти правила действуют.

## Чтение и безопасные команды (можно разрешить без вопросов)
Не меняют деньги и состояние (или меняют только локальные расчёты):
```json
"allow": [
  "Bash(./tb test)",
  "Bash(./tb help)",
  "Bash(./tb bybit read balance)",
  "Bash(./tb bybit read positions)",
  "Bash(./tb bybit read info *)",
  "Bash(./tb guard *)",
  "Bash(./tb journal review *)",
  "Bash(./tb journal review)",
  "Bash(./tb journal list)",
  "Bash(./tb portfolio show)",
  "Bash(./tb session status)",
  "Bash(./tb research *)",
  "Bash(./tb feed selftest)",
  "Bash(./tb context --selftest)"
]
```
`./tb guard *` только считает и печатает JSON, ничего не пишет и не торгует. `./tb research *` читает локальные CSV.

## Торговля (разрешай осознанно, это реальные деньги)
Ключ `trade` = mainnet-ключ «Orders & Positions» без вывода. Узкие правила вместо `./tb bybit trade *`:
```json
"allow": [
  "Bash(./tb bybit trade open * --dry-run)",
  "Bash(./tb bybit trade open *)",
  "Bash(./tb bybit trade close *)",
  "Bash(./tb bybit trade pending)",
  "Bash(./tb bybit trade cancel *)",
  "Bash(./tb bybit trade sync)",
  "Bash(./tb bybit trade positions)",
  "Bash(./tb bybit trade balance)",
  "Bash(./tb bybit testnet *)"
]
```
Если хочешь, чтобы каждый реальный вход подтверждал ты, оставь `open`/`close` для mainnet в `ask`, а в `allow` только `--dry-run`, `positions`, `balance`.
Реальный ордер на mainnet в любом случае требует флага `--mainnet-confirm` в самой команде, а размер и плечо даёт guard.
`pending` только читает локальный файл и биржу, `cancel` снимает ждущие лимиты (снижает риск, `--mainnet-confirm` не нужен), `sync` заодно заносит заполненные лимиты в журнал.
`open --limit-price ...` попадает под `open *` (ждущий лимит на mainnet тоже требует `--mainnet-confirm`).
Правила `open *`/`close *` пускают любые аргументы (символ, стоп, тейк): ограничивают риск лимиты `config/risk_profile.json`, а не маска.

## Спрашивать каждый раз (ask)
Меняют капитал, риск-бюджет или тянут из сети:
```json
"ask": [
  "Bash(./tb session start *)",
  "Bash(./tb session extend *)",
  "Bash(./tb session stop *)",
  "Bash(./tb portfolio set *)",
  "Bash(./tb portfolio deposit *)",
  "Bash(./tb journal open *)",
  "Bash(./tb journal close *)",
  "Bash(./tb history *)",
  "Bash(./tb funding *)",
  "Bash(./tb feed start *)",
  "Bash(./tb watch *)"
]
```
Если `session start` и запись в журнал входят в твоё постоянное разрешение на торговлю в сессии, перенеси `session *` и `journal open/close` в `allow`.

## Чего НЕ разрешать
- `Bash(./tb *)`, `Bash(./tb bybit *)`, `Bash(./tb bybit trade *)` одной широкой маской: накроют `deposit`, смену профиля, будущие команды.
- Любые маски на `*` без префикса `./tb`, `Bash(python3 *)`, `Bash(sh *)`, `Bash(env *)`, `Bash(source *)`.
- Чтение `~/.config/traider-bot/**`: ключи читает только `./tb`, агенту они не нужны. Можно запретить явно:
```json
"deny": ["Read(~/.config/traider-bot/**)", "Bash(cat ~/.config/traider-bot/*)"]
```
- `./tb tick *` (старый движок, живой торговлей не используется).
