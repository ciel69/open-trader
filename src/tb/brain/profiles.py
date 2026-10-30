"""Профили и роли мозга без правки кода (WP-X): профиль = (провайдер, модель, параметры вызова), роль = основной профиль + цепочка запасных + необязательные
переопределения параметров и суточный бюджет роли.

Где что хранится:
  профили и переопределения ролей (`role_opts`)  `config/local/brain.json` (слой localcfg; поставляемый `config/brain.json` не меняется);
  назначения ролей (основной/запасные, команда)  подписанный `state/brain/active.json` с ревизиями и откатом (tb.brain.swap: use, set_chain, rollback).
Параметры вызова (temperature, max_tokens, thinking) задаются на уровне профиля; `role_opts.<роль>.params` переопределяет их для роли; `role_opts.<роль>.budget.day_usd`
ограничивает расход роли за сутки. Метку live_ok профилям ставит ТОЛЬКО живой eval владельца (здесь её не поставить); при смене модели или параметров профиля метка
сама становится недействительной (отпечаток профиля меняется).
Гейты: профиль, на который ссылается live-роль live-команды в режиме live, менять и удалять нельзя (создайте новый профиль и смените основной через brain.use с подтверждением);
профиль, стоящий в любой цепочке ролей, удалить нельзя; профиль можно создать только на включённого провайдера; роль live не переопределяется параметрами в режиме live.
Операции возвращают {"ok","code","msg","data"}; аудит записывает вызывающий (ctl или CLI).
"""
import time

from tb import paths
from tb.brain import common, conf as conf_mod, roles_runtime, swap
from tb.core import localcfg
from tb.llm import budget as budget_mod, providers, usage as usage_mod


def _res(ok, code, msg="", **data):
    return {"ok": ok, "code": code, "msg": msg, "data": data}


def _env(root):
    try:
        reg = providers.load(root)
    except providers.ProviderError:
        reg = None
    conf = conf_mod.load(root, reg, conf_mod.read_agents(root))
    return conf, reg


def _base_profiles(root):
    try:
        import json
        with open(conf_mod.conf_file(root), encoding="utf-8") as f:
            return set((json.load(f).get("profiles") or {}).keys())
    except (OSError, ValueError):
        return set()


def _update(root, fn):
    try:
        return localcfg.update_local("brain", fn, root)
    except ValueError as e:
        raise conf_mod.ConfError(str(e))


def chains_using(root, pid):
    """-> [(роль, позиция)] во всех цепочках действующих назначений (подписанный active.json или brain.json)."""
    conf, _reg = _env(root)
    a = swap.load_effective(root, conf)
    out = []
    for role, ch in a.shared_roles.items():
        for i, x in enumerate([ch["primary"]] + list(ch.get("fallback") or [])):
            if x == pid:
                out.append(("shared.%s" % role, i))
    for tid, t in a.teams.items():
        for role, ch in t["roles"].items():
            for i, x in enumerate([ch["primary"]] + list(ch.get("fallback") or [])):
                if x == pid:
                    out.append(("%s.%s" % (tid, role), i))
    return out


def live_roles_using(root, pid):
    """Роли, для которых профиль участвует в цепочке live-команды и роль несёт риск (live_risk)."""
    conf, _reg = _env(root)
    a = swap.load_effective(root, conf)
    roles = roles_runtime.load(root)
    out = []
    for role, ch in ((a.teams.get(a.live_team) or {}).get("roles") or {}).items():
        r = roles.get(role)
        if r is not None and r.live_risk and pid in [ch["primary"]] + list(ch.get("fallback") or []):
            out.append(role)
    return sorted(out)


def _is_live(root):
    return swap.day_mode(root) == "live"


# ---------------------------------------------------------------- профили
def profile_rows(root=None):
    conf, reg = _env(root)
    a = swap.load_effective(root, conf)
    rows = []
    for pid, p in sorted(conf.profiles.items()):
        prov = reg.providers.get(p.provider) if reg else None
        used = [r for r, _i in chains_using(root, pid)]
        rows.append({"id": pid, "provider": p.provider, "model": p.model, "model_name": prov.models[p.model].name if prov and p.model in prov.models else None,
                     "params": p.params, "live_ok": p.live_ok, "eval_ts": p.eval_ts, "provider_enabled": bool(prov and prov.enabled), "used_by": used,
                     "builtin": pid in _base_profiles(root)})
    return {"profiles": rows, "errors": conf.errors, "rev": a.rev, "source": a.source}


def profile_set(root, pid, provider=None, model=None, params=None):
    """Создаёт профиль или меняет существующий (поля, которых нет в вызове, остаются). Поставляемый профиль меняется слоем local."""
    if not (isinstance(pid, str) and conf_mod.RE_PROFILE.match(pid)):
        return _res(False, "E_ARGS", "id профиля: строчные буквы, цифры, _ и -, до 30 символов")
    conf, reg = _env(root)
    cur = conf.profiles.get(pid)
    new_prov, new_model = provider or (cur.provider if cur else None), model or (cur.model if cur else None)
    if not new_prov or not new_model:
        return _res(False, "E_ARGS", "для нового профиля нужны provider и model")
    if reg is None or new_prov not in reg.providers:
        return _res(False, "E_ARGS", "провайдер %s неизвестен" % str(new_prov)[:20])
    prov = reg.providers[new_prov]
    if new_model not in prov.models:
        return _res(False, "E_ARGS", "у провайдера %s нет модели %s" % (new_prov, str(new_model)[:20]))
    if not prov.enabled:
        return _res(False, "E_PROVIDER_OFF", "провайдер %s выключен: сначала включите (нужен ключ)" % new_prov)
    prm = dict(cur.params) if cur else {"temperature": 0.1, "max_tokens": 2500, "thinking": "off"}
    if params is not None:
        bad = conf_mod.check_params(params)
        if bad:
            return _res(False, "E_ARGS", bad)
        prm.update(params)
    if cur is not None and _is_live(root):
        live = live_roles_using(root, pid)
        if live:
            return _res(False, "E_LIVE_IN_USE", "профиль используют live-роли (%s): создайте новый профиль и смените основной через brain.use" % ", ".join(live), roles=live)
    body = {"provider": new_prov, "model": new_model, "params": prm}
    if cur is None or (cur.provider, cur.model) != (new_prov, new_model):
        body["live_ok"] = False
        body["eval_ts"] = None

    def mut(d):
        d.setdefault("version", 1)
        d.setdefault("profiles", {})[pid] = localcfg.merge((d.get("profiles") or {}).get(pid, {}), body)
        return d
    try:
        _update(root, mut)
    except conf_mod.ConfError as e:
        return _res(False, "E_STATE", str(e))
    conf2, _ = _env(root)
    if pid not in conf2.profiles:
        return _res(False, "E_ARGS", "профиль не принят проверкой: %s" % "; ".join(conf2.errors[:2]))
    return _res(True, "OK", "профиль сохранён", id=pid, created=cur is None, live_ok=conf2.profiles[pid].live_ok)


def profile_delete(root, pid):
    """Удаляет профиль, добавленный владельцем (слой local). Нельзя, пока он стоит в любой цепочке ролей; поставляемые профили не удаляются."""
    if pid in _base_profiles(root):
        return _res(False, "E_STATE", "поставляемый профиль удалить нельзя (config/brain.json): уберите его из цепочек ролей и оставьте")
    loc = localcfg.read_local("brain", root) or {}
    if pid not in (loc.get("profiles") or {}):
        return _res(False, "E_ARGS", "профиль неизвестен")
    use = chains_using(root, pid)
    if use:
        live = live_roles_using(root, pid)
        return _res(False, "E_LIVE_IN_USE" if live else "E_IN_USE", "профиль стоит в цепочках ролей: %s" % ", ".join(r for r, _i in use[:6]),
                    roles=[r for r, _i in use], live_roles=live)

    def mut(d):
        (d.get("profiles") or {}).pop(pid, None)
        return d
    try:
        _update(root, mut)
    except conf_mod.ConfError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "профиль удалён", id=pid)


# ---------------------------------------------------------------- роли
def role_rows(root=None, now=None):
    """Таблица ролей: роль -> основной/запасные (с провайдером/моделью/состоянием), параметры (эффективные), бюджет, расход за сутки, live_ok, live_risk."""
    now = time.time() if now is None else now
    conf, reg = _env(root)
    a = swap.load_effective(root, conf)
    roles = roles_runtime.load(root)
    rows_today = usage_mod.read_rows(root, since=budget_mod.window_start(now, 86400))
    by_role = usage_mod.aggregate(rows_today, "role")
    out = []

    def prof_info(pid):
        p = conf.profiles.get(pid)
        if p is None:
            return {"id": pid, "missing": True}
        prov = reg.providers.get(p.provider) if reg else None
        return {"id": pid, "provider": p.provider, "model": p.model, "model_name": prov.models[p.model].name if prov and p.model in prov.models else None,
                "provider_enabled": bool(prov and prov.enabled), "live_ok": p.live_ok, "params": p.params}

    def row(rid, scope, team, ch):
        r = roles.get(rid)
        ro = conf.role_opts.get(rid) or {}
        prim = prof_info(ch["primary"]) if ch else None
        eff = dict((prim or {}).get("params") or {})
        eff.update(ro.get("params") or {})
        spent = by_role.get(rid) or {}
        bud = (ro.get("budget") or {}).get("day_usd")
        out.append({"role": rid, "scope": scope, "team": team, "live_risk": bool(r.live_risk) if r else None, "io": r.io if r else None, "known": r is not None,
                    "primary": prim, "fallback": [prof_info(x) for x in (ch.get("fallback") or [])] if ch else [],
                    "live_ok": all(conf.profiles[x].live_ok for x in ([ch["primary"]] + list(ch.get("fallback") or [])) if x in conf.profiles) if ch else None,
                    "params_override": ro.get("params") or {}, "params_effective": eff, "budget": {"day_usd": bud},
                    "today": {"usd": round(spent.get("usd", 0.0), 6), "calls": spent.get("calls", 0), "errors": spent.get("errors", 0),
                              "budget_left_usd": None if bud is None else round(max(0.0, float(bud) - spent.get("usd", 0.0)), 6)}})
    for tid, t in sorted(a.teams.items()):
        for rid, ch in sorted(t["roles"].items()):
            row(rid, "team", tid, ch)
    for rid, ch in sorted(a.shared_roles.items()):
        row(rid, "shared", None, ch)
    return {"roles": out, "live_team": a.live_team, "rev": a.rev, "source": a.source, "mode": swap.day_mode(root), "errors": conf.errors}


def role_opts_set(root, role, params=None, budget=None, clear=False):
    """Переопределения роли: params (temperature, max_tokens, thinking) и budget {day_usd}. clear=True снимает все. Для live-роли в режиме live запрещено."""
    if role not in conf_mod.ROLE_SCOPE:
        return _res(False, "E_ARGS", "роль неизвестна")
    r = roles_runtime.load(root).get(role)
    if r is not None and r.live_risk and _is_live(root):
        return _res(False, "E_LIVE_IN_USE", "роль %s несёт риск, режим live: параметры меняются при остановленном дне" % role)
    conf, _ = _env(root)
    cur = dict(conf.role_opts.get(role) or {"params": {}, "budget": {}})
    if clear:
        new = {}
    else:
        new = {"params": dict(cur.get("params") or {}), "budget": dict(cur.get("budget") or {})}
        if params is not None:
            new["params"].update(params)          # None остаётся в записи: локальный слой так гасит значение из config/brain.json (читатель отбрасывает None)
        if budget is not None:
            new["budget"].update(budget)
        _o, bad = conf_mod.check_role_opts(role, new)
        if bad:
            return _res(False, "E_ARGS", bad)

    def mut(d):
        d.setdefault("version", 1)
        ro = d.setdefault("role_opts", {})
        if new and not clear:                # прежние «сняты» (None) в локальном слое не теряем, иначе значение из config/brain.json вернётся
            for sec in ("params", "budget"):
                for k, v in ((ro.get(role) or {}).get(sec) or {}).items():
                    if v is None and k not in new[sec]:
                        new[sec][k] = None
        if new:
            ro[role] = new
        else:
            ro.pop(role, None)
        return d
    try:
        # слой local сливается рекурсивно: чтобы снять ключ, перезаписываем запись роли целиком (удаляем и ставим заново)
        _update(root, mut)
    except conf_mod.ConfError as e:
        return _res(False, "E_STATE", str(e))
    return _res(True, "OK", "параметры роли сохранены" if new else "переопределения роли сняты", role=role, opts=new)


def effective_role_opts(root, role):
    conf, _ = _env(root)
    return conf.role_opts.get(role) or {}


def signal_reload(root, by="settings", now=None):
    """Просит демон мозга перечитать providers/brain/roles (файл-сигнал; назначения не меняются). Ошибки записи не мешают операции."""
    from tb.core.filelock import atomic_write_json
    now = time.time() if now is None else now
    try:
        atomic_write_json(swap.reload_req_file(root), {"ts": round(now, 3), "by": by}, indent=None)
        return True
    except OSError:
        return False
