"""Сканер фигур и уровней (только чтение): последние бары из data/ и цена из runtime/market.json. Ничего не пишет и не торгует.

  ./tb patterns scan --sym ETH [--tf 1h|4h|1d] [--last 6] [--root DIR] [--json]
Определения и пороги: marketdata/patterns.py, config/patterns_config.json (ключ "detector"). Вывод — описание рынка и счёт подтверждений (confluence),
НЕ сигнал: на истории ни одна фигура не прошла проверку (docs/RESEARCH.md, ./tb research patterns).
"""
import argparse
import json
import sys
import time

from tb import paths
from tb.marketdata import data
from tb.marketdata import patterns as pt

DISCLAIMER = "Фигуры НЕ доказаны как сигнал (см. docs/RESEARCH.md). Это подтверждение для решения, не повод входить."


def load_cfg(root=None):
    """-> (cfg детектора, min_score для событий наблюдателя). Нет файла/битый JSON -> значения по умолчанию."""
    try:
        with open(paths.config("patterns_config.json", root=root), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    return pt.merge_cfg(raw.get("detector")), int(raw.get("watch_min_score", 3))


def load_bars(sym, tf, root=None):
    """Бары нужного ТФ: 1h из candles_<SYM>_1h.csv; 4h агрегируется из 1h; 1d из candles_<SYM>_1d.csv, иначе агрегируется из 1h."""
    h1 = data.load_candles(paths.data("candles_%s_1h.csv" % sym, root=root))
    if tf == "1h":
        return h1
    if tf == "4h":
        return pt.aggregate(h1, 4)[0]
    d1 = data.load_candles(paths.data("candles_%s_1d.csv" % sym, root=root))
    return d1 if d1 else pt.aggregate(h1, 24)[0]


def live_price(sym, root=None, now=None, max_age=300):
    try:
        with open(paths.runtime("market.json", root=root), encoding="utf-8") as f:
            m = json.load(f)
        age = (time.time() if now is None else now) - float(m.get("ts_epoch") or 0)
        p = ((m.get("symbols") or {}).get(sym) or {}).get("price")
        return (float(p), age) if isinstance(p, (int, float)) and p > 0 and age <= max_age else (None, age)
    except (OSError, ValueError, TypeError):
        return None, None


def analyze(sym, tf="1h", last=6, root=None, now=None):
    cfg, _ = load_cfg(root)
    if tf != "1h":
        cfg = dict(cfg, htf_k=0)  # старший ТФ для 4h/1d не считаем
    bars = load_bars(sym, tf, root)
    if len(bars) < 60:
        return {"sym": sym, "tf": tf, "error": "мало баров (%d) в data/" % len(bars)}
    an = pt.Analyzer(bars, cfg)
    i = len(bars) - 1
    a = an.atr(i)
    px, age = live_price(sym, root, now)
    ref = px if px else bars[i][pt.C]
    lv = an.levels(i)
    below, above = pt.nearest_levels(lv, ref, int(cfg["level_min_touches"]))
    lv_out = lambda x: None if x is None else {"price": round(x["price"], 6), "touches": x["touches"], "strength": round(x["strength"], 2),
                                              "dist_atr": round(abs(x["price"] - ref) / a, 2) if a else None}
    rk = an.bb_rank(i)
    recent = []
    for j in range(max(0, i - last + 1), i + 1):
        c = an.candles(j)
        if c:
            recent.append({"ts": data.epoch_to_iso(bars[j][0]), "patterns": {k: v for k, v in c.items()}})
    d, info = pt.double_extreme(bars, i, cfg, an.flags(), a)
    return {"sym": sym, "tf": tf, "last_bar": data.epoch_to_iso(bars[i][0]), "last_close": bars[i][pt.C],
            "live_price": px, "feed_age_s": None if age is None else round(age, 1), "atr": round(a, 6) if a else None,
            "support": lv_out(below), "resistance": lv_out(above), "bb_width_rank": None if rk is None else round(rk, 3),
            "squeeze": rk is not None and rk <= cfg["squeeze_pct"], "htf_trend": an.htf(i) if tf == "1h" else None,
            "double": None if not d else dict(info, dir=d), "recent": recent, "confluence": an.confluence(i),
            "note": DISCLAIMER}


def render(r):
    if "error" in r:
        return "%s %s: %s" % (r["sym"], r["tf"], r["error"])
    L = ["%s %s: последний закрытый бар %s, close %.6g, ATR %s" % (r["sym"], r["tf"], r["last_bar"], r["last_close"], r["atr"]),
         "цена фида: %s" % ("нет (фид не пишет или данные старше 5 мин)" if r["live_price"] is None else "%.6g (возраст %.0f с)" % (r["live_price"], r["feed_age_s"])),
         "поддержка: %s" % _lv(r["support"]), "сопротивление: %s" % _lv(r["resistance"]),
         "Bollinger: ранг ширины %s%s; тренд старшего ТФ: %s" % (r["bb_width_rank"], " (СЖАТИЕ)" if r["squeeze"] else "", {1: "вверх", -1: "вниз", 0: "нет", None: "n/a"}[r["htf_trend"]])]
    if r["double"]:
        L.append("двойное %s: неклайн %.6g" % ("дно" if r["double"]["dir"] == 1 else "вершина", r["double"]["neckline"]))
    L.append("фигуры в последних барах: " + ("нет" if not r["recent"] else ""))
    for x in r["recent"]:
        L.append("  %s  %s" % (x["ts"], ", ".join("%s%s" % (k, "" if v is True else (" (бычья)" if v == 1 else " (медвежья)")) for k, v in x["patterns"].items())))
    for side in ("long", "short"):
        c = r["confluence"][side]
        L.append("confluence %-5s: %+d  %s" % (side, c["score"], "; ".join(c["reasons"]) or "-"))
    L.append(r["note"])
    return "\n".join(L)


def _lv(x):
    return "нет" if x is None else "%.6g (касаний %d, сила %.2f, %.1f ATR)" % (x["price"], x["touches"], x["strength"], x["dist_atr"] or 0)


def main(argv):
    ap = argparse.ArgumentParser(prog="tb patterns")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan")
    s.add_argument("--sym", required=True)
    s.add_argument("--tf", default="1h", choices=["1h", "4h", "1d"])
    s.add_argument("--last", type=int, default=6)
    s.add_argument("--root", default=None)
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = analyze(a.sym.upper(), a.tf, a.last, a.root)
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else render(r))
    return 1 if "error" in r else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
