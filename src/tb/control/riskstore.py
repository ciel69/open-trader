"""Тонкий адаптер к модулю уровней риска WP16 (`tb.core.risklevels`). Владелец: WP12.

Файл `state/control/risk_level.json` и его подпись ведёт ТОЛЬКО WP16 (`set_level` проверяет фразы, принятые предупреждения и кулдаун, подписывает ключом
`control.json` и пишет свой аудит); ctl лишь вызывает `set_level` по команде `risk.set` и читает `current_level` для гейтов H5/H7 и предупреждения W5.
Подпись и ключ читает один модуль (core/risklevels._read_key), так что здесь ключей и путей `.config` нет.
"""
from tb.core import risklevels as RL

ACK = RL.ACK_PHRASE


def current(root=None):
    """-> {"level","name","source","valid","chosen","warnings"}. valid: файл подписан и верен ИЛИ его нет (уровень по умолчанию); chosen: подписанный выбор владельца."""
    c = RL.current_level(root)
    return {"level": c["level"], "name": c["name"], "source": c["source"], "valid": c["source"] != "fallback", "chosen": c["source"] == "signed",
            "warnings": list(c.get("warnings") or []), "since": c.get("since"), "config": bool(c.get("config", True))}


def phrase(level, root=None):
    return RL.confirm_phrase(level, root)


def warnings_for(level, root=None):
    """Предупреждения о ПОВЫШЕНИИ до level: [{"id","code","msg","numbers"}] (только обязательные; необязательные WP16 показываем тоже, принимаются все)."""
    return [{"id": w["id"], "code": str(w["id"]).lower(), "msg": w.get("msg", ""), "numbers": {}, "required": bool(w.get("required"))}
            for w in RL.level_warnings(level, root)]


def set_level(level, accepted, phrase_typed, ack_typed, now=None, root=None):
    """-> результат risklevels.set_level (ok, errors, warnings, ...). by всегда owner: дашборд и CLI владельца."""
    return RL.set_level(level, by="owner", root=root, accepted=list(accepted or ()), phrase=phrase_typed, ack=ack_typed, why="ctl", now=now)


def level_exists(level, root=None):
    cfg, _ = RL.load_config(root)
    return cfg is not None and str(level) in cfg["levels"]


def details(level, root=None):
    """Числа уровня для гейтов: {"name","daily_loss_frac","risk_per_trade_frac","carry_policy"} (из эффективного пресета, терпимо к ошибкам)."""
    try:
        cfg, _ = RL.load_config(root)
        lv = (cfg or {}).get("levels", {}).get(str(level)) or {}
        carry = lv.get("carry") if isinstance(lv.get("carry"), dict) else {}
        return {"name": lv.get("name"), "daily_loss_frac": lv.get("daily_loss_frac"), "risk_per_trade_frac": lv.get("risk_per_trade_frac"),
                "carry_policy": carry.get("policy")}
    except Exception:
        return {"name": None, "daily_loss_frac": None, "risk_per_trade_frac": None, "carry_policy": None}


def levels_list(root=None):
    """-> [{"n","name"}] из config/risk_levels.json (для выбора на дашборде)."""
    try:
        cfg, _ = RL.load_config(root)
        return [{"n": int(k), "name": v.get("name") or str(k)} for k, v in sorted((cfg or {}).get("levels", {}).items(), key=lambda kv: int(kv[0]))]
    except Exception:
        return []
