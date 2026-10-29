"""Реестр агентов шлюза (WP11-A): `config/agents.json`, токены, ограничения.

Файл хранит ТОЛЬКО хэш токена (`sha256:<hex>`). Сам токен печатается один раз командой `./tb gateway token new ID` и кладётся владельцем в
`<каталог ключей>/agents/<id>.token` (каталог: TB_KEYS_DIR или ~/.config/traider-bot; права файла 0600 где применимо) либо в переменную окружения
TB_AGENT_TOKEN процесса агента. Токен никогда не пишется в аудит, ответы и тексты исключений.

Ограничения агента (`limits`) ТОЛЬКО сужают то, что допускают guard и exec_limits: шлюз их проверяет дополнительно и ничего не расширяет.
Неверная запись в реестре отключает агента (fail closed), а не открывает доступ.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time

from tb import paths

RE_AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,9}$")       # brain:<id> должен уместиться в 16 символов (дашборд)
AUTH_MODES = ("token", "local")
MODES = ("paper", "live")
TOKEN_ENV = "TB_AGENT_TOKEN"
HASH_PREFIX = "sha256:"
LIMIT_DEFAULTS = {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": [], "modes": ["paper"], "max_size_mult": 1.0, "allow_add": False,
                  "calls_per_min": 30, "plans_per_hour": 4}
_RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")


class RegistryError(Exception):
    pass


def config_file(root=None):
    return paths.config("agents.json", root=root)


def token_dir(environ=None):
    """Каталог токенов агентов: <TB_KEYS_DIR | ~/.config/traider-bot>/agents. Читаются только файлы <id>.token."""
    env = os.environ if environ is None else environ
    base = env.get("TB_KEYS_DIR") or os.path.join(env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~"), ".config", "traider-bot")
    return os.path.join(base, "agents")


def hash_token(token):
    return HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


def verify_token(token, token_hash):
    if not isinstance(token, str) or not token or not isinstance(token_hash, str) or not token_hash.startswith(HASH_PREFIX):
        return False
    return hmac.compare_digest(hash_token(token).encode("ascii"), token_hash.encode("ascii", "replace"))


def read_token(agent_id, environ=None):
    """Токен процесса агента: переменная окружения TB_AGENT_TOKEN, затем файл <id>.token. -> строка или None."""
    env = os.environ if environ is None else environ
    t = env.get(TOKEN_ENV)
    if t:
        return t.strip()
    try:
        with open(os.path.join(token_dir(env), agent_id + ".token"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_token_file(agent_id, token, environ=None):
    d = token_dir(environ)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, agent_id + ".token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


class Agent(object):
    def __init__(self, agent_id, role, auth, token_hash, tools, limits, consumer, enabled=True, error=None):
        self.id, self.role, self.auth, self.token_hash = agent_id, role, auth, token_hash
        self.tools, self.limits, self.consumer, self.enabled, self.error = tuple(tools), limits, consumer, enabled, error

    @property
    def actor(self):
        return "brain:" + self.id


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not (lo <= v <= hi):
        raise ValueError("число вне %s..%s" % (lo, hi))
    return float(v)


def _limits(raw):
    lim = dict(LIMIT_DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    if "max_risk_usdt" in raw:
        lim["max_risk_usdt"] = _num(raw["max_risk_usdt"], 0.0, 1000.0)
    if "max_risk_pct" in raw:
        lim["max_risk_pct"] = _num(raw["max_risk_pct"], 0.0, 100.0)
    if "max_size_mult" in raw:
        lim["max_size_mult"] = _num(raw["max_size_mult"], 0.0, 1.0)
    if "calls_per_min" in raw:
        lim["calls_per_min"] = int(_num(raw["calls_per_min"], 1, 6000))
    if "plans_per_hour" in raw:
        lim["plans_per_hour"] = int(_num(raw["plans_per_hour"], 0, 1000))
    if "allow_add" in raw:
        if not isinstance(raw["allow_add"], bool):
            raise ValueError("allow_add: bool")
        lim["allow_add"] = raw["allow_add"]
    if "modes" in raw:
        m = raw["modes"]
        if not isinstance(m, list) or not m or any(x not in MODES for x in m):
            raise ValueError("modes: подмножество %s" % (MODES,))
        lim["modes"] = sorted(set(m))
    if "symbols" in raw:
        s = raw["symbols"]
        if not isinstance(s, list) or any(not isinstance(x, str) or not (x == "*" or _RE_SYM.fullmatch(x)) for x in s):
            raise ValueError("symbols: список тикеров или [\"*\"]")
        lim["symbols"] = list(s)
    return lim


def expand_tools(items, catalog_tools):
    """['get_brief', '@read'] -> имена инструментов; неизвестное имя = ValueError (опечатка не должна тихо давать права)."""
    out = []
    for it in items:
        if not isinstance(it, str):
            raise ValueError("tools: строки")
        if it.startswith("@"):
            names = [n for n, t in catalog_tools.items() if t.right == it[1:]]
            if not names:
                raise ValueError("нет инструментов с правом %s" % it[1:])
        elif it in catalog_tools:
            names = [it]
        else:
            raise ValueError("неизвестный инструмент %s" % it[:30])
        out.extend(n for n in names if n not in out)
    return out


def parse_agent(agent_id, raw, catalog_tools):
    """-> Agent. Любая ошибка в записи -> Agent(enabled=False, error=...): отключённый агент не получает ничего."""
    try:
        if not RE_AGENT_ID.fullmatch(agent_id or ""):
            raise ValueError("id агента: ^[a-z][a-z0-9_-]{0,9}$")
        if not isinstance(raw, dict):
            raise ValueError("запись агента должна быть объектом")
        auth = raw.get("auth", "token")
        if auth not in AUTH_MODES:
            raise ValueError("auth: token|local")
        th = raw.get("token_hash")
        if th is not None and not (isinstance(th, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", th)):
            raise ValueError("token_hash: sha256:<64 hex>")
        consumer = raw.get("consumer", "chat")
        from tb.core import bus
        bus.check_consumer(consumer)
        role = raw.get("role", "agent")
        if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,30}", role):
            raise ValueError("role: идентификатор")
        tools = expand_tools(raw.get("tools") or [], catalog_tools)
        return Agent(agent_id, role, auth, th, tools, _limits(raw.get("limits")), consumer, enabled=raw.get("enabled", True) is not False)
    except (ValueError, TypeError) as e:
        return Agent(agent_id if isinstance(agent_id, str) else "?", "?", "token", None, [], dict(LIMIT_DEFAULTS), "chat", enabled=False,
                     error=str(e)[:120])


def load(catalog_tools, root=None):
    """-> {id: Agent}. Нет файла/битый JSON = пустой реестр (никто не авторизован)."""
    try:
        with open(config_file(root), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, dict):
        return {}
    return {k: parse_agent(k, v, catalog_tools) for k, v in agents.items() if isinstance(k, str)}


def authenticate(agent, token):
    """Проверка токена для транспортов за границей процесса (MCP). auth=local токена не требует. -> (ok, code)."""
    if agent is None:
        return False, "E_AGENT_UNKNOWN"
    if not agent.enabled:
        return False, "E_AGENT_DISABLED"
    if agent.auth == "local":
        return True, None
    if not agent.token_hash:
        return False, "E_AUTH_NO_TOKEN"
    return (True, None) if verify_token(token, agent.token_hash) else (False, "E_AUTH")


# ------------------------------------------------------------------ правка файла владельцем (CLI `gateway agent|token`)
def _read_raw(root):
    try:
        with open(config_file(root), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("agents"), dict):
        d["agents"] = {}
    return d


def _write_raw(d, root):
    path = config_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")
    from tb import fsutil
    fsutil.replace(tmp, path)


def add_agent(agent_id, role, tools, catalog_tools, root=None):
    """Новый агент: auth=token, режим только paper, лимиты по умолчанию, токена пока нет. Существующий не перезаписывается."""
    d = _read_raw(root)
    if agent_id in d["agents"]:
        raise RegistryError("агент %s уже есть" % agent_id)
    entry = {"role": role, "auth": "token", "token_hash": None, "enabled": True, "tools": list(tools), "limits": dict(LIMIT_DEFAULTS)}
    a = parse_agent(agent_id, entry, catalog_tools)
    if not a.enabled:
        raise RegistryError(a.error or "неверная запись")
    d["agents"][agent_id] = entry
    _write_raw(d, root)
    return a


def set_token_hash(agent_id, token_hash, root=None):
    d = _read_raw(root)
    if agent_id not in d["agents"]:
        raise RegistryError("нет агента %s" % agent_id)
    d["agents"][agent_id]["token_hash"] = token_hash
    _write_raw(d, root)


# ------------------------------------------------------------------ торговый флаг дня (WP11-C2; 07 п. 2.1)
TRADING_CODES = {"E_TRADING_FLAG": "торговый флаг нечитаем или отсутствует", "E_TRADING_OFF": "торговля выключена (торговый флаг)",
                 "E_TRADING_MODE": "режим торгового флага не допускает live", "E_TRADING_EXPIRED": "срок торгового флага истёк",
                 "E_TRADING_DAY": "торговый флаг относится к другому дню", "E_TRADING_AGENT": "торговый флаг выдан другому агенту"}


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def trading_allowed(root=None, mode="live", now=None, agent_id=None):
    """-> (bool, код|None). Контракт 07 п. 2.1: paper всегда доступен; не-paper (live/testnet-исполнитель) только при `state/control/trading.json`
    {"day_id","enabled":true,"mode":"live|testnet","agent","until_ts"} и совпадении day_id с `state/control/platform.json`. Нечитаемый/отсутствующий/битый файл = отказ (fail closed).
    Касается инструментов, создающих или ослабляющих риск (submit_plan, amend_plan с ослаблением, добор); ужесточение и закрытие не блокируются."""
    if mode == "paper":
        return True, None
    now = time.time() if now is None else now
    flag = _read_json_file(paths.state("control", "trading.json", root=root))
    if not isinstance(flag, dict):
        return False, "E_TRADING_FLAG"
    if flag.get("enabled") is not True:
        return False, "E_TRADING_OFF"
    if flag.get("mode") not in ("live", "testnet"):
        return False, "E_TRADING_MODE"
    until = flag.get("until_ts")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float)) or until < now):
        return False, "E_TRADING_EXPIRED"
    day = flag.get("day_id")
    plat = _read_json_file(paths.state("control", "platform.json", root=root))
    if not isinstance(day, str) or not day or not isinstance(plat, dict) or plat.get("day_id") != day:
        return False, "E_TRADING_DAY"
    ag = flag.get("agent")
    if agent_id is not None and ag is not None and ag != agent_id:
        return False, "E_TRADING_AGENT"
    return True, None
