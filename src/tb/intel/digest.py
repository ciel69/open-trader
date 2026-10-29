"""Дайджест карантинного читателя новостей: файл `runtime/intel/digest.json`, перечисления и безопасные проекции (WP13b, 07 п. 3.4). Владелец: WP13.

Лист модульного графа: не импортирует tb.llm и tb.intel.items. Его читают reader (пишет), dashapi (показ владельцу) и, ЧЕРЕЗ ФАЙЛ, brain/brief.py (по явному белому списку полей).
Документ (tb.digest/1): ТОЛЬКО числа, bool, enum и идентификаторы; запись проходит `store.check_facts` (любая строка = идентификатор без пробелов, <= 40 символов),
поэтому заголовок новости в файл попасть не может физически.
 {"schema":"tb.digest/1","ts":..,"status":"ok|idle|off|limit|paused|llm_down","stale":false,"degraded":["reader"],
  "reader":{"profile":"deepseek.flash","calls_h":3,"usd_h":0.0004,"usd_day":0.002,"max_calls_h":12,"max_usd_day":0.05,"fail_streak":0,"pause_until":null,
            "last_ok":..,"err":null,"pending":4,"ok_n":120,"fail_n":2,"susp_n":3},
  "items":{"N-3fa9c2d1":{"st":"ok|fail|susp","type":"hack","imp":4,"ir":5,"dir":"down","syms":["ETH"],"ttl_min":360,"conf":0.7,"its":..,"rt":..,"src":"coindesk"}}}
`imp` = эффективная важность после детерминированной политики (уровень 3), `ir` = то, что сказал читатель.
"""
import copy
import json

from tb import paths
from tb.core import filelock
from tb.intel import store

SCHEMA = "tb.digest/1"
# типы события читателя (07 п. 3.4) + upgrade/outage; `suspicious` ставит только код (inj_suspect), модель его выдать не может
TYPES = ("listing", "delisting", "maintenance", "hack", "regulation", "macro", "etf", "unlock", "partnership", "price_move", "rumor", "upgrade", "outage", "other")
SUSPICIOUS = "suspicious"
ALL_TYPES = TYPES + (SUSPICIOUS,)
DIRS = ("up", "down", "neutral", "unknown")
TTL_MIN = (30, 120, 360, 1440, 2880)             # срок актуальности: 0.5 ч, 2 ч, 6 ч, 24 ч, 48 ч
STATES = ("ok", "fail", "susp")                    # ok = разметка читателя; fail = заглушка после провала формата; susp = подозрение на инъекцию (текст в LLM не уходил)
STATUSES = ("ok", "idle", "off", "limit", "paused", "llm_down")
MAX_ENTRIES = 400


def digest_path(root=None):
    return paths.runtime("intel", "digest.json", root=root)


def read_digest(root=None):
    try:
        with open(digest_path(root), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("schema") == SCHEMA else None


def write_digest(root, doc):
    """Атомарная запись; документ с произвольной строкой не записывается (ValueError)."""
    bad = store.check_facts(doc)
    if bad:
        raise ValueError("digest: нарушения формата: %s" % ",".join(bad[:3]))
    filelock.atomic_write_json(digest_path(root), doc, indent=None)


def signature(doc):
    return json.dumps({k: v for k, v in doc.items() if k != "ts"}, sort_keys=True)


def stub(its, rt, src, st="fail", imp=1):
    """Заглушка (07 п. 3.4): ни на что не влияет."""
    return {"st": st, "type": SUSPICIOUS if st == "susp" else "other", "imp": imp, "ir": imp, "dir": "unknown", "syms": [], "ttl_min": 120, "conf": 0,
            "its": int(its), "rt": int(rt), "src": src}


def events(doc, now, min_imp=2, limit=6, with_age=True):
    """Живые размеченные события из дайджеста: st=ok, imp >= min_imp, возраст <= ttl. -> список словарей с ref, отсортированный по важности."""
    out = []
    for ref, e in ((doc or {}).get("items") or {}).items():
        if not isinstance(e, dict) or e.get("st") != "ok" or not isinstance(e.get("imp"), int) or e["imp"] < min_imp:
            continue
        its, ttl = e.get("its"), e.get("ttl_min")
        if not isinstance(its, (int, float)) or not isinstance(ttl, int):
            continue
        age = (now - its) / 60.0
        if age < -5 or age > ttl:
            continue
        r = dict(e)
        r["ref"] = ref
        if with_age:
            r["age_min"] = max(0, int(age))
        out.append(r)
    out.sort(key=lambda r: (-r["imp"], r.get("age_min", 0)))
    return out[:limit]


READER_DEFAULTS = {
    "enabled": False,                      # включает владелец после проверки ключа (`./tb llm check --live`)
    "provider": "deepseek", "model": "flash",
    "batch": 10, "max_tokens": 600, "retries": 1, "title_len": 160, "snip_len": 160,
    "max_age_h": 6.0, "cache_h": 24.0,     # не читаем элементы старше max_age_h; результат помним cache_h
    "min_gap_s": 15.0, "max_wait_s": 120.0,
    "max_calls_hour": 12, "max_usd_hour": 0.02, "max_usd_day": 0.05, "job_usd": 0.01,
    "pause_fail_streak": 3, "pause_s": 1800.0, "err_backoff_s": [60, 120, 300, 600, 900],
    "stale_s": 900.0, "write_s": 60.0, "max_entries": 300,
    "skip_srcs": ["bybit_ann"],            # объявления биржи уже разобраны структурно в facts.json
    "official_srcs": ["bybit_ann"], "symbols": [],
}


def reader_cfg(cfg):
    """Секция `reader` config/intel.json поверх READER_DEFAULTS (неизвестные ключи игнорируются)."""
    r = copy.deepcopy(READER_DEFAULTS)
    r.update({k: v for k, v in ((cfg or {}).get("reader") or {}).items() if k in READER_DEFAULTS})
    return r
