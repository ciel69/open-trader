"""CLI уровней риска (WP16): `./tb risk level show|list|check|set` и `./tb risk init-key`.

  risk level show [--json]                     выбранный уровень, итоговые лимиты (минимум с agents.json и exec_limits), предупреждения
  risk level list                              таблица уровней 1-5 (значения уровней 1, 2, 4, 5 и потолки ПРЕДЛОЖЕНЫ, подтвердить владельцу)
  risk level check [--apply]                   условия авто-понижения (просадка, суточный убыток); --apply понижает на ступень (повысить нельзя)
  risk level set N --confirm "УРОВЕНЬ РИСКА N <имя>" [--accept ID ...] [--ack "я принимаю риск"] [--why причина]
  risk init-key                                создаёт ~/.config/traider-bot/control.json (hmac_key, права 0600), только если файла нет

`set` и `init-key` запускает владелец в Terminal (нужен TTY). Права для чата: `deny Bash(./tb risk level set *)`, `Bash(./tb risk init-key)`.
Без --confirm `set` ничего не меняет: печатает фразу и предупреждения (для уровней 4-5 каждое принимается отдельно: --accept ID).
Агент через шлюз уровень менять не может (инструмента нет, файл подписан ключом вне досягаемости чата).
"""
import argparse
import json
import os
import secrets
import sys
import time

from tb import paths
from tb.core import portfolio, risklevels

EXIT_REFUSED = 2


def _is_tty():
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _out(*a):
    print(*a)


def _err(*a):
    print(*a, file=sys.stderr)


def _equity():
    try:
        return portfolio.total(portfolio.load())
    except Exception:
        return None


def _agents_limits(root=None):
    try:
        with open(paths.config("agents.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return {k: (v.get("limits") or {}) for k, v in (d.get("agents") or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def cmd_show(args):
    root = None
    cur = risklevels.current_level(root)
    eq = _equity()
    lim = risklevels.limits_summary(root, equity=eq)
    agents = {}
    for name, al in sorted(_agents_limits(root).items()):
        s = risklevels.limits_summary(root, agent_limits=al, equity=eq)
        agents[name] = {k: s.get(k) for k in ("risk_per_trade_frac", "risk_per_trade_usdt", "risk_cap_usdt", "size_mult_cap")}
        agents[name]["warnings"] = risklevels.agent_warnings(al, root, name)
    if args.json:
        _out(json.dumps({"current": {k: cur[k] for k in ("level", "name", "source", "since", "by", "warnings", "confirmed")}, "limits": lim, "agents": agents},
                        ensure_ascii=False, indent=1))
        return 0
    _out("Уровень риска: %d %s (источник: %s%s)" % (cur["level"], cur["name"], cur["source"], ", с %s" % cur["since"] if cur.get("since") else ""))
    for w in cur["warnings"]:
        _out("  ВНИМАНИЕ: " + w)
    if lim.get("config"):
        _out("  риск на сделку %.2f%% капитала%s, убыток за день до %.0f%%, позиций до %d, общий открытый риск до %.0f%%" % (
            100 * lim["risk_per_trade_frac"], " (%.3f USDT)" % lim["risk_per_trade_usdt"] if "risk_per_trade_usdt" in lim else "",
            100 * lim["daily_loss_frac"], lim["max_open_positions"], 100 * lim["total_open_risk_frac"]))
        _out("  бюджет сессий x%.2f, плечо x%.2f, RR +%.2f, size_mult не выше %.2f" % (lim["session_budget_mult"], lim["leverage_mult"], lim["min_net_rr_add"], lim["size_mult_cap"]))
        _out("  перенос: %s; добор: %s; вход без подтверждения: %s; сетапы до класса %s" % (
            lim["carry"]["policy"], "разрешён уровнем" if lim["pyramid"]["level_allows"] else "нет", ("до x%s" % lim["no_confirm"]["size_mult_max"]) if lim["no_confirm"]["allowed"] else "нет",
            lim["setups_aggr"]))
    for name, a in agents.items():
        _out("  агент %s: риск на сделку не выше %.2f%%%s" % (name, 100 * a["risk_per_trade_frac"], " и %.2f USDT" % a["risk_cap_usdt"] if a.get("risk_cap_usdt") else ""))
        for w in a["warnings"]:
            _out("    " + w)
    return 0


def cmd_list(args):
    cfg, w = risklevels.load_config()
    if cfg is None:
        _err("нет config/risk_levels.json")
        return EXIT_REFUSED
    cur = risklevels.current_level()["level"]
    conf = set(cfg.get("confirmed_levels") or [])
    _out("ПРЕДЛОЖЕНО, подтвердить владельцу (подтверждён только %s)" % ", ".join(str(x) for x in sorted(conf)) if conf else "ни один уровень не подтверждён")
    _out(" ур  имя                    риск/сделка  убыток/день  позиций  общий риск  перенос         добор  без подтв.  статус")
    for n in sorted(int(k) for k in cfg["levels"]):
        L = cfg["levels"][str(n)]
        _out("%s%d  %-22s %6.1f%%     %6.1f%%     %5d    %6.1f%%    %-14s  %-5s  %-10s  %s" % (
            "*" if n == cur else " ", n, L["name"], 100 * L["risk_per_trade_frac"], 100 * L["daily_loss_frac"], L["max_open_positions"], 100 * L["total_open_risk_frac"],
            L["carry"]["policy"], "да" if L["pyramid"]["allowed"] else "нет", ("до x%s" % L["no_confirm"].get("size_mult_max")) if L["no_confirm"]["allowed"] else "нет",
            "подтверждён" if n in conf else "ПРЕДЛОЖЕНО"))
    ce = cfg["ceilings"]
    _out("потолки (уровнем не превысить): риск/сделка %.0f%%, убыток/день %.0f%%, позиций %d, общий риск %.0f%%" % (
        100 * ce["risk_per_trade_frac"], 100 * ce["daily_loss_frac"], ce["max_open_positions"], 100 * ce["total_open_risk_frac"]))
    return 0


def cmd_check(args):
    st = risklevels.check_state()
    _out("уровень %d; просадка %.1f%%; результат за сегодня %.3f при лимите %.3f" % (st["level"], 100 * st["dd"], st["realized_today"], st["daily_limit"]))
    if not st["reasons"]:
        _out("условий авто-понижения нет")
        return 0
    _out("условия авто-понижения: " + ", ".join(st["reasons"]))
    if not args.apply:
        _out("(без --apply ничего не меняется)")
        return 0
    r = risklevels.auto_downgrade(reasons=st["reasons"])
    if r.get("error"):
        _err("понизить не удалось: %s" % r["error"])
        return EXIT_REFUSED
    _out("уровень %d -> %d" % (r["prev"], r["level"]) if r["changed"] else "понижение не требуется (уже сделано по этим причинам или уровень 1)")
    return 0


def cmd_set(args):
    lvl = args.level
    cfg, _ = risklevels.load_config()
    if cfg is None or str(lvl) not in cfg["levels"]:
        _err("уровень должен быть 1..5 (config/risk_levels.json)")
        return EXIT_REFUSED
    phrase = risklevels.confirm_phrase(lvl)
    cur = risklevels.current_level()
    if not _is_tty():
        _err("смену уровня запускает владелец в Terminal (нужен TTY); из чата и скриптов отказ")
        return EXIT_REFUSED
    if args.confirm is None:
        _out("Сейчас уровень %d. Для перехода к %d нужна фраза: --confirm \"%s\"" % (cur["level"], lvl, phrase))
        if lvl > cur["level"]:
            for w in risklevels.level_warnings(lvl):
                _out("  %s%s: %s" % ("[обязательно принять] " if w["required"] else "", w["id"], w["msg"]))
            req = [w["id"] for w in risklevels.level_warnings(lvl) if w["required"]]
            if req:
                _out("для уровней 4-5 примите каждое: %s и добавьте --ack \"%s\"" % (" ".join("--accept " + i for i in req), risklevels.ACK_PHRASE))
        return EXIT_REFUSED
    accepted = [x for a in (args.accept or []) for x in a.split(",") if x]
    res = risklevels.set_level(lvl, by="owner", accepted=accepted, phrase=args.confirm, ack=args.ack, why=args.why)
    for w in res["warnings"]:
        _out("  предупреждение %s: %s" % (w["id"], w["msg"]))
    if not res["ok"]:
        for e in res["errors"]:
            _err("отказ: " + e)
        return EXIT_REFUSED
    _out("уровень риска %d -> %d (%s); записано, аудит: state/control/risk_audit.jsonl" % (res["prev"], res["level"], cfg["levels"][str(lvl)]["name"]))
    if lvl > res["prev"]:
        _out("ПОВЫШЕНИЕ риска: действует для НОВЫХ входов; действующие планы сохраняют свой риск")
    return 0


def cmd_init_key(args):
    if not _is_tty():
        _err("ключ создаёт владелец в Terminal (нужен TTY)")
        return EXIT_REFUSED
    d = risklevels.key_dir()
    path = os.path.join(d, "control.json")
    if os.path.exists(path):
        _err("файл ключа уже есть: %s (не читаю и не трогаю)" % path)
        return EXIT_REFUSED
    os.makedirs(d, mode=0o700, exist_ok=True)
    body = json.dumps({"hmac_key": secrets.token_hex(32), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": "risk init-key"}, indent=1)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _out("создан %s (права 0600). Ключ не печатается. Windows: проверьте ACL (icacls) на файл." % path)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="tb risk")
    sp = ap.add_subparsers(dest="group")
    lv = sp.add_parser("level").add_subparsers(dest="cmd")
    s = lv.add_parser("show")
    s.add_argument("--json", action="store_true")
    lv.add_parser("list")
    c = lv.add_parser("check")
    c.add_argument("--apply", action="store_true")
    st = lv.add_parser("set")
    st.add_argument("level", type=int)
    st.add_argument("--confirm")
    st.add_argument("--accept", action="append")
    st.add_argument("--ack")
    st.add_argument("--why", default="manual")
    sp.add_parser("init-key")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if e.code in (0, None) else EXIT_REFUSED
    if a.group == "init-key":
        return cmd_init_key(a)
    if a.group == "level" and a.cmd:
        return {"show": cmd_show, "list": cmd_list, "check": cmd_check, "set": cmd_set}[a.cmd](a)
    _err("использование: risk level <show|list|check|set N> | risk init-key")
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
