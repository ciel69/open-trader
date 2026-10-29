"""`./tb intel verify [--source ID] [--write]`: проверка ЖИВЫХ источников владельцем (в Terminal.app, где сеть открыта). Владелец: WP13.

По одному GET на источник (ничего не пишет в кэш, items и facts): доступность (HTTP), формат, поля, которые ждёт парсер; печатает таблицу,
НЕ печатает тексты заголовков. `--write` дописывает раздел с результатами в docs/spec_v2/WP13_sources.md. Код выхода 0, если все проверенные источники ОК, иначе 1.
Для источников с символами/валютами проверяется один представитель (BTC). Тесты подменяют транспорт (сети нет).
Источник, формат которого изменился, отключается флагом в config/intel.json (`sources.<id>.enabled=false`), а не падением. Подтвердив источник, владелец
ставит там же `sources.<id>.verified=true`.
"""
import argparse
import os
import sys
import time

from tb import paths
from tb.intel import fetch as ifetch
from tb.intel import sources as src_mod
from tb.intel import store

DOC = os.path.join("docs", "spec_v2", "WP13_sources.md")


def pick(cfg, only=None):
    """-> список источников для проверки (по одному представителю на семейство, если не задан --source)."""
    allv = src_mod.expand(cfg, ["BTC"])
    if only:
        want = set(only)
        sel = [s for s in allv if s["id"] in want or s["base"] in want]
        if not sel:                                                   # id вида oi_SOL: расширяем под запрошенный символ
            sel = [s for s in src_mod.expand(cfg, [x.split("_", 1)[1] for x in want if "_" in x and x.split("_", 1)[1].isupper()] or ["BTC"]) if s["id"] in want]
        return sel
    return allv


def check_one(fetcher, src, now, sleep):
    """-> dict результата: id, url, http, ms, kb, ok, detail. now: функция текущего времени (с учётом проведённых пауз)."""
    ctx = {"symbols": [src["symbol"]] if src.get("symbol") else ["BTC", "ETH", "SOL"], "symbol": src.get("symbol")}
    o = None
    for _ in range(4):
        o = fetcher.fetch(src, ctx, now=now(), force=True, persist=False)
        if o.deferred and o.error in ("gap", "budget"):
            sleep(fetcher.min_gap + 0.1)
            continue
        break
    res = {"id": src["id"], "url": src["url"], "http": o.status, "ms": o.ms, "kb": round(o.nbytes / 1024.0, 1), "ok": False, "detail": ""}
    if not o.ok:
        res["detail"] = "ОШИБКА %s" % (o.error or "?")
        return res
    full = dict(o.data)
    full["items"] = o.items
    miss = src_mod.missing_fields(src, full)
    extra = []
    if o.items:
        extra.append("элементов %d, с датой %d" % (len(o.items), sum(1 for i in o.items if i.get("ts"))))
    if "ann" in o.data:
        extra.append("объявлений с символами %d" % sum(1 for a in o.data["ann"] if a.get("syms")))
    if "events" in o.data:
        extra.append("событий %d" % len(o.data["events"]))
    if "symbols" in o.data:
        extra.append("символов %d" % len(o.data["symbols"]))
    if o.status == 304:
        extra.append("304")
    res["ok"] = not miss
    res["detail"] = ("нет полей: " + ",".join(miss)) if miss else "поля есть" + ("; " + "; ".join(extra) if extra else "")
    return res


def render_table(rows):
    head = ("источник", "HTTP", "мс", "КБ", "итог", "детали")
    data = [(r["id"], str(r["http"] or "-"), str(r["ms"] if r["ms"] is not None else "-"), str(r["kb"]), "OK" if r["ok"] else "ПРОБЛЕМА", r["detail"]) for r in rows]
    w = [max(len(head[i]), max([len(d[i]) for d in data] or [0])) for i in range(len(head))]
    fmt = "  ".join("%%-%ds" % x for x in w)
    return "\n".join([fmt % head, fmt % tuple("-" * x for x in w)] + [fmt % d for d in data])


def render_md(rows, now):
    out = ["", "## Проверка источников %s UTC (`./tb intel verify`)" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)), "",
           "| источник | URL | HTTP | мс | КБ | итог | детали |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| %s | %s | %s | %s | %s | %s | %s |" % (r["id"], r["url"], r["http"] or "-", r["ms"] if r["ms"] is not None else "-", r["kb"],
                                                          "OK" if r["ok"] else "ПРОБЛЕМА", r["detail"].replace("|", "/")))
    out += ["", "Источники со статусом OK владелец помечает `verified:true` в `config/intel.json` (`sources.<id>.verified`); с ПРОБЛЕМА: `enabled:false` или поправить URL/парсер.", ""]
    return "\n".join(out)


def write_doc(rows, now, root):
    path = os.path.join(root, DOC)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if not exists:
            f.write("# WP13: источники фактуры и новостей, результаты проверки\n")
        f.write(render_md(rows, now))
    return path


def main(argv=None, transport=None, clock=time.time, sleep=time.sleep, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="tb intel verify", description="Проверка живых источников (по одному GET). Запускать владельцу в Terminal.app.")
    ap.add_argument("--source", action="append", help="id источника или семейства (можно несколько раз)")
    ap.add_argument("--write", action="store_true", help="дописать результаты в docs/spec_v2/WP13_sources.md")
    ap.add_argument("--proxy", action="store_true", help="использовать системные прокси (по умолчанию как у сервиса: без прокси)")
    try:
        a = ap.parse_args(list(argv or []))
    except SystemExit:
        return 2
    root = paths.root()
    cfg = store.load_cfg(root)
    cfg["proxy"] = bool(a.proxy)
    cfg["max_per_hour"] = 1000
    f = ifetch.Fetcher(root, cfg, transport, clock)
    try:
        srcs = [s for s in pick(cfg, a.source)]
    except (ValueError, KeyError) as e:
        print("verify: %s" % e, file=out)
        return 2
    if not srcs:
        print("verify: нет такого источника (./tb intel sources)", file=out)
        return 2
    print("Проверка %d источников, по одному GET (прокси: %s). Тексты заголовков не печатаются." % (len(srcs), "системные" if a.proxy else "выкл."), file=out)
    rows = []
    slept = [0.0]

    def nap(sec):
        slept[0] += sec
        sleep(sec)
    for s in srcs:
        r = check_one(f, s, lambda: clock() + slept[0], nap)
        rows.append(r)
        print("  %-18s %s" % (s["id"], "OK" if r["ok"] else "ПРОБЛЕМА " + r["detail"]), file=out, flush=True)
    print("", file=out)
    print(render_table(rows), file=out)
    if a.write:
        print("\nзаписано: %s" % write_doc(rows, clock(), root), file=out)
    bad = [r["id"] for r in rows if not r["ok"]]
    if bad:
        print("\nПроблемные источники: %s" % ", ".join(bad), file=out)
    return 1 if bad else 0
