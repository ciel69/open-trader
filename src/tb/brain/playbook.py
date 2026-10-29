"""Карточки сетапов (знания опытных трейдеров): config/playbook.json -> 1-2 подходящие карточки в brief.playbook по сигналу и режиму рынка. Владелец: мозг.

Поиск детерминированный и дешёвый (без LLM и без эмбеддингов): карточка подходит, если вид сигнала события (скан: range_5m, oi_flush, liq_cascade, ...; новости: news:<класс>) есть в её kinds и режим
монеты (структура 1ч: trend_up|trend_down|range|storm) входит в её regimes (пусто = любой). Больше совпавших видов, известный режим и подтверждённая гипотеза карточки (brief.experiments) поднимают её выше.
Карточки это общеизвестная методология, НЕ доказанное преимущество: у каждой тег гипотезы hyp, и по нему копится статистика paper (./tb exp stats). Жёсткие правила entry_scout и guard выше карточек.
Текст карточек пишет владелец репозитория (не третьи лица), но длина ограничена: брифу нужны токены.
"""
from tb import paths
from tb.brain import common, validate
from tb.core import bus

MAX_TEXT = {"when": 210, "stop": 120, "target": 120, "inval": 120, "avoid": 120}
TEXT_FIELDS = ("when", "stop", "target", "inval", "avoid")
SIDES = ("long", "short", "both")
KINDS = tuple(bus.SCANNER_KINDS) + tuple("news:%s" % c for c in bus.FLASH_CLASSES)
REGIMES = ("trend_up", "trend_down", "range", "storm")
MAX_CARDS = 2


def check_card(c):
    """-> (карточка | None, причина). Проверяет типы, enum и длину текстов; лишние поля отбрасываются."""
    if not isinstance(c, dict):
        return None, "not_dict"
    cid, hyp = c.get("id"), c.get("hyp")
    if not (isinstance(cid, str) and common.ident(cid) == cid) or not (isinstance(hyp, str) and common.ident(hyp) == hyp):
        return None, "bad_id_or_hyp"
    if c.get("setup") not in validate.SETUPS or c.get("side") not in SIDES:
        return None, "bad_setup_or_side"
    kinds = c.get("kinds")
    if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds):
        return None, "bad_kinds"
    regimes = c.get("regimes") or []
    if not isinstance(regimes, list) or any(r not in REGIMES for r in regimes):
        return None, "bad_regimes"
    out = {"id": cid, "setup": c["setup"], "side": c["side"], "kinds": list(kinds), "regimes": list(regimes), "hyp": hyp}
    for f in TEXT_FIELDS:
        v = c.get(f)
        if not isinstance(v, str) or not v.strip() or len(v) > MAX_TEXT[f]:
            return None, "bad_text:%s" % f
        out[f] = v.strip()
    rr = c.get("rr")
    out["rr"] = float(rr) if isinstance(rr, (int, float)) and not isinstance(rr, bool) and 0.5 <= rr <= 10 else None
    return out, None


def load(root):
    """-> (карточки, ошибки). Нет файла или мусор -> ([], [..]); неверная карточка пропускается, остальные работают; дубликаты id отбрасываются."""
    doc = common.read_json(paths.config("playbook.json", root=root))
    if not isinstance(doc, dict) or not isinstance(doc.get("cards"), list):
        return [], ["no_playbook"]
    cards, errs, seen = [], [], set()
    for i, raw in enumerate(doc["cards"]):
        c, err = check_card(raw)
        if c is None:
            errs.append("card%d:%s" % (i, err))
        elif c["id"] in seen:
            errs.append("card%d:dup_id" % i)
        else:
            seen.add(c["id"])
            cards.append(c)
    return cards, errs


def match(cards, kinds, reg=None, exp=None, k=MAX_CARDS):
    """kinds: виды сигналов события (scanner kind и news:<класс>); reg: режим монеты или None; exp: {hyp: {"n","avg_r","confirmed"}} из brief.experiments.
    -> до k компактных карточек {id, setup, side, when, stop, target, inval, avoid, rr, hyp, why[, exp]} по убыванию соответствия."""
    kinds = set(kinds or ())
    exp = exp or {}
    scored = []
    for order, c in enumerate(cards):
        hit = [x for x in c["kinds"] if x in kinds]
        if not hit:
            continue
        if reg in REGIMES and c["regimes"] and reg not in c["regimes"]:
            continue                                         # режим известен и не тот, для которого карточка
        score = 4.0 * min(len(hit), 2) + (1.0 if (reg in c["regimes"]) else 0.0) + 3.0 / len(c["kinds"])         # узкая карточка (мало видов сигнала) точнее общей
        e = exp.get(c["hyp"])
        if e and e.get("confirmed"):
            score += 2.0
        scored.append((-score, order, c, hit, e))
    scored.sort(key=lambda t: (t[0], t[1]))
    out = []
    for n, (_, _, c, hit, e) in enumerate(scored[:k]):
        fields = ("id", "setup", "side", "when", "stop", "target", "inval", "avoid", "hyp") if n == 0 else ("id", "setup", "side", "when", "stop", "inval", "hyp")   # вторая карточка короче: токены
        row = {f: c[f] for f in fields}
        if c["rr"] is not None:
            row["rr"] = c["rr"]
        row["why"] = ["kind:%s" % h for h in hit[:2]] + (["reg:%s" % reg] if reg in c["regimes"] else [])
        if e:
            row["exp"] = {kk: e[kk] for kk in ("n", "avg_r") if e.get(kk) is not None}
        out.append(row)
    return out


def exp_index(experiments):
    """brief.experiments -> {hyp: {n, avg_r, confirmed}} для match."""
    idx = {}
    for key, flag in (("confirmed", True), ("open", False)):
        for r in ((experiments or {}).get(key) or []):
            if isinstance(r, dict) and r.get("hyp"):
                idx[r["hyp"]] = {"n": r.get("n"), "avg_r": r.get("avg_r"), "confirmed": flag}
    return idx
