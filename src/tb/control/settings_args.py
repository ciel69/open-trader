"""Проверка аргументов настроечных действий ctl (провайдеры LLM, профили, роли, биржи). Чистый модуль без файлов и сети; формат: docs/spec_v2/SETTINGS_API.md.

Здесь проверяются только типы, белые списки полей и размеры; смысловые проверки (адаптер, URL, цены, наличие ключа, ссылки профилей) делают tb.llm.provstore,
tb.brain.profiles и tb.core.exchcfg при исполнении. Секретов в аргументах НЕТ и быть не может: ключи LLM принимает отдельный маршрут дашборда (write-only),
ключи бирж не принимаются нигде, кроме терминала.
"""
import re

ACTIONS = ("llm.provider", "brain.profile", "brain.role", "exchange.set")
RE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
RE_ROLE = re.compile(r"^[a-z][a-z_]{1,30}$")
RE_EXCH = re.compile(r"^[a-z][a-z0-9]{1,11}$")
PROVIDER_OPS = ("add", "update", "remove", "enable", "disable", "model_set", "model_remove", "check", "key_clear")
PROFILE_OPS = ("set", "delete")
ROLE_OPS = ("chain", "opts", "opts_clear")
EXCH_OPS = ("activate", "enable", "disable", "mode", "allowed", "limits")
PROVIDER_FIELDS = ("name", "adapter", "base_url", "beta_url", "key_file", "key_env", "timeouts", "retry", "limits", "note", "verified", "version_header", "models")
MODEL_FIELDS = ("name", "ctx", "max_out", "tools", "strict", "json_mode", "thinking", "cache", "price", "tags", "max_tokens_field", "no_temperature")


def _scalar_tree(v, depth=0):
    """JSON-значение без больших структур: числа, строки, bool, null, словари и списки глубины <= 4."""
    if depth > 4:
        return False
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        return len(v) <= 300
    if isinstance(v, list):
        return len(v) <= 40 and all(_scalar_tree(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return len(v) <= 30 and all(isinstance(k, str) and len(k) <= 40 and _scalar_tree(x, depth + 1) for k, x in v.items())
    return False


def _only(d, allowed, what):
    if not isinstance(d, dict) or not d:
        return "%s: непустой объект" % what
    bad = [k for k in d if k not in allowed]
    if bad:
        return "%s: неизвестные поля %s" % (what, ", ".join(str(k)[:20] for k in bad[:3]))
    if not _scalar_tree(d):
        return "%s: недопустимые значения" % what
    return None


def validate(action, a):
    """-> (ok, msg, args)."""
    op = a.get("op")
    out = {"op": op}
    if action == "llm.provider":
        if op not in PROVIDER_OPS:
            return False, "op: " + "|".join(PROVIDER_OPS), {}
        if not (isinstance(a.get("id"), str) and RE_ID.match(a["id"])):
            return False, "id: идентификатор провайдера", {}
        out["id"] = a["id"]
        if op == "add":
            if "preset" in a and not (isinstance(a["preset"], str) and RE_ID.match(a["preset"])):
                return False, "preset: идентификатор шаблона", {}
            if "spec" in a:
                bad = _only(a["spec"], PROVIDER_FIELDS, "spec")
                if bad:
                    return False, bad, {}
                out["spec"] = a["spec"]
            if "preset" in a:
                out["preset"] = a["preset"]
            if "preset" not in out and "spec" not in out:
                return False, "нужен preset или spec", {}
        elif op == "update":
            bad = _only(a.get("patch"), tuple(f for f in PROVIDER_FIELDS if f != "models"), "patch")
            if bad:
                return False, bad, {}
            out["patch"] = a["patch"]
        elif op in ("model_set", "model_remove"):
            if not (isinstance(a.get("model"), str) and RE_ID.match(a["model"])):
                return False, "model: ключ модели", {}
            out["model"] = a["model"]
            if op == "model_set":
                bad = _only(a.get("spec"), MODEL_FIELDS, "spec")
                if bad:
                    return False, bad, {}
                out["spec"] = a["spec"]
        elif op == "check":
            if "model" in a:
                if not (isinstance(a["model"], str) and RE_ID.match(a["model"])):
                    return False, "model: ключ модели", {}
                out["model"] = a["model"]
        return True, "", out
    if action == "brain.profile":
        if op not in PROFILE_OPS:
            return False, "op: " + "|".join(PROFILE_OPS), {}
        if not (isinstance(a.get("id"), str) and RE_ID.match(a["id"])):
            return False, "id: идентификатор профиля", {}
        out["id"] = a["id"]
        if op == "set":
            for k in ("provider", "model"):
                if k in a:
                    if not (isinstance(a[k], str) and RE_ID.match(a[k])):
                        return False, "%s: идентификатор" % k, {}
                    out[k] = a[k]
            if "params" in a:
                bad = _only(a["params"], ("temperature", "max_tokens", "thinking"), "params")
                if bad:
                    return False, bad, {}
                out["params"] = a["params"]
        return True, "", out
    if action == "brain.role":
        if op not in ROLE_OPS:
            return False, "op: " + "|".join(ROLE_OPS), {}
        r = a.get("role")
        if not (isinstance(r, str) and re.match(r"^([A-Za-z0-9_-]{1,10}\.|shared\.)?[a-z][a-z_]{1,30}$", r)):
            return False, "role: [команда.|shared.]роль", {}
        out["role"] = r
        if op == "chain":
            if "primary" in a:
                if not (isinstance(a["primary"], str) and RE_ID.match(a["primary"])):
                    return False, "primary: идентификатор профиля", {}
                out["primary"] = a["primary"]
            if "fallback" in a:
                fb = a["fallback"]
                if not isinstance(fb, list) or len(fb) > 4 or any(not (isinstance(x, str) and RE_ID.match(x)) for x in fb):
                    return False, "fallback: список до 4 профилей", {}
                out["fallback"] = fb
            if "primary" not in out and "fallback" not in out:
                return False, "нужен primary и/или fallback", {}
            acc = a.get("accepted", [])
            if not isinstance(acc, list) or len(acc) > 20 or not all(isinstance(x, str) and re.match(r"^[A-Za-z0-9_.:-]{1,40}$", x) for x in acc):
                return False, "accepted: список идентификаторов предупреждений", {}
            out["accepted"] = sorted(set(acc))
        elif op == "opts":
            if "params" in a:
                bad = _only(a["params"], ("temperature", "max_tokens", "thinking"), "params")
                if bad:
                    return False, bad, {}
                out["params"] = a["params"]
            if "budget" in a:
                bad = _only(a["budget"], ("day_usd",), "budget")
                if bad:
                    return False, bad, {}
                out["budget"] = a["budget"]
            if "params" not in out and "budget" not in out:
                return False, "нужен params и/или budget", {}
        return True, "", out
    if action == "exchange.set":
        if op not in EXCH_OPS:
            return False, "op: " + "|".join(EXCH_OPS), {}
        if op == "allowed":
            ids = a.get("ids")
            if not isinstance(ids, list) or not ids or len(ids) > 10 or any(not (isinstance(x, str) and RE_EXCH.match(x)) for x in ids):
                return False, "ids: непустой список бирж", {}
            out["ids"] = ids
            return True, "", out
        if not (isinstance(a.get("id"), str) and RE_EXCH.match(a["id"])):
            return False, "id: идентификатор биржи", {}
        out["id"] = a["id"]
        if op == "mode":
            if a.get("mode") not in ("paper", "testnet", "live"):
                return False, "mode: paper|testnet|live", {}
            out["mode"] = a["mode"]
        if op == "limits":
            v = a.get("max_open_positions")
            if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 50):
                return False, "max_open_positions: целое 0..50 или null", {}
            out["max_open_positions"] = v
        return True, "", out
    return False, "неизвестное действие", {}
