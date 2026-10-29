# Рыночный фид: демоны вместо ручных запросов

Проблема: каждый мой ручной запрос к API — это секунды задержки и токены. Решение: отдельные процессы
держат постоянные WebSocket-соединения, сами считают признаки и пишут результат в файлы. Агент (Claude)
читает готовый файл (мс) или получает событие через Monitor (секунды), но НЕ ходит в API.

Исполнение сделок остаётся только через MCP кошелька (его вызывает агент). Демоны ничего не торгуют.
Защитные стоп/TP стоят на стороне платформы (auto_close), поэтому низкая задержка нужна на входах, а не на выходах.

Только stdlib, Python 3.9 (без websockets/aiohttp/requests — их нет и ставить нельзя). Всё, что связано с сетью,
изолировано в тонких функциях, чтобы тесты шли на локальных фикстурах и локальном mock-сервере без интернета.
Адреса и форматы эндпоинтов написаны по памяти и НЕ проверены вживую: держать их в `config/feed_config.json`,
парсить защитно (неизвестное сообщение = пропуск + счётчик ошибок, не падение), форматы сверять с документацией
в комментариях, а в итоговом отчёте перечислить, что осталось непроверенным.

## Файлы
```
config/feed_config.json     адреса, символы, пороги событий, пути (единственный источник правды для фида)
marketdata/wsclient.py          минимальный WebSocket-клиент (RFC 6455) на asyncio+ssl: connect, ping/pong, reconnect с backoff, text frames   [Агент C]
marketdata/feed.py              демон: подписки, расчёт признаков, запись runtime/*                                                      [Агент C]
marketdata/features.py          чистые функции признаков (EMA, доходности, дисбаланс стакана, CVD, реализ. волатильность)                 [Агент C]
marketdata/history.py           REST-загрузчик истории свечей -> data/ticks_<SYM>.csv (формат ts_iso,price) + data/candles_<SYM>.csv        [Агент D]
marketdata/context.py           опрос медленных данных раз в N секунд -> runtime/context.json                                              [Агент D]
runtime/market.json  атомарно перезаписывается ~1 раз/с
runtime/events.jsonl дописываются события (по одной JSON-строке)
runtime/context.json атомарно перезаписывается
runtime/health.json  состояние каждого источника: connected, last_msg_age_s, reconnects, errors
```

## runtime/market.json (пишет marketdata/feed.py)
```
{"ts": "2026-10-27T06:30:00Z", "ts_epoch": 1793..., "feed_ok": true,
 "symbols": {"ETH": {
    "price": 2307.9, "src": "okx", "age_s": 0.4,
    "prices_by_src": {"okx": 2307.9, "bybit": 2308.1},
    "ret_1m": 0.0004, "ret_5m": -0.0012, "ret_15m": 0.003,          # доли, не проценты
    "rv_5m": 0.0009, "rv_1h": 0.0021,                               # реализованная волатильность (доля)
    "book": {"spread_bps": 0.4, "imbalance": 0.18},                 # imbalance в [-1, 1], + = больше bid
    "cvd_1m": 120.5, "cvd_5m": -300.0,                              # дельта агрессивных покупок/продаж (в базовом активе)
    "funding_hourly": 0.0000880, "oi": 123456.0, "oi_chg_5m": 0.002,
    "ema_fast": 2306.1, "ema_slow": 2301.4, "regime": "trend_up|trend_down|range|storm|unknown"}}}
```
Правила: если источник молчит дольше `stale_seconds` — его цена не попадает в `prices_by_src`; если живых источников
нет — `feed_ok=false`. `regime` строго по чистой функции в marketdata/features.py (storm = rv_5m выше порога; trend = |ema_fast-ema_slow|/atr
и согласованный ret_15m; иначе range).

## runtime/events.jsonl (пишет marketdata/feed.py, читает агент через Monitor)
Одна строка = одно событие, без многострочного вывода: `{"ts":..., "type":..., "symbol":..., "data":{...}}`.
Типы: `regime_change`, `breakout` (цена вышла за N-минутный диапазон на объёме), `imbalance_spike`, `vol_spike`,
`src_divergence` (расхождение источников > `divergence_bps`), `feed_stale`, `feed_recovered`.
Антиспам: не чаще одного события одного типа на символ за `event_cooldown_s`. События — триггер «посмотри сейчас»,
а не торговый приказ.

## runtime/context.json (пишет marketdata/context.py)
Опрос REST раз в 30–300 с по источнику: индекс волатильности DVOL (Deribit), Fear & Greed (alternative.me, раз в час),
funding/open interest/long-short ratio с биржи, доминация BTC (CoinGecko, раз в 5 мин), список макро-событий из локального
`config/macro_events.json` (ручной календарь, формат `[{"ts_iso":..., "name":..., "impact":"high"}]`; окно блокировки входов
`macro_block_minutes` до и после). Каждое поле с собственным `ts` и `ok`. Недоступный источник = `ok:false`, а не падение.

## marketdata/history.py
`./tb history --sym ETH --days 30 --out data/` скачивает минутные свечи (OKX `history-candles` как основной,
Bybit `kline` как запасной), страницами с паузой по rate limit, возобновляемо (докачка с последней метки), дедупликация,
сортировка. Пишет `data/candles_<SYM>.csv` (ts_iso,o,h,l,c,volume) и `data/ticks_<SYM>_hist.csv` (ts_iso,price=close),
которые читает `data.load_ticks`. Существующий `data/ticks_<SYM>.csv` (живой сбор через MCP) НЕ перезаписывать.

## Запуск (на машине пользователя, не в песочнице)
`./tb feed start` (демон, Ctrl+C = корректное завершение, перезапись health.json), `./tb context`.
Оба с флагами `--root`, `--config`, `--once` (один цикл для отладки), `--selftest` (прогон на встроенных фикстурах без сети).
