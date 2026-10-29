"""Реестр бирж: `config/exchanges.json` + локальный слой `config/local/exchanges.json` (WP-X). Чтение, проверка, запись настроек. Биржевого кода здесь нет.

Запись биржи: id, name, adapter, enabled, mode (paper|testnet|live: режим по умолчанию исполнителя этой биржи), keys (имена файлов ключей по профилям read/trade/testnet:
только справка, значения здесь не читаются), limits (max_open_positions: лимит открытых позиций ИМЕННО на этой бирже; общий лимит и риск считает guard по всем биржам),
symbols (allow/deny), note. Поле `implemented` не из файла: его даёт IMPLEMENTED (адаптеры, которые реально есть в tb.venues.registry). Включить (enabled=true) или сделать
активной биржу без адаптера нельзя. `active` одна (как сейчас: исполнитель по умолчанию), `allowed` список разрешённых для запуска исполнителей (параллельный запуск: по одному
исполнителю на биржу, docs/EXCHANGES.md). Ключи бирж через эти функции не передаются никогда.
"""
import re

from tb.core import localcfg

MODES = ("paper", "testnet", "live")
DEFAULT_ID = "bybit"
RE_ID = re.compile(r"^[a-z][a-z0-9]{1,11}$")                 # без «-» и «_»: id входит в имя сервиса exec@<id> и в префикс orderLinkId
IMPLEMENTED = {"bybit"}                                        # адаптеры, реально существующие в tb.venues.registry (тесты добавляют фейковые)
KEY_PROFILES = ("read", "trade", "testnet")


class ExchangeError(Exception):
    pass


def _norm_entry(eid, raw):
    """-> (запись, ошибка|None). Нормализованная запись всегда возвращается (с enabled=False при ошибке)."""
    e = {"id": eid, "name": eid, "adapter": eid, "enabled": False, "mode": "paper", "keys": {}, "limits": {"max_open_positions": None},
         "symbols": {"allow": [], "deny": []}, "note": None}
    if not isinstance(raw, dict):
        return e, "запись не объект"
    if isinstance(raw.get("name"), str):
        e["name"] = raw["name"][:60]
    if isinstance(raw.get("adapter"), str) and re.match(r"^[a-z][a-z0-9_]{0,19}$", raw["adapter"]):
        e["adapter"] = raw["adapter"]
    if isinstance(raw.get("note"), str):
        e["note"] = raw["note"][:200]
    if raw.get("mode", "paper") not in MODES:
        return e, "mode: %s" % "|".join(MODES)
    e["mode"] = raw.get("mode", "paper")
    keys = raw.get("keys", {})
    if not isinstance(keys, dict) or any(k not in KEY_PROFILES or not (isinstance(v, str) and re.match(r"^[A-Za-z0-9_.-]{1,60}$", v)) for k, v in keys.items()):
        return e, "keys: профили read|trade|testnet -> имя файла"
    e["keys"] = dict(keys)
    lim = raw.get("limits", {})
    mo = lim.get("max_open_positions") if isinstance(lim, dict) else None
    if not isinstance(lim, dict) or (mo is not None and not (isinstance(mo, int) and not isinstance(mo, bool) and 0 <= mo <= 50)):
        return e, "limits.max_open_positions: целое 0..50 или null"
    e["limits"] = {"max_open_positions": mo}
    sy = raw.get("symbols", {})
    if not isinstance(sy, dict) or any(not isinstance(sy.get(k, []), list) or any(not (isinstance(x, str) and re.match(r"^[A-Z0-9]{2,20}$", x)) for x in sy.get(k, []))
                                       for k in ("allow", "deny")):
        return e, "symbols: allow/deny списки символов"
    e["symbols"] = {"allow": list(sy.get("allow", [])), "deny": list(sy.get("deny", []))}
    e["enabled"] = raw.get("enabled") is True
    return e, None


def parse(doc):
    """doc -> {"active","allowed","exchanges":{id: запись+implemented+error},"errors":[...]}. Ошибочная запись отключается, остальные работают."""
    out = {"active": DEFAULT_ID, "allowed": [DEFAULT_ID], "exchanges": {}, "errors": [], "version": 1}
    if not isinstance(doc, dict) or not isinstance(doc.get("exchanges"), dict):
        out["errors"].append("exchanges.json: ожидается {version:1, exchanges:{...}}")
        doc = {"exchanges": {DEFAULT_ID: {"enabled": True}}}
    for eid, raw in doc["exchanges"].items():
        if not (isinstance(eid, str) and RE_ID.match(eid)):
            out["errors"].append("биржа %r: некорректный id" % str(eid)[:20])
            continue
        e, err = _norm_entry(eid, raw)
        e["implemented"] = e["adapter"] in IMPLEMENTED
        e["error"] = err
        if err:
            out["errors"].append("биржа %s отключена: %s" % (eid, err))
        elif e["enabled"] and not e["implemented"]:
            e["enabled"] = False
            e["error"] = "адаптер %s не реализован" % e["adapter"]
            out["errors"].append("биржа %s отключена: адаптер %s не реализован" % (eid, e["adapter"]))
        out["exchanges"][eid] = e
    ex = out["exchanges"]
    al = doc.get("allowed")
    if isinstance(al, list):
        out["allowed"] = [x for x in al if isinstance(x, str) and x in ex and ex[x]["enabled"]]
        for x in al:
            if x not in out["allowed"]:
                out["errors"].append("allowed: %s неизвестна или отключена" % str(x)[:12])
    else:
        out["allowed"] = [k for k, v in ex.items() if v["enabled"]]
    act = doc.get("active", DEFAULT_ID)
    if act in ex and ex[act]["enabled"] and act in out["allowed"]:
        out["active"] = act
    else:
        out["errors"].append("active: %s не включена или не в allowed; действует %s" % (str(act)[:12], DEFAULT_ID))
        out["active"] = DEFAULT_ID if DEFAULT_ID in ex else (out["allowed"][0] if out["allowed"] else DEFAULT_ID)
    return out


def load(root=None):
    """-> нормализованный реестр бирж (локальный слой наложен). Нет файла -> только bybit."""
    return parse(localcfg.load("exchanges", root))


def active(root=None):
    return load(root)["active"]


def enabled_ids(root=None):
    """-> список включённых И разрешённых бирж, активная первой (порядок запуска исполнителей)."""
    c = load(root)
    return [c["active"]] + [x for x in c["allowed"] if x != c["active"]]


def get(eid, root=None):
    return load(root)["exchanges"].get(eid)


def check_id(eid):
    if not (isinstance(eid, str) and RE_ID.match(eid)):
        raise ExchangeError("id биржи: строчные латинские буквы и цифры, 2-12 символов")
    return eid


# ---------------------------------------------------------------- запись (config/local/exchanges.json)
def _patch(root, fn):
    try:
        return localcfg.update_local("exchanges", fn, root)
    except ValueError as e:
        raise ExchangeError(str(e))


def set_enabled(eid, enabled, root=None):
    """Включает/выключает биржу. Включить биржу без адаптера нельзя. Активную биржу выключить нельзя."""
    check_id(eid)
    cur = load(root)
    e = cur["exchanges"].get(eid)
    if e is None:
        raise ExchangeError("биржа неизвестна")
    if enabled and not e["implemented"]:
        raise ExchangeError("адаптер биржи не реализован: включить нельзя")
    if not enabled and cur["active"] == eid:
        raise ExchangeError("активную биржу выключить нельзя: сначала выберите другую")

    def mut(d):
        d.setdefault("exchanges", {}).setdefault(eid, {})["enabled"] = bool(enabled)
        al = [x for x in (d.get("allowed") if isinstance(d.get("allowed"), list) else cur["allowed"])]
        if enabled and eid not in al:
            al.append(eid)
        if not enabled:
            al = [x for x in al if x != eid]
        d["allowed"] = al
        return d
    return _patch(root, mut)


def set_mode(eid, mode, root=None):
    check_id(eid)
    if mode not in MODES:
        raise ExchangeError("mode: %s" % "|".join(MODES))
    if eid not in load(root)["exchanges"]:
        raise ExchangeError("биржа неизвестна")
    return _patch(root, lambda d: (d.setdefault("exchanges", {}).setdefault(eid, {}).__setitem__("mode", mode), d)[1])


def set_active(eid, root=None):
    check_id(eid)
    cur = load(root)
    e = cur["exchanges"].get(eid)
    if e is None or not e["enabled"]:
        raise ExchangeError("биржа выключена или неизвестна")
    if eid not in cur["allowed"]:
        raise ExchangeError("биржа не в списке разрешённых")
    return _patch(root, lambda d: dict(d, active=eid))


def set_allowed(ids, root=None):
    cur = load(root)
    ids = [check_id(x) for x in ids]
    if len(set(ids)) != len(ids) or not ids:
        raise ExchangeError("allowed: непустой список без повторов")
    for x in ids:
        if x not in cur["exchanges"] or not cur["exchanges"][x]["enabled"]:
            raise ExchangeError("в allowed попала выключенная или неизвестная биржа: %s" % x)
    if cur["active"] not in ids:
        raise ExchangeError("активная биржа должна оставаться в allowed")
    return _patch(root, lambda d: dict(d, allowed=ids))


def set_limits(eid, max_open_positions, root=None):
    check_id(eid)
    if eid not in load(root)["exchanges"]:
        raise ExchangeError("биржа неизвестна")
    if max_open_positions is not None and not (isinstance(max_open_positions, int) and not isinstance(max_open_positions, bool) and 0 <= max_open_positions <= 50):
        raise ExchangeError("max_open_positions: целое 0..50 или null")
    return _patch(root, lambda d: (d.setdefault("exchanges", {}).setdefault(eid, {}).setdefault("limits", {}).__setitem__("max_open_positions", max_open_positions), d)[1])
