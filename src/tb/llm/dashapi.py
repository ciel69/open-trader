"""Чтение для раздела «Настройки» дашборда (EXT_API): `/api/settings/*`. Только чтение; запись идёт через ctl-действия (docs/spec_v2/SETTINGS_API.md).

  /api/settings/overview    сводка: активная биржа, режим, ревизия назначений, число провайдеров/профилей/ролей, расход за сутки
  /api/settings/providers   провайдеры и их модели (ключ: есть/нет и маска, значение никогда)
  /api/settings/presets     шаблоны провайдеров (verified:false)
  /api/settings/profiles    профили (провайдер, модель, параметры, live_ok, где используются)
  /api/settings/roles       роли -> основной/запасные/параметры/бюджет/расход за сутки
  /api/settings/exchanges   биржи: реализовано/включено/режим/лимиты, активная и разрешённые, исполнители
  /api/settings/usage       расход по моделям (?days=N&by=model|provider|profile|role)
  /api/settings/revisions   журнал смен назначений и доступные ревизии для отката
Ответы проходят dash.model.scrub, который вырезает поля со словами key/token/secret: поэтому имена переименованы (`_RENAME`; прочее: token->tok, key->cred; список в SETTINGS_API.md).
Значения ключей в этом модуле не читаются вообще (только факт наличия и маска через keystore.status).
"""
import os
import time

from tb import paths

_RENAME = {"max_tokens": "max_out_tok", "day_tokens": "day_toks", "hour_tokens": "hour_toks", "job_tokens": "job_toks", "tokens": "toks", "key": "cred", "key_file": "cred_file",
           "key_env": "cred_env", "keys": "cred_files", "perms_ok": "perms_ok"}
PREFIX = "/api/settings/"


def _rn(k):
    if k in _RENAME:
        return _RENAME[k]
    return k.replace("token", "tok").replace("key", "cred").replace("secret", "sec") if isinstance(k, str) else k


def _safe(o):
    if isinstance(o, dict):
        return {_rn(k): _safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_safe(v) for v in o]
    return o


def _providers(root, now, q):
    from tb.llm import provstore
    r = provstore.listing(root)
    rows = []
    for p in r["providers"]:
        p = dict(p)
        p["models"] = [dict(v, id=k) for k, v in sorted(p["models"].items())]
        rows.append(p)
    return {"providers": rows, "errors": r["errors"], "budget": r["budget"]}


def _presets(root, now, q):
    from tb.llm import provstore
    return {"presets": provstore.preset_rows(root)}


def _profiles(root, now, q):
    from tb.brain import profiles
    return profiles.profile_rows(root)


def _roles(root, now, q):
    from tb.brain import profiles
    return profiles.role_rows(root, now)


def _exchanges(root, now, q):
    from tb.core import exchcfg
    c = exchcfg.load(root)
    rows = []
    for eid, e in sorted(c["exchanges"].items()):
        rows.append(dict(e, active=(eid == c["active"]), allowed=(eid in c["allowed"]), exec=_exec_state(root, eid, now)))
    plat = _platform_state(root)
    return {"exchanges": rows, "active": c["active"], "allowed": c["allowed"], "errors": c["errors"],
            "platform": {"state": plat.get("state"), "mode": plat.get("mode")}, "editable": plat.get("state") == "stopped"}


def _platform_state(root):
    """state/control/platform.json (пишет ctl): только чтение, без импорта плоскости управления."""
    import json
    try:
        with open(paths.control_dir("platform.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _exec_state(root, eid, now):
    """Исполнитель биржи: жив ли heartbeat его файла здоровья (для не основной биржи имя файла со суффиксом)."""
    try:
        p = paths.exec_health_file(root=root, venue=eid)
        age = max(0.0, now - os.path.getmtime(p))
        return {"present": True, "age_s": round(age, 1), "alive": age < 60.0}
    except (OSError, TypeError):
        return {"present": False, "age_s": None, "alive": False}


def _usage(root, now, q):
    from tb.llm import budget as B, providers, usage as U
    try:
        days = max(1, min(90, int((q.get("days") or ["7"])[0])))
    except ValueError:
        days = 7
    by = (q.get("by") or ["model"])[0]
    by = by if by in ("model", "provider", "profile", "role", "team") else "model"
    rows = U.read_rows(root, since=now - days * 86400.0)
    today = U.read_rows(root, since=B.window_start(now, 86400))
    out = {"days": days, "by": by, "total": U.window_totals(rows), "today": U.window_totals(today),
           "groups": [dict(v, id=k) for k, v in sorted(U.aggregate(rows, by).items())]}
    try:
        reg = providers.load(root)
        bud = B.from_registry(reg, root=root)
        bud.emit = False
        out["budget"] = bud.status(None)
        out["providers"] = [{"id": pid, "state": bud.status(pid)["state"]} for pid in reg.providers]
    except Exception:
        out["budget"] = None
        out["providers"] = []
    return out


def _ts(s):
    import calendar
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def models_perf(root, now, days, mode):
    """Результат по моделям: сделки с тегом model.<профиль> (expstats, без исключённых сбоем платформы) + учёт вызовов LLM (usage) за период.
    Эффективность: чистый результат (PnL минус расход на ИИ), PnL на 100 вызовов, на $ и на 1M токенов. n < MIN_N помечается: выводы по такой выборке ненадёжны."""
    from tb.core import expstats
    from tb.llm import usage as U
    since = now - days * 86400.0
    calls = U.aggregate(U.read_rows(root, since=since), "profile")
    by = {}
    for t in expstats.load_trades(mode, root=root, with_rules=False):
        ts = _ts(t.get("ts_close"))
        if ts is None or ts < since or t.get("shadow"):
            continue
        by.setdefault(t.get("model") or "—", []).append(t)
    rows = []
    for mid in sorted(set(calls) | set(by)):
        c, ts = calls.get(mid) or {}, by.get(mid, [])
        n = len(ts)
        pnl = sum(t["pnl"] or 0.0 for t in ts)
        rs = [t["r"] for t in ts if t.get("r") is not None]
        n_calls = c.get("calls", 0)
        toks = (c.get("in", 0) or 0) + (c.get("out", 0) or 0)
        usd = c.get("usd", 0.0) or 0.0
        rows.append({"id": mid, "calls": n_calls, "toks": toks, "usd": round(usd, 4), "err_rate": c.get("err_rate"), "fmt_err_rate": c.get("fmt_err_rate"),
                     "lat_p50_ms": c.get("lat_p50_ms"), "n": n, "wins": sum(1 for t in ts if (t["pnl"] or 0.0) > 0), "pnl": round(pnl, 4),
                     "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "net": round(pnl - usd, 4),
                     "pnl_per_100_calls": round(pnl / n_calls * 100.0, 3) if n_calls and n else None,
                     "pnl_per_usd": round(pnl / usd, 2) if usd > 0 and n else None,
                     "pnl_per_mtok": round(pnl / toks * 1e6, 2) if toks and n else None,
                     "calls_per_trade": round(n_calls / float(n), 1) if n else None, "reliable": n >= expstats.MIN_N})
    rows.sort(key=lambda r: (-(r["net"] if r["n"] else -1e9), r["id"]))
    return {"days": days, "mode": mode, "min_n": expstats.MIN_N, "rows": rows,
            "note": "Результат сделки приписан модели, подавшей план (тег model.*); учёт вызовов идёт по профилю. Выборка мала: до n=%d это наблюдение, а не вывод." % expstats.MIN_N}


def _models_perf(root, now, q):
    try:
        days = max(1, min(90, int((q.get("days") or ["7"])[0])))
    except ValueError:
        days = 7
    mode = (q.get("mode") or ["paper"])[0]
    return models_perf(root, now, days, mode if mode in ("live", "paper", "testnet", "all") else "paper")


def _revisions(root, now, q):
    from tb.brain import swap
    names = []
    try:
        names = sorted((int(n[2:-5]) for n in os.listdir(swap.history_dir(root)) if n.startswith("R-") and n.endswith(".json") and n[2:-5].isdigit()), reverse=True)
    except OSError:
        pass
    st = swap.status(root, now)
    rows = [{k: r.get(k) for k in ("seq", "ts", "rev", "prev_rev", "kind", "tier", "target", "from", "to", "by")} for r in swap.read_swaps(root, 20)]
    return {"rev": st["rev"], "source": st["source"], "warn": st["warn"], "revisions": names[:50], "swaps": rows, "chain_ok": swap.verify_swaps(root)["ok"]}


def _overview(root, now, q):
    from tb.brain import profiles, swap
    from tb.core import exchcfg
    from tb.llm import budget as B, provstore, usage as U
    prov = provstore.listing(root)
    pr = profiles.profile_rows(root)
    ro = profiles.role_rows(root, now)
    today = U.window_totals(U.read_rows(root, since=B.window_start(now, 86400)))
    ex = exchcfg.load(root)
    return {"exchange": {"active": ex["active"], "allowed": ex["allowed"]}, "mode": swap.day_mode(root), "rev": ro["rev"], "source": ro["source"], "live_team": ro["live_team"],
            "providers": {"total": len(prov["providers"]), "enabled": sum(1 for p in prov["providers"] if p["enabled"]),
                          "no_cred": [p["id"] for p in prov["providers"] if p["enabled"] and p["key"] and p["key"]["needed"] and not p["key"]["present"]]},
            "profiles": len(pr["profiles"]), "roles": len(ro["roles"]), "usage_today": today, "errors": prov["errors"] + pr["errors"] + ro["errors"]}


ROUTES = {"overview": _overview, "providers": _providers, "presets": _presets, "profiles": _profiles, "roles": _roles, "exchanges": _exchanges, "usage": _usage,
          "models_perf": _models_perf, "revisions": _revisions}


def api(path, query, root, now):
    """Контракт реестра EXT_API (dash/server.py): None, если путь не наш."""
    if not path.startswith(PREFIX):
        return None
    fn = ROUTES.get(path[len(PREFIX):])
    if fn is None:
        return 404, {"error": "unknown endpoint"}
    now = time.time() if now is None else now
    try:
        return 200, _safe(fn(root, now, query or {}))
    except Exception as e:           # раздел настроек не должен ронять дашборд; текст ошибки без путей
        return 500, {"error": "settings_failed", "kind": type(e).__name__}
