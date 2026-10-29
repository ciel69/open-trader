# Автономный трейдер (Wallet Perps) — спецификация

Исполнитель — агент Claude. Он единственный, кто может вызывать MCP-инструменты кошелька
(`perps_latest_rate`, `perps_list_positions`, `perps_prepare_order_place`, `perps_execute_operation` и т.д.).
Скрипты в этом проекте НЕ ходят в сеть и НЕ торгуют сами: они принимают снимок рынка/счёта,
считают решение и печатают JSON. Агент выполняет решение через MCP и пишет результат обратно.

Реальность, под которую проектируем: цикл «тик» раз в ~60 с (не HFT, а внутридневная торговля
на минутных барах). Издержки: taker 0.09% на вход и на выход (≈0.18% круг), проскальзывание до 1%
в худшем случае, funding ≈0.009%/час (положительный: лонги платят шортам). Капитал ~30 USDT.
Python 3.9, только stdlib (+numpy/pandas опционально). Никаких `X | Y` в аннотациях, никакого `match`.

## Структура (пакет src/tb/; пути в src/tb/paths.py)
```
config/config.json        параметры стратегии и риска (единственный источник правды)
marketdata/data.py            хранение тиков, ресемплинг в бары, генератор синтетики для тестов
legacy_engine/signals.py         compute_signal(...)            [Агент A]
research/backtest.py        симулятор + CLI                [Агент A]
legacy_engine/risk.py            риск-менеджер                  [Агент B]
legacy_engine/state.py           state/state.json + state/journal.jsonl     [Агент B]
legacy_engine/tick.py            CLI: один тик -> решения       [Агент B]
tests/             unittest: `./tb test` или `python3 -m unittest discover -s tests` из корня
data/ticks_<SYM>.csv   ts_iso,price   (дописывает legacy_engine/tick.py)
state/state.json, state/journal.jsonl
(модули относительно src/tb/: marketdata/, legacy_engine/, research/)
```

## Контракты

### marketdata/data.py (владелец: Агент A)
- `append_tick(path, ts_iso, price)`; `load_ticks(path, max_rows=None) -> list[(ts_epoch:float, price:float)]`
- `to_bars(ticks, bar_seconds) -> list[dict(ts, o, h, l, c)]`
- `synthetic_ticks(n, seed, regime="mixed") -> list[(ts_epoch, price)]` (тренд/боковик/шоки, шаг 60 с)

### legacy_engine/signals.py (владелец: Агент A)
```
compute_signal(bars: list[dict], funding_hourly: float, cfg: dict) -> dict
# bars: закрытые бары из data.to_bars, старые -> новые
# возвращает:
# {"action": "long"|"short"|"flat",
#  "score": float 0..1, "stop_pct": float, "tp_pct": float,   # проценты цены, напр. 1.2
#  "atr_pct": float, "reasons": [str], "warmup": bool}
```
Правила: при нехватке баров `warmup=True, action="flat"`. Вход только если ожидаемое движение
(tp_pct) ≥ `cfg["strategy"]["min_edge_mult"]` × (круговая комиссия + буфер проскальзывания + funding за hold).
Стратегия — трендовая с фильтром: EMA fast/slow + подтверждение моментумом + фильтр волатильности (ATR) и
фильтр боковика; без мартингейла и усреднения. Детерминирована (без случайности).

### legacy_engine/risk.py (владелец: Агент B)
```
check_open(state: dict, snapshot: dict, proposal: dict, cfg: dict) -> {"ok": bool, "reasons": [str], "margin": float}
manage_positions(state: dict, snapshot: dict, cfg: dict) -> list[decision]   # закрытия: max_hold, время сессии, смещение стопа, дрейф TP/SL
kill_switch(state: dict, snapshot: dict, cfg: dict) -> {"halt": bool, "reason": str}
```
Обязательные правила (см. config/config.json): лимит маржи на сделку, макс. одновременных позиций, резерв свободных
средств, дневной лимит убытка (по equity от старта дня) -> halt, макс. сделок в день, cooldown после стопа на
контракте, запрет усреднения, дистанция до ликвидации, устаревшие данные (> `stale_seconds`) -> не открывать,
минимальная маржа контракта, чтобы не нарушить min_size_quote. Все пороги — из cfg, не хардкод.

### legacy_engine/state.py (владелец: Агент B)
`load_state(path)`, `save_state(path, st)` (атомарная запись), `journal(path, event: dict)` (jsonl, добавляет ts),
`new_day_reset(st, now, equity)`. Поля state: `session_active`, `day`, `day_start_equity`, `realized_pnl_today`,
`trades_today`, `last_stop_ts` по контракту, `halted`, `halt_reason`, `last_tick_ts`.

### legacy_engine/tick.py (владелец: Агент B)
Вход: JSON из stdin:
```
{"ts": "2026-10-27T06:30:00Z",
 "prices": {"ETH": 2313.67, "BTC": 79960.4},
 "funding_hourly": {"ETH": 0.000088, "BTC": 0.000096},
 "balance_usdt": 20.35,
 "positions": [ <элементы perps_list_positions как есть> ],
 "contracts": {"ETH": {"contract_id":0,"min_size_quote":"1.0","max_leverage":50,"taker_fee_pct":"0.0009"}, ...}}
```
Действия: дописать тики в data/, посчитать бары и сигналы, прогнать kill_switch/manage_positions/check_open,
сохранить state/journal. Вывод — JSON:
```
{"halt": bool, "halt_reason": str,
 "decisions": [
   {"op":"open","symbol":"ETH","contract_id":0,"side":"long|short","margin":8.0,"leverage":3,
    "ref_price":2313.67,"stop_price":..., "tp_price":..., "why":[...]},
   {"op":"close","symbol":"ETH","contract_id":0,"why":[...]},
   {"op":"set_auto_close","symbol":"ETH","contract_id":0,"stop_price":..., "tp_price":..., "why":[...]},
   {"op":"hold","why":[...]}],
 "diagnostics": {...}}
```
Доп. режимы: `legacy_engine/tick.py --start` (session_active=true, сбрасывает дневные счётчики, сохраняет day_start_equity из stdin),
`legacy_engine/tick.py --stop` (session_active=false; вывод `{"flatten": true}` — агент закрывает всё), `legacy_engine/tick.py --report`
(итог дня: PnL, число сделок, причины входов/выходов из journal).
Позиции, уже открытые вручную (без записи в journal), принимаются под управление: им применяются стоп/TP/max_hold/kill_switch.
Если `session_active=false` — тик не открывает новых позиций, только управляет существующими.

### Цикл агента (не код, для справки)
Каждые ~60 с: `perps_latest_rate` по символам -> `perps_list_positions` -> `balance_get` -> `legacy_engine/tick.py` ->
для каждого решения `perps_prepare_order_place`/`perps_prepare_position_close`/`perps_prepare_auto_close_set` ->
`perps_execute_operation` сразу после успешного превью (токен живёт ~90 с) -> сверить `perps_list_positions`.
Никогда не повторять неудачный execute вслепую.
