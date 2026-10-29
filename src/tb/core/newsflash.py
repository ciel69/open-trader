"""Реестр быстрых новостей (чтение): state/news_flash.jsonl, пишет его только intel/flash.py (слой intel). Здесь лишь пути и чтение для досье мозга и дашборда, чтобы слои
brain/gateway/dash не импортировали intel (см. тесты слоёв). Только числа, enum и идентификаторы: текста заголовков в реестре нет. Сети нет.
"""
from tb import paths
from tb.core import ideas


def ledger_path(root=None):
    return paths.state("news_flash.jsonl", root=root)


def read_rows(root=None):
    return ideas.read_rows(ledger_path(root))


def recent_for(root, sym, now, max_age_s=1800.0, n=3):
    """Для досье мозга: flash по монете за последние max_age_s: [{cls, dir, imp, age_s, move_pct}] (move_pct = изменение цены с момента новости к последней цене)."""
    rows = [r for r in read_rows(root) if r.get("ev") == "flash" and r.get("sym") == sym and now - float(r.get("ts") or 0) <= max_age_s]
    if not rows:
        return []
    cur = (ideas.price_book(root, now).get(sym) or (None,))[0]
    out = []
    for r in sorted(rows, key=lambda x: -float(x.get("ts") or 0))[:n]:
        mv = round(100.0 * (cur / r["px0"] - 1.0), 3) if cur and r.get("px0") else None
        d = {"cls": r.get("cls"), "dir": r.get("dir"), "imp": r.get("imp"), "age_s": int(now - float(r["ts"])), "move_pct": mv}
        out.append({k: v for k, v in d.items() if v is not None})
    return out


def stats(root, min_n=1):
    """Сводка реестра: по классу и горизонту: n, средний ret_pct, средний dir_pct (по подсказке направления) и доля попаданий."""
    rows = read_rows(root)
    cls_of = {r["id"]: r.get("cls") for r in rows if r.get("ev") == "flash"}
    agg = {}
    for r in rows:
        if r.get("ev") != "outcome" or r.get("id") not in cls_of:
            continue
        a = agg.setdefault((cls_of[r["id"]], r.get("h")), {"n": 0, "ret": 0.0, "dir_n": 0, "dir": 0.0, "hit": 0})
        a["n"] += 1
        a["ret"] += r.get("ret_pct") or 0.0
        if r.get("dir_pct") is not None:
            a["dir_n"] += 1
            a["dir"] += r["dir_pct"]
            a["hit"] += 1 if r["dir_pct"] > 0 else 0
    out = []
    for (cls, h), a in sorted(agg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0)):
        if a["n"] >= min_n:
            out.append({"cls": cls, "h": h, "n": a["n"], "mean_ret_pct": round(a["ret"] / a["n"], 4),
                        "mean_dir_pct": round(a["dir"] / a["dir_n"], 4) if a["dir_n"] else None, "hit": round(a["hit"] / a["dir_n"], 3) if a["dir_n"] else None})
    return out
