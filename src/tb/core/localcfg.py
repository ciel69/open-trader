"""Слой ЛОКАЛЬНЫХ переопределений конфигов (личные настройки владельца отдельно от публикуемых умолчаний).

Если существует `config/local/<имя>.json` (каталог в .gitignore, файлы создаёт владелец), загрузчики видят глубокое слияние поверх `config/<имя>.json`:
  - словари сливаются рекурсивно;
  - списки словарей с полем `id` (например `subscribers`) сливаются по id: запись с тем же id дополняет/меняет поставляемую, новый id добавляется в конец;
  - все остальные значения (числа, строки, обычные списки) заменяются целиком.
Битый локальный файл игнорируется (остаются поставляемые умолчания) с предупреждением в stderr. Файлы редактирует владелец; для настроек из дашборда и CLI
(провайдеры, профили, биржи) есть `update_local(name, fn)`: чтение-изменение-запись под блокировкой, атомарно, права обычного файла (секретов в этих файлах нет).
Для тестов: переменная TB_CONFIG_LOCAL подменяет каталог локальных файлов, но только когда корень не задан (ни аргументом, ни TB_ROOT) либо равен папке проекта.
"""
import copy
import json
import os
import re
import sys

from tb import paths

RE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WARNED = set()


def local_dir(root=None):
    env = os.environ.get("TB_CONFIG_LOCAL")
    if env and not os.environ.get("TB_ROOT") and (root is None or os.path.abspath(root) == os.path.abspath(paths.DEFAULT_ROOT)):
        return os.path.abspath(env)
    return paths.config("local", root=root)


def local_path(name, root=None):
    """Путь к локальному файлу или None, если имя пытается выйти за каталог config/local."""
    name = str(name or "")
    if name.endswith(".json"):
        name = name[:-5]
    if not RE_NAME.match(name) or ".." in name:
        return None
    base = os.path.realpath(local_dir(root))
    p = os.path.realpath(os.path.join(base, name + ".json"))
    if os.path.dirname(p) != base:
        return None
    return p


def _warn(key, msg):
    if key not in _WARNED:
        _WARNED.add(key)
        try:
            sys.stderr.write("[localcfg] %s\n" % msg)
        except (OSError, ValueError):
            pass


def _by_id(lst):
    return isinstance(lst, list) and bool(lst) and all(isinstance(x, dict) and "id" in x for x in lst)


def merge(base, over):
    """Глубокое слияние over поверх base (без изменения аргументов)."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    if _by_id(base) and _by_id(over):
        out = copy.deepcopy(base)
        idx = {x["id"]: i for i, x in enumerate(out)}
        for it in over:
            if it["id"] in idx:
                out[idx[it["id"]]] = merge(out[idx[it["id"]]], it)
            else:
                idx[it["id"]] = len(out)
                out.append(copy.deepcopy(it))
        return out
    return copy.deepcopy(over)


def read_local(name, root=None):
    """-> словарь локального файла или None (нет файла / битый / не объект; битый даёт предупреждение)."""
    p = local_path(name, root)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _warn(p, "config/local/%s.json не прочитан (%s): используются поставляемые умолчания" % (name, type(e).__name__))
        return None
    if not isinstance(d, dict):
        _warn(p, "config/local/%s.json: нужен JSON-объект, файл проигнорирован" % name)
        return None
    return d


def read(name, root=None):
    """Как json.load(open(config/<имя>.json)) + локальный слой. Ошибки ОСНОВНОГО файла (FileNotFoundError, OSError, ValueError) пробрасываются как раньше."""
    if name.endswith(".json"):
        name = name[:-5]
    with open(paths.config(name + ".json", root=root), encoding="utf-8") as f:
        base = json.load(f)
    over = read_local(name, root)
    if over is not None and isinstance(base, dict):
        return merge(base, over)
    return base


def load(name, root=None):
    """-> слитый словарь; нет/битый основной файл -> локальный слой сам по себе или {} (без исключений)."""
    try:
        return read(name, root)
    except (OSError, ValueError):
        return read_local(name, root) or {}


def update_local(name, fn, root=None):
    """Атомарно меняет config/local/<имя>.json: fn(dict) -> dict (новое содержимое) | None (без записи). Битый файл не перезаписывается (ValueError).
    -> записанный словарь или None. Блокировка `config/local/.<имя>.lock`."""
    from tb.core.filelock import FileLock, atomic_write_json
    p = local_path(name, root)
    if not p:
        raise ValueError("недопустимое имя локального файла")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with FileLock(os.path.join(os.path.dirname(p), ".%s.lock" % os.path.basename(p)[:-5]), wait=10.0):
        cur = {}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cur = json.load(f)
            except (OSError, ValueError):
                raise ValueError("config/local/%s.json повреждён: исправьте вручную" % name)
            if not isinstance(cur, dict):
                raise ValueError("config/local/%s.json: нужен JSON-объект" % name)
        new = fn(copy.deepcopy(cur))
        if new is None:
            return None
        atomic_write_json(p, new, indent=1)
        return new
