"""`./tb gateway ...`: управление шлюзом владельцем (WP11-A). Не торгует и не читает ключей биржи.

  tools [--agent ID]                    каталог инструментов (или доступные агенту)
  agents                                реестр: id, роль, auth, режимы, число инструментов, ошибки записей (токены и хэши не печатаются)
  agent add ID [--role R] [--tools a,@read]   новый агент: auth=token, только paper, права по умолчанию @read (лимиты можно расширить только правкой config/agents.json)
  token new ID [--write-file]           новый токен: печатается ОДИН раз, в config/agents.json пишется только хэш; --write-file кладёт файл agents/ID.token (0600)
  call --agent ID TOOL [JSON]           один вызов шлюза in-process (отладка; пишется в аудит)
  audit [--tail 20]                     хвост state/gateway_audit.jsonl
  check                                 проверка реестра и каталога
"""
import argparse
import json
import sys

from tb.brain import common
from tb.gateway import audit, catalog, registry
from tb.gateway.rpc import Gateway


def _out(o):
    common.out(o)


def main(argv=None):
    common.setup_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="tb gateway")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    p.add_argument("cmd", choices=["tools", "agents", "agent", "token", "call", "audit", "check"])
    p.add_argument("rest", nargs="*")
    p.add_argument("--agent", default=None)
    p.add_argument("--role", default="agent")
    p.add_argument("--tools", default="@read")
    p.add_argument("--tail", type=int, default=20)
    p.add_argument("--write-file", action="store_true", dest="write_file")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    gw = Gateway(a.root)
    root = gw.root
    if a.cmd == "tools":
        ts = gw.tools_for(a.agent) if a.agent else list(catalog.TOOLS.values())
        _out({"ok": True, "tools": [{"name": t.name, "right": t.right, "write": t.write, "desc": t.desc} for t in ts]})
        return 0
    if a.cmd == "agents":
        rows = [{"id": x.id, "role": x.role, "auth": x.auth, "enabled": x.enabled, "has_token": bool(x.token_hash), "modes": x.limits["modes"],
                 "symbols": x.limits["symbols"], "tools": len(x.tools), "error": x.error} for x in gw.agents().values()]
        _out({"ok": True, "agents": rows})
        return 0
    if a.cmd == "agent":
        if len(a.rest) != 2 or a.rest[0] != "add":
            return common.fail("agent add ID [--role R] [--tools a,@read]")
        try:
            ag = registry.add_agent(a.rest[1], a.role, [x for x in a.tools.split(",") if x], catalog.TOOLS, root)
        except registry.RegistryError as e:
            return common.fail(str(e))
        _out({"ok": True, "agent": ag.id, "modes": ag.limits["modes"], "note": "режим только paper; токен: ./tb gateway token new %s" % ag.id})
        return 0
    if a.cmd == "token":
        if len(a.rest) != 2 or a.rest[0] != "new":
            return common.fail("token new ID")
        ag = gw.agent(a.rest[1])
        if ag is None or not ag.enabled:
            return common.fail("нет активного агента %s" % common.clean_text(a.rest[1], 20))
        tok = registry.new_token()
        registry.set_token_hash(ag.id, registry.hash_token(tok), root)
        where = None
        if a.write_file:
            where = registry.write_token_file(ag.id, tok)
        print("TOKEN (показан один раз, не сохраняется): %s" % tok)
        if where:
            print("записан в %s" % where, file=sys.stderr)
        return 0
    if a.cmd == "call":
        if not a.agent or not a.rest:
            return common.fail("call --agent ID TOOL [JSON]")
        try:
            args = json.loads(a.rest[1]) if len(a.rest) > 1 else {}
        except ValueError:
            return common.fail("аргументы не JSON")
        res = gw.call(a.agent, a.rest[0], args, via="cli")
        _out(res)
        return 0 if res.get("ok") is True else 1
    if a.cmd == "audit":
        _out({"ok": True, "audit": audit.tail(a.tail, root)})
        return 0
    bad = [{"id": x.id, "error": x.error} for x in gw.agents().values() if not x.enabled and x.error]
    _out({"ok": not bad, "tools": len(catalog.TOOLS), "agents": len(gw.agents()), "invalid": bad})
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
