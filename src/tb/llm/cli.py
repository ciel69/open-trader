"""`./tb llm ...` (WP11-C1). Живых запросов без явного --live не делает; ключи не печатает (только наличие и маска …xxxx).

  providers                         реестр: провайдеры, модели, флаги, цены, наличие ключа, ошибки конфигурации
  check [--provider ID] [--model K] проверка конфигурации и наличия ключей БЕЗ сети
  check --live [--provider ID] [--model K] [--no-ping]
                                    пробный запрос с ключом владельца: GET списка моделей (есть ли имя из конфига) и один минимальный запрос
  usage [--days N] [--by profile|provider|model|role|team|agent]   расход из state/llm_calls.jsonl и состояние потолков
  presets                           шаблоны провайдеров (openai, gemini, openrouter, anthropic, deepseek, groq, mistral, local, lmstudio): ВСЕ не проверены живым ключом
  provider add ID [--preset P]      добавить провайдера в config/local/providers.json (выключен); update ID --set base_url=... | remove ID | enable ID | disable ID
  model set ID МОДЕЛЬ --name ИМЯ_У_ПРОВАЙДЕРА [--price-in X --price-out Y --tools --json-mode] | model remove ID МОДЕЛЬ
  key set ID                        ввод ключа провайдера (скрытый ввод getpass, нужен TTY; значение не печатается и не логируется), key status ID | key clear ID
Каждое изменение пишется в журнал аудита (без значений). Торговые ключи бирж здесь не принимаются никогда (только ./tb setup).
"""
import argparse
import json
import sys
import time

from tb.llm import adapters, budget as budget_mod, keys, providers, usage as usage_mod
from tb.llm.transport import Transport
from tb.llm.types import LLMError


def _out(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def _setup():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _provider_row(p):
    has, mask = keys.present(p.key_file, p.key_env)
    needs = bool(p.key_file or p.key_env)
    return {"id": p.id, "adapter": p.adapter, "enabled": p.enabled, "error": p.error, "base_url": p.base_url,
            "key": {"needed": needs, "present": has if needs else None, "mask": mask, "file": p.key_file},
            "limits": p.limits,
            "models": {k: {"name": m.name, "tools": m.tools, "strict": m.strict, "json_mode": m.json_mode, "thinking": m.thinking, "cache": m.cache,
                           "price": m.price, "tags": m.tags} for k, m in p.models.items()}}


def _select(reg, a):
    out = []
    for p in reg.providers.values():
        if a.provider and p.id != a.provider:
            continue
        for k in p.models:
            if a.model and k != a.model:
                continue
            out.append((p, p.models[k]))
    return out


def cmd_check(reg, a):
    sel = _select(reg, a)
    if not sel:
        _out({"ok": False, "error": "нет подходящих провайдеров или моделей"})
        return 2
    rows, ok = [], not reg.errors
    for p, m in sel:
        row = {"provider": p.id, "model": m.key, "enabled": p.enabled, "config_error": p.error}
        needs = bool(p.key_file or p.key_env)
        has, mask = keys.present(p.key_file, p.key_env)
        row["key"] = {"needed": needs, "present": has if needs else None, "mask": mask}
        if p.error:
            ok = False
        elif needs and not has:
            row["note"] = "ключа нет: положите файл %s (строка %s=...) в каталог ключей %s" % (p.key_file, p.key_env, keys.keys_dir_hint())
            if a.live:
                row["live"] = {"skipped": "нет ключа"}
        elif a.live:
            if not p.enabled:
                row["live"] = {"skipped": "провайдер отключён (enabled=false)"}
            else:
                tr = Transport()
                try:
                    row["live"] = adapters.make(p, m, transport=tr).probe(ping=not a.no_ping)
                except LLMError as e:
                    row["live"] = {"error": e.code, "msg": e.msg}
                    ok = False
                finally:
                    tr.close()
        rows.append(row)
    _out({"ok": ok, "live": bool(a.live), "config_errors": reg.errors, "checked": rows,
          "note": None if a.live else "сеть не использовалась; живая проверка: ./tb llm check --live (нужен ключ владельца)"})
    return 0 if ok else 1


def cmd_usage(reg, a):
    now = time.time()
    rows = usage_mod.read_rows(None, since=now - a.days * 86400.0)
    today = usage_mod.read_rows(None, since=budget_mod.window_start(now, 86400))
    bud = budget_mod.from_registry(reg)
    bud.emit = False          # просмотр расхода не публикует событий
    _out({"ok": True, "days": a.days, "by": a.by, "total": usage_mod.window_totals(rows), "today": usage_mod.window_totals(today),
          "groups": usage_mod.aggregate(rows, a.by), "budget": bud.status(None),
          "providers": {pid: {"state": bud.status(pid)["state"]} for pid in reg.providers}})
    return 0


def _audit(action, args, ok, code="OK"):
    """Запись в журнал аудита (хеш аргументов без значений; формат общий с ctl)."""
    try:
        from tb import paths
        from tb.core import cfgaudit
        cfgaudit.append(paths.root(), action, args, ok, code)
    except Exception:
        pass


def _res_out(action, args, r):
    _audit(action, args, r["ok"], r["code"])
    if r["ok"]:
        try:
            from tb.brain import profiles
            from tb import paths
            profiles.signal_reload(paths.root(), "cli")
        except Exception:
            pass
    _out(r)
    return 0 if r["ok"] else 1


def _parse_set(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def cmd_manage(argv):
    """provider|model|key|presets: управление реестром без правки кода."""
    from tb import paths
    from tb.llm import keystore, provstore
    root = paths.root()
    p = argparse.ArgumentParser(prog="tb llm")
    sp = p.add_subparsers(dest="grp")
    sp.add_parser("presets")
    pv = sp.add_parser("provider")
    pv.add_argument("op", choices=["add", "update", "remove", "enable", "disable"])
    pv.add_argument("id")
    pv.add_argument("--preset", default=None)
    pv.add_argument("--set", action="append", default=[], dest="sets", help="поле=значение (JSON), повторяется")
    md = sp.add_parser("model")
    md.add_argument("op", choices=["set", "remove"])
    md.add_argument("id")
    md.add_argument("model")
    md.add_argument("--name", default=None)
    md.add_argument("--price-in", type=float, default=None, dest="price_in")
    md.add_argument("--price-out", type=float, default=None, dest="price_out")
    md.add_argument("--tools", action="store_true")
    md.add_argument("--json-mode", action="store_true", dest="json_mode")
    md.add_argument("--set", action="append", default=[], dest="sets")
    ky = sp.add_parser("key")
    ky.add_argument("op", choices=["set", "status", "clear"])
    ky.add_argument("id")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.grp == "presets":
        _out({"ok": True, "presets": provstore.preset_rows(root), "note": "все шаблоны НЕ ПРОВЕРЕНЫ живым ключом: ./tb llm check --provider ID --live"})
        return 0
    if a.grp == "provider":
        if a.op == "add":
            return _res_out("llm.provider", {"op": "add", "id": a.id}, provstore.add(root, a.id, preset=a.preset, spec=_parse_set(a.sets) or None))
        if a.op == "update":
            return _res_out("llm.provider", {"op": "update", "id": a.id}, provstore.update(root, a.id, _parse_set(a.sets)))
        if a.op == "remove":
            return _res_out("llm.provider", {"op": "remove", "id": a.id}, provstore.remove(root, a.id))
        if a.op in ("enable", "disable"):
            return _res_out("llm.provider", {"op": a.op, "id": a.id}, provstore.set_enabled(root, a.id, a.op == "enable"))
    if a.grp == "model":
        if a.op == "remove":
            return _res_out("llm.provider", {"op": "model_remove", "id": a.id}, provstore.model_remove(root, a.id, a.model))
        spec = _parse_set(a.sets)
        if a.name:
            spec["name"] = a.name
        if a.price_in is not None or a.price_out is not None:
            spec["price"] = {"in": a.price_in or 0.0, "out": a.price_out or 0.0}
        if a.tools:
            spec["tools"] = True
        if a.json_mode:
            spec["json_mode"] = True
        return _res_out("llm.provider", {"op": "model_set", "id": a.id}, provstore.model_set(root, a.id, a.model, spec))
    if a.grp == "key":
        try:
            reg = providers.load(root)
        except providers.ProviderError as e:
            _out({"ok": False, "error": str(e)})
            return 1
        prov = reg.providers.get(a.id)
        if prov is None or not (prov.key_file and prov.key_env):
            _out({"ok": False, "error": "провайдер неизвестен или ему не нужен ключ"})
            return 1
        if a.op == "status":
            _out({"ok": True, "provider": a.id, "cred": keystore.status(prov.key_file, prov.key_env)})
            return 0
        try:
            if a.op == "clear":
                st = keystore.clear_key(prov.key_file, prov.key_env)
            else:
                import getpass
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    print("ключ вводится только в терминале (скрытый ввод): запустите команду в Terminal", file=sys.stderr)
                    return 2
                val = getpass.getpass("ключ провайдера %s (ввод скрыт): " % a.id).strip()
                if not val:
                    print("пусто: ничего не записано", file=sys.stderr)
                    return 2
                st = keystore.set_key(prov.key_file, prov.key_env, val)
                val = None
        except keystore.SecretError as e:
            _audit("llm.key.set", {"provider": a.id}, False, "E_KEY")
            _out({"ok": False, "error": str(e)})
            return 1
        _audit("llm.key.clear" if a.op == "clear" else "llm.key.set", {"provider": a.id}, True)
        _out({"ok": True, "provider": a.id, "cred": st})
        return 0
    p.print_usage(sys.stderr)
    return 2


def main(argv=None):
    _setup()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("presets", "provider", "model", "key"):
        return cmd_manage(argv)
    p = argparse.ArgumentParser(prog="tb llm")
    p.add_argument("cmd", choices=["providers", "check", "usage"])
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--live", action="store_true")
    p.add_argument("--no-ping", action="store_true", dest="no_ping")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--by", default="profile", choices=["profile", "provider", "model", "role", "team", "agent"])
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        reg = providers.load()
    except providers.ProviderError as e:
        _out({"ok": False, "error": str(e)})
        return 1
    if a.cmd == "providers":
        _out({"ok": not reg.errors, "errors": reg.errors, "providers": [_provider_row(x) for x in reg.providers.values()]})
        return 0 if not reg.errors else 1
    if a.cmd == "check":
        return cmd_check(reg, a)
    return cmd_usage(reg, a)


if __name__ == "__main__":
    sys.exit(main())
