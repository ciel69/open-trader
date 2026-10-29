"""Управление реестром провайдеров LLM без правки кода (WP-X): запись в `config/local/providers.json` (слой localcfg), шаблоны `config/provider_presets.json`.

Операции возвращают словари `{"ok","code","msg","data"}` и сами НИЧЕГО не пишут в аудит (аудит делает вызывающий: ctl или CLI). Секретов тут нет: ключи пишет keystore.
Поставляемый `config/providers.json` не меняется: провайдеры из него можно выключить/включить и поменять поля слоем local, удалить нельзя (только добавленные владельцем).
Правила: добавление проверяется `providers._check_provider` (id, адаптер, URL, модели, цены); включить провайдера можно, только если ключ на месте
(для локальных серверов без ключа проверки нет); удалить провайдера или модель нельзя, пока на них ссылается профиль brain.json (удаление сначала профиля).
Проверка `check()` без сети: конфигурация и наличие ключа; живая (`live=True`) делает GET /models и пробный запрос тем же адаптером, что и мозг.
"""
import copy
import json

from tb.core import localcfg
from tb.llm import keystore, providers


def _res(ok, code, msg="", **data):
    return {"ok": ok, "code": code, "msg": msg, "data": data}


# ---------------------------------------------------------------- шаблоны
def presets(root=None):
    """-> {id: шаблон}. Поставляемые `config/provider_presets.json` + локальный слой `config/local/provider_presets.json`."""
    try:
        doc = localcfg.read("provider_presets", root)
    except (OSError, ValueError):
        return {}
    p = doc.get("presets") if isinstance(doc, dict) else None
    return p if isinstance(p, dict) else {}


def preset_rows(root=None):
    reg = _effective(root)
    out = []
    for pid, t in sorted(presets(root).items()):
        out.append({"id": pid, "name": t.get("name") or pid, "adapter": providers.norm_adapter(t.get("adapter")), "base_url": t.get("base_url"),
                    "verified": t.get("verified") is True, "note": t.get("note"), "models": sorted((t.get("models") or {}).keys()),
                    "key_file": t.get("key_file"), "key_env": t.get("key_env"), "added": reg is not None and pid in reg.providers})
    return out


def _effective(root):
    try:
        return providers.load(root)
    except providers.ProviderError:
        return None


def _local(root):
    return localcfg.read_local("providers", root) or {}


def _update(root, fn):
    try:
        return localcfg.update_local("providers", fn, root)
    except ValueError as e:
        raise providers.ProviderError(str(e))


def _validate_effective(root, mutate):
    """Применяет mutate к локальному слою В ПАМЯТИ и проверяет итог (слой поверх поставляемого). -> текст ошибки или None."""
    try:
        with open(providers.providers_file(root), encoding="utf-8") as f:
            base = json.load(f)
    except (OSError, ValueError):
        return "нет config/providers.json"
    loc = mutate(copy.deepcopy(_local(root)))
    reg = providers.parse(localcfg.merge(base, loc))
    bad = [e for e in reg.errors]
    return "; ".join("%s: %s" % (e["provider"], e["error"]) for e in bad[:3]) if bad else None


def _profiles_using(root, pid=None, model=None):
    """Профили brain.json (поставляемый + локальный слой), ссылающиеся на провайдера/модель."""
    try:
        doc = localcfg.read("brain", root)
    except (OSError, ValueError):
        return []
    out = []
    for name, p in sorted(((doc or {}).get("profiles") or {}).items()):
        if isinstance(p, dict) and p.get("provider") == pid and (model is None or p.get("model") == model):
            out.append(name)
    return out


# ---------------------------------------------------------------- чтение
def provider_row(p, with_key=True):
    key = None
    if with_key:
        needs = bool(p.key_file or p.key_env)
        st = keystore.status(p.key_file, p.key_env) if needs else {"present": None, "mask": None, "perms_ok": None}
        key = {"needed": needs, "present": st["present"], "mask": st["mask"], "file": p.key_file, "env": p.key_env, "perms_ok": st["perms_ok"]}
    return {"id": p.id, "name": p.name, "adapter": p.adapter, "enabled": p.enabled, "error": p.error, "base_url": p.base_url, "verified": p.verified, "note": p.note,
            "key": key, "timeouts": p.timeouts, "retry": p.retry, "limits": p.limits,
            "models": {k: {"name": m.name, "ctx": m.ctx, "max_out": m.max_out, "tools": m.tools, "strict": m.strict, "json_mode": m.json_mode, "thinking": m.thinking,
                           "cache": m.cache, "price": m.price, "tags": m.tags, "max_tokens_field": m.max_tokens_field, "no_temperature": m.no_temperature}
                       for k, m in p.models.items()}}


def listing(root=None):
    reg = providers.load(root)
    loc = (_local(root).get("providers") or {})
    rows = []
    for p in reg.providers.values():
        r = provider_row(p)
        r["local"] = p.id in loc
        r["builtin"] = p.id not in loc or p.id in _builtin_ids(root)
        r["profiles"] = _profiles_using(root, p.id)
        rows.append(r)
    return {"providers": rows, "errors": reg.errors, "budget": reg.budget}


def _builtin_ids(root):
    try:
        with open(providers.providers_file(root), encoding="utf-8") as f:
            return set((json.load(f).get("providers") or {}).keys())
    except (OSError, ValueError):
        return set()


# ---------------------------------------------------------------- запись
def add(root, pid, preset=None, spec=None, enable=False):
    """Добавляет провайдера из шаблона (preset) и/или полной записи spec (spec поверх шаблона). Новый провайдер ВЫКЛЮЧЕН; enable=True включает только при наличии ключа."""
    if not (isinstance(pid, str) and providers.RE_ID.match(pid)):
        return _res(False, "E_ARGS", "id: строчные буквы, цифры, _ и -, до 30 символов")
    if pid in _builtin_ids(root) or pid in (_local(root).get("providers") or {}):
        return _res(False, "E_EXISTS", "провайдер %s уже есть: используйте update" % pid)
    base = {}
    if preset:
        t = presets(root).get(preset)
        if t is None:
            return _res(False, "E_ARGS", "шаблон %s неизвестен" % str(preset)[:20])
        base = copy.deepcopy(t)
    if isinstance(spec, dict):
        base = localcfg.merge(base, spec)
    if not base:
        return _res(False, "E_ARGS", "нужен preset или spec")
    base["enabled"] = False
    if base.get("key_file") or base.get("key_env"):
        bad = keystore.check_spec(base.get("key_file"), base.get("key_env"))
        if bad:
            return _res(False, "E_ARGS", bad)
    base.setdefault("verified", False)
    err = _validate_effective(root, lambda d: _set_provider(d, pid, base))
    if err:
        return _res(False, "E_ARGS", err)
    try:
        _update(root, lambda d: _set_provider(d, pid, base))
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    r = _res(True, "OK", "провайдер добавлен (выключен, не проверен живым ключом)", id=pid)
    if enable:
        r = set_enabled(root, pid, True)
    return r


def _set_provider(d, pid, body):
    d.setdefault("version", 1)
    d.setdefault("providers", {})[pid] = body
    return d


def update(root, pid, patch):
    """Меняет поля провайдера (name, base_url, timeouts, retry, limits, key_file, key_env, note, verified, adapter) слоем local. Модели меняют model_set/model_remove."""
    reg = _effective(root)
    if reg is None or pid not in reg.providers:
        return _res(False, "E_ARGS", "провайдер неизвестен")
    allowed = ("name", "adapter", "base_url", "beta_url", "key_file", "key_env", "timeouts", "retry", "limits", "note", "verified", "version_header")
    if not isinstance(patch, dict) or not patch or any(k not in allowed for k in patch):
        return _res(False, "E_ARGS", "можно менять: %s" % ", ".join(allowed))
    kf, ke = patch.get("key_file", reg.providers[pid].key_file), patch.get("key_env", reg.providers[pid].key_env)
    if ("key_file" in patch or "key_env" in patch) and (kf or ke):
        bad = keystore.check_spec(kf, ke)
        if bad:
            return _res(False, "E_ARGS", bad)

    def mut(d):
        d.setdefault("version", 1)
        d.setdefault("providers", {}).setdefault(pid, {})
        d["providers"][pid] = localcfg.merge(d["providers"][pid], patch)
        return d
    err = _validate_effective(root, mut)
    if err:
        return _res(False, "E_ARGS", err)
    try:
        _update(root, mut)
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "провайдер обновлён", id=pid)


def set_enabled(root, pid, enabled):
    """Включение требует ключ (если провайдеру он нужен). Выключение запрещено, пока им пользуется live-роль (проверяет вызывающий через brain.profiles)."""
    reg = _effective(root)
    if reg is None or pid not in reg.providers:
        return _res(False, "E_ARGS", "провайдер неизвестен")
    p = reg.providers[pid]
    if enabled:
        if p.error:
            return _res(False, "E_ARGS", "в конфигурации ошибка: %s" % p.error)
        if p.key_file or p.key_env:
            if not keystore.status(p.key_file, p.key_env)["present"]:
                return _res(False, "E_NO_KEY", "нет ключа: ./tb llm key set %s (или ввод ключа в дашборде)" % pid)

    def mut(d):
        d.setdefault("version", 1)
        d.setdefault("providers", {}).setdefault(pid, {})["enabled"] = bool(enabled)
        return d
    try:
        _update(root, mut)
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "провайдер %s" % ("включён" if enabled else "выключен"), id=pid, enabled=bool(enabled))


def remove(root, pid):
    """Удаляет провайдера, добавленного владельцем (слой local). Поставляемых удалять нельзя (только выключить). Нельзя, пока есть профили."""
    if pid in _builtin_ids(root):
        return _res(False, "E_STATE", "поставляемого провайдера удалить нельзя: выключите")
    if pid not in (_local(root).get("providers") or {}):
        return _res(False, "E_ARGS", "провайдер неизвестен")
    use = _profiles_using(root, pid)
    if use:
        return _res(False, "E_IN_USE", "на провайдера ссылаются профили: %s" % ", ".join(use[:5]), profiles=use)

    def mut(d):
        (d.get("providers") or {}).pop(pid, None)
        return d
    try:
        _update(root, mut)
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "провайдер удалён", id=pid)


def model_set(root, pid, mkey, spec):
    """Создаёт/меняет модель провайдера (имя у провайдера, флаги, цены). Часть spec: поля слоем local поверх существующей записи."""
    reg = _effective(root)
    if reg is None or pid not in reg.providers:
        return _res(False, "E_ARGS", "провайдер неизвестен")
    if not (isinstance(mkey, str) and providers.RE_ID.match(mkey)) or not isinstance(spec, dict) or not spec:
        return _res(False, "E_ARGS", "ключ модели и поля обязательны")

    def mut(d):
        d.setdefault("version", 1)
        pr = d.setdefault("providers", {}).setdefault(pid, {})
        pr.setdefault("models", {})[mkey] = localcfg.merge(pr.get("models", {}).get(mkey, {}), spec)
        return d
    err = _validate_effective(root, mut)
    if err:
        return _res(False, "E_ARGS", err)
    try:
        _update(root, mut)
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "модель сохранена", provider=pid, model=mkey)


def model_remove(root, pid, mkey):
    reg = _effective(root)
    if reg is None or pid not in reg.providers or mkey not in reg.providers[pid].models:
        return _res(False, "E_ARGS", "модель неизвестна")
    use = _profiles_using(root, pid, mkey)
    if use:
        return _res(False, "E_IN_USE", "на модель ссылаются профили: %s" % ", ".join(use[:5]), profiles=use)
    if len(reg.providers[pid].models) <= 1:
        return _res(False, "E_STATE", "у провайдера должна остаться хотя бы одна модель")
    in_base = False
    try:
        with open(providers.providers_file(root), encoding="utf-8") as f:
            in_base = mkey in ((json.load(f).get("providers") or {}).get(pid) or {}).get("models", {})
    except (OSError, ValueError):
        pass
    if in_base:
        return _res(False, "E_STATE", "поставляемую модель удалить нельзя")

    def mut(d):
        ((d.get("providers") or {}).get(pid) or {}).get("models", {}).pop(mkey, None)
        return d
    try:
        _update(root, mut)
    except providers.ProviderError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "модель удалена", provider=pid, model=mkey)


# ---------------------------------------------------------------- проверка
def check(root, pid, mkey=None, live=False, transport=None, ping=True, environ=None):
    """Проверка провайдера (и модели): конфигурация и наличие ключа без сети; live=True делает запросы адаптером (GET /models + минимальный вызов).
    -> {"ok","code","msg","data":{"checked":[...]}}. Значения ключей не раскрываются."""
    from tb.llm import adapters
    from tb.llm.types import LLMError
    reg = _effective(root)
    if reg is None or pid not in reg.providers:
        return _res(False, "E_ARGS", "провайдер неизвестен")
    p = reg.providers[pid]
    rows, ok = [], p.error is None
    for mk, m in p.models.items():
        if mkey and mk != mkey:
            continue
        row = {"provider": pid, "model": mk, "enabled": p.enabled, "config_error": p.error}
        needs = bool(p.key_file or p.key_env)
        st = keystore.status(p.key_file, p.key_env) if needs else {"present": None, "mask": None}
        row["key"] = {"needed": needs, "present": st["present"], "mask": st["mask"]}
        if p.error:
            ok = False
        elif needs and not st["present"]:
            ok = False
            row["note"] = "ключа нет"
        elif live:
            from tb.llm.transport import Transport
            tr = transport or Transport()
            try:
                row["live"] = adapters.make(p, m, transport=tr, environ=environ).probe(ping=ping)
            except LLMError as e:
                ok = False
                row["live"] = {"error": e.code, "msg": e.msg}
            finally:
                if transport is None:
                    tr.close()
        rows.append(row)
    if not rows:
        return _res(False, "E_ARGS", "модель неизвестна")
    return _res(ok, "OK" if ok else "E_CHECK", "проверка пройдена" if ok else "есть проблемы", checked=rows, live=bool(live))
