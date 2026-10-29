"""Общий формат вывода и ошибок CLI мозга (03 п. 3.1). Владелец: WP6.

Вывод по умолчанию: JSON одной строкой; код выхода 0 при ok:true. Ответ команд plan: {"ok","cmd_id","status","plan_id","rev","warnings","guard_preview","errors"}.
Модуль не импортирует биржевой код и исполнитель (слои, tests/test_layering.py).
"""
import json
import math
import re
import sys
import time
import unicodedata

from tb import paths

EXIT_OK = 0
EXIT_FAIL = 1           # ok:false (отказ валидатора/guard)
EXIT_USAGE = 2          # ошибка использования / не реализовано
EXIT_QUEUED = 3         # ok:null, status=queued: команда принята в очередь, ответа исполнителя нет (повторно не слать)
STATUS_QUEUED = "queued"   # таймаут --wait: команда осталась в очереди

EXEC_ALIVE_S = 20.0     # heartbeat исполнителя старше этого = исполнитель не жив (03 п. 3.3)
RE_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
RE_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
RE_PLAN_ID = re.compile(r"^PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3}$")
RE_IDEA_ID = re.compile(r"^I\d+$")
MAX_REASON_LEN = 200


def setup_stdout():
    """UTF-8 в stdout/stderr (Windows-консоль по умолчанию не умеет кириллицу в каналах)."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def dumps(obj):
    """Компактный JSON одной строкой (кириллица как есть)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def out(obj, stream=None):
    (stream or sys.stdout).write(dumps(obj) + "\n")
    (stream or sys.stdout).flush()


def fail(msg, code="E_USAGE", status="error", stream=None):
    """Ошибка использования: ok:false в stdout (машинно читаемо) и код 2."""
    out({"ok": False, "status": status, "errors": [{"code": code, "path": "", "msg": msg}]}, stream)
    return EXIT_USAGE


def clean_text(s, limit):
    """Свой текст мозга/владельца: управляющие и невидимые символы убираются, пробелы схлопываются, длина обрезается."""
    if not isinstance(s, str):
        return ""
    buf = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in "\r\n\t":
            buf.append(" ")
        elif cat[0] == "C":          # Cc, Cf (нулевой ширины, bidi), Cs, Co, Cn
            continue
        else:
            buf.append(ch)
    return re.sub(r"\s+", " ", "".join(buf)).strip()[:limit]


def num(x, default=None):
    """Число или default (строки, None, bool, nan и inf не пропускаются)."""
    if isinstance(x, bool):
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def parse_ts(s):
    """'2026-10-27T07:26:57Z' -> epoch (UTC) или None; число возвращается как есть."""
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return num(s)
    if not isinstance(s, str):
        return None
    try:
        import calendar
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def read_json(path):
    """-> dict/list или None (нет файла, битый JSON, идёт запись)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ident(v, default=None):
    """Строка, подходящая как идентификатор (белый список для вывода), иначе default."""
    return v if isinstance(v, str) and RE_IDENT.match(v) else default


def exec_health(root=None, now=None):
    """Состояние исполнителя по runtime/exec_health.json: {"present","alive","age_s","data"}. Возраст: ts_epoch, затем ts, затем mtime файла."""
    import os
    now = time.time() if now is None else now
    path = paths.exec_health_file(root=root)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"present": False, "alive": False, "age_s": None, "data": {}}
    d = read_json(path)
    d = d if isinstance(d, dict) else {}
    ts = num(d.get("ts_epoch")) or parse_ts(d.get("ts")) or mtime
    age = max(0.0, now - ts)
    return {"present": True, "alive": age <= EXEC_ALIVE_S and d.get("alive", True) is not False, "age_s": round(age, 1), "data": d}


# ------------------------------------------------------------------ чтение файлов планов исполнителя (без блокировок, устойчиво к частичным папкам)
def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


read_jsonl = _read_jsonl


def load_plan(plan_id, root=None):
    """-> (spec, rt) плана из state/plans/PL-*/ или (None, None); частичная папка даёт то, что есть."""
    import os
    if not isinstance(plan_id, str) or not RE_PLAN_ID.match(plan_id):
        return None, None
    d = paths.plans(plan_id, root=root)
    if not os.path.isdir(d):
        return None, None
    spec, rt = read_json(os.path.join(d, "spec.json")), read_json(os.path.join(d, "runtime.json"))
    return (spec if isinstance(spec, dict) else None), (rt if isinstance(rt, dict) else None)


def load_plans(root=None):
    """-> ([{"id","spec","rt"}], bad): все PL-* из state/plans (папки без читаемого spec пропускаются и считаются в bad)."""
    import os
    base = paths.plans(root=root)
    try:
        names = sorted(n for n in os.listdir(base) if n.startswith("PL-") and os.path.isdir(os.path.join(base, n)))
    except OSError:
        return [], 0
    out, bad = [], 0
    for n in names:
        spec, rt = load_plan(n, root=root)
        if spec is None:
            bad += 1
            continue
        out.append({"id": n, "spec": spec, "rt": rt or {}})
    return out, bad


def plan_status(item):
    """Статус плана: runtime.status, иначе spec.status, иначе draft."""
    return (item.get("rt") or {}).get("status") or (item.get("spec") or {}).get("status") or "draft"


def clean_struct(obj, depth=0):
    """Рекурсивный белый список для структуры плана: числа, bool, идентификаторы; ключи только идентификаторы; thesis отдельно."""
    if depth > 6:
        return None
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, float)):
        return num(obj)
    if isinstance(obj, str):
        return obj if RE_IDENT.match(obj) else None
    if isinstance(obj, list):
        return [x for x in (clean_struct(v, depth + 1) for v in obj[:20]) if x is not None]
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:40]:
            if not ident(k):
                continue
            cv = clean_struct(v, depth + 1)
            if cv is not None:
                out[k] = cv
        return out
    return None
