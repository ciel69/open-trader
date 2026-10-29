"""Реестр ролей API-мозга: `config/roles/<role>.json` + `<role>.md` (WP11-C2; 07b п. 5.1).

Роль с ошибкой в файле отключается (в `reg.errors` текст без содержимого файлов), остальные работают. Системный промпт = текст `.md` (первая строка `# prompt vN`,
N совпадает с `version` в json) + схема ответа, сериализованная детерминированно: стабильный префикс для кэша провайдера (без дат, id заданий и случайных порядков).
WP11-C3: финальные роли v1. Системное сообщение = ОБЩИЕ ПРАВИЛА (`config/roles/_common.md`, у всех ролей с `"common": true`, по умолчанию true) + текст роли + схема ответа:
общий кусок идёт первым, поэтому префикс одинаков у ролей и кэшируется; всё изменяемое (досье, события) только в пользовательском сообщении. `prompt_hash` покрывает весь системный текст.
Необязательное поле `tools_optional`: инструменты, которые добавляются роли, только если они уже есть в каталоге шлюза (add_case_review появится с WP15b).
"""
import json
import os
import re

from tb import paths
from tb.brain import validate
from tb.core import bus as bus_mod
from tb.llm import usage as usage_mod

ROLE_SCHEMA = "tb.role/1"
RE_ROLE = re.compile(r"^[a-z][a-z_]{1,30}$")
RE_PROMPT_VER = re.compile(r"^#\s*prompt\s+v(\d+)")
COMMON_FILE = "_common.md"
IO_MODES = ("decision_json", "tools")
SCOPES = ("team", "shared")
KEYS_REQUIRED = ("schema", "id", "version", "io", "agent_scope", "tools", "max_rounds", "output_schema", "max_output_tokens", "live_risk", "timeout_s", "retries_format")


def roles_dir(root=None):
    return paths.config("roles", root=root)


def compact_schema(sch):
    """Схема для промпта: свободные вложенные значения (validate._FREE) сворачиваются в {"free":1}, пустые minLength опускаются; смысл проверки не меняется (валидатор берёт полную схему)."""
    if sch is validate._FREE:
        return {"free": 1}
    if isinstance(sch, dict):
        return {k: compact_schema(v) for k, v in sch.items() if not (k == "minLength" and v == 0)}
    if isinstance(sch, list):
        return [compact_schema(v) for v in sch]
    return sch


def read_common(root=None):
    """Текст общих правил для ролей (или пустая строка, если файла нет)."""
    try:
        with open(os.path.join(roles_dir(root), COMMON_FILE), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


class Role(object):
    def __init__(self, raw, prompt, common="", tools_extra=()):
        self.raw = raw
        self.id, self.version, self.io, self.scope = raw["id"], raw["version"], raw["io"], raw["agent_scope"]
        self.tools = list(raw["tools"]) + [t for t in tools_extra if t not in raw["tools"]]
        self.common = (common or "") if raw.get("common", True) is not False else ""
        self.max_rounds, self.output_schema = raw["max_rounds"], raw["output_schema"]
        self.max_output_tokens, self.live_risk = raw["max_output_tokens"], raw["live_risk"]
        self.timeout_s, self.retries_format = raw["timeout_s"], raw["retries_format"]
        self.params = dict(raw.get("params") or {})
        self.wake_on = list(raw.get("wake_on") or [])
        self.wake_kinds = list(raw.get("wake_kinds") or [])          # для scanner_signal: будят только эти kind (idea_* и прочее роль не будят)
        self.inputs = list(raw.get("inputs") or [])
        self.prompt = prompt
        self.schema = validate.schema_for(self.output_schema)

    @property
    def system(self):
        """Стабильный системный текст: роль + схема (sort_keys). Динамики нет."""
        sch = json.dumps(compact_schema(self.schema), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        head = (self.common.rstrip() + "\n\n") if self.common else ""
        return "%s%s\n\nRESPONSE SCHEMA (%s):\n%s\n" % (head, self.prompt.rstrip(), self.output_schema, sch)

    @property
    def prompt_hash(self):
        return usage_mod.prompt_hash(self.system)


class RoleRegistry(object):
    def __init__(self):
        self.roles, self.errors = {}, []

    def get(self, rid):
        return self.roles.get(rid)

    def for_event_types(self, types, mapping=None, kinds=None):
        """Роли, которым нужно проснуться хотя бы на один из типов событий (порядок: position_manager, reviewer, morning_analyst, entry_scout, прочие).
        mapping: {роль: [типы]} из подписки (config/subscriptions.json, поле roles) заменяет wake_on роли; роль, которой нет в mapping, спит.
        kinds: виды сигналов сканера задания (job.kinds); у роли с wake_kinds тип scanner_signal будит её только при совпадении вида. kinds=None: вид не проверяется."""
        ts = set(types or [])
        order = ("position_manager", "reviewer", "morning_analyst", "entry_scout")
        out = []
        for r in self.roles.values():
            hit = ts & set((mapping.get(r.id) or ()) if mapping is not None else r.wake_on)
            if hit == {"scanner_signal"} and r.wake_kinds and kinds is not None and not (set(kinds) & set(r.wake_kinds)):
                continue
            if hit:
                out.append(r)
        return sorted(out, key=lambda r: (order.index(r.id) if r.id in order else 9, r.id))


def _check(raw, rid, catalog_names, event_types):
    for k in KEYS_REQUIRED:
        if k not in raw:
            return "нет поля %s" % k
    if raw["schema"] != ROLE_SCHEMA or raw["id"] != rid or not RE_ROLE.match(rid):
        return "schema/id не совпадают с именем файла"
    if raw["io"] not in IO_MODES or raw["agent_scope"] not in SCOPES:
        return "io/agent_scope"
    if not (isinstance(raw["version"], int) and not isinstance(raw["version"], bool) and raw["version"] >= 0):
        return "version"
    if not isinstance(raw["tools"], list) or any(t not in catalog_names for t in raw["tools"]):
        return "tools: неизвестный инструмент шлюза"
    for k, lo, hi in (("max_rounds", 1, 12), ("max_output_tokens", 50, 16000), ("timeout_s", 5, 600), ("retries_format", 0, 4)):
        v = raw[k]
        if not (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi):
            return "%s: %d..%d" % (k, lo, hi)
    if raw["output_schema"] not in validate.SCHEMAS:
        return "output_schema неизвестна"
    if not isinstance(raw["live_risk"], bool):
        return "live_risk: bool"
    wk = raw.get("wake_on", [])
    if not isinstance(wk, list) or any(t not in event_types for t in wk):
        return "wake_on: неизвестный тип события"
    wkn = raw.get("wake_kinds", [])
    if not isinstance(wkn, list) or any(k not in bus_mod.SCANNER_KINDS for k in wkn):
        return "wake_kinds: неизвестный вид сигнала сканера"
    opt = raw.get("tools_optional", [])
    if not isinstance(opt, list) or any(not isinstance(t, str) for t in opt):
        return "tools_optional: список имён"
    if not isinstance(raw.get("common", True), bool):
        return "common: bool"
    prm = raw.get("params", {})
    if not isinstance(prm, dict) or any(k not in ("temperature",) for k in prm):
        return "params: только temperature"
    return None


def load(root=None):
    """-> RoleRegistry (все .json каталога config/roles)."""
    reg = RoleRegistry()
    d = roles_dir(root)
    try:
        names = sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        reg.errors.append("нет каталога config/roles")
        return reg
    try:
        from tb.core import bus
        from tb.gateway import catalog
        cat, evt = set(catalog.TOOLS), set(bus.EVENT_TYPES)
    except Exception:
        reg.errors.append("каталог инструментов шлюза недоступен")
        return reg
    common = read_common(root)
    for rid in names:
        try:
            with open(os.path.join(d, rid + ".json"), encoding="utf-8") as f:
                raw = json.load(f)
            with open(os.path.join(d, rid + ".md"), encoding="utf-8") as f:
                prompt = f.read()
        except (OSError, ValueError):
            reg.errors.append("роль %s отключена: файл json/md не читается" % rid)
            continue
        if not isinstance(raw, dict):
            reg.errors.append("роль %s отключена: json не объект" % rid)
            continue
        bad = _check(raw, rid, cat, evt)
        if bad is None:
            m = RE_PROMPT_VER.match(prompt.lstrip())
            if not m or int(m.group(1)) != raw["version"]:
                bad = "первая строка .md должна быть '# prompt v%s'" % raw["version"]
        if bad:
            reg.errors.append("роль %s отключена: %s" % (rid, bad))
            continue
        reg.roles[rid] = Role(raw, prompt, common, [t for t in raw.get("tools_optional", []) if t in cat])
    return reg


def prompt_info(root=None):
    """-> {роль: {"version","prompt_hash","chars"}} для отчётов eval/status."""
    reg = load(root)
    return {rid: {"version": r.version, "prompt_hash": r.prompt_hash, "chars": len(r.system)} for rid, r in sorted(reg.roles.items())}
