# SPEC V2 / 06. Контракты между пакетами (интерфейсы и форматы файлов)

Часть `docs/SPEC_V2.md`. Этот файл ЗАМОРАЖИВАЕТСЯ после WP0: все пакеты работ пишут против него. Изменение контракта делает только ведущий (архитектор) и сообщает владельцам затронутых пакетов. Python 3.9, только stdlib, никаких `X | Y` в аннотациях и `match`. Сигнатуры даны как контракт поведения; детали реализации на усмотрение владельца. Стиль как у соседнего кода (комментарии и docstring по-русски, `root=` в публичных функциях хранилищ, `paths.*` для путей, `fsutil.replace` для атомарной замены, `open(..., encoding="utf-8")`).

## 6.1 Дерево новых файлов и владельцы (непересекающиеся множества)
```
src/tb/core/     plan_schema.py linkid.py                     WP2
                 filelock.py plan_store.py decisions.py exec_cfg.py   WP4
                 bus.py                                       WP6
                 expstats.py                                  WP7
                 guard.py journal.py (правки)                 WP9
src/tb/logic/    __init__.py candles.py conds.py rules.py pyramid.py levels.py     WP3
                 paper.py                                     WP7
src/tb/venues/   bybit_exec.py bybit_wsx.py                   WP1 (+ bybit.py правит ТОЛЬКО WP1)
src/tb/executor/ __init__.py ports.py core.py gate.py actions.py reconcile.py protect.py daemon.py scanbridge.py mdsvc.py main.py   WP5
                 live_port.py                                 WP1
                 paper_port.py                                WP7
src/tb/brain/    __init__.py common.py plan_cli.py brief.py events_cli.py     WP6
                 exp_cli.py                                   WP7
                 api_daemon.py                                WP11 (позже)
src/tb/dash/     весь каталог (model.py, model2.py, server.py, static/*)      WP8
src/tb/__main__.py src/tb/launcher.py config/exec_limits.json config/brief_checklist.json tests/test_layering.py   WP0
docs/*, tests/test_e2e_*.py                                   WP10
tests/fakes/     __init__.py fake_port.py fake_feed.py clock.py              WP5
```
Каждый WP создаёт и свои тесты `tests/test_<модуль>*.py`. Прежние тесты не переписываются; правки прежних тестовых файлов только владельцем соответствующего прежнего модуля (WP9 для guard/journal, WP8 для dash, WP1 для bybit).

## 6.2 `executor/ports.py`: VenuePort (WP5 владеет, WP0 создаёт заготовку)
```python
class PortError(Exception):
    kind = "rejected|unknown|rate|position_gone|network"    # атрибут экземпляра
    code = None            # retCode Bybit, если есть
class VenuePort(object):
    env = "mainnet|testnet|paper"
    # --- чтение (REST; ничего не меняют)
    def account(self): ...                       # -> {"equity":float,"free":float,"ts":epoch}
    def positions(self): ...                     # -> [{"sym","side":"long|short","size":float,"avg":float,"mark":float,"sl":float,"tp":float,"liq":float,"upnl":float,"lev":float,"margin":float,"ts":epoch}]  sl/tp=0.0 если нет
    def open_orders(self, sym=None): ...         # -> [{"sym","link_id","order_id","side":"long|short|buy|sell"?,"type":"limit|market","price":float,"qty":float,"filled":float,"reduce_only":bool,"status","ts"}]  (side: "Buy"/"Sell" как биржа)
    def order_by_link(self, sym, link_id): ...   # realtime, затем history; -> тот же словарь или None
    def executions(self, since_ms, sym=None): ...# -> [{"exec_id","sym","side","price","qty","fee","is_maker":bool,"link_id","order_id","stop_type":"|StopLoss|TakeProfit|...","exec_type","closed_size":float,"pnl":float,"ts_ms":int}] по возрастанию времени
    def closed_pnl(self, sym, since_ms): ...     # -> [{"sym","side","qty","entry","exit","pnl","open_fee","close_fee","ts_ms"}]
    def instrument(self, sym): ...               # -> {"tick":str,"qty_step":str,"min_qty":str,"min_notional":str,"max_leverage":float} (кэш 1 ч)
    def quote(self, sym): ...                    # -> (bid, ask, last)
    def klines(self, sym, tf_min, limit=200): ...# -> [(start_ms,o,h,l,c,vol,turnover)] по возрастанию времени, последняя может быть незакрытой (confirm определяется по времени)
    # --- запись (каждая операция ЛОГИРУЕТСЯ вызывающим через WAL; сама ничего не повторяет)
    def set_leverage(self, sym, lev): ...        # 110043 "not modified" -> успех
    def place_entry(self, sym, side, qty, link_id, order_type, price=None, post_only=True, sl=None, sl_trigger="last"): ...
        # side "long|short"; order_type "market|limit"; sl ОБЯЗАТЕЛЕН (вход без стопа запрещён на уровне порта: PortError(rejected, code="NO_STOP")); tpslMode Full
    def place_reduce_only(self, sym, side, qty, link_id, price=None, post_only=False): ...
        # side = сторона ЗАКРЫТИЯ ("sell" для long); price=None -> market; всегда reduceOnly=True
    def amend_order(self, sym, link_id, price=None, qty=None): ...
    def cancel_order(self, sym, link_id): ...
    def cancel_all(self, sym, reduce_only_only=False): ...
    def set_stop(self, sym, side, stop_price, trigger_by="last"): ...
        # side позиции; читает ТЕКУЩИЙ стоп; если новый НЕ строже (long: new < cur, short: new > cur) -> PortError(rejected, code="STOP_NOT_TIGHTER") БЕЗ вызова биржи; снять стоп (<=0) нельзя; новый стоп обязан быть на правильной стороне от mark/last (иначе PortError(rejected,"STOP_ON_WRONG_SIDE")); -> {"ok":True,"stop":float,"unchanged":bool}
    def close_market(self, sym, side, qty, link_id): ...   # reduce-only market; qty=None -> вся позиция
```
Реализации: `executor/live_port.py::LivePort(client)` (WP1, поверх `venues/bybit_exec.py`), `executor/paper_port.py::PaperPort` (WP7), `tests/fakes/fake_port.py::FakePort` (WP5, программируемые ответы и журнал вызовов). Все порты потокобезопасны на уровне «вызывающий сериализует» (исполнитель вызывает записи из одной торговой очереди).

## 6.3 `executor/ports.py`: MarketFeed (WP1 реализует для Bybit, WP5 и WP7 используют)
```python
class MarketFeed(object):
    def subscribe(self, symbols, tfs): ...       # динамически; tfs из {1,5,15,60}
    def unsubscribe(self, symbols): ...
    # события отдаются в обработчики, регистрируемые заранее:
    on_ticker = None   # fn(sym, last, bid, ask, mark, funding, ts)
    on_kline  = None   # fn(sym, tf_min, start_ms, o, h, l, c, vol, turnover, confirm)
    on_private = None  # fn(ev) ev: {"kind":"execution"|"order"|"position", ...поля порта... с link_id}
    on_state  = None   # fn(channel "public|private", up: bool, down_s: float)
```
`venues/bybit_wsx.py::BybitFeed(client)` (asyncio, `marketdata.wsclient.WSClient`, переподключение, ping, auth как `bybit_stream`); `tests/fakes/fake_feed.py::FakeFeed` (скриптованные тики/свечи, ручной `emit`). Нормализация приватных событий в тех же полях, что `VenuePort.executions/open_orders/positions`, плюс `link_id` из `orderLinkId`.

## 6.4 `core/plan_schema.py` (WP2, чистые функции)
```python
SCHEMA = "tb.plan/1"
STATUSES, TERMINAL, TRANSITIONS          # константы (01 п. 1.6)
COND_KINDS, ACTIONS                      # словари описаний (kind -> поля, где разрешён)
def normalize(raw, now=None): ...        # -> (plan_dict, issues) приводит типы/регистр, обрезает/чистит строки, проставляет умолчания; issues: [Issue]
def validate_static(plan): ...           # -> [Issue] (структура, типы, диапазоны, словарь, E_UNKNOWN_FIELD, размеры)
def validate_context(plan, ctx): ...     # ctx: {"now","limits":exec_cfg,"session":dict|None,"instrument":dict|None,"price":float|None,"profile":dict,"active_plans":[...],"cooldowns":{...}}  -> [Issue]
def validate_amend(old_spec, new_spec, status, live): ...  # live: {"exch_stop":float|None,"position":bool,"stop_at_or_better_be":bool}; -> [Issue]
def merge_patch(spec, patch): ...        # RFC 7386
def canon_hash(plan): ...                # sha256 канонического JSON полей предрегистрации -> hex[:16]
def can_transition(cur, new): ...        # bool
def json_schema(): ...                   # dict (JSON Schema draft-07) из единого описания полей
def describe_cond(cond): ...             # человекочитаемая строка по-русски («5м закрытие выше 142.5 (n=1)»)
Issue = {"code":str,"severity":"error|warn","path":str,"msg":str}
```
Единое описание полей (таблица `FIELDS`) питает и валидатор, и `json_schema()`, и `describe_cond`: расхождение исключено структурно.

## 6.5 `core/linkid.py` (WP2)
`make(plan_n, action_n)` -> `"tbx-%d-%d"` (<= 36 символов); `parse(link)` -> `(plan_n, action_n)` или None; `is_ours(link)` -> bool (префикс `tbx-`). Не пересекается с `pending.LINK_RE`.

## 6.6 `core/filelock.py`, `core/plan_store.py`, `core/decisions.py`, `core/exec_cfg.py` (WP4)
```python
# filelock.py: обобщение pending.lock (O_EXCL, pid, устаревание 120 с, реентерабельность по процессу), не трогая pending.py
class FileLock(object):
    def __init__(self, path, wait=10.0, stale_s=120.0): ...
    def __enter__/__exit__            # TimeoutError при неуспехе
# plan_store.py
class PlanStore(object):
    def __init__(self, root=None): ...
    def list(self, status=None, mode=None): ...      # -> [{"spec":..,"rt":..}] (читает все PL-*; битые папки пропускаются и считаются)
    def load(self, plan_id): ...                     # -> (spec, rt) или (None, None)
    def create(self, spec, rt): ...                  # новая папка PL-..., revs.jsonl с rev 1; назначает plan_n из _counters.json
    def save_spec(self, spec, why): ...              # rev+1, атомарно, добавляет в revs.jsonl
    def save_rt(self, plan_id, rt): ...              # атомарная замена runtime.json
    def archive(self, plan_id): ...                  # перенос терминальных старше 7 суток в _archive/YYYY-MM/
    def counters_next(self, name): ...               # атомарный счётчик (plan_n)
    # очереди (CLI мозга пишет только inbox_put)
    def inbox_put(self, cmd): ...                    # -> cmd_id; tmp + replace; имя файла C-<ts>-<rand>.json
    def inbox_list(self): ...                        # по возрастанию имени
    def inbox_done(self, cmd_id, result): ...        # пишет _outbox/<cmd_id>.json, удаляет из _inbox
    def outbox_get(self, cmd_id): ...
    def seen_cmd(self, cmd_id): ...                  # идемпотентность (хранит последние 500)
# decisions.py
def append(rec, root=None, fsync=False): ...         # -> seq (под блокировкой; считает prev/h)
def read(after_seq=0, plan=None, limit=500, root=None): ...
def verify(root=None): ...                           # -> {"ok":bool,"n":int,"bad_seq":int|None}
# exec_cfg.py
DEFAULTS                                             # раздел 6.8
def load(root=None): ...                             # -> dict (умолчания + файл; ValueError при мусоре с понятным текстом)
def kill_state(root=None): ...                       # -> None | {"flatten":bool,"reason":str,"ts":..}   читает state/KILL каждый раз
def set_kill(flatten=False, reason="", root=None): ...   # создаёт state/KILL (атомарно); снятия нет в API (только CLI владельца exec resume)
def clear_kill(root=None): ...
```
Обе записи (`PlanStore.*`, `decisions.append`) устойчивы к параллельным процессам (блокировка), к битой последней строке (пропуск), к Windows (замена файла с повторами).

## 6.7 Формат `runtime.json` плана (исполнитель; WP4 хранит, WP5 заполняет, WP8 читает)
```
{"plan_id":"PL-1028-SOL-01","plan_n":12,"status":"managing","rev_applied":3,"hold":null,"hold_reason":null,
 "ts":{"created":..,"armed":..,"entered":..,"closed":null},
 "entry":{"state":"waiting|placing|resting|filled|blocked","link_id":"tbx-12-001","order_id":null,"attempts":0,"last_attempt":null,"blocked_why":null,
          "cond_state":{"seq_step":1,"seq_started":..,"latched":{"c0":true}},"already":{"c0":false},"ref_px":142.3,"fired_px":null},
 "pos":{"qty":0.3,"avg":145.6,"entries":[{"ts":..,"px":145.6,"qty":0.3,"fee":0.017,"link":"tbx-12-001","exec_id":"..","kind":"entry|add"}],
        "initial_stop":143.2,"cur_stop":143.2,"r_unit":2.4,"best_px":146.9,"worst_px":145.4,"t_best":..,"t_worst":..,"journal_id":"BB-M-SOL-1028-101502"},
 "targets":{"t1":{"state":"pending|resting|filled|cancelled|touch","qty":0.15,"price":148.6,"link":"tbx-12-002","filled_qty":0.0}},
 "rules":{"be_1r":{"fired":0,"last_ts":null,"state":"waiting|done|skipped"}},
 "adds":{"n":0,"risk_used":0.0},
 "realized":{"pnl":0.0,"fees":0.017,"parts":[{"ts":..,"px":..,"qty":..,"fee":..,"pnl":..}]},
 "close":{"reason":null,"px":null,"pnl":null},
 "actions":{"seq":3,"pending":["a-12-003"]},
 "counters":{"stop_moves_1h":[ts..],"blocked_attempts":0},
 "paper":false}
```
Поля, которые читает дашборд и `brief`: `status, hold, entry.state, entry.blocked_why, pos.*, targets, rules.*.state, realized, close`.

## 6.8 `config/exec_limits.json` (WP0 создаёт с умолчаниями; редактирует только владелец)
```json
{
 "version": 1,
 "watch_symbols": [],
 "pyramid": {"enabled": false, "min_r": 1.0, "max_adds": 1, "max_add_risk_frac": 1.0, "min_gap_s": 60},
 "stop_moves": {"min_interval_s": 5, "min_step_ticks": 2, "min_gap_bps_from_price": 8, "max_moves_per_hour": 30},
 "entry": {"max_price_age_s": 3, "max_chase_bps": 15, "no_confirm_max_size_mult": 0.5, "blocked_attempts_max": 3, "retry_after_s": 15,
           "fill_wait_s": 2, "max_level_distance_pct": 25},
 "protections": {"symbol_cooldown_after_stop_min": 30, "pause_after_consecutive_stops": {"n": 2, "minutes": 60, "require_confirm": true},
                 "max_entries_per_hour": 4, "max_entries_per_day": 12, "max_plans_active": 6, "max_submits_per_hour": 20,
                 "max_actions_per_symbol_per_10s": 4},
 "connectivity": {"private_ws_down_hold_s": 20, "public_price_stale_s": 10, "rest_blind_s": 60, "reconcile_every_s": 30,
                  "snapshot_every_s": 2, "heartbeat_every_s": 2, "max_symbols": 12, "max_tick_jump_pct": 15},
 "rate": {"rest_write_rps": 4, "rest_read_rps": 8},
 "equity_sync": {"auto_set_portfolio": true, "max_dev_frac": 0.10, "stale_after_s": 300},
 "paper": {"slip_bps": 2, "stop_slip_bps": 5, "fill_through_ticks": 1, "latency_ms": 300, "max_active": 20, "auto_shadow_ideas": false},
 "events": {"retention_days": 14, "unacked_warn_min": 10, "near_target_frac": 0.8, "wake_r": [1.0, -0.5]}
}
```
Правило: значения могут только СУЖАТЬ то, что разрешает `risk_profile.json`; ключа, ослабляющего guard, в файле нет. Недостающие ключи = умолчания выше. `pyramid.enabled=false` до подтверждения владельцем.

## 6.9 `logic/*` (WP3, чистая логика: без `os`, `socket`, `urllib`, `asyncio`, `open`; время только аргументом; проверяется `test_layering.py`)
```python
# candles.py
Candle = namedtuple("Candle", "start o h l c vol turn confirm")   # start в мс
class CandleBook(object):
    def seed(self, tf, rows): ...                  # rows [(start_ms,o,h,l,c,vol,turn)], последняя свеча считается формирующейся, если start+tf*60000 > now (now передаётся)
    def on_kline(self, tf, row, confirm): ...      # -> Candle закрытая | None; дозаполнение и дедупликация по start; пропуск (gap) -> self.gaps[tf] = (from,to)
    def last_closed(self, tf, n=1): ...            # -> [Candle] старые -> новые
    def forming(self, tf): ...                     # -> Candle | None
    def closed_since(self, tf, ts_ms): ...         # -> [Candle] закрытые свечи, начавшиеся >= ts_ms
    def atr(self, tf, n=14): ...                   # -> float | None (Уайлдер по закрытым)
    def vol_median(self, tf, lookback=20): ...     # -> float | None
    def swing_levels(self, tf, left=3, right=3, n=4): ...
class MarketView(object):                          # снимок для вычислений
    def __init__(self, sym, last, bid, ask, mark, ts, book, now): ...
    def price_age(self): ...
# conds.py
def eval_cond(cond, view, st, ctx): ...            # -> (bool, st2) ; st = память условия (seq-шаги, already), ctx: {"armed_ms","entered_ms","pos":{...}|None,"targets":{...}}; не мутирует st
def describe(cond, view, st, ctx): ...             # -> {"met":bool,"text":str,"dist_pct":float|None,"eta_s":int|None} для дашборда («до закрытия свечи 130 с»)
# rules.py
def evaluate_entry(plan, rt, view, now, limits): ...   # -> {"fire":bool,"blocked":None|"CHASE|SPREAD|STALE|...","rt_patch":{..},"cond":{...}}
def evaluate_lifecycle(plan, rt, view, now, limits): ...# -> [Action] ttl/invalidation/cancel_levels/max_hold/session_end
def evaluate_manage(plan, rt, view, now, limits): ...   # -> {"actions":[Action],"rt_patch":{..},"events":[{"name":"r_plus_1",..}]}
def trail_candidate(rule, plan, rt, view): ...           # -> float | None (абсолютная цена стопа-кандидата; монотонность проверяет вызывающий)
def r_now(rt, price): ...                                # -> float | None
Action = {"type":"fire_entry|cancel_entry|transition|move_stop|partial_close|close|move_target|add|notify","rule":str|None,"why":"RULE:id|LIFECYCLE:TTL|...", ...поля}
  # move_stop: {"to":float,"mode":"breakeven|price|lock_r|trail"} (to уже абсолютная цена, строго строже cur_stop иначе действие НЕ создаётся)
  # partial_close: {"frac_initial":float}|{"frac_remaining":float};  close: {"frac":float,"reason":str};  move_target: {"target":str,"price":float}
  # add: {"risk_frac":float};  notify: {"code":str};  transition: {"to":"expired|invalidated|cancelled","reason":str}
# pyramid.py
def worst_case_pnl(rt, stop, fee_pct, slip_pct, side): ...      # W из SPEC_V2 п. 1.7
def check_add(plan, rt, view, limits, add_rule, price, guard_add): ...  # -> {"ok":bool,"codes":[...],"qty_max":float,"risk_add":float}
# levels.py
def key_levels(book, now_ms): ...                  # -> dict для runtime/levels.json по символу
```
Требования к чистоте: функции детерминированы (одинаковый вход = одинаковый выход), не мутируют аргументы, возвращают новые структуры; во всех ветках, где данных нет (мало свечей, нет цены), результат «условие не выполнено» и причина, а не исключение.

## 6.10 Расширения `core/guard.py` и `core/journal.py` (WP9, обратная совместимость)
- `guard.evaluate(a)` принимает необязательные `a.paper` (bool; True: пропустить `no_active_session`, слоты, дневные лимиты, серию стопов, корреляцию, `learning`; остаются геометрия (стоп/RR/издержки), потолок плеча, размер от `risk_per_trade_frac x equity x score`, маржа `margin_per_trade_frac`; `risk_mult` = 1.0), `a.exec` (как есть: `taker|maker|limit`). Без `paper` поведение и все прежние тесты неизменны.
- `guard.evaluate_add(a)`: `a` = Namespace(`sym`, `side`, `px`, `stop`, `qty_open`, `avg_open`, `plan_risk0`, `risk_add_cap`, `score`, `contract_max_lev`, `exec="taker"`). Возвращает `{"ok","reasons","qty_max","margin_total","leverage"}`: проверяет сессию (`verdict` кроме `session_max_trades`), `dd_mult>0`, `daily_loss_limit` не достигнут (реализованный сегодня), серия стопов `<` лимита, суммарная маржа позиции (старая + новая) `<= margin_per_trade_max`, плечо позиции `<=` потолка, ликвидационный зазор по новой средней цене и текущему стопу (`liq_factor`), минимальный лот/номинал. НЕ проверяет `position_exists`, слоты, `max_open_positions` (позиция уже занимает слот). Риск добора считается внутри резерва `plan_risk0` (SPEC_V2 п. 1.7), новый слот и новый дневной резерв не нужны.
- `journal.cmd_open` копирует необязательные поля `plan_id`, `plan_rev`, `exp`, `var`, `setup`, `hyp`, `plan_ctx` (как сейчас `idea`, `pending_id`). Новые события (старые читатели их пропускают, потому что `_load` обрабатывает только `open/close/post`): `ev:"add"` (`id`, `ts`, `px`, `qty`, `fee`), `ev:"part"` (частичное закрытие: `id`, `ts`, `px`, `qty`, `fee`, `pnl`), `ev:"adjust"` (стоп/тейк: `id`, `ts`, `sl`, `tp`, `by`). `journal._load` применяет `add` (пересчёт `entry` как средневзвешенной, `notional`, `margin`; `risk_usdt` НЕ меняется, R относится к исходному риску) и `part` (накопление `realized_parts`, `fees_parts`), `adjust` (текущие `sl/tp`, поле `sl0` хранит исходный). `cmd_close` принимает необязательные `a.pnl_override` и `a.fees_usdt` (итог по частям), `a.exit` = средневзвешенный выход; без них поведение прежнее. Тесты WP9 фиксируют: прежние файлы журнала читаются идентично; журнал с новыми событиями читается прежним кодом `model.load_trades` без падений (проверяет WP8 на своей стороне).
- `journal.analyze` получает необязательный `by` (`exp|var|setup`): добавляет разрез в отчёт, прежний вывод без `by` не меняется.

## 6.11 Запись в журнал плановой сделки (кто что делает)
Исполнитель пишет через `journal.cmd_open/cmd_close` (функции, не subprocess) в том же процессе: тот же `PATH`, та же запись (`append`), порядок `open` -> (`add`/`part`/`adjust`)* -> `close`. Идентификатор сделки `B.journal_id(c, sym)` (формат `BB-M-SYM-MMDD-HHMMSS`). Режим `mode`: `live` на mainnet, `testnet` на тестнете, `paper` для бумажных. `venue=bybit`, `session`/`stype` из активной сессии (для paper из плана), `tags` = `bybit,<env>,plan,<exp:..>,<var:..>,<setup:..>[,no-confirm][,shadow]`, `fee_open` фактическая. Прежний `cmd_sync` пропускает записи с `plan_id` (правка WP1), `cmd_close` прежней команды для монеты с планом закрывает только позицию на бирже и не пишет `close` (его запишет исполнитель при финализации, идемпотентно).

## 6.12 Инфраструктура тестов (WP5 владеет `tests/fakes/`)
- `FakeClock(t0)`: `time()`, `advance(s)`, передаётся везде вместо `time.time`.
- `FakePort(env="testnet")`: словарное состояние (позиции, ордера, исполнения), `script(op, result_or_exception)` для заранее заданных ответов (таймаут, retCode), журнал вызовов `calls`, методы `fill(link_id, qty, px)`, `trigger_stop(sym)`, `set_price(...)` для симуляции; соблюдает `set_stop` монотонность как живой порт (иначе тесты Gate бессмысленны).
- `FakeFeed`: `emit_ticker`, `emit_kline`, `emit_private`, `set_state`.
- Каждый WP тестирует свой слой изолированно; сквозные сценарии (`test_e2e_*`) у WP10 используют `Executor` + `FakePort` + `FakeFeed` + реальные `PlanStore`/`decisions`/`bus` во временном корне.
- `./tb test` зелёный; прежние ~560 тестов не меняются по поведению. Тесты не трогают реальные `state/` и `runtime/` (корень через `root=`).
