"""`./tb exp list|stats|show|compare`: статистика экспериментов и гипотез (04 п. 4.2). Владелец: WP7.

  exp list    [--mode paper|live|testnet|all] [--json]
  exp stats   [--by exp|variant|setup|hyp|rule|symbol|tf_session|agent|model|mem|blackout] [--mode ..] [--min-n 30] [--exp EXP] [--json]
  exp show    EXP [--mode ..] [--min-n 30] [--json]          эксперимент целиком: метаданные, варианты, попарные сравнения
  exp compare EXP A B [--mode ..] [--k N] [--min-n 30] [--json]   разность средних R (Уэлч) с доверительным интервалом

Вывод для владельца, без вердиктов: `*` = n < min-n (p-значения нет); K = число сравнений в запросе, порог Бонферрони alpha/K;
режимы (paper/live/testnet) в одной строке не смешиваются. Ничего не меняет и не пишет. Код 0 при успехе, 2 при ошибке использования.
Модуль не импортирует биржевой код (слои, tests/test_layering.py).
"""
import argparse
import json
import sys

from tb.core import expstats as X

EXIT_OK = 0
EXIT_USAGE = 2


def _f(v, fmt="%.2f", none="-"):
    return fmt % v if isinstance(v, (int, float)) else none


def _pf(r):
    return "inf" if r.get("pf_inf") else _f(r.get("pf"))


def _row_text(r):
    ravg = "%s±%s" % (_f(r["avg_r"], "%+.2f"), _f(r["se_r"])) if r["avg_r"] is not None else "-"
    p = "-" if r["p"] is None else "%.3f" % r["p"]
    return "%-26s %-7s %4d%s %5s %13s %5s %8s %6s %9s %5s%s" % (
        str(r["key"])[:26], r["mode"], r["n"], r["mark"] or " ", "%.0f%%" % (100 * r["win_rate"]) if r["win_rate"] is not None else "-", ravg,
        _pf(r), _f(r["fees"], "%.3f"), _f(r["fees_share"] * 100 if r["fees_share"] is not None else None, "%.0f%%"),
        "%s/%s" % (_f(r["mae_r_win"], "%.1f"), _f(r["mfe_r_win"], "%.1f")), p, "  смеш.ревизии" if r["mixed_rules"] else "")


HEAD = "%-26s %-7s %5s %5s %13s %5s %8s %6s %9s %5s" % ("группа", "режим", "n", "win", "avgR±se", "PF", "комис.", "доля", "MAE/MFE+", "p")


def render_stats(rows, by, min_n):
    if not rows:
        return "Нет закрытых сделок для разреза %s." % by
    L = ["Разрез: %s. Сделки закрытые, R после издержек; режимы не смешиваются." % by, HEAD]
    L.extend(_row_text(r) for r in rows)
    k, adj = rows[0]["k"], rows[0]["alpha_adj"]
    L.append("* n < %d: p-значения и выводов нет. K=%d сравнений, порог с поправкой Бонферрони alpha/K = %.4g (p ниже порога это не вердикт, "
             "а повод проверить на новой выборке)." % (min_n, k, adj))
    cap = [r for r in rows if r.get("mfe_capture") is not None or r.get("rule_close_share") is not None]
    if cap:
        L.append("Дополнительно: " + "; ".join("%s: по правилам %s, забрано MFE %s, ожидание %s USDT/сделку" % (
            str(r["key"])[:20], _f(r["rule_close_share"] * 100 if r["rule_close_share"] is not None else None, "%.0f%%"),
            _f(r["mfe_capture"] * 100 if r["mfe_capture"] is not None else None, "%.0f%%"), _f(r["expectancy_usdt"], "%+.3f")) for r in cap[:6]))
    return "\n".join(L)


def render_list(rows):
    if not rows:
        return "Экспериментов (exp-тег у закрытых сделок) нет."
    L = []
    for e in rows:
        vs = ", ".join("%s=%d" % kv for kv in sorted(e["variants"].items()))
        L.append("%s [%s] n=%d (набрано %d из %d по наименьшему варианту: %s) %s .. %s" % (
            e["exp"], "/".join(e["modes"]), e["n"], min(e["variants"].values()) if e["variants"] else 0, e["min_n"], vs, e["first"] or "?",
            e["last"] or "?"))
        L.append("    статус: %s%s" % (e["status"], "; гипотеза: %s" % e["hypothesis"] if e["hypothesis"] else ""))
    return "\n".join(L)


def render_compare(c):
    L = ["Эксперимент %s: %s (n=%d) против %s (n=%d), режимы: %s" % (c["exp"], c["a"], c["n_a"], c["b"], c["n_b"], ",".join(c["modes"]) or "-")]
    if c.get("diff") is not None:
        L.append("средний R: %s=%s, %s=%s, разность %+.3f" % (c["a"], _f(c.get("mean_a"), "%+.3f"), c["b"], _f(c.get("mean_b"), "%+.3f"), c["diff"]))
    if c.get("ci95"):
        L.append("95%% ДИ разности: [%+.3f; %+.3f]; с поправкой на K=%d: [%+.3f; %+.3f]; p=%s (Уэлч, df=%.1f)" % (
            c["ci95"][0], c["ci95"][1], c["k"], c["ci_adj"][0], c["ci_adj"][1], _f(c.get("p"), "%.4f"), c["df"]))
    if c.get("paired"):
        p = c["paired"]
        L.append("парное (общий сигнал): пар=%d, средняя разность %+.3f, p=%s" % (p["n_pairs"], p["mean_diff"], _f(p["p"], "%.4f")))
    L.extend(c["notes"])
    if c["significant"]:
        L.append("p ниже порога Бонферрони %.4g при n >= %d в обеих группах. Это не доказательство: перепроверить на новой выборке." % (c["alpha_adj"], c["min_n"]))
    return "\n".join(L)


def _parser():
    p = argparse.ArgumentParser(prog="tb exp", description=__doc__.split("\n")[0])
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--mode", choices=list(X.MODES), default="all")
        sp.add_argument("--min-n", type=int, default=X.MIN_N)
        sp.add_argument("--json", action="store_true")

    common(sub.add_parser("list"))
    s = sub.add_parser("stats")
    common(s)
    s.add_argument("--by", choices=list(X.GROUP_BY), default="exp")
    s.add_argument("--exp", default=None, help="только сделки одного эксперимента")
    sh = sub.add_parser("show")
    common(sh)
    sh.add_argument("exp")
    c = sub.add_parser("compare")
    common(c)
    c.add_argument("exp")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--k", type=int, default=1, help="число сравнений, заявленных заранее (для порога Бонферрони)")
    return p


def main(argv=None, root=None):
    try:
        for s in (sys.stdout, sys.stderr):
            s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = _parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if not a.cmd:
        p.print_usage(sys.stderr)
        return EXIT_USAGE
    root = root or a.root
    if a.min_n < 1:
        print("--min-n >= 1", file=sys.stderr)
        return EXIT_USAGE
    if a.cmd == "compare" and a.k < 1:
        print("--k >= 1", file=sys.stderr)
        return EXIT_USAGE
    trades = X.load_trades(a.mode, root, with_rules=(a.cmd == "stats" and a.by == "rule"))
    meta = X.load_meta(root)
    if a.cmd == "list":
        rows = X.list_experiments(trades, meta, a.min_n)
        print(json.dumps({"ok": True, "experiments": rows}, ensure_ascii=False) if a.json else render_list(rows))
    elif a.cmd == "stats":
        sel = [t for t in trades if not a.exp or t["exp"] == a.exp]
        rows = X.group_stats(sel, a.by, a.min_n)
        print(json.dumps({"ok": True, "by": a.by, "mode": a.mode, "min_n": a.min_n, "k": len(rows), "alpha_adj": X.bonferroni(len(rows)),
                          "groups": rows}, ensure_ascii=False) if a.json else render_stats(rows, a.by, a.min_n))
    elif a.cmd == "show":
        sel = [t for t in trades if t["exp"] == a.exp]
        rows = X.group_stats(sel, "variant", a.min_n)
        vs = sorted(set(t["var"] for t in sel if t["var"]))
        pairs = [(x, y) for i, x in enumerate(vs) for y in vs[i + 1:]]
        cmps = [X.compare(sel, a.exp, x, y, a.min_n, k=max(1, len(pairs))) for x, y in pairs]
        m = meta.get(a.exp) or {}
        if a.json:
            print(json.dumps({"ok": True, "exp": a.exp, "meta": m, "variants": rows, "comparisons": cmps}, ensure_ascii=False))
        else:
            L = ["Эксперимент %s" % a.exp]
            for k in ("hypothesis", "prereg", "started", "min_n"):
                if m.get(k) is not None:
                    L.append("  %s: %s" % (k, m[k]))
            L.append(render_stats(rows, "variant", a.min_n))
            L.extend(render_compare(c) for c in cmps)
            print("\n".join(L))
    else:
        c = X.compare([t for t in trades], a.exp, a.a.lower(), a.b.lower(), a.min_n, a.k)
        print(json.dumps(dict(c, ok=True), ensure_ascii=False) if a.json else render_compare(c))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
