"""Загрузчик `config/brain.json` с проверкой (WP11-C2; 07b п. 6.2).

Профиль = (провайдер, модель, параметры, live_ok). Роль внутри команды/общая = основной профиль + цепочка запасных. Торгует в live ровно `trading.live_team`.
Сравнительные команды (`trading.compare`) ОТЛОЖЕНЫ: флаг читается и проверяется (команда с правом live в `agents.json` = ошибка конфигурации), но ничего не запускает.
Ошибка записи не роняет загрузку: профиль/роль с ошибкой отключается, текст ошибки (без содержимого файла) попадает в `conf.errors`.
Имён моделей в коде нет: профили ссылаются на (provider, model) из `config/providers.json`.
"""
import copy
import json
import re

from tb import paths

RE_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
RE_TEAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,9}$")
RE_AGENT = re.compile(r"^[a-z][a-z0-9_-]{0,9}$")
ROLE_SCOPE = {"news_reader": "shared", "reviewer": "shared", "day_auditor": "shared", "morning_analyst": "team", "position_manager": "team",
              "entry_scout": "team"}
THINKING = ("off", "low", "medium", "high")
FALLBACK_ON = ("timeout", "http_5xx", "rate", "budget", "format_x2")

DEFAULTS = {
    "budget": {"day_usd": 2.0, "job_usd": 0.05, "job_tokens": 60000, "jobs_per_hour": 12, "critical_per_hour": 30, "warn_frac": 0.8},
    "swap_rules": {"min_paper_trades": 30, "min_paper_days": 7, "soft": True, "max_invariant_violations": 0, "min_eval_valid_rate": 0.95, "cooldown_min": 240,
                   "max_swaps_day": 3, "require_live_ok": True, "phrase": "СМЕНИТЬ ТОРГОВОГО АГЕНТА {old} -> {new}",
                   "phrase_model": "СМЕНИТЬ МОДЕЛЬ {role} {old} -> {new}", "allow_with_open_positions": True, "probe_max_age_s": 300},
    "eval": {"min_valid_first": 0.95, "max_critical_violations": 0, "max_violation_rate": 0.05, "min_decision_match": 0.8, "max_usd": 0.5},
    "fallback": {"on": list(FALLBACK_ON), "live_risk_requires_live_ok": True, "protect_only_after_s": 120, "cooldown_s": {"base": 60, "max": 900}},
    "runtime": {"workers": 2, "poll_s": 1.0, "job_ttl_s": 900, "max_attempts": 3, "stale_taken_s": 300, "keep_jobs_s": 259200,
                "guards": {"same_decision_max": 3, "same_decision_window_s": 3600, "loop_jobs_max": 8, "loop_window_s": 600, "loop_block_s": 600,
                           "jobs_total_per_hour": 60, "scan_jobs_per_hour": 6, "role_jobs_per_hour": {}}},
}


class ConfError(Exception):
    pass


def conf_file(root=None):
    return paths.config("brain.json", root=root)


def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


class Profile(object):
    def __init__(self, pid, raw):
        self.id, self.raw = pid, raw
        self.provider, self.model = raw.get("provider"), raw.get("model")
        self.params = dict(raw.get("params") or {})
        self.live_ok = raw.get("live_ok") is True
        self.eval_ts = raw.get("eval_ts") if _num(raw.get("eval_ts")) else None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class BrainConf(object):
    def __init__(self):
        self.profiles, self.shared_roles, self.teams = {}, {}, {}
        self.live_team, self.compare = None, {"enabled": False, "teams": []}
        self.role_opts = {}                  # {роль: {"params": {temperature,max_tokens,thinking}, "budget": {"day_usd"}}}: переопределения владельца на уровне роли
        self.budget, self.swap_rules, self.fallback, self.runtime, self.eval = (copy.deepcopy(DEFAULTS[k]) for k in ("budget", "swap_rules", "fallback", "runtime", "eval"))
        self.errors, self.warnings = [], []
        self.loaded = False

    def assignments(self):
        """-> {"live_team","teams":{...},"shared_roles":{...}} (то, что копируется в active.json)."""
        return {"live_team": self.live_team, "teams": copy.deepcopy(self.teams), "shared_roles": copy.deepcopy(self.shared_roles)}

    def agent_of(self, team=None):
        t = self.teams.get(team or self.live_team) or {}
        return t.get("agent_id")


def _chain(raw, profiles, where, errors):
    """{"primary","fallback"} -> проверенный словарь или None."""
    if not isinstance(raw, dict) or raw.get("primary") not in profiles:
        errors.append("%s: основной профиль неизвестен" % where)
        return None
    fb = raw.get("fallback", [])
    if not isinstance(fb, list) or any(x not in profiles for x in fb):
        errors.append("%s: запасной профиль неизвестен" % where)
        return None
    return {"primary": raw["primary"], "fallback": list(fb)}


def check_params(prm):
    """Параметры вызова модели (профиль или роль) -> текст ошибки или None."""
    if not isinstance(prm, dict):
        return "params: объект"
    if any(k not in ("temperature", "max_tokens", "thinking") for k in prm):
        return "params: только temperature, max_tokens, thinking"
    if "thinking" in prm and prm["thinking"] not in THINKING:
        return "params.thinking: %s" % "|".join(THINKING)
    if "temperature" in prm and not _num(prm["temperature"], 0, 2):
        return "params.temperature: 0..2"
    if "max_tokens" in prm and not (isinstance(prm["max_tokens"], int) and not isinstance(prm["max_tokens"], bool) and 16 <= prm["max_tokens"] <= 65536):
        return "params.max_tokens: 16..65536"
    return None


def check_role_opts(role, raw):
    """-> ({"params","budget"}, ошибка|None). Бюджет роли: day_usd (потолок расхода роли за сутки по учёту llm_calls)."""
    if role not in ROLE_SCOPE:
        return {}, "неизвестная роль"
    if not isinstance(raw, dict) or any(k not in ("params", "budget") for k in raw):
        return {}, "ожидается {params, budget}"
    prm = raw.get("params", {})
    bad = check_params(prm)
    if bad:
        return {}, bad
    bud = raw.get("budget", {})
    if not isinstance(bud, dict) or any(k != "day_usd" for k in bud) or any(not _num(v, 0) for v in bud.values()):
        return {}, "budget: {day_usd: число >= 0}"
    return {"params": dict(prm), "budget": dict(bud)}, None


def live_ok_warnings(c):
    """Мягкие замечания: цепочка live-роли без live_ok (пересчитывается после наложения подписанных меток eval)."""
    c.warnings[:] = [w for w in c.warnings if "профили без live_ok" not in w]
    for tid, t in c.teams.items():
        for role, ch in t["roles"].items():
            bad = [p for p in [ch["primary"]] + ch["fallback"] if not c.profiles[p].live_ok]
            if bad and tid == c.live_team:
                c.warnings.append("команда %s/%s: профили без live_ok: %s" % (tid, role, ",".join(sorted(set(bad)))))


def parse(doc, registry=None, agents=None):
    """doc (dict из brain.json) -> BrainConf. registry: tb.llm.providers.Registry (проверка provider/model); agents: словарь agents.json["agents"] (проверка прав команд)."""
    c = BrainConf()
    if not isinstance(doc, dict) or doc.get("version") != 1:
        c.errors.append("brain.json: ожидается {version:1, ...}")
        return c
    c.loaded = True
    errs = c.errors
    for name in ("budget", "swap_rules", "fallback", "runtime", "eval"):
        raw = doc.get(name)
        merged = _merge(DEFAULTS[name], raw if isinstance(raw, dict) else {})
        setattr(c, name, merged)
    b = c.budget
    for k in ("day_usd", "job_usd", "job_tokens", "jobs_per_hour", "critical_per_hour", "warn_frac"):
        if not _num(b.get(k), 0):
            errs.append("budget.%s: число >= 0" % k)
            b[k] = DEFAULTS["budget"][k]
    sr = c.swap_rules
    if not (isinstance(sr.get("phrase"), str) and "{old}" in sr["phrase"] and "{new}" in sr["phrase"]):
        errs.append("swap_rules.phrase: нужны {old} и {new}")
        sr["phrase"] = DEFAULTS["swap_rules"]["phrase"]
    if not (isinstance(sr.get("phrase_model"), str) and "{new}" in sr["phrase_model"]):
        sr["phrase_model"] = DEFAULTS["swap_rules"]["phrase_model"]
    fb = c.fallback
    if not isinstance(fb.get("on"), list) or any(x not in FALLBACK_ON for x in fb["on"]):
        errs.append("fallback.on: подмножество %s" % (FALLBACK_ON,))
        fb["on"] = list(FALLBACK_ON)
    rt = c.runtime
    g = rt.get("guards") if isinstance(rt.get("guards"), dict) else {}
    if not (isinstance(g.get("scan_jobs_per_hour"), int) and not isinstance(g.get("scan_jobs_per_hour"), bool) and g["scan_jobs_per_hour"] >= 0):
        errs.append("runtime.guards.scan_jobs_per_hour: целое >= 0")
        g["scan_jobs_per_hour"] = DEFAULTS["runtime"]["guards"]["scan_jobs_per_hour"]
    rj = g.get("role_jobs_per_hour")
    if not isinstance(rj, dict) or any(k not in ROLE_SCOPE or isinstance(v, bool) or not isinstance(v, int) or v < 0 for k, v in rj.items()):
        errs.append("runtime.guards.role_jobs_per_hour: {роль: целое >= 0}")
        g["role_jobs_per_hour"] = {}
    if not (isinstance(rt.get("workers"), int) and 1 <= rt["workers"] <= 4):
        errs.append("runtime.workers: 1..4")
        rt["workers"] = 1
    # ---- профили
    profs = doc.get("profiles")
    if not isinstance(profs, dict):
        errs.append("profiles: объект")
        profs = {}
    for pid, raw in profs.items():
        if not (isinstance(pid, str) and RE_PROFILE.match(pid)) or not isinstance(raw, dict):
            errs.append("профиль %r: некорректный id или запись" % str(pid)[:30])
            continue
        p = Profile(pid, raw)
        prm = p.params
        bad = None
        if not (isinstance(p.provider, str) and isinstance(p.model, str)):
            bad = "provider и model обязательны"
        elif prm.get("thinking", "off") not in THINKING:
            bad = "params.thinking: %s" % "|".join(THINKING)
        elif "temperature" in prm and not _num(prm["temperature"], 0, 2):
            bad = "params.temperature: 0..2"
        elif "max_tokens" in prm and not (isinstance(prm["max_tokens"], int) and not isinstance(prm["max_tokens"], bool) and 16 <= prm["max_tokens"] <= 65536):
            bad = "params.max_tokens: 16..65536"
        elif registry is not None:
            try:
                registry.model(p.provider, p.model)
            except Exception:
                bad = "нет %s/%s в providers.json" % (str(p.provider)[:20], str(p.model)[:20])
        if bad:
            errs.append("профиль %s отключён: %s" % (pid, bad))
            continue
        c.profiles[pid] = p
    # ---- общие роли
    sh = doc.get("shared_roles")
    for role, raw in (sh.items() if isinstance(sh, dict) else []):
        if ROLE_SCOPE.get(role) != "shared":
            errs.append("shared_roles.%s: роль не общая" % str(role)[:30])
            continue
        ch = _chain(raw, c.profiles, "shared_roles.%s" % role, errs)
        if ch:
            c.shared_roles[role] = ch
    # ---- команды
    teams = doc.get("teams")
    for tid, raw in (teams.items() if isinstance(teams, dict) else []):
        if not (isinstance(tid, str) and RE_TEAM.match(tid)) or not isinstance(raw, dict) or not (isinstance(raw.get("agent_id"), str) and RE_AGENT.match(raw["agent_id"])):
            errs.append("команда %r: некорректный id или agent_id" % str(tid)[:20])
            continue
        roles = {}
        for role, rr in (raw.get("roles") or {}).items():
            if ROLE_SCOPE.get(role) != "team":
                errs.append("teams.%s.%s: роль не командная" % (tid, str(role)[:30]))
                continue
            ch = _chain(rr, c.profiles, "teams.%s.%s" % (tid, role), errs)
            if ch:
                roles[role] = ch
        c.teams[tid] = {"agent_id": raw["agent_id"], "roles": roles}
    ro = doc.get("role_opts")
    for role, raw in (ro.items() if isinstance(ro, dict) else []):
        o, bad = check_role_opts(role, raw)
        if bad:
            errs.append("role_opts.%s: %s" % (str(role)[:30], bad))
        else:
            c.role_opts[role] = o
    tr = doc.get("trading") if isinstance(doc.get("trading"), dict) else {}
    c.live_team = tr.get("live_team")
    if c.live_team not in c.teams:
        errs.append("trading.live_team: неизвестная команда")
        c.live_team = None
    cmp_ = tr.get("compare") if isinstance(tr.get("compare"), dict) else {}
    c.compare = {"enabled": cmp_.get("enabled") is True, "teams": [t for t in (cmp_.get("teams") or []) if isinstance(t, str)], "exec": cmp_.get("exec", "dry"),
                 "max_usd_day": cmp_.get("max_usd_day", 0.5), "roles": list(cmp_.get("roles") or [])}
    if c.compare["enabled"]:
        c.warnings.append("trading.compare.enabled=true: сравнительные команды отложены, запуск не выполняется")
    for t in c.compare["teams"]:
        if t not in c.teams:
            errs.append("compare.teams: неизвестная команда %s" % t[:10])
        elif agents is not None:
            ag = agents.get(c.teams[t]["agent_id"])
            modes = (((ag or {}).get("limits") or {}).get("modes")) or []
            if "live" in modes:
                errs.append("COMPARE_LIVE_RIGHT: сравнительная команда %s имеет право live" % t[:10])
    live_ok_warnings(c)
    return c


def load(root=None, registry=None, agents=None, path=None):
    """-> BrainConf. Нет файла/битый JSON -> conf.loaded=False и conf.errors."""
    p = path or conf_file(root)
    c = BrainConf()
    try:
        if path:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        else:
            from tb.core import localcfg        # + config/local/brain.json
            doc = localcfg.read("brain", root)
    except OSError:
        c.errors.append("нет файла brain.json")
        return c
    except ValueError:
        c.errors.append("brain.json: не JSON")
        return c
    if registry is None:
        try:
            from tb.llm import providers
            registry = providers.load(root)
        except Exception:
            registry = None
    if agents is None:
        agents = read_agents(root)
    c = parse(doc, registry, agents)
    if path is None and c.loaded:
        try:
            from tb.brain import liveok
            liveok.overlay(c, root)
            live_ok_warnings(c)              # подписанные метки live_ok от живого eval (WP11-C3); нет ключа/файла -> без изменений
        except Exception:
            pass
    return c


def read_agents(root=None):
    try:
        with open(paths.config("agents.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        a = d.get("agents") if isinstance(d, dict) else None
        return a if isinstance(a, dict) else None
    except (OSError, ValueError):
        return None
