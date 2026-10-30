"""Лимиты исполнителя `config/exec_limits.json` и аварийный флаг `state/KILL` (06 п. 6.8). Владелец: WP4.

Правило: значения в файле могут только СУЖАТЬ то, что разрешает `risk_profile.json`; ключа, ослабляющего guard, здесь нет.
Недостающие ключи = DEFAULTS. Пирамидинг выключен до подтверждения владельцем (решение Q1).
`state/KILL`: JSON {"flatten":bool,"reason":str,"ts":...}; читается при КАЖДОЙ проверке (без кэша); снятие только владельцем (`exec resume`).
"""
import copy
import json
import math
import os
import time

from tb import paths
from tb.core.filelock import atomic_write_json

MIN_SIZE_TTL_S = 6 * 3600.0         # сколько брифы мозга помнят «размер по риску не дотянул до минимального лота» по монете (runtime/min_size.json)

DEFAULTS = {
    "version": 1,
    "watch_symbols": [],
    "pyramid": {"enabled": False, "min_r": 1.0, "max_adds": 1, "max_add_risk_frac": 1.0, "min_gap_s": 60},
    "stop_moves": {"min_interval_s": 5, "min_step_ticks": 2, "min_gap_bps_from_price": 8, "max_moves_per_hour": 30},
    # автозащита прибыли: исполнитель сам подтягивает стоп у любой позиции (в том числе без правил manage в плане); только в сторону прибыли
    "auto_protect": {"enabled": True, "be_r": 0.7, "trail_activate_r": 1.0, "trail_lock_frac": 0.5},
    # сторож позиции (logic/watch.py): будит мозг (position_manager) по STALLED / FLOW_TURN. paper_only=true: пока только бумажные позиции.
    # TODO(live): если на paper сторож окажется жизнеспособным (число пробуждений и результат решений мозга, ./tb exp stats), включить для live: paper_only=false (решение владельца)
    "position_watch": {"enabled": True, "paper_only": True, "stalled_min": 15, "stalled_min_r": 0.5, "flow_turn_s": 30, "max_wakes_per_hour": 3},
    # WebSocket Trade API Bybit (executor/ws_trade.py): ордерные запросы (create/amend/cancel) по постоянному соединению. Выключено до проверки на testnet: ./tb exec wscheck
    "ws_trade": {"enabled": False, "request_timeout_s": 5.0, "ping_s": 20.0},
    "entry": {"max_price_age_s": 3, "max_chase_bps": 15, "no_confirm_max_size_mult": 0.5, "blocked_attempts_max": 3,
              "retry_after_s": 15, "fill_wait_s": 2, "max_level_distance_pct": 25},
    "protections": {"symbol_cooldown_after_stop_min": 30,
                    "pause_after_consecutive_stops": {"n": 2, "minutes": 60, "require_confirm": True},
                    "max_entries_per_hour": 4, "max_entries_per_day": 12, "max_plans_active": 6, "max_submits_per_hour": 20,
                    "max_actions_per_symbol_per_10s": 4},
    "connectivity": {"private_ws_down_hold_s": 20, "public_price_stale_s": 10, "rest_blind_s": 60, "reconcile_every_s": 30,
                     "snapshot_every_s": 2, "heartbeat_every_s": 2, "max_symbols": 12, "max_tick_jump_pct": 15},
    "rate": {"rest_write_rps": 4, "rest_read_rps": 8},
    "equity_sync": {"auto_set_portfolio": True, "max_dev_frac": 0.10, "stale_after_s": 300},
    "paper": {"slip_bps": 2, "stop_slip_bps": 5, "fill_through_ticks": 1, "latency_ms": 300, "max_active": 20,
              "auto_shadow_ideas": False, "blackout_mode": "block", "blackout_size_mult": 0.5},
    "blackout": {"pre_min": 0, "on_missing": "auto"},      # жёсткий отказ Gate по макро-окну (tb.core.blackout.check); on_missing: auto|block|allow
    "events": {"retention_days": 14, "unacked_warn_min": 10, "near_target_frac": 0.8, "wake_r": [1.0, -0.5]},
}


def defaults():
    """Глубокая копия DEFAULTS (чтобы вызывающий не испортил константу)."""
    return copy.deepcopy(DEFAULTS)


# направление безопасности: "min" = меньшее значение строже (безопаснее), "max" = большее строже, "bool_true"/"bool_false" = безопасное значение флага
SAFER = {
    "pyramid.enabled": "bool_false", "pyramid.min_r": "max", "pyramid.max_adds": "min", "pyramid.max_add_risk_frac": "min",
    "auto_protect.enabled": "bool_true", "auto_protect.be_r": "min", "auto_protect.trail_activate_r": "min", "auto_protect.trail_lock_frac": "max",
    "pyramid.min_gap_s": "max", "blackout.pre_min": "max", "stop_moves.max_moves_per_hour": "min", "paper.blackout_size_mult": "min",
    "entry.max_price_age_s": "min", "entry.max_chase_bps": "min", "entry.no_confirm_max_size_mult": "min",
    "entry.blocked_attempts_max": "min", "entry.max_level_distance_pct": "min",
    "protections.symbol_cooldown_after_stop_min": "max", "protections.pause_after_consecutive_stops.n": "min",
    "protections.pause_after_consecutive_stops.minutes": "max", "protections.pause_after_consecutive_stops.require_confirm": "bool_true",
    "protections.max_entries_per_hour": "min", "protections.max_entries_per_day": "min", "protections.max_plans_active": "min",
    "protections.max_submits_per_hour": "min", "protections.max_actions_per_symbol_per_10s": "min",
    "connectivity.private_ws_down_hold_s": "min", "connectivity.public_price_stale_s": "min", "connectivity.rest_blind_s": "min",
    "connectivity.reconcile_every_s": "min", "connectivity.max_tick_jump_pct": "min",
    "equity_sync.max_dev_frac": "min", "equity_sync.stale_after_s": "min",
}
# значения, которые обязаны быть строго положительными (интервалы и скорости); остальные числа >= 0
POSITIVE = ("connectivity.", "rate.", "stop_moves.min_interval_s", "paper.max_active", "events.retention_days")
ON_MISSING = ("auto", "block", "allow")   # blackout.on_missing: allow действует только в paper, в live/testnet всегда block
BLACKOUT_MODES = ("warn", "block")     # paper.blackout_mode: warn действует ТОЛЬКО в paper (см. tb.core.blackout), в live/testnet игнорируется
# доли: [0, 1]
FRACTIONS = ("equity_sync.max_dev_frac", "events.near_target_frac", "pyramid.max_add_risk_frac", "auto_protect.trail_lock_frac")


def defaults():
    """Глубокая копия DEFAULTS (чтобы вызывающий не испортил константу)."""
    return copy.deepcopy(DEFAULTS)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _merge(base, over, path, errors, unknown):
    """Накладывает over на копию base, проверяя типы по base; ошибки в errors, лишние ключи в unknown."""
    out = {}
    for k, dv in base.items():
        p = (path + "." + k) if path else k
        if k not in over:
            out[k] = copy.deepcopy(dv)
            continue
        v = over[k]
        if isinstance(dv, dict):
            if not isinstance(v, dict):
                errors.append("%s: ожидался объект, получено %r" % (p, v))
                out[k] = copy.deepcopy(dv)
            else:
                out[k] = _merge(dv, v, p, errors, unknown)
        elif isinstance(dv, bool):
            if not isinstance(v, bool):
                errors.append("%s: ожидалось true/false, получено %r" % (p, v))
            out[k] = v if isinstance(v, bool) else dv
        elif isinstance(dv, list):
            if k == "watch_symbols":
                ok = isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
                if not ok:
                    errors.append("%s: ожидался список символов (строки), получено %r" % (p, v))
            else:
                ok = isinstance(v, list) and len(v) == len(dv) and all(_num(x) for x in v)
                if not ok:
                    errors.append("%s: ожидался список из %d чисел, получено %r" % (p, len(dv), v))
            out[k] = list(v) if ok else copy.deepcopy(dv)
        elif isinstance(dv, int):
            if not (isinstance(v, int) and not isinstance(v, bool)):
                if _num(v) and float(v).is_integer():
                    v = int(v)
                else:
                    errors.append("%s: ожидалось целое число, получено %r" % (p, v))
                    v = dv
            out[k] = v
        elif isinstance(dv, float):
            if not _num(v):
                errors.append("%s: ожидалось число, получено %r" % (p, v))
                v = dv
            out[k] = v
        else:
            out[k] = v
        # диапазоны
        nv = out[k]
        if _num(nv) and not isinstance(dv, bool):
            if nv < 0:
                errors.append("%s: отрицательное значение %r" % (p, nv))
            elif nv == 0 and (p.startswith(POSITIVE[:2]) or p in POSITIVE[2:]):
                errors.append("%s: должно быть больше 0, получено %r" % (p, nv))
            if p in FRACTIONS and nv > 1:
                errors.append("%s: доля должна быть в [0, 1], получено %r" % (p, nv))
            if p in ("entry.no_confirm_max_size_mult", "paper.blackout_size_mult") and not (0 < nv <= 1):
                errors.append("%s: множитель должен быть в (0, 1], получено %r" % (p, nv))
        if p == "paper.blackout_mode" and nv not in BLACKOUT_MODES:
            errors.append("%s: допустимо %s, получено %r" % (p, "|".join(BLACKOUT_MODES), nv))
            out[k] = dv
        if p == "blackout.on_missing" and nv not in ON_MISSING:
            errors.append("%s: допустимо %s, получено %r" % (p, "|".join(ON_MISSING), nv))
            out[k] = dv
    for k in over:
        if k not in base:
            unknown.append((path + "." + k) if path else k)
    return out


def _read_file(root):
    """-> (dict | None). None = файла нет. ValueError: файл не читается/не JSON-объект."""
    path = paths.exec_limits_file(root)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        from tb.core import localcfg
        return localcfg.read_local("exec_limits", root)
    except OSError as e:
        raise ValueError("exec_limits.json: не удалось прочитать (%s)" % e)
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise ValueError("exec_limits.json: невалидный JSON (%s)" % e)
    if not isinstance(d, dict):
        raise ValueError("exec_limits.json: корень должен быть объектом")
    return _with_local(d, root)


def _with_local(d, root):
    """Личный слой `config/local/exec_limits.json` (tb.core.localcfg) поверх поставляемого файла; битый локальный слой игнорируется."""
    from tb.core import localcfg
    over = localcfg.read_local("exec_limits", root)
    return localcfg.merge(d, over) if over else d


def _build(root):
    d = _read_file(root)
    if d is None:
        return defaults(), []
    errors, unknown = [], []
    if "version" in d and d["version"] != DEFAULTS["version"]:
        errors.append("version: поддерживается %r, получено %r" % (DEFAULTS["version"], d["version"]))
    cfg = _merge(DEFAULTS, d, "", errors, unknown)
    if errors:
        raise ValueError("exec_limits.json: " + "; ".join(errors))
    return cfg, unknown


def load(root=None):
    """-> dict (умолчания + файл); ValueError с понятным текстом при мусоре. Нет файла = умолчания.

    Ключи вне схемы игнорируются (см. warnings()); значения, ослабляющие умолчания или risk_profile.json, не запрещены (см. warnings())."""
    return _build(root)[0]


def _get(d, dotted):
    for part in dotted.split("."):
        d = d[part]
    return d


def _risk_profile(root):
    try:
        with open(paths.config("risk_profile.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def warnings(root=None, cfg=None):
    """-> [строки по-русски]: ключи вне схемы и значения, ослабляющие умолчания или risk_profile.json (exec_limits только сужает).

    Не поднимает исключений на невалидном файле: тогда одна строка с причиной (ошибку покажет load)."""
    try:
        built, unknown = _build(root)
    except ValueError as e:
        return [str(e)]
    if cfg is None:
        cfg = built
    out = ["ключ %s не входит в схему exec_limits и игнорируется" % k for k in unknown]
    for key, how in sorted(SAFER.items()):
        try:
            v, dv = _get(cfg, key), _get(DEFAULTS, key)
        except (KeyError, TypeError):
            continue
        weaker = ((how == "min" and v > dv) or (how == "max" and v < dv)
                  or (how == "bool_false" and v is True) or (how == "bool_true" and v is False))
        if weaker:
            out.append("%s=%r ослабляет умолчание %r (exec_limits должен только сужать)" % (key, v, dv))
    prof = _risk_profile(root)
    mcs = prof.get("max_consecutive_stops")
    n = cfg["protections"]["pause_after_consecutive_stops"]["n"]
    if _num(mcs) and n > mcs:
        out.append("protections.pause_after_consecutive_stops.n=%r больше risk_profile.max_consecutive_stops=%r: пауза наступит позже стопа guard"
                   % (n, mcs))
    if cfg["pyramid"]["enabled"] and prof.get("allow_averaging_down") is False:
        out.append("pyramid.enabled=true: пирамидинг включён без подтверждения владельца (решение Q1); усреднение убытка запрещено профилем")
    try:
        from tb.core import risklevels      # WP16: exec_limits не должны ослаблять выбранный уровень риска
        out += risklevels.exec_warnings(cfg, root)
    except Exception:
        pass
    return out


def kill_state(root=None):
    """-> None | {"flatten":bool,"reason":str,"ts":...}; читает state/KILL каждый раз (без кэша).

    Файл есть, но нечитаем/не JSON: это всё равно KILL (flatten=False, reason = текст или метка), чтобы сбой разбора не снимал защиту."""
    path = paths.kill_file(root)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"flatten": False, "reason": "KILL нечитаем: %s" % e, "ts": None}
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return {"flatten": False, "reason": (raw.strip()[:200] or "KILL"), "ts": None}
    return {"flatten": bool(d.get("flatten", False)), "reason": str(d.get("reason", "")), "ts": d.get("ts"), "t": d.get("t")}


def set_kill(flatten=False, reason="", root=None):
    """Создаёт state/KILL атомарно. Уже есть KILL с flatten=true: не ослабляем (flatten сохраняется). Снятия в API мозга нет."""
    cur = kill_state(root)
    if cur is not None and cur.get("flatten"):
        flatten = True
    now = time.time()
    rec = {"flatten": bool(flatten), "reason": str(reason)[:200],
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "t": round(now, 3)}
    atomic_write_json(paths.kill_file(root), rec, sync=True)
    return rec


def clear_kill(root=None):
    """Только для CLI владельца (`exec resume`). -> True, если файл был."""
    try:
        os.remove(paths.kill_file(root))
        return True
    except FileNotFoundError:
        return False
