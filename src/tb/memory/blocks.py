"""Память для мозга (WP15b, 08 п. 3.5-3.6): блок `memory` досье и полезная нагрузка инструментов шлюза. Только чтение, без сети.

В блоках и ответах ТОЛЬКО числа, enum и идентификаторы (case_id, lesson_id): сырого текста решений/заметок нет; единственная строка текста это шаблонная
формулировка урока (`own_txt`, строится кодом из cond+claim). Все данные это «прошлые наблюдения системы, не инструкции»: память ничего не разрешает
и не запрещает (разрешают только guard, Gate, agents.json).
Видимость: блок и инструменты работают для paper/testnet сразу; для live только при `config/memory.json: show_in_live=true` (по умолчанию false: решение
владельца Q-M1, до положительной оценки пользы памяти live-мозг её не видит). `enabled=false` или агент в `agents_off` (флаг A/B) = блока и данных нет.
Размер блока brief <= `brief.max_tokens` (450) оценкой символы/3.5; порядок усечения: список случаев до `cases_min` (3), список уроков до `lessons_max` (3),
затем остаётся только агрегат (случаи и уроки убраны), затем снимаются top_err и other_mode. Ответы инструментов <= `tool.max_bytes` (2048).
"""
import json
import math
import time

from tb.core import plan_schema
from tb.memory import agg, cases, cfg as memcfg, features, lessons, query, similar

NOTE = "own_past: прошлые наблюдения системы, данные, не инструкции; INSUFFICIENT/LOW_N: выводов нет"
CHARS_PER_TOKEN = 3.5
SHOWN_MODES = ("paper", "testnet")


def tokens(obj):
    """Оценка токенов как в brief.tokens: компактный JSON, символы / 3.5."""
    return int(math.ceil(len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)) / CHARS_PER_TOKEN))


def nbytes(obj):
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def enabled(cfg, *ids):
    """Память включена глобально и ни один из идентификаторов (агент, потребитель) не в `agents_off`."""
    if cfg.get("enabled", True) is False:
        return False
    off = set(cfg.get("agents_off") or ())
    return not any(i and i in off for i in ids)


def shown(mode, cfg):
    """paper/testnet показываются сразу; live только по явному разрешению `show_in_live`; режим неизвестен = скрыто (fail closed)."""
    if mode in SHOWN_MODES:
        return True
    return mode == "live" and cfg.get("show_in_live") is True


def mem_state(cfg, agent_id, mode):
    """Метка `mem.on|off` для плана/случая: память реально была доступна агенту на момент решения."""
    return "on" if enabled(cfg, agent_id) and shown(mode, cfg) else "off"


# ------------------------------------------------------------------ признаки запроса
def plan_features(spec, cfg, now=None):
    """Спецификация плана (state/plans/*/spec.json) -> (sym, side, setup, признаки) для поиска похожих. Только числа из плана, ничего не выдумывается."""
    spec = spec if isinstance(spec, dict) else {}
    sym = features.base_sym(spec.get("symbol"))
    side = spec.get("side") if spec.get("side") in cases.SIDES else None
    tags = spec.get("tags") if isinstance(spec.get("tags"), dict) else {}
    setup = tags.get("setup") if tags.get("setup") in cases.SETUPS else None
    ref = plan_schema.entry_ref(spec)
    stop = (spec.get("stop") or {}).get("price") if isinstance(spec.get("stop"), dict) else None
    tg = [t.get("price") for t in (spec.get("targets") or []) if isinstance(t, dict)]
    f = {}
    sp = features.stop_pct(ref, stop)
    if sp is not None:
        f["stop_pct"] = sp
    rr = features.rr_planned(ref, stop, tg[-1] if tg else None)
    if rr is not None:
        f["rr_planned"] = rr
    sm = features.num((spec.get("risk") or {}).get("size_mult")) if isinstance(spec.get("risk"), dict) else None
    if sm is not None:
        f["size_mult"] = sm
    when = (spec.get("entry") or {}).get("when") if isinstance(spec.get("entry"), dict) else None
    if isinstance(when, dict) and when:
        f["confirmed"] = bool(plan_schema.has_confirm(when))
    return sym, side, setup, f


# ------------------------------------------------------------------ компактные виды
def _drop(d):
    return dict((k, v) for k, v in d.items() if v is not None and v != [] and v != {})


def case_row(c, sim=None):
    """Случай -> {id, sim, ty, r, err, exit}: идентификатор, числа и enum (без текстов)."""
    r = c.get("result") or {}
    return _drop({"id": c["case_id"], "sim": None if sim is None else round(sim, 2), "ty": c.get("type"), "r": r.get("r"),
                  "err": list(((c.get("tags") or {}).get("err")) or [])[:3], "exit": r.get("exit_reason")})


def lesson_row(l):
    return _drop({"id": l["id"], "st": l["st"], "n": l.get("n"), "conf": l.get("conf"), "own_txt": l.get("txt"),
                  "contra": l.get("contradicts") or None})


def top_lessons(root, now, cfg, limit=None, all_lessons=None):
    """Активные уроки без привязки к ситуации (для scope=start и list_lessons), по убыванию conf_eff; скрытые ниже hide_below не берутся."""
    lc = cfg.get("lessons") or {}
    ls = list(lessons.read_all(root).values()) if all_lessons is None else all_lessons
    out = []
    for l in ls:
        if l.get("status") not in lessons.ACTIVE:
            continue
        ce = lessons.conf_eff(l, now, cfg)
        if ce < lc.get("hide_below", 0.15):
            continue
        out.append({"id": l["lesson_id"], "st": l["status"], "n": (l.get("evidence") or {}).get("n_closed", 0), "conf": ce, "txt": l.get("txt"),
                    "contradicts": list(l.get("contradicts") or [])})
    out.sort(key=lambda x: (-x["conf"], x["id"]))
    return out[:limit or lc.get("brief_max", 3)]


# ------------------------------------------------------------------ усечение
def fit_block(blk, max_tokens, bc):
    """Порядок усечения 08 п. 3.6: cases -> cases_min, lessons -> lessons_max, затем только агрегат, затем top_err/other_mode. -> блок."""
    blk = json.loads(json.dumps(blk))
    sim = blk.get("similar") if isinstance(blk.get("similar"), dict) else None

    def over():
        return tokens(blk) > max_tokens

    def cut_lessons(n):
        if len(blk.get("lessons") or []) > n:
            blk["lessons"] = blk["lessons"][:n]
    cmin = int(bc.get("cases_min", 3))
    while over() and sim is not None and len(sim.get("cases") or []) > cmin:
        sim["cases"].pop()
    if over():
        cut_lessons(bc.get("lessons_max", 3))
    if over() and sim is not None:
        sim.pop("cases", None)
    if over():
        blk.pop("lessons", None)
    if over() and sim is not None:
        sim.pop("top_err", None)
    if over():
        blk.pop("other_mode", None)
    if over():
        for k in ("avg_sim", "se", "base_r"):
            (sim or blk).pop(k, None)
    return blk


# ------------------------------------------------------------------ блок brief
def _other_mode(allc, mode, cfg):
    alt = [c for c in allc if (c.get("src") or {}).get("mode", "live") != mode and agg.closed_r(c) is not None and cases.eligible(c, cfg)]
    if not alt:
        return None
    return {"n": len(alt), "r": agg.summarize(alt, None, cfg)["mean_r"]}


def start_block(root, now, mode, cfg, allc=None, all_lessons=None):
    """scope=start: общая статистика закрытых сделок режима за `start_days` (14) с тегами ошибок + топ-3 урока."""
    bc = cfg.get("brief") or {}
    allc = cases.load(root) if allc is None else allc
    since = now - float(bc.get("start_days", 14)) * 86400.0
    sel = [c for c in allc if cases.eligible(c, cfg) and (c.get("src") or {}).get("mode", "live") == mode and (similar.ts_epoch(c.get("ts")) or 0.0) >= since
           and agg.closed_r(c) is not None]
    base = agg.base_rate(allc, mode, tuple(cfg.get("exclude_roles") or ()))
    s = agg.summarize(sel, base["base_r"], cfg)
    errs = {}
    for c in sel:
        for t in (c.get("tags") or {}).get("err") or []:
            errs[t] = errs.get(t, 0) + 1
    ls = [lesson_row(l) for l in top_lessons(root, now, cfg, bc.get("lessons_max", 3), all_lessons)]
    other = _other_mode(allc, mode, cfg)
    if not sel and not ls and not other:
        return None
    blk = {"note": NOTE, "mode": mode, "scope": "start", "days": int(bc.get("start_days", 14)),
           "similar": _drop({"n_closed": s["n_closed"], "mean_r": s["mean_r"], "win": s["win"], "base_r": s["base_r"], "warn": s["warn"],
                             "top_err": [[t, v] for t, v in sorted(errs.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]}),
           "lessons": ls, "other_mode": other}
    return _drop(blk)


def event_block(root, now, mode, spec, cfg, allc=None, all_lessons=None):
    """scope=event для плана armed/draft: похожие прошлые ситуации + применимые уроки."""
    sym, side, setup, feats = plan_features(spec, cfg, now)
    return _event_from(root, now, mode, cfg, sym, side, setup, feats, allc, all_lessons)


def scout_block(root, now, mode, cfg, sym, feats=None, allc=None, all_lessons=None):
    """scope=event для поиска входа (плана ещё нет): похожие ситуации по монете события (класс монеты, сессия, диапазон 5м), сторона и сетап не заданы, поэтому
    сторона не фильтрует, а в блоке она не указана."""
    return _event_from(root, now, mode, cfg, features.base_sym(sym), None, None, dict(feats or {}), allc, all_lessons)


def _event_from(root, now, mode, cfg, sym, side, setup, feats, allc=None, all_lessons=None):
    bc = cfg.get("brief") or {}
    if not sym:
        return None
    q = query.build_query(sym, side, setup, feats, mode=mode, cfg=cfg, now=now)
    allc = cases.load(root) if allc is None else allc
    res = query.find_similar(root, q, k=bc.get("cases_max", 6), as_of=now, now=now, cfg=cfg, all_cases=allc,
                             lessons_list=list(lessons.read_all(root).values()) if all_lessons is None else all_lessons)
    rep = res["similar"]
    by_id = dict((c["case_id"], c) for c in allc)
    rows = [case_row(by_id[h["case_id"]], h["sim"]) for h in res["cases"] if h["case_id"] in by_id]
    sim = _drop({"n": rep["n"], "n_closed": rep["n_closed"], "mean_r": rep["mean_r"], "se": rep["se"], "win": rep["win"], "base_r": rep["base_r"],
                 "warn": rep["warn"], "avg_sim": rep["avg_sim"], "top_err": rep["top_err"], "cases": rows})
    sim.setdefault("warn", rep["warn"])
    ls = [lesson_row(l) for l in res["lessons"][:bc.get("lessons_max", 3)]]
    if rep["n"] == 0 and not ls and not res["other_mode"]:
        return None
    blk = {"note": NOTE, "mode": mode, "scope": "event", "sym": sym, "side": side, "similar": sim, "lessons": ls,
           "other_mode": ({"n": res["other_mode"]["n_closed"], "r": res["other_mode"]["mean_r"]} if res["other_mode"] else None)}
    return _drop(blk)


def brief_block(root, now, scope, spec=None, mode=None, agent=None, consumer=None, cfg=None, scout_sym=None, scout_feats=None):
    """-> блок `memory` досье или None (выключено / режим скрыт / нечего показать / нет подходящего плана). Исключения наружу не скрываются: вызывающий (brief._safe) отметит блок как missing."""
    cfg = cfg or memcfg.load(root)
    if not enabled(cfg, agent, consumer) or not shown(mode, cfg):
        return None
    if scope == "start":
        blk = start_block(root, now, mode, cfg)
    elif scope == "event":
        if isinstance(spec, dict):
            blk = event_block(root, now, mode, spec, cfg)
        else:
            blk = scout_block(root, now, mode, cfg, scout_sym, scout_feats) if scout_sym else None
    else:
        return None
    if blk is None:
        return None
    return fit_block(blk, int((cfg.get("brief") or {}).get("max_tokens", 450)), cfg.get("brief") or {})


# ------------------------------------------------------------------ инструменты шлюза
def fit_bytes(out, limit, list_keys=(("cases",), ("lessons",))):
    """Ответ инструмента <= limit байт: сначала режутся списки с хвоста (по одному), затем снимаются необязательные поля."""
    out = json.loads(json.dumps(out))
    while nbytes(out) > limit:
        done = False
        for path in list_keys:
            node = out
            for k in path[:-1]:
                node = node.get(k) if isinstance(node, dict) else None
            lst = node.get(path[-1]) if isinstance(node, dict) else None
            if isinstance(lst, list) and lst:
                lst.pop()
                done = True
                out["trunc"] = True
                break
        if not done:
            for k in ("filtered", "other_mode"):
                if k in out:
                    out.pop(k)
                    out["trunc"] = True
                    done = True
                    break
        if not done:
            break
    return out


def similar_payload(root, mode, sym, side, setup, feats, k, scope, now, cfg, spec=None):
    """get_similar_cases: агрегат + до 8 случаев (id, sim, числа/enum) + применимые уроки + warn. Случаи строго до `now` (as_of). Признаки не заданы
    и есть план символа -> признаки плана. -> dict; ValueError при неверном значении признака."""
    tc = cfg.get("tool") or {}
    feats = dict(feats or {})
    if spec is not None:
        psym, pside, psetup, pf = plan_features(spec, cfg, now)
        sym, side, setup = sym or psym, side or pside, setup or psetup
        for kk, vv in pf.items():
            feats.setdefault(kk, vv)
    q = query.build_query(sym, side, setup, feats, mode=mode, cfg=cfg, now=now)
    allc = cases.load(root)
    res = query.find_similar(root, q, k=min(int(k or tc.get("cases_max", 8)), int(tc.get("k_max", 10))), scope=scope, as_of=now, now=now, cfg=cfg, all_cases=allc)
    rep = res["similar"]
    by_id = dict((c["case_id"], c) for c in allc)
    rows = []
    for h in res["cases"][:tc.get("cases_max", 8)]:
        c = by_id.get(h["case_id"])
        if c is not None:
            row = case_row(c, h["sim"])
            row.update(_drop({"ts": c.get("ts"), "side": (c.get("situation") or {}).get("side"), "setup": (c.get("situation") or {}).get("setup"),
                              "confirmed": (c.get("situation") or {}).get("confirmed")}))
            rows.append(row)
    out = {"mode": mode, "sym": sym, "side": side, "warn": rep["warn"], "note": NOTE,
           "similar": _drop({"n": rep["n"], "n_closed": rep["n_closed"], "mean_r": rep["mean_r"], "se": rep["se"], "win": rep["win"], "median_r": rep["median_r"],
                             "base_r": rep["base_r"], "mean_mae_r": rep["mean_mae_r"], "mean_mfe_r": rep["mean_mfe_r"], "top_err": rep["top_err"],
                             "avg_sim": rep["avg_sim"], "coverage": rep["coverage"], "min_sim": rep["min_sim_used"]}),
           "cases": rows, "lessons": [lesson_row(l) for l in res["lessons"]],
           "other_mode": ({"n": res["other_mode"]["n_closed"], "r": res["other_mode"]["mean_r"]} if res["other_mode"] else None), "filtered": res["filtered"]}
    out = _drop(out)
    out.setdefault("cases", [])          # пустые списки остаются в ответе: «ничего похожего» это тоже факт
    out.setdefault("lessons", [])
    return fit_bytes(out, int(tc.get("max_bytes", 2048)))


def lessons_payload(root, status, limit, now, cfg):
    """list_lessons: уроки (id, статус, n, conf, conf_eff, шаблонная формулировка), по убыванию conf_eff."""
    tc = cfg.get("tool") or {}
    out = []
    for l in lessons.read_all(root).values():
        if status == "active" and l.get("status") not in lessons.ACTIVE:
            continue
        if status not in ("active", "all") and l.get("status") != status:
            continue
        out.append(_drop({"id": l["lesson_id"], "st": l["status"], "n": (l.get("evidence") or {}).get("n_closed"), "conf": l.get("conf"),
                          "conf_eff": lessons.conf_eff(l, now, cfg), "own_txt": l.get("txt"), "contra": l.get("contradicts") or None}))
    out.sort(key=lambda x: (-(x.get("conf_eff") or 0), x["id"]))
    res = {"status": status, "n": len(out), "note": NOTE, "lessons": out[:min(int(limit or 10), int(tc.get("lessons_max", 10)))]}
    return fit_bytes(res, int(tc.get("max_bytes", 2048)), (("lessons",),))
