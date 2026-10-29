"""Настройки жизненного цикла дня: `config/lifecycle.json` поверх DEFAULTS (WP14). Лимиты риска сюда не входят (они в risk_profile/risk_levels)."""
import copy
import json

from tb import paths

DEFAULTS = {
    "version": 1,
    "day_start_utc": "07:00",          # начало «следующего дня» (расчёт funding, окно переноса)
    "day_end_utc": None,               # "HH:MM": авто-стоп дня в это время UTC (None = только max_day_hours)
    "max_day_hours": 16,
    "stop_day_on_session_end": False,
    "default_setups": ["level_reclaim", "sweep_reject"],
    "default_max_new_plans": 2,
    "preflight": {"require_supervisor": True, "hb_max_s": 10, "reconcile_max_s": 60, "scanner_hb_s": 120, "data_max_age_s": 300, "facts_max_age_s": 900,
                  "disk_min_mb": 200, "logs_max_mb": 500, "blackout_min": 30, "min_free_usdt": 1.0},
    "briefing": {"enabled": True, "attempts": 2, "timeout_s": 150, "facts_wait_s": 60},
    "closing": {"exec_wait_s": 120, "retry_after_s": 20, "owner_wait_min": 15, "auditor_timeout_s": 90, "reconcile_wait_s": 30, "snapshot_max_age_s": 30},
    "carry": {"enabled": True, "reconcile_alert_min": 15, "link_window_min": 30, "need_trail_or_max_hold": True},
    "resume": {"starting_abort_s": 600, "runner_stale_s": 45},
    "notify": {"os": False, "bus": True},
    "report": {"max_trades": 50},
}


def _merge(base, over):
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base and type(v) is not type(base[k]) and not (isinstance(v, (int, float)) and isinstance(base[k], (int, float)) and not isinstance(v, bool)) \
                and base[k] is not None:
            continue                     # неверный тип: оставляем умолчание
        else:
            base[k] = v
    return base


def load(root=None):
    """-> dict настроек; нет файла или он битый -> умолчания."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(paths.config("lifecycle.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _merge(cfg, d)
    except (OSError, ValueError):
        pass
    return cfg


def parse_hhmm(s, default=None):
    """'07:00' -> минуты от полуночи UTC; мусор -> default."""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (ValueError, TypeError):
        pass
    return default


def next_day_start(now, cfg):
    """Ближайший после `now` момент day_start_utc (epoch)."""
    mins = parse_hhmm(cfg.get("day_start_utc"), 7 * 60)
    day0 = int(now // 86400) * 86400
    t = day0 + mins * 60
    return float(t if t > now else t + 86400)
