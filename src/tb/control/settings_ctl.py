"""Исполнение настроечных действий ctl: llm.provider, brain.profile, brain.role, exchange.set (WP-X). Вызывается из handlers.Ctl.dispatch/requirements.

Защита: те же рамки, что у остальных действий (подпись, срок, nonce, аудит каждой команды без значений). Секретов здесь нет. Смена основного профиля live-роли в режиме live идёт
через tb.brain.swap (гейты, проба, фраза, предупреждения), включение live-биржи требует фразы и nonce, смена активной биржи и режима биржи возможна только при остановленной платформе.
"""
from tb.control import brain_iface

def _out(ctl, cmd, r, extra=None):
    """Результат операции {"ok","code","msg","data"} -> ответ ctl."""
    data = dict(r.get("data") or {})
    if r.get("msg"):
        data["detail"] = r["msg"]
    data.update(extra or {})
    if r["ok"]:
        return ctl.finish(cmd, "done", "OK", data)
    return ctl.finish(cmd, "rejected", r["code"] if r["code"].startswith("E_") else "E_ARGS", data)


def requirements(ctl, action, args):
    """-> {"danger","phrases","gates","warnings","blocked"} или None, если действие не требует подтверждений."""
    req = {"danger": False, "phrases": [], "gates": [], "warnings": [], "blocked": False}
    root, now = ctl.root, ctl.now()
    if action == "brain.role" and args["op"] == "chain" and args.get("primary"):
        from tb.brain import swap
        try:
            tier = swap.role_tier(root, args["role"])
        except Exception:
            tier = 0
        if tier >= 1:
            c = brain_iface.classify(root, {"role": args["role"], "profile": args["primary"], "accepted": args.get("accepted", [])}, now, "use")
            if c is not None:
                req["gates"], req["warnings"], req["blocked"], req["danger"] = c["gates"], c["warnings"], c["blocked"], bool(c["danger"])
                req["phrases"] = list(c["phrases"]) + (["я принимаю риск"] if c["warnings"] else [])
    elif action == "exchange.set":
        from tb.core import exchcfg
        op, eid = args["op"], args.get("id")
        ex = (exchcfg.load(root)["exchanges"].get(eid) or {}) if eid else {}
        live = (op == "mode" and args.get("mode") == "live") or (op in ("activate", "enable") and ex.get("mode") == "live")
        if op in ("activate", "mode", "enable", "disable", "allowed") and ctl.platform().get("state") != "stopped":
            req["gates"] = [{"id": "E_STATE", "ok": False, "msg": "биржи меняются только при остановленной платформе"}]
            req["blocked"] = True
        if live:
            req["danger"] = True
            req["phrases"] = ["ВКЛЮЧАЮ LIVE НА БИРЖЕ %s" % str(eid).upper()]
    return req


def handle(ctl, cmd, phrase_ok=False):
    a, args = cmd["action"], cmd["args"]
    root, now = ctl.root, ctl.now()
    op = args["op"]
    if a == "llm.provider":
        from tb.brain import profiles
        from tb.llm import keystore, provstore, providers
        pid = args["id"]
        if op == "add":
            r = provstore.add(root, pid, preset=args.get("preset"), spec=args.get("spec"))
        elif op == "update":
            r = provstore.update(root, pid, args["patch"])
        elif op == "remove":
            r = provstore.remove(root, pid)
        elif op == "enable":
            r = provstore.set_enabled(root, pid, True)
        elif op == "disable":
            use = [row for row in profiles.profile_rows(root)["profiles"] if row["provider"] == pid and profiles.live_roles_using(root, row["id"])]
            if use:
                return _out(ctl, cmd, {"ok": False, "code": "E_LIVE_IN_USE", "msg": "провайдера использует live-роль (профиль %s)" % use[0]["id"], "data": {}})
            r = provstore.set_enabled(root, pid, False)
        elif op == "model_set":
            r = provstore.model_set(root, pid, args["model"], args["spec"])
        elif op == "model_remove":
            r = provstore.model_remove(root, pid, args["model"])
        elif op == "key_clear":
            reg = None
            try:
                reg = providers.load(root)
            except providers.ProviderError:
                pass
            p = reg.providers.get(pid) if reg else None
            if p is None or not p.key_file:
                r = {"ok": False, "code": "E_ARGS", "msg": "у провайдера нет ключа", "data": {}}
            elif p.enabled and profiles.profile_rows(root) and any(row["provider"] == pid and row["used_by"] for row in profiles.profile_rows(root)["profiles"]):
                r = {"ok": False, "code": "E_IN_USE", "msg": "провайдер используется ролями: сначала выключите его или смените профили", "data": {}}
            else:
                try:
                    st = keystore.clear_key(p.key_file, p.key_env)
                    r = {"ok": True, "code": "OK", "msg": "ключ удалён", "data": {"key": st}}
                except keystore.SecretError as e:
                    r = {"ok": False, "code": "E_ARGS", "msg": str(e), "data": {}}
        else:
            r = provstore.check(root, pid, args.get("model"), live=False)
            if not r["ok"]:      # проверка не «ошибка команды»: отчёт возвращается как данные
                return ctl.finish(cmd, "done", "OK", dict(r["data"], ok=False, detail=r["msg"]))
            return ctl.finish(cmd, "done", "OK", dict(r["data"], ok=True))
        if r["ok"]:
            profiles.signal_reload(root, "ctl", now)
        return _out(ctl, cmd, r)
    if a == "brain.profile":
        from tb.brain import profiles
        r = profiles.profile_set(root, args["id"], args.get("provider"), args.get("model"), args.get("params")) if op == "set" else profiles.profile_delete(root, args["id"])
        if r["ok"]:
            profiles.signal_reload(root, "ctl", now)
        return _out(ctl, cmd, r)
    if a == "brain.role":
        from tb.brain import profiles
        if op == "chain":
            res = brain_iface.swap_action(root, "set_chain", dict(args, role=args["role"]), now, phrase_ok=phrase_ok)
            return _out(ctl, cmd, res)
        r = profiles.role_opts_set(root, args["role"].split(".")[-1], args.get("params"), args.get("budget"), clear=(op == "opts_clear"))
        if r["ok"]:
            profiles.signal_reload(root, "ctl", now)
        return _out(ctl, cmd, r)
    if a == "exchange.set":
        from tb.core import exchcfg
        try:
            if op == "activate":
                exchcfg.set_active(args["id"], root)
            elif op == "enable":
                exchcfg.set_enabled(args["id"], True, root)
            elif op == "disable":
                exchcfg.set_enabled(args["id"], False, root)
            elif op == "mode":
                exchcfg.set_mode(args["id"], args["mode"], root)
            elif op == "allowed":
                exchcfg.set_allowed(args["ids"], root)
            else:
                exchcfg.set_limits(args["id"], args["max_open_positions"], root)
        except exchcfg.ExchangeError as e:
            return ctl.finish(cmd, "rejected", "E_ARGS", {"detail": str(e)})
        return ctl.finish(cmd, "done", "OK", {"exchanges": exchcfg.load(root)["active"], "detail": "сохранено; действует при следующем старте платформы"})
    return ctl.finish(cmd, "rejected", "E_UNKNOWN")
