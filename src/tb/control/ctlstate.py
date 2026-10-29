"""Файлы состояния сервиса ctl: настройки, состояние платформы/дня (заглушка WP14), учёт запусков live, журнал виденных команд. Владелец: WP12."""
import copy
import json
import os
import time

from tb import paths
from tb.control import protocol
from tb.core.filelock import atomic_write_json


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, obj, indent=None)


def load_cfg(root=None):
    """config/control.json поверх protocol.DEFAULTS; битый файл -> умолчания (+ cfg["_warn"])."""
    cfg = copy.deepcopy(protocol.DEFAULTS)
    d = read_json(paths.control_config_file(root))
    if isinstance(d, dict):
        for k, v in d.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    elif os.path.exists(paths.control_config_file(root)):
        cfg["_warn"] = "config/control.json не прочитан: умолчания"
    return cfg


def f_platform(root=None):
    return paths.control_dir("platform.json", root=root)


def f_live(root=None):
    return paths.control_dir("live_starts.json", root=root)


def f_seen(root=None):
    return paths.control_dir("seen.json", root=root)


def f_trading(root=None):
    return paths.control_dir("trading.json", root=root)


def f_risk(root=None):
    return paths.control_dir("risk_level.json", root=root)


def platform_state(root=None):
    d = read_json(f_platform(root))
    d = d if isinstance(d, dict) else {}
    st = {"state": "stopped", "since": None, "mode": "observe", "session": None, "day_id": None, "team": None, "agent": None, "trading": False,
          "live_waivers": [], "step": None}
    st.update(d)
    return st


def save_platform_state(st, root=None):
    write_json(f_platform(root), st)


def utc_day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def live_starts(root=None, now=None):
    """-> {"starts":[ts...] (за сутки UTC), "last_stop_ts": float|None}."""
    now = time.time() if now is None else now
    d = read_json(f_live(root))
    d = d if isinstance(d, dict) else {}
    day = utc_day(now)
    starts = [t for t in (d.get("starts") or []) if isinstance(t, (int, float)) and utc_day(t) == day]
    return {"starts": starts, "last_stop_ts": d.get("last_stop_ts") if isinstance(d.get("last_stop_ts"), (int, float)) else None}


def note_live_start(root, now):
    d = live_starts(root, now)
    d["starts"].append(now)
    write_json(f_live(root), d)


def note_live_stop(root, now):
    d = live_starts(root, now)
    d["last_stop_ts"] = now
    write_json(f_live(root), d)


class Seen(object):
    """Последние id команд и использованные nonce (идемпотентность при повторной доставке)."""

    def __init__(self, root, keep=500):
        self.root, self.keep = root, keep
        d = read_json(f_seen(root))
        d = d if isinstance(d, dict) else {}
        self.ids = [x for x in (d.get("ids") or []) if isinstance(x, str)][-keep:]
        self.nonces = [x for x in (d.get("nonces") or []) if isinstance(x, str)][-keep:]

    def save(self):
        write_json(f_seen(self.root), {"ids": self.ids[-self.keep:], "nonces": self.nonces[-self.keep:]})

    def has_id(self, cid):
        return cid in self.ids

    def add_id(self, cid):
        self.ids.append(cid)
        self.save()

    def is_used(self, nonce):
        return nonce in self.nonces

    def use_nonce(self, nonce):
        """-> False, если nonce уже использован."""
        if nonce in self.nonces:
            return False
        self.nonces.append(nonce)
        self.save()
        return True
