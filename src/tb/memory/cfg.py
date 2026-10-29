"""Загрузка config/memory.json (шкалы, веса, пороги). Нет файла в корне -> берётся файл проекта по умолчанию; битый файл -> ValueError."""
import hashlib
import json
import os

from tb import paths


def load(root=None):
    for r in (root, None) if root else (None,):
        p = paths.config("memory.json", root=r)
        if os.path.isfile(p):
            break
    else:
        p = paths.config("memory.json", root=paths.DEFAULT_ROOT)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        with open(paths.config("memory.json", root=paths.DEFAULT_ROOT), encoding="utf-8") as f:
            d = json.load(f)
    except ValueError as e:
        raise ValueError("config/memory.json: %s" % e)
    if not isinstance(d, dict) or d.get("schema") != "tb.memory_cfg/1":
        raise ValueError("config/memory.json: ожидается schema tb.memory_cfg/1")
    return d


def cfg_hash(cfg):
    """Хеш настроек (для предрегистрации A/B: параметры фиксируются до старта)."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
