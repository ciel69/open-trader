"""CLI мозга (кроме `run`): смена модели, откат, статус, задания, проверка конфигурации (WP11-C2; 07b п. 6.3).

  brain status [--json]                         назначения, ревизия, расход сегодня по профилям, очередь, «только защита», последняя смена
  brain enable | disable                        включить/выключить подписчика api в config/local/subscriptions.json (без ручного JSON), напечатать чего ещё не хватает для старта мозга
  brain use ЦЕЛЬ ПРОФИЛЬ [--confirm "ФРАЗА"] [--accept ID ...] [--ack "я принимаю риск"]
        ЦЕЛЬ: all (профиль для ВСЕХ ролей: роли live-команды и общие, одна ревизия) | A.position_manager | shared.news_reader | position_manager (роль live-команды) | A (все роли команды) | live (тогда ПРОФИЛЬ = команда: `use live B`)
        T0 (без влияния на деньги) применяется сразу; в режиме live смена live-ролей/команды требует TTY (Terminal), фразу `--confirm` (её печатает команда без --confirm)
        и приём мягких предупреждений (`--accept W_SWAP_...` по одному плюс `--ack "я принимаю риск"`)
  brain profile list | set ID [--provider P --model M --set temperature=0.1 --set max_tokens=2500 --set thinking=low] | delete ID    профили (config/local/brain.json)
  brain role list | chain РОЛЬ [--primary P] [--fallback A,B] | params РОЛЬ --set k=v ... | budget РОЛЬ --day-usd X | clear РОЛЬ         роли: цепочка (подписанные ревизии), параметры, бюджет
  brain rollback [--to REV] [--confirm]         возврат к предыдущей ревизии (новая ревизия; в live нужен TTY)
  brain reload [--from-config]                  сигнал демону перечитать конфиги | назначения из config/brain.json новой ревизией
  brain jobs [--status S] [-n N]                очередь заданий
  brain probe ПРОФИЛЬ|--all [--live]            проверка провайдера (GET списка моделей; --live обращается к сети, нужен ключ владельца) и отметка свежей проверки
  brain check                                   проверка config/brain.json, config/roles/*, providers.json
  brain verify                                  цепочка журнала смен state/brain/swaps.jsonl
  brain eval [--role R] [--profile P] [--mode golden|yield|garbage] [--json]   регрессионный набор промптов на ФЕЙКОВОМ LLM (сети нет, меток нет; WP11-C3)
  brain eval [--role R] --profile P --live --confirm [--max-usd X]   ВЛАДЕЛЕЦ: прогон на живой модели (нужен ключ и TTY), отчёт state/brain/eval/, метка live_ok при выполнении порогов
Права для чата Claude Code: deny `Bash(./tb brain use *)`, `Bash(./tb brain rollback *)`; смена в live без владельца невозможна (TTY, фраза, подписанный active.json).
"""
import argparse
import json
import sys
import time

from tb import paths
from tb.brain import common, conf as conf_mod, jobs, roles_runtime, swap

ACK = "я принимаю риск"


def _out(o):
    print(json.dumps(o, ensure_ascii=False, indent=1, sort_keys=True))


def _tty():
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _usage_today(root, now):
    try:
        from tb.llm import budget as B, usage as U
        rows = U.read_rows(root, since=B.window_start(now, 86400))
        return {"total": U.window_totals(rows), "by_profile": U.aggregate(rows, "profile"), "by_role": U.aggregate(rows, "role")}
    except Exception:
        return {}


def cmd_status(root, a):
    now = time.time()
    st = swap.status(root, now)
    health = common.read_json(paths.runtime("brain", "state.json", root=root)) or {}
    out = dict(st, queue=jobs.stats(root), cooldown=health.get("cooldown") or {}, last_fallback=health.get("last_fallback"), usage_today=_usage_today(root, now),
               last_swaps=[{k: r.get(k) for k in ("seq", "ts", "rev", "kind", "tier", "from", "to", "by")} for r in swap.read_swaps(root, 5)])
    if a.json:
        _out(out)
    else:
        print("ревизия %s (%s), live-команда %s, агент %s%s" % (st["rev"], st["source"], st["live_team"], st["agent"], "  ТОЛЬКО ЗАЩИТА" if st["protect_only"] else ""))
        for r, p in sorted(st["profiles"].items()):
            print("  %-18s %s%s" % (r, p, "" if st["live_ok"].get(p, {}).get("ok") else "  (без live_ok)"))
        print("очередь: %s; расход сегодня: $%.4f" % (out["queue"] or "пусто", (out.get("usage_today", {}).get("total") or {}).get("usd", 0.0)))
        if st.get("warn"):
            print("ВНИМАНИЕ: " + st["warn"])
        for e in st.get("config_errors") or []:
            print("конфигурация: " + e)
    return 0


def cmd_use(root, a, rollback=False):
    args = {} if rollback else ({"role": "live", "team": a.profile} if a.target == "live" else
                                ({"all": True, "profile": a.profile} if a.target == "all" else {"role": a.target, "profile": a.profile}))
    if rollback and a.to is not None:
        args["to"] = a.to
    args["accepted"] = sorted(set(a.accept or []))
    now = time.time()
    chk = swap.classify(root, args, now, action="rollback" if rollback else "use")
    live_gate = chk["danger"]
    if chk["blocked"]:
        _out({"ok": False, "code": chk["code"], "gates": [g for g in chk["gates"] if not g["ok"]]})
        return 1
    if live_gate:
        if not _tty():
            print("смена в режиме live: запускает владелец в Terminal (нужен TTY)", file=sys.stderr)
            return 2
        want = chk["phrases"]
        if want and a.confirm != want[0]:
            _out({"ok": False, "code": "E_SWAP_PHRASE", "phrase": want[0], "warnings": chk["warnings"],
                  "hint": "повторите с --confirm \"%s\"%s" % (want[0], " и по одному --accept ID, плюс --ack \"%s\"" % ACK if chk["warnings"] else "")})
            return 2
        if chk["warnings"] and a.ack != ACK:
            _out({"ok": False, "code": "E_WARN", "warnings": chk["warnings"], "hint": "--accept ID для каждого и --ack \"%s\"" % ACK})
            return 2
    r = (swap.rollback if rollback else swap.use)(root, args, now, by="owner-cli", phrase_ok=bool(live_gate))
    _out(r)
    return 0 if r["ok"] else 1


def cmd_reload(root, a):
    r = swap.reload(root, {"from_config": bool(a.from_config)}, time.time(), by="owner-cli")
    _out(r)
    return 0 if r["ok"] else 1


def cmd_jobs(root, a):
    rows = jobs.list_jobs(root, a.status)[-a.n:]
    for j in rows:
        print(json.dumps({k: j.get(k) for k in ("id", "status", "prio", "key", "types", "count", "attempts", "rev", "result")}, ensure_ascii=False, separators=(",", ":")))
    return 0


def cmd_check(root, a):
    from tb.llm import providers
    try:
        reg = providers.load(root)
    except Exception as e:
        reg, perr = None, str(e)[:80]
    else:
        perr = None
    c = conf_mod.load(root, reg)
    rr = roles_runtime.load(root)
    ok = c.loaded and not c.errors and not rr.errors and perr is None
    _out({"ok": ok, "brain_errors": c.errors, "brain_warnings": c.warnings, "roles": sorted(rr.roles), "role_errors": rr.errors, "providers_error": perr})
    return 0 if ok else 1


def cmd_probe(root, a):
    from tb.llm import adapters, providers
    from tb.llm.transport import Transport
    conf = conf_mod.load(root)
    reg = providers.load(root)
    ids = sorted(conf.profiles) if a.all else [a.profile]
    out, ok = [], True
    for pid in ids:
        p = conf.profiles.get(pid)
        if p is None:
            out.append({"profile": pid, "error": "профиль неизвестен"})
            ok = False
            continue
        row = {"profile": pid, "provider": p.provider, "model": p.model}
        if not a.live:
            row["note"] = "сеть не использовалась; проверка провайдера: --live (нужен ключ владельца)"
        else:
            tr = Transport()
            try:
                prov = reg.provider(p.provider)
                row["probe"] = adapters.make(prov, prov.model(p.model), transport=tr).probe(ping=False)
                good = row["probe"].get("model_name_in_list") is not False
                swap.record_probe(root, p, good, time.time())
                ok = ok and good
            except Exception as e:
                row["error"] = getattr(e, "code", e.__class__.__name__)
                swap.record_probe(root, p, False, time.time())
                ok = False
            finally:
                tr.close()
        out.append(row)
    _out({"ok": ok, "live": bool(a.live), "profiles": out})
    return 0 if ok else 1


def _fmt_report(rep):
    t = rep["totals"]
    print("eval (%s): сценариев %d, валидных с первой попытки %.1f%%, нарушений %d (критичных %d), прошло запрет %d, допустимых решений %.1f%%, расход $%.4f"
          % (rep["mode"], t["n"], 100 * t["valid_first_rate"], t["violations"], t["critical_violations"], t["leaked"], 100 * t["match_rate"], t["usd"]))
    for rid, v in sorted(rep["roles"].items()):
        print("  %-17s v%s n=%-3d valid1=%.0f%% нарушений=%d (крит %d) прошло=%d решение=%.0f%% %s" % (
            rid, v["version"], v["n"], 100 * v["valid_first_rate"], v["violations"], v["critical_violations"], v["leaked"], 100 * v["match_rate"], "OK" if v["passed"] else "НЕ ПРОШЛО"))
    if rep.get("aborted"):
        print("прогон прерван: %s" % rep["aborted"])


def cmd_eval(root, a):
    from tb.brain import evalrun
    conf = conf_mod.load(root)
    rr = roles_runtime.load(root)
    th = evalrun.thresholds_from(conf)
    if a.role and a.role not in evalrun.ROLES:
        print("роль для eval: %s" % ", ".join(evalrun.ROLES), file=sys.stderr)
        return 2
    if a.live:
        if not a.profile:
            print("--live требует --profile ПРОФИЛЬ", file=sys.stderr)
            return 2
        scens = evalrun.load_scenarios(root, a.role)
        n = sum(len(v) for v in scens.values())
        if not a.confirm:
            _out({"ok": False, "live": True, "note": "ЖИВОЙ прогон тратит деньги и использует ключ владельца; повторите с --confirm в Terminal", "profile": a.profile,
                  "scenarios": n, "max_usd": a.max_usd if a.max_usd is not None else th["max_usd"], "thresholds": th})
            return 2
        if not _tty():
            print("живой eval запускает владелец в Terminal (нужен TTY)", file=sys.stderr)
            return 2
        rep = evalrun.run_live(root, conf, rr, a.profile, role=a.role, max_usd=a.max_usd)
        if rep.get("ok") is False:
            _out(rep)
            return 1
        if a.json:
            _out({k: v for k, v in rep.items() if k != "rows"})
        else:
            _fmt_report(rep)
            print("отчёт: %s" % rep.get("report"))
            print("метка live_ok: %s%s" % ("поставлена" if rep.get("live_ok_set") else "не поставлена", " (частичный прогон)" if rep.get("partial") else ""))
        return 0 if rep["passed"] else 1
    scens = evalrun.load_scenarios(root, a.role)
    pool = [s for v in scens.values() for s in v]
    rep = evalrun.run(rr, scens, evalrun.ScenarioClient(pool, a.mode), th, role=a.role, mode="fake:%s" % a.mode)
    if a.json:
        _out({k: v for k, v in rep.items() if k != "rows"})
    else:
        _fmt_report(rep)
        print("фейковый прогон: меток live_ok не ставит; живой: --live --confirm --profile P (владелец)")
    return 0 if rep["passed"] else 1


def _audit(action, args, r):
    try:
        from tb.core import cfgaudit
        cfgaudit.append(paths.root(), action, args, bool(r.get("ok")), r.get("code") or "-")
    except Exception:
        pass


def _kv(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def cmd_profile_role(root, argv):
    from tb.brain import profiles
    ap = argparse.ArgumentParser(prog="tb brain")
    sp = ap.add_subparsers(dest="grp")
    pf = sp.add_parser("profile")
    pf.add_argument("op", choices=["list", "set", "delete"])
    pf.add_argument("id", nargs="?")
    pf.add_argument("--provider", default=None)
    pf.add_argument("--model", default=None)
    pf.add_argument("--set", action="append", default=[], dest="sets")
    rl = sp.add_parser("role")
    rl.add_argument("op", choices=["list", "chain", "params", "budget", "clear"])
    rl.add_argument("role", nargs="?")
    rl.add_argument("--primary", default=None)
    rl.add_argument("--fallback", default=None, help="через запятую; пустая строка = без запасных")
    rl.add_argument("--set", action="append", default=[], dest="sets")
    rl.add_argument("--day-usd", type=float, default=None, dest="day_usd")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    now = time.time()
    if a.grp == "profile":
        if a.op == "list":
            _out(profiles.profile_rows(root))
            return 0
        if not a.id:
            print("нужен ID профиля", file=sys.stderr)
            return 2
        r = profiles.profile_set(root, a.id, a.provider, a.model, _kv(a.sets) or None) if a.op == "set" else profiles.profile_delete(root, a.id)
        _audit("brain.profile", {"op": a.op, "id": a.id}, r)
    else:
        if a.op == "list":
            _out(profiles.role_rows(root, now))
            return 0
        if not a.role:
            print("нужна РОЛЬ", file=sys.stderr)
            return 2
        if a.op == "chain":
            args = {"role": a.role}
            if a.primary:
                args["primary"] = a.primary
            if a.fallback is not None:
                args["fallback"] = [x for x in a.fallback.split(",") if x]
            r = swap.set_chain(root, args, now, by="owner-cli", phrase_ok=False)
        elif a.op == "params":
            r = profiles.role_opts_set(root, a.role.split(".")[-1], params=_kv(a.sets))
        elif a.op == "budget":
            r = profiles.role_opts_set(root, a.role.split(".")[-1], budget={"day_usd": a.day_usd})
        else:
            r = profiles.role_opts_set(root, a.role.split(".")[-1], clear=True)
        _audit("brain.role", {"op": a.op, "role": a.role}, r)
    if r.get("ok"):
        profiles.signal_reload(root, "owner-cli", now)
    _out(r)
    return 0 if r.get("ok") else 1


def cmd_enable(root, enable=True, out=print):
    """Подписчик api в личном слое (localcfg, глубокое слияние; поставляемый config/subscriptions.json не трогается). Идемпотентно: уже в нужном состоянии -> записи нет.
    -> 0 (включён и условия есть / выключен) | 1 (включён, но чего-то не хватает: причина и как исправить напечатаны) | 2 (локальный файл повреждён)."""
    from tb.core import localcfg
    from tb.platform import dispatcher, quick
    subs, _ = dispatcher.load_subscribers(root)
    now_on = any(x.get("id") == "api" and x.get("enabled") for x in subs)
    word = "включён" if enable else "выключен"
    if now_on == enable:
        out("подписчик api уже %s (config/local/subscriptions.json не менялся)" % word)
    else:
        def fn(cur):
            lst = cur.get("subscribers") if isinstance(cur.get("subscribers"), list) else []
            row = next((x for x in lst if isinstance(x, dict) and x.get("id") == "api"), None)
            if row is None:
                row = {"id": "api"}
                lst.append(row)
            row["enabled"] = bool(enable)
            cur["subscribers"] = lst
            return cur
        try:
            localcfg.update_local("subscriptions", fn, root)
        except (ValueError, OSError) as e:
            print("%s" % e, file=sys.stderr)
            return 2
        out("подписчик api %s: config/local/subscriptions.json (поставляемый config/subscriptions.json не менялся)" % word)
    running = False
    try:
        from tb.platform import status as ST
        running = bool(ST.supervisor_info(root).get("running"))
    except Exception:
        pass
    if not enable:
        out("мозг не будет получать задания; сервис brain остановится при ./tb restart" if running else "мозг выключен; ./tb start запустит платформу без него")
        return 0
    ok, reason, howto = quick.brain_readiness(root)
    if ok:
        out("условия мозга выполнены (подписчик, профиль/роли, ключ провайдера)")
        out("дальше: ./tb restart" if running else "дальше: ./tb start")
        return 0
    out("для старта мозга ещё не хватает: %s" % reason)
    out("как исправить: %s" % howto)
    out("после этого: ./tb brain enable (проверка) и %s" % ("./tb restart" if running else "./tb start"))
    return 1


def main(argv):
    if argv and argv[0] in ("profile", "role"):
        return cmd_profile_role(paths.root(), argv)
    ap = argparse.ArgumentParser(prog="tb brain", description="API-мозг: смена модели, статус, очередь")
    sp = ap.add_subparsers(dest="cmd")
    s = sp.add_parser("status")
    s.add_argument("--json", action="store_true")
    for name in ("use", "rollback"):
        u = sp.add_parser(name)
        if name == "use":
            u.add_argument("target")
            u.add_argument("profile")
        else:
            u.add_argument("--to", type=int, default=None)
        u.add_argument("--confirm", default=None)
        u.add_argument("--accept", action="append", default=[])
        u.add_argument("--ack", default=None)
    rl = sp.add_parser("reload")
    rl.add_argument("--from-config", action="store_true", dest="from_config")
    j = sp.add_parser("jobs")
    j.add_argument("--status", default=None)
    j.add_argument("-n", type=int, default=20)
    pr = sp.add_parser("probe")
    pr.add_argument("profile", nargs="?")
    pr.add_argument("--all", action="store_true")
    pr.add_argument("--live", action="store_true")
    sp.add_parser("check")
    sp.add_parser("verify")
    ev = sp.add_parser("eval")
    ev.add_argument("--role", default=None)
    ev.add_argument("--profile", default=None)
    ev.add_argument("--live", action="store_true")
    ev.add_argument("--confirm", action="store_true")
    ev.add_argument("--max-usd", type=float, default=None, dest="max_usd")
    ev.add_argument("--mode", default="golden", choices=("golden", "yield", "garbage", "silent"))
    ev.add_argument("--json", action="store_true")
    sp.add_parser("enable")
    sp.add_parser("disable")
    sp.add_parser("run")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not a.cmd:
        ap.print_usage(sys.stderr)
        return 2
    root = paths.root()
    if a.cmd in ("enable", "disable"):
        return cmd_enable(root, a.cmd == "enable")
    if a.cmd == "status":
        return cmd_status(root, a)
    if a.cmd == "use":
        return cmd_use(root, a)
    if a.cmd == "rollback":
        return cmd_use(root, a, rollback=True)
    if a.cmd == "reload":
        return cmd_reload(root, a)
    if a.cmd == "jobs":
        return cmd_jobs(root, a)
    if a.cmd == "check":
        return cmd_check(root, a)
    if a.cmd == "probe":
        if not a.all and not a.profile:
            print("использование: brain probe ПРОФИЛЬ|--all [--live]", file=sys.stderr)
            return 2
        return cmd_probe(root, a)
    if a.cmd == "eval":
        return cmd_eval(root, a)
    if a.cmd == "verify":
        v = swap.verify_swaps(root)
        _out(v)
        return 0 if v["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
