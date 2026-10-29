"""Адаптер ctl к мозгу: проба доступности (H6), назначения моделей (W2, W4) и смена команды/модели. Владелец: WP12.

Пока WP11-C2 (`tb.brain.swap`, `config/brain.json`, `state/brain/active.json`) не влит, работают безопасные умолчания:
- проба H6: heartbeat сервиса `brain` свеж + суточный бюджет LLM не исчерпан (tb.llm.budget) + выбранный агент включён в agents.json;
- профили live-ролей берутся из `state/brain/active.json` (если есть), иначе из providers.json (метка `live_ok` в `tags` модели, `eval_ts` рядом);
- смена модели (`brain.use|rollback|reload`): контракт ниже, без WP11-C2 возвращает E_UNAVAILABLE.
Контракт для C2 (модуль `tb.brain.swap`, функции с root, now): `status(root) -> {"live_team","agent","profiles":{role:profile},"live_ok":{profile:{"ok":bool,"eval_ts":float|None}},
"last_swap_ts":float|None}`, `probe(root, now) -> {"ok":bool,"msg":str,"numbers":dict}`, `use(root, args, now) -> {"ok","code","msg","data"}`, `rollback(...)`, `reload(...)` (те же).
Ничего про ключи провайдеров здесь не читается.
"""
import importlib
import importlib.util
import json

from tb import paths
from tb.control import ctlstate
from tb.core import localcfg
from tb.platform import services as S

DEFAULT_AGENT = "api"


def _swap():
    try:
        if importlib.util.find_spec("tb.brain.swap") is not None:
            return importlib.import_module("tb.brain.swap")
    except (ImportError, ValueError, AttributeError):
        pass
    return None


def available():
    return _swap() is not None


def agent_cfg(root, agent):
    d = ctlstate.read_json(paths.config("agents.json", root=root))
    return ((d or {}).get("agents") or {}).get(agent) if isinstance(d, dict) else None


def status(root, now=None):
    sw = _swap()
    if sw is not None and hasattr(sw, "status"):
        try:
            return sw.status(root)
        except Exception:
            pass
    act = ctlstate.read_json(paths.state("brain", "active.json", root=root))
    act = act if isinstance(act, dict) else {}
    prov = localcfg.load("providers", root) or None
    live_ok = {}
    if isinstance(prov, dict):
        for pid, p in (prov.get("providers") or {}).items():
            for mid, m in ((p or {}).get("models") or {}).items():
                if isinstance(m, dict):
                    live_ok["%s/%s" % (pid, mid)] = {"ok": "live_ok" in (m.get("tags") or []), "eval_ts": m.get("eval_ts")}
    return {"live_team": act.get("live_team"), "agent": act.get("agent") or DEFAULT_AGENT, "profiles": act.get("profiles") or {}, "live_ok": live_ok,
            "last_swap_ts": act.get("last_swap_ts")}


def last_swap_ts(root):
    st = status(root)
    ts = st.get("last_swap_ts")
    if isinstance(ts, (int, float)):
        return ts
    last = None
    try:
        with open(paths.state("brain", "swaps.jsonl", root=root), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)):
                    last = r["ts"]
    except OSError:
        pass
    return last


def probe(root, now, cfg=None, agent=None):
    """H6 -> {"ok","msg","numbers"}."""
    sw = _swap()
    if sw is not None and hasattr(sw, "probe"):
        try:
            return sw.probe(root, now)
        except Exception:
            return {"ok": False, "msg": "проба мозга завершилась ошибкой", "numbers": {}}
    agent = agent or status(root).get("agent") or DEFAULT_AGENT
    ac = agent_cfg(root, agent)
    if not isinstance(ac, dict) or ac.get("enabled") is False:
        return {"ok": False, "msg": "агент %s не включён в agents.json" % agent, "numbers": {"agent": agent}}
    age, _d = S.read_hb("brain", root, now)
    scfg = S.load_cfg(root)
    stale = float(((scfg.get("svc") or {}).get("brain") or {}).get("hb_stale_s") or 300.0)
    if age is None or age > stale:
        return {"ok": False, "msg": "сервис мозга не отвечает (heartbeat %s)" % ("нет" if age is None else "%d с" % age), "numbers": {"hb_age_s": age, "limit_s": stale}}
    try:
        from tb.llm import budget as B
        prov = localcfg.load("providers", root)
        st = B.Budget(limits=prov.get("budget") or {}, root=root, clock=lambda: now, emit=False).status()
        if st.get("state") == "exhausted":
            return {"ok": False, "msg": "суточный бюджет LLM исчерпан", "numbers": {"state": "exhausted"}}
    except Exception:
        pass
    return {"ok": True, "msg": "мозг отвечает (hb %d с)" % age, "numbers": {"hb_age_s": age}}


def classify(root, args, now, action="use"):
    """Для challenge/requirements: что потребует смена (опасность, фразы, мягкие предупреждения, блокировка жёсткими условиями) или None, если tb.brain.swap нет."""
    sw = _swap()
    fn = getattr(sw, "classify", None) if sw is not None else None
    if fn is None:
        return None
    try:
        return fn(root, args, now, action=action)
    except Exception:
        return {"danger": True, "phrases": [], "warnings": [], "blocked": True, "gates": [{"id": "E_INTERNAL", "ok": False, "msg": "проверка смены не выполнена"}], "tier": 2}


def swap_action(root, name, args, now, phrase_ok=False):
    """brain.use|rollback|reload -> {"ok","code","msg","data"}; без tb.brain.swap E_UNAVAILABLE. phrase_ok=True ставит ctl, проверивший набранные фразы."""
    sw = _swap()
    fn = getattr(sw, name, None) if sw is not None else None
    if fn is None:
        return {"ok": False, "code": "E_UNAVAILABLE", "msg": "смена модели недоступна (tb.brain.swap)", "data": {}}
    try:
        r = fn(root, args, now, by="ctl", phrase_ok=phrase_ok)
        return r if isinstance(r, dict) and "ok" in r else {"ok": False, "code": "E_INTERNAL", "msg": "", "data": {}}
    except Exception:
        return {"ok": False, "code": "E_INTERNAL", "msg": "", "data": {}}
