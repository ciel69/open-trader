"""Писатель случаев и миграция накопленного (08 п. 1.3, 2.2). Читает журналы (trades/ideas/hot_outcomes/decisions), ничего в них не меняет.

plan() строит список новых случаев и update-строк, apply() пишет их в cases.jsonl. Идемпотентность: case_id = детерминированная функция источника,
существующие case_id пропускаются; update (whatif/разбор) добавляется только если поля ещё нет. `./tb memory migrate [--dry-run] [--with-signals]` и
`./tb memory sync` используют один и тот же план (sync дополнительно пересчитывает уроки).
Правила: закрытые сделки -> trade (механические проверки: role=mechanics и exclude_from_stats), идеи -> idea (из заметки числа не парсятся; taken со сделкой
помечается dup_of_trade), решения skip -> skip (если есть sym), hot_outcomes без идеи -> signal только при --with-signals. Свободный текст lesson/note НЕ переносится.
"""
import json
import os

from tb import paths
from tb.core import decisions, ideas
from tb.core.tradectx import ERR_TAGS
from tb.memory import cases, features, privacy


def _fold_trades(path):
    trades, order = {}, []
    for r in ideas.read_rows(path):
        i, ev = r.get("id"), r.get("ev")
        if ev == "open" and i:
            trades[i] = {"open": dict(r), "close": None, "post": None}
            order.append(i)
        elif ev == "close" and i in trades:
            trades[i]["close"] = dict(trades[i]["close"] or {}, **r)
        elif ev == "post" and i in trades:
            trades[i]["post"] = dict(trades[i]["post"] or {}, **dict((k, v) for k, v in r.items() if k in ("err", "rule_followed", "lesson", "ts_post")))
        elif ev == "adjust" and i in trades and trades[i]["close"] is None:
            for k in ("sl", "tp"):
                if r.get(k) is not None:
                    trades[i]["open"].setdefault(k + "0", trades[i]["open"].get(k))
    return [(i, trades[i]) for i in order]


def read_sources(root=None):
    outs = {}
    for r in ideas.read_rows(ideas.outcomes_path(root)):
        if r.get("id") is not None:
            outs.setdefault((r.get("kind"), str(r["id"])), []).append(r)
    try:
        decs = [d for d in decisions.read(limit=0, root=root) if d.get("kind") == "skip"]
    except (OSError, ValueError):
        decs = []
    return {"trades": _fold_trades(paths.state("trades.jsonl", root=root)), "ideas": ideas.load_ideas(root=root), "outcomes": outs, "decisions": decs}


def _brief(root, brief_id):
    if not brief_id:
        return None
    try:
        with open(paths.briefs("%s.json" % brief_id, root=root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def _post_tags(post):
    err = [e for e in ((post or {}).get("err") or []) if e in ERR_TAGS] if isinstance((post or {}).get("err"), list) else []
    rf = (post or {}).get("rule_followed")
    return {"err": err[:5], "rule_followed": rf if rf in cases.RULE_FOLLOWED else None}


def build_trade_case(tid, t, cfg, root=None):
    op, cl, post = t["open"], t["close"], t["post"]
    tags = features.parse_tags(op.get("tags"))
    sit = features.situation_from_trade(op, post, cfg)
    sit = features.apply_brief(sit, _brief(root, op.get("brief_id")))
    mech = features.is_mechanics(op)
    why, sus = features.why_text(op.get("thesis"))
    role = "mechanics" if mech else ("manual" if tags["manual"] else "chat")
    c = {"case_id": cases.case_id_for(op.get("ts_open"), sit["sym"], tid), "type": "trade", "ts": op.get("ts_open"),
         "refs": {"journal_id": tid, "idea": op.get("idea"), "plan": op.get("plan_id") or tags["plan"]},
         "src": {"actor": "owner" if tags["manual"] else "agent", "agent_id": tags["agent_id"], "model": tags["model"], "role": role, "exp": op.get("exp") or tags["exp"],
                 "var": op.get("var") or tags["var"], "rules_ver": cfg.get("rules_ver"), "mode": op.get("mode") if op.get("mode") in cases.MODES else "live", "mem": tags["mem"]},
         "situation": sit,
         "decision": {"action": "enter", "why": why or None, "conf": None, "stop_dist_vs_range5": features.stop_vs_range5(sit)},
         "result": features.result_from_close(op, cl), "whatif": None, "review": None, "tags": _post_tags(post),
         "flags": (["retro_features"] if sit.get("retro") else []) + (["inj_suspect"] if sus else []),
         "exclude_from_stats": role in (cfg.get("exclude_roles") or [])}
    return c


IDEA_ACTION = {"taken": "enter", "skipped": "skip"}


def build_idea_case(i, outs, cfg, trade_ids):
    sit = features.situation_from_idea(i, cfg)
    why, sus = features.why_text(i.get("note"))
    status = i.get("status")
    taken = bool(i.get("trade")) and i.get("trade") in trade_ids          # идея привела к сделке (статус мог смениться на invalidated после)
    wi = features.whatif_from_outcomes(outs.get(("idea", str(i.get("id"))), []), cfg.get("whatif_h_min", 15))
    return {"case_id": cases.case_id_for(i.get("ts"), sit["sym"], "idea:%s" % i.get("id")), "type": "idea", "ts": i.get("ts"),
            "refs": {"idea": i.get("id"), "journal_id": i.get("trade") if taken else None, "outcome_id": str(i.get("id")) if wi else None},
            "src": {"actor": "agent" if i.get("src") in (None, "agent") else "owner", "role": "chat", "rules_ver": cfg.get("rules_ver"), "mode": "live"},
            "situation": sit, "decision": {"action": "enter" if taken else IDEA_ACTION.get(status, "wait"), "why": why or None, "skip_code": "OTHER" if status == "skipped" else None},
            "result": {"status": "na"}, "whatif": wi, "review": None, "tags": {"err": [], "rule_followed": None},
            "flags": (["backfill"] if i.get("tag") == "backfill" else []) + (["dup_of_trade"] if taken else []) + (["inj_suspect"] if sus else []),
            "exclude_from_stats": False}


def build_signal_case(oid, rows, cfg):
    r0 = rows[0]
    sit = features.situation_from_outcome(r0, cfg)
    wi = features.whatif_from_outcomes(rows, cfg.get("whatif_h_min", 15))
    return {"case_id": cases.case_id_for(r0.get("ts"), sit["sym"], "sig:%s" % oid), "type": "signal", "ts": r0.get("ts"),
            "refs": {"outcome_id": str(oid)[:48]}, "src": {"actor": "system", "rules_ver": cfg.get("rules_ver"), "mode": "live"}, "situation": sit,
            "decision": {"action": "wait"}, "result": {"status": "na"}, "whatif": wi, "review": None, "tags": {"err": [], "rule_followed": None}, "flags": [],
            "exclude_from_stats": False}


def build_skip_case(d, cfg):
    a = d.get("args") if isinstance(d.get("args"), dict) else {}
    sym = features.base_sym(a.get("sym"))
    if not cases.RE_SYM.match(sym or "") or a.get("side") not in cases.SIDES:
        return None
    sit = features.finalize({"sym": sym, "side": a["side"], "coin_class": features.classify_coin(sym, cfg), "setup": a.get("setup") if a.get("setup") in cases.SETUPS else None,
                             "confirm_kind": "unknown", **features.time_feats(d.get("ts"))})
    why, sus = features.why_text(d.get("why"))
    actor = str(d.get("actor") or "")
    code = a.get("skip_code") if a.get("skip_code") in cases.SKIP_CODES else "OTHER"
    return {"case_id": cases.case_id_for(d.get("ts"), sym, "dec:%s" % d.get("seq")), "type": "skip", "ts": d.get("ts"),
            "refs": {"plan": d.get("plan"), "event_seq": d.get("seq")},
            "src": {"actor": "owner" if actor == "owner" else ("agent" if actor.startswith("brain") else "system"), "agent_id": actor.split(":", 1)[1][:16] if ":" in actor else None,
                    "rules_ver": cfg.get("rules_ver"), "mode": "live"},
            "situation": sit, "decision": {"action": "skip", "why": why or None, "skip_code": code}, "result": {"status": "na"}, "whatif": None, "review": None,
            "tags": {"err": [], "rule_followed": None}, "flags": ["inj_suspect"] if sus else [], "exclude_from_stats": False}


def plan(root=None, cfg=None, with_signals=False, final_only=False):
    """-> {"new": [случай], "updates": [(case_id, patch)], "report": {...}} без записи.
    final_only (периодический писатель, WP15b): идеи в статусах idea/watching ещё не решены и случаем не становятся (decision/situation после записи неизменны);
    случай идеи создаётся, когда она получила итоговый статус (taken/skipped/expired/invalidated), whatif дописывается update-строкой позже."""
    from tb.memory import cfg as memcfg
    cfg = cfg or memcfg.load(root)
    src = read_sources(root)
    existing = dict((c["case_id"], c) for c in cases.load(root))
    rep = {"sources": {"trades_closed": 0, "ideas": len(src["ideas"]), "decisions_skip": len(src["decisions"]), "signals": 0}, "new": {}, "exists": 0,
           "excluded": [], "with_missing": 0, "errors": [], "updates": {"whatif": 0, "tags": 0}}
    built = []
    trade_ids = set()
    for tid, t in src["trades"]:
        if t["close"] is not None:
            rep["sources"]["trades_closed"] += 1
            trade_ids.add(tid)
            built.append(("trades", tid, build_trade_case(tid, t, cfg, root), t))
    for i in src["ideas"]:
        if final_only and (i.get("status") or "idea") in ideas.ACTIVE:
            continue
        built.append(("ideas", i.get("id"), build_idea_case(i, src["outcomes"], cfg, trade_ids), None))
    for d in src["decisions"]:
        c = build_skip_case(d, cfg)
        if c:
            built.append(("decisions", d.get("seq"), c, None))
    if with_signals or cfg.get("record_signals"):
        idea_ids = set(str(i.get("id")) for i in src["ideas"])
        sig = dict((k[1], v) for k, v in src["outcomes"].items() if k[0] == "event" and k[1] not in idea_ids)
        rep["sources"]["signals"] = len(sig)
        for oid in sorted(sig):
            built.append(("signals", oid, build_signal_case(oid, sorted(sig[oid], key=lambda r: r.get("h") or 0), cfg), None))
    new, updates = [], []
    for kind, sid, c, t in sorted(built, key=lambda x: (x[2].get("ts") or "", x[2]["case_id"])):
        old = existing.get(c["case_id"])
        if old is not None:
            rep["exists"] += 1
            patch = {}
            if c.get("whatif") and not old.get("whatif"):
                patch["whatif"] = c["whatif"]
                rep["updates"]["whatif"] += 1
            if t is not None and (c["tags"]["err"] or c["tags"]["rule_followed"]) and not (old.get("tags") or {}).get("err") and not (old.get("tags") or {}).get("rule_followed"):
                patch["tags"] = c["tags"]
                rep["updates"]["tags"] += 1
            if patch:
                updates.append((c["case_id"], patch))
            continue
        norm, errs = cases.validate(c)
        if errs:
            rep["errors"].append({"source": kind, "id": str(sid), "errors": errs[:3]})
            continue
        new.append(c)
        rep["new"][c["type"]] = rep["new"].get(c["type"], 0) + 1
        if (c["situation"].get("missing") or []):
            rep["with_missing"] += 1
        if c.get("exclude_from_stats"):
            rep["excluded"].append({"case_id": c["case_id"], "role": c["src"].get("role"), "journal_id": c["refs"].get("journal_id")})
    return {"new": new, "updates": updates, "report": rep}


def apply(pl, root=None):
    """Пишет план -> число записанных. Конкурирующий дубль (кто-то успел записать) тихо пропускается."""
    n = 0
    for c in pl["new"]:
        try:
            cases.append_new(c, root=root)
            n += 1
        except cases.CaseError as e:
            if not str(e).startswith("exists"):
                raise
    for cid, patch in pl["updates"]:
        cases.append_update(cid, patch, root=root)
        n += 1
    return n


def migrate(root=None, dry_run=False, with_signals=False, cfg=None, final_only=False):
    """-> отчёт миграции (сколько случаев из каких источников, с missing, исключённые, ошибки)."""
    pl = plan(root, cfg, with_signals, final_only)
    rep = dict(pl["report"], dry_run=bool(dry_run), would_write=len(pl["new"]) + len(pl["updates"]))
    rep["written"] = 0 if dry_run else apply(pl, root)
    return rep
