"""Шаги старта S2 (фактура), S4 (досье), S5 (утренний брифинг через мозг), S6 (план дня tb.dayplan/1). WP14.

LLM здесь только предлагает: ответ роли `morning_analyst` проходит строгий валидатор, лимиты обрезаются минимумом с agents.json / уровнем риска /
risk_profile. Любой сбой мозга = консервативный план дня (07 п. 2.3), старт идёт дальше (в live без мозга старт отклоняется на S5: H6).
"""
import json
import os
import re
import time

from tb import paths
from tb.control import ctlstate
from tb.core import blackout as BLACKOUT
from tb.core.filelock import atomic_write_json
from tb.lifecycle import carry as CARRY
from tb.lifecycle import days

SCHEMA = "tb.dayplan/1"
JOB_SCHEMA = "tb.lifecycle.job/1"
REGIMES = ("trend", "range", "volatile", "unclear")
BIAS = ("long", "short", "neutral")
SESSION_TYPES = ("scalp", "intraday", "swing", "position")
SESSION_RANK = dict((t, i) for i, t in enumerate(SESSION_TYPES))
DAY = "day"                    # сессия-день: горизонт сделок выбирает агент; dayplan.session.type = максимальный горизонт на сегодня (по умолчанию position)
DAY_MAX_HORIZON = "position"
SETUPS = ("level_reclaim", "level_reject", "sweep_reject", "breakout_retest", "range_fade", "trend_pullback", "none")
RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")
RE_RISK = re.compile(r"^[a-z0-9_]{2,30}$")
TOP_KEYS = {"schema", "day_id", "regime", "bias", "session", "watch", "allowed_setups", "avoid", "limits", "carry", "carried_review", "risks", "questions", "why"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _rd(path):
    return ctlstate.read_json(path)


def read_facts(root):
    """runtime/intel/facts.json (только структурные поля; формат tb.facts/1) или None. Модули tb.intel.* не импортируются (изоляция текста новостей)."""
    d = _rd(paths.runtime("intel", "facts.json", root=root))
    return d if isinstance(d, dict) else None


# ------------------------------------------------------------------ ожидание задания мозга (общий помощник)
def wait_job(ctx, d, job_id, timeout_s, beat_s=5.0):
    """Блокирующее ожидание ответа (в потоке старта). -> результат порта | None (таймаут). Пульс дня обновляется, чтобы возобновление не вмешивалось."""
    t0 = ctx.now()
    last_beat = t0
    while True:
        r = ctx.brain.poll(job_id, ctx.now())
        if r is not None:
            return r
        now = ctx.now()
        if now - t0 >= timeout_s:
            try:
                ctx.brain.cancel(job_id)
            except Exception:
                pass
            return None
        if now - last_beat >= beat_s:
            last_beat = now
            days.touch(ctx.root, d, now)
        ctx.sleep(0.5)


# ------------------------------------------------------------------ S2 фактура
def step_facts(ctx, d, args):
    """intel.refresh all синхронно (если сервис intel жив), затем проверка свежести facts.json. Всегда ok (amber в detail)."""
    now = ctx.now()
    wait_s = float(ctx.cfg["briefing"]["facts_wait_s"])
    status = None
    try:
        from tb.platform import services as S
        age, _x = S.read_hb("intel", ctx.root, now)
    except Exception:
        age = None
    if age is not None and age < 120.0:
        ctlstate.write_json(paths.runtime("intel", "refresh.req", root=ctx.root), {"source": "all", "ts": round(now, 1)})
        t0 = now
        while True:
            fx = read_facts(ctx.root)
            if isinstance(fx, dict) and _num(fx.get("ts")) and fx["ts"] >= int(t0):
                status = "fresh"
                break
            if ctx.now() - t0 >= wait_s:
                status = "refresh_timeout"
                break
            if int(ctx.now() - t0) % 5 == 0:
                days.touch(ctx.root, d, ctx.now())              # пульс: tick не должен принять живой старт за упавший
            ctx.sleep(1.0)
    else:
        status = "no_service"
    fx = read_facts(ctx.root)
    age_s = ctx.now() - fx["ts"] if isinstance(fx, dict) and _num(fx.get("ts")) else None
    if status in ("no_service", "refresh_timeout"):
        if fx is None:
            status += "+missing"
        elif age_s is not None and age_s > float(ctx.cfg["preflight"]["facts_max_age_s"]):
            status += "+stale"
    d["facts"] = {"status": status, "age_s": round(age_s, 1) if age_s is not None else None,
                  "degraded": [str(x)[:30] for x in ((fx or {}).get("degraded") or [])][:8]}
    amber = status != "fresh"
    return "ok", {"status": status, "amber": amber, "age_s": d["facts"]["age_s"]}, None


# ------------------------------------------------------------------ S4 досье
def step_brief(ctx, d, args):
    from tb.brain import brief as B
    now = ctx.now()
    b = B.build(scope="start", root=ctx.root, now=now)
    B.save(b, root=ctx.root, now=now)
    d["brief_id"] = b.get("brief_id")
    return "ok", {"brief_id": d["brief_id"], "trunc": bool(b.get("trunc"))}, None


def load_brief(root, brief_id):
    if not isinstance(brief_id, str) or not re.match(r"^B-[0-9]{8}-[0-9]{4}-[0-9]{2}$", brief_id):
        return None
    b = _rd(paths.briefs(brief_id + ".json", root=root))
    return b if isinstance(b, dict) else None


# ------------------------------------------------------------------ лимиты и допустимые символы
def allowed_symbols(root):
    out = []
    for fn, key in (("exec_limits.json", "watch_symbols"), ("watch_config.json", "symbols")):
        d = _rd(paths.config(fn, root=root))
        for s in (d or {}).get(key) or []:
            if isinstance(s, str) and RE_SYM.match(s) and s not in out:
                out.append(s)
    ins = _rd(paths.instruments_file(root=root))
    for s in ((ins or {}).get("symbols") or {}):
        if isinstance(s, str) and RE_SYM.match(s) and s not in out:
            out.append(s)
    return out


def caps(ctx, d):
    """Потолки лимитов дня: {"max_risk_usdt","size_mult","max_new_plans","symbols_any"} = min(agents.json, уровень риска, risk_profile)."""
    from tb.core import risklevels
    root = ctx.root
    prof = _rd(paths.config("risk_profile.json", root=root)) or {}
    ag = ((_rd(paths.config("agents.json", root=root)) or {}).get("agents") or {}).get(d.get("agent") or "api") or {}
    lim = ag.get("limits") if isinstance(ag.get("limits"), dict) else {}
    snap = ctx.execp.snapshot(ctx.now())
    eq = snap.get("equity") if _num(snap.get("equity")) else None
    if eq is None:
        pf = _rd(paths.state("portfolio.json", root=root)) or {}
        eq = sum(float(v.get("equity") or 0.0) for v in (pf.get("venues") or {}).values() if isinstance(v, dict)) or None
    try:
        ls = risklevels.limits_summary(root, lim or None, eq)
    except Exception:
        ls = {}
    risk_caps = [x for x in (lim.get("max_risk_usdt"), ls.get("risk_per_trade_usdt")) if _num(x) and x >= 0]
    size_caps = [x for x in (lim.get("max_size_mult"), ls.get("size_mult_cap"), 1.0) if _num(x) and x >= 0]
    return {"max_risk_usdt": min(risk_caps) if risk_caps else 0.0, "size_mult": min(size_caps), "max_new_plans": 6, "equity": eq,
            "agent_symbols": lim.get("symbols") or ["*"], "daily_loss_usdt": ls.get("daily_loss_usdt")}


def blackout_soon(facts, minutes=60):
    bl = ((facts or {}).get("calendar") or {}).get("blackout") if isinstance(facts, dict) else None
    return isinstance(bl, dict) and _num(bl.get("in_min")) and bl["in_min"] <= minutes


# ------------------------------------------------------------------ валидатор плана дня
def validate_dayplan(raw, env):
    """-> (план|None, errors[], warnings[]). errors непустой = план отвергнут целиком (консервативный). Обрезки лимитов = warnings."""
    errs, warns = [], []
    if not isinstance(raw, dict):
        return None, ["BAD_TYPE"], warns
    for k in sorted(set(raw) - TOP_KEYS):
        errs.append("EXTRA_KEY:%s" % str(k)[:20])
    if raw.get("schema") not in (None, SCHEMA):
        errs.append("BAD_SCHEMA")
    p = {"schema": SCHEMA, "day_id": env["day_id"]}
    if raw.get("regime") not in REGIMES:
        errs.append("BAD_ENUM:regime")
    if raw.get("bias") not in BIAS:
        errs.append("BAD_ENUM:bias")
    p["regime"], p["bias"] = raw.get("regime"), raw.get("bias")
    s = raw.get("session") if isinstance(raw.get("session"), dict) else {}
    st = s.get("type", env["session_type"])             # s.minutes (если модель прислала) игнорируется: срока у сессии нет
    if st not in SESSION_TYPES:
        errs.append("BAD_ENUM:session.type")
        st = env["session_type"]
    day = env["session_type"] == DAY
    if SESSION_RANK[st] > SESSION_RANK.get(DAY_MAX_HORIZON if day else env["session_type"], 1):
        warns.append("CLAMP:session.type")
        st = DAY_MAX_HORIZON if day else env["session_type"]
    p["session"] = {"type": st}
    watch, wraw = [], raw.get("watch", [])
    if not isinstance(wraw, list) or len(wraw) > 6:
        errs.append("BAD_WATCH")
        wraw = []
    for w in wraw:
        ok = (isinstance(w, dict) and set(w) <= {"sym", "side", "level", "setup", "note"} and isinstance(w.get("sym"), str) and RE_SYM.match(w["sym"])
              and w["sym"] in env["symbols"] and w.get("side") in ("long", "short") and _num(w.get("level")) and w["level"] > 0 and w.get("setup") in SETUPS)
        if not ok:
            errs.append("BAD_WATCH_ITEM")
            continue
        if (w["side"] == "long" and p["bias"] == "short") or (w["side"] == "short" and p["bias"] == "long"):
            if w["setup"] not in ("range_fade", "level_reject"):
                errs.append("WATCH_AGAINST_BIAS:%s" % w["sym"])
                continue
        watch.append({"sym": w["sym"], "side": w["side"], "level": float(w["level"]), "setup": w["setup"], "note": CARRY.clean_text(w.get("note"), 120)})
    p["watch"] = watch
    al = raw.get("allowed_setups", [])
    if not isinstance(al, list) or any(x not in SETUPS for x in al):
        errs.append("BAD_ENUM:allowed_setups")
        al = []
    p["allowed_setups"] = sorted(set(al), key=al.index)
    av = raw.get("avoid", [])
    if not isinstance(av, list) or len(av) > 10 or any(not (isinstance(x, str) and RE_SYM.match(x)) for x in av):
        errs.append("BAD_AVOID")
        av = []
    p["avoid"] = av
    lim = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    if set(lim) - {"max_new_plans", "max_risk_usdt", "symbols", "size_mult"}:
        errs.append("EXTRA_KEY:limits")
    c = env["caps"]
    mnp = lim.get("max_new_plans", env["default_max_new_plans"])
    if not (isinstance(mnp, int) and not isinstance(mnp, bool) and 0 <= mnp <= 6):
        errs.append("BAD_RANGE:max_new_plans")
        mnp = env["default_max_new_plans"]
    mr = lim.get("max_risk_usdt", c["max_risk_usdt"])
    if not (_num(mr) and mr >= 0):
        errs.append("BAD_RANGE:max_risk_usdt")
        mr = c["max_risk_usdt"]
    if mr > c["max_risk_usdt"] + 1e-12:
        warns.append("CLAMP:max_risk_usdt")
        mr = c["max_risk_usdt"]
    sm = lim.get("size_mult", c["size_mult"])
    if not (_num(sm) and 0 <= sm <= 1):
        errs.append("BAD_RANGE:size_mult")
        sm = c["size_mult"]
    if sm > c["size_mult"] + 1e-12:
        warns.append("CLAMP:size_mult")
        sm = c["size_mult"]
    syms = lim.get("symbols", list(env["symbols"])[:12])
    if not isinstance(syms, list) or len(syms) > 12 or any(not (isinstance(x, str) and RE_SYM.match(x)) for x in syms):
        errs.append("BAD_SYMBOLS")
        syms = []
    extra = [x for x in syms if x not in env["symbols"]]
    if extra:
        warns.append("CLAMP:symbols")
        syms = [x for x in syms if x in env["symbols"]]
    bo_warn = bool(env["blackout_soon"] and mnp and env.get("blackout_warn"))
    if bo_warn:              # paper + paper.blackout_mode=warn: окно новостей = предупреждение; планы остаются, размер не выше потолка, тег blackout ставит исполнитель
        cap_b = float(env.get("blackout_size") or BLACKOUT.MAX_SIZE_MULT)
        warns.append("BLACKOUT:warn_paper")
        if sm > cap_b + 1e-12:
            warns.append("CLAMP:size_mult_blackout")
            sm = cap_b
    elif env["blackout_soon"] and mnp:
        warns.append("BLACKOUT:max_new_plans=0")
        mnp = 0
    p["limits"] = {"max_new_plans": int(mnp), "max_risk_usdt": round(float(mr), 4), "symbols": syms, "size_mult": round(float(sm), 4)}
    cr = raw.get("carry", {"allow": False, "max_positions": 0})
    if not (isinstance(cr, dict) and set(cr) <= {"allow", "max_positions"} and isinstance(cr.get("allow", False), bool)
            and isinstance(cr.get("max_positions", 0), int) and not isinstance(cr.get("max_positions", 0), bool) and 0 <= cr.get("max_positions", 0) <= 2):
        errs.append("BAD_CARRY")
        cr = {"allow": False, "max_positions": 0}
    p["carry"] = {"allow": bool(cr.get("allow", False)), "max_positions": int(cr.get("max_positions", 0))}
    cr_rev = raw.get("carried_review", [])
    rev = []
    if not isinstance(cr_rev, list):
        errs.append("BAD_CARRIED_REVIEW")
        cr_rev = []
    for x in cr_rev[:6]:
        if isinstance(x, dict) and isinstance(x.get("plan"), str) and re.match(r"^PL-[A-Za-z0-9_-]{1,40}$", x["plan"]) and x.get("decision") in ("hold", "tighten", "close"):
            rev.append({"plan": x["plan"], "decision": x["decision"], "why": CARRY.clean_text(x.get("why"), 160)})
        else:
            errs.append("BAD_CARRIED_REVIEW_ITEM")
    if rev:
        p["carried_review"] = rev
    rk = raw.get("risks", [])
    if not isinstance(rk, list) or len(rk) > 8 or any(not (isinstance(x, str) and RE_RISK.match(x)) for x in rk):
        errs.append("BAD_RISKS")
        rk = []
    if bo_warn and BLACKOUT.RISK_CODE not in rk and len(rk) < 8:
        rk = list(rk) + [BLACKOUT.RISK_CODE]
    p["risks"] = rk
    q = raw.get("questions", [])
    if not isinstance(q, list) or len(q) > 3 or any(not isinstance(x, str) for x in q):
        errs.append("BAD_QUESTIONS")
        q = []
    p["questions"] = [CARRY.clean_text(x, 160) for x in q]
    p["why"] = CARRY.clean_text(raw.get("why"), 200)
    if not p["why"]:
        errs.append("EMPTY_WHY")
    return (None if errs else p), errs, warns


def conservative_plan(env, why="консервативный план: мозг недоступен или ответ невалиден"):
    c = env["caps"]
    n = env["default_max_new_plans"] if not env["blackout_soon"] else 0
    return {"schema": SCHEMA, "day_id": env["day_id"], "regime": "unclear", "bias": "neutral",
            "session": {"type": DAY_MAX_HORIZON if env["session_type"] == DAY else env["session_type"]}, "watch": [], "allowed_setups": list(env["default_setups"]), "avoid": [],
            "limits": {"max_new_plans": int(n), "max_risk_usdt": round(float(c["max_risk_usdt"]), 4), "symbols": list(env["symbols"])[:12], "size_mult": round(float(c["size_mult"]), 4)},
            "carry": {"allow": False, "max_positions": 0}, "risks": [], "questions": [], "why": why[:200]}


def make_env(ctx, d, args):
    fx = _rd(paths.runtime("intel", "facts.json", root=ctx.root))
    pol = BLACKOUT.policy(ctx.root, d.get("mode"))          # warn только при mode == paper; live/testnet всегда block
    return {"blackout_warn": pol["mode"] == "warn", "blackout_size": pol["size_mult"], "day_id": d["day_id"], "session_type": (d.get("session") or {}).get("type") or DAY,
            "caps": caps(ctx, d), "symbols": allowed_symbols(ctx.root), "blackout_soon": blackout_soon(fx), "default_setups": ctx.cfg["default_setups"],
            "default_max_new_plans": int(ctx.cfg["default_max_new_plans"])}


# ------------------------------------------------------------------ S5 брифинг
def raw_file(root, day_id):
    return os.path.join(days.day_dir(root, day_id), "dayplan_raw.json")


def step_briefing(ctx, d, args):
    """Задание `morning_analyst`. Сохраняет сырой ответ в dayplan_raw.json (если был) для S6. Мозг недоступен: live -> отказ, иначе запасной план."""
    cfg = ctx.cfg["briefing"]
    live = d.get("mode") == "live"
    try:
        os.remove(raw_file(ctx.root, d["day_id"]))
    except OSError:
        pass
    if not cfg.get("enabled", True):
        return "ok", {"fallback": "disabled", "amber": True}, None
    ok, msg = ctx.brain.available(ctx.root, ctx.now())
    if not ok:
        if live:
            return "fail", {"why": "мозг недоступен: %s" % msg}, "E_GATE"
        return "ok", {"fallback": "brain_down", "amber": True, "msg": msg}, None
    env = make_env(ctx, d, args)
    brief = load_brief(ctx.root, d.get("brief_id")) or {}
    payload = {"schema": JOB_SCHEMA, "kind": "morning", "day_id": d["day_id"], "mode": d.get("mode"), "brief": brief, "facts": _rd(paths.runtime("intel", "facts.json", root=ctx.root)),
               "owner_choice": {"session": env["session_type"]},
               "caps": {"max_risk_usdt": env["caps"]["max_risk_usdt"], "size_mult": env["caps"]["size_mult"]},
               "symbols": env["symbols"], "carried_in": d.get("carry_in") or []}
    last = None
    for attempt in range(int(cfg["attempts"])):
        try:
            job = ctx.brain.submit("morning_analyst", payload, float(cfg["timeout_s"]), ctx.now(), meta={"day_id": d["day_id"], "attempt": attempt + 1})
        except Exception as e:
            last = "submit:%s" % e.__class__.__name__
            break
        r = wait_job(ctx, d, job, float(cfg["timeout_s"]))
        if r is None:
            last = "timeout"
            continue
        if not r.get("ok") or not isinstance(r.get("output"), dict):
            last = r.get("err") or "bad_answer"
            continue
        plan, errs, _w = validate_dayplan(r["output"], env)
        if plan is None:
            last = "invalid:" + ",".join(errs[:3])
            payload = dict(payload, previous_errors=errs[:6])           # короткие КОДЫ, без цитат (07b п. 5.2)
            continue
        atomic_write_json(raw_file(ctx.root, d["day_id"]), r["output"], indent=None)
        return "ok", {"source": "agent", "attempt": attempt + 1}, None
    return "ok", {"fallback": last or "no_answer", "amber": True}, None


# ------------------------------------------------------------------ S6 план дня
def step_dayplan(ctx, d, args):
    env = make_env(ctx, d, args)
    raw = _rd(raw_file(ctx.root, d["day_id"]))
    meta = {"source": "agent", "warnings": [], "errors": []}
    plan = None
    if isinstance(raw, dict):
        plan, errs, warns = validate_dayplan(raw, env)
        meta["warnings"], meta["errors"] = warns, errs
    amber = False
    if plan is None:
        amber = True
        meta["source"] = "fallback"
        plan = conservative_plan(env)
    out = dict(plan, meta=meta)
    atomic_write_json(os.path.join(days.day_dir(ctx.root, d["day_id"]), "dayplan.json"), out, indent=1)
    lim = plan["limits"]
    env2 = make_env(ctx, d, args)
    ctlstate.write_json(paths.control_dir("day_limits.json", root=ctx.root),
                        {"day_id": d["day_id"], "ts_epoch": round(time.time(), 1), "symbols": lim["symbols"], "max_new_plans": lim["max_new_plans"], "max_risk_usdt": lim["max_risk_usdt"],
                         "size_mult": lim["size_mult"], "allowed_setups": plan["allowed_setups"], "avoid": plan["avoid"],
                         # сессия-день: dayplan.session.type = максимальный горизонт сделок на сегодня (entry_scout не выше него)
                         "max_horizon": plan["session"]["type"] if env2["session_type"] == DAY else None})
    d["dayplan"] = {"source": meta["source"], "regime": plan["regime"], "bias": plan["bias"], "session": plan["session"], "max_new_plans": lim["max_new_plans"],
                    "carry": plan["carry"], "n_watch": len(plan["watch"])}
    return "ok", {"source": meta["source"], "warnings": meta["warnings"][:6], "errors": meta["errors"][:6], "amber": amber}, None


def read_dayplan(root, day_id):
    p = _rd(os.path.join(days.day_dir(root, day_id), "dayplan.json"))
    return p if isinstance(p, dict) else None
