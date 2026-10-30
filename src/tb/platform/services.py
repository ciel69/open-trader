"""Сервисы платформы: конфиг `config/platform.json`, пути `runtime/platform/`, команды и окружение дочерних процессов. Владелец: WP11-B.

Сервис = дочерний процесс супервизора. Список и порядок запуска: exec, scan, feed, context, intel, sync, dispatch, ctl, dash, mcp, brain. Ключи Bybit получают ТОЛЬКО исполнитель
(launcher грузит ключ в его собственный процесс для testnet/trade) и сервис sync (launcher грузит читающий ключ `read`, только баланс); окружение остальных чистится в `child_env`.
Без fork и сигнальных API: только subprocess (кроссплатформенно); сетевых вызовов и торговых модулей здесь нет.
"""
import copy
import json
import os
import re
import sys
import time

from tb import paths
from tb.core import localcfg
from tb.core.filelock import atomic_write_json

PROFILES = ("paper", "testnet", "trade")
ORDER = ("exec", "scan", "feed", "context", "intel", "sync", "dispatch", "ctl", "dash", "mcp", "brain")      # порядок запуска; остановка в обратном, но exec последним (см. stop_order)
SERVICE_NAMES = ORDER
SECRET_PREFIXES = ("BYBIT_", "ANTHROPIC_")                        # не передаются никому, кроме процесса, который грузит их сам (launcher)
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
# что сделать, чтобы закрыть предстартовую проверку P1-P16 (lifecycle.preflight и дашборд показывают одну строку действия)
PREFLIGHT_FIX = {
    "P1": "запустить платформу: ./tb start",
    "P2": "дождаться сверки исполнителя или проверить ./tb exec status",
    "P3": "проверить интернет и сервис exec: ./tb status",
    "P4": "проверить сервисы scan и dispatch: ./tb status (при необходимости ./tb up заново)",
    "P5": "снять KILL в терминале: ./tb exec resume",
    "P6": "проверить ключи: ./tb setup --check",
    "P7": "дождаться сервиса sync (баланс раз в 5 минут) или ./tb portfolio set",
    "P8": "закрыть позицию или привязать к плану: ./tb plan adopt",
    "P9": "исправить конфиг: ./tb day preflight покажет какой",
    "P10": "перезапустить платформу (./tb restart): сервисы feed и context входят в набор по умолчанию",
    "P11": "проверить мозг: ./tb brain probe и ./tb llm check",
    "P12": "освободить диск или очистить runtime/logs",
    "P13": "лимит убытка исчерпан: торговля до завтра запрещена",
    "P14": "подождать окончания окна новостей",
    "P15": "запустить ./tb intel verify (в Terminal.app) и держать сервис intel в наборе",
    "P16": "завершить предыдущий день: ./tb day status",
}
RE_SERVICE = re.compile(r"^[a-z]{2,12}$")

DEFAULTS = {
    "version": 1,
    "profile": "paper",
    "services": {                       # какие сервисы поднимаются по умолчанию в профиле (`--services` перекрывает)
        "paper": ["exec", "scan", "feed", "context", "intel", "sync", "dispatch", "ctl", "dash", "brain"],
        "testnet": ["exec", "scan", "feed", "context", "intel", "sync", "dispatch", "ctl", "dash", "brain"],
        "trade": ["exec", "scan", "feed", "context", "intel", "sync", "dispatch", "ctl", "dash"],
    },
    "supervisor": {
        "poll_s": 1.0,                  # период цикла контроля
        "status_s": 2.0,                # период записи heartbeat и status.json
        "stop_grace_s": 8.0,            # ожидание мягкой остановки сервиса, затем kill
        "exec_stop_grace_s": 20.0,
        "stable_s": 120.0,              # проработал дольше: счётчик подряд идущих сбоев обнуляется
        "restart": {"base_s": 2.0, "factor": 2.0, "max_s": 60.0, "max_tries": 5},
        "retry_after_giveup_s": 900.0,  # сдавшийся сервис пробуется заново раз в 15 минут (0 = никогда, только `./tb up`)
        "fatal_codes": [2, 4],          # код выхода: ошибка использования / не реализовано / второй экземпляр: не перезапускать
        "fatal_within_s": 10.0,         # ...если сервис завершился так быстро
        "down_event_repeat_s": 3600.0,  # service_down по одному сервису не чаще раза в час
        "log": {"max_bytes": 1048576, "keep": 3},
    },
    "svc": {                            # настройки сервисов
        "exec": {"hb": "exec", "hb_stale_s": 60.0, "grace_s": 90.0},
        "scan": {"hb": "file", "hb_stale_s": 120.0, "grace_s": 60.0},
        "feed": {"hb": "none"},         # рыночный фид BTC/ETH/SOL (публичный WS) -> runtime/market.json; без него P10 жёлтый и в досье нет блока market
        "dispatch": {"hb": "file", "hb_stale_s": 30.0, "grace_s": 30.0},
        "ctl": {"hb": "file", "hb_stale_s": 30.0, "grace_s": 30.0},
        "dash": {"hb": "none", "port": 8765},
        "mcp": {"hb": "none", "module": "tb.mcp.server", "args": ["serve"], "port": 8766},
        "brain": {"hb": "file", "hb_stale_s": 300.0, "grace_s": 60.0, "module": "tb.brain.api_daemon", "args": ["run"]},
        "intel": {"hb": "file", "hb_stale_s": 180.0, "grace_s": 90.0},
        "context": {"hb": "file", "hb_stale_s": 180.0, "grace_s": 60.0},
        "sync": {"hb": "file", "hb_stale_s": 900.0, "grace_s": 90.0},
    },
    "context": {"poll_s": 15.0},        # период цикла сервиса context (публичные источники: деривативы, DVOL, индекс страха, макро-блэкаут); интервалы источников в config/context_config.json
    "sync": {                           # автосинхронизация капитала: баланс читающим ключом read -> state/portfolio.json
        "interval_min": 5.0, "venue": "bybit",
        "max_dev_frac": 0.10,           # расхождение больше: не обновлять без подтверждения (./tb sync confirm), событие reconcile_alert equity_mismatch (как equity_sync в 02_executor)
        "stale_min": 15.0,              # старше: портфель помечается «устарел»
        "hwm_confirm_reads": 2,         # высшая отметка растёт только если рост подтверждён таким числом подряд идущих чтений
    },
    "scan": {                           # пороги прежние (hotscan.DEFAULTS); сюда выносятся только те, что владелец захочет менять
        "env": "mainnet",               # публичные данные: сканеру нужен рынок, а не testnet
        "interval": 30.0, "top": 10, "min_turnover": 5e6, "max_spread_bps": 8.0, "max_lot": 15.0, "range_pct": 0.8, "surge_min": 4.0,
        "cooldown": 1200.0, "wake_score": 4.0, "max_per_hour": 12, "idea_zone_pct": 0.3, "outcome_min": "15,60",
        "publish_info": True,           # тихие (wake=false) события тоже в шину, pri=info (как раньше у моста исполнителя)
        "dedup_s": {"default": 300.0, "patrol": 240.0, "idea_near": 1800.0, "idea_level": 86400.0, "idea_invalidated": 86400.0, "idea_tp": 86400.0, "idea_expired": 86400.0},
        "hb_s": 5.0,
        "patrol": {                     # плановый обход монет дня (scansvc.Patrol): на уровнях риска >= min_level сканер сам будит entry_scout
            "enabled": True, "every_s": 300.0, "min_level": 4, "hot_n": 2,
        },
        "liq": {"enabled": True, "min_usd": 100000.0, "window_s": 300.0, "cooldown_s": 900.0, "max_loud_per_hour": 6, "n_syms": 80},      # поток ликвидаций (marketdata/liqwatch.py)
        "flow": {"enabled": True, "n_syms": 12, "xex": True},                                                                            # ордерфлоу: сделки, стакан, разрыв с Binance (marketdata/flowwatch.py)
        "structure": {                  # поток структуры рынка внутри сканера (marketdata/structure.py): runtime/structure.json для досье мозга
            "enabled": True, "env": "mainnet", "every_s": 60.0, "h1_every_s": 600.0, "max_syms": 16,
            "hot_n": 8, "hot_every_s": 180.0, "hot_h1_every_s": 1800.0, "pause_s": 2.0,   # топ-8 горячих монет сверх BTC/ETH/SOL и позиций; пауза между запросами свечей
        },
    },
    "dispatch": {
        "poll_s": 1.0,
        "retry_base_s": 2.0, "retry_max_s": 60.0, "max_attempts": 5,       # webhook: повторы доставки, затем отказ с записью в журнал
        "api": {"jobs_per_hour": 12, "critical_per_hour": 30, "coalesce_s": 60.0, "settle_s": 5.0, "settle_by_class": {"1": 0.5, "2": 1.0}, "max_seqs": 20},
        "queue_keep": 2000,
        "classes": {                    # тип события -> класс приоритета (0 высший); scanner_signal: kind idea_* -> 4, прочие -> 3
            "connectivity": 0, "kill": 0, "limit_hit": 0, "error": 0, "reconcile_alert": 0, "service_down": 0, "exec_down": 0, "exec_stopped": 0,
            "exec_started": 1, "plan_armed": 1, "plan_rejected": 1, "trigger_fired": 1, "entry_blocked": 1, "entry_filled": 1, "position_closed": 1,
            "plan_ended": 1, "action_rejected": 1, "stop_moved": 1, "rule_fired": 1, "session_ending": 1, "brain_wake_request": 1,
            "milestone": 2, "macro_blackout_soon": 1, "news_flash": 2, "scanner_signal": 3,
        },
    },
    "links": {"dash": "http://127.0.0.1:{dash_port}/"},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_cfg(root=None):
    """config/platform.json поверх DEFAULTS (битый/отсутствующий файл -> умолчания; предупреждение в cfg["_warn"])."""
    cfg = copy.deepcopy(DEFAULTS)
    p = paths.config("platform.json", root=root)
    try:
        cfg = deep_merge(cfg, localcfg.read("platform", root))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError) as e:
        cfg["_warn"] = "config/platform.json не прочитан (%s): умолчания" % type(e).__name__
    return cfg


# ---- пути ----
def pdir(root=None):
    return paths.runtime("platform", root=root)


def logs_dir(root=None):
    return paths.runtime("logs", root=root)


def log_file(name, root=None):
    return os.path.join(logs_dir(root), "%s.log" % name)


def pid_file(name, root=None):
    return os.path.join(pdir(root), "%s.pid" % name)


def hb_file(name, root=None):
    return os.path.join(pdir(root), "%s.hb.json" % name)


def status_file(root=None):
    return os.path.join(pdir(root), "status.json")


def lock_file(root=None):
    return os.path.join(pdir(root), "supervisor.lock")


def stop_req_file(root=None):
    return os.path.join(pdir(root), "stop.req")


def reconf_req_file(root=None):
    return os.path.join(pdir(root), "reconf.req")


def reconf_ack_file(root=None):
    return os.path.join(pdir(root), "reconf.ack")


def unread_file(root=None):
    return os.path.join(pdir(root), "unread.json")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, obj, indent=None)


# ---- heartbeat сервиса ----
def write_hb(name, extra=None, root=None, now=None):
    """Сервис пишет свой heartbeat (supervisor сверяет возраст)."""
    now = time.time() if now is None else now
    d = {"ts_epoch": round(now, 3), "pid": os.getpid(), "service": name}
    d.update(extra or {})
    try:
        write_json(hb_file(name, root), d)
    except OSError:
        pass


def read_hb(name, root=None, now=None):
    """-> (возраст_с | None, данные). Возраст по ts_epoch, иначе по mtime файла."""
    now = time.time() if now is None else now
    p = hb_file(name, root)
    d = read_json(p)
    if not isinstance(d, dict):
        return None, {}
    ts = d.get("ts_epoch")
    if not isinstance(ts, (int, float)):
        try:
            ts = os.stat(p).st_mtime
        except OSError:
            return None, d
    return max(0.0, now - ts), d


# ---- выбор сервисов, команды, окружение ----
class ConfigError(Exception):
    pass


def default_services(profile, cfg, root=None):
    """Сервисы по умолчанию. cfg["exec_per_exchange"]=true (config/platform.json): `exec` заменяется исполнителями всех разрешённых бирж (exec_services); по умолчанию false: один `exec`."""
    names = list((cfg.get("services") or {}).get(profile) or DEFAULTS["services"][profile])
    if cfg.get("exec_per_exchange") is True and "exec" in names:
        i = names.index("exec")
        names[i:i + 1] = exec_services(root)
    return names


def exec_venue(name):
    """Имя сервиса исполнителя -> id биржи: `exec` = основная (bybit), `exec@okx` = okx; не исполнитель -> None."""
    if name == "exec":
        return "bybit"
    if isinstance(name, str) and name.startswith("exec@") and re.match(r"^exec@[a-z][a-z0-9]{1,11}$", name):
        return name[5:]
    return None


def exec_name(venue):
    """Id биржи -> имя сервиса исполнителя (bybit сохраняет прежнее имя `exec`, файлы и проверки не меняются)."""
    return "exec" if venue in (None, "", "bybit") else "exec@%s" % venue


def exec_services(root=None):
    """Исполнители для разрешённых бирж из config/exchanges.json: ['exec'] (одна биржа, как сейчас) или ['exec', 'exec@okx', ...]. Нереализованные биржи сюда не попадают."""
    try:
        from tb.core import exchcfg
        c = exchcfg.load(root)
        ids = [x for x in c["allowed"] if c["exchanges"][x]["enabled"] and c["exchanges"][x]["implemented"]]
    except Exception:
        ids = ["bybit"]
    return [exec_name(x) for x in sorted(ids, key=lambda x: (x != "bybit", x))] or ["exec"]


def parse_services(text, cfg=None):
    """'exec,scan' -> ['exec','scan'] в порядке запуска; неизвестное имя -> ConfigError. Допустимы исполнители бирж `exec@<id>` (идут сразу за `exec`)."""
    names = [x.strip() for x in (text or "").split(",") if x.strip()]
    bad = [n for n in names if n not in SERVICE_NAMES and exec_venue(n) is None]
    if bad or not names:
        raise ConfigError("неизвестные сервисы: %s (доступны: %s)" % (",".join(bad) or "-", ",".join(SERVICE_NAMES)))
    extra = sorted(n for n in names if n.startswith("exec@"))
    out = []
    for n in ORDER:
        if n in names:
            out.append(n)
            if n == "exec":
                out += extra
    return out + ([n for n in extra if n not in out])


def stop_order(selected):
    """Остановка: сначала потребители, исполнители (все биржи) последними (пока они живы, позиции ведутся)."""
    return [n for n in reversed(ORDER) if n in selected and n != "exec"] + [n for n in selected if n.startswith("exec@")] + (["exec"] if "exec" in selected else [])


def module_available(module):
    """Есть ли модуль (без импорта): для сервисов, которые делает другой пакет работ (mcp, brain)."""
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def service_cmd(name, profile, observe, cfg, py=None):
    """-> argv дочернего процесса (или None, если сервис недоступен: нет модуля)."""
    py = py or sys.executable
    sc = (cfg.get("svc") or {}).get(name) or {}
    launcher = [py, "-m", "tb.launcher"]
    if name == "exec":
        return launcher + ["exec", "run", "--profile", profile] + (["--observe"] if observe else [])
    if exec_venue(name) is not None:           # исполнитель другой биржи: свой процесс, свои lock/health/WAL (docs/EXCHANGES.md)
        return launcher + ["exec", "run", "--profile", profile, "--exchange", exec_venue(name)] + (["--observe"] if observe else [])
    if name == "scan":
        return [py, "-m", "tb.platform.scansvc", "run"]
    if name == "feed":
        return [py, "-m", "tb.marketdata.feed"]
    if name == "dispatch":
        return launcher + ["dispatch", "run"]
    if name == "intel":
        return [py, "-m", "tb.intel.service", "run"]
    if name == "context":
        return [py, "-m", "tb.platform.contextsvc", "run"]
    if name == "sync":
        return launcher + ["sync", "run"]              # launcher загружает читающий ключ read в окружение ЭТОГО процесса (не супервизора)
    if name == "ctl":
        return [py, "-m", "tb.control.service", "run"]
    if name == "dash":
        return launcher + ["dash", "--port", str(int(sc.get("port") or 8765))]
    if name in ("mcp", "brain"):
        mod = sc.get("module") or DEFAULTS["svc"][name]["module"]
        if not module_available(mod):
            return None
        group = "mcp" if name == "mcp" else "brain"
        return launcher + [group] + list(sc.get("args") or DEFAULTS["svc"][name]["args"])
    raise ConfigError("неизвестный сервис %s" % name)


def src_dir():
    import tb
    return os.path.dirname(os.path.dirname(os.path.abspath(tb.__file__)))


def child_env(name, base, root, selected=(), profile="paper"):
    """Окружение дочернего процесса. Секреты (BYBIT_*, ANTHROPIC_*) и прокси НЕ передаются никому: ключ исполнителя грузит launcher
    внутри самого процесса (testnet/trade), прокси не нужны публичным/локальным сервисам и ломали бы доступ к бирже."""
    env = {}
    for k, v in base.items():
        ku = k.upper()
        if ku.startswith(SECRET_PREFIXES) or ku in PROXY_VARS:
            continue
        env[k] = v
    env["TB_ROOT"] = os.path.abspath(root)
    sd = src_dir()
    env["PYTHONPATH"] = sd + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TB_PLATFORM_SERVICE"] = name
    env["TB_PLATFORM_PROFILE"] = profile
    if exec_venue(name) is not None and "scan" in selected:
        env["TB_SCAN_BRIDGE"] = "0"           # события сканера в шину кладёт сервис scan, мост исполнителя выключен (иначе дубли)
    else:
        env.pop("TB_SCAN_BRIDGE", None)
    return env


def link_of(name, cfg):
    if name == "dash":
        port = int(((cfg.get("svc") or {}).get("dash") or {}).get("port") or 8765)
        return (cfg.get("links") or {}).get("dash", "http://127.0.0.1:{dash_port}/").replace("{dash_port}", str(port))
    return None
