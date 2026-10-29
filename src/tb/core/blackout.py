"""Макро-блэкаут (окна FOMC/CPI/NFP): режим paper = предупреждение, live и testnet = запрет. Владелец: эксперимент paper.

Флаг `exec_limits.json: paper.blackout_mode` ("block" по умолчанию, "warn") действует ТОЛЬКО когда площадка/режим РОВНО paper;
для live, testnet, observe и любого неизвестного значения политика всегда `block` (приоритет безопасности, флаг там игнорируется).
В режиме warn вход в окне допускается с тегом `blackout`, размером size_mult <= `paper.blackout_size_mult` (0.5) и предупреждением W_BLACKOUT;
такие сделки режутся отдельным срезом (`./tb exp stats --by blackout`) и не переносятся на live: проскальзывание paper-модели в новости занижено.
Окна читаются из `runtime/intel/blackouts.json` (watch_config + календарь intel; окно календаря уже включает «до» и «после» из intel.json `windows`).

ЖЁСТКИЙ запрет Gate исполнителя (`check`): при политике block (live, testnet, любой не-paper режим, paper при blackout_mode=block) вход, лимитный вход и ДОБОР
в окне (и за `blackout.pre_min` минут до него сверх «до» окна) отклоняются кодом E_BLACKOUT. Защитные действия (стоп, закрытие, отмена, reduce-only) Gate сюда не пускает.
Нет файла окон / он битый / устарел (> STALE_S): в live и testnet всегда отказ (`blackout.on_missing` там игнорируется, ослаблять нельзя); в paper
решает `blackout.on_missing` (auto = allow). Окно непроверенного кандидата (v:false) считается окном. Единый источник для Gate, preflight и валидатора.
"""
import calendar as _cal
import json
import math
import re

from tb import paths
from tb.core import exec_cfg

TAG = "blackout"
WARN_CODE = "W_BLACKOUT"
RISK_CODE = "blackout_warn"          # код риска в плане дня (RE_RISK)
LEAD_MIN = 60                        # «окно близко»: как порог мозга (BLACKOUT_PLANS, P14/RELEASE_UNSAFE)
DENY_CODE = "E_BLACKOUT"             # код отказа Gate
STALE_S = 48 * 3600                  # blackouts.json старше этого (поле ts) = данных об окнах нет
MAX_SIZE_MULT = 0.5                  # потолок size_mult в окне, даже если файл разрешает больше
_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?Z?$")


def is_paper(mode):
    """Только точное значение paper (live, testnet, trade, observe, None и мусор = не paper)."""
    return isinstance(mode, str) and mode == "paper"


def policy(root=None, mode=None, cfg=None):
    """-> {"mode": "warn"|"block", "size_mult": число, "configured": что записано в файле}. warn только при mode == paper; сбой чтения = block."""
    conf, size = "block", MAX_SIZE_MULT
    try:
        pc = (cfg if isinstance(cfg, dict) else exec_cfg.load(root)).get("paper") or {}
        if pc.get("blackout_mode") in exec_cfg.BLACKOUT_MODES:
            conf = pc["blackout_mode"]
        v = pc.get("blackout_size_mult")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 1:
            size = float(v)
    except Exception:
        conf = "block"
    eff = "warn" if (conf == "warn" and is_paper(mode)) else "block"
    return {"mode": eff, "size_mult": min(size, MAX_SIZE_MULT), "configured": conf}


def _ts(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _RE_ISO.match(v.strip()) if isinstance(v, str) else None
    if not m:
        return None
    a = [int(x or 0) for x in m.groups()]
    try:
        return float(_cal.timegm((a[0], a[1], a[2], a[3], a[4], a[5], 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def state(root, now, lead_min=LEAD_MIN):
    """Текущее/ближайшее окно из blackouts.json: {"name","in_min"} (in_min <= 0: внутри окна) при in_min <= lead_min, иначе None."""
    try:
        with open(paths.runtime("intel", "blackouts.json", root=root), encoding="utf-8") as f:
            wins = json.load(f)["windows"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    best = None
    for w in wins if isinstance(wins, list) else []:
        if not isinstance(w, dict):
            continue
        a, z = _ts(w.get("from")), _ts(w.get("to"))
        if a is None or z is None or now > z:
            continue
        in_min = int((a - now) // 60) if a > now else -int((now - a) // 60)
        if in_min > lead_min:
            continue
        if best is None or in_min < best["in_min"]:
            nm = w.get("name")
            best = {"name": nm if isinstance(nm, str) and re.match(r"^[A-Za-z0-9_.-]{1,40}$", nm) else "macro", "in_min": in_min}
    return best


def cap_size(size_mult, pol):
    """size_mult плана -> итоговый в окне при warn (не выше pol["size_mult"])."""
    try:
        m = float(size_mult)
    except (TypeError, ValueError):
        m = 1.0
    return min(m, float(pol["size_mult"]))


def _load(root):
    """-> (список окон | None, ts файла | None). None вместо списка = данных нет (файла нет, битый, нет ключа windows)."""
    try:
        with open(paths.runtime("intel", "blackouts.json", root=root), encoding="utf-8") as f:
            doc = json.load(f)
        wins = doc["windows"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(wins, list):
        return None, None
    ts = doc.get("ts")
    return wins, (float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None)


def _num_cfg(cfg, key, default):
    try:
        v = (cfg.get("blackout") or {}).get(key)
    except AttributeError:
        return default
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default


def check(root, mode, now, cfg=None):
    """Жёсткая проверка входа/добора (RISK_UP). -> {"blocked", "code", "reasons", "name", "in_min", "end_min", "verified", "missing", "policy"}.

    blocked=True только при политике block (см. policy: warn лишь в paper) и если: now в [from - pre_min, to] любого окна (границы включительно)
    ИЛИ данных об окнах нет и on_missing=block (в не-paper режимах всегда). in_min: минут до начала окна (<= 0 внутри), end_min: минут до конца окна (с округлением вверх)."""
    try:
        c = cfg if isinstance(cfg, dict) else exec_cfg.load(root)
    except Exception:
        c = {}
    pol = policy(root, mode, cfg=c)
    out = {"blocked": False, "code": None, "reasons": [], "name": None, "in_min": None, "end_min": None, "verified": True, "missing": False, "policy": pol["mode"]}
    wins, fts = _load(root)
    if wins is None or (fts is not None and now - fts > STALE_S):
        out["missing"] = True
        om = (c.get("blackout") or {}).get("on_missing") if isinstance(c.get("blackout"), dict) else None
        allow = is_paper(mode) and om in (None, "auto", "allow")
        if pol["mode"] == "block" and not allow:
            out.update(blocked=True, code=DENY_CODE, reasons=["нет данных об окнах блэкаута (runtime/intel/blackouts.json отсутствует, битый или устарел), консервативный отказ"])
        return out
    pre = float(_num_cfg(c, "pre_min", 0)) * 60.0
    best = None
    for w in wins:
        if not isinstance(w, dict):
            continue
        a, z = _ts(w.get("from")), _ts(w.get("to"))
        if a is None or z is None or not (a - pre <= now <= z):
            continue
        if best is None or z > best[1]:
            best = (a, z, w)
    if best is None:
        return out
    a, z, w = best
    nm = w.get("name")
    out["name"] = nm if isinstance(nm, str) and re.match(r"^[A-Za-z0-9_.-]{1,40}$", nm) else "macro"
    out["in_min"] = int((a - now) // 60) if a > now else -int((now - a) // 60)
    out["end_min"] = int(math.ceil((z - now) / 60.0))
    out["verified"] = w.get("v", True) is not False
    if pol["mode"] == "block":
        out.update(blocked=True, code=DENY_CODE)
        out["reasons"] = ["%s: %s, до конца окна %d мин%s" % (out["name"], "окно идёт" if a <= now else "окно через %d мин" % out["in_min"], out["end_min"],
                                                             "" if out["verified"] else " (дата события не проверена, считается окном)")]
    return out
