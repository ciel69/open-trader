"""Вывод ревьюера tb.casereview/1: схема из enum-значений и валидатор «known_at_decision» против самой ситуации (08 п. 1.4).

Защита от ретроспективной предвзятости и галлюцинаций: названный в known_at_decision признак ОБЯЗАН быть истинным по situation/decision случая
(вычисляется кодом из чисел), а признак, восстановленный после сделки (situation.retro), считается неизвестным на момент решения. Ложное
утверждение = весь вывод отклоняется. Свободный текст только alternative.detail (<=160, очищается, в brief не попадает). Один вывод на случай.
Ревьюер не меняет situation/result: пишется update-строка с review и (детерминированно по improve) тегами err.
"""
from tb.memory import cases, lessons, privacy

SCHEMA = "tb.casereview/1"
VERDICTS = ("good_process", "bad_process", "unlucky", "lucky", "unclear")
RIGHT = ("confirm_waited", "stop_beyond_extreme", "size_ok", "skipped_bad_range", "respected_cooldown", "waited_for_blackout")
IMPROVE = ("no_confirm", "chase", "wide_stop", "tight_stop", "size", "ignored_cooldown", "blackout", "fee_drag", "wrong_direction", "late_exit", "none")
ALT_ACTIONS = ("wait_confirm", "skip", "smaller_size", "wider_stop_skip", "other_side", "exit_earlier", "hold_longer", "none")
KNOWN = ("range_5m_gt_stop", "funding_extreme", "no_confirm", "wide_stop", "tight_stop", "spread_wide", "blackout", "chase_move", "low_rr", "big_move_24h", "meme_coin")
IMPROVE_TO_ERR = {"no_confirm": "no-confirm", "chase": "chase", "wide_stop": "wide-stop", "tight_stop": "tight-stop", "size": "size", "fee_drag": "fee-drag",
                  "wrong_direction": "wrong-direction", "ignored_cooldown": "rule-break", "blackout": "rule-break"}
# признак улики -> поля situation, от которых он зависит (если поле восстановлено ретроспективно, признак неизвестен)
DEPENDS = {"no_confirm": ("confirmed", "no_confirm_tag"), "range_5m_gt_stop": ("range_5m_pct", "stop_pct"), "wide_stop": ("stop_pct",),
           "funding_extreme": ("funding_pct",), "spread_wide": ("spread_bps",), "blackout": ("blackout",), "chase_move": ("ret_5m_pct",),
           "low_rr": ("rr_planned",), "big_move_24h": ("chg_24h_pct",), "meme_coin": ("coin_class",)}
IMPROVE_FEATURE = {"no_confirm": "no_confirm", "wide_stop": "wide_stop", "blackout": "blackout", "chase": "chase_move", "tight_stop": "tight_stop"}


def known_features(case, cfg):
    """Признаки, вычислимые из situation/decision -> {имя: True|False|None (неизвестно)}."""
    s, d = case.get("situation") or {}, case.get("decision") or {}
    retro = set(s.get("retro") or [])
    rc = cfg.get("review") or {}
    out = {}

    def have(name, fields):
        return not retro.intersection(fields)
    st, r5, ret5 = s.get("stop_pct"), s.get("range_5m_pct"), s.get("ret_5m_pct")
    out["range_5m_gt_stop"] = (r5 > st) if st is not None and r5 is not None else None
    fr = s.get("funding_pct")
    out["funding_extreme"] = (abs(fr) >= rc.get("funding_extreme_pct", 0.1)) if fr is not None else None
    if s.get("confirmed") is False or s.get("no_confirm_tag") is True:
        out["no_confirm"] = True
    elif s.get("confirmed") is True:
        out["no_confirm"] = False
    else:
        out["no_confirm"] = None
    out["wide_stop"] = (st >= rc.get("wide_stop_pct", 3.0)) if st is not None else None
    sv = d.get("stop_dist_vs_range5")
    out["tight_stop"] = (sv < 1.0) if sv is not None else None
    sp = s.get("spread_bps")
    out["spread_wide"] = (sp >= rc.get("spread_wide_bps", 5.0)) if sp is not None else None
    out["blackout"] = s.get("blackout")
    side = s.get("side")
    if ret5 is not None and side in ("long", "short"):
        th = rc.get("chase_ret_5m_pct", 1.0)
        out["chase_move"] = (ret5 >= th) if side == "long" else (ret5 <= -th)
    else:
        out["chase_move"] = None
    rr = s.get("rr_planned")
    out["low_rr"] = (rr < rc.get("low_rr", 1.5)) if rr is not None else None
    ch = s.get("chg_24h_pct")
    out["big_move_24h"] = (abs(ch) >= rc.get("big_move_24h_pct", 10.0)) if ch is not None else None
    cc = s.get("coin_class")
    out["meme_coin"] = (cc == "meme") if cc not in (None, "unknown") else None
    for name, fields in DEPENDS.items():
        if not have(name, fields):
            out[name] = None
    if "tight_stop" in out and "stop_pct" in retro:
        out["tight_stop"] = None
    return out


def suggest_verdict(case):
    """Код сам предлагает кандидатов unlucky/lucky по явному правилу (LLM лишь подтверждает): unlucky = процесс чистый и R<0, lucky = процесс с ошибками и R>0."""
    r = (case.get("result") or {})
    if case.get("type") != "trade" or r.get("status") != "closed" or r.get("r") is None:
        return None
    t = case.get("tags") or {}
    errs = [e for e in (t.get("err") or []) if e not in ("good", "good-process-bad-luck")]
    clean = t.get("rule_followed") == "yes" and not errs
    dirty = t.get("rule_followed") in ("no", "partial") or bool(errs)
    if r["r"] < 0 and clean:
        return "unlucky"
    if r["r"] > 0 and dirty:
        return "lucky"
    return None


def _enum_list(v, allowed, name, errs, maxlen=5):
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > maxlen or len(set(v)) != len(v) or any(x not in allowed for x in v):
        errs.append("%s: список (до %d, без повторов) из %s" % (name, maxlen, list(allowed)))
        return []
    return list(v)


def validate(review, case, cfg):
    """-> (нормализованный вывод, [ошибки], suspect: bool). Принимает только ключи схемы."""
    errs = []
    if not isinstance(review, dict):
        return None, ["review: нужен объект"], False
    keys = ("schema", "verdict", "right", "improve", "alternative", "known_at_decision", "lesson_hint", "conf")
    bad = [k for k in review if k not in keys]
    if bad:
        errs.append("review: неизвестные ключи %s" % bad)
    if review.get("schema") != SCHEMA:
        errs.append("review.schema должна быть %s" % SCHEMA)
    out = {"schema": SCHEMA}
    out["verdict"] = review.get("verdict")
    if out["verdict"] not in VERDICTS:
        errs.append("verdict: не из %s" % list(VERDICTS))
    out["right"] = _enum_list(review.get("right"), RIGHT, "right", errs)
    out["improve"] = _enum_list(review.get("improve"), IMPROVE, "improve", errs)
    if "none" in out["improve"] and len(out["improve"]) > 1:
        errs.append("improve: none только в одиночку")
    if not out["improve"]:
        errs.append("improve: нужен хотя бы none")
    out["known_at_decision"] = _enum_list(review.get("known_at_decision"), KNOWN, "known_at_decision", errs, maxlen=8)
    alt = review.get("alternative") or {"action": "none", "detail": ""}
    suspect = False
    if not isinstance(alt, dict) or set(alt) - {"action", "detail"} or alt.get("action") not in ALT_ACTIONS:
        errs.append("alternative: {action из %s, detail<=160}" % list(ALT_ACTIONS))
        alt = {"action": "none", "detail": ""}
    detail, suspect = privacy.safe_text(alt.get("detail"), 160)
    out["alternative"] = {"action": alt["action"], "detail": detail}
    conf = review.get("conf")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        errs.append("conf: число 0..1")
    else:
        out["conf"] = round(float(conf), 2)
    lh = review.get("lesson_hint")
    if lh is not None:
        if not isinstance(lh, dict) or set(lh) - {"code", "cond"} or lh.get("code") not in lessons.LESSON_CODES or lh.get("cond") not in lessons.OPS:
            errs.append("lesson_hint: {code из %s, cond из %s}" % (list(lessons.LESSON_CODES), list(lessons.OPS)))
        else:
            out["lesson_hint"] = {"code": lh["code"], "cond": lh["cond"]}
    out.setdefault("lesson_hint", None)
    # --- проверка против самой ситуации
    kf = known_features(case, cfg)
    for name in out["known_at_decision"]:
        if kf.get(name) is not True:
            errs.append("known_at_decision: %r не подтверждается ситуацией на момент решения (%s)" % (name, "неизвестно" if kf.get(name) is None else "ложно"))
    for imp in out["improve"]:
        f = IMPROVE_FEATURE.get(imp)
        if f and kf.get(f) is False:
            errs.append("improve: %r противоречит ситуации (признак %s ложен)" % (imp, f))
    res = case.get("result") or {}
    r = res.get("r")
    v = out["verdict"]
    if v in ("unlucky", "lucky"):
        if case.get("type") != "trade" or res.get("status") != "closed" or r is None:
            errs.append("verdict %s допустим только для закрытой сделки с R" % v)
        elif v == "unlucky" and not (r < 0 and out["improve"] == ["none"]):
            errs.append("unlucky: нужен минус при improve=[none] (процесс без ошибок)")
        elif v == "lucky" and not (r > 0 and out["improve"] != ["none"]):
            errs.append("lucky: нужен плюс при ошибках процесса (improve не none)")
    if v == "good_process" and out["improve"] != ["none"]:
        errs.append("good_process несовместим с перечнем improve")
    if v == "bad_process" and out["improve"] == ["none"]:
        errs.append("bad_process требует хотя бы одну ошибку в improve")
    return (None if errs else out), errs, suspect


def normalize_hint(review):
    """Шов схем ролей (brain/validate: код `L-[a-z0-9_]+`, cond = код условия) и памяти (код из LESSON_CODES в верхнем регистре, cond = оператор):
    `L-no_confirm` -> `L-NO_CONFIRM` с оператором его условия; неизвестный код = подсказка отбрасывается (null), весь вывод из-за неё не отклоняется.
    Подсказка с лишними ключами не трогается (validate её отклонит). Остальные поля не меняются."""
    if not isinstance(review, dict) or not isinstance(review.get("lesson_hint"), dict):
        return review
    lh = review["lesson_hint"]
    if set(lh) - {"code", "cond"}:
        return review
    out = dict(review)
    up = lh.get("code").upper() if isinstance(lh.get("code"), str) else None
    out["lesson_hint"] = {"code": up, "cond": lessons.LESSON_CODES[up][0]["op"]} if up in lessons.LESSON_CODES else None
    return out


def map_to_err(improve):
    """Детерминированный перенос improve -> теги ошибок LESSONS (tags.err)."""
    out = []
    for i in improve:
        t = IMPROVE_TO_ERR.get(i)
        if t and t not in out:
            out.append(t)
    return out


def add_review(root, case_id, review, cfg, ts=None):
    """Проверяет и записывает вывод (update-строка). Повтор = отказ. -> записанная строка. CaseError при отказе."""
    c = cases.get(case_id, root)
    if c is None:
        raise cases.CaseError("нет случая %s" % case_id)
    if c.get("review"):
        raise cases.CaseError("у случая %s уже есть review (один вывод на случай)" % case_id)
    norm, errs, suspect = validate(review, c, cfg)
    if errs:
        raise cases.CaseError("; ".join(errs))
    tags = dict(c.get("tags") or {"err": [], "rule_followed": None})
    err = list(tags.get("err") or [])
    for t in map_to_err(norm["improve"]):
        if t not in err:
            err.append(t)
    patch = {"review": norm, "tags": {"err": err[:5], "rule_followed": tags.get("rule_followed")}}
    if suspect:
        patch["flags"] = sorted(set((c.get("flags") or []) + ["inj_suspect"]))
    return cases.append_update(case_id, patch, root=root, ts=ts)


def reviewer_input(case):
    """Что видит ревьюер: ситуация и решение ОТДЕЛЬНО от исхода (исход помечен «после решения»)."""
    return {"case_id": case["case_id"], "type": case.get("type"), "refs": case.get("refs"),
            "known_at_decision": {"situation": case.get("situation"), "decision": case.get("decision")},
            "after_decision": {"result": case.get("result"), "whatif": case.get("whatif")}, "verdict_candidate": suggest_verdict(case)}


def queue(all_cases, limit=20):
    """Случаи без review (для ревьюера): сначала закрытые сделки, затем остальные; без исключённых и подозрительных."""
    todo = [c for c in all_cases if not c.get("review") and not c.get("exclude_from_stats") and "inj_suspect" not in (c.get("flags") or [])
            and c.get("type") in ("trade", "skip")]
    todo.sort(key=lambda c: (0 if (c.get("result") or {}).get("status") == "closed" else 1, c.get("ts") or "", c["case_id"]))
    return todo[:limit]
