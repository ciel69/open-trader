"""Уровни риска (WP16, 07 п. 4.2): пресеты `config/risk_levels.json` ПОВЕРХ `config/risk_profile.json`.

Идея: риск считается от капитала и настраивается одним переключателем (1 Защитный ... 5 Агрессивный). Уровень 3 («Базовый») воспроизводит
текущее поведение guard БАЗОВЫХ значений без изменений (`effective` возвращает базу как есть, `identity_level`). Остальные уровни заменяют
доли риска/дневного убытка/слоты и масштабируют бюджет сессий, плечо и минимальный RR; потолки (`ceilings`) превысить нельзя.
`effective(prof, root)` читает ВЫБРАННЫЙ уровень из подписанного файла `state/control/risk_level.json`:
  нет файла                              -> `default_level` (3), поведение прежнее;
  файл есть, ключа нет / подпись неверна -> уровень 1 (безопасный) + предупреждение (а не 3);
  подпись верна                          -> записанный уровень.
Подпись: mac = HMAC-SHA256(hmac_key, canonical_json(тело без mac)); ключ `hmac_key` (hex) лежит в `<TB_KEYS_DIR|~/.config/traider-bot>/control.json`
и читается ОДНОЙ функцией `_read_key()` (в тестах подменяется каталог TB_KEYS_DIR). Агент и шлюз файл уровня менять не могут: инструмента нет,
подписать без ключа нельзя; пакеты gateway/mcp/brain/llm не импортируют tb.control и не зовут `set_level`.
Лестница просадки (`dd_mult`) применяется ПОСЛЕ уровня и не отключается. Авто-понижение (`auto_downgrade`): на ступень вниз при просадке не
меньше первой ступени лестницы или при дневном убытке >= 70% лимита; повышать автоматически нельзя; вернуть уровень может только владелец.
Итог по каждому лимиту = МИНИМУМ из уровня, потолка, `agents.json limits` и `exec_limits.json` (`limits_summary`).
Запросы политик для WP5/WP9/WP14: `carry_policy`, `pyramid_policy`, `no_confirm_policy`, `size_mult_cap`, `setup_allowed`.
Только stdlib. Тесты: tests/test_risklevels*.py (временный TB_ROOT и TB_KEYS_DIR).
"""
import calendar
import copy
import hashlib
import hmac
import json
import math
import os
import re
import time

from tb import paths
from tb.core.filelock import FileLock, atomic_write_json

SAFE_LEVEL = 1
LEVELS = (1, 2, 3, 4, 5)
ACK_PHRASE = "я принимаю риск"
HISTORY_MAX = 20
AUDIT_MAX_FIELD = 200
_RE_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
AGGR_RANK = {"low": 0, "mid": 1, "high": 2}
CARRY_POLICIES = ("never", "owner_confirm", "agent")
_REQ_NUM = ("risk_per_trade_frac", "daily_loss_frac", "session_budget_mult", "max_open_positions", "total_open_risk_frac",
            "leverage_mult", "min_net_rr_add", "size_mult_cap")
_REQ_OBJ = ("pyramid", "no_confirm", "carry", "setups")

# Безопасный пресет в коде: действует, если risk_levels.json есть, но испорчен (не повышаем риск из-за опечатки в файле).
BUILTIN_CEILINGS = {"risk_per_trade_frac": 0.05, "daily_loss_frac": 0.15, "session_budget_frac": {"scalp": 0.03, "intraday": 0.06, "swing": 0.08, "position": 0.08},
                    "max_open_positions": 3, "total_open_risk_frac": 0.12, "margin_per_trade_frac": 0.30}
BUILTIN_L1 = {"name": "Защитный", "risk_per_trade_frac": 0.01, "daily_loss_frac": 0.04, "session_budget_mult": 0.5, "max_open_positions": 1,
              "total_open_risk_frac": 0.02, "leverage_mult": 0.5, "min_net_rr_add": 0.5, "size_mult_cap": 0.5,
              "pyramid": {"allowed": False}, "no_confirm": {"allowed": False},
              "carry": {"policy": "never", "scalp": False, "max_positions": 0, "max_total_risk_frac": 0, "max_risk_frac_of_trade": 0.5,
                        "event_hours": 12, "max_funding_frac_of_risk": 0.25},
              "setups": {"aggr": "low"}}


def _builtin_cfg():
    return {"version": 1, "default_level": SAFE_LEVEL, "identity_level": 3, "confirmed_levels": [], "cooldown_min": 60,
            "ceilings": copy.deepcopy(BUILTIN_CEILINGS), "auto_downgrade": {"daily_loss_ratio": 0.7, "on_drawdown_step": True},
            "setup_aggr": {}, "levels": {"1": copy.deepcopy(BUILTIN_L1)}}


# ------------------------------------------------------------------ пути, конфиг
def config_file(root=None):
    return paths.config("risk_levels.json", root=root)


def level_file(root=None):
    return paths.state("control", "risk_level.json", root=root)


def audit_file(root=None):
    return paths.state("control", "risk_audit.jsonl", root=root)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate(cfg):
    """-> [ошибки по-русски]; пусто = конфиг пригоден."""
    err = []
    if not isinstance(cfg, dict):
        return ["корень должен быть объектом"]
    if cfg.get("version") != 1:
        err.append("version: поддерживается 1")
    lv = cfg.get("levels")
    if not isinstance(lv, dict) or not lv:
        return err + ["levels: ожидался объект уровней"]
    for k in ("default_level", "identity_level"):
        if cfg.get(k) not in LEVELS or str(cfg.get(k)) not in lv:
            err.append("%s: уровень 1..5 из levels" % k)
    ce = cfg.get("ceilings")
    if not isinstance(ce, dict):
        return err + ["ceilings: ожидался объект"]
    for k in ("risk_per_trade_frac", "daily_loss_frac", "max_open_positions", "total_open_risk_frac", "margin_per_trade_frac"):
        if not _num(ce.get(k)) or ce[k] <= 0:
            err.append("ceilings.%s: положительное число" % k)
    if not isinstance(ce.get("session_budget_frac"), dict) or not all(_num(x) and x > 0 for x in ce["session_budget_frac"].values()):
        err.append("ceilings.session_budget_frac: объект положительных чисел")
    for n, L in sorted(lv.items()):
        if n not in [str(x) for x in LEVELS] or not isinstance(L, dict):
            err.append("levels.%s: неверный уровень" % n)
            continue
        for k in _REQ_NUM:
            if not _num(L.get(k)) or L[k] < 0:
                err.append("levels.%s.%s: неотрицательное число" % (n, k))
        for k in _REQ_OBJ:
            if not isinstance(L.get(k), dict):
                err.append("levels.%s.%s: ожидался объект" % (n, k))
        if isinstance(L.get("carry"), dict) and L["carry"].get("policy") not in CARRY_POLICIES:
            err.append("levels.%s.carry.policy: одно из %s" % (n, CARRY_POLICIES))
        if isinstance(L.get("setups"), dict) and L["setups"].get("aggr") not in AGGR_RANK:
            err.append("levels.%s.setups.aggr: low|mid|high" % n)
        if isinstance(L.get("no_confirm"), dict) and not isinstance(L["no_confirm"].get("allowed"), bool):
            err.append("levels.%s.no_confirm.allowed: true/false" % n)
        if isinstance(L.get("pyramid"), dict) and not isinstance(L["pyramid"].get("allowed"), bool):
            err.append("levels.%s.pyramid.allowed: true/false" % n)
    return err


def load_config(root=None):
    """-> (cfg | None, [предупреждения]). None = файла нет (наследие: уровни не применяются, поведение прежнее).
    Файл испорчен -> встроенный безопасный пресет (только уровень 1) и предупреждение."""
    path = config_file(root)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None, []
    except (OSError, ValueError) as e:
        return _builtin_cfg(), ["risk_levels.json не читается (%s): действует встроенный уровень 1" % e]
    err = _validate(cfg)
    if err:
        return _builtin_cfg(), ["risk_levels.json неверен (%s): действует встроенный уровень 1" % "; ".join(err[:4])]
    return cfg, []


def unconfirmed_levels(cfg):
    conf = set(cfg.get("confirmed_levels") or [])
    return [n for n in LEVELS if str(n) in cfg["levels"] and n not in conf]


# ------------------------------------------------------------------ подпись
def key_dir(environ=None):
    env = os.environ if environ is None else environ
    if env.get("TB_KEYS_DIR"):
        return env["TB_KEYS_DIR"]
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".config", "traider-bot")


def _read_key(environ=None):
    """-> bytes | None. ЕДИНСТВЕННОЕ место чтения ключа подписи (поле hmac_key файла control.json). Любая ошибка = None."""
    try:
        with open(os.path.join(key_dir(environ), "control.json"), encoding="utf-8") as f:
            d = json.load(f)
        h = d.get("hmac_key") if isinstance(d, dict) else None
        if not isinstance(h, str) or len(h) < 32:
            return None
        return bytes.fromhex(h)
    except (OSError, ValueError, TypeError):
        return None


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_state(body, key):
    """-> hex-подпись тела (без поля mac)."""
    body = {k: v for k, v in body.items() if k != "mac"}
    return hmac.new(key, canonical(body), hashlib.sha256).hexdigest()


def verify_state(obj, key):
    if not isinstance(obj, dict) or not key:
        return False
    mac = obj.get("mac")
    if not isinstance(mac, str):
        return False
    return hmac.compare_digest(mac.encode("ascii", "replace"), sign_state(obj, key).encode("ascii"))


# ------------------------------------------------------------------ выбранный уровень
def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _read_state(root):
    """-> (obj | None, ошибка | None); (None, None) = файла нет."""
    try:
        with open(level_file(root), encoding="utf-8") as f:
            obj = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "файл уровня нечитаем (%s)" % e
    if not isinstance(obj, dict):
        return None, "файл уровня: корень не объект"
    return obj, None


def current_level(root=None):
    """-> {"level","name","source":"default|signed|fallback","since","by","warnings","confirmed","history"}. Никогда не бросает исключений."""
    cfg, warns = load_config(root)
    warns = list(warns)
    if cfg is None:
        return {"level": 3, "name": "Базовый", "source": "default", "since": None, "by": None, "warnings": warns, "confirmed": True,
                "history": [], "config": False}
    obj, err = _read_state(root)
    level, source, since, by, hist = cfg["default_level"], "default", None, None, []
    if err or obj is not None:
        key = _read_key()
        if err:
            level, source = SAFE_LEVEL, "fallback"
            warns.append(err + ": действует уровень 1")
        elif key is None:
            level, source = SAFE_LEVEL, "fallback"
            warns.append("файл уровня есть, но ключ подписи не найден (./tb risk init-key): действует уровень 1")
        elif not verify_state(obj, key):
            level, source = SAFE_LEVEL, "fallback"
            warns.append("подпись файла уровня неверна: действует уровень 1")
        elif obj.get("level") not in LEVELS or str(obj.get("level")) not in cfg["levels"]:
            level, source = SAFE_LEVEL, "fallback"
            warns.append("в файле уровня неизвестный уровень %r: действует уровень 1" % (obj.get("level"),))
        else:
            level, source, since, by = obj["level"], "signed", obj.get("since"), obj.get("by")
            hist = obj.get("history") if isinstance(obj.get("history"), list) else []
    if str(level) not in cfg["levels"]:
        level, source = SAFE_LEVEL, "fallback"
        warns.append("уровня нет в risk_levels.json: действует уровень 1")
    if level not in (cfg.get("confirmed_levels") or []) and source != "fallback" and level != SAFE_LEVEL:
        warns.append("значения уровня %d не подтверждены владельцем (Q-R1)" % level)
    return {"level": level, "name": cfg["levels"][str(level)].get("name", str(level)), "source": source, "since": since, "by": by,
            "warnings": warns, "confirmed": level in (cfg.get("confirmed_levels") or []), "history": hist, "config": True}


# ------------------------------------------------------------------ эффективный профиль
def _lev(base, mult):
    if mult >= 1.0:
        return base
    return max(1, min(base, int(math.floor(base * mult + 1e-9))))


def _rr(base, add):
    return base if add <= 0 else max(1.0, base + add)


def effective(prof, root=None, level=None):
    """Профиль с применённым уровнем: -> новый dict (база не меняется); ключ `risk_level` = метаданные (level, name, source, identity, warnings, clamped).
    Нет risk_levels.json -> возвращается сам prof. Для identity_level (3) значения базы остаются, только проверяется потолок."""
    cfg, cw = load_config(root)
    if cfg is None:
        return prof
    if level is None:
        cur = current_level(root)
        level, source, warns = cur["level"], cur["source"], list(cur["warnings"])
    else:
        level, source, warns = int(level), "explicit", list(cw)
        if str(level) not in cfg["levels"]:
            raise ValueError("нет уровня %r" % (level,))
    L = cfg["levels"][str(level)]
    ce = cfg["ceilings"]
    identity = (level == cfg["identity_level"])
    q = dict(prof)
    clamped = []

    def cap(key, val, ceil):
        if val > ceil + 1e-12:
            clamped.append(key)
            return ceil
        return val

    if identity:
        for k, ck in (("risk_per_trade_frac", "risk_per_trade_frac"), ("daily_loss_frac", "daily_loss_frac"),
                      ("max_open_positions", "max_open_positions"), ("margin_per_trade_frac", "margin_per_trade_frac")):
            if k in q and _num(q[k]):
                q[k] = cap(k, q[k], ce[ck])
    else:
        q["risk_per_trade_frac"] = cap("risk_per_trade_frac", L["risk_per_trade_frac"], ce["risk_per_trade_frac"])
        q["daily_loss_frac"] = cap("daily_loss_frac", L["daily_loss_frac"], ce["daily_loss_frac"])
        q["max_open_positions"] = int(cap("max_open_positions", L["max_open_positions"], ce["max_open_positions"]))
        if _num(prof.get("margin_per_trade_frac")):
            q["margin_per_trade_frac"] = cap("margin_per_trade_frac", prof["margin_per_trade_frac"], ce["margin_per_trade_frac"])
        if _num(prof.get("max_leverage")):
            q["max_leverage"] = _lev(prof["max_leverage"], L["leverage_mult"])
        if _num(prof.get("min_rr")):
            q["min_rr"] = _rr(prof["min_rr"], L["min_net_rr_add"])
        if isinstance(prof.get("session_types"), dict):
            st = {}
            for name, c0 in prof["session_types"].items():
                c = dict(c0)
                if _num(c0.get("budget_frac")):
                    cl = ce["session_budget_frac"].get(name)
                    v = c0["budget_frac"] * L["session_budget_mult"]
                    c["budget_frac"] = round(cap("session_budget_frac.%s" % name, v, cl) if cl else v, 10)
                if _num(c0.get("max_leverage")):
                    c["max_leverage"] = _lev(c0["max_leverage"], L["leverage_mult"])
                if _num(c0.get("min_rr")):
                    c["min_rr"] = _rr(c0["min_rr"], L["min_net_rr_add"])
                st[name] = c
            q["session_types"] = st
    # суммарный открытый риск: ограничение вводим только когда оно строже неявного (число слотов x риск сделки)
    tcap = min(L["total_open_risk_frac"], ce["total_open_risk_frac"])
    if not identity and tcap < q["max_open_positions"] * q["risk_per_trade_frac"] - 1e-12:
        q["total_open_risk_cap_frac"] = tcap
    if clamped:
        warns.append("значения уровня %d выше потолка, применён потолок: %s" % (level, ", ".join(clamped)))
    q["risk_level"] = {"level": level, "name": L.get("name", str(level)), "source": source, "identity": identity, "warnings": warns,
                       "clamped": clamped, "confirmed": level in (cfg.get("confirmed_levels") or [])}
    return q


def level_changes_behavior(prof_eff):
    """True, если применён не identity-уровень (guard тогда добавляет `risk_level` в ctx вывода)."""
    m = prof_eff.get("risk_level") if isinstance(prof_eff, dict) else None
    return bool(m) and not m.get("identity")


def apply_agent_limits(prof, limits):
    """Сужение профиля лимитами агента (agents.json limits): -> (prof', risk_cap_usdt | None). Только min."""
    q = dict(prof)
    cap = None
    if isinstance(limits, dict):
        pct = limits.get("max_risk_pct")
        if _num(pct) and pct > 0:
            q["risk_per_trade_frac"] = min(q["risk_per_trade_frac"], pct / 100.0)
        usd = limits.get("max_risk_usdt")
        if _num(usd) and usd > 0:
            cap = float(usd)
    return q, cap


# ------------------------------------------------------------------ запросы политик
def _level_cfg(root=None, level=None):
    """-> (cfg|None, L|None, level). Нет файла risk_levels.json: (None, None, 3)."""
    cfg, _ = load_config(root)
    if cfg is None:
        return None, None, 3
    lvl = current_level(root)["level"] if level is None else int(level)
    return cfg, cfg["levels"][str(lvl)], lvl


def _exec(root, exec_limits):
    if exec_limits is not None:
        return exec_limits
    try:
        from tb.core import exec_cfg
        return exec_cfg.load(root=root)
    except Exception:
        return None


def carry_policy(root=None, level=None):
    """Политика переноса через сутки (07 п. 2.5): {"level","policy","scalp","max_positions","max_total_risk_frac","max_risk_frac_of_trade","event_hours","max_funding_frac_of_risk"}.
    Нет risk_levels.json: политика уровня 3 (owner_confirm) по умолчанию."""
    cfg, L, lvl = _level_cfg(root, level)
    if L is None:
        return {"level": 3, "policy": "owner_confirm", "scalp": False, "max_positions": 2, "max_total_risk_frac": 0.5,
                "max_risk_frac_of_trade": 0.5, "event_hours": 12, "max_funding_frac_of_risk": 0.25}
    d = copy.deepcopy(L["carry"])
    d["scalp"] = False         # скальп не переносится ни на каком уровне
    d["level"] = lvl
    return d


def pyramid_policy(root=None, exec_limits=None, agent_limits=None, level=None):
    """Добор прибыльной позиции: {"allowed","level_allows","exec_enabled","agent_allows","max_adds","max_add_risk_frac"}.
    allowed = уровень разрешает И exec_limits.pyramid.enabled И (agents.json allow_add, если агент задан); числа = минимум."""
    cfg, L, lvl = _level_cfg(root, level)
    ex = _exec(root, exec_limits)
    ep = (ex or {}).get("pyramid") or {}
    level_allows = True if L is None else bool(L["pyramid"]["allowed"])
    exec_enabled = bool(ep.get("enabled", False))
    agent_allows = True if agent_limits is None else bool(agent_limits.get("allow_add", False))
    lp = (L or {}).get("pyramid") or {}
    adds = [x for x in (lp.get("max_adds"), ep.get("max_adds")) if _num(x)]
    frac = [x for x in (lp.get("max_add_risk_frac"), ep.get("max_add_risk_frac")) if _num(x)]
    return {"allowed": level_allows and exec_enabled and agent_allows, "level_allows": level_allows, "exec_enabled": exec_enabled,
            "agent_allows": agent_allows, "max_adds": int(min(adds)) if adds else 0, "max_add_risk_frac": min(frac) if frac else 0.0, "level": lvl}


def no_confirm_policy(root=None, exec_limits=None, level=None):
    """Вход без подтверждающей свечи: {"allowed","size_mult_max","level"}; size_mult_max = min(уровень, exec_limits.entry.no_confirm_max_size_mult)."""
    cfg, L, lvl = _level_cfg(root, level)
    ex = _exec(root, exec_limits)
    em = ((ex or {}).get("entry") or {}).get("no_confirm_max_size_mult", 0.5)
    if L is None:
        return {"allowed": True, "size_mult_max": em, "level": lvl}
    nc = L["no_confirm"]
    mx = min(nc.get("size_mult_max", 0.0), em) if nc["allowed"] else 0.0
    return {"allowed": bool(nc["allowed"]) and mx > 0, "size_mult_max": mx, "level": lvl}


def size_mult_cap(root=None, agent_limits=None, level=None):
    """Потолок size_mult планов = min(уровень, agents.json max_size_mult)."""
    cfg, L, lvl = _level_cfg(root, level)
    v = 1.0 if L is None else float(L["size_mult_cap"])
    if agent_limits and _num(agent_limits.get("max_size_mult")):
        v = min(v, float(agent_limits["max_size_mult"]))
    return v


def setup_allowed(setup, root=None, level=None):
    """-> (ok, код|None). Класс сетапа из risk_levels.json:setup_aggr (неизвестный сетап считается `mid`); сетап агрессивнее уровня = E_SETUP_AGGR."""
    cfg, L, lvl = _level_cfg(root, level)
    if L is None or not setup:
        return True, None
    cls = (cfg.get("setup_aggr") or {}).get(str(setup).lower(), "mid")
    if AGGR_RANK.get(cls, 1) > AGGR_RANK[L["setups"]["aggr"]]:
        return False, "E_SETUP_AGGR"
    return True, None


def limits_summary(root=None, agent_limits=None, equity=None, level=None):
    """Итоговые лимиты (минимум из уровня, потолка, agents.json limits и exec_limits) для show/brief. Единицы: доли капитала; при equity ещё USDT."""
    cfg, L, lvl = _level_cfg(root, level)
    cur = current_level(root) if level is None else {"source": "explicit", "name": (L or {}).get("name"), "warnings": []}
    if L is None:
        return {"level": 3, "name": "Базовый", "source": "default", "config": False, "warnings": cur.get("warnings", [])}
    ce = cfg["ceilings"]
    rpt = min(L["risk_per_trade_frac"], ce["risk_per_trade_frac"])
    src = {"risk_per_trade_frac": "level"}
    if rpt < L["risk_per_trade_frac"]:
        src["risk_per_trade_frac"] = "ceiling"
    cap_usd = None
    if agent_limits:
        if _num(agent_limits.get("max_risk_pct")) and agent_limits["max_risk_pct"] > 0 and agent_limits["max_risk_pct"] / 100.0 < rpt:
            rpt, src["risk_per_trade_frac"] = agent_limits["max_risk_pct"] / 100.0, "agent"
        if _num(agent_limits.get("max_risk_usdt")) and agent_limits["max_risk_usdt"] > 0:
            cap_usd = float(agent_limits["max_risk_usdt"])
    dlf = min(L["daily_loss_frac"], ce["daily_loss_frac"])
    out = {"level": lvl, "name": L.get("name"), "source": cur.get("source"), "confirmed": lvl in (cfg.get("confirmed_levels") or []), "config": True,
           "risk_per_trade_frac": rpt, "daily_loss_frac": dlf, "max_open_positions": int(min(L["max_open_positions"], ce["max_open_positions"])),
           "total_open_risk_frac": min(L["total_open_risk_frac"], ce["total_open_risk_frac"]),
           "session_budget_mult": L["session_budget_mult"], "leverage_mult": L["leverage_mult"], "min_net_rr_add": L["min_net_rr_add"],
           "size_mult_cap": size_mult_cap(root, agent_limits, lvl), "risk_cap_usdt": cap_usd, "sources": src,
           "pyramid": pyramid_policy(root, None, agent_limits, lvl), "no_confirm": no_confirm_policy(root, None, lvl),
           "carry": carry_policy(root, lvl), "setups_aggr": L["setups"]["aggr"], "warnings": list(cur.get("warnings", []))}
    if equity is not None and _num(equity) and equity > 0:
        t = rpt * equity
        out["risk_per_trade_usdt"] = round(min(t, cap_usd) if cap_usd else t, 4)
        out["daily_loss_usdt"] = round(dlf * equity, 4)
        out["total_open_risk_usdt"] = round(out["total_open_risk_frac"] * equity, 4)
    return out


def agent_warnings(limits, root=None, name="agent"):
    """Предупреждения: лимиты агента выше уровня (итог всё равно min, но владелец должен видеть расхождение)."""
    cfg, L, lvl = _level_cfg(root)
    out = []
    if L is None or not isinstance(limits, dict):
        return out
    rpt = min(L["risk_per_trade_frac"], cfg["ceilings"]["risk_per_trade_frac"]) * 100.0
    if _num(limits.get("max_risk_pct")) and limits["max_risk_pct"] > rpt + 1e-9:
        out.append("агент %s: limits.max_risk_pct=%s выше уровня %d (%.2f%%): действует меньшее" % (name, limits["max_risk_pct"], lvl, rpt))
    if _num(limits.get("max_size_mult")) and limits["max_size_mult"] > L["size_mult_cap"] + 1e-9:
        out.append("агент %s: limits.max_size_mult=%s выше потолка уровня %d (%s): действует меньшее" % (name, limits["max_size_mult"], lvl, L["size_mult_cap"]))
    if limits.get("allow_add") and not L["pyramid"]["allowed"]:
        out.append("агент %s: allow_add=true, но уровень %d добор не разрешает" % (name, lvl))
    return out


def exec_warnings(cfg_exec, root=None):
    """Для exec_cfg.warnings(): exec_limits не должны ослаблять уровень. Нет risk_levels.json -> []."""
    cfg, L, lvl = _level_cfg(root)
    if L is None or not isinstance(cfg_exec, dict):
        return []
    out = []
    p = cfg_exec.get("pyramid") or {}
    if p.get("enabled") and not L["pyramid"]["allowed"]:
        out.append("pyramid.enabled=true, но уровень риска %d добор не разрешает (добор будет заблокирован уровнем)" % lvl)
    if p.get("enabled") and L["pyramid"]["allowed"]:
        if _num(p.get("max_adds")) and p["max_adds"] > L["pyramid"].get("max_adds", 0):
            out.append("pyramid.max_adds=%r выше уровня %d (%r): действует меньшее" % (p["max_adds"], lvl, L["pyramid"].get("max_adds", 0)))
    nm = (cfg_exec.get("entry") or {}).get("no_confirm_max_size_mult")
    nc = L["no_confirm"]
    if _num(nm) and nc["allowed"] and nm > nc.get("size_mult_max", 0) + 1e-12:
        out.append("entry.no_confirm_max_size_mult=%r выше уровня %d (%r): действует меньшее" % (nm, lvl, nc.get("size_mult_max")))
    return out


# ------------------------------------------------------------------ предупреждения и смена уровня
def confirm_phrase(level, root=None):
    cfg, _ = load_config(root)
    name = ((cfg or {}).get("levels", {}).get(str(level)) or {}).get("name") or str(level)
    return "УРОВЕНЬ РИСКА %d %s" % (level, name)


def level_warnings(level, root=None):
    """Предупреждения о повышении до `level`: [{"id","msg","required"}]. required=True: владелец принимает КАЖДОЕ отдельно (уровни 4-5)."""
    cfg, L, lvl = _level_cfg(root, level)
    if L is None:
        return []
    req = lvl >= 4
    ce = cfg["ceilings"]
    out = []
    if lvl not in (cfg.get("confirmed_levels") or []):
        out.append({"id": "W_UNCONFIRMED", "required": req,
                    "msg": "значения уровня %d ПРЕДЛОЖЕНЫ и не подтверждены владельцем (Q-R1)" % lvl})
    if lvl >= 4:
        out.append({"id": "W_TRADE_LOSS", "required": True,
                    "msg": "одна сделка может потерять до %.0f%% капитала" % (100 * min(L["risk_per_trade_frac"], ce["risk_per_trade_frac"]))})
        out.append({"id": "W_DAY_LOSS", "required": True,
                    "msg": "день может закончиться убытком до -%.0f%% капитала, до %d позиций, общий риск до %.0f%%" % (
                        100 * min(L["daily_loss_frac"], ce["daily_loss_frac"]), min(L["max_open_positions"], ce["max_open_positions"]),
                        100 * min(L["total_open_risk_frac"], ce["total_open_risk_frac"]))})
        if L["pyramid"]["allowed"]:
            out.append({"id": "W_PYRAMID", "required": True,
                        "msg": "возможен добор прибыльных позиций (до %s добавлений), включает exec_limits.pyramid" % L["pyramid"].get("max_adds")})
        if L["carry"]["policy"] == "agent":
            out.append({"id": "W_CARRY", "required": True,
                        "msg": "агент сам решает о переносе позиций через сутки БЕЗ вашего подтверждения (до %d позиций)" % L["carry"]["max_positions"]})
        if L["no_confirm"]["allowed"]:
            out.append({"id": "W_NO_CONFIRM", "required": True,
                        "msg": "вход без подтверждающей свечи разрешён (размер до x%s)" % L["no_confirm"].get("size_mult_max")})
    return out


def _write_state(root, level, by, why, now, key):
    """Подписывает и пишет файл уровня (под блокировкой) -> запись. Историю читает из текущего (проверенного) файла."""
    cur = current_level(root)
    hist = list(cur["history"]) if cur["source"] == "signed" else []
    prev = cur["level"]
    hist.append({"ts": _iso(now), "from": prev, "to": level, "by": by, "why": str(why)[:40]})
    body = {"level": level, "since": _iso(now), "by": by, "history": hist[-HISTORY_MAX:]}
    body["mac"] = sign_state(body, key)
    path = level_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(path + ".lock", wait=5.0):
        atomic_write_json(path, body, sync=True)
    return body, prev


def _audit(root, rec):
    path = audit_file(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with FileLock(path + ".lock", wait=5.0):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except (OSError, TimeoutError):
        return False


def _emit(root, prev, level, by, why, now):
    try:
        from tb.core import bus
        return bus.emit("risk_level_changed", {"from": prev, "to": level, "by": by, "why": why}, root=root, now=now)
    except Exception:
        return None


def _record_change(root, prev, level, by, why, now, accepted, warns):
    aud = _audit(root, {"ts": _iso(now), "t": round(now, 3), "kind": "risk_level_changed", "from": prev, "to": level, "by": by, "why": str(why)[:AUDIT_MAX_FIELD],
                        "accepted": sorted(accepted), "warnings": [w["id"] for w in warns]})
    return aud, _emit(root, prev, level, by, why if _ident(why) else ("auto" if by == "auto" else "manual"), now)


def _ident(s):
    return bool(_RE_IDENT.match(str(s)))


def set_level(level, by="owner", root=None, accepted=(), phrase=None, ack=None, why="manual", now=None):
    """Смена уровня владельцем (CLI `risk level set`, действие ctl `risk.set`). -> {"ok":bool,"level","prev","errors":[...],"warnings":[...],"required":[ids]}.
    Понижение: нужна только фраза `УРОВЕНЬ РИСКА <n> <имя>`. Повышение: фраза; для 4-5 ещё `accepted` ⊇ обязательные id и ack=«я принимаю риск»;
    кулдаун `cooldown_min` между повышениями. Повышать может только by=owner. Без ключа подписи записи нет."""
    now = time.time() if now is None else now
    cfg, _ = load_config(root)
    res = {"ok": False, "level": None, "prev": None, "errors": [], "warnings": [], "required": []}
    if cfg is None:
        res["errors"].append("нет config/risk_levels.json")
        return res
    if level not in LEVELS or str(level) not in cfg["levels"]:
        res["errors"].append("уровень должен быть 1..5")
        return res
    cur = current_level(root)
    prev = cur["level"]
    res["prev"], res["level"] = prev, prev
    warns = level_warnings(level, root) if level > prev else []
    res["warnings"] = warns
    res["required"] = [w["id"] for w in warns if w["required"]]
    if by not in ("owner", "auto"):
        res["errors"].append("уровень меняет только владелец (by=owner) или авто-понижение (by=auto)")
        return res
    if by == "auto" and level >= prev:
        res["errors"].append("авто-понижение может только понижать")
        return res
    if by == "owner" and phrase != confirm_phrase(level, root):
        res["errors"].append("нужна фраза подтверждения: «%s»" % confirm_phrase(level, root))
    if level > prev and by == "owner":
        miss = [i for i in res["required"] if i not in set(accepted or ())]
        if miss:
            res["errors"].append("не приняты предупреждения: %s" % ", ".join(miss))
        if level >= 4 and ack != ACK_PHRASE:
            res["errors"].append("нужна фраза «%s»" % ACK_PHRASE)
        last = None
        for h in cur["history"]:
            if isinstance(h, dict) and _num(h.get("to")) and _num(h.get("from")) and h["to"] > h["from"]:
                last = h.get("ts")
        if last:
            try:
                lt = calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
            except (ValueError, TypeError):
                lt = None
            wait = (cfg.get("cooldown_min", 60) * 60) - (now - lt) if lt else 0
            if wait > 0:
                res["errors"].append("кулдаун между повышениями: ещё %d мин" % math.ceil(wait / 60.0))
    if level == prev and cur["source"] == "signed":
        res["errors"].append("уровень уже %d" % level)
    if res["errors"]:
        return res
    key = _read_key()
    if key is None:
        res["errors"].append("нет ключа подписи: владелец создаёт его командой ./tb risk init-key")
        return res
    try:
        _write_state(root, level, by, why, now, key)
    except (OSError, TimeoutError) as e:
        res["errors"].append("не удалось записать файл уровня: %s" % e)
        return res
    aud, seq = _record_change(root, prev, level, by, why, now, accepted or (), warns)
    res.update({"ok": True, "level": level, "audit": aud, "event_seq": seq})
    return res


def auto_downgrade_reasons(dd, ladder, realized_today, daily_limit, ratio=0.7, today=None):
    """Чистая функция: коды причин авто-понижения. dd = просадка от максимума (доля), ladder = [[порог, множитель], ...] профиля,
    realized_today <= -ratio x daily_limit. Код просадки несёт достигнутую ступень (глубже ступень = новая причина)."""
    out = []
    day = today or time.strftime("%Y-%m-%d", time.gmtime())
    steps = sorted(float(t[0]) for t in (ladder or []))
    reached = [t for t in steps if dd >= t]
    if reached:
        out.append("dd_%s_%s" % (("%g" % reached[-1]), day))
    if daily_limit and daily_limit > 0 and realized_today <= -ratio * daily_limit:
        out.append("daily_loss_%s" % day)
    return out


def auto_downgrade(root=None, reasons=None, now=None):
    """Понижает уровень на ступень (by=auto), если есть причина, которой в истории ещё нет. Повысить не умеет, ниже 1 не идёт.
    -> {"changed":bool,"level","prev","reasons","error"?}. Нужен ключ подписи; без ключа: changed=False, error=no_key."""
    now = time.time() if now is None else now
    cur = current_level(root)
    res = {"changed": False, "level": cur["level"], "prev": cur["level"], "reasons": list(reasons or [])}
    if not reasons or cur["level"] <= SAFE_LEVEL:
        return res
    done = set(h.get("why") for h in cur["history"] if isinstance(h, dict) and h.get("by") == "auto")
    new = [r for r in reasons if r not in done]
    if not new:
        return res
    key = _read_key()
    if key is None:
        res["error"] = "no_key"
        return res
    lvl = cur["level"] - 1
    try:
        _write_state(root, lvl, "auto", new[0], now, key)
    except (OSError, TimeoutError) as e:
        res["error"] = str(e)
        return res
    _record_change(root, cur["level"], lvl, "auto", new[0], now, (), [])
    res.update({"changed": True, "level": lvl, "reasons": new})
    return res


def check_state(root=None, now=None):
    """Собирает входы авто-понижения из портфеля и журнала -> {"reasons","dd","realized_today","daily_limit","level"}. Ничего не пишет."""
    from tb.core import guard, portfolio
    now = time.time() if now is None else now
    base = guard.load_profile(paths.config("risk_profile.json", root=root))
    eff = effective(base, root)
    port = portfolio.load()
    sp = portfolio.scaled_profile(eff, port)
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    st = guard.day_state(eff, today)
    cfg, _ = load_config(root)
    ratio = ((cfg or {}).get("auto_downgrade") or {}).get("daily_loss_ratio", 0.7)
    dd = portfolio.drawdown(port)
    reasons = auto_downgrade_reasons(dd, base.get("drawdown_ladder"), st["realized_today"], sp["daily_loss_limit_usdt"], ratio, today)
    return {"reasons": reasons, "dd": round(dd, 4), "realized_today": st["realized_today"], "daily_limit": sp["daily_loss_limit_usdt"],
            "level": current_level(root)["level"]}
