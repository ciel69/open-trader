"""Реестр провайдеров: загрузка и проверка `config/providers.json` (WP11-C1). Имён моделей и цен в коде нет.

Некорректный провайдер отключается (enabled=False, error=текст), остальные работают. Секретов в файле нет; ключ читает только keys.py.
"""
import json
import os
import re

from tb import paths
from tb.core.secretfile import FORBIDDEN_ENV_PREFIX, FORBIDDEN_FILE_PREFIX

KNOWN_ADAPTERS = ("deepseek", "anthropic", "openai-compatible")
ADAPTER_ALIASES = {"openai_compat": "openai-compatible", "openai-compat": "openai-compatible", "openai_compatible": "openai-compatible"}     # имена для config/local


def norm_adapter(name):
    return ADAPTER_ALIASES.get(name, name)
RE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
RE_KEYFILE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
RE_ENVNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")
PRICE_KEYS = ("in", "in_miss", "in_hit", "out", "cache_write_factor", "offpeak_factor")
THINKING_MODES = ("param", "none")


class ProviderError(Exception):
    pass


def providers_file(root=None):
    return paths.config("providers.json", root=root)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


class Model(object):
    def __init__(self, key, name, raw, price):
        self.key, self.name, self.raw, self.price = key, name, raw, price
        self.ctx = raw.get("ctx")
        self.max_out = raw.get("max_out")
        self.tools = bool(raw.get("tools"))
        self.strict = bool(raw.get("strict"))
        self.json_mode = bool(raw.get("json_mode"))
        self.thinking = raw.get("thinking") or "none"
        self.cache = raw.get("cache") or "auto"
        self.tags = list(raw.get("tags") or [])
        self.extra_body = raw.get("extra_body") if isinstance(raw.get("extra_body"), dict) else {}
        self.thinking_map = raw.get("thinking_map") if isinstance(raw.get("thinking_map"), dict) else {}
        self.max_tokens_field = raw.get("max_tokens_field") if raw.get("max_tokens_field") in ("max_tokens", "max_completion_tokens") else "max_tokens"
        self.no_temperature = raw.get("no_temperature") is True       # модели с рассуждением не принимают temperature


class Provider(object):
    def __init__(self, id, raw, models, error=None):
        self.id, self.raw, self.models, self.error = id, raw, models, error
        self.adapter = norm_adapter(raw.get("adapter"))
        self.name = raw.get("name") if isinstance(raw.get("name"), str) else id
        self.verified = raw.get("verified") is True          # проверен ли живым ключом владельца (./tb llm check --live)
        self.note = raw.get("note") if isinstance(raw.get("note"), str) else None
        self.base_url = (raw.get("base_url") or "").rstrip("/")
        self.beta_url = (raw.get("beta_url") or "").rstrip("/") or None
        self.key_file, self.key_env = raw.get("key_file"), raw.get("key_env")
        self.enabled = bool(raw.get("enabled")) and error is None
        self.timeouts = dict(raw.get("timeouts") or {})
        self.retry = dict(raw.get("retry") or {})
        self.limits = dict(raw.get("limits") or {})
        self.version_header = raw.get("version_header")

    def model(self, key):
        m = self.models.get(key)
        if m is None:
            raise ProviderError("у провайдера %s нет модели %s" % (self.id, key))
        return m


def _check_provider(pid, raw):
    """-> (models, ошибка|None)."""
    if not isinstance(raw, dict):
        return {}, "запись не объект"
    if norm_adapter(raw.get("adapter")) not in KNOWN_ADAPTERS:
        return {}, "неизвестный adapter"
    bu = raw.get("base_url")
    if not isinstance(bu, str) or not re.match(r"^https?://[^\s/]+", bu):
        return {}, "base_url должен начинаться с http:// или https://"
    if raw.get("beta_url") is not None and not (isinstance(raw["beta_url"], str) and raw["beta_url"].startswith(("http://", "https://"))):
        return {}, "beta_url некорректен"
    kf, ke = raw.get("key_file"), raw.get("key_env")
    if kf is not None and not (isinstance(kf, str) and RE_KEYFILE.match(kf) and ".." not in kf):
        return {}, "key_file: только имя файла без пути"
    if ke is not None and not (isinstance(ke, str) and RE_ENVNAME.match(ke)):
        return {}, "key_env некорректен"
    if norm_adapter(raw.get("adapter")) != "openai-compatible" and not (kf and ke):
        return {}, "для этого адаптера нужны key_file и key_env"
    # провайдер не может указывать на ключи бирж/служебные файлы: иначе ключ биржи ушёл бы на чужой base_url
    if (isinstance(kf, str) and kf.lower().startswith(FORBIDDEN_FILE_PREFIX)) or (isinstance(ke, str) and ke.upper().startswith(FORBIDDEN_ENV_PREFIX)):
        return {}, "key_file/key_env: имена ключей бирж и служебных файлов запрещены"
    for blk, keys in (("timeouts", ("connect_s", "read_s", "total_s")), ("retry", ("max", "base_s", "max_s"))):
        b = raw.get(blk, {})
        if not isinstance(b, dict) or any(k in b and not (_num(b[k]) and b[k] >= 0) for k in keys):
            return {}, "%s: числа >= 0" % blk
    on = (raw.get("retry") or {}).get("on", [])
    if not isinstance(on, list) or any(not isinstance(x, int) or x < 400 or x > 599 or x in (400, 401, 402, 422) for x in on):
        return {}, "retry.on: коды 4xx/5xx, но не 400/401/402/422"
    lim = raw.get("limits", {})
    if not isinstance(lim, dict) or any(not (_num(v) and v >= 0) for v in lim.values()):
        return {}, "limits: числа >= 0"
    rm = raw.get("models")
    if not isinstance(rm, dict) or not rm:
        return {}, "нет models"
    models = {}
    for mk, m in rm.items():
        if not (isinstance(mk, str) and RE_ID.match(mk)) or not isinstance(m, dict):
            return {}, "модель %r: некорректный ключ или запись" % str(mk)[:20]
        nm = m.get("name")
        if not isinstance(nm, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$", nm):
            return {}, "модель %s: name" % mk
        if m.get("thinking", "none") not in THINKING_MODES:
            return {}, "модель %s: thinking" % mk
        pr = m.get("price")
        if not isinstance(pr, dict) or any(k not in PRICE_KEYS and k != "peak_utc" for k in pr) or any(not (_num(pr[k]) and pr[k] >= 0) for k in pr if k != "peak_utc"):
            return {}, "модель %s: price (числа >= 0, ключи %s)" % (mk, ",".join(PRICE_KEYS))
        pk = pr.get("peak_utc")
        if pk is not None and not (isinstance(pk, dict) and all(isinstance(d, int) and 0 <= d <= 6 for d in pk.get("days", []))
                                   and all(isinstance(h, list) and len(h) == 2 and all(isinstance(x, int) and 0 <= x <= 24 for x in h)
                                           for h in pk.get("hours", []))):
            return {}, "модель %s: price.peak_utc" % mk
        models[mk] = Model(mk, nm, m, pr)
    return models, None


class Registry(object):
    def __init__(self, providers, budget, errors, version=1):
        self.providers, self.budget, self.errors, self.version = providers, budget, errors, version

    def provider(self, pid):
        p = self.providers.get(pid)
        if p is None:
            raise ProviderError("неизвестный провайдер %s" % str(pid)[:30])
        return p

    def model(self, pid, mkey):
        return self.provider(pid).model(mkey)


def parse(doc):
    errors, provs = [], {}
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("providers"), dict):
        raise ProviderError("providers.json: ожидается {version:1, providers:{...}}")
    for pid, raw in doc["providers"].items():
        if not (isinstance(pid, str) and RE_ID.match(pid)):
            errors.append({"provider": str(pid)[:30], "error": "некорректный id"})
            continue
        models, err = _check_provider(pid, raw)
        provs[pid] = Provider(pid, raw if isinstance(raw, dict) else {}, models, err)
        if err:
            errors.append({"provider": pid, "error": err})
    budget = doc.get("budget") if isinstance(doc.get("budget"), dict) else {}
    return Registry(provs, budget, errors, doc.get("version"))


def load(root=None, path=None):
    """-> Registry. Нет файла или битый JSON -> ProviderError (текст без содержимого файла)."""
    p = path or providers_file(root)
    try:
        if path:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        else:
            from tb.core import localcfg        # + config/local/providers.json (enabled провайдеров и т.п.)
            doc = localcfg.read("providers", root)
    except OSError:
        raise ProviderError("нет файла %s" % os.path.basename(p))
    except ValueError:
        raise ProviderError("%s: не JSON" % os.path.basename(p))
    return parse(doc)
