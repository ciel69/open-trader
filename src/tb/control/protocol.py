"""Протокол плоскости управления: форматы команды и результата, справочник кодов, проверка аргументов. Владелец: WP12.

Команда (файл state/control/_inbox/K-<UTC>-<6 hex>.json): {"schema","id","ts","src","action","args","nonce","phrase_hash","sess","mac"}.
Результат (state/control/_outbox/<id>.json): {"id","status","code","msg","ts_done","data"}. Тексты ошибок только из CODES (без путей, ключей, исключений).
Чистый модуль: без файловых операций и сети.
"""
import copy
import hashlib
import json
import re
import time

from tb.control import settings_args

SCHEMA = "tb.ctl/1"
MODES = ("observe", "paper", "testnet", "live")
SAFER_ORDER = {"observe": 0, "paper": 1, "testnet": 2, "live": 3}     # чем больше, тем рискованнее
SESSIONS = ("day", "scalp", "intraday", "swing", "position")     # day = торговый день (горизонт сделок выбирает агент); остальные = сессия одного горизонта
RE_ID = re.compile(r"^K-\d{8}T\d{6}-[0-9a-f]{6}$")
RE_SESS = re.compile(r"^s-[0-9a-f]{4,12}$")
RE_IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
MAX_COMMAND_BYTES = 8192

DEFAULTS = {
    "version": 1, "session_ttl_min": 60, "command_ttl_s": 120, "nonce_ttl_s": 60, "min_delay_s": 2, "min_type_s": 4, "max_post_per_min": 30,
    "login_fail_max": 5, "login_fail_window_s": 300, "login_block_s": 600, "max_live_starts_day": 3, "live_restart_cooldown_min": 10,
    "allow_shutdown": False, "allow_clear_kill_from_dash": False, "status_stale_s": 15, "risk_level_cooldown_min": 60,
    "model_change_cooldown_min": 240, "eval_max_age_days": 30, "facts_max_age_min": 30, "auto_downgrade_s": 5, "start_wait_s": 90,
    "recommend": {"paper_trades": 30, "paper_days": 7}, "min_session_trades": 10,
    # вход в дашборд: open_local = с 127.0.0.1 без токена (безопасные действия), токен один раз для опасных; token = как раньше (токен для любых действий)
    "auth": {"mode": "open_local", "remember_days": 30},
}

CODES = {
    "OK": "выполнено",
    "E_AUTH": "нет прав: обновите страницу (сессия не начата)",
    "E_ELEVATE": "для этого действия нужен токен управления: введите его один раз, браузер запомнится",
    "E_MAC": "подпись команды неверна",
    "E_EXPIRED": "команда устарела",
    "E_DUP": "команда уже была получена",
    "E_NONCE": "нужно подтверждение: запросите его заново и подождите пару секунд",
    "E_PHRASE": "фраза подтверждения набрана неверно",
    "E_STATE": "действие невозможно в текущем состоянии платформы",
    "E_GATE": "жёсткие условия безопасности не выполнены",
    "E_WARN_CHANGED": "набор предупреждений изменился: запросите подтверждение заново",
    "E_WARN": "не приняты все предупреждения",
    "E_AMBER": "предупреждения (жёлтые проверки) не приняты",
    "E_PREFLIGHT": "предстартовая проверка не пройдена",
    "E_LIMITS": "превышен лимит запусков или частоты",
    "E_COOLDOWN": "действует пауза после предыдущего запуска или остановки",
    "E_ARGS": "некорректные параметры команды",
    "E_UNKNOWN": "неизвестное действие",
    "E_DISABLED": "действие отключено настройкой",
    "E_UNAVAILABLE": "функция ещё не подключена",
    "E_INTERNAL": "внутренняя ошибка сервиса управления",
    "E_SWAP_NO_LIVE_RIGHTS": "у агента нет прав на live в реестре агентов",
    "E_SWAP_AMBIGUOUS_STATE": "есть неоднозначные состояния (очередь, ждущие действия, hold): дождитесь завершения",
    "E_SWAP_PHRASE": "фраза подтверждения смены не принята",
    "E_SWAP_PROBE": "нет свежей проверки провайдера нового профиля",
    "E_SWAP_LIMIT_DAY": "достигнут суточный лимит смен",
    "E_SWAP_PROFILE": "профиль неизвестен, отключён или не подходит роли",
    "E_SWAP_TARGET": "цель смены не распознана",
    "E_SWAP_NO_KEY": "нет ключа подписи назначений",
    "E_SWAP_ROLLBACK": "откат невозможен",
    "E_EXISTS": "такая запись уже есть",
    "E_NO_KEY": "нет ключа провайдера: введите ключ (в терминале командой llm key set или в поле ключа дашборда)",
    "E_IN_USE": "запись используется профилями или ролями: сначала уберите ссылки",
    "E_LIVE_IN_USE": "запись используется live-ролью: смена только через brain.use с подтверждением или при остановленном дне",
    "E_PROVIDER_OFF": "провайдер выключен: сначала включите его",
    "E_CHECK": "проверка нашла проблемы",
}

# action -> {"live": нужен ли ctl-контекст для решения об опасности; "dash": можно ли положить из дашборда}
ACTIONS = ("challenge", "day.start", "day.stop", "stop.emergency", "kill.set", "mode.set", "risk.set", "carry.decide", "report.ack", "day.resume",
           "brain.use", "brain.rollback", "brain.reload", "intel.refresh", "notify.test", "notify.set", "platform.shutdown",
           "llm.provider", "brain.profile", "brain.role", "exchange.set")
NO_NONCE_ACTIONS = ("challenge", "day.stop", "day.resume", "kill.set", "stop.emergency", "report.ack", "intel.refresh", "notify.test", "notify.set", "brain.reload")

# категории уведомлений ОС (WP-N): ключ -> подпись; критичные по умолчанию обходят тихие часы
NOTIFY_CATEGORIES = (("day_start", "Старт дня"), ("day_report", "Итог дня"), ("emergency", "Аварийная остановка / KILL"),
                     ("service_down", "Сбой сервиса и деградация платформы"), ("live_gate", "Запуск live"), ("risk_level_changed", "Смена уровня риска"),
                     ("brain_swap", "Смена мозга / запасной профиль"), ("position_closed", "Закрытие позиции по стопу или тейку"),
                     ("day_close_failed", "Закрытие дня не подтверждено"), ("macro_blackout_soon", "Скоро макро-событие"))
NOTIFY_KEYS = tuple(k for k, _ in NOTIFY_CATEGORIES)
NOTIFY_CRITICAL = ("emergency", "service_down", "day_close_failed")
RE_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def canon(obj):
    """Канонический JSON (ключи по алфавиту, без пробелов, не ASCII как есть): вход подписи."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DIGEST_SKIP = ("accepted", "applied")     # не входят в привязку nonce: принятые предупреждения проверяются отдельно, applied ставит только дашборд для KILL


def args_digest(action, args):
    a = dict((k, v) for k, v in (args if isinstance(args, dict) else {}).items() if k not in DIGEST_SKIP)
    return sha(action + "\n" + canon(a))


def phrase_hash(phrases):
    """Хеш набранных фраз (список строк; пробелы схлопнуты, регистр и буквы как есть)."""
    return sha("\n".join(norm_phrase(p) for p in phrases))


def norm_phrase(p):
    return " ".join(str(p or "").split())


def new_id(now=None, rnd=None):
    import os
    now = time.time() if now is None else now
    return "K-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)), rnd or os.urandom(3).hex())


def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _str(v, n=200):
    return isinstance(v, str) and 0 < len(v) <= n


def validate_notify(a):
    """notify.set: {"master":bool?, "categories":{кат:bool}?, "quiet":{"enabled","from","to","tz","bypass":[кат]}?, "limits":{"per_hour","same_kind_min"}?} -> (ok, msg, args)."""
    out = {}
    unknown = [k for k in a if k not in ("master", "categories", "quiet", "limits")]
    if unknown:
        return False, "неизвестные поля: " + ", ".join(sorted(str(k)[:20] for k in unknown)), {}
    if "master" in a:
        if not isinstance(a["master"], bool):
            return False, "master: true|false", {}
        out["master"] = a["master"]
    if "categories" in a:
        c = a["categories"]
        if not isinstance(c, dict) or not c or any(k not in NOTIFY_KEYS or not isinstance(v, bool) for k, v in c.items()):
            return False, "categories: {категория: true|false}, категории: " + ", ".join(NOTIFY_KEYS), {}
        out["categories"] = dict(c)
    if "quiet" in a:
        q = a["quiet"]
        if not isinstance(q, dict) or not q or any(k not in ("enabled", "from", "to", "tz", "bypass") for k in q):
            return False, "quiet: enabled, from, to, tz, bypass", {}
        o = {}
        if "enabled" in q:
            if not isinstance(q["enabled"], bool):
                return False, "quiet.enabled: true|false", {}
            o["enabled"] = q["enabled"]
        for k in ("from", "to"):
            if k in q:
                if not (isinstance(q[k], str) and RE_HHMM.match(q[k])):
                    return False, "quiet.%s: HH:MM" % k, {}
                o[k] = q[k]
        if "tz" in q:
            if q["tz"] not in ("local", "utc"):
                return False, "quiet.tz: local|utc", {}
            o["tz"] = q["tz"]
        if "bypass" in q:
            if not isinstance(q["bypass"], list) or any(x not in NOTIFY_KEYS for x in q["bypass"]):
                return False, "quiet.bypass: список категорий", {}
            o["bypass"] = sorted(set(q["bypass"]))
        out["quiet"] = o
    if "limits" in a:
        lim = a["limits"]
        if not isinstance(lim, dict) or not lim or any(k not in ("per_hour", "same_kind_min") for k in lim):
            return False, "limits: per_hour, same_kind_min", {}
        o = {}
        if "per_hour" in lim:
            if not _int(lim["per_hour"], 1, 60):
                return False, "limits.per_hour: 1..60", {}
            o["per_hour"] = lim["per_hour"]
        if "same_kind_min" in lim:
            if not _int(lim["same_kind_min"], 0, 1440):
                return False, "limits.same_kind_min: 0..1440", {}
            o["same_kind_min"] = lim["same_kind_min"]
        out["limits"] = o
    if not out:
        return False, "пустое изменение: master, categories, quiet или limits", {}
    return True, "", out


def validate_args(action, args):
    """-> (ok, msg_ru) и нормализованные аргументы: (ok, msg, args)."""
    if not isinstance(args, dict):
        return False, "args должен быть объектом", {}
    a = copy.deepcopy(args)
    if len(canon(a)) > 2000:
        return False, "слишком длинные параметры", {}
    if action == "day.start":
        if a.get("mode") not in MODES:
            return False, "mode: observe|paper|testnet|live", {}
        a.setdefault("session", "day")
        if a["session"] not in SESSIONS:
            return False, "session: " + "|".join(SESSIONS), {}
        a.pop("minutes", None)                              # срока у дня нет: игнорируется (старые клиенты могут слать)
        if "team" in a and a["team"] is not None and not (isinstance(a["team"], str) and RE_IDENT.match(a["team"])):
            return False, "team: идентификатор", {}
        a["allow_amber"] = bool(a.get("allow_amber", False))
        acc = a.get("accepted", [])
        if not isinstance(acc, list) or len(acc) > 20 or not all(isinstance(x, str) and RE_IDENT.match(x) for x in acc):
            return False, "accepted: список идентификаторов предупреждений", {}
        a["accepted"] = sorted(set(acc))
    elif action == "day.stop":
        a.setdefault("mode", "soft")
        if a["mode"] != "soft":
            return False, "mode: soft", {}
    elif action in ("stop.emergency", "kill.set"):
        a["flatten"] = bool(a.get("flatten", False))
        r = a.get("reason", "")
        a["reason"] = " ".join(str(r).split())[:120]
        a["applied"] = bool(a.get("applied", False))      # KILL уже создан дашбордом: ctl только фиксирует (аудит, шина, halted)
    elif action == "mode.set":
        if a.get("mode") not in MODES:
            return False, "mode: observe|paper|testnet|live", {}
    elif action == "risk.set":
        if not _int(a.get("level"), 1, 9):
            return False, "level: целое 1..9", {}
        acc = a.get("accepted", [])
        if not isinstance(acc, list) or len(acc) > 20 or not all(isinstance(x, str) and RE_IDENT.match(x) for x in acc):
            return False, "accepted: список идентификаторов", {}
        a["accepted"] = sorted(set(acc))
    elif action == "carry.decide":
        if not _str(a.get("plan"), 40) or a.get("decision") not in ("carry", "close"):
            return False, "plan и decision (carry|close)", {}
    elif action == "report.ack":
        if not _str(a.get("day_id"), 40):
            return False, "day_id", {}
    elif action in ("brain.use", "brain.rollback", "brain.reload"):
        for k in ("team", "profile", "agent", "role"):
            if k in a and not (isinstance(a[k], str) and RE_IDENT.match(a[k])):
                return False, "%s: идентификатор" % k, {}
        acc = a.get("accepted", [])
        if not isinstance(acc, list) or len(acc) > 20 or not all(isinstance(x, str) and RE_IDENT.match(x) for x in acc):
            return False, "accepted: список идентификаторов предупреждений", {}
        a["accepted"] = sorted(set(acc))
        if "to" in a and not _int(a["to"], 0, 10 ** 9):
            return False, "to: номер ревизии", {}
        if "from_config" in a:
            a["from_config"] = bool(a["from_config"])
        if "all" in a:
            a["all"] = bool(a["all"])
    elif action in settings_args.ACTIONS:
        ok, msg, a = settings_args.validate(action, a)
        if not ok:
            return False, msg, {}
    elif action == "intel.refresh":
        a.setdefault("source", "all")
        if not (isinstance(a["source"], str) and RE_IDENT.match(a["source"])):
            return False, "source", {}
    elif action == "challenge":
        inner = a.get("action")
        if inner not in ACTIONS or inner == "challenge":
            return False, "action: неизвестное действие", {}
        ok, msg, ia = validate_args(inner, a.get("args") or {})
        if not ok:
            return False, msg, {}
        a = {"action": inner, "args": ia}
    elif action == "notify.set":
        ok, msg, a = validate_notify(a)
        if not ok:
            return False, msg, {}
    elif action in ("notify.test", "platform.shutdown", "day.resume"):
        a = {}
    else:
        return False, "неизвестное действие", {}
    return True, "", a


def validate_command(cmd, max_bytes=MAX_COMMAND_BYTES):
    """Структура команды (подпись и время проверяются отдельно) -> (ok, code)."""
    if not isinstance(cmd, dict) or len(canon(cmd)) > max_bytes:
        return False, "E_ARGS"
    if cmd.get("schema") != SCHEMA or not isinstance(cmd.get("id"), str) or not RE_ID.match(cmd["id"]):
        return False, "E_ARGS"
    if not isinstance(cmd.get("ts"), (int, float)) or isinstance(cmd.get("ts"), bool):
        return False, "E_ARGS"
    if cmd.get("src") not in ("dash", "cli"):
        return False, "E_ARGS"
    if not isinstance(cmd.get("mac"), str) or not re.match(r"^[0-9a-f]{64}$", cmd["mac"]):
        return False, "E_MAC"
    if cmd.get("action") not in ACTIONS:
        return False, "E_UNKNOWN"
    for k in ("nonce", "phrase_hash", "sess"):
        v = cmd.get(k)
        if v is not None and not (isinstance(v, str) and len(v) <= 200):
            return False, "E_ARGS"
    if cmd.get("sess") is not None and not RE_SESS.match(cmd["sess"]):
        return False, "E_ARGS"
    ok, _msg, _a = validate_args(cmd["action"], cmd.get("args"))
    return (True, "OK") if ok else (False, "E_ARGS")


def result(cid, status, code, data=None, now=None, msg=None):
    """Запись результата для _outbox."""
    now = time.time() if now is None else now
    return {"id": cid, "status": status, "code": code, "msg": msg or CODES.get(code, CODES["E_INTERNAL"]), "ts_done": round(now, 3),
            "data": data if data is not None else {}}
