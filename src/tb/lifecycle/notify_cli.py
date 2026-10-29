"""`./tb notify ...`: уведомления ОС без правки конфига (WP-N, docs/NOTIFICATIONS.md). Настройки: `state/control/notify.json`.

  status                          источник настроек, мастер, категории, тихие часы, сколько показано/подавлено
  on | off                        мастер-выключатель
  set КАТЕГОРИЯ on|off            категория (day_start, day_report, emergency, service_down, live_gate, risk_level_changed, brain_swap, position_closed, day_close_failed, macro_blackout_soon)
  set quiet HH:MM-HH:MM | off     тихие часы (местное время; диапазон может переходить через полночь), off = без тихих часов
  set bypass КАТЕГОРИЯ on|off     показывать категорию и в тихие часы
  set limit per_hour|same_kind_min N   антиспам
  test                            ОДНО тестовое уведомление ОС (только по этой команде)
Каждое изменение пишется в журнал аудита ctl (src=cli).
"""
import json
import sys
import time

from tb import paths
from tb.control import audit, protocol
from tb.lifecycle import notifycfg


def _audit(root, patch, changes, now):
    audit.append(root, protocol.new_id(now), "cli", None, "notify.set", protocol.args_digest("notify.set", patch), "ok", "OK", now=now, note="; ".join(changes)[:160])


def _apply(root, patch):
    now = time.time()
    ok, msg, s, ch = notifycfg.set_patch(root, patch, now, "cli")
    if not ok:
        print("ошибка: " + msg, file=sys.stderr)
        return 2
    _audit(root, patch, ch, now)
    print("сохранено: " + ("; ".join(ch) if ch else "без изменений"))
    return 0


def _parse_quiet(v):
    a, _, b = v.partition("-")
    return {"enabled": True, "from": a.strip(), "to": b.strip()}


def cmd_status(root, as_json):
    v = notifycfg.view(root, time.time())
    if as_json:
        print(json.dumps(v, ensure_ascii=False, indent=1))
        return 0
    s = v["settings"]
    print("источник: %s; мастер: %s%s" % ("state/control/notify.json" if v["source"] == "file" else "config/lifecycle.json (файла настроек нет)", "ВКЛ" if s["master"] else "выкл",
                                        "" if v["engine"] else " (работает только итог дня при notify.os=true)"))
    for c in v["categories"]:
        print("  %-20s %-4s %s%s" % (c["id"], "вкл" if c["on"] else "выкл", c["label"], " [обходит тихие часы]" if c["bypass"] else ""))
    q = s["quiet"]
    print("тихие часы: %s %s-%s (%s)%s; накоплено: %d" % ("вкл" if q["enabled"] else "выкл", q["from"], q["to"], "местное" if q["tz"] == "local" else "UTC",
                                                         ", сейчас идут" if v["quiet_now"] else "", v["held_n"]))
    print("антиспам: %d в час на категорию, один вид не чаще раза в %d мин" % (s["limits"]["per_hour"], s["limits"]["same_kind_min"]))
    st = v["stats"]
    print("показано %d, объединено повторов %d, отсечено лимитом %d, сбоев показа %d" % (st.get("sent", 0), st.get("merged", 0), st.get("limited", 0), st.get("failed", 0)))
    if v["warn"]:
        print("предупреждение: " + v["warn"])
    print(v["hint"])
    return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    root = paths.root()
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    c, rest = argv[0], argv[1:]
    if c == "status":
        return cmd_status(root, as_json)
    if c in ("on", "off"):
        return _apply(root, {"master": c == "on"})
    if c == "test":
        from tb.lifecycle import api
        r = api.notify_test(root, {}, time.time())
        print("уведомление отправлено ОС (%s). Если не появилось: проверьте системные настройки уведомлений (macOS: «Редактор скриптов», Windows: PowerShell)." % r["data"].get("how")
              if r["ok"] else "не показано: %s" % r["msg"])
        return 0 if r["ok"] else 1
    if c == "set" and rest:
        if rest[0] == "quiet" and len(rest) == 2:
            return _apply(root, {"quiet": {"enabled": False} if rest[1] == "off" else _parse_quiet(rest[1])})
        if rest[0] == "bypass" and len(rest) == 3 and rest[2] in ("on", "off"):
            cur = set(notifycfg.load(root)["settings"]["quiet"]["bypass"])
            (cur.add if rest[2] == "on" else cur.discard)(rest[1])
            return _apply(root, {"quiet": {"bypass": sorted(cur)}})
        if rest[0] == "limit" and len(rest) == 3 and rest[2].isdigit():
            return _apply(root, {"limits": {rest[1]: int(rest[2])}})
        if len(rest) == 2 and rest[1] in ("on", "off"):
            return _apply(root, {"categories": {rest[0]: rest[1] == "on"}})
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
