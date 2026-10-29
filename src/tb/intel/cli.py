"""`./tb intel status|fetch|show|sources|calendar|verify`. Владелец: WP13.

status    состояние сервиса, свежесть и ошибки источников (только числа и коды);
fetch     ОДИН цикл загрузки прямо сейчас (живые запросы: запускает владелец; `--source ID`, `--force`);
show      facts.json | blackouts | items (заголовки показываются только в интерактивном терминале владельца, не в канале агента);
sources   реестр источников (без сети);
calendar  события на 30 дней с пометкой verified (v:false = кандидат, ПРОВЕРИТЬ ПО ОФИЦИАЛЬНОМУ КАЛЕНДАРЮ), окна blackout; `calendar import` = fetch cal_*;
verify    проверка живых источников (tb.intel.verify).
"""
import argparse
import json
import sys
import time

from tb import paths
from tb.intel import calendar as ical
from tb.intel import fetch as ifetch
from tb.intel import sources as src_mod
from tb.intel import store


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + "Z" if isinstance(ts, (int, float)) else "-"


def cmd_status(a, out):
    root, now = paths.root(), time.time()
    cfg = store.load_cfg(root)
    syms = store.symbols_now(root, cfg)
    srcs = src_mod.expand(cfg, syms)
    from tb.platform import services as S
    age, hb = S.read_hb("intel", root, now)
    print("сервис intel: heartbeat %s" % ("нет (сервис не запущен)" if age is None else "%.0f с назад, pid %s" % (age, hb.get("pid"))), file=out)
    facts = store.read_facts(root)
    print("facts.json: %s" % ("нет" if not facts else "возраст %d с, degraded: %s" % (now - facts.get("ts", 0), ",".join(facts.get("degraded") or []) or "-")), file=out)
    print("символы: %s" % ",".join(syms), file=out)
    print("%-20s %-4s %8s %5s  %-22s %s" % ("источник", "ok", "возраст_с", "fails", "последняя ошибка", "verified"), file=out)
    for s in srcs:
        c = ifetch.read_cache(s["id"], root)
        fetched = c.get("fetched")
        age_s = "%d" % (now - fetched) if isinstance(fetched, (int, float)) else "-"
        print("%-20s %-4s %8s %5s  %-22s %s%s" % (s["id"], "да" if c.get("ok") else ("нет" if c else "-"), age_s, c.get("fails", 0), c.get("last_error") or "-",
                                                  "да" if s["verified"] else "нет", "" if s["enabled"] else " (выкл.)"), file=out)
    from tb.intel import items as iitems
    print("заголовки: %s" % json.dumps(iitems.ItemStore(root).stats(now)), file=out)
    return 0


def cmd_fetch(argv, out):
    ap = argparse.ArgumentParser(prog="tb intel fetch")
    ap.add_argument("--source", action="append")
    ap.add_argument("--force", action="store_true", help="игнорировать расписание и backoff")
    a = ap.parse_args(argv)
    from tb.intel import service
    root = paths.root()
    svc = service.Service(root, inline=True, log=lambda m: print(m, file=out))
    now = time.time()
    svc.refresh_symbols(now, force=True)
    upd = svc.fetch_due(now, force=a.force or bool(a.source), only=a.source)
    svc.calendar_step(now, force=True)
    svc.publish(now, force=True)
    print("загружено: %s; ошибок: %d; отложено: %d; новых заголовков: %d" % (",".join(upd) or "-", svc.stats["failed"], svc.stats["deferred"], svc.stats["items_new"]), file=out)
    svc.close()
    return 0


def cmd_show(argv, out):
    ap = argparse.ArgumentParser(prog="tb intel show")
    ap.add_argument("what", nargs="?", default="facts", choices=("facts", "blackouts", "items"))
    ap.add_argument("-n", type=int, default=20)
    a = ap.parse_args(argv)
    root = paths.root()
    if a.what == "items":
        if not (hasattr(out, "isatty") and out.isatty()):
            print("заголовки новостей показываются только владельцу в интерактивном терминале (текст третьих лиц не выводится в канал агента)", file=sys.stderr)
            return 2
        from tb.intel import items as iitems
        for r in iitems.ItemStore(root).load(limit=a.n):
            print("%s  %-14s %s %s%s" % (_fmt_ts(r.get("ts")), r.get("src"), r["title"], "[ПОДОЗРЕНИЕ] " if "inj_suspect" in (r.get("flags") or []) else "",
                                          "(+%s)" % ",".join(r["seen"]) if r.get("seen") else ""), file=out)
        return 0
    if a.what == "blackouts":
        d = None
        try:
            with open(ical.blackouts_path(root), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            pass
        print(json.dumps(d, ensure_ascii=False, indent=1) if d else "нет runtime/intel/blackouts.json (сервис не запускался)", file=out)
        return 0
    d = store.read_facts(root)
    print(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) if d else "нет runtime/intel/facts.json (сервис не запускался: ./tb intel fetch)", file=out)
    return 0


def cmd_sources(out):
    cfg = store.load_cfg(paths.root())
    print("%-18s %-6s %-7s %8s %-9s %s" % ("id", "вид", "формат", "период_с", "verified", "URL"), file=out)
    for s in src_mod.expand(cfg, cfg.get("symbols")):
        print("%-18s %-6s %-7s %8s %-9s %s%s" % (s["id"], s["kind"], s["format"], s["interval_s"], "да" if s["verified"] else "НЕТ", s["url"], "" if s["enabled"] else "  [выкл.]"), file=out)
    for s in src_mod.LOCAL_SOURCES:
        print("%-18s %-6s %-7s %8s %-9s %s" % (s["id"], s["kind"], "-", "-", "-", s["note"]), file=out)
    print("verified=НЕТ: URL и формат не проверены живым запросом; проверка: ./tb intel verify [--write]", file=out)
    return 0


def cmd_calendar(argv, out):
    if argv and argv[0] == "import":
        return cmd_fetch(["--source", "cal_bls_ics", "--source", "cal_fomc_html", "--force"], out)
    root, now = paths.root(), time.time()
    cfg = store.load_cfg(root)
    ccfg = cfg.get("calendar") or {}
    srcs = src_mod.expand(cfg, [])
    imp = {s["id"]: bool(s.get("verified")) for s in srcs if s["id"].startswith("cal_")}
    ev = ical.all_events(root, ccfg, imp)
    print("События (v=нет: КАНДИДАТ из памяти или непроверенный импорт; ПРОВЕРИТЬ ПО ОФИЦИАЛЬНОМУ КАЛЕНДАРЮ ФРС/BLS):", file=out)
    print("%-18s %-5s %-18s %6s %6s %-5s %s" % ("id", "тип", "время UTC", "важн.", "через_ч", "v", "источник"), file=out)
    for e in ev:
        if e["ts"] >= now - 6 * 3600 and e["ts"] <= now + 30 * 86400:
            print("%-18s %-5s %-18s %6s %6.1f %-5s %s" % (e["id"], e["type"], _fmt_ts(e["ts"]), e["impact"], (e["ts"] - now) / 3600.0, "да" if e["v"] else "НЕТ", e["src"]), file=out)
    doc = ical.build_blackouts(root, ccfg, ev, now)
    print("\nОкна blackout (объединение watch_config + календарь):", file=out)
    for w in doc["windows"]:
        print("  %s .. %s  %-6s %s v=%s" % (w["from"], w["to"], w["name"], w["src"], "да" if w.get("v") else "НЕТ"), file=out)
    m = ical.us_market(cfg, now)
    print("\nРынок США: %s, следующее открытие %s, праздник сегодня: %s" % ("открыт" if m["open"] else "закрыт", _fmt_ts(m["next_open_ts"]), m["holiday"] or "нет"), file=out)
    return 0


def main(argv=None, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__, file=out)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "status":
            return cmd_status(None, out)
        if cmd == "fetch":
            return cmd_fetch(rest, out)
        if cmd == "show":
            return cmd_show(rest, out)
        if cmd == "sources":
            return cmd_sources(out)
        if cmd == "calendar":
            return cmd_calendar(rest, out)
        if cmd == "verify":
            from tb.intel import verify
            return verify.main(rest, out=out)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    print("intel: неизвестная команда %s (status|fetch|show|sources|calendar|verify)" % cmd, file=sys.stderr)
    return 2
