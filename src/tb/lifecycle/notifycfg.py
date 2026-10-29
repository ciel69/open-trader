"""Настройки уведомлений ОС, управляемые владельцем без правки конфига: `state/control/notify.json`. Уведомления (WP-N).

Приоритет: файл `state/control/notify.json` (создаёт и правит только ctl действием `notify.set` и CLI `./tb notify`) > `config/lifecycle.json: notify.os`
(по умолчанию выключено). Если файла нет, работает прежний путь WP14 (итог дня напрямую, только при notify.os=true), а подписчик шины молчит.
Подпись файла не нужна, запись атомарная. Всё выключено по умолчанию: мастер-выключатель и каждая категория.

Формат файла:
  {"version":1, "master":false,
   "categories":{"day_start":false, ... (все ключи protocol.NOTIFY_KEYS)},
   "quiet":{"enabled":false,"from":"23:00","to":"07:00","tz":"local","bypass":["emergency","service_down","day_close_failed"]},
   "limits":{"per_hour":6,"same_kind_min":10},
   "updated":<ts>, "by":"cli|dash"}
Битый файл не включает ничего (мастер выключен), в ответе `warn`.
"""
import copy
import os
import time

from tb import paths
from tb.control import ctlstate, protocol
from tb.lifecycle import cfg as cfgmod

CATEGORIES = protocol.NOTIFY_CATEGORIES
KEYS = protocol.NOTIFY_KEYS
LABEL = dict(CATEGORIES)
CRITICAL = protocol.NOTIFY_CRITICAL
DEFAULT_LIMITS = {"per_hour": 6, "same_kind_min": 10}


def defaults():
    return {"version": 1, "master": False, "categories": dict((k, False) for k in KEYS),
            "quiet": {"enabled": False, "from": "23:00", "to": "07:00", "tz": "local", "bypass": list(CRITICAL)},
            "limits": dict(DEFAULT_LIMITS), "updated": None, "by": None}


def file_path(root=None):
    return paths.control_dir("notify.json", root=root)


def state_path(root=None):
    return paths.control_dir("notify_state.json", root=root)


def _int(v, lo, hi, dflt):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else dflt


def engine_on(root=None):
    """Есть файл настроек владельца: уведомления рассылает подписчик шины, прямой путь WP14 молчит."""
    return os.path.exists(file_path(root))


def normalize(raw):
    """Любой словарь -> полный корректный (мусор заменяется умолчанием)."""
    s = defaults()
    if not isinstance(raw, dict):
        return s
    s["master"] = raw.get("master") is True
    c = raw.get("categories")
    if isinstance(c, dict):
        for k in KEYS:
            s["categories"][k] = c.get(k) is True
    q = raw.get("quiet")
    if isinstance(q, dict):
        s["quiet"]["enabled"] = q.get("enabled") is True
        for k in ("from", "to"):
            if isinstance(q.get(k), str) and protocol.RE_HHMM.match(q[k]):
                s["quiet"][k] = q[k]
        if q.get("tz") in ("local", "utc"):
            s["quiet"]["tz"] = q["tz"]
        if isinstance(q.get("bypass"), list):
            s["quiet"]["bypass"] = sorted(set(x for x in q["bypass"] if x in KEYS))
    lim = raw.get("limits")
    if isinstance(lim, dict):
        s["limits"]["per_hour"] = _int(lim.get("per_hour"), 1, 60, DEFAULT_LIMITS["per_hour"])
        s["limits"]["same_kind_min"] = _int(lim.get("same_kind_min"), 0, 1440, DEFAULT_LIMITS["same_kind_min"])
    if isinstance(raw.get("updated"), (int, float)) and not isinstance(raw.get("updated"), bool):
        s["updated"] = raw["updated"]
    if isinstance(raw.get("by"), str):
        s["by"] = raw["by"][:12]
    return s


def load(root=None, lc=None):
    """-> {"source":"file"|"config","engine":bool,"settings":{...},"warn":str|None}.
    engine=True: рассылает подписчик шины (файл есть). engine=False: действует конфиг (старый путь WP14: итог дня при notify.os=true)."""
    p = file_path(root)
    if os.path.exists(p):
        raw = ctlstate.read_json(p)
        if raw is None:
            s = defaults()
            return {"source": "file", "engine": True, "settings": s, "warn": "state/control/notify.json не прочитан: всё выключено"}
        return {"source": "file", "engine": True, "settings": normalize(raw), "warn": None}
    lc = lc if lc is not None else cfgmod.load(root)
    s = defaults()
    s["master"] = bool(((lc or {}).get("notify") or {}).get("os"))
    return {"source": "config", "engine": False, "settings": s, "warn": None}


def apply(patch, base):
    """Применяет проверенный protocol.validate_notify патч к настройкам -> (новые настройки, список изменений по-русски)."""
    s = copy.deepcopy(base)
    ch = []
    if "master" in patch and s["master"] != patch["master"]:
        s["master"] = patch["master"]
        ch.append("мастер %s" % ("вкл" if patch["master"] else "выкл"))
    for k, v in (patch.get("categories") or {}).items():
        if s["categories"].get(k) != v:
            s["categories"][k] = v
            ch.append("%s %s" % (k, "вкл" if v else "выкл"))
    for k, v in (patch.get("quiet") or {}).items():
        if s["quiet"].get(k) != v:
            s["quiet"][k] = v
            ch.append("тихие часы %s=%s" % (k, ",".join(v) if isinstance(v, list) else v))
    for k, v in (patch.get("limits") or {}).items():
        if s["limits"].get(k) != v:
            s["limits"][k] = v
            ch.append("лимит %s=%s" % (k, v))
    return normalize(s), ch


def save(root, settings, now, by):
    s = normalize(settings)
    s["updated"], s["by"] = round(now, 1), by
    ctlstate.write_json(file_path(root), s)
    return s


def set_patch(root, patch, now, by):
    """Проверяет, применяет и сохраняет патч -> (ok, msg, settings, изменения). Файл создаётся при первом изменении (поверх конфиг-умолчаний)."""
    ok, msg, p = protocol.validate_notify(patch if isinstance(patch, dict) else {})
    if not ok:
        return False, msg, None, []
    cur = load(root)
    s, ch = apply(p, cur["settings"])
    saved = save(root, s, now, by)
    return True, "", saved, ch


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet(settings, now):
    """Сейчас тихие часы? Диапазон может переходить через полночь (23:00-07:00); from == to -> пусто (тишины нет)."""
    q = settings.get("quiet") or {}
    if not q.get("enabled"):
        return False
    tm = time.gmtime(now) if q.get("tz") == "utc" else time.localtime(now)
    m = tm.tm_hour * 60 + tm.tm_min
    a, b = minutes(q["from"]), minutes(q["to"])
    if a == b:
        return False
    return a <= m < b if a < b else (m >= a or m < b)


def bypasses(settings, cat):
    return cat in ((settings.get("quiet") or {}).get("bypass") or [])


def view(root=None, now=None, lc=None):
    """Для дашборда и CLI: настройки, источник, подписи категорий, тихие часы сейчас, хвост очереди и счётчики."""
    now = time.time() if now is None else now
    cur = load(root, lc)
    st = ctlstate.read_json(state_path(root)) or {}
    s = cur["settings"]
    return {"source": cur["source"], "engine": cur["engine"], "warn": cur["warn"], "settings": s,
            "categories": [{"id": k, "label": l, "on": bool(s["categories"].get(k)), "critical": k in CRITICAL, "bypass": bypasses(s, k)} for k, l in CATEGORIES],
            "quiet_now": in_quiet(s, now), "held_n": len(st.get("held") or []), "sent_last": st.get("last_sent"), "stats": st.get("stats") or {},
            "hint": "Если уведомление не показывается: проверьте системные настройки уведомлений (macOS: «Редактор скриптов»; Windows: PowerShell и режим «Не беспокоить»)."}
