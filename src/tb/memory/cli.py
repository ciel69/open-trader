"""`./tb memory <команда>`: similar | stats | add | show | sync | migrate | verify | review-queue | review | lesson | backtest | compare. Вывод JSON одной строкой (--text: кратко для человека).

Код возврата: 0 успех, 1 verify нашёл проблему, 2 ошибка использования/валидации. Сеть и биржа не используются; пишутся только state/cases.jsonl и state/lessons.jsonl.
"""
import argparse
import json
import sys
import time

from tb.memory import agg, backtest, cases, cfg as memcfg, lessons, migrate, privacy, query, review, similar


def _out(obj, text=False, lines=None):
    if text and lines is not None:
        print("\n".join(lines))
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _read_json(path):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("%s: %s" % (path, e))


def _fmt_similar(res):
    s = res["similar"]
    L = ["n=%s n_closed=%s mean_r=%s se=%s win=%s base_r=%s warn=%s (%s)" % (s["n"], s["n_closed"], s["mean_r"], s["se"], s["win"], s["base_r"], s["warn"], s["warn_text"])]
    for c in res["cases"]:
        L.append("  %s sim=%.2f %s %s conf=%s r=%s err=%s exit=%s" % (c["case_id"], c["sim"], c["side"], c["setup"], c["confirmed"], c["r"], ",".join(c["err"]), c["exit"]))
    for l in res["lessons"]:
        L.append("  урок %s [%s] conf=%s %s" % (l["id"], l["st"], l["conf"], l["txt"]))
    return L


def cmd_similar(a, root, cfg):
    feats = {}
    for kv in a.feature or []:
        k, _, v = kv.partition("=")
        feats[k.strip()] = query.parse_feature(k.strip(), v.strip())
    if a.brief_file:
        from tb.memory import features
        got = features.apply_brief({"sym": a.sym}, _read_json(a.brief_file))
        for k in ("fng", "dvol", "blackout", "regime", "btc_ret_1h_pct"):
            if got.get(k) is not None and k not in feats:
                feats[k] = got[k]
    now = time.time()
    q = query.build_query(a.sym, a.side, a.setup, feats, a.mode, cfg, now=now if a.now_features else None)
    res = query.find_similar(root, q, k=a.k, scope=a.scope, rules=a.rules, as_of=similar.ts_epoch(a.as_of) if a.as_of else None, by=a.by, now=now,
                             types=a.type.split(",") if a.type else None, cfg=cfg)
    _out(res, a.text, _fmt_similar(res))
    return 0


def cmd_stats(a, root, cfg):
    res = query.stats(root, by=a.by, mode=a.mode, since=a.since, min_n=a.min_n, cfg=cfg)
    t = res["total"]
    _out(res, a.text, ["n=%s n_closed=%s mean_r=%s se=%s win=%s warn=%s" % (t["n"], t["n_closed"], t["mean_r"], t["se"], t["win"], t["warn"])]
         + ["  %s n=%s mean_r=%s win=%s %s" % (r["value"], r["n"], r["mean_r"], r["win"], r["warn"]) for r in ((res.get("by") or {}).get(a.by) or [])])
    return 0


def cmd_add(a, root, cfg):
    rec = _read_json(a.file)
    now = time.time()
    rec.setdefault("ts", cases.iso(now))
    rec.setdefault("src", {})
    rec["src"].setdefault("actor", "owner")
    rec["src"].setdefault("role", "manual")
    rec["src"].setdefault("rules_ver", cfg.get("rules_ver"))
    rec["src"].setdefault("mode", "live")
    sym = ((rec.get("situation") or {}).get("sym")) or "XX"
    rec.setdefault("case_id", cases.case_id_for(rec["ts"], sym, json.dumps(rec, sort_keys=True, ensure_ascii=False)))
    from tb.memory import features
    if isinstance(rec.get("situation"), dict):
        rec["situation"] = features.finalize(dict((k, v) for k, v in rec["situation"].items() if k not in ("missing", "retro")), rec["situation"].get("retro") or [])
    row = cases.append_new(rec, root=root)
    _out({"ok": True, "case_id": row["case_id"], "seq": row["seq"]})
    return 0


def cmd_show(a, root, cfg):
    c = cases.get(a.case_id, root, rev=a.rev)
    if c is None:
        print("нет случая %s" % a.case_id, file=sys.stderr)
        return 2
    _out(c)
    return 0


def cmd_sync(a, root, cfg):
    now = time.time()
    rep = migrate.migrate(root, dry_run=a.dry_run, cfg=cfg, final_only=a.final_only)
    rep["lessons"] = lessons.refresh(root, cases.load(root), cfg, now, dry_run=a.dry_run)
    _out(rep)
    return 0


def cmd_backtest(a, root, cfg):
    rep = backtest.run(cases.load(root), cfg, mode=a.mode, split_ts=a.split_ts)
    w, sp = rep["walk_forward"], rep["split"]
    _out(rep, a.text, ["сделок=%s mode=%s" % (rep["n_trades"], rep["mode"]),
                       "walk-forward: n_eval=%s acc_mem=%s acc_base=%s brier_mem=%s brier_base=%s d_brier=%s±%s [%s]" % (
                           w["n_eval"], w.get("acc_mem"), w.get("acc_base"), w.get("brier_mem"), w.get("brier_base"), w.get("d_brier_mean"), w.get("d_brier_se"), w["warn"]),
                       "split: старых=%s новых=%s с советом=%s избегать=%s R_сохранено=%s [%s]" % (
                           sp["n_old"], sp["n_new"], sp["n_covered"], sp.get("n_avoid"), sp.get("r_saved_if_avoided"), sp["warn"])])
    return 0


def cmd_compare(a, root, cfg):
    rep = backtest.compare_periods(cases.load(root), cfg, a.a_from, a.a_to, a.b_from, a.b_to, mode=a.mode, split_ts=a.split_ts)
    _out(rep, a.text, ["A: n_closed=%s mean_r=%s | B: n_closed=%s mean_r=%s | разность=%s±%s [%s] (%s)" % (
        rep["a"]["n_closed"], rep["a"]["mean_r"], rep["b"]["n_closed"], rep["b"]["mean_r"], rep["diff_mean_r"], rep["diff_se"], rep["warn"], rep["note"])])
    return 0


def cmd_migrate(a, root, cfg):
    _out(migrate.migrate(root, dry_run=a.dry_run, with_signals=a.with_signals, cfg=cfg))
    return 0


def cmd_verify(a, root, cfg):
    c, l = cases.verify(root), lessons.chain.verify(lessons.lessons_path(root))
    res = {"ok": bool(c["ok"] and l["ok"]), "cases": c, "lessons": l}
    _out(res)
    return 0 if res["ok"] else 1


def cmd_review_queue(a, root, cfg):
    q = review.queue(cases.load(root), a.limit)
    _out({"n": len(q), "cases": [review.reviewer_input(c) for c in q]})
    return 0


def cmd_review(a, root, cfg):
    row = review.add_review(root, a.case_id, _read_json(a.file), cfg)
    _out({"ok": True, "case_id": a.case_id, "rev": row["rev"]})
    return 0


def cmd_lesson(a, root, cfg):
    now = time.time()
    act = a.action
    if act == "list":
        ls = sorted(lessons.read_all(root).values(), key=lambda l: l["lesson_id"])
        if a.status:
            ls = [l for l in ls if l["status"] == a.status]
        _out([{"id": l["lesson_id"], "st": l["status"], "n": (l.get("evidence") or {}).get("n_closed"), "conf": l["conf"], "conf_eff": lessons.conf_eff(l, now, cfg),
               "txt": l["txt"], "contradicts": l.get("contradicts")} for l in ls], a.text,
             ["%s [%s] n=%s conf_eff=%s %s" % (l["lesson_id"], l["status"], (l.get("evidence") or {}).get("n_closed"), lessons.conf_eff(l, now, cfg), l["txt"]) for l in ls])
    elif act == "show":
        l = lessons.read_all(root).get(a.id)
        if l is None:
            print("нет урока %s" % a.id, file=sys.stderr)
            return 2
        _out(l)
    elif act == "add":
        d = _read_json(a.file)
        l = lessons.create(root, d.get("cond"), d.get("claim"), "owner", cases.load(root), cfg, now, status="confirmed" if d.get("confirmed") is True else "hyp")
        _out({"ok": True, "lesson_id": l["lesson_id"], "status": l["status"]})
    elif act in ("confirm", "reject", "expire", "merge"):
        l = lessons.owner_set(root, a.id, act, cfg, now, into=a.into)
        _out({"ok": True, "lesson_id": l["lesson_id"], "status": l["status"]})
    elif act == "refresh":
        _out({"changes": lessons.refresh(root, cases.load(root), cfg, now, dry_run=a.dry_run)})
    elif act == "propose":
        cands = lessons.propose(root, cases.load(root), cfg, now)
        made = []
        if a.write:
            for c in cands:
                made.append(lessons.create(root, c["cond"], c["claim"], "code", cases.load(root), cfg, now)["lesson_id"])
        _out({"candidates": cands, "created": made})
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="tb memory", description="память случаев (WP15a)")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("similar")
    s.add_argument("--sym")
    s.add_argument("--side", choices=cases.SIDES)
    s.add_argument("--setup")
    s.add_argument("--feature", action="append", metavar="k=v")
    s.add_argument("--brief-file")
    s.add_argument("--k", type=int)
    s.add_argument("--scope", choices=("same_mode", "all"))
    s.add_argument("--rules", choices=("cur", "any"))
    s.add_argument("--mode", choices=cases.MODES)
    s.add_argument("--by", choices=agg.BY_KEYS)
    s.add_argument("--type", help="trade,skip,idea,signal")
    s.add_argument("--as-of", help="ISO UTC: только случаи до этого момента")
    s.add_argument("--now-features", action="store_true", help="добавить utc_session/hour/dow текущего времени")
    s.add_argument("--json", action="store_true")
    s.add_argument("--text", action="store_true")
    st = sub.add_parser("stats")
    st.add_argument("--by", choices=agg.BY_KEYS)
    st.add_argument("--mode", choices=cases.MODES)
    st.add_argument("--since")
    st.add_argument("--min-n", type=int, default=5)
    st.add_argument("--text", action="store_true")
    ad = sub.add_parser("add")
    ad.add_argument("--file", required=True)
    sh = sub.add_parser("show")
    sh.add_argument("case_id")
    sh.add_argument("--rev", type=int)
    for n in ("sync", "migrate"):
        m = sub.add_parser(n)
        m.add_argument("--dry-run", action="store_true")
        if n == "migrate":
            m.add_argument("--with-signals", action="store_true")
        else:
            m.add_argument("--final-only", action="store_true", help="как периодический писатель: идеи idea/watching не записываются")
    bt = sub.add_parser("backtest", help="ретро-проверка: помогал бы совет похожих случаев (старые -> новые)")
    cp = sub.add_parser("compare", help="сравнение двух периодов (до/после включения памяти), разрез по метке mem")
    for sp_ in (bt, cp):
        sp_.add_argument("--mode", choices=cases.MODES)
        sp_.add_argument("--split-ts", help="ISO UTC: граница старые/новые (по умолчанию медиана)")
        sp_.add_argument("--text", action="store_true")
    for k in ("a-from", "a-to", "b-from", "b-to"):
        cp.add_argument("--" + k, dest=k.replace("-", "_"))
    sub.add_parser("verify")
    rq = sub.add_parser("review-queue")
    rq.add_argument("--limit", type=int, default=20)
    rv = sub.add_parser("review")
    rv.add_argument("case_id")
    rv.add_argument("--file", required=True)
    ls = sub.add_parser("lesson")
    ls.add_argument("action", choices=("list", "show", "add", "confirm", "reject", "expire", "merge", "refresh", "propose"))
    ls.add_argument("id", nargs="?")
    ls.add_argument("--file")
    ls.add_argument("--into")
    ls.add_argument("--status", choices=lessons.STATUSES)
    ls.add_argument("--dry-run", action="store_true")
    ls.add_argument("--write", action="store_true")
    ls.add_argument("--text", action="store_true")
    return p


HANDLERS = {"similar": cmd_similar, "stats": cmd_stats, "add": cmd_add, "show": cmd_show, "sync": cmd_sync, "migrate": cmd_migrate, "verify": cmd_verify,
            "review-queue": cmd_review_queue, "review": cmd_review, "lesson": cmd_lesson, "backtest": cmd_backtest, "compare": cmd_compare}


def main(argv, root=None):
    p = build_parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not a.cmd:
        p.print_usage(sys.stderr)
        return 2
    try:
        cfg = memcfg.load(root)
        return HANDLERS[a.cmd](a, root, cfg)
    except (ValueError, cases.CaseError) as e:
        print("ошибка: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
