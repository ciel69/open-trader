# WP13: источники фактуры и новости

Статус на 2026-10-28: **ни один источник не проверен вживую** (`verified:false`). Разработка шла на синтетических фикстурах `tests/fixtures_intel/`, построенных по известным
форматам; сетевых запросов к публичным сайтам не делалось. Проверка выполняется владельцем: в Terminal.app (там сеть открыта)

```
./tb intel verify                 # по одному GET на источник, таблица: HTTP, мс, КБ, поля парсера; заголовки НЕ печатаются
./tb intel verify --source fng    # один источник или семейство (oi, ls, dvol); oi_SOL = конкретный символ
./tb intel verify --write         # то же + раздел с результатами в конец этого файла
./tb intel verify --proxy         # если сеть доступна только через системный прокси (по умолчанию как у сервиса: без прокси)
```

Код выхода 0, если все проверенные источники OK. Источник со статусом OK владелец помечает в `config/intel.json`: `"sources": {"fng": {"verified": true}}`. Источник с ПРОБЛЕМА:
`{"enabled": false}` (источник отключается флагом, а не падением) или поправить `url`; формат парсера в `src/tb/intel/sources.py`.

## Реестр (код: `src/tb/intel/sources.py`, переопределения: `config/intel.json: sources.<id>`)
| id | URL | формат | период | что берём | подтверждение |
|---|---|---|---|---|---|
| bybit_ann | `https://api.bybit.com/v5/announcements/index?locale=en-US&limit=20` | JSON V5 | 5 мин | тип из `type.key` (new_crypto, delistings, maintenance_updates, прочее), символы по шаблонам `XXXUSDT`/`XXX/USDT`, окно обслуживания; текст -> items | по памяти, ПРОВЕРИТЬ |
| bybit_time | `https://api.bybit.com/v5/market/time` | JSON | 5 мин | доступность REST, дрейф часов | клиент проекта |
| bybit_tickers | `https://api.bybit.com/v5/market/tickers?category=linear` | JSON | 5 мин | funding по нужным символам | context.py |
| oi_<SYM> | `.../v5/market/open-interest?category=linear&symbol=<SYM>USDT&intervalTime=1h&limit=25` | JSON | 5 мин | изменение OI за 24 ч | по памяти, ПРОВЕРИТЬ |
| ls_<SYM> | `.../v5/market/account-ratio?category=linear&symbol=<SYM>USDT&period=1h&limit=1` | JSON | 5 мин | long/short | context.py |
| fng | `https://api.alternative.me/fng/?limit=2&format=json` | JSON | 60 мин | значение 0-100, вчера; класс считается по числу | context.py |
| dvol_BTC, dvol_ETH | `https://www.deribit.com/api/v2/public/get_volatility_index_data` (currency, resolution=3600, start/end ms) | JSON-RPC | 30 мин | последний close | context.py |
| btc_dom | `https://api.coingecko.com/api/v3/global` | JSON | 30 мин | `data.market_cap_percentage.btc`; изменение за 24 ч по своей истории | context.py |
| rss_coindesk | `https://www.coindesk.com/arc/outboundfeeds/rss/` | RSS 2.0 | 10 мин | заголовки (только items.jsonl) | по памяти, ПРОВЕРИТЬ |
| rss_cointelegraph | `https://cointelegraph.com/rss` | RSS | 10 мин | то же | по памяти, ПРОВЕРИТЬ |
| rss_theblock | `https://www.theblock.co/rss.xml` | RSS | 10 мин | то же | по памяти, ПРОВЕРИТЬ |
| rss_decrypt | `https://decrypt.co/feed` | RSS/Atom | 15 мин | то же | по памяти, ПРОВЕРИТЬ |
| cal_bls_ics | `https://www.bls.gov/schedule/news_release/bls.ics` | ICS | сутки | CPI, NFP (PCE, GDP) | по памяти, ПРОВЕРИТЬ (сайт может отдавать 403 ботам) |
| cal_fomc_html | `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` | HTML | сутки | даты заседаний (регулярки по классам `fomc-meeting__month/date`) | по памяти, ПРОВЕРИТЬ, хрупко |
| calendar, us_market | локально (`config/macro_events.json`, `config/intel.json`) | | 60 с | события и расписание NYSE | без сети |

Лимиты: 2 с между запросами к одному домену, не более 280 запросов в час, параллелизм <= 4, ответ <= 1 МБ, `User-Agent: traider-bot/2`.

## Календарь: даты-кандидаты (НЕ ПРОВЕРЕНЫ, из памяти автора; ПРОВЕРИТЬ ПО ОФИЦИАЛЬНОМУ КАЛЕНДАРЮ)
| событие | дата (ET) | UTC | где проверить |
|---|---|---|---|
| FOMC, решение | 2026-10-28 14:00 (заседание 27-28 окт) | 18:00 (EDT) | federalreserve.gov/monetarypolicy/fomccalendars.htm |
| NFP (октябрь) | 2026-11-06 08:30 | 13:30 (EST) | bls.gov/schedule/news_release/empsit.htm |
| CPI (октябрь) | 2026-11-10 08:30 (самая неуверенная дата) | 13:30 | bls.gov/schedule/news_release/cpi.htm |
| NFP (ноябрь) | 2026-12-04 08:30 | 13:30 | то же |
| FOMC, решение | 2026-12-09 14:00 (заседание 8-9 дек) | 19:00 (EST) | то же |
| CPI (ноябрь) | 2026-12-10 08:30 | 13:30 | то же |
Перевод летнего времени США в 2026: 8 марта и 1 ноября (Восточное время: EDT до 1 ноября, потом EST); код считает это сам (`calendar.et_to_utc`), без tzdata.
Подтверждённые даты вносятся в `config/macro_events.json` (`[{"id","name","type":"FOMC|CPI|NFP|PCE|GDP|OTHER","ts_utc","impact":1-3}]`): такие события получают `v:true` и перекрывают кандидатов.
Праздники NYSE (`config/intel.json: us_holidays`, `us_half_days`) тоже по памяти и требуют проверки владельцем (nyse.com/markets/hours-calendars).
Запись FOMC в `config/watch_config.json` (16:00-19:00 UTC 28 окт, «НЕ ПРОВЕРЕНА») не менялась: она совпадает с кандидатом (решение 18:00 UTC), но подтверждения от источника нет.

## Результаты проверки
(пока пусто: запустите `./tb intel verify --write`; раздел дополняется автоматически)
