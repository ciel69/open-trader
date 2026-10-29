"""Схема плана `tb.plan/1` (SPEC_V2, 01_plan.md; контракт 06_contracts.md п. 6.4). Чистые функции: без файлов, сети, часов.

Владелец: WP2. Константы (версия, статусы, переходы, словари условий и действий, коды валидации, лимиты) зафиксированы WP0.
Единая таблица `FIELDS` (+ `COND_FIELDS`, `ACTION_FIELDS`) питает нормализацию, валидатор, `json_schema()` и `describe_cond`.

Конвейер: normalize -> validate_static -> (validate_context) ; для правок: amend = merge_patch -> normalize -> validate_static -> validate_amend.
Импортируются только json/re/math/hashlib/copy: ни файлов, ни сети, ни часов (время только аргументом).
"""

import copy
import hashlib
import json
import math
import re

SCHEMA = "tb.plan/1"

# ---- статусы и переходы (01 п. 1.6) ----
STATUSES = ("draft", "armed", "entered", "managing", "closed", "cancelled", "expired", "invalidated", "rejected")
TERMINAL = ("closed", "cancelled", "expired", "invalidated", "rejected")
TRANSITIONS = {
    "draft": ("armed", "rejected", "cancelled"),
    "armed": ("entered", "cancelled", "expired", "invalidated"),
    "entered": ("managing", "closed"),
    "managing": ("closed",),
    "closed": (), "cancelled": (), "expired": (), "invalidated": (), "rejected": (),
}
CLOSE_REASONS = ("tp", "sl", "manual", "timeout", "invalidation", "rule", "kill", "session_end", "risk", "liquidation")

# ---- значения перечислений полей (01 п. 1.2-1.5) ----
SIDES = ("long", "short")
MODES = ("live", "paper")
SESSION_TYPES = ("scalp", "intraday", "swing", "position")
SOURCE_KINDS = ("idea", "scanner", "brain", "manual", "shadow", "adopt")
CREATED_BY = ("brain:chat", "brain:api", "owner")
HOW_TYPES = ("market", "limit", "adopt")
TRIGGER_BY = ("last", "mark")
TARGET_EXEC = ("resting", "touch")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "1h")
NOTIFY_CODES = ("R_PLUS", "R_MINUS", "NEAR_TARGET", "CHECK_PLAN", "TIME")
CMD_OPS = ("submit", "amend", "cancel", "close", "stop", "hold", "release", "adopt")  # op конверта _inbox (01 п. 1.11)

# ---- регулярные выражения идентификаторов (строки, компилирует WP2) ----
RE_PLAN_ID = r"^PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3}$"
RE_SYMBOL = r"^[A-Z0-9]{2,20}$"
RE_TAG = r"^[a-z0-9._-]{2,40}$"
RE_RULE_ID = r"^[a-z0-9_]{2,16}$"
RE_TARGET_ID = r"^t[1-4]$"
RE_IDEA_ID = r"^I\d+$"

# ---- лимиты размера (01 п. 1.1) ----
MAX_PLAN_BYTES = 16 * 1024
MAX_STR_LEN = 280
MAX_TARGETS = 4
MAX_MANAGE = 12
MAX_INVALIDATION = 4
MAX_SHADOW_VARIANTS = 3
MAX_COND_DEPTH = 3
MAX_COND_LEAVES = 8
MAX_REASON_LEN = 200
THESIS_LEN = (10, 280)

# ---- словарь условий (01 п. 1.3): kind -> поля и места, где разрешён ----
WHERE_ENTRY, WHERE_INVALIDATION, WHERE_MANAGE = "entry", "invalidation", "manage"
_ALL_PLACES = (WHERE_ENTRY, WHERE_INVALIDATION, WHERE_MANAGE)
COND_KINDS = {
    "price_beyond": {"fields": ("dir", "level", "allow_already"), "where": _ALL_PLACES},
    "candle_close": {"fields": ("tf", "dir", "level", "n"), "where": _ALL_PLACES},
    "candle_reject": {"fields": ("tf", "level", "dir", "wick_min_pct"), "where": (WHERE_ENTRY, WHERE_INVALIDATION, WHERE_MANAGE)},
    "candle_body": {"fields": ("tf", "dir", "min_body_pct", "min_close_pos"), "where": (WHERE_ENTRY, WHERE_MANAGE)},
    "volume_spike": {"fields": ("tf", "mult", "lookback", "on", "dir"), "where": (WHERE_ENTRY, WHERE_MANAGE)},
    "price_in_zone": {"fields": ("low", "high"), "where": _ALL_PLACES},
    "spread_bps": {"fields": ("lte",), "where": (WHERE_ENTRY,)},
    "pnl_r": {"fields": ("gte", "gt", "lte", "lt"), "where": (WHERE_MANAGE, WHERE_INVALIDATION)},
    "elapsed": {"fields": ("min",), "where": (WHERE_ENTRY, WHERE_MANAGE)},
    "target_filled": {"fields": ("target",), "where": (WHERE_MANAGE,)},
    "near_target": {"fields": ("target", "frac"), "where": (WHERE_MANAGE,)},
}
# составные: {"all":[...]}, {"any":[...]}, {"not": <лист>}, и только в entry.when: {"kind":"seq","steps":[лист,...],"within_min":N}
COMPOSITE_KEYS = ("all", "any", "not")
SEQ_KIND = "seq"

# ---- словарь действий сопровождения (01 п. 1.5) ----
ACTIONS = {
    "move_stop": {"params": ("to", "price", "r", "pct", "offset_bps")},
    "trail": {"params": ("mode", "tf", "n", "mult", "pct", "step_r", "buffer_bps", "min_step_ticks")},
    "partial_close": {"params": ("frac_of_remaining", "frac_of_initial", "exec")},
    "close": {"params": ("frac", "reason")},
    "move_target": {"params": ("target", "price")},
    "add": {"params": ("risk_frac", "how")},
    "notify": {"params": ("code",)},
}
MOVE_STOP_TO = ("breakeven", "price", "lock_r", "entry_pct")
TRAIL_MODES = ("candle_extreme", "atr", "percent", "step_r")
CLOSE_RULE_REASONS = ("timeout", "invalidation", "rule", "risk")

# ---- коды валидации (01 п. 1.8) ----
ERROR_CODES = (
    "E_SCHEMA", "E_UNKNOWN_FIELD", "E_TYPE", "E_RANGE", "E_TOO_LARGE", "E_ID", "E_DUP_ID", "E_COND_DEPTH",
    "E_COND_KIND_FORBIDDEN", "E_NONFINITE",
    "E_SYMBOL_UNKNOWN", "E_STOP_SIDE", "E_TARGET_SIDE", "E_TARGET_ORDER", "E_FRAC_SUM", "E_LEVEL_DISTANCE",
    "E_CANCEL_LEVELS", "E_TTL", "E_ADOPT_NO_POSITION", "E_ADOPT_NO_STOP",
    "E_GUARD", "E_STOP_PCT", "E_RR", "E_SESSION_MISMATCH", "E_NO_SESSION", "E_NO_CONFIRM", "E_COOLDOWN",
    "E_LIMIT_PLANS", "E_SYMBOL_BUSY",
    "E_STOP_WIDEN", "E_RISK_UP", "E_IMMUTABLE", "E_STATUS_FORBIDS", "E_REV_STALE", "E_TARGET_FARTHER_NOT_RISKFREE",
    "E_HOLD_EXTEND", "E_TRANSITION",
    "E_ADD_NOT_PROFIT_GATED", "E_ADD_RISK",
)
WARN_CODES = (
    "W_FIELD_IGNORED", "W_LADDER_COLLAPSE", "W_TTL_CLAMPED", "W_NO_INVALIDATION", "W_ADD_DISABLED", "W_NO_RUNNER_RULE",
    "W_PRICE_STALE_AT_ACCEPT",
)
SEVERITIES = ("error", "warn")


# =====================================================================================================================
# Единая таблица полей. Узел: {"t": тип, ...}. Типы: str, int, num, bool, enum, obj, arr, cond, any.
# Общие ключи узла: req (обязательное), default (значение по умолчанию), exec (поле выставляет исполнитель).
#   str: re, id (ошибка формата = E_ID), minlen, maxlen, lower/upper (нормализация регистра)
#   num/int: min, max, gt (строго больше)      enum: values, upper
#   obj: fields {имя: узел}                    arr: item (узел), min, max
# =====================================================================================================================
MAX_NUM = 1e15           # числа по модулю больше = E_RANGE (защита от «огромных чисел»)
MAX_DEPTH = 32           # глубина вложенности JSON; глубже = E_TOO_LARGE (устойчивость к мусору)
EXEC_FIELDS = ("id", "rev", "created_ts", "created_by", "armed_ts", "guard_preview", "prereg_hash")
PREREG_FIELDS = ("symbol", "side", "entry", "stop", "targets", "manage", "invalidation", "risk")
SHADOW_PATCH_KEYS = ("entry", "stop", "targets", "manage", "invalidation", "lifecycle", "risk")
DEFAULT_TTL_MIN = {"scalp": 15, "intraday": 180, "swing": 1440, "position": 1440}   # уточнение WP2: когда ttl не задан нигде
CONFIRM_KINDS = ("candle_close", "candle_reject", "candle_body", "volume_spike")
TF_LABEL = {"1m": "1м", "3m": "3м", "5m": "5м", "15m": "15м", "1h": "1ч"}


def _n(t, **kw):
    d = {"t": t}
    d.update(kw)
    return d


def _level(**kw):
    return _n("num", gt=0, max=MAX_NUM, **kw)


def _tf(**kw):
    return _n("enum", values=TIMEFRAMES, **kw)


COND_FIELDS = {
    "price_beyond": {"dir": _n("enum", values=("above", "below"), req=True), "level": _level(req=True),
                     "allow_already": _n("bool", default=False)},
    "candle_close": {"tf": _tf(req=True), "dir": _n("enum", values=("above", "below"), req=True), "level": _level(req=True),
                     "n": _n("int", min=1, max=5, default=1)},
    "candle_reject": {"tf": _tf(req=True), "level": _level(req=True), "dir": _n("enum", values=("short", "long"), req=True),
                      "wick_min_pct": _n("num", min=0, max=100, default=0.0)},
    "candle_body": {"tf": _tf(req=True), "dir": _n("enum", values=("up", "down"), req=True),
                    "min_body_pct": _n("num", min=0, max=100, req=True), "min_close_pos": _n("num", min=0, max=1)},
    "volume_spike": {"tf": _tf(req=True), "mult": _n("num", gt=0, max=1000, req=True), "lookback": _n("int", min=5, max=100, default=20),
                     "on": _n("enum", values=("close", "live"), default="close"),
                     "dir": _n("enum", values=("any", "up", "down"), default="any")},
    "price_in_zone": {"low": _level(req=True), "high": _level(req=True)},
    "spread_bps": {"lte": _n("num", gt=0, max=10000, req=True)},
    "pnl_r": {"gte": _n("num", min=-100, max=100), "gt": _n("num", min=-100, max=100),
              "lte": _n("num", min=-100, max=100), "lt": _n("num", min=-100, max=100)},
    "elapsed": {"min": _n("num", gt=0, max=43200, req=True)},
    "target_filled": {"target": _n("str", re=RE_TARGET_ID, id=True, req=True)},
    "near_target": {"target": _n("str", re=RE_TARGET_ID, id=True, req=True), "frac": _n("num", gt=0, max=1, req=True)},
}

_STEP_BPS = _n("num", min=0, max=500)
ACTION_FIELDS = {
    "move_stop": {"to": _n("enum", values=MOVE_STOP_TO, req=True), "price": _level(), "r": _n("num", min=0, max=50),
                  "pct": _n("num", min=0, max=100), "offset_bps": _STEP_BPS},
    "trail": {"mode": _n("enum", values=TRAIL_MODES, req=True), "tf": _tf(), "n": _n("int", min=1, max=100),
              "mult": _n("num", gt=0, max=20), "pct": _n("num", gt=0, max=50), "step_r": _n("num", gt=0, max=10),
              "buffer_bps": _STEP_BPS, "min_step_ticks": _n("int", min=1, max=1000)},
    "partial_close": {"frac_of_remaining": _n("num", gt=0, max=1), "frac_of_initial": _n("num", gt=0, max=1),
                      "exec": _n("enum", values=("market",), default="market")},
    "close": {"frac": _n("num", gt=0, max=1, default=1.0), "reason": _n("enum", values=CLOSE_RULE_REASONS, default="rule")},
    "move_target": {"target": _n("str", re=RE_TARGET_ID, id=True, req=True), "price": _level(req=True)},
    "add": {"risk_frac": _n("num", gt=0, max=1, req=True), "how": _n("enum", values=("market",), default="market")},
    "notify": {"code": _n("enum", values=NOTIFY_CODES, upper=True, req=True)},
}
# параметры trail по режимам: (обязательные, допустимые дополнительные)
_TRAIL_MODE_PARAMS = {
    "candle_extreme": (("tf", "n"), ("buffer_bps", "min_step_ticks")),
    "atr": (("tf", "n", "mult"), ("buffer_bps", "min_step_ticks")),
    "percent": (("pct",), ("buffer_bps", "min_step_ticks")),
    "step_r": (("step_r",), ("buffer_bps", "min_step_ticks")),
}
_MOVE_STOP_PARAM = {"price": "price", "lock_r": "r", "entry_pct": "pct"}

_TAG = _n("str", re=RE_TAG, lower=True, schema_re=r"^[A-Za-z0-9._-]{2,40}$")
_VARIANT = _n("str", re=r"^[a-z0-9._-]{1,40}$", lower=True, schema_re=r"^[A-Za-z0-9._-]{1,40}$")   # расхождение с 01: в примерах variant "A" (1 символ)
FIELDS = {
    "schema": _n("enum", values=(SCHEMA,), req=True),
    "id": _n("str", re=RE_PLAN_ID, id=True, exec=True),
    "rev": _n("int", min=1, max=10 ** 9, exec=True),
    "symbol": _n("str", re=RE_SYMBOL, upper=True, req=True),
    "side": _n("enum", values=SIDES, req=True),
    "mode": _n("enum", values=MODES, default="live"),
    "venue": _n("str", re=r"^[a-z][a-z0-9]{1,11}$", lower=True, schema_re=r"^[a-z][a-z0-9]{1,11}$"),     # биржа плана (id из config/exchanges.json); нет поля = bybit
    "session_type": _n("enum", values=SESSION_TYPES),
    "source": _n("obj", req=True, fields={
        "kind": _n("enum", values=SOURCE_KINDS, req=True),
        "idea_id": _n("str", re=RE_IDEA_ID, id=True),
        "event_seq": _n("int", min=0, max=10 ** 12),
        "parent_plan": _n("str", re=RE_PLAN_ID, id=True),
    }),
    "thesis": _n("str", minlen=THESIS_LEN[0], maxlen=THESIS_LEN[1], req=True),
    "tags": _n("obj", default={}, fields={
        "experiment": _TAG, "variant": _VARIANT, "setup": _TAG, "hypothesis": _TAG,
        "extra": _n("arr", item=_TAG, max=5),
    }),
    "entry": _n("obj", req=True, fields={
        "when": _n("cond"),
        "how": _n("obj", req=True, fields={
            "type": _n("enum", values=HOW_TYPES, req=True),
            "price": _level(),
            "post_only": _n("bool"),
            "snap": _n("bool"),
        }),
        "ref_price": _level(),
        "valid": _n("obj", fields={
            "ttl_min": _n("int", min=1, max=1440),
            "cancel_above": _level(),
            "cancel_below": _level(),
            "max_chase_bps": _n("num", gt=0, max=1000, default=15),
        }),
    }),
    "stop": _n("obj", req=True, fields={
        "price": _level(req=True),
        "trigger_by": _n("enum", values=TRIGGER_BY, default="last"),
    }),
    "targets": _n("arr", req=True, min=1, max=MAX_TARGETS, item=_n("obj", fields={
        "id": _n("str", re=RE_TARGET_ID, id=True, req=True),
        "price": _level(req=True),
        "frac": _n("num", gt=0, max=1, req=True),
        "exec": _n("enum", values=TARGET_EXEC, default="resting"),
    })),
    "risk": _n("obj", default={}, fields={
        "score": _n("num", min=0, max=1, default=0.5),
        "size_mult": _n("num", gt=0, max=1, default=1.0),
        "confirm_waiver": _n("bool", default=False),
    }),
    "manage": _n("arr", default=[], max=MAX_MANAGE, item=_n("obj", fields={
        "id": _n("str", re=RE_RULE_ID, id=True, req=True),
        "when": _n("cond", req=True),
        "do": _n("action", req=True),
        "once": _n("bool", default=False),
        "max_times": _n("int", min=1, max=100000),
        "cooldown_s": _n("num", min=0, max=86400, default=0),
    })),
    "invalidation": _n("arr", default=[], max=MAX_INVALIDATION, item=_n("cond")),
    "lifecycle": _n("obj", default={}, fields={
        "ttl_min": _n("int", min=1, max=1440),
        "max_hold_min": _n("int", min=1, max=43200),
        "flatten_at_session_end": _n("bool"),    # умолчание зависит от session_type (scalp = true): post-шаг normalize
        "not_before_ts": _n("num", gt=0, max=MAX_NUM),
    }),
    "shadow_variants": _n("arr", default=[], max=MAX_SHADOW_VARIANTS, item=_n("obj", fields={
        "name": _n("str", re=RE_TAG, req=True),
        "patch": _n("any", req=True),
    })),
    "created_ts": _n("num", gt=0, max=MAX_NUM, exec=True),
    "created_by": _n("enum", values=CREATED_BY, exec=True),
    "armed_ts": _n("num", gt=0, max=MAX_NUM, exec=True),
    "guard_preview": _n("any", exec=True),
    "prereg_hash": _n("str", re=r"^[0-9a-f]{16}$", exec=True),
}

_RX = {}


def _rx(p):
    r = _RX.get(p)
    if r is None:
        r = _RX[p] = re.compile(p, re.ASCII)
    return r


_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")
_WS_CTRL = re.compile("[\t\n\r\x0b\x0c]+")
_NUMSTR = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def issue(code, severity, path, msg):
    """Одна проблема валидации: {"code","severity":"error|warn","path","msg"} (формат Issue из 06 п. 6.4)."""
    return {"code": code, "severity": severity, "path": path, "msg": msg}


def _err(iss, code, path, msg):
    iss.append(issue(code, "error", path, msg))


def _warn(iss, code, path, msg):
    iss.append(issue(code, "warn", path, msg))


def has_errors(issues):
    """True, если среди проблем есть хотя бы одна с severity=error."""
    return any(i.get("severity") == "error" for i in issues)


def can_transition(cur, new):
    """True, если переход статуса плана разрешён таблицей TRANSITIONS (01 п. 1.6). Из терминалов выхода нет."""
    return new in TRANSITIONS.get(cur, ())


def check_transition(cur, new, path="status"):
    """-> [Issue]: пусто, если переход cur -> new допустим, иначе E_TRANSITION."""
    if can_transition(cur, new):
        return []
    return [issue("E_TRANSITION", "error", path, "переход %s -> %s запрещён (из терминалов выхода нет)" % (cur, new))]


def check_rev(spec, rev_expected):
    """-> [Issue]: E_REV_STALE, если rev_expected не равен текущему rev плана (оптимистичная блокировка amend)."""
    cur = spec.get("rev") if isinstance(spec, dict) else None
    if rev_expected is None or isinstance(rev_expected, bool) or rev_expected != cur:
        return [issue("E_REV_STALE", "error", "rev", "ожидалась ревизия %r, у плана %r: обновите вид (plan show)" % (rev_expected, cur))]
    return []


# ---------------------------------------------------------------------------------------------------------------------
# безопасные утилиты
# ---------------------------------------------------------------------------------------------------------------------
def _too_deep(obj, limit=MAX_DEPTH):
    """True, если вложенность dict/list глубже limit (итеративно, без рекурсии)."""
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if isinstance(o, dict):
            ch = o.values()
        elif isinstance(o, (list, tuple)):
            ch = o
        else:
            continue
        if d > limit:
            return True
        for c in ch:
            if isinstance(c, (dict, list, tuple)):
                stack.append((c, d + 1))
    return False


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fin(v):
    return _num(v) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _jeq(a, b):
    """Равенство по каноническому JSON (143 == 143.0)."""
    try:
        return _canon_json(a) == _canon_json(b)
    except (TypeError, ValueError, RecursionError):
        return a == b


def _canon(o):
    if isinstance(o, dict):
        return dict((k, _canon(v)) for k, v in o.items())
    if isinstance(o, (list, tuple)):
        return [_canon(v) for v in o]
    if isinstance(o, float) and not (math.isnan(o) or math.isinf(o)) and o == int(o) and abs(o) < MAX_NUM:
        return int(o)
    return o


def _canon_json(o):
    return json.dumps(_canon(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------------------------------------------------
def _clean_str(s):
    s = _WS_CTRL.sub(" ", s)
    s = _CTRL.sub("", s)
    return s.strip()


def _norm_val(node, v):
    """Приводит значение к типу узла, не поднимая исключений: что не удалось привести, остаётся как есть (поймает validate_static)."""
    t = node["t"]
    if t == "str" or t == "enum":
        if not isinstance(v, str):
            return v
        v = _clean_str(v)
        if node.get("upper"):
            v = v.upper()
        elif t == "enum" or node.get("lower"):
            v = v.lower()
        return v
    if t == "num":
        if isinstance(v, str) and _NUMSTR.fullmatch(v.strip()):
            try:
                return float(v)
            except (ValueError, OverflowError):
                return v
        if isinstance(v, int) and not isinstance(v, bool):
            return float(v) if abs(v) < MAX_NUM else v
        return v
    if t == "int":
        if isinstance(v, str) and re.fullmatch(r"[+-]?[0-9]+", v.strip()):
            return int(v)
        if isinstance(v, float) and not (math.isnan(v) or math.isinf(v)) and v == int(v):
            return int(v)
        return v
    if t == "obj":
        return _norm_obj(node["fields"], v)
    if t == "arr":
        if isinstance(v, list):
            return [_norm_val(node["item"], x) for x in v]
        return v
    if t == "cond":
        return _norm_cond(v)
    if t == "action":
        return _norm_action(v)
    if t == "any":
        return v
    return v


def _default_of(node):
    d = node.get("default")
    return copy.deepcopy(d)


def _norm_obj(fields, v):
    if not isinstance(v, dict):
        return v
    out = {}
    for k, x in v.items():
        node = fields.get(k) if isinstance(k, str) else None
        out[k] = _norm_val(node, x) if node else copy.deepcopy(x)
    for k, node in fields.items():
        if k not in out and "default" in node:
            out[k] = _norm_val(node, _default_of(node))
    return out


def _norm_cond(c):
    if not isinstance(c, dict):
        return c
    if isinstance(c.get("kind"), str) and c["kind"].strip().lower() == SEQ_KIND:
        out = {"kind": SEQ_KIND}
        for k, v in c.items():
            if k == "kind":
                continue
            if k == "steps" and isinstance(v, list):
                out[k] = [_norm_cond(x) for x in v]
            elif k == "within_min":
                out[k] = _norm_val(_n("num"), v)
            else:
                out[k] = copy.deepcopy(v)
        return out
    for key in COMPOSITE_KEYS:
        if key in c:
            out = {}
            for k, v in c.items():
                if k in ("all", "any") and isinstance(v, list):
                    out[k] = [_norm_cond(x) for x in v]
                elif k == "not":
                    out[k] = _norm_cond(v)
                else:
                    out[k] = copy.deepcopy(v)
            return out
    kind = c.get("kind")
    kind = _clean_str(kind).lower() if isinstance(kind, str) else kind
    spec = COND_FIELDS.get(kind) if isinstance(kind, str) else None
    out = {"kind": kind}
    for k, v in c.items():
        if k == "kind":
            continue
        node = spec.get(k) if spec else None
        out[k] = _norm_val(node, v) if node else copy.deepcopy(v)
    if spec:
        for k, node in spec.items():
            if k not in out and "default" in node:
                out[k] = _norm_val(node, _default_of(node))
    return out


def _norm_action(a):
    if not isinstance(a, dict):
        return a
    name = a.get("action")
    name = _clean_str(name).lower() if isinstance(name, str) else name
    spec = ACTION_FIELDS.get(name) if isinstance(name, str) else None
    out = {"action": name}
    for k, v in a.items():
        if k == "action":
            continue
        node = spec.get(k) if spec else None
        out[k] = _norm_val(node, v) if node else copy.deepcopy(v)
    if spec:
        for k, node in spec.items():
            if k not in out and "default" in node:
                out[k] = _norm_val(node, _default_of(node))
    return out


def normalize(raw, now=None, keep_executor=False):
    """-> (plan_dict, issues): приводит типы и регистр, чистит строки (управляющие и RTL-символы вырезаются), проставляет умолчания.

    Поля исполнителя (id, rev, created_ts, ..., см. EXEC_FIELDS) при submit игнорируются с W_FIELD_IGNORED; keep_executor=True оставляет их
    (для хранимого spec и amend). Умолчания: mode=live, tags={}, risk, manage/invalidation/shadow_variants=[], stop.trigger_by=last, target.exec=resting,
    session_type=intraday для paper, lifecycle.flatten_at_session_end = (session_type == scalp) (STATUS.md расхождение 5), lifecycle.ttl_min из
    DEFAULT_TTL_MIN, если ttl не задан ни в lifecycle, ни в entry.valid (уточнение WP2), для limit post_only=true/snap=false, max_times=1 при once.
    Никогда не бросает исключение: непригодный вход -> ({}, [E_TYPE|E_TOO_LARGE]). Аргумент не мутируется.

    >>> p, iss = normalize({"schema": "tb.plan/1", "symbol": "sol", "side": "LONG"})
    >>> p["symbol"], p["side"], p["mode"]
    ('SOL', 'long', 'live')
    """
    iss = []
    if not isinstance(raw, dict):
        _err(iss, "E_TYPE", "", "план должен быть JSON-объектом")
        return {}, iss
    if _too_deep(raw):
        _err(iss, "E_TOO_LARGE", "", "вложенность глубже %d" % MAX_DEPTH)
        return {}, iss
    try:
        plan = _norm_obj(FIELDS, raw)
    except (RecursionError, TypeError, ValueError, OverflowError) as e:   # защитный пояс: нормализатор не должен падать на мусоре
        _err(iss, "E_TYPE", "", "не удалось разобрать план: %s" % e)
        return {}, iss
    sym = plan.get("symbol")
    if isinstance(sym, str) and len(sym) > 4 and sym.endswith("USDT"):
        plan["symbol"] = sym[:-4]
    if not keep_executor:
        for k in EXEC_FIELDS:
            if k in plan:
                del plan[k]
                _warn(iss, "W_FIELD_IGNORED", k, "поле выставляет исполнитель, значение проигнорировано")
    if plan.get("mode") == "paper" and "session_type" not in plan:
        plan["session_type"] = "intraday"
    lc = plan.get("lifecycle")
    if isinstance(lc, dict):
        st = plan.get("session_type")
        if "flatten_at_session_end" not in lc:
            lc["flatten_at_session_end"] = (st == "scalp")
        ent = plan.get("entry")
        valid = ent.get("valid") if isinstance(ent, dict) else None
        has_valid_ttl = isinstance(valid, dict) and "ttl_min" in valid
        if "ttl_min" not in lc and not has_valid_ttl and isinstance(st, str) and st in DEFAULT_TTL_MIN:
            lc["ttl_min"] = DEFAULT_TTL_MIN[st]
    ent = plan.get("entry")
    if isinstance(ent, dict) and isinstance(ent.get("how"), dict) and ent["how"].get("type") == "limit":
        ent["how"].setdefault("post_only", True)
        ent["how"].setdefault("snap", False)
    if isinstance(plan.get("manage"), list):
        for r in plan["manage"]:
            if isinstance(r, dict) and r.get("once") is True and "max_times" not in r:
                r["max_times"] = 1
    return plan, iss


# =====================================================================================================================
# validate_static
# =====================================================================================================================
def _check_scalar(node, v, path, iss):
    """Проверка листового значения по узлу. True, если тип подошёл (дальнейшие проверки смысла имеют смысл)."""
    t = node["t"]
    if t in ("str", "enum"):
        if not isinstance(v, str):
            _err(iss, "E_TYPE", path, "ожидалась строка")
            return False
        if _CTRL.search(v):
            _err(iss, "E_TYPE", path, "недопустимые управляющие символы в строке")
            return False
        if len(v) > MAX_STR_LEN:
            _err(iss, "E_TOO_LARGE", path, "строка длиннее %d символов" % MAX_STR_LEN)
            return False
        if t == "enum":
            if v not in node["values"]:
                _err(iss, "E_RANGE", path, "значение %r вне списка %s" % (v[:40], "/".join(node["values"])))
                return False
            return True
        if "minlen" in node and len(v) < node["minlen"]:
            _err(iss, "E_RANGE", path, "строка короче %d символов" % node["minlen"])
            return False
        if "maxlen" in node and len(v) > node["maxlen"]:
            _err(iss, "E_TOO_LARGE", path, "строка длиннее %d символов" % node["maxlen"])
            return False
        if "re" in node and not _rx(node["re"]).fullmatch(v):
            _err(iss, "E_ID" if node.get("id") else "E_RANGE", path, "формат не соответствует %s" % node["re"])
            return False
        return True
    if t in ("num", "int"):
        if not _num(v):
            _err(iss, "E_TYPE", path, "ожидалось число")
            return False
        if not _fin(v):
            _err(iss, "E_NONFINITE", path, "число не конечное (NaN/inf)")
            return False
        if t == "int" and not isinstance(v, int):
            _err(iss, "E_TYPE", path, "ожидалось целое")
            return False
        if abs(v) > MAX_NUM:
            _err(iss, "E_RANGE", path, "число по модулю больше %g" % MAX_NUM)
            return False
        if "min" in node and v < node["min"]:
            _err(iss, "E_RANGE", path, "значение %s < %s" % (v, node["min"]))
            return False
        if "max" in node and v > node["max"]:
            _err(iss, "E_RANGE", path, "значение %s > %s" % (v, node["max"]))
            return False
        if "gt" in node and v <= node["gt"]:
            _err(iss, "E_RANGE", path, "значение %s должно быть > %s" % (v, node["gt"]))
            return False
        return True
    if t == "bool":
        if not isinstance(v, bool):
            _err(iss, "E_TYPE", path, "ожидалось true/false")
            return False
        return True
    return True


def _check_obj(fields, v, path, iss, extra_ok=()):
    if not isinstance(v, dict):
        _err(iss, "E_TYPE", path, "ожидался объект")
        return False
    for k in v:
        if k not in fields and k not in extra_ok:
            _err(iss, "E_UNKNOWN_FIELD", _j(path, k), "неизвестное поле")
    for k, node in fields.items():
        if node.get("req") and k not in v:
            _err(iss, "E_SCHEMA", _j(path, k), "обязательное поле отсутствует")
    for k, node in fields.items():
        if k in v:
            _check_node(node, v[k], _j(path, k), iss)
    return True


def _j(path, k):
    return "%s.%s" % (path, k) if path else str(k)


def _check_node(node, v, path, iss, ctx=None):
    t = node["t"]
    if t == "obj":
        _check_obj(node["fields"], v, path, iss)
    elif t == "arr":
        if not isinstance(v, list):
            _err(iss, "E_TYPE", path, "ожидался массив")
            return
        if "max" in node and len(v) > node["max"]:
            _err(iss, "E_TOO_LARGE", path, "элементов %d > %d" % (len(v), node["max"]))
        if "min" in node and len(v) < node["min"]:
            _err(iss, "E_RANGE", path, "элементов %d < %d" % (len(v), node["min"]))
        for i, x in enumerate(v[:max(node.get("max", 1000), 0) + 1]):
            _check_node(node["item"], x, "%s[%d]" % (path, i), iss, ctx)
    elif t == "any":
        if v is None or isinstance(v, (dict, list, str, int, float, bool)):
            pass
        else:
            _err(iss, "E_TYPE", path, "недопустимый тип значения")
    else:
        _check_scalar(node, v, path, iss)


def _place_cond(c, place, path, iss, st, depth=0, in_seq=False):
    """Проверка условия по словарю. st: {"leaves":int,"targets":set|None,"leaf_list":[(path, leaf)]}."""
    if not isinstance(c, dict):
        _err(iss, "E_TYPE", path, "условие должно быть объектом")
        return
    comp = [k for k in COMPOSITE_KEYS if k in c]
    if comp:
        if len(c) != 1:
            _err(iss, "E_SCHEMA", path, "составное условие содержит ровно один ключ all/any/not")
            return
        key = comp[0]
        if depth + 1 > MAX_COND_DEPTH:
            _err(iss, "E_COND_DEPTH", path, "глубина условия больше %d" % MAX_COND_DEPTH)
            return
        sub = c[key]
        if key == "not":
            if not isinstance(sub, dict) or any(k in sub for k in COMPOSITE_KEYS) or sub.get("kind") == SEQ_KIND:
                _err(iss, "E_SCHEMA", _j(path, "not"), "not принимает только лист")
                return
            _place_cond(sub, place, _j(path, "not"), iss, st, depth + 1, in_seq)
            return
        if not isinstance(sub, list):
            _err(iss, "E_TYPE", _j(path, key), "ожидался массив условий")
            return
        if not sub:
            _err(iss, "E_RANGE", _j(path, key), "пустой список условий")
            return
        if len(sub) > MAX_COND_LEAVES:
            _err(iss, "E_TOO_LARGE", _j(path, key), "условий больше %d" % MAX_COND_LEAVES)
            return
        for i, x in enumerate(sub):
            _place_cond(x, place, "%s.%s[%d]" % (path, key, i), iss, st, depth + 1, in_seq)
        return
    kind = c.get("kind")
    if kind == SEQ_KIND:
        if place != WHERE_ENTRY or in_seq:
            _err(iss, "E_COND_KIND_FORBIDDEN", path, "seq разрешён только в entry.when (и не вложенный)")
            return
        for k in c:
            if k not in ("kind", "steps", "within_min"):
                _err(iss, "E_UNKNOWN_FIELD", _j(path, k), "неизвестное поле")
        steps = c.get("steps")
        if not isinstance(steps, list):
            _err(iss, "E_SCHEMA", _j(path, "steps"), "steps обязателен: массив листьев")
        elif not 1 <= len(steps) <= MAX_COND_LEAVES:
            _err(iss, "E_RANGE", _j(path, "steps"), "шагов должно быть 1..%d" % MAX_COND_LEAVES)
        else:
            for i, x in enumerate(steps):
                sp = "%s.steps[%d]" % (path, i)
                if isinstance(x, dict) and (any(k in x for k in COMPOSITE_KEYS) or x.get("kind") == SEQ_KIND):
                    _err(iss, "E_SCHEMA", sp, "шаг seq должен быть листом")
                else:
                    _place_cond(x, place, sp, iss, st, depth, True)
        wm = c.get("within_min")
        if wm is None:
            _err(iss, "E_SCHEMA", _j(path, "within_min"), "обязательное поле отсутствует")
        else:
            _check_scalar(_n("num", gt=0, max=1440), wm, _j(path, "within_min"), iss)
        return
    if not isinstance(kind, str):
        _err(iss, "E_SCHEMA", _j(path, "kind"), "у условия нет kind")
        return
    spec = COND_FIELDS.get(kind)
    if spec is None:
        _err(iss, "E_UNKNOWN_FIELD", _j(path, "kind"), "неизвестный вид условия %r" % kind[:40])
        return
    if place not in COND_KINDS[kind]["where"]:
        _err(iss, "E_COND_KIND_FORBIDDEN", path, "условие %s не разрешено в %s" % (kind, place))
        return
    st["leaves"] += 1
    if st["leaves"] == MAX_COND_LEAVES + 1:
        _err(iss, "E_TOO_LARGE", path, "листьев условия больше %d" % MAX_COND_LEAVES)
    n0 = len(iss)
    _check_obj(spec, dict((k, v) for k, v in c.items() if k != "kind"), path, iss)
    if len(iss) > n0:
        return
    if kind == "pnl_r":
        cmp_ = [k for k in ("gte", "gt", "lte", "lt") if k in c]
        if len(cmp_) != 1:
            _err(iss, "E_SCHEMA", path, "pnl_r требует ровно один из gte/gt/lte/lt")
    elif kind == "price_in_zone":
        if c["low"] >= c["high"]:
            _err(iss, "E_RANGE", path, "low должен быть меньше high")
    elif kind in ("target_filled", "near_target"):
        if st.get("targets") is not None and c["target"] not in st["targets"]:
            _err(iss, "E_ID", _j(path, "target"), "цель %s не найдена в targets" % c["target"])
    st.setdefault("leaf_list", []).append((path, c))


def _walk_leaves(c, depth=0):
    """Все листья условия (включая шаги seq) без проверок: для семантических функций. Возвращает [leaf]."""
    out = []
    if depth > MAX_COND_DEPTH + 2 or not isinstance(c, dict):
        return out
    if c.get("kind") == SEQ_KIND:
        for s in c["steps"] if isinstance(c.get("steps"), list) else []:
            out.extend(_walk_leaves(s, depth + 1))
        return out
    for k in ("all", "any"):
        if isinstance(c.get(k), list):
            for s in c[k]:
                out.extend(_walk_leaves(s, depth + 1))
            return out
    if "not" in c:
        return _walk_leaves(c["not"], depth + 1)
    if isinstance(c.get("kind"), str):
        out.append(c)
    return out


def has_confirm(cond):
    """True, если условие входа содержит подтверждение свечой/объёмом (candle_close/reject/body, volume_spike) на ВСЕХ ветках any.

    >>> has_confirm({"all": [{"kind": "spread_bps", "lte": 5}, {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 1.0}]})
    True
    """
    def go(c, d):
        if d > MAX_COND_DEPTH + 3 or not isinstance(c, dict):
            return False
        if c.get("kind") == SEQ_KIND:
            return isinstance(c.get("steps"), list) and any(go(s, d + 1) for s in c["steps"])
        if isinstance(c.get("all"), list):
            return any(go(s, d + 1) for s in c["all"])
        if isinstance(c.get("any"), list):
            return bool(c["any"]) and all(go(s, d + 1) for s in c["any"])
        if "not" in c:
            return False
        return c.get("kind") in CONFIRM_KINDS
    return go(cond, 0)


def _gated_min_r(when):
    """Максимальный порог x из конъюнкта pnl_r gte x в `when` (сам лист или верхний all); None, если конъюнкта нет."""
    leaves = []
    if isinstance(when, dict):
        if when.get("kind") == "pnl_r":
            leaves = [when]
        elif isinstance(when.get("all"), list):
            leaves = [x for x in when["all"] if isinstance(x, dict) and x.get("kind") == "pnl_r"]
    xs = [x["gte"] for x in leaves if _fin(x.get("gte"))]
    return max(xs) if xs else None


def _check_action(a, path, iss, plan):
    """Параметры действия по ACTION_FIELDS + взаимные правила; для move_stop сверка с начальным стопом (E_STOP_WIDEN)."""
    if not isinstance(a, dict):
        _err(iss, "E_TYPE", path, "do должен быть объектом")
        return
    name = a.get("action")
    if not isinstance(name, str):
        _err(iss, "E_SCHEMA", _j(path, "action"), "у действия нет action")
        return
    spec = ACTION_FIELDS.get(name)
    if spec is None:
        _err(iss, "E_UNKNOWN_FIELD", _j(path, "action"), "неизвестное действие %r" % name[:40])
        return
    n0 = len(iss)
    _check_obj(spec, dict((k, v) for k, v in a.items() if k != "action"), path, iss)
    if len(iss) > n0:
        return
    if name == "move_stop":
        to = a["to"]
        need = _MOVE_STOP_PARAM.get(to)
        if need and need not in a:
            _err(iss, "E_SCHEMA", _j(path, need), "to=%s требует параметр %s" % (to, need))
            return
        for p in ("price", "r", "pct"):
            if p in a and p != need:
                _err(iss, "E_SCHEMA", _j(path, p), "параметр не применим при to=%s" % to)
                return
        if "offset_bps" in a and to != "breakeven":
            _err(iss, "E_SCHEMA", _j(path, "offset_bps"), "offset_bps только для to=breakeven")
            return
        if to == "price":
            sp = ((plan.get("stop") or {}).get("price")) if isinstance(plan.get("stop"), dict) else None
            if _fin(sp):
                if (plan.get("side") == "long" and a["price"] < sp) or (plan.get("side") == "short" and a["price"] > sp):
                    _err(iss, "E_STOP_WIDEN", _j(path, "price"), "цена стопа %s шире начального %s" % (a["price"], sp))
    elif name == "trail":
        req, opt = _TRAIL_MODE_PARAMS[a["mode"]]
        for p in req:
            if p not in a:
                _err(iss, "E_SCHEMA", _j(path, p), "trail mode=%s требует %s" % (a["mode"], p))
                return
        for p in a:
            if p not in ("action", "mode") and p not in req and p not in opt:
                _err(iss, "E_SCHEMA", _j(path, p), "параметр не применим при mode=%s" % a["mode"])
                return
    elif name == "partial_close":
        both = [p for p in ("frac_of_remaining", "frac_of_initial") if p in a]
        if len(both) != 1:
            _err(iss, "E_SCHEMA", path, "нужен ровно один из frac_of_remaining/frac_of_initial")
    elif name == "add":
        pass


def _check_semantic(plan, iss):
    side = plan.get("side")
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    how = entry.get("how") if isinstance(entry.get("how"), dict) else {}
    htype = how.get("type")
    valid = entry.get("valid") if isinstance(entry.get("valid"), dict) else {}
    stop = plan.get("stop") if isinstance(plan.get("stop"), dict) else {}
    sp = stop.get("price") if _fin(stop.get("price")) else None
    ref = how.get("price") if (htype == "limit" and _fin(how.get("price"))) else (entry.get("ref_price") if _fin(entry.get("ref_price")) else None)

    # режим и сессия
    if plan.get("mode") == "live" and not plan.get("session_type"):
        _err(iss, "E_SCHEMA", "session_type", "для live-плана session_type обязателен")
    src = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    if src.get("kind") == "shadow" and not src.get("parent_plan"):
        _err(iss, "E_SCHEMA", "source.parent_plan", "для source.kind=shadow нужен parent_plan")

    # how
    if htype == "limit" and "price" not in how:
        _err(iss, "E_SCHEMA", "entry.how.price", "для limit обязательна цена")
    if htype in ("market", "adopt"):
        for k in ("price", "post_only", "snap"):
            if k in how:
                _err(iss, "E_SCHEMA", _j("entry.how", k), "поле применимо только к how.type=limit")
    if htype == "adopt":
        if "when" in entry:
            _err(iss, "E_SCHEMA", "entry.when", "для adopt условие входа запрещено")
        for k in ("ref_price", "valid"):
            if k in entry:
                _err(iss, "E_SCHEMA", _j("entry", k), "для adopt поле не применимо")

    # цели
    tg = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    tg = [t for t in tg if isinstance(t, dict)]
    ids = [t.get("id") for t in tg]
    seen = set()
    for i, tid in enumerate(ids):
        if isinstance(tid, str):
            if tid in seen:
                _err(iss, "E_DUP_ID", "targets[%d].id" % i, "повтор id цели %s" % tid)
            seen.add(tid)
    for i, tid in enumerate(ids):
        if isinstance(tid, str) and _rx(RE_TARGET_ID).fullmatch(tid) and tid != "t%d" % (i + 1) and tid not in ids[:i]:
            _err(iss, "E_TARGET_ORDER", "targets[%d].id" % i, "ожидался id t%d: цели идут по порядку от ближней к дальней" % (i + 1))
    prices = [(i, t["price"]) for i, t in enumerate(tg) if _fin(t.get("price"))]
    if side in SIDES:
        for (i, a), (j, b) in zip(prices, prices[1:]):
            if (side == "long" and b <= a) or (side == "short" and b >= a):
                _err(iss, "E_TARGET_ORDER", "targets[%d].price" % j, "цены целей должны %s от ближней к дальней" % ("расти для long" if side == "long" else "убывать для short"))
    fr = [t["frac"] for t in tg if _fin(t.get("frac"))]
    if fr and sum(fr) > 1 + 1e-9:
        _err(iss, "E_FRAC_SUM", "targets", "сумма frac %.4g > 1" % sum(fr))

    # стороны стопа и целей (опорная цена: лимит, ref_price; иначе цели сравниваем со стопом)
    if side in SIDES and sp is not None:
        if ref is not None:
            if (side == "long" and sp >= ref) or (side == "short" and sp <= ref):
                _err(iss, "E_STOP_SIDE", "stop.price", "стоп %s не с той стороны от цены входа %s для %s" % (sp, ref, side))
            for i, a in prices:
                if (side == "long" and a <= ref) or (side == "short" and a >= ref):
                    _err(iss, "E_TARGET_SIDE", "targets[%d].price" % i, "цель %s не с той стороны от цены входа %s" % (a, ref))
        else:
            for i, a in prices:
                if (side == "long" and a <= sp) or (side == "short" and a >= sp):
                    _err(iss, "E_TARGET_SIDE", "targets[%d].price" % i, "цель %s не с той стороны от стопа %s" % (a, sp))

    # cancel-уровни
    ca, cb = valid.get("cancel_above"), valid.get("cancel_below")
    if _fin(ca) and _fin(cb) and cb >= ca:
        _err(iss, "E_CANCEL_LEVELS", "entry.valid", "cancel_below должен быть ниже cancel_above")
    if ref is not None:
        if _fin(ca) and ca <= ref:
            _err(iss, "E_CANCEL_LEVELS", "entry.valid.cancel_above", "cancel_above %s не выше цены входа %s" % (ca, ref))
        if _fin(cb) and cb >= ref:
            _err(iss, "E_CANCEL_LEVELS", "entry.valid.cancel_below", "cancel_below %s не ниже цены входа %s" % (cb, ref))

    # ttl
    lc = plan.get("lifecycle") if isinstance(plan.get("lifecycle"), dict) else {}
    if _fin(valid.get("ttl_min")) and _fin(lc.get("ttl_min")) and valid["ttl_min"] > lc["ttl_min"]:
        _err(iss, "E_TTL", "entry.valid.ttl_min", "ttl входа %s больше lifecycle.ttl_min %s" % (valid["ttl_min"], lc["ttl_min"]))

    # условия
    tgt_ids = set(i for i in ids if isinstance(i, str))
    st = {"leaves": 0, "targets": tgt_ids}
    if "when" in entry:
        _place_cond(entry["when"], WHERE_ENTRY, "entry.when", iss, st)
    inv = plan.get("invalidation") if isinstance(plan.get("invalidation"), list) else []
    for i, c in enumerate(inv[:MAX_INVALIDATION]):
        _place_cond(c, WHERE_INVALIDATION, "invalidation[%d]" % i, iss, {"leaves": 0, "targets": tgt_ids})

    # manage
    mg = plan.get("manage") if isinstance(plan.get("manage"), list) else []
    rseen = set()
    has_trail = False
    for i, r in enumerate(mg[:MAX_MANAGE]):
        if not isinstance(r, dict):
            continue
        p = "manage[%d]" % i
        rid = r.get("id")
        if isinstance(rid, str):
            if rid in rseen:
                _err(iss, "E_DUP_ID", p + ".id", "повтор id правила %s" % rid)
            rseen.add(rid)
        if "when" in r:
            _place_cond(r["when"], WHERE_MANAGE, p + ".when", iss, {"leaves": 0, "targets": tgt_ids})
        if "do" in r:
            _check_action(r["do"], p + ".do", iss, plan)
            do = r["do"] if isinstance(r["do"], dict) else {}
            if do.get("action") == "trail":
                has_trail = True
            if do.get("action") == "move_target" and isinstance(do.get("target"), str) and do["target"] not in tgt_ids:
                _err(iss, "E_ID", p + ".do.target", "цель %s не найдена в targets" % do["target"])
            if do.get("action") == "add":
                x = _gated_min_r(r.get("when"))
                if x is None:
                    _err(iss, "E_ADD_NOT_PROFIT_GATED", p + ".when", "add обязан содержать конъюнкт pnl_r gte x")
                elif x <= 0:
                    _err(iss, "E_ADD_NOT_PROFIT_GATED", p + ".when", "порог pnl_r gte должен быть > 0")
                if _fin(do.get("risk_frac")) and do["risk_frac"] > 1:
                    _err(iss, "E_ADD_RISK", p + ".do.risk_frac", "risk_frac больше 1")
        if r.get("once") is True and _fin(r.get("max_times")) and r["max_times"] != 1:
            _err(iss, "E_RANGE", p + ".max_times", "при once=true max_times может быть только 1")

    # подтверждение входа
    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    if htype in ("market", "limit") and not has_confirm(entry.get("when")) and risk.get("confirm_waiver") is not True:
        _err(iss, "E_NO_CONFIRM", "risk.confirm_waiver", "вход без условия подтверждения требует risk.confirm_waiver=true и size_mult <= 0.5")

    # shadow
    sv = plan.get("shadow_variants") if isinstance(plan.get("shadow_variants"), list) else []
    names = set()
    for i, v in enumerate(sv[:MAX_SHADOW_VARIANTS]):
        if not isinstance(v, dict):
            continue
        nm = v.get("name")
        if isinstance(nm, str):
            if nm in names:
                _err(iss, "E_DUP_ID", "shadow_variants[%d].name" % i, "повтор имени варианта")
            names.add(nm)
        pt = v.get("patch")
        if "patch" in v and not isinstance(pt, dict):
            _err(iss, "E_TYPE", "shadow_variants[%d].patch" % i, "patch должен быть объектом")
        elif isinstance(pt, dict):
            for k in pt:
                if k not in SHADOW_PATCH_KEYS:
                    _err(iss, "E_UNKNOWN_FIELD", "shadow_variants[%d].patch.%s" % (i, k), "поле нельзя менять в теневом варианте")

    # предупреждения
    if not inv:
        _warn(iss, "W_NO_INVALIDATION", "invalidation", "нет условий инвалидации тезиса")
    if fr and sum(fr) < 1 - 1e-9 and not has_trail and not _fin(lc.get("max_hold_min")):
        _warn(iss, "W_NO_RUNNER_RULE", "manage", "есть бегун (frac<1), но нет трейла и lifecycle.max_hold_min")


def validate_static(plan):
    """-> [Issue]: структура, типы, диапазоны, словарь условий и действий, E_UNKNOWN_FIELD, размеры, смысл без сети и файлов (01 п. 1.8).

    Включает локальные проверки смысла: стороны стопа и целей, порядок и доли целей, cancel-уровни, E_NO_CONFIRM (waiver), E_STOP_WIDEN
    для move_stop price, E_ADD_NOT_PROFIT_GATED, E_DUP_ID, предупреждения W_NO_INVALIDATION и W_NO_RUNNER_RULE. Не бросает исключений.

    >>> [i["code"] for i in validate_static({"schema": "tb.plan/1"})][:2]
    ['E_SCHEMA', 'E_SCHEMA']
    """
    iss = []
    if not isinstance(plan, dict):
        _err(iss, "E_TYPE", "", "план должен быть JSON-объектом")
        return iss
    if _too_deep(plan):
        _err(iss, "E_TOO_LARGE", "", "вложенность глубже %d" % MAX_DEPTH)
        return iss
    try:
        size = len(json.dumps(plan, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _err(iss, "E_TYPE", "", "план не сериализуется в JSON")
        return iss
    if size > MAX_PLAN_BYTES:
        _err(iss, "E_TOO_LARGE", "", "размер плана %d байт > %d" % (size, MAX_PLAN_BYTES))
    try:
        _check_obj(FIELDS, plan, "", iss)
        # условия и действия (узлы cond/action) проверяет семантический проход
        _check_semantic(plan, iss)
    except RecursionError:
        _err(iss, "E_TOO_LARGE", "", "слишком глубокая вложенность")
    return _dedup(iss)


def _dedup(iss):
    seen, out = set(), []
    for i in iss:
        k = (i["code"], i["path"], i["msg"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


# =====================================================================================================================
# validate_context
# =====================================================================================================================
def _lim(limits, path, default):
    cur = limits
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else cur


def _fnum(v, default=None):
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return default
    return float(v) if _fin(v) else default


def entry_ref(plan, price=None):
    """Опорная цена входа: цена лимита, иначе entry.ref_price, иначе переданная живая цена; None, если нет ничего."""
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    how = entry.get("how") if isinstance(entry.get("how"), dict) else {}
    for v in (how.get("price") if how.get("type") == "limit" else None, entry.get("ref_price"), price):
        if _fin(v) and v > 0:
            return float(v)
    return None


def rr_preview(plan, ref, profile=None):
    """-> {"risk_pct","reward_pct","cost_pct","net_rr"} или None. Издержки круга = 2 x (fee + slip) из profile.costs (bybit, иначе platform).

    Награда = средневзвешенная цель по frac, бегун (1 - sum frac) считается на ПОСЛЕДНЕМ тейке (01 п. 1.4). Это оценка валидатора;
    окончательный расчёт делает guard.

    >>> p = {"side": "short", "stop": {"price": 143.0}, "targets": [{"price": 141.2, "frac": 0.5}, {"price": 140.2, "frac": 0.5}]}
    >>> round(rr_preview(p, 142.3, {"costs": {"bybit": {"fee": 0.001, "slip": 0.0002}}})["net_rr"], 2)
    1.21
    """
    try:
        sp = plan["stop"]["price"]
        tg = [t for t in plan["targets"]]
        if not (_fin(ref) and ref > 0 and _fin(sp)):
            return None
        fr = [float(t["frac"]) for t in tg]
        px = [float(t["price"]) for t in tg]
    except (KeyError, TypeError, ValueError):
        return None
    if not px or sum(fr) <= 0:
        return None
    sgn = 1.0 if plan.get("side") == "long" else -1.0
    runner = max(0.0, 1.0 - sum(fr))
    reward = sum(f * sgn * (p - ref) for f, p in zip(fr, px)) + runner * sgn * (px[-1] - ref)
    reward_pct = reward / ref * 100.0
    risk_pct = sgn * (ref - sp) / ref * 100.0
    prof = profile or {}
    costs = prof.get("costs") if isinstance(prof.get("costs"), dict) else {}
    c = costs.get("bybit") or costs.get("platform") or {}
    fee = _fnum(c.get("fee"), _fnum(prof.get("taker_fee_pct"), 0.0009))
    slip = _fnum(c.get("slip"), 0.0)
    cost_pct = 2.0 * (fee + slip) * 100.0
    den = risk_pct + cost_pct
    return {"risk_pct": risk_pct, "reward_pct": reward_pct, "cost_pct": cost_pct,
            "net_rr": ((reward_pct - cost_pct) / den) if den > 0 else None}


def ladder_parts(qty, fracs, qty_step, min_qty=0.0, min_notional=0.0, prices=None):
    """Раскладка количества по ступеням лесенки (01 п. 1.4): округление вниз по шагу, последняя ступень берёт остаток (если sum frac == 1),
    ступень меньше min_qty (или номинала < min_notional) сливается со следующей. -> [qty_части] (меньше входных ступеней, если были слияния).

    >>> ladder_parts(0.3, [0.5, 0.5], 0.1, 0.1)
    [0.1, 0.2]
    >>> ladder_parts(0.1, [0.5, 0.5], 0.1, 0.1)
    [0.1]
    """
    if not (_fin(qty) and qty > 0 and _fin(qty_step) and qty_step > 0):
        return []
    if qty / qty_step > 1e12 or not all(_fin(f) and 0 < f <= 1 for f in fracs):
        return []
    total = int(math.floor(qty / qty_step + 1e-9))
    full = sum(fracs) >= 1 - 1e-9
    steps = []
    for i, f in enumerate(fracs):
        steps.append(int(math.floor(total * f + 1e-9)))
    if full and steps:
        steps[-1] = total - sum(steps[:-1])
    minq = int(math.ceil((min_qty or 0.0) / qty_step - 1e-9))
    parts, carry = [], 0
    for i, s in enumerate(steps):
        s += carry
        carry = 0
        px = prices[i] if prices and i < len(prices) and _fin(prices[i]) else None
        small = s < minq or (px and min_notional and s * qty_step * px < min_notional)
        if small and i < len(steps) - 1:
            carry = s
            continue
        parts.append(s)
    if parts and (parts[-1] < minq or parts[-1] <= 0) and len(parts) > 1:
        last = parts.pop()
        parts[-1] += last
    parts = [p for p in parts if p > 0]
    return [round(p * qty_step, 12) for p in parts]


def validate_context(plan, ctx):
    """-> [Issue]. Контекстные проверки (01 п. 1.8 уровень 2). ctx: {"now","limits","session","instrument","price","profile","active_plans","cooldowns"};
    необязательные ключи: "guard" ({"ok","reasons"[,"qty"]}), "position" (для adopt: {"side","sl",...}), "price_age_s", "submits_1h".
    Отсутствующий ключ = проверка пропускается; ключ со значением None = «данных нет» (session None = E_NO_SESSION, instrument None = E_SYMBOL_UNKNOWN,
    price None = W_PRICE_STALE_AT_ACCEPT). cooldowns: {"symbols": {"SOL": until_epoch}, "global": until_epoch}. active_plans: список spec (или {"spec":..})
    со статусом в поле "status" (если нет, план считается активным). Структурные проверки здесь не повторяются (validate_static).
    """
    iss = []
    if not isinstance(plan, dict) or not isinstance(ctx, dict):
        _err(iss, "E_TYPE", "", "plan и ctx должны быть объектами")
        return iss
    limits = ctx.get("limits") if isinstance(ctx.get("limits"), dict) else {}
    now = ctx.get("now")
    mode = plan.get("mode", "live") if isinstance(plan.get("mode", "live"), str) else "live"
    stype = plan.get("session_type") if isinstance(plan.get("session_type"), str) else None
    sym = plan.get("symbol") if isinstance(plan.get("symbol"), str) else None
    side = plan.get("side") if isinstance(plan.get("side"), str) else None
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    how = entry.get("how") if isinstance(entry.get("how"), dict) else {}
    valid = entry.get("valid") if isinstance(entry.get("valid"), dict) else {}
    lc = plan.get("lifecycle") if isinstance(plan.get("lifecycle"), dict) else {}
    stop = plan.get("stop") if isinstance(plan.get("stop"), dict) else {}
    sp = stop.get("price") if _fin(stop.get("price")) else None
    tg = [t for t in (plan.get("targets") or []) if isinstance(t, dict)] if isinstance(plan.get("targets"), list) else []
    price = _fnum(ctx.get("price")) if "price" in ctx else None

    # --- сессия (только live)
    if mode == "live" and "session" in ctx:
        sess = ctx.get("session")
        if not isinstance(sess, dict):
            _err(iss, "E_NO_SESSION", "session_type", "нет активной торговой сессии")
        else:
            if sess.get("type") not in (stype, "day"):          # сессия-день (дашборд): горизонт у каждого плана свой; сессия-горизонт (CLI): один на все планы
                _err(iss, "E_SESSION_MISMATCH", "session_type", "тип плана %r, активная сессия %r" % (stype, sess.get("type")))

    # --- инструмент
    if "instrument" in ctx and ctx.get("instrument") is None:
        _err(iss, "E_SYMBOL_UNKNOWN", "symbol", "инструмент %s не найден на бирже" % sym)

    # --- цена: свежесть, стороны, дистанции
    stale_s = _lim(limits, ("connectivity", "public_price_stale_s"), 10)
    if "price" in ctx and (price is None or price <= 0):
        _warn(iss, "W_PRICE_STALE_AT_ACCEPT", "price", "нет свежей цены при приёме: проверки по цене пропущены")
        price = None
    elif _fin(ctx.get("price_age_s")) and ctx["price_age_s"] > stale_s:
        _warn(iss, "W_PRICE_STALE_AT_ACCEPT", "price", "цена старше %s с" % stale_s)
    max_dist = _lim(limits, ("entry", "max_level_distance_pct"), 25)
    if price:
        def far(level, path):
            if _fin(level) and abs(level - price) / price * 100.0 > max_dist:
                _err(iss, "E_LEVEL_DISTANCE", path, "уровень %s дальше %s%% от цены %s" % (level, max_dist, price))
        far(sp, "stop.price")
        for i, t in enumerate(tg):
            far(t.get("price"), "targets[%d].price" % i)
        far(entry.get("ref_price"), "entry.ref_price")
        far(how.get("price") if how.get("type") == "limit" else None, "entry.how.price")
        far(valid.get("cancel_above"), "entry.valid.cancel_above")
        far(valid.get("cancel_below"), "entry.valid.cancel_below")
        inv = plan.get("invalidation") if isinstance(plan.get("invalidation"), list) else []
        conds = [("entry.when", entry.get("when"))] + [("invalidation[%d]" % i, c) for i, c in enumerate(inv)]
        for p, c in conds:
            for lf in _walk_leaves(c):
                for k in ("level", "low", "high"):
                    far(lf.get(k), "%s.%s" % (p, k))
        if side in SIDES and sp is not None:
            if (side == "long" and sp >= price) or (side == "short" and sp <= price):
                _err(iss, "E_STOP_SIDE", "stop.price", "стоп %s не с той стороны от живой цены %s" % (sp, price))
            for i, t in enumerate(tg):
                if _fin(t.get("price")) and ((side == "long" and t["price"] <= price) or (side == "short" and t["price"] >= price)):
                    _err(iss, "E_TARGET_SIDE", "targets[%d].price" % i, "цель %s не с той стороны от живой цены %s" % (t["price"], price))
        ca, cb = valid.get("cancel_above"), valid.get("cancel_below")
        if _fin(ca) and ca <= price:
            _err(iss, "E_CANCEL_LEVELS", "entry.valid.cancel_above", "cancel_above %s не выше текущей цены %s" % (ca, price))
        if _fin(cb) and cb >= price:
            _err(iss, "E_CANCEL_LEVELS", "entry.valid.cancel_below", "cancel_below %s не ниже текущей цены %s" % (cb, price))

    # --- риск-геометрия по профилю
    prof = ctx.get("profile") if isinstance(ctx.get("profile"), dict) else {}
    st = (prof.get("session_types") or {}).get(stype) if isinstance(prof.get("session_types"), dict) else None
    st = st if isinstance(st, dict) else {}
    ref = entry_ref(plan, price)
    if prof and ref and sp is not None and side in SIDES:
        max_stop = _fnum(st.get("max_stop_pct"), _fnum(prof.get("max_stop_pct")))
        min_rr = _fnum(st.get("min_rr"), _fnum(prof.get("min_rr")))
        pv = rr_preview(plan, ref, prof)
        if pv and max_stop is not None and pv["risk_pct"] > max_stop:
            _err(iss, "E_STOP_PCT", "stop.price", "стоп %.2f%% от входа больше лимита %.2f%% для %s" % (pv["risk_pct"], max_stop, stype))
        if pv and min_rr is not None and tg and (pv["net_rr"] is None or pv["net_rr"] < min_rr):
            _err(iss, "E_RR", "targets", "net RR %s < min_rr %.2f для %s (издержки круга %.2f%%)" % (
                "n/a" if pv["net_rr"] is None else "%.2f" % pv["net_rr"], min_rr, stype, pv["cost_pct"]))

    # --- подтверждение входа: ограничение размера
    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    cap = _lim(limits, ("entry", "no_confirm_max_size_mult"), 0.5)
    if how.get("type") in ("market", "limit") and not has_confirm(entry.get("when")):
        sm = risk.get("size_mult", 1.0)
        if risk.get("confirm_waiver") is not True or (_fin(sm) and sm > cap):
            _err(iss, "E_NO_CONFIRM", "risk.size_mult", "вход без подтверждения: confirm_waiver=true и size_mult <= %s" % cap)
    # max_chase_bps выше потолка exec_limits НЕ ошибка (пример интрадея из 01 п. 1.10 использует 20 при умолчании 15): потолок применяет исполнитель

    # --- пирамидинг
    pyr = _lim(limits, ("pyramid",), None)
    if isinstance(pyr, dict):
        for i, r in enumerate(plan.get("manage") or []) if isinstance(plan.get("manage"), list) else []:
            do = r.get("do") if isinstance(r, dict) and isinstance(r.get("do"), dict) else {}
            if do.get("action") != "add":
                continue
            p = "manage[%d]" % i
            if not pyr.get("enabled", False):
                _warn(iss, "W_ADD_DISABLED", p, "пирамидинг выключен (pyramid.enabled=false): правило будет пропускаться")
            x = _gated_min_r(r.get("when"))
            mr = _fnum(pyr.get("min_r"))
            if x is not None and mr is not None and x < mr:
                _err(iss, "E_ADD_NOT_PROFIT_GATED", p + ".when", "порог pnl_r %s ниже pyramid.min_r %s" % (x, mr))
            mx = _fnum(pyr.get("max_add_risk_frac"))
            if mx is not None and _fin(do.get("risk_frac")) and do["risk_frac"] > mx:
                _err(iss, "E_ADD_RISK", p + ".do.risk_frac", "risk_frac %s > max_add_risk_frac %s" % (do["risk_frac"], mx))

    # --- adopt
    if how.get("type") == "adopt" and "position" in ctx:
        pos = ctx.get("position")
        if not isinstance(pos, dict) or not _fin(pos.get("size", 1)) or pos.get("size", 1) == 0:
            _err(iss, "E_ADOPT_NO_POSITION", "entry.how.type", "по %s нет открытой позиции" % sym)
        else:
            if pos.get("side") not in (None, side):
                _err(iss, "E_ADOPT_NO_POSITION", "side", "сторона плана не совпадает с позицией")
            ex = _fnum(pos.get("sl"), 0.0)
            if not ex or ex <= 0:
                _err(iss, "E_ADOPT_NO_STOP", "stop.price", "на бирже у позиции нет стопа")
            elif sp is not None and ((side == "long" and sp < ex) or (side == "short" and sp > ex)):
                _err(iss, "E_STOP_WIDEN", "stop.price", "стоп плана %s слабее биржевого %s" % (sp, ex))

    # --- лесенка тейков
    inst, g = ctx.get("instrument"), ctx.get("guard")
    if isinstance(inst, dict) and isinstance(g, dict) and _fnum(g.get("qty")) and len(tg) > 1:
        step = _fnum(inst.get("qty_step"))
        if step:
            parts = ladder_parts(_fnum(g["qty"]), [t["frac"] for t in tg if _fin(t.get("frac"))], step, _fnum(inst.get("min_qty"), 0.0),
                                 _fnum(inst.get("min_notional"), 0.0), [t.get("price") for t in tg])
            if 0 < len(parts) < len(tg):
                _warn(iss, "W_LADDER_COLLAPSE", "targets", "лесенка из %d целей вырождается в %d част. (qty %s, шаг %s)" % (len(tg), len(parts), g["qty"], step))

    # --- guard
    if isinstance(g, dict) and g.get("ok") is False:
        rs = g.get("reasons")
        _err(iss, "E_GUARD", "guard", "guard отказал: %s" % (", ".join(str(x) for x in rs)[:200] if isinstance(rs, (list, tuple)) and rs else "без причины"))

    # --- защиты: пауза, лимиты, занятость монеты
    cd = ctx.get("cooldowns") if isinstance(ctx.get("cooldowns"), dict) else {}
    if _fin(now) and mode == "live":
        syms = cd.get("symbols") if isinstance(cd.get("symbols"), dict) else {}
        until = _fnum(syms.get(sym))
        if until is not None and until > now:
            _err(iss, "E_COOLDOWN", "symbol", "пауза по %s ещё %.0f мин после стопа" % (sym, (until - now) / 60.0))
        gl = _fnum(cd.get("global"))
        if gl is not None and gl > now:
            _err(iss, "E_COOLDOWN", "symbol", "общая пауза после серии стопов ещё %.0f мин" % ((gl - now) / 60.0))
    active = []
    for a in ctx.get("active_plans") or [] if isinstance(ctx.get("active_plans"), list) else []:
        sp_ = a.get("spec") if isinstance(a, dict) and isinstance(a.get("spec"), dict) else a
        if not isinstance(sp_, dict):
            continue
        status = a.get("status") if isinstance(a, dict) and "status" in a else sp_.get("status", "armed")
        if status in TERMINAL:
            continue
        active.append(sp_)
    same_mode = [a for a in active if a.get("mode", "live") == mode]
    cap_n = _lim(limits, ("protections", "max_plans_active"), 6) if mode == "live" else _lim(limits, ("paper", "max_active"), 20)
    if _fin(cap_n) and len(same_mode) >= cap_n:
        _err(iss, "E_LIMIT_PLANS", "symbol", "активных планов %d >= лимита %s" % (len(same_mode), cap_n))
    sub_cap = _lim(limits, ("protections", "max_submits_per_hour"), 20)
    if _fin(ctx.get("submits_1h")) and _fin(sub_cap) and ctx["submits_1h"] >= sub_cap:
        _err(iss, "E_LIMIT_PLANS", "symbol", "подач за час %s >= лимита %s" % (ctx["submits_1h"], sub_cap))
    if mode == "live" and any(a.get("symbol") == sym for a in same_mode):
        _err(iss, "E_SYMBOL_BUSY", "symbol", "по %s уже есть активный план/позиция: усреднения нет" % sym)
    return _dedup(iss)


# =====================================================================================================================
# amend
# =====================================================================================================================
def _tighter(side, new, cur):
    """True, если new не слабее cur (long: выше или равен, short: ниже или равен)."""
    return new >= cur if side == "long" else new <= cur


def _getp(d, *path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _tmap(spec):
    """{id цели: цель} для целей с валидным строковым id."""
    ts = spec.get("targets") if isinstance(spec.get("targets"), list) else []
    return dict((t["id"], t) for t in ts if isinstance(t, dict) and isinstance(t.get("id"), str))


def validate_amend(old_spec, new_spec, status, live):
    """-> [Issue]. Инварианты изменения плана (01 п. 1.7), сравнение new c old И с живым состоянием исполнителя. Структуру new проверяет validate_static
    (вызывающий делает normalize -> validate_static -> validate_amend; см. amend()).

    live: {"exch_stop": float|None, "position": bool, "stop_at_or_better_be": bool}; необязательные: "filled_targets": [id], "has_order": bool
    (для armed: есть ли уже заявка; по умолчанию True = осторожно). Стоп в позиции только в сторону уменьшения риска ОТНОСИТЕЛЬНО БИРЖЕВОГО стопа
    (exch_stop, иначе старый stop.price); снять нельзя. Терминальные статусы: любое изменение = E_STATUS_FORBIDS.

    >>> o = {"side": "long", "stop": {"price": 100.0}}
    >>> [i["code"] for i in validate_amend(o, {"side": "long", "stop": {"price": 99.0}}, "managing", {"exch_stop": 100.0})]
    ['E_STOP_WIDEN']
    """
    iss = []
    live = live if isinstance(live, dict) else {}
    if not isinstance(old_spec, dict) or not isinstance(new_spec, dict):
        return [issue("E_TYPE", "error", "", "old/new должны быть объектами")]
    if status not in STATUSES:
        return [issue("E_STATUS_FORBIDS", "error", "status", "неизвестный статус %r" % (status,))]
    changed = set(k for k in set(old_spec) | set(new_spec) if not _jeq(old_spec.get(k), new_spec.get(k)))
    changed.discard("rev")
    changed.discard("guard_preview")
    if not changed:
        return iss
    if status in TERMINAL:
        return [issue("E_STATUS_FORBIDS", "error", "status", "план в терминальном статусе %s: правки запрещены" % status)]
    pos = status in ("entered", "managing")
    be = bool(live.get("stop_at_or_better_be"))
    side = old_spec.get("side")

    for k in ("symbol", "side", "mode", "venue", "source", "session_type", "schema", "id", "created_ts", "created_by", "armed_ts", "prereg_hash"):
        if k in changed:
            _err(iss, "E_IMMUTABLE", k, "поле неизменяемо")
    if "tags" in changed and status != "draft" and not _jeq(_getp(old_spec, "tags", "experiment"), _getp(new_spec, "tags", "experiment")):
        _err(iss, "E_IMMUTABLE", "tags.experiment", "эксперимент нельзя менять после armed (предрегистрация)")

    if "entry" in changed and pos:
        _err(iss, "E_STATUS_FORBIDS", "entry", "вход уже исполнен: entry менять нельзя")

    # --- стоп
    if "stop" in changed:
        newp, oldp = _getp(new_spec, "stop", "price"), _getp(old_spec, "stop", "price")
        if pos:
            ex = live.get("exch_stop")
            cur = ex if _fin(ex) and ex > 0 else oldp
            if not _fin(newp) or newp <= 0:
                _err(iss, "E_STOP_WIDEN", "stop.price", "стоп снять нельзя")
            elif _fin(cur) and side in SIDES and not _tighter(side, newp, cur):
                _err(iss, "E_STOP_WIDEN", "stop.price", "новый стоп %s слабее текущего %s: только в сторону уменьшения риска" % (newp, cur))
            if isinstance(new_spec.get("stop"), dict) and _getp(new_spec, "stop", "trigger_by") != _getp(old_spec, "stop", "trigger_by"):
                _err(iss, "E_STATUS_FORBIDS", "stop.trigger_by", "тип триггера стопа в позиции менять нельзя")
        elif not _fin(newp) or newp <= 0:
            _err(iss, "E_STOP_WIDEN", "stop.price", "стоп снять нельзя")

    # --- цели
    if "targets" in changed:
        ot, nt = _tmap(old_spec), _tmap(new_spec)
        filled = set(live.get("filled_targets") or [])
        if pos:
            for tid, t in ot.items():
                n = nt.get(tid)
                if tid in filled and (n is None or not _jeq(n, t)):
                    _err(iss, "E_IMMUTABLE", "targets.%s" % tid, "исполненную цель менять нельзя")
                    continue
                if n is None or not _fin(t.get("price")) or not _fin(n.get("price")):
                    continue
                farther = n["price"] > t["price"] if side == "long" else n["price"] < t["price"]
                if farther and not be:
                    _err(iss, "E_TARGET_FARTHER_NOT_RISKFREE", "targets.%s.price" % tid, "цель можно отодвинуть только при стопе в безубытке или лучше")
                if _fin(t.get("frac")) and _fin(n.get("frac")) and n["frac"] > t["frac"] + 1e-12 and not be:
                    _err(iss, "E_TARGET_FARTHER_NOT_RISKFREE", "targets.%s.frac" % tid, "увеличить долю цели можно только при стопе в безубытке или лучше")

    # --- риск
    if "risk" in changed:
        orr = old_spec.get("risk") if isinstance(old_spec.get("risk"), dict) else {}
        nrr = new_spec.get("risk") if isinstance(new_spec.get("risk"), dict) else {}
        if pos:
            _err(iss, "E_STATUS_FORBIDS", "risk", "в позиции risk менять нельзя")
        elif status == "armed":
            osm, nsm = orr.get("size_mult", 1.0), nrr.get("size_mult", 1.0)
            if live.get("has_order", True) and _fin(osm) and _fin(nsm) and nsm > osm + 1e-12:
                _err(iss, "E_RISK_UP", "risk.size_mult", "size_mult можно только уменьшать (%s -> %s)" % (osm, nsm))

    # --- сопровождение
    # manage, invalidation, thesis: свободно в draft/armed/entered/managing (проверки содержимого делает validate_static, в т.ч. E_STOP_WIDEN, E_ADD_*)

    # --- lifecycle
    if "lifecycle" in changed:
        ol = old_spec.get("lifecycle") if isinstance(old_spec.get("lifecycle"), dict) else {}
        nl = new_spec.get("lifecycle") if isinstance(new_spec.get("lifecycle"), dict) else {}
        if pos:
            for k in ("ttl_min", "not_before_ts"):
                if not _jeq(ol.get(k), nl.get(k)):
                    _err(iss, "E_STATUS_FORBIDS", "lifecycle.%s" % k, "в позиции параметр до входа менять нельзя")
            oh, nh = ol.get("max_hold_min"), nl.get("max_hold_min")
            if not _jeq(oh, nh):
                extend = (not _fin(nh)) or (_fin(oh) and nh > oh)
                if extend and not be:
                    _err(iss, "E_HOLD_EXTEND", "lifecycle.max_hold_min", "продлить удержание можно только при стопе в безубытке или лучше")
            if ol.get("flatten_at_session_end") is True and nl.get("flatten_at_session_end") is False and not be:
                _err(iss, "E_HOLD_EXTEND", "lifecycle.flatten_at_session_end", "отключить закрытие в конце сессии можно только при стопе в безубытке или лучше")

    if "shadow_variants" in changed and pos:
        _err(iss, "E_STATUS_FORBIDS", "shadow_variants", "в позиции менять теневые варианты нельзя")
    for k in changed:
        if k not in FIELDS:
            _err(iss, "E_UNKNOWN_FIELD", k, "неизвестное поле")
    return _dedup(iss)


def merge_patch(spec, patch):
    """RFC 7386 JSON Merge Patch -> новый spec (аргументы не мутируются). null удаляет ключ, массивы заменяются целиком, не-объект заменяет значение.
    Слишком глубокий patch (> MAX_DEPTH) -> ValueError.

    >>> merge_patch({"a": 1, "b": {"c": 2}}, {"b": {"c": None, "d": 3}, "a": None})
    {'b': {'d': 3}}
    """
    if _too_deep(patch) or _too_deep(spec):
        raise ValueError("слишком глубокая вложенность")
    return _merge(spec, patch)


def _merge(target, patch):
    if isinstance(patch, dict):
        out = dict((k, copy.deepcopy(v)) for k, v in target.items()) if isinstance(target, dict) else {}
        for k, v in patch.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = _merge(out.get(k), v)
        return out
    return copy.deepcopy(patch)


def diff(old, new):
    """-> [{"path","op":"add|remove|change","old","new"}]: различия двух spec по листьям; массивы сравниваются целиком (как при merge patch).

    >>> diff({"a": {"b": 1}}, {"a": {"b": 2}})
    [{'path': 'a.b', 'op': 'change', 'old': 1, 'new': 2}]
    """
    out = []

    def go(o, n, path, d):
        if d > MAX_DEPTH:
            return
        if isinstance(o, dict) and isinstance(n, dict):
            for k in sorted(set(o) | set(n), key=str):
                p = _j(path, k)
                if k not in n:
                    out.append({"path": p, "op": "remove", "old": o[k], "new": None})
                elif k not in o:
                    out.append({"path": p, "op": "add", "old": None, "new": n[k]})
                else:
                    go(o[k], n[k], p, d + 1)
        elif not _jeq(o, n):
            out.append({"path": path, "op": "change", "old": o, "new": n})
    go(old, new, "", 0)
    return out


def amend(spec, patch, status, live, rev_expected, now=None):
    """-> (new_spec | None, issues). Полный конвейер правки: rev_expected -> статус -> merge_patch -> normalize -> validate_static -> validate_amend.
    Атомарно: при любой error new_spec = None и ничего не применено. rev в new_spec не растёт (это делает PlanStore.save_spec); поля исполнителя
    в patch игнорируются с W_FIELD_IGNORED.

    >>> s = {"schema": "tb.plan/1", "rev": 2}
    >>> amend(s, {}, "armed", {}, 1)[0] is None
    True
    """
    iss = check_rev(spec, rev_expected)
    if iss:
        return None, iss
    if status in TERMINAL:
        return None, [issue("E_STATUS_FORBIDS", "error", "status", "план в терминальном статусе %s: правки запрещены" % status)]
    if not isinstance(patch, dict):
        return None, [issue("E_TYPE", "error", "", "patch должен быть объектом")]
    patch = dict(patch)
    for k in EXEC_FIELDS:
        if k in patch:
            del patch[k]
            iss.append(issue("W_FIELD_IGNORED", "warn", k, "поле выставляет исполнитель, значение проигнорировано"))
    try:
        merged = merge_patch(spec, patch)
    except ValueError as e:
        return None, iss + [issue("E_TOO_LARGE", "error", "", str(e))]
    new, ni = normalize(merged, now, keep_executor=True)
    iss += ni
    if has_errors(iss):
        return None, iss
    iss += validate_static(new)
    iss += validate_amend(spec, new, status, live)
    iss = _dedup(iss)
    return (None if has_errors(iss) else new), iss


# =====================================================================================================================
# хеш, JSON Schema, описание условий
# =====================================================================================================================
def canon_hash(plan):
    """sha256 канонического JSON полей предрегистрации (symbol, side, entry, stop, targets, manage, invalidation, risk) -> hex[:16].
    Устойчив к порядку ключей, пробелам и 143 vs 143.0.

    >>> canon_hash({"symbol": "SOL", "side": "long"}) == canon_hash({"side": "long", "symbol": "SOL"})
    True
    """
    src = plan if isinstance(plan, dict) else {}
    sub = dict((k, src.get(k)) for k in PREREG_FIELDS)
    return hashlib.sha256(_canon_json(sub).encode("utf-8")).hexdigest()[:16]


def _js_node(node, defs_ref=None):
    t = node["t"]
    if t == "str":
        d = {"type": "string"}
        if "re" in node:
            d["pattern"] = node.get("schema_re", node["re"])   # схема принимает и сырой ввод с любым регистром тегов (normalize приведёт к нижнему)
        if "minlen" in node:
            d["minLength"] = node["minlen"]
        d["maxLength"] = node.get("maxlen", MAX_STR_LEN)
        return d
    if t == "enum":
        return {"type": "string", "enum": list(node["values"])}
    if t in ("num", "int"):
        d = {"type": "number" if t == "num" else "integer"}
        if "min" in node:
            d["minimum"] = node["min"]
        if "max" in node:
            d["maximum"] = node["max"]
        if "gt" in node:
            d["exclusiveMinimum"] = node["gt"]
        return d
    if t == "bool":
        return {"type": "boolean"}
    if t == "obj":
        return _js_obj(node["fields"])
    if t == "arr":
        d = {"type": "array", "items": _js_node(node["item"])}
        if "min" in node:
            d["minItems"] = node["min"]
        if "max" in node:
            d["maxItems"] = node["max"]
        return d
    if t == "cond":
        return None
    if t == "action":
        return {"$ref": "#/definitions/action"}
    return {}


def _js_obj(fields):
    props, req = {}, []
    for k, n in fields.items():
        if n["t"] == "cond":
            props[k] = {"$ref": "#/definitions/cond_%s" % WHERE_MANAGE}   # место уточняет json_schema() для entry/invalidation
        else:
            props[k] = _js_node(n)
        if n.get("req"):
            req.append(k)
    d = {"type": "object", "properties": props, "additionalProperties": False}
    if req:
        d["required"] = req
    return d


def json_schema():
    """JSON Schema draft-07 из единой таблицы FIELDS/COND_FIELDS/ACTION_FIELDS. Места условий: cond_entry/cond_invalidation/cond_manage
    (виды по COND_KINDS[...]['where'], seq только в entry). Правила смысла (стороны, суммы, id) схемой не выражаются: их проверяет validate_static."""
    defs = {}

    def leaf(kind):
        d = _js_obj(COND_FIELDS[kind])
        d["properties"] = dict(d["properties"])
        d["properties"]["kind"] = {"enum": [kind]}
        d["required"] = ["kind"] + d.get("required", [])
        return d
    for place in _ALL_PLACES:
        leaves = [leaf(k) for k, v in COND_KINDS.items() if place in v["where"]]
        ref = {"$ref": "#/definitions/cond_%s" % place}
        alts = list(leaves)
        alts.append({"type": "object", "properties": {"all": {"type": "array", "minItems": 1, "maxItems": MAX_COND_LEAVES, "items": ref}},
                     "required": ["all"], "additionalProperties": False})
        alts.append({"type": "object", "properties": {"any": {"type": "array", "minItems": 1, "maxItems": MAX_COND_LEAVES, "items": ref}},
                     "required": ["any"], "additionalProperties": False})
        alts.append({"type": "object", "properties": {"not": {"oneOf": leaves}}, "required": ["not"], "additionalProperties": False})
        if place == WHERE_ENTRY:
            alts.append({"type": "object", "properties": {
                "kind": {"enum": [SEQ_KIND]}, "steps": {"type": "array", "minItems": 1, "maxItems": MAX_COND_LEAVES, "items": {"oneOf": leaves}},
                "within_min": {"type": "number", "exclusiveMinimum": 0, "maximum": 1440}},
                "required": ["kind", "steps", "within_min"], "additionalProperties": False})
        defs["cond_%s" % place] = {"oneOf": alts}
    acts = []
    for name, spec in ACTION_FIELDS.items():
        d = _js_obj(spec)
        d["properties"]["action"] = {"enum": [name]}
        d["required"] = ["action"] + d.get("required", [])
        acts.append(d)
    defs["action"] = {"oneOf": acts}

    root = _js_obj(FIELDS)
    props = root["properties"]
    props["entry"]["properties"]["when"] = {"$ref": "#/definitions/cond_entry"}
    props["invalidation"]["items"] = {"$ref": "#/definitions/cond_invalidation"}
    props["manage"]["items"]["properties"]["when"] = {"$ref": "#/definitions/cond_manage"}
    props["manage"]["items"]["properties"]["do"] = {"$ref": "#/definitions/action"}
    props["shadow_variants"]["items"]["properties"]["patch"] = {"type": "object"}
    props["guard_preview"] = {"type": "object"}
    root["$schema"] = "http://json-schema.org/draft-07/schema#"
    root["title"] = SCHEMA
    root["definitions"] = defs
    return root


def _fmt(v):
    if isinstance(v, float) and v == int(v) and abs(v) < 1e12:
        return "%d" % v
    return "%s" % (v,)


def describe_cond(cond):
    """Человекочитаемая строка по-русски. Не бросает исключений на мусоре (вернёт «?»).

    >>> describe_cond({"kind": "candle_close", "tf": "5m", "dir": "above", "level": 142.5, "n": 1})
    '5м закрытие выше 142.5 (n=1)'
    """
    return _desc(cond, 0)


def _desc(c, d):
    if d > MAX_COND_DEPTH + 3 or not isinstance(c, dict):
        return "?"
    if isinstance(c.get("all"), list):
        return "все: " + " И ".join("(%s)" % _desc(x, d + 1) for x in c["all"])
    if isinstance(c.get("any"), list):
        return "любое: " + " ИЛИ ".join("(%s)" % _desc(x, d + 1) for x in c["any"])
    if "not" in c:
        return "не (%s)" % _desc(c["not"], d + 1)
    k = c.get("kind")
    tf = TF_LABEL.get(c.get("tf"), c.get("tf")) if isinstance(c.get("tf"), str) else c.get("tf")
    up = {"above": "выше", "below": "ниже"}
    try:
        if k == SEQ_KIND:
            steps = c.get("steps") or []
            return "по порядку (в течение %s мин): %s" % (_fmt(c.get("within_min")), " затем ".join("%d) %s" % (i + 1, _desc(s, d + 1)) for i, s in enumerate(steps)))
        if k == "price_beyond":
            return "цена %s %s%s" % (up.get(c["dir"], c["dir"]), _fmt(c["level"]), " (допустимо, если уже за уровнем)" if c.get("allow_already") else "")
        if k == "candle_close":
            return "%s закрытие %s %s (n=%s)" % (tf, up.get(c["dir"], c["dir"]), _fmt(c["level"]), c.get("n", 1))
        if k == "candle_reject":
            if c["dir"] == "short":
                s = "%s прокол %s вверх и закрытие обратно под уровнем" % (tf, _fmt(c["level"]))
            else:
                s = "%s прокол %s вниз и закрытие обратно над уровнем" % (tf, _fmt(c["level"]))
            w = c.get("wick_min_pct", 0)
            return s + (" (фитиль >= %s%%)" % _fmt(w) if w else "")
        if k == "candle_body":
            s = "%s свеча %s, тело >= %s%%" % (tf, "вверх" if c["dir"] == "up" else "вниз", _fmt(c["min_body_pct"]))
            if "min_close_pos" in c:
                s += ", закрытие на доле диапазона >= %s" % _fmt(c["min_close_pos"])
            return s
        if k == "volume_spike":
            return "%s объём >= %sx медианы %s свечей (%s, %s)" % (tf, _fmt(c["mult"]), c.get("lookback", 20),
                                                                  "по закрытию" if c.get("on", "close") == "close" else "на формирующейся",
                                                                  {"any": "любое направление", "up": "вверх", "down": "вниз"}.get(c.get("dir", "any"), c.get("dir")))
        if k == "price_in_zone":
            return "цена в зоне %s..%s" % (_fmt(c["low"]), _fmt(c["high"]))
        if k == "spread_bps":
            return "спред <= %s бп" % _fmt(c["lte"])
        if k == "pnl_r":
            for op, sym in (("gte", ">="), ("gt", ">"), ("lte", "<="), ("lt", "<")):
                if op in c:
                    return "результат %s %s R" % (sym, _fmt(c[op]))
        if k == "elapsed":
            return "прошло >= %s мин" % _fmt(c["min"])
        if k == "target_filled":
            return "цель %s исполнена" % c["target"]
        if k == "near_target":
            return "цена прошла %s%% пути до цели %s" % (_fmt(round(c["frac"] * 100, 4)), c["target"])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        pass
    return "?"
