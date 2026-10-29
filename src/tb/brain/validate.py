"""Схемы ответов ролей API-мозга, смысловая проверка решений и маскирование секретов (WP11-C2; 07b п. 5.2, 5.5).

SCHEMAS: id схемы роли (`output_schema`) -> JSON Schema для `tb.llm.schema.validate` (additionalProperties по умолчанию запрещены).
entry/1 (роль entry_scout, только paper): skip | propose (тезис, сторона, уровень, стоп, цели, сетап, гипотеза); размер/плечо/маржу модель не задаёт, исполняет код раннера.
WP11-C3: pm/1, dayplan/1, audit/1 (с вложенным tb.carry/1), casereview/1 (tb.casereview/1 из 08 п. 1.4) полные; review/1 (старая форма) и reader/1 оставлены.
semantic(schema_id, obj, ctx) -> список кодов: проверки, которые схема выразить не может (набор args по решению, совпадение плана, стоп только строже,
символы из списка досье, уровни рядом с ценой, дубликаты, окно новостей, потолки лимитов, «нельзя ослабить детерминированное решение»).
pm_context(brief, plan) / dayplan_context(brief, facts) / audit_context(inp) строят ctx из досье: ctx ОБЯЗАТЕЛЬНО берётся из данных системы, не из ответа модели.
redact(text) режет строки, похожие на ключи (sk-..., длинные hex/base64), для логов, журнала решений и сообщений об ошибках.
Модуль чистый: без файлов и сети.
"""
import json
import re

# paper + blackout_mode=warn: константы совпадают с tb.core.blackout (проверяет tests/test_blackout_warn.py); модуль остаётся чистым, без импорта ядра
BLACKOUT_MAX_SIZE_MULT = 0.5
BLACKOUT_RISK_CODE = "blackout_warn"

RE_PLAN_ID = r"^PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3}$"
PM_DECISIONS = ("hold", "move_stop", "close", "partial_close", "cancel", "amend", "hold_plan", "release_plan", "escalate")
PM_ARGS = {"hold": (), "close": (), "cancel": (), "hold_plan": (), "release_plan": (), "move_stop": ("to",), "partial_close": ("frac",),
           "amend": ("patch",), "escalate": ("reason_code",)}
ESCALATE_CODES = ("SECRET_REQUEST", "NEED_OWNER", "DATA_GAP")
ERR_TAGS = ("no-confirm", "chase", "tight-stop", "wide-stop", "wrong-direction", "news-miss", "size", "fee-drag", "rule-break", "good-process-bad-luck", "good")
SETUPS = ("level_reclaim", "level_reject", "sweep_reject", "breakout_retest", "range_fade", "trend_pullback", "none")
RE_CODE = r"^[a-z0-9_]{2,30}$"
RE_TAG = r"^[a-z0-9._-]{2,40}$"                    # как plan_schema.RE_TAG (теги гипотезы/сетапа плана)
ENTRY_DECISIONS = ("skip", "propose")
ENTRY_SKIP_CODES = ("no_setup", "no_confirm", "blackout", "pause", "busy", "rr_low", "wide_stop", "no_slots", "no_data", "no_levels", "kill", "other")
ENTRY_TAGS = ("no-confirm", "blackout")
ENTRY_PROPOSE_KEYS = ("sym", "side", "horizon", "level", "stop", "tp", "setup", "hypothesis", "ttl_min", "confirm", "thesis")
ENTRY_OPTIONAL_KEYS = ("zone", "size_mult", "tags")
ENTRY_MIN_RR = 2.0                                  # риск/прибыль брутто не ниже 2, когда в досье нет порога сессии (по средней цене целей при равных долях)
ENTRY_PRECHECK_CODES = ("WIDE_RANGE", "DAY_NO_PLANS", "NO_SLOTS", "ROOM_CLOSED", "SYM_BUSY", "PAUSE_SYM", "PAUSE_ALL")   # отказы по данным досье без вызова модели
ENTRY_SIZE_MULT_MAX = 0.5                           # потолок size_mult, который вообще может назвать модель (только уменьшение)
ENTRY_MIN_STOP_PCT = 0.1                            # стоп не ближе 0.1% к уровню входа (и не ближе ATR(5м) и трёх спредов)
ENTRY_MAX_STOP_PCT = 10.0
ENTRY_MAX_LEVEL_DIST_PCT = 3.0                      # уровень входа не дальше 3% от цены
ENTRY_MAX_LEVEL_DIST_LOOSE_PCT = 5.0                # на уровнях риска 4-5 (paper-разведка входов): до 5%, лимитная заявка ждёт у уровня до ttl
LOOSE_RISK_LEVEL = 4                                # с этого уровня риска мягче правила диапазона свечи и дальности уровня (entry_scout работает только в paper)
ENTRY_MAX_ZONE_PCT = 1.0                            # ширина зоны входа без подтверждения не шире 1% от уровня
RE_SYM = r"^[A-Z0-9]{2,20}$"
SESSION_ORDER = ("scalp", "intraday", "swing", "position")
OPPOSITE_OK = ("range_fade", "level_reject")        # watch против bias допустим только для этих сетапов (07b 5.3)
CARRY_BASIS = ("trend_intact", "level_held", "volume_supportive", "news_neutral", "event_far", "funding_ok", "stop_at_be", "thesis_valid")
CARRY_AGAINST = ("news_risk", "volume_fading", "level_lost", "event_near", "funding_high", "range_expanding")
SIZE_KEYS = ("size", "qty", "quantity", "leverage", "lev", "margin", "amount", "notional", "risk", "risk_usdt", "risk_pct", "size_mult", "contracts")
RE_ITEM = r"^(pos|plan):[A-Za-z0-9_-]{1,40}$"
CR_VERDICTS = ("good_process", "bad_process", "unlucky", "lucky", "unclear")
CR_RIGHT = ("confirm_waited", "stop_beyond_extreme", "size_ok", "skipped_bad_range", "respected_cooldown", "waited_for_blackout")
CR_IMPROVE = ("no_confirm", "chase", "wide_stop", "tight_stop", "size", "ignored_cooldown", "blackout", "fee_drag", "wrong_direction", "late_exit", "none")
CR_ALT = ("wait_confirm", "skip", "smaller_size", "wider_stop_skip", "other_side", "exit_earlier", "hold_longer", "none")
CR_KNOWN = ("range_5m_gt_stop", "funding_extreme", "no_confirm", "wide_stop", "tight_stop", "spread_wide", "blackout", "chase_move", "low_rr", "big_move_24h", "meme_coin")


def _free(depth):
    """Свободное значение ограниченной глубины (патч плана, карточки): скаляры, массивы и объекты; точную проверку полей делает plan_schema/шлюз."""
    leaf = [{"type": "null"}, {"type": "string", "maxLength": 300}, {"type": "number"}, {"type": "boolean"}]
    if depth <= 0:
        return {"anyOf": leaf}
    sub = _free(depth - 1)
    return {"anyOf": leaf + [{"type": "array", "maxItems": 20, "items": sub}, {"type": "object", "additionalProperties": sub}]}


_FREE = _free(5)


def _s(n, lo=0):
    return {"type": "string", "minLength": lo, "maxLength": n}


PM = {"type": "object", "required": ["schema", "event_seq", "plan", "rev", "decision", "args", "why", "conf", "next"], "properties": {
    "schema": {"const": "tb.pm/1"}, "event_seq": {"type": "integer", "minimum": 0}, "plan": {"type": "string", "pattern": RE_PLAN_ID},
    "rev": {"type": "integer", "minimum": 1}, "decision": {"enum": list(PM_DECISIONS)},
    "args": {"type": "object", "properties": {
        "to": {"anyOf": [{"type": "number", "minimum": 0}, {"const": "be"}]}, "frac": {"type": "number", "minimum": 0.05, "maximum": 1},
        "patch": {"type": "object", "additionalProperties": _FREE}, "reason_code": {"enum": list(ESCALATE_CODES)}}},
    "why": _s(200, 1), "conf": {"type": "number", "minimum": 0, "maximum": 1}, "next": {"enum": ["event", "check_5m"]}}}

ENTRY = {"type": "object", "required": ["schema", "event_seq", "decision", "why", "conf"], "properties": {
    "schema": {"const": "tb.entry/1"}, "event_seq": {"type": "integer", "minimum": 0}, "decision": {"enum": list(ENTRY_DECISIONS)},
    "skip_code": {"enum": list(ENTRY_SKIP_CODES)},
    "sym": {"type": "string", "pattern": RE_SYM}, "side": {"enum": ["long", "short"]}, "horizon": {"enum": list(SESSION_ORDER)}, "level": {"type": "number", "minimum": 1e-12},
    "zone": {"type": "object", "required": ["low", "high"], "properties": {"low": {"type": "number", "minimum": 1e-12}, "high": {"type": "number", "minimum": 1e-12}}},
    "stop": {"type": "number", "minimum": 1e-12}, "tp": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "number", "minimum": 1e-12}},
    "setup": {"enum": list(SETUPS)}, "hypothesis": {"type": "string", "pattern": RE_TAG}, "ttl_min": {"type": "integer", "minimum": 5, "maximum": 240},
    "confirm": {"enum": ["candle", "none"]}, "size_mult": {"type": "number", "minimum": 0.1, "maximum": ENTRY_SIZE_MULT_MAX},
    "tags": {"type": "array", "maxItems": 2, "items": {"enum": list(ENTRY_TAGS)}}, "thesis": _s(280, 10),
    "why": _s(200, 1), "conf": {"type": "number", "minimum": 0, "maximum": 1}}}

REVIEW = {"type": "object", "required": ["schema", "journal_id", "errors", "rule_followed", "lesson"], "properties": {
    "schema": {"const": "tb.review/1"}, "journal_id": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]{1,40}$"},
    "errors": {"type": "array", "maxItems": 6, "items": {"enum": list(ERR_TAGS)}}, "rule_followed": {"enum": ["yes", "no", "partial"]},
    "lesson": _s(200), "idea": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": _FREE}]}}}

DAYPLAN = {"type": "object", "required": ["schema", "regime", "bias", "session", "watch", "allowed_setups", "avoid", "limits", "carry", "carried_review", "risks", "questions", "why"], "properties": {
    "schema": {"const": "tb.dayplan/1"}, "day_id": {"type": "string", "pattern": r"^D-[0-9A-Za-z-]{1,30}$"},
    "regime": {"enum": ["trend", "range", "volatile", "unclear"]}, "bias": {"enum": ["long", "short", "neutral"]},
    "session": {"type": "object", "required": ["type"], "properties": {
        "type": {"enum": list(SESSION_ORDER)}, "minutes": {"type": "integer", "minimum": 15, "maximum": 43200}}},     # minutes устарело: принимается и игнорируется
    "watch": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["sym", "side", "level", "setup", "note"], "properties": {
        "sym": {"type": "string", "pattern": RE_SYM}, "side": {"enum": ["long", "short"]}, "level": {"type": "number", "minimum": 1e-12},
        "setup": {"enum": list(SETUPS)}, "note": _s(120)}}},
    "allowed_setups": {"type": "array", "maxItems": 7, "items": {"enum": list(SETUPS)}},
    "avoid": {"type": "array", "maxItems": 10, "items": {"type": "string", "pattern": RE_SYM}},
    "limits": {"type": "object", "required": ["max_new_plans", "max_risk_usdt", "symbols", "size_mult"], "properties": {
        "max_new_plans": {"type": "integer", "minimum": 0, "maximum": 6}, "max_risk_usdt": {"type": "number", "minimum": 0},
        "symbols": {"type": "array", "maxItems": 12, "items": {"type": "string", "pattern": RE_SYM}},
        "size_mult": {"type": "number", "minimum": 0, "maximum": 1}}},
    "carry": {"type": "object", "required": ["allow", "max_positions"], "properties": {"allow": {"type": "boolean"}, "max_positions": {"type": "integer", "minimum": 0, "maximum": 2}}},
    "carried_review": {"type": "array", "maxItems": 5, "items": {"type": "object", "required": ["plan", "decision", "why"], "properties": {
        "plan": {"type": "string", "pattern": RE_PLAN_ID}, "decision": {"enum": ["hold", "tighten", "close"]}, "why": _s(160)}}},
    "risks": {"type": "array", "maxItems": 8, "items": {"type": "string", "pattern": RE_CODE}},
    "questions": {"type": "array", "maxItems": 3, "items": _s(160)}, "why": _s(200, 1)}}

CARRY = {"type": "object", "required": ["schema", "item", "decision", "basis", "against", "view", "why", "conf"], "properties": {
    "schema": {"const": "tb.carry/1"}, "item": {"type": "string", "pattern": RE_ITEM}, "decision": {"enum": ["carry", "close"]},
    "basis": {"type": "array", "maxItems": 8, "items": {"enum": list(CARRY_BASIS)}}, "against": {"type": "array", "maxItems": 6, "items": {"enum": list(CARRY_AGAINST)}},
    "view": {"type": "object", "required": ["risk", "horizon_h", "recheck_min"], "properties": {
        "risk": {"enum": ["low", "mid", "high"]}, "horizon_h": {"type": "number", "minimum": 1, "maximum": 168}, "recheck_min": {"type": "number", "minimum": 5, "maximum": 1440}}},
    "why": _s(200, 1), "conf": {"type": "number", "minimum": 0, "maximum": 1}}}

AUDIT = {"type": "object", "required": ["schema", "day_id", "carry", "changes", "owner_questions", "narrative"], "properties": {
    "schema": {"const": "tb.audit/1"}, "day_id": {"type": "string", "pattern": r"^D-[0-9A-Za-z-]{1,30}$"},
    "carry": {"type": "array", "maxItems": 20, "items": CARRY},
    "changes": {"type": "array", "maxItems": 20, "items": {"type": "object", "required": ["item", "decision", "why"], "properties": {
        "item": {"type": "string", "pattern": RE_ITEM}, "decision": {"enum": ["close", "carry"]}, "why": _s(160)}}},
    "owner_questions": {"type": "array", "maxItems": 5, "items": {"type": "object", "required": ["item", "text", "options"], "properties": {
        "item": {"type": "string", "pattern": RE_ITEM}, "text": _s(160, 1), "options": {"type": "array", "maxItems": 2, "items": {"enum": ["carry", "close"]}}}}},
    "narrative": _s(600)}}

CASEREVIEW = {"type": "object", "required": ["schema", "verdict", "right", "improve", "alternative", "known_at_decision", "lesson_hint", "conf"], "properties": {
    "schema": {"const": "tb.casereview/1"}, "verdict": {"enum": list(CR_VERDICTS)},
    "right": {"type": "array", "maxItems": 5, "items": {"enum": list(CR_RIGHT)}}, "improve": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"enum": list(CR_IMPROVE)}},
    "alternative": {"type": "object", "required": ["action", "detail"], "properties": {"action": {"enum": list(CR_ALT)}, "detail": _s(160)}},
    "known_at_decision": {"type": "array", "maxItems": 8, "items": {"enum": list(CR_KNOWN)}},
    "lesson_hint": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["code", "cond"], "properties": {
        "code": {"type": "string", "pattern": r"^L-[a-z0-9_]{2,30}$"}, "cond": {"type": "string", "pattern": RE_CODE}}}]},
    "conf": {"type": "number", "minimum": 0, "maximum": 1}}}

READER = {"type": "object", "required": ["schema", "items"], "properties": {
    "schema": {"const": "tb.reader/1"}, "items": {"type": "array", "maxItems": 10, "items": {"type": "object", "additionalProperties": _FREE}}}}

SCHEMAS = {"pm/1": PM, "entry/1": ENTRY, "review/1": REVIEW, "casereview/1": CASEREVIEW, "dayplan/1": DAYPLAN, "audit/1": AUDIT, "reader/1": READER}


def schema_for(schema_id):
    return SCHEMAS.get(schema_id)


# ------------------------------------------------------------------ контекст проверки (из данных системы, не из ответа модели)
def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def pm_context(brief, plan, event=None):
    """Контекст смысловой проверки pm/1 из досье scope=event и события задания: сторона, цена, стоп, наличие позиции, типы событий, политика переноса, окно новостей.
    Нет данных -> поле None (проверка пропускается, остаётся шлюз). Досье есть, а плана в нём нет -> unknown_plan (любое действие кроме hold/escalate отклоняется)."""
    ctx = {"plan": plan, "has_position": None, "side": None, "px": None, "sl": None, "avg": None, "status": None, "unknown_plan": False,
           "event_types": sorted(set(t for t in ((event or {}).get("types") or []) if isinstance(t, str))), "carry_policy": None, "blackout_min": None, "blackout_warn": False}
    if not isinstance(brief, dict):
        return ctx
    rl = brief.get("risk_level")
    if isinstance(rl, dict):
        ctx["carry_policy"] = (rl.get("carry") or {}).get("policy") if isinstance(rl.get("carry"), dict) else None
    bo = (brief.get("market") or {}).get("blackout") if isinstance(brief.get("market"), dict) else None
    if isinstance(bo, dict):
        ctx["blackout_min"] = _num(bo.get("in_min"))
        ctx["blackout_warn"] = bo.get("mode") == "warn"          # режим выставляет досье (paper + blackout_mode=warn); live/testnet = block
    pos = next((x for x in brief.get("positions") or [] if isinstance(x, dict) and x.get("plan") == plan), None)
    pl = next((x for x in brief.get("plans") or [] if isinstance(x, dict) and x.get("id") == plan), None)
    if pos:
        ctx.update(has_position=True, side=pos.get("side"), px=_num(pos.get("px")), sl=_num(pos.get("sl")), avg=_num(pos.get("avg")))
    elif pl:
        ctx.update(has_position=False, side=pl.get("side"))
    if pl:
        ctx["status"] = pl.get("status")
        if ctx["side"] is None:
            ctx["side"] = pl.get("side")
    elif pos:
        ctx["has_position"] = True
    if not pos and not pl:
        ctx["unknown_plan"] = True
    return ctx


def dayplan_context(brief, facts=None, **over):
    """Контекст dayplan/1 из досье start: допустимые символы, цены, окно новостей, потолки лимитов, тип сессии владельца, политика переноса."""
    brief = brief if isinstance(brief, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    syms, prices = set(), {}
    for key in ("positions", "plans"):
        for x in brief.get(key) or []:
            if isinstance(x, dict) and x.get("sym"):
                syms.add(x["sym"])
                if _num(x.get("px")):
                    prices[x["sym"]] = _num(x["px"])
    for key, val in (brief.get("levels") or {}).items() if isinstance(brief.get("levels"), dict) else []:
        syms.add(key)
        if isinstance(val, dict) and _num(val.get("px")):
            prices.setdefault(key, _num(val["px"]))
    mk = brief.get("market") if isinstance(brief.get("market"), dict) else {}
    for key, name in (("btc", "BTC"), ("eth", "ETH")):          # интеграция: цены BTC/ETH из блока market (фид), пока в levels нет символа без плана/идеи
        m = mk.get(key)
        if isinstance(m, dict) and _num(m.get("px")):
            syms.add(name)
            prices.setdefault(name, _num(m["px"]))
    for x in (brief.get("watchlist") or brief.get("symbols") or []):
        if isinstance(x, str):
            syms.add(x)
    # монеты блоков struct / hot / ideas: промпт morning_analyst разрешает watch и limits.symbols из них, иначе SOL/KORU из struct отвергались как SYMBOL_UNKNOWN
    st = (brief.get("struct") or {}).get("s") if isinstance(brief.get("struct"), dict) else None
    for key, val in (st.items() if isinstance(st, dict) else []):
        if isinstance(key, str) and re.match(RE_SYM, key):
            syms.add(key)
            if isinstance(val, dict) and _num(val.get("px")):
                prices.setdefault(key, _num(val["px"]))
    hot = (brief.get("hot") or {}).get("top") if isinstance(brief.get("hot"), dict) else None
    ideas = (brief.get("ideas") or {}).get("active") if isinstance(brief.get("ideas"), dict) else None
    for x in (hot if isinstance(hot, list) else []) + (ideas if isinstance(ideas, list) else []):
        if isinstance(x, dict) and isinstance(x.get("sym"), str) and re.match(RE_SYM, x["sym"]):
            syms.add(x["sym"])
            if _num(x.get("px")):
                prices.setdefault(x["sym"], _num(x["px"]))
    bo = (brief.get("market") or {}).get("blackout") if isinstance(brief.get("market"), dict) else None
    bl = bo.get("in_min") if isinstance(bo, dict) else None
    acc = brief.get("account") if isinstance(brief.get("account"), dict) else {}
    ph = brief.get("paper_history") if isinstance(brief.get("paper_history"), dict) else None
    mode = ((brief.get("system") or {}).get("exec") or {}).get("mode") if isinstance(brief.get("system"), dict) else None
    cap = None
    if (_num(acc.get("stop_streak")) or 0) >= 2 or (mode == "live" and ph is not None and (_num(ph.get("closed")) or 0) < 30):
        cap = 0.5                                   # после двух стопов подряд и live без paper-истории: только осторожный размер
    ctx = {"symbols": sorted(syms) if brief else None, "prices": prices, "data_ok": bool(prices) and isinstance(brief.get("market"), dict), "size_cap": cap,
           "blackout_min": _num(bl), "blackout_warn": isinstance(bo, dict) and bo.get("mode") == "warn", "limits": brief.get("limits") if isinstance(brief.get("limits"), dict) else None,
           "session_type": (brief.get("session") or {}).get("type") if isinstance(brief.get("session"), dict) else None,
           "carry_policy": ((brief.get("risk_level") or {}).get("carry") or {}).get("policy") if isinstance(brief.get("risk_level"), dict) else None,
           "max_level_distance_pct": 12.0}
    ctx.update(over)
    return ctx


def entry_context(brief, event=None):
    """Контекст смысловой проверки entry/1 из досье scope=event и события задания: символ события, цена, ATR 5м, спред, диапазон 5м, окно новостей, слоты, занятые символы,
    паузы после стопа, политика входа без подтверждения, тип сессии, режим исполнителя, KILL. Нет данных -> None (проверка пропускается, остаётся Gate исполнителя)."""
    brief = brief if isinstance(brief, dict) else {}
    event = event if isinstance(event, dict) else {}
    sym = event.get("sym") if isinstance(event.get("sym"), str) else None
    ctx = {"sym": sym, "symbols": None, "px": None, "a5_pct": None, "spread_bps": None, "range_5m_pct": None, "blackout_min": None, "blackout_warn": False,
           "slots": None, "room_allowed": None, "busy": [], "pause_sym": None, "pause_all": None, "stop_streak": None, "kill": None, "exec_mode": None,
           "no_confirm_allowed": None, "no_confirm_max": None, "session_type": None, "has_brief": bool(brief), "risk_level": None, "struct_missing": False}
    if not brief:
        return ctx
    rec = [e for e in (brief.get("recent") or []) if isinstance(e, dict) and e.get("type") == "scanner_signal" and isinstance(e.get("sym"), str)]
    syms = sorted(set([e["sym"] for e in rec] + ([sym] if sym else [])))
    ctx["symbols"] = syms or None
    last = next((e for e in reversed(rec) if e["sym"] == sym), None) if sym else None
    d = last.get("d") if isinstance(last, dict) and isinstance(last.get("d"), dict) else {}
    px = _num((((brief.get("levels") or {}).get(sym) or {}) if isinstance(brief.get("levels"), dict) else {}).get("px")) if sym else None
    st = (((brief.get("struct") or {}).get("s") or {}).get(sym)) if sym and isinstance(brief.get("struct"), dict) and isinstance(brief["struct"].get("s"), dict) else None
    st = st if isinstance(st, dict) else {}
    ctx["px"] = px or _num(st.get("px")) or _num(d.get("px"))
    a5 = _num(st.get("a5_pct"))
    lv = ((brief.get("levels") or {}).get(sym)) if sym and isinstance(brief.get("levels"), dict) else None
    if a5 is None and isinstance(lv, dict) and _num(lv.get("atr_5m")) and ctx["px"]:
        a5 = _num(lv["atr_5m"]) / ctx["px"] * 100.0
    ctx["a5_pct"] = a5
    ctx["spread_bps"] = _num(st.get("sp")) if _num(st.get("sp")) is not None else _num(d.get("spread_bps"))
    ctx["range_5m_pct"] = _num(d.get("range_5m_pct"))
    bo = (brief.get("market") or {}).get("blackout") if isinstance(brief.get("market"), dict) else None
    if isinstance(bo, dict):
        ctx["blackout_min"] = _num(bo.get("in_min"))
        ctx["blackout_warn"] = bo.get("mode") == "warn"
    lim = brief.get("limits") if isinstance(brief.get("limits"), dict) else {}
    ctx["slots"] = _num(lim.get("slots"))
    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    ctx["room_allowed"] = room.get("allowed") if isinstance(room.get("allowed"), bool) else None
    ctx["busy"] = sorted(set(x["sym"] for key in ("positions", "plans") for x in (brief.get(key) or []) if isinstance(x, dict) and isinstance(x.get("sym"), str)))
    pz = brief.get("pause") if isinstance(brief.get("pause"), dict) else {}
    ctx["pause_sym"] = _num((pz.get("syms") or {}).get(sym)) if sym and isinstance(pz.get("syms"), dict) else None
    ctx["pause_all"] = _num(pz.get("all"))
    ctx["stop_streak"] = _num(pz.get("streak"))
    sysd = brief.get("system") if isinstance(brief.get("system"), dict) else {}
    ctx["kill"] = sysd.get("kill") if isinstance(sysd.get("kill"), bool) else None
    ctx["exec_mode"] = (sysd.get("exec") or {}).get("mode") if isinstance(sysd.get("exec"), dict) else None
    rl = _num((brief.get("risk_level") or {}).get("level")) if isinstance(brief.get("risk_level"), dict) else None
    ctx["risk_level"] = int(rl) if rl is not None else None
    sm_ = brief.get("struct_missing") if isinstance(brief.get("struct_missing"), list) else []
    ctx["struct_missing"] = bool(sym and sym in sm_ and not isinstance(lv, dict))      # структуры по монете события ещё нет (свечи подтягиваются)
    nc = (brief.get("risk_level") or {}).get("no_confirm") if isinstance(brief.get("risk_level"), dict) else None
    if isinstance(nc, dict):
        ctx["no_confirm_allowed"] = nc.get("allowed") if isinstance(nc.get("allowed"), bool) else None
        ctx["no_confirm_max"] = _num(nc.get("size_mult_max"))
    ses = brief.get("session") if isinstance(brief.get("session"), dict) else {}
    ctx["session_type"] = ses.get("type")
    # пороги типа сессии и издержки круга из досье (те же, что у guard): без них остаются константы ENTRY_MIN_RR / ENTRY_MAX_STOP_PCT;
    # сессия-день: пороги по горизонтам (session.horizons), горизонт называет модель в ответе
    ctx["min_rr"], ctx["max_stop_pct"], ctx["cost_rt_pct"] = _num(ses.get("min_rr")), _num(ses.get("max_stop_pct")), _num(ses.get("cost_rt_pct"))
    hz = ses.get("horizons") if isinstance(ses.get("horizons"), dict) else None
    ctx["horizons"] = {k: {kk: _num(v.get(kk)) for kk in ("max_stop_pct", "min_rr")} for k, v in hz.items() if k in SESSION_ORDER and isinstance(v, dict)} if hz else None
    # рамка плана дня (brief.day из state/control/day_limits.json): допустимые сетапы, монеты избегать, число новых планов, потолок размера
    day = brief.get("day") if isinstance(brief.get("day"), dict) else {}
    ctx["day_setups"] = [x for x in (day.get("setups") or []) if isinstance(x, str)] if isinstance(day.get("setups"), list) else None
    ctx["day_avoid"] = [x for x in (day.get("avoid") or []) if isinstance(x, str)] if isinstance(day.get("avoid"), list) else []
    mnp = day.get("max_new_plans")
    ctx["day_max_new_plans"] = mnp if isinstance(mnp, int) and not isinstance(mnp, bool) else None
    ctx["day_size_mult"] = _num(day.get("size_mult"))
    ctx["max_horizon"] = day.get("max_horizon") if day.get("max_horizon") in SESSION_ORDER else None
    return ctx


def _loose(ctx):
    """Уровень риска 4-5: разведка входов в paper. Диапазон свечи события не закрывает вход (вход ждёт новую свечу у уровня), уровень допустим дальше."""
    return (ctx.get("risk_level") or 0) >= LOOSE_RISK_LEVEL


def entry_precheck(ctx):
    """Отказ entry/1 по данным досье ДО вызова модели (экономия токенов; те же коды, что дала бы смысловая проверка) -> код или None.
    WIDE_RANGE: диапазон 5м события шире максимального стопа типа сессии (для scalp любой range_5m-сигнал: по определению сканера диапазон >= 0.8%);
    DAY_NO_PLANS: план дня запрещает новые планы; NO_SLOTS / ROOM_CLOSED / SYM_BUSY / PAUSE_*: как в _entry."""
    ctx = ctx or {}
    if not ctx.get("has_brief"):
        return None
    if ctx.get("day_max_new_plans") == 0:
        return "DAY_NO_PLANS"
    r5, ms = ctx.get("range_5m_pct"), ctx.get("max_stop_pct")
    if r5 is not None and ms is not None and r5 > ms and not _loose(ctx):
        return "WIDE_RANGE"
    if ctx.get("slots") is not None and ctx["slots"] <= 0:
        return "NO_SLOTS"
    if ctx.get("room_allowed") is False:
        return "ROOM_CLOSED"
    if ctx.get("sym") and ctx["sym"] in (ctx.get("busy") or []):
        return "SYM_BUSY"
    if (ctx.get("pause_sym") or 0) > 0:
        return "PAUSE_SYM"
    if (ctx.get("pause_all") or 0) > 0:
        return "PAUSE_ALL"
    return None


def audit_context(inp):
    """Контекст audit/1 из входа day_auditor: {"day_id","items":[item...],"det":{item:"close|carry|cancel|keep"}}."""
    inp = inp if isinstance(inp, dict) else {}
    rows = inp.get("items") or inp.get("rows") or []
    items, det = [], {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("item"), str):
            items.append(r["item"])
            det[r["item"]] = r.get("decision")
    return {"day_id": inp.get("day_id"), "items": items, "det": det, "llm_unavailable": bool(inp.get("llm_unavailable"))}


# ------------------------------------------------------------------ смысловая проверка
def semantic(schema_id, obj, ctx=None):
    """Дополнительные проверки после схемы -> список кодов (пусто = ок). ctx строится из данных системы (pm_context/dayplan_context/audit_context)."""
    ctx = ctx or {}
    if _RE_STRONG.search(json.dumps(obj, ensure_ascii=False)):
        return ["SECRET_IN_ANSWER"]                  # ключ/длинный идентификатор в ответе: отклоняем и просим повторить без него
    if schema_id == "pm/1":
        return _pm(obj, ctx)
    if schema_id == "entry/1":
        return _entry(obj, ctx)
    if schema_id == "dayplan/1":
        return _dayplan(obj, ctx)
    if schema_id == "audit/1":
        return _audit(obj, ctx)
    if schema_id == "casereview/1":
        return _casereview(obj, ctx)
    return []


def _deny_keys(o, depth=0):
    """Ключи размера/плеча/маржи в патче amend (на любой глубине)."""
    if depth > 6:
        return []
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if str(k).lower() in SIZE_KEYS:
                out.append(str(k)[:20])
            out += _deny_keys(v, depth + 1)
    elif isinstance(o, list):
        for v in o[:20]:
            out += _deny_keys(v, depth + 1)
    return out


def _pm(o, ctx):
    errs = []
    d, a = o.get("decision"), o.get("args") or {}
    need = PM_ARGS.get(d, ())
    for k in need:
        if k not in a:
            errs.append("ARGS_MISSING:%s" % k)
    for k in a:
        if k not in need:
            errs.append("ARGS_EXTRA:%s" % k)
    if d == "move_stop" and "to" in a and a["to"] != "be" and not (isinstance(a["to"], (int, float)) and a["to"] > 0):
        errs.append("RANGE:args.to")
    if d == "amend":
        if not a.get("patch"):
            errs.append("ARGS_MISSING:patch")
        elif _deny_keys(a["patch"]):
            errs.append("AMEND_SIZE_KEY")
    want = ctx.get("plan")
    if want and o.get("plan") != want:
        errs.append("PLAN_MISMATCH")
    hp = ctx.get("has_position")
    if ctx.get("unknown_plan") and d not in ("hold", "escalate"):
        errs.append("PLAN_NOT_IN_BRIEF")
    ets = set(ctx.get("event_types") or [])
    bl = ctx.get("blackout_min")
    if d == "release_plan" and (ets & {"connectivity", "reconcile_alert", "limit_hit", "kill"} or (bl is not None and bl <= 60 and not ctx.get("blackout_warn"))):
        errs.append("RELEASE_UNSAFE")                # пауза снимается только когда причина паузы ушла
    if "session_ending" in ets and ctx.get("carry_policy") == "never" and hp and d in ("hold", "hold_plan", "move_stop", "amend", "release_plan"):
        errs.append("CARRY_NOT_ALLOWED")             # политика never: к концу сессии позицию закрывают
    if hp is False and d in ("close", "partial_close"):
        errs.append("NO_POSITION")
    if hp is True and d == "cancel":
        errs.append("CANCEL_IN_POSITION")
    if d == "move_stop" and hp is False and a.get("to") == "be":
        errs.append("NO_POSITION")
    to, side, px, sl, avg = a.get("to"), ctx.get("side"), ctx.get("px"), ctx.get("sl"), ctx.get("avg")
    if d == "move_stop" and isinstance(to, (int, float)) and not isinstance(to, bool) and side in ("long", "short"):
        sign = 1 if side == "long" else -1
        if sl is not None and (to - sl) * sign <= 0:
            errs.append("STOP_NOT_TIGHTER")
        if px is not None and (px - to) * sign <= 0:
            errs.append("STOP_WRONG_SIDE")
    if d == "move_stop" and to == "be" and avg is not None and sl is not None and side in ("long", "short"):
        if ((sl - avg) if side == "long" else (avg - sl)) >= 0:
            errs.append("STOP_NOT_TIGHTER")                 # стоп уже в безубытке или лучше
    return errs[:8]


def _entry_rr(side, level, px, stop, tps):
    """Риск/прибыль по худшей цене входа (long: max(уровень, цена); short: min) и средней цене целей (равные доли) -> число или None."""
    ref = level if px is None else (max(level, px) if side == "long" else min(level, px))
    risk = (ref - stop) if side == "long" else (stop - ref)
    reward = (sum(tps) / len(tps) - ref) if side == "long" else (ref - sum(tps) / len(tps))
    if risk <= 0:
        return None
    return reward / risk


def _entry(o, ctx):
    errs = []
    top = {k: v for k, v in o.items() if k != "size_mult"}
    if _deny_keys(top):
        errs.append("SIZE_KEY")                              # size/qty/leverage/margin и подобное модель не задаёт
    if ctx.get("kill") is True:
        errs.append("KILL")
    if o.get("decision") == "skip":
        extra = [k for k in ENTRY_PROPOSE_KEYS + ENTRY_OPTIONAL_KEYS if k in o]
        return (errs + ["SKIP_EXTRA:%s" % extra[0]])[:8] if extra else errs[:8]
    for k in ENTRY_PROPOSE_KEYS:
        if k not in o:
            errs.append("ARGS_MISSING:%s" % k)
    if "skip_code" in o:
        errs.append("ARGS_EXTRA:skip_code")
    if errs and any(e.startswith("ARGS_MISSING") for e in errs):
        return errs[:8]
    side, sym, lvl, stop, tps = o["side"], o["sym"], o["level"], o["stop"], o["tp"]
    want = ctx.get("sym")
    if want and sym != want:
        errs.append("SYMBOL_NOT_EVENT")                      # вход только по монете события сканера
    elif ctx.get("symbols") and sym not in ctx["symbols"]:
        errs.append("SYMBOL_UNKNOWN")
    if sym in (ctx.get("busy") or []):
        errs.append("SYM_BUSY")                              # по монете уже есть позиция или план: второй не подаём
    if ctx.get("slots") is not None and ctx["slots"] <= 0:
        errs.append("NO_SLOTS")
    if ctx.get("room_allowed") is False:
        errs.append("ROOM_CLOSED")
    sign = 1 if side == "long" else -1
    ordered = all((b - a) * sign > 0 for a, b in zip(tps, tps[1:]))
    if not ((lvl - stop) * sign > 0 and all((t - lvl) * sign > 0 for t in tps) and ordered):
        errs.append("SIDE_PRICES")                           # long: stop < level < tp1 < tp2; short: зеркально
        return errs[:8]
    px = ctx.get("px")
    if px is not None:
        if (px - stop) * sign <= 0:
            errs.append("STOP_WRONG_SIDE")                   # цена уже за стопом
        if (tps[0] - px) * sign <= 0:
            errs.append("TP_REACHED")
        if abs(lvl - px) / px * 100.0 > (ENTRY_MAX_LEVEL_DIST_LOOSE_PCT if _loose(ctx) else ENTRY_MAX_LEVEL_DIST_PCT):
            errs.append("LEVEL_FAR")
    rr = _entry_rr(side, lvl, px, stop, tps)
    dist = abs(lvl - stop) / lvl * 100.0
    min_rr, max_stop, cost = ctx.get("min_rr"), ctx.get("max_stop_pct"), ctx.get("cost_rt_pct")
    hz, h = ctx.get("horizons"), o.get("horizon")
    if hz:                                                   # сессия-день: пороги по горизонту, который назвала модель
        geo = hz.get(h) or {}
        min_rr, max_stop = geo.get("min_rr"), geo.get("max_stop_pct")
        if h not in hz:
            errs.append("HORIZON_UNKNOWN")
    elif ctx.get("session_type") in SESSION_ORDER and h != ctx["session_type"]:
        errs.append("HORIZON_MISMATCH")                      # сессия одного горизонта: план только этого горизонта (шлюз исполнителя отклонит иначе)
    mh = ctx.get("max_horizon")
    if mh and h in SESSION_ORDER and SESSION_ORDER.index(h) > SESSION_ORDER.index(mh):
        errs.append("HORIZON_ABOVE_DAY")                     # план дня ограничил горизонт на сегодня
    if rr is None:
        errs.append("RR_LOW")
    elif min_rr is not None and cost is not None:
        # как guard: RR после издержек круга, выигрыш уменьшается на cost, проигрыш растёт на cost (в % от уровня); порог = min_rr типа сессии
        if (rr * dist - cost) / (dist + cost) < min_rr - 1e-9:
            errs.append("RR_LOW")
    elif rr < ENTRY_MIN_RR - 1e-9:
        errs.append("RR_LOW")
    floor = max(ENTRY_MIN_STOP_PCT, ctx.get("a5_pct") or 0.0, 3.0 * (ctx.get("spread_bps") or 0.0) / 100.0)
    if dist < floor:
        errs.append("STOP_TIGHT")                            # стоп не вплотную: не ближе ATR(5м), трёх спредов и 0.1%
    if dist > (max_stop if max_stop is not None else ENTRY_MAX_STOP_PCT) + 1e-9:
        errs.append("STOP_WIDE")                             # шире допустимого для типа сессии (guard отклонит так же): пропускают, а не сжимают
    if ctx.get("day_max_new_plans") == 0:
        errs.append("DAY_NO_PLANS")                          # план дня: новых планов нет
    if ctx.get("day_setups") and o.get("setup") not in ctx["day_setups"]:
        errs.append("SETUP_NOT_ALLOWED")                     # сетап вне списка allowed_setups плана дня
    if sym in (ctx.get("day_avoid") or []):
        errs.append("SYM_AVOID")                             # монета в списке avoid плана дня
    dsm = ctx.get("day_size_mult")
    if dsm is not None and dsm < 1.0 and not (isinstance(o.get("size_mult"), (int, float)) and o["size_mult"] <= dsm + 1e-9):
        errs.append("DAY_SIZE")                              # план дня ограничил размер: size_mult обязателен и не выше потолка дня
    if ctx.get("range_5m_pct") is not None and ctx["range_5m_pct"] > dist and not (_loose(ctx) and o.get("confirm") == "candle"):
        errs.append("RANGE5_GT_STOP")                        # диапазон 5-минутной свечи шире стопа: пропускают (на уровнях 4-5 не для входа с подтверждением свечой: он ждёт новую свечу)
    tags = set(o.get("tags") or [])
    sm = o.get("size_mult")
    bl = ctx.get("blackout_min")
    in_window = bl is not None and bl <= 60
    if in_window:
        if not ctx.get("blackout_warn"):
            errs.append("BLACKOUT")
        else:                                               # paper + warn: только размер не выше потолка и тег blackout
            if not (isinstance(sm, (int, float)) and sm <= BLACKOUT_MAX_SIZE_MULT + 1e-9):
                errs.append("BLACKOUT_SIZE")
            if "blackout" not in tags:
                errs.append("BLACKOUT_TAG")
    elif "blackout" in tags:
        errs.append("TAG_MISMATCH:blackout")
    if o.get("confirm") == "none":
        cap = ENTRY_SIZE_MULT_MAX if ctx.get("no_confirm_max") is None else min(ENTRY_SIZE_MULT_MAX, ctx["no_confirm_max"])
        if ctx.get("no_confirm_allowed") is False:
            errs.append("NO_CONFIRM_FORBIDDEN")              # уровень риска запрещает вход без подтверждения
        if not (isinstance(sm, (int, float)) and sm <= cap + 1e-9):
            errs.append("NO_CONFIRM_SIZE")
        if "no-confirm" not in tags:
            errs.append("NO_CONFIRM_TAG")
        if (ctx.get("stop_streak") or 0) >= 2:
            errs.append("NO_CONFIRM_AFTER_STOPS")            # после двух стопов подряд только со свечным подтверждением
        z = o.get("zone")
        if not isinstance(z, dict) or not (z["low"] <= lvl <= z["high"]) or (z["high"] - z["low"]) / lvl * 100.0 > ENTRY_MAX_ZONE_PCT:
            errs.append("ZONE_REQUIRED")                     # без подтверждения вход только внутри узкой зоны вокруг уровня
    elif "no-confirm" in tags:
        errs.append("TAG_MISMATCH:no-confirm")
    if (ctx.get("pause_sym") or 0) > 0:
        errs.append("PAUSE_SYM")                             # 30 минут после стопа по монете
    if (ctx.get("pause_all") or 0) > 0:
        errs.append("PAUSE_ALL")                             # час после двух стопов подряд
    return errs[:8]


def _dayplan(o, ctx):
    errs = []
    lim = o.get("limits") or {}
    if lim.get("max_new_plans", 0) > 0 and o.get("regime") == "unclear":
        errs.append("UNCLEAR_WITH_PLANS")
    syms = set(ctx.get("symbols") or [])
    if ctx.get("symbols") is not None and not syms:
        syms = {"-"}                                     # досье без символов: любой символ в плане неизвестен
    if ctx.get("data_ok") is False and (lim.get("max_new_plans", 0) > 0 or o.get("watch")):
        errs.append("NO_DATA_PLANS")                     # нет рынка/цен в досье: план «не торговать»
    seen, avoid = set(), set(o.get("avoid") or [])
    for w in o.get("watch") or []:
        if syms and w.get("sym") not in syms:
            errs.append("SYMBOL_UNKNOWN:%s" % str(w.get("sym"))[:12])
        key = (w.get("sym"), w.get("side"), w.get("setup"))
        if key in seen:
            errs.append("DUP_WATCH:%s" % str(w.get("sym"))[:12])
        seen.add(key)
        if w.get("sym") in avoid:
            errs.append("AVOID_IN_WATCH:%s" % str(w.get("sym"))[:12])
        px = (ctx.get("prices") or {}).get(w.get("sym"))
        if px and isinstance(w.get("level"), (int, float)):
            dist = abs(w["level"] - px) / px * 100.0
            if dist > ctx.get("max_level_distance_pct", 12.0):
                errs.append("LEVEL_FAR:%s" % str(w.get("sym"))[:12])
        bias = o.get("bias")
        if bias in ("long", "short") and w.get("side") != bias and w.get("setup") not in OPPOSITE_OK:
            errs.append("SIDE_VS_BIAS:%s" % str(w.get("sym"))[:12])
    if syms:
        for k in (lim.get("symbols") or []):
            if k not in syms:
                errs.append("SYMBOL_UNKNOWN:%s" % str(k)[:12])
    bl = ctx.get("blackout_min")
    if bl is not None and bl <= 60 and lim.get("max_new_plans", 0) > 0:
        if not ctx.get("blackout_warn"):
            errs.append("BLACKOUT_PLANS")
        else:                      # paper + warn: планы допустимы только с размером не выше потолка и кодом риска blackout_warn
            if not (isinstance(lim.get("size_mult"), (int, float)) and lim["size_mult"] <= BLACKOUT_MAX_SIZE_MULT + 1e-9):
                errs.append("BLACKOUT_SIZE")
            if BLACKOUT_RISK_CODE not in (o.get("risks") or []):
                errs.append("BLACKOUT_RISK_CODE")
    sc_ = ctx.get("size_cap")
    if sc_ is not None and isinstance(lim.get("size_mult"), (int, float)) and lim["size_mult"] > sc_:
        errs.append("SIZE_ABOVE_CAP")
    cap = ctx.get("limits") or {}
    for k in ("max_new_plans", "max_risk_usdt", "size_mult"):
        if isinstance(cap.get(k), (int, float)) and isinstance(lim.get(k), (int, float)) and lim[k] > cap[k]:
            errs.append("LIMIT_ABOVE:%s" % k)
    st = (o.get("session") or {}).get("type")
    own = ctx.get("session_type")
    if own in SESSION_ORDER and st in SESSION_ORDER and SESSION_ORDER.index(st) > SESSION_ORDER.index(own):
        errs.append("SESSION_ABOVE_OWNER")
    if ctx.get("carry_policy") == "never" and (o.get("carry") or {}).get("allow"):
        errs.append("CARRY_NOT_ALLOWED")
    return errs[:8]


def _audit(o, ctx):
    errs = []
    items, det = set(ctx.get("items") or []), ctx.get("det") or {}
    if ctx.get("llm_unavailable") and (o.get("carry") or o.get("changes")):
        errs.append("LLM_UNAVAILABLE_ACTIONS")           # при недоступном агенте чек-лист не меняется
    if ctx.get("day_id") and o.get("day_id") != ctx["day_id"]:
        errs.append("DAY_MISMATCH")
    seen = set()
    for c in o.get("carry") or []:
        it = c.get("item")
        if items and it not in items:
            errs.append("UNKNOWN_ITEM")
        if it in seen:
            errs.append("DUP_ITEM")
        seen.add(it)
        if c.get("decision") == "carry":
            if not c.get("basis"):
                errs.append("CARRY_NO_BASIS")
            if det.get(it) in ("close", "cancel"):
                errs.append("CARRY_OVERRIDES_CLOSE")           # LLM может только ужесточить детерминированное решение
    for ch in o.get("changes") or []:
        it = ch.get("item")
        if items and it not in items:
            errs.append("UNKNOWN_ITEM")
        if ch.get("decision") == "carry" and det.get(it) in ("close", "cancel"):
            errs.append("CARRY_OVERRIDES_CLOSE")
    for q in o.get("owner_questions") or []:
        if items and q.get("item") not in items:
            errs.append("UNKNOWN_ITEM")
    return sorted(set(errs))[:8]


def _casereview(o, ctx):
    errs = []
    known = ctx.get("known")                                     # {признак: True|False|None} из situation (memory.review.known_features)
    for k in o.get("known_at_decision") or []:
        if isinstance(known, dict) and known.get(k) is not True:
            errs.append("KNOWN_FALSE:%s" % k)
    imp = o.get("improve") or []
    if "none" in imp and len(imp) > 1:
        errs.append("IMPROVE_NONE_MIXED")
    sv = ctx.get("suggested_verdict")
    if o.get("verdict") in ("unlucky", "lucky") and ("suggested_verdict" in ctx) and sv != o.get("verdict"):
        errs.append("VERDICT_NOT_SUPPORTED")
    return errs[:8]


# ------------------------------------------------------------------ секреты
_RE_KEYLIKE = re.compile(r"\b(?:sk|pk|key|tok|api)[-_][A-Za-z0-9_\-]{8,}\b", re.I)
_RE_LONG = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_RE_STRONG = re.compile(r"\b(?:sk|pk)[-_][A-Za-z0-9_\-]{12,}|[A-Za-z0-9+/=]{40,}|\b[A-Fa-f0-9]{32,}\b")        # для ответов ролей: только явные ключи (без ложных срабатываний на коды)


def redact(text, limit=None):
    """Строки, похожие на ключи, заменяются на «[скрыто]»; длинные идентификаторы (hex/base64 от 32 символов) тоже."""
    s = text if isinstance(text, str) else str(text)
    s = _RE_KEYLIKE.sub("[скрыто]", s)
    s = _RE_LONG.sub("[скрыто]", s)
    return s[:limit] if limit else s


def has_secret(text):
    s = text if isinstance(text, str) else str(text)
    return bool(_RE_KEYLIKE.search(s) or _RE_LONG.search(s))
