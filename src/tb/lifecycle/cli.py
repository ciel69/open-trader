"""`./tb day ...` (WP14): только чтение. status | list | report [ID] | checklist | preflight [--mode M]. Управление днём (старт/стоп/перенос/ack) идёт через ctl (дашборд или `./tb control cmd`)."""
import argparse
import json
import sys
import time

from tb import paths
from tb.lifecycle import dashapi, days


def _out(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
        return True
    return False


def cmd_status(a):
    st = dashapi.status(a.root, time.time())
    if _out(st, a.json):
        return 0
    c = st.get("current") or st.get("last")
    if not c:
        print("дней ещё не было")
        return 0
    print("%s: %s, режим %s, старт %s%s" % (c["day_id"], c["state"], c["mode"], c["started"], (", остановка " + c["stopped"]) if c.get("stopped") else ""))
    for s in (st.get("current") or {}).get("steps") or []:
        print("  %-3s %-5s %s %s" % (s["name"], {True: "ok", False: "FAIL", None: "..."}[s["ok"]], s.get("code") or "", json.dumps(s.get("detail"), ensure_ascii=False)[:100] if s.get("detail") else ""))
    for q in (st.get("current") or {}).get("questions") or []:
        print("  вопрос %s: %s [%s] ответ: %s" % (q["id"], q["text"], "/".join(q["options"] or []), q.get("answer")))
    if st.get("banner"):
        print("баннер [%s]: %s" % (st["banner"].get("level"), st["banner"].get("text")))
    return 0


def cmd_list(a):
    r = dashapi.listing(a.root)
    if _out(r, a.json):
        return 0
    for d in r["days"]:
        print("%s %-8s %-8s net %s оценка %s" % (d["day_id"], d["state"], d["mode"], d.get("net"), d.get("verdict") or "-"))
    return 0


def cmd_report(a):
    day = a.id or ((days.current(a.root) or days.last_day(a.root)) or {}).get("day_id")
    r = dashapi.report(a.root, day) if day else None
    if r is None:
        print("отчёта нет", file=sys.stderr)
        return 1
    print(json.dumps(r["report"], ensure_ascii=False, indent=1) if a.json else r["md"])
    return 0


def cmd_checklist(a):
    """Что сделала бы остановка дня сейчас (сухой прогон, ничего не отправляется)."""
    from tb.lifecycle import audit, runtime
    from tb.lifecycle import checklist as CL
    cur = days.current(a.root) or {"day_id": "D-00000000-0"}
    ctx = runtime.Ctx(a.root, time.time())
    _inp, rows = audit.rows_for(ctx, cur)
    rows = CL.public_rows(rows)
    if _out(rows, a.json):
        return 0
    for r in rows:
        print("%-28s %-4s %-7s %s %s" % (r["item"], r["rule"], r["decision"], "(агент может перенести)" if r.get("candidate") else "", ",".join(r["codes"])))
    return 0


def cmd_preflight(a):
    from tb.lifecycle import preflight, runtime
    ctx = runtime.Ctx(a.root, time.time())
    v = preflight.run(ctx, {"mode": a.mode, "session": "intraday", "minutes": 60, "allow_amber": True})
    out = [{k: c[k] for k in ("id", "name", "sev", "ok", "msg")} for c in v["checks"]]
    if _out(out, a.json):
        return 0
    for c in out:
        print("%-4s %-4s %s %-42s %s" % (c["id"], c["sev"], "ok  " if c["ok"] else "FAIL", c["name"], "" if c["ok"] else c["msg"]))
    return 0 if v["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb day", description="жизненный цикл дня (только чтение)")
    ap.add_argument("--root", default=None)
    ap.add_argument("--json", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    sp.add_parser("list")
    r = sp.add_parser("report")
    r.add_argument("id", nargs="?")
    sp.add_parser("checklist")
    p = sp.add_parser("preflight")
    p.add_argument("--mode", default="paper", choices=("observe", "paper", "testnet", "live"))
    a = ap.parse_args(argv)
    a.root = a.root or paths.root()
    return {"status": cmd_status, "list": cmd_list, "report": cmd_report, "checklist": cmd_checklist, "preflight": cmd_preflight}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
