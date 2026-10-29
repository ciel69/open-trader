"""Каталог инструментов шлюза (WP11-A): имя, описание для модели, схема входа, право, обработчик.

Обработчики вызывают ТОЛЬКО функции пакетов мозга и ядра (brain.brief/events_cli/plan_cli/common, core.plan_schema/bus/decisions/ideas/expstats/
journal/tradectx). Нет инструментов чтения файлов, shell, сети, ключей, прямых ордеров, правки risk_profile/exec_limits, снятия KILL и смены режима
исполнителя. Вход проверяется схемой (лишние поля отбрасываются, строки ограничены), выход проходит белый список верхнего уровня и `scrub`
(управляющие символы, длина, ключи-идентификаторы, конечные числа). Единицы в описаниях: цены в котировочной валюте, риск в USDT, время в секундах/минутах.
"""
import json
import math
import re
import threading
import time
import unicodedata

from tb import paths
from tb.brain import brief as brief_mod
from tb.brain import common
from tb.brain import events_cli, plan_cli
from tb.core import bus, decisions, expstats, ideas, journal, plan_schema, portfolio, risklevels, tradectx
from tb.gateway import registry as _registry
from tb.memory import blocks as memblocks, cases as memcases, cfg as memcfg, features as memfeatures, lessons as memlessons, query as memquery, review as memreview

MAX_STR = 300
MAX_LIST = 200
MAX_DEPTH = 8
MAX_FREE_BYTES = 32 * 1024
FREE_DEPTH = 12
WAIT_MAX_S = 60
DOWN_REPEAT_S = 300.0
RE_TAG = r"^[a-z0-9._-]{2,40}$"
_KEY_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_JOURNAL_LOCK = threading.Lock()
_DOWN_AT = {}


class ToolError(Exception):
    """Ожидаемый отказ обработчика: -> {"ok": false, "errors":[{code,path,msg}]}."""

    def __init__(self, code, msg, path="", status="rejected"):
        Exception.__init__(self, msg)
        self.code, self.msg, self.path, self.status = code, msg, path, status


class Ctx(object):
    def __init__(self, root, agent, now=None, cancel=None, meta=None):
        self.root, self.agent = root, agent
        self.meta = meta if isinstance(meta, dict) else {}      # профиль/задание/роль от раннера мозга (не от модели)
        self.now = time.time() if now is None else now
        self.cancel = cancel or threading.Event()


class Tool(object):
    def __init__(self, name, desc, props, right, handler, required=(), out=(), write=False):
        self.name, self.desc, self.right, self.handler, self.write = name, desc, right, handler, write
        self.schema = {"type": "object", "properties": props, "required": list(required)}
        self.out = ("ok", "status", "errors", "warnings") + tuple(out)

    def input_schema(self):
        """JSON Schema для MCP tools/list (внутренние ключи clean/free убраны)."""
        def conv(s):
            o = {k: v for k, v in s.items() if k not in ("clean", "free", "props", "default_none")}
            if s.get("type") == "object" and "properties" in s:
                o["properties"] = {k: conv(v) for k, v in s["properties"].items()}
                o["additionalProperties"] = False
            if s.get("type") == "array" and "items" in s:
                o["items"] = conv(s["items"])
            return o
        return conv(self.schema)


# ------------------------------------------------------------------ схема входа
def _s(desc, maxlen=200, pattern=None, enum=None, free=True):
    d = {"type": "string", "description": desc, "maxLength": maxlen}
    if pattern:
        d["pattern"] = pattern
        d["free"] = False
    elif enum:
        d["enum"] = list(enum)
        d["free"] = False
    else:
        d["clean"] = True
    return d


def _i(desc, lo, hi, default=None):
    d = {"type": "integer", "description": desc, "minimum": lo, "maximum": hi}
    if default is not None:
        d["default"] = default
    return d


def _n(desc, lo, hi):
    return {"type": "number", "description": desc, "minimum": lo, "maximum": hi}


def _b(desc, default=None):
    d = {"type": "boolean", "description": desc}
    if default is not None:
        d["default"] = default
    return d


def _obj(desc):
    return {"type": "object", "description": desc, "free": True}


def _depth(o, d=0):
    if d > FREE_DEPTH:
        return d
    if isinstance(o, dict):
        return max([_depth(v, d + 1) for v in o.values()] + [d])
    if isinstance(o, list):
        return max([_depth(v, d + 1) for v in o] + [d])
    return d


def validate(schema, value, path=""):
    """-> (чистое значение, [ошибки-строки], число отброшенных лишних полей). Лишние поля отбрасываются, умолчания подставляются."""
    errs, dropped = [], [0]

    def go(sc, v, p):
        t = sc.get("type")
        if t == "object":
            if not isinstance(v, dict):
                errs.append("%s: ожидался объект" % (p or "args"))
                return None
            if "properties" not in sc:           # свободный объект (план, патч): размер и глубина, остальное проверяет plan_schema
                try:
                    raw = json.dumps(v, ensure_ascii=False)
                except (TypeError, ValueError):
                    errs.append("%s: не JSON" % p)
                    return None
                if len(raw.encode("utf-8")) > MAX_FREE_BYTES or _depth(v) > FREE_DEPTH:
                    errs.append("%s: слишком большой или глубокий объект" % p)
                    return None
                return v
            out = {}
            for k, sub in sc["properties"].items():
                if k in v and v[k] is not None:
                    r = go(sub, v[k], (p + "." if p else "") + k)
                    if r is not None:
                        out[k] = r
                elif "default" in sub:
                    out[k] = sub["default"]
            dropped[0] += len([k for k in v if k not in sc["properties"]])
            for k in sc.get("required", []):
                if k not in out:
                    errs.append("%s: обязательное поле" % ((p + "." if p else "") + k))
            return out
        if t == "string":
            if not isinstance(v, str):
                errs.append("%s: ожидалась строка" % p)
                return None
            if len(v) > sc.get("maxLength", 200):
                errs.append("%s: длиннее %d" % (p, sc.get("maxLength", 200)))
                return None
            if sc.get("clean"):
                v = common.clean_text(v, sc.get("maxLength", 200))
            if "pattern" in sc and not re.fullmatch(sc["pattern"], v):
                errs.append("%s: не соответствует %s" % (p, sc["pattern"]))
                return None
            if "enum" in sc and v not in sc["enum"]:
                errs.append("%s: допустимо %s" % (p, "|".join(sc["enum"])))
                return None
            if sc.get("minLength") and len(v) < sc["minLength"]:
                errs.append("%s: короче %d" % (p, sc["minLength"]))
                return None
            return v
        if t in ("integer", "number"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
                errs.append("%s: ожидалось число" % p)
                return None
            if t == "integer" and (isinstance(v, float) and v != int(v)):
                errs.append("%s: ожидалось целое" % p)
                return None
            v = int(v) if t == "integer" else float(v)
            if v < sc.get("minimum", -1e18) or v > sc.get("maximum", 1e18):
                errs.append("%s: вне диапазона %s..%s" % (p, sc.get("minimum"), sc.get("maximum")))
                return None
            return v
        if t == "boolean":
            if not isinstance(v, bool):
                errs.append("%s: ожидался bool" % p)
                return None
            return v
        if t == "array":
            if not isinstance(v, list) or len(v) > sc.get("maxItems", 20):
                errs.append("%s: ожидался массив до %d" % (p, sc.get("maxItems", 20)))
                return None
            out = [go(sc["items"], x, p + "[]") for x in v]
            return [x for x in out if x is not None] if not errs else None
        errs.append("%s: тип не поддержан" % p)
        return None

    if value is None:
        value = {}
    res = go(schema, value, path)
    return (res if not errs else None), errs[:6], dropped[0]


def scrub(obj, depth=0, limit=MAX_STR):
    """Выходной фильтр: строки очищены и обрезаны, ключи только идентификаторы, числа конечные, глубина и списки ограничены."""
    if depth > MAX_DEPTH:
        return None
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj if abs(obj) < 10 ** 18 else None
    if isinstance(obj, float):
        return common.num(obj)
    if isinstance(obj, str):
        return common.clean_text(obj, limit)
    if isinstance(obj, (list, tuple)):
        return [scrub(x, depth + 1, limit) for x in list(obj)[:MAX_LIST]]
    if isinstance(obj, dict):
        return {str(k): scrub(v, depth + 1, limit) for k, v in list(obj.items())[:MAX_LIST] if isinstance(k, str) and _KEY_OK.fullmatch(k)}
    return None


# ------------------------------------------------------------------ ограничения агента (только сужают)
def _preview(plan, root):
    return plan_cli.guard_preview(plan, root)


def _equity():
    try:
        return float(portfolio.total(portfolio.load()))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _walk_actions(o):
    if isinstance(o, dict):
        if o.get("action") == "add" or o.get("kind") == "add":
            yield o
        for v in o.values():
            for x in _walk_actions(v):
                yield x
    elif isinstance(o, list):
        for v in o:
            for x in _walk_actions(v):
                yield x


def check_scope(agent, symbol, mode):
    lim = agent.limits
    if "*" not in lim["symbols"] and symbol not in lim["symbols"]:
        raise ToolError("E_SYMBOL_FORBIDDEN", "символ %s не разрешён агенту" % common.clean_text(str(symbol), 20), "symbol", "denied")
    if mode not in lim["modes"]:
        raise ToolError("E_MODE_FORBIDDEN", "режим %s не разрешён агенту (разрешено: %s)" % (common.clean_text(str(mode), 10), ",".join(lim["modes"])), "mode", "denied")


def check_plan(ctx, plan):
    """Ограничения реестра для нормализованного плана (submit/amend). -> guard_preview (или None)."""
    lim = ctx.agent.limits
    check_scope(ctx.agent, plan.get("symbol"), plan.get("mode"))
    sm = (plan.get("risk") or {}).get("size_mult", 1.0)
    if sm > lim["max_size_mult"] + 1e-9:
        raise ToolError("E_SIZE_MULT", "risk.size_mult %.2f > допустимого агенту %.2f" % (sm, lim["max_size_mult"]), "risk.size_mult", "denied")
    if (plan.get("entry") or {}).get("how", {}).get("type") == "adopt":
        raise ToolError("E_FORBIDDEN_FIELD", "how.type=adopt недоступен через шлюз", "entry.how.type", "denied")
    adds = any(True for _ in _walk_actions([plan.get("manage"), plan.get("shadow_variants")]))
    if adds and not lim["allow_add"]:
        raise ToolError("E_ADD_FORBIDDEN", "добор/пирамидинг запрещён агенту", "manage", "denied")
    if adds and not risklevels.pyramid_policy(ctx.root, agent_limits=lim)["allowed"]:
        raise ToolError("E_ADD_FORBIDDEN", "добор не разрешён политикой пирамидинга (уровень риска, exec_limits или агент)", "manage", "denied")
    setup = (plan.get("tags") or {}).get("setup")
    if setup:
        ok_su, code_su = risklevels.setup_allowed(setup, root=ctx.root)
        if not ok_su:
            raise ToolError(code_su, "сетап %s агрессивнее выбранного уровня риска" % common.clean_text(str(setup), 30), "tags.setup", "denied")
    pv = _preview(plan, ctx.root)
    risk = common.num((pv or {}).get("risk_usdt"))
    if risk is None:
        raise ToolError("E_RISK_UNKNOWN", "риск плана в USDT не посчитан (нет цены/профиля): план отклонён шлюзом", "risk", "denied")
    risk *= sm
    if risk > lim["max_risk_usdt"] + 1e-9:
        raise ToolError("E_RISK_LIMIT", "риск плана %.2f USDT > лимита агента %.2f" % (risk, lim["max_risk_usdt"]), "risk", "denied")
    eq = _equity()
    if lim["max_risk_pct"] > 0 and (eq <= 0 or 100.0 * risk / eq > lim["max_risk_pct"] + 1e-9):
        raise ToolError("E_RISK_PCT", "риск плана больше %.1f%% капитала (или капитал неизвестен)" % lim["max_risk_pct"], "risk", "denied")
    return pv


def _plan_scope(ctx, plan_id):
    spec, rt = common.load_plan(plan_id, root=ctx.root)
    if spec is None:
        raise ToolError("E_NOT_FOUND", "план не найден", "id", "not_found")
    check_scope(ctx.agent, spec.get("symbol"), spec.get("mode", "live"))
    return spec, rt or {}


# ------------------------------------------------------------------ чтение
def h_status(ctx, a):
    b = brief_mod.build("event", since=bus.last_seq(ctx.root), root=ctx.root, now=ctx.now, consumer=ctx.agent.consumer)
    out = {"ok": True, "ts": b.get("ts")}
    for k in ("system", "account", "session", "room", "market"):
        if b.get(k) is not None:
            out[k] = b[k]
    out["positions_n"], out["plans_n"] = len(b.get("positions") or []), len(b.get("plans") or [])
    st = bus.stats(ctx.agent.consumer, root=ctx.root, now=ctx.now)
    out["events"] = {k: st[k] for k in ("last_seq", "acked", "backlog", "unacked_wake")}
    out["actor"] = ctx.agent.actor
    return out


def h_brief(ctx, a):
    b = brief_mod.build(a.get("scope", "start"), since=a.get("since"), budget_tokens=a.get("budget_tokens"), root=ctx.root, now=ctx.now,
                        consumer=ctx.agent.consumer, seqs=a.get("seqs"), sym=a.get("sym"))
    brief_mod.save(b, root=ctx.root, now=ctx.now)
    b["ok"] = True
    return b


def _events_out(evs):
    return {"ok": True, "events": evs, "last_seq": 0}


def h_events(ctx, a):
    c = ctx.agent.consumer
    bus.cursor_init(c, root=ctx.root)
    evs = events_cli.read_events(c, a.get("after"), a.get("limit", 20), a.get("wake_only", False), root=ctx.root)
    if evs and a.get("after") is None:
        bus.cursor_delivered(c, max(e["seq"] for e in evs), root=ctx.root)
    return {"ok": True, "events": evs, "last_seq": bus.last_seq(ctx.root), "acked": bus.cursor_get(c, root=ctx.root)["acked"]}


def h_wait_events(ctx, a):
    c, root = ctx.agent.consumer, ctx.root
    cur = bus.cursor_init(c, root=root)
    after = a.get("after")
    after = cur["delivered"] if after is None else after
    timeout = min(a.get("timeout_s", 30), WAIT_MAX_S)
    limit, wake_only = a.get("limit", 20), a.get("wake_only", True)
    end = time.monotonic() + timeout
    while True:
        evs = [e for e in (events_cli._line(x) for x in bus.read(after_seq=after, limit=limit, wake_only=wake_only, root=root)) if e]
        if evs:
            if a.get("after") is None:
                bus.cursor_delivered(c, max(e["seq"] for e in evs), root=root, force=True)
            return {"ok": True, "events": evs, "last_seq": bus.last_seq(root), "timed_out": False}
        down = events_cli.exec_down_event(root=root)
        if down is not None:
            key = (root, c)
            if time.time() - _DOWN_AT.get(key, 0.0) >= DOWN_REPEAT_S:
                _DOWN_AT[key] = time.time()
                return {"ok": True, "events": [down], "last_seq": bus.last_seq(root), "timed_out": False}
        else:
            _DOWN_AT.pop((root, c), None)
        if ctx.cancel.is_set() or time.monotonic() >= end:
            return {"ok": True, "events": [], "last_seq": bus.last_seq(root), "timed_out": not ctx.cancel.is_set(), "cancelled": ctx.cancel.is_set()}
        ctx.cancel.wait(min(0.5, max(0.05, end - time.monotonic())))


def h_ack(ctx, a):
    try:
        cur = bus.ack(ctx.agent.consumer, a["seq"], root=ctx.root)
    except ValueError as e:
        raise ToolError("E_ACK", str(e), "seq")
    return {"ok": True, "acked": cur["acked"], "delivered": cur["delivered"]}


def h_list_plans(ctx, a):
    items, nbad = common.load_plans(ctx.root)
    rows = [plan_cli._plan_row(i) for i in items]
    st, mode = a.get("status"), a.get("mode")
    rows = [r for r in rows if (not st or r["status"] == st) and (not mode or r["mode"] == mode)]
    out = {"ok": True, "plans": rows[-a.get("limit", 50):], "queued": plan_cli._queued(ctx.root)}
    if nbad:
        out["skipped_bad"] = nbad
    return out


def h_show_plan(ctx, a):
    pid = a["id"]
    spec, rt = common.load_plan(pid, root=ctx.root)
    if spec is None:
        raise ToolError("E_NOT_FOUND", "план не найден или spec.json нечитаем", "id", "not_found")
    if a.get("rev") is not None:
        spec = plan_cli._revision(ctx.root, pid, a["rev"])
        if spec is None:
            raise ToolError("E_NOT_FOUND", "ревизия %d не найдена" % a["rev"], "rev", "not_found")
    body = {k: v for k, v in spec.items() if k != "thesis"}
    out = {"ok": True, "id": pid, "spec": common.clean_struct(body), "own_thesis": common.clean_text(spec.get("thesis"), 280) or None,
           "rt": common.clean_struct(rt or {})}
    if a.get("events"):
        evs = [bus.clean_out(e) for e in bus.read(after_seq=0, limit=0, root=ctx.root) if e.get("plan") == pid]
        out["events"] = [e for e in evs if e][-plan_cli.SHOW_EVENTS:]
    if a.get("decisions"):
        out["decisions"] = plan_cli._decisions(ctx.root, pid, plan_cli.SHOW_DECISIONS)
    return {k: v for k, v in out.items() if v is not None}


def h_decisions(ctx, a):
    plan = a.get("plan")
    rows = decisions.read(after_seq=a["after"], plan=plan, limit=a.get("limit", 20), root=ctx.root) if a.get("after") is not None \
        else decisions.last(a.get("limit", 20), plan=plan, root=ctx.root)
    out = []
    for r in rows:
        d = {k: common.clean_struct(r.get(k)) if k not in ("actor", "what", "res", "args") else r.get(k) for k in plan_cli.DECISION_KEYS if k in r}
        d["why"] = common.clean_text(r.get("why"), common.MAX_REASON_LEN)
        out.append(d)
    return {"ok": True, "decisions": out}


def h_list_ideas(ctx, a):
    st = a.get("status", "active")
    rows = ideas.load_ideas(ctx.root)
    if st == "active":
        rows = [i for i in rows if (i.get("status") or "idea") in ideas.ACTIVE]
    elif st != "all":
        rows = [i for i in rows if i.get("status") == st]
    out = []
    for i in rows[-a.get("limit", 20):]:
        out.append({"id": common.ident(i.get("id")), "sym": i.get("sym") if common.RE_SYMBOL.fullmatch(str(i.get("sym"))) else None,
                    "side": common.ident(i.get("side")), "level": common.num(i.get("level")), "stop": common.num(i.get("stop")),
                    "tp": common.num(i.get("tp")), "tag": common.ident(i.get("tag")), "status": common.ident(i.get("status")),
                    "note": common.clean_text(i.get("note"), 160), "ts": common.ident(i.get("ts"), None) or common.clean_text(i.get("ts"), 24)})
    return {"ok": True, "ideas": out}


def _fix_journal(root):
    p = paths.state("trades.jsonl", root=root)
    if journal.PATH != p:
        journal.PATH = p


def h_stats(ctx, a):
    mode, min_n = a.get("mode", "all"), a.get("min_n", expstats.MIN_N)
    by = a.get("by", "exp")
    trades = expstats.load_trades(mode, ctx.root, with_rules=(by == "rule"))
    rows = expstats.group_stats(trades, by, min_n)
    keep = ("key", "mode", "n", "mark", "win_rate", "avg_r", "se_r", "pf", "pf_inf", "fees", "fees_share", "p", "expectancy_usdt")
    out = {"ok": True, "by": by, "mode": mode, "min_n": min_n, "k": len(rows), "alpha_adj": expstats.bonferroni(len(rows)),
           "groups": [{k: r.get(k) for k in keep if k in r} for r in rows[:40]]}
    with _JOURNAL_LOCK:
        _fix_journal(ctx.root)
        try:
            tr, order = journal._load()
        except (OSError, ValueError, KeyError):
            tr, order = {}, []
    closed = [tr[i] for i in order if tr[i].get("ts_close") and tr[i].get("mode") == "live" and tr[i].get("pnl_usdt") is not None]
    rep = journal.analyze(closed, journal.MIN_BUCKET)
    out["journal"] = {k: rep.get(k) for k in ("n", "summary", "err_tags", "untagged", "rule_followed", "mae_mfe")}
    return out


# ------------------------------------------------------------------ запись: идеи и разбор
def h_add_idea(ctx, a):
    sym = a["sym"]
    check_scope(ctx.agent, sym, ctx.agent.limits["modes"][0])
    for k in ("level", "stop", "tp"):
        if a.get(k) is not None and a[k] <= 0:
            raise ToolError("E_RANGE", "%s должен быть > 0" % k, k)
    side, lvl, stop = a["side"], a.get("level"), a.get("stop")
    if side in ("long", "short") and lvl is not None and stop is not None and ((side == "long" and stop >= lvl) or (side == "short" and stop <= lvl)):
        raise ToolError("E_STOP_SIDE", "стоп с неверной стороны от уровня", "stop")
    rec = ideas.add_idea(sym, side, a["note"], level=lvl, stop=stop, tp=a.get("tp"), tag=a.get("tag"), src=ctx.agent.actor, root=ctx.root, now=ctx.now,
                         mode=ctx.agent.limits["modes"][0])
    return {"ok": True, "idea_id": rec["id"]}


def h_post_mortem(ctx, a):
    f = {}
    if a.get("err"):
        try:
            f["err"] = tradectx.parse_err(",".join(a["err"]))
        except ValueError as e:
            raise ToolError("E_ERR_TAG", str(e)[:200], "err")
    if a.get("rule_followed"):
        f["rule_followed"] = a["rule_followed"]
    if a.get("lesson"):
        f["lesson"] = a["lesson"]
    if not f:
        raise ToolError("E_EMPTY", "нечего писать: задай err, rule_followed или lesson")
    with _JOURNAL_LOCK:
        _fix_journal(ctx.root)
        trades, _ = journal._load()
        t = trades.get(a["trade_id"])
        if not t:
            raise ToolError("E_NOT_FOUND", "нет записи сделки", "trade_id", "not_found")
        if not t.get("ts_close"):
            raise ToolError("E_NOT_CLOSED", "сделка ещё открыта: разбор пишется после закрытия", "trade_id")
        journal._append(dict({"ev": "post", "id": a["trade_id"], "ts_post": journal._now()}, **f))
    return {"ok": True, "trade_id": a["trade_id"], "fields": sorted(f)}


# ------------------------------------------------------------------ память случаев (WP15b, 08 п. 3.5): чтение и запись разбора ревьюера
MEM_KEYS = tuple(memquery.QUERY_FEATURES)


def _mem_mode(ctx):
    """Режим для памяти: режим исполнителя (paper/live/testnet), иначе по правам агента (не только paper -> live: скрыто без разрешения владельца)."""
    m = ((common.exec_health(root=ctx.root, now=ctx.now).get("data") or {}).get("mode"))
    if m in memcases.MODES:
        return m
    return "paper" if ctx.agent.limits["modes"] == ["paper"] else "live"


def _mem_cfg(ctx):
    try:
        return memcfg.load(ctx.root)
    except ValueError:
        raise ToolError("E_MEMORY_CFG", "config/memory.json не читается", status="unavailable")


def _mem_gate(ctx, cfg, tool):
    """Память выключена агенту (флаг A/B agents_off / enabled=false) или частота вызовов выше лимита -> отказ."""
    if not memblocks.enabled(cfg, ctx.agent.id, ctx.agent.consumer):
        raise ToolError("E_MEMORY_OFF", "память выключена для этого агента", status="denied")
    from tb.gateway import audit
    lim = int((cfg.get("tool") or {}).get("calls_per_hour", 30))
    if audit.count_recent(ctx.agent.id, tool, 3600.0, root=ctx.root, now=ctx.now) >= lim:
        raise ToolError("E_RATE_MEMORY", "не больше %d вызовов %s в час" % (lim, tool), status="denied")


def _plan_spec_for(ctx, sym):
    """Ближайший армированный/черновой план символа (для признаков по умолчанию) или None."""
    items, _ = common.load_plans(ctx.root)
    for it in items:
        sp = it["spec"]
        if common.plan_status(it) in brief_mod.ACTIVE_PLAN and memfeatures.base_sym(sp.get("symbol")) == sym:
            return sp
    return None


def h_similar(ctx, a):
    cfg = _mem_cfg(ctx)
    _mem_gate(ctx, cfg, "get_similar_cases")
    mode = _mem_mode(ctx)
    sym = a.get("sym")
    if sym:
        check_scope(ctx.agent, sym, mode if mode in ctx.agent.limits["modes"] else ctx.agent.limits["modes"][0])
    if not memblocks.shown(mode, cfg):
        return {"ok": True, "mode": mode, "warn": "MEMORY_HIDDEN_LIVE", "similar": {"n": 0}, "cases": [], "lessons": []}
    feats = dict((k, v) for k, v in (a.get("features") or {}).items() if k in MEM_KEYS)
    spec = None if feats or not sym else _plan_spec_for(ctx, sym)
    try:
        out = memblocks.similar_payload(ctx.root, mode, sym, a.get("side"), a.get("setup"), feats, a.get("k"), a.get("scope"), ctx.now, cfg, spec=spec)
    except ValueError as e:
        raise ToolError("E_FEATURE", str(e)[:160], "features")
    out["ok"] = True
    return out


def h_lessons(ctx, a):
    cfg = _mem_cfg(ctx)
    _mem_gate(ctx, cfg, "list_lessons")
    mode = _mem_mode(ctx)
    if not memblocks.shown(mode, cfg):
        return {"ok": True, "warn": "MEMORY_HIDDEN_LIVE", "n": 0, "lessons": []}
    out = memblocks.lessons_payload(ctx.root, a.get("status", "active"), a.get("limit"), ctx.now, cfg)
    out["ok"] = True
    return out


def h_case_review(ctx, a):
    """Вывод ревьюера -> память. Пишет только роль reviewer (раннер ставит meta.role) или агент с ролью postmortem; case_id из meta раннера (задание) обязан совпасть."""
    if ctx.meta.get("role") != "reviewer" and ctx.agent.role != "postmortem":
        raise ToolError("E_NOT_REVIEWER", "вывод по случаю пишет только роль reviewer", "tool", "denied")
    want = ctx.meta.get("case_id")
    if want is not None and want != a["case_id"]:
        raise ToolError("E_CASE_MISMATCH", "case_id не совпадает с заданием ревьюера", "case_id", "denied")
    cfg = _mem_cfg(ctx)
    try:
        row = memreview.add_review(ctx.root, a["case_id"], memreview.normalize_hint(a["review"]), cfg, ts=memcases.iso(ctx.now))
    except memcases.CaseError as e:
        msg = str(e)
        code = "E_NOT_FOUND" if msg.startswith("нет случая") else ("E_REVIEW_EXISTS" if "уже есть review" in msg else "E_REVIEW")
        raise ToolError(code, common.clean_text(msg, 200), "review", "not_found" if code == "E_NOT_FOUND" else "rejected")
    out = {"ok": True, "case_id": a["case_id"], "rev": row["rev"], "verdict": (row.get("review") or {}).get("verdict")}
    hint = (row.get("review") or {}).get("lesson_hint")
    if hint:
        try:
            les = memlessons.from_hint(hint, ctx.root, memcases.load(ctx.root), cfg, ctx.now)
            out["lesson"] = les["lesson_id"] if les else None
        except (OSError, ValueError, KeyError, TypeError):
            out["lesson"] = None
    return out


# ------------------------------------------------------------------ план: команды в _inbox (единственный путь к торговле)
def _need_reason(a, optional=False):
    r = common.clean_text(a.get("reason") or "", common.MAX_REASON_LEN)
    if not r and not optional:
        raise ToolError("E_REASON", "reason обязателен (<= 200 символов)", "reason")
    return r


def _send(ctx, op, a, plan_id=None, rev=None, payload=None, reason="", wait=None):
    if _kill(ctx.root) and op == "submit":
        raise ToolError("E_KILL", "активен KILL: новые планы не принимаются", "", "denied")
    cmd = {"actor": ctx.agent.actor, "op": op}
    if plan_id:
        cmd["plan_id"] = plan_id
    if rev is not None:
        cmd["rev_expected"] = rev
    if payload is not None:
        cmd["payload"] = payload
    cmd["reason"] = reason
    if a.get("event_seq") is not None:
        cmd["event_seq"] = a["event_seq"]
    if a.get("brief_id"):
        cmd["brief_id"] = a["brief_id"]
    w = min(a.get("wait_s", 10), 30)
    return plan_cli.send(cmd, w, root=ctx.root)


def _kill(root):
    import os
    return os.path.exists(paths.kill_file(root=root))


def _issues(iss):
    return plan_cli._items(iss, ("code", "path", "msg"))


def _need_trading(ctx, mode):
    """Торговый флаг дня (07 п. 2.1): не-paper планы только при разрешающем флаге; нечитаемый флаг = отказ."""
    ok, code = _registry.trading_allowed(ctx.root, mode, ctx.now, ctx.agent.id)
    if not ok:
        raise ToolError(code, _registry.TRADING_CODES.get(code, "торговля недоступна"), "mode", "denied")


def _stamp_tags(raw, ctx):
    """Теги agent.<id> и model.<профиль> ставит шлюз сам (из контекста вызова, не от модели): 07b п. 6.6. Чужие agent./model. из плана убираются."""
    tags = dict(raw.get("tags")) if isinstance(raw.get("tags"), dict) else {}
    extra = [t for t in (tags.get("extra") or []) if isinstance(t, str) and not t.startswith(("agent.", "model.", "mem."))]
    mine = ["agent.%s" % ctx.agent.id]
    prof = ctx.meta.get("profile")
    if isinstance(prof, str):
        mine.append("model.%s" % prof)
    try:         # mem.on|off: была ли память доступна агенту при решении (разрез `exp stats --by mem`, поле src.mem случая); конфиг памяти нечитаем = метки нет
        mode = raw.get("mode") or ("paper" if ctx.agent.limits["modes"] == ["paper"] else "live")
        mine.append("mem.%s" % memblocks.mem_state(memcfg.load(ctx.root), ctx.agent.id, mode))
    except ValueError:
        pass
    mine = [t for t in mine if re.match(RE_TAG, t)]
    tags["extra"] = (extra + mine)[-5:]
    raw["tags"] = tags


def h_submit(ctx, a):
    raw = dict(a["plan"])
    for k in plan_schema.EXEC_FIELDS:
        raw.pop(k, None)
    _stamp_tags(raw, ctx)
    lim = ctx.agent.limits
    if "mode" not in raw and lim["modes"] == ["paper"]:
        raw["mode"] = "paper"
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > plan_schema.MAX_PLAN_BYTES:
        raise ToolError("E_TOO_LARGE", "план больше %d байт" % plan_schema.MAX_PLAN_BYTES)
    th = raw.get("thesis")
    reason = _need_reason(a, optional=isinstance(th, str) and len(th.strip()) >= 10)
    plan, errs, warns, ready = plan_cli.precheck(raw, ctx.now)
    if not ready:
        raise ToolError("E_NOT_READY", "схема плана недоступна", status="unavailable")
    if errs:
        return {"ok": False, "status": "rejected_local", "errors": _issues(errs), "warnings": _issues(warns)}
    check_scope(ctx.agent, plan.get("symbol"), plan.get("mode"))      # права агента раньше флага дня: чужой символ/режим отказ по своей причине
    _need_trading(ctx, plan.get("mode"))
    check_plan(ctx, plan)
    n = _plans_last_hour(ctx)
    if n >= lim["plans_per_hour"]:
        raise ToolError("E_RATE_PLANS", "лимит планов в час: %d" % lim["plans_per_hour"], "", "denied")
    resp = _send(ctx, "submit", a, payload=raw, reason=reason)
    resp["gateway_actor"] = ctx.agent.actor
    return resp


def _plans_last_hour(ctx):
    from tb.gateway import audit
    return audit.count_recent(ctx.agent.id, "submit_plan", 3600.0, root=ctx.root, now=ctx.now)


def h_amend(ctx, a):
    spec, rt = _plan_scope(ctx, a["id"])
    reason = _need_reason(a)
    patch = dict(a["patch"])
    for k in plan_schema.EXEC_FIELDS:
        patch.pop(k, None)
    if not patch:
        raise ToolError("E_SCHEMA", "патч пуст", "patch")
    status = common.plan_status({"spec": spec, "rt": rt})
    pos = status in ("entered", "managing")
    p = rt.get("pos") or {}
    cur, avg = common.num(p.get("cur_stop")), common.num(p.get("avg"))
    sign = 1 if spec.get("side") == "long" else -1
    live = {"exch_stop": cur if pos else None, "position": pos, "stop_at_or_better_be": bool(pos and cur and avg and (cur - avg) * sign >= 0)}
    try:
        new, iss = plan_schema.amend(spec, patch, status, live, a["rev"], ctx.now)
    except NotImplementedError:
        raise ToolError("E_NOT_READY", "схема плана недоступна", status="unavailable")
    errs = [i for i in iss if i.get("severity") == "error"]
    if new is None or errs:
        return {"ok": False, "status": "rejected_local", "errors": _issues(errs), "warnings": _issues([i for i in iss if i.get("severity") != "error"])}
    check_plan(ctx, new)
    ok_tr, code_tr = _registry.trading_allowed(ctx.root, new.get("mode"), ctx.now, ctx.agent.id)
    if not ok_tr and not (pos and _tightening(spec, new)):
        raise ToolError(code_tr, "%s: ослабляющее изменение плана недоступно (ужесточение разрешено)" % _registry.TRADING_CODES.get(code_tr, "торговля недоступна"), "mode", "denied")
    return _send(ctx, "amend", a, plan_id=a["id"], rev=a["rev"], payload=patch, reason=reason)


def _tightening(old, new):
    """Изменение плана в позиции, которое не создаёт и не ослабляет риск: размер не вырос, добор не появился (стопы ослаблять plan_schema.amend не даёт)."""
    sm0, sm1 = (old.get("risk") or {}).get("size_mult", 1.0), (new.get("risk") or {}).get("size_mult", 1.0)
    if sm1 > sm0 + 1e-12:
        return False
    return sum(1 for _ in _walk_actions([new.get("manage")])) <= sum(1 for _ in _walk_actions([old.get("manage")]))


def h_stop(ctx, a):
    spec, rt = _plan_scope(ctx, a["id"])
    reason = _need_reason(a)
    to = a["to"]
    if to != "be":
        to = common.num(to)
        if to is None or to <= 0:
            raise ToolError("E_RANGE", "to: положительная цена или be", "to")
        cur = common.num((rt.get("pos") or {}).get("cur_stop")) or common.num((spec.get("stop") or {}).get("price"))
        if cur is not None and not (to > cur if spec.get("side") == "long" else to < cur):
            raise ToolError("E_STOP_WIDEN", "новый стоп %s не строже текущего %s: расширять стоп нельзя" % (to, cur), "to")
    return _send(ctx, "stop", a, plan_id=a["id"], rev=a["rev"], payload={"to": to}, reason=reason)


def h_close(ctx, a):
    _plan_scope(ctx, a["id"])
    return _send(ctx, "close", a, plan_id=a["id"], payload={"frac": a.get("frac", 1.0)}, reason=_need_reason(a))


def _simple(op):
    def run(ctx, a):
        _plan_scope(ctx, a["id"])
        return _send(ctx, op, a, plan_id=a["id"], reason=_need_reason(a))
    return run


# ------------------------------------------------------------------ сам каталог
PLAN_ID = {"type": "string", "description": "Plan id, e.g. PL-1028-SOL-01.", "pattern": common.RE_PLAN_ID.pattern, "maxLength": 40, "free": False}
REASON = _s("Short reason for the decision, max 200 chars (logged in the decision journal).", 200)
WAIT = _i("Seconds to wait for the executor answer (0..30). If it does not answer the command stays queued: do NOT resend.", 0, 30, 10)
EVSEQ = _i("Bus event seq that triggered this decision (traceability).", 0, 10 ** 12)
BRIEF = _s("Brief id (B-...) the decision is based on.", 40, pattern=r"^B-[0-9]{8}-[0-9]{4}-[0-9]{2}$")
CMD_COMMON = {"reason": REASON, "wait_s": WAIT, "event_seq": EVSEQ, "brief_id": BRIEF}
TF_AFTER = _i("Event seq; return events strictly after it. Default: after the consumer cursor.", 0, 10 ** 12)
ERR_TAGS = sorted(tradectx.ERR_TAGS)

def _mem_feature_props():
    """Схема признаков запроса get_similar_cases: белый список memory.query.FEATURE_TYPES, диапазоны и enum из memory.cases (лишние имена отбрасываются шлюзом)."""
    props = {}
    for k in memquery.QUERY_FEATURES:
        t = memquery.FEATURE_TYPES[k]
        if t == "bool":
            props[k] = _b("%s (true|false)" % k)
        elif t == "enum":
            props[k] = _s("%s (enum)" % k, 20, enum=memcases.SITUATION[k][1])
        elif t == "int":
            spec = memcases.SITUATION.get(k)
            props[k] = _i("%s (integer)" % k, spec[1] if spec else 0, spec[2] if spec else 1000)
        else:
            spec = memcases.SITUATION.get(k)
            props[k] = _n("%s (number)" % k, spec[1] if spec else -1e6, spec[2] if spec else 1e6)
    return props


TOOLS = {}


def _reg(t):
    TOOLS[t.name] = t


_reg(Tool("get_status", "System status: executor alive/mode/hold, KILL flag, scanner/feed freshness, account (equity and free in USDT, day PnL, drawdown %), "
          "session, risk room (headroom) and event backlog. Read-only, no arguments.", {}, "read", h_status,
          out=("ts", "system", "account", "session", "room", "market", "positions_n", "plans_n", "events", "actor")))
_reg(Tool("get_brief", "Compact decision brief (JSON, token-budgeted). scope=start: full picture; event: after a wake event; plan:PL-id: one plan in depth. "
          "Contains only numbers and whitelisted fields; no third-party text.",
          {"scope": dict(_s("start | event | plan:<PL-id>", 60, pattern=r"^(start|event|plan:PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3})$"), default="start"),
           "since": _i("For scope=event: events strictly after this seq.", 0, 10 ** 12),
           "seqs": {"type": "array", "description": "For scope=event: seqs of the job's own events; they are always kept in `recent`.", "maxItems": 40,
                    "items": {"type": "integer", "minimum": 0, "maximum": 10 ** 12}},
           "sym": _s("For scope=event: symbol of the job; it goes first in struct/levels.", 20, pattern=common.RE_SYMBOL.pattern),
           "budget_tokens": _i("Approximate token budget 200..6000.", 200, 6000)}, "read", h_brief,
          out=("brief_id", "ts", "scope", "trunc", "cut", "system", "account", "session", "room", "positions", "plans", "pending", "market", "hot",
               "ideas", "levels", "struct", "struct_missing", "day", "limits", "risk_level", "economics", "experiments", "paper_history", "pause", "flash", "flow", "playbook", "intel", "news_digest", "recent", "since_last", "lessons", "memory", "checklist",
               "plan", "plans_bad", "missing")))
_reg(Tool("get_events", "Read bus events (whitelisted fields, no third-party text) without waiting. By default events after the last acked seq (unacked ones are "
          "re-delivered). Does not acknowledge: call ack_events after handling.",
          {"after": TF_AFTER, "limit": _i("Max events 1..50.", 1, 50, 20), "wake_only": _b("Only wake-priority events.", False)}, "read", h_events,
          out=("events", "last_seq", "acked")))
_reg(Tool("wait_events", "Long-poll for new wake events (up to 60 s, default 30). Returns early on an event or when the executor goes silent (exec_down, at most "
          "every 5 min). Returns events=[] and timed_out=true on timeout; call again. Does not acknowledge.",
          {"timeout_s": _i("Seconds to wait 1..60.", 1, 60, 30), "after": TF_AFTER, "limit": _i("Max events 1..50.", 1, 50, 20),
           "wake_only": _b("Only wake-priority events (default true).", True)}, "read", h_wait_events,
          out=("events", "last_seq", "timed_out", "cancelled")))
_reg(Tool("ack_events", "Acknowledge bus events up to and including seq (the consumer cursor of this agent). Call after the decision on those events is made.",
          {"seq": _i("Highest handled event seq.", 0, 10 ** 12)}, "events.ack", h_ack, required=["seq"], out=("acked", "delivered"), write=True))
_reg(Tool("list_plans", "List trading plans: id, symbol, side, mode, status, rev, hold; plus commands still queued for the executor.",
          {"status": _s("Filter by status.", 12, enum=plan_schema.STATUSES), "mode": _s("paper | live", 5, enum=("paper", "live")),
           "limit": _i("Max rows 1..100.", 1, 100, 50)}, "read", h_list_plans, out=("plans", "queued", "skipped_bad")))
_reg(Tool("show_plan", "Show one plan: whitelisted spec (numbers and identifiers), own thesis, runtime state; optionally recent events and decisions.",
          {"id": PLAN_ID, "rev": _i("Show an older revision.", 1, 10 ** 6), "events": _b("Include recent plan events.", False),
           "decisions": _b("Include decision journal rows of the plan.", False)}, "read", h_show_plan, required=["id"],
          out=("id", "spec", "own_thesis", "rt", "events", "decisions")))
_reg(Tool("submit_plan", "Submit a new trading plan (schema tb.plan/1; see the plan schema/template). Validated offline, checked against this agent's limits "
          "(symbols, mode, risk in USDT and % of equity, size_mult, no pyramiding), then queued for the executor which runs guard. You never set size, leverage or "
          "margin. Entry/stop/targets are prices in quote currency. Answer: ok true/false, or ok null = queued (do not resend).",
          dict({"plan": _obj("The plan object (tb.plan/1)."), "reason": REASON}, **{k: v for k, v in CMD_COMMON.items() if k != "reason"}), "plan.submit", h_submit,
          required=["plan"], out=("cmd_id", "plan_id", "rev", "guard_preview", "executor", "gateway_actor"), write=True))
_reg(Tool("amend_plan", "Amend a plan with a JSON Merge Patch against revision rev. Stops can only be tightened in a position; risk can only go down. Validated "
          "offline and again by the executor.",
          dict({"id": PLAN_ID, "rev": _i("Current plan rev (stale rev is rejected).", 1, 10 ** 6), "patch": _obj("JSON Merge Patch (RFC 7386) of plan fields.")}, **CMD_COMMON),
          "plan.manage", h_amend, required=["id", "rev", "patch", "reason"], out=("cmd_id", "plan_id", "rev", "guard_preview", "executor"), write=True))
_reg(Tool("stop_plan", "Tighten the stop of a plan to a price (quote currency) or to breakeven ('be'). Only stricter than the current stop is accepted.",
          dict({"id": PLAN_ID, "rev": _i("Current plan rev.", 1, 10 ** 6), "to": {"type": "string", "description": "Price like '143.2' or 'be'.", "maxLength": 24,
                                                                              "pattern": r"^(be|[0-9]+(\.[0-9]+)?)$", "free": False}}, **CMD_COMMON),
          "plan.manage", h_stop, required=["id", "rev", "to", "reason"], out=("cmd_id", "plan_id", "rev", "executor"), write=True))
_reg(Tool("close_plan", "Close the position of a plan (reduce-only, market) fully or by fraction frac in (0,1].",
          dict({"id": PLAN_ID, "frac": _n("Fraction of the remaining position, 0 < frac <= 1.", 0.0001, 1.0)}, **CMD_COMMON), "plan.manage", h_close,
          required=["id", "reason"], out=("cmd_id", "plan_id", "rev", "executor"), write=True))
_reg(Tool("cancel_plan", "Cancel a plan before entry (removes the resting order). A plan in a position cannot be cancelled: use close_plan.",
          dict({"id": PLAN_ID}, **CMD_COMMON), "plan.manage", _simple("cancel"), required=["id", "reason"], out=("cmd_id", "plan_id", "rev", "executor"), write=True))
_reg(Tool("hold_plan", "Pause a plan: forbids entry and adding until released.", dict({"id": PLAN_ID}, **CMD_COMMON), "plan.manage", _simple("hold"),
          required=["id", "reason"], out=("cmd_id", "plan_id", "rev", "executor"), write=True))
_reg(Tool("release_plan", "Release a pause set by the brain on a plan (a hold set by the executor or the owner cannot be released).",
          dict({"id": PLAN_ID}, **CMD_COMMON), "plan.manage", _simple("release"), required=["id", "reason"], out=("cmd_id", "plan_id", "rev", "executor"), write=True))
_reg(Tool("add_idea", "Record a trading idea with a level (price, quote currency). Ideas are watched by the scanner and wake the brain near the level. "
          "side long|short need level and stop; stop must be on the losing side of level.",
          {"sym": _s("Ticker like SOL (no USDT suffix).", 20, pattern=common.RE_SYMBOL.pattern), "side": _s("long | short | watch", 5, enum=("long", "short", "watch")),
           "note": _s("Why, one sentence, max 200 chars.", 200), "level": _n("Entry level price > 0.", 1e-12, 1e12), "stop": _n("Stop price > 0.", 1e-12, 1e12),
           "tp": _n("Target price > 0.", 1e-12, 1e12), "tag": _s("Setup tag.", 40, pattern=RE_TAG)}, "idea.write", h_add_idea,
          required=["sym", "side", "note"], out=("idea_id",), write=True))
_reg(Tool("list_ideas", "List ideas (default only active: idea/watching): id, symbol, side, level, stop, tp, tag, status, note.",
          {"status": dict(_s("active | all | idea | watching | taken | skipped | expired | invalidated", 12, enum=("active", "all") + ideas.STATUSES), default="active"),
           "limit": _i("Max rows 1..50.", 1, 50, 20)}, "read", h_list_ideas, out=("ideas",)))
_reg(Tool("post_mortem", "Post-trade review of a CLOSED trade: error tags, whether rules were followed, one-line lesson. Appends to the journal (does not edit trades).",
          {"trade_id": _s("Journal trade id.", 40, pattern=common.RE_IDENT.pattern), "err": {"type": "array", "items": _s("Error tag.", 24, enum=ERR_TAGS), "maxItems": 6,
                                                                                         "description": "Error tags: " + ", ".join(ERR_TAGS)},
           "rule_followed": _s("yes | no | partial", 8, enum=tradectx.RULE_VALUES), "lesson": _s("Lesson, one sentence, max 200 chars.", 200)},
          "journal.post", h_post_mortem, required=["trade_id"], out=("trade_id", "fields"), write=True))
_reg(Tool("get_stats", "Experiment statistics (average R per group after costs, win rate, profit factor, p-values with Bonferroni, mark * = n below min_n: no "
          "conclusion) and a trimmed journal analysis (error tags, rule adherence).",
          {"by": dict(_s("Grouping.", 12, enum=expstats.GROUP_BY), default="exp"), "mode": dict(_s("Trade mode.", 8, enum=expstats.MODES), default="all"),
           "min_n": _i("Minimum trades for p-values 1..1000.", 1, 1000, expstats.MIN_N)}, "read", h_stats,
          out=("by", "mode", "min_n", "k", "alpha_adj", "groups", "journal")))
_reg(Tool("get_decisions", "Tail of the decision journal (who decided what: actor, kind, action, reason, result code). Optional filter by plan or rows after a seq.",
          {"plan": dict(PLAN_ID), "after": _i("Rows with seq greater than this.", 0, 10 ** 12), "limit": _i("Max rows 1..50.", 1, 50, 20)}, "read", h_decisions,
          out=("decisions",)))

_reg(Tool("get_similar_cases", "Past situations similar to a planned entry (own system memory: trades, skips, ideas) with their outcomes in R, error tags and applicable "
          "lesson hypotheses. Numbers, enums and ids only. Read-only context, NOT a rule: it forbids and allows nothing. warn INSUFFICIENT (n<5) / LOW_N (n<30) means "
          "no conclusions. Without features the plan of sym is used. Hidden for live unless the owner allowed it. At most 30 calls/hour.",
          {"sym": _s("Ticker like SOL.", 20, pattern=common.RE_SYMBOL.pattern), "side": _s("long | short | watch", 5, enum=("long", "short", "watch")),
           "setup": _s("Setup enum.", 20, enum=memcases.SETUPS), "k": _i("Max cases 1..10.", 1, 10, 8),
           "scope": dict(_s("same_mode | all", 9, enum=("same_mode", "all")), default="same_mode"),
           "features": {"type": "object", "description": "Situation features (numbers/enums from the whitelist).", "properties": _mem_feature_props()}},
          "memory.read", h_similar, out=("mode", "sym", "side", "warn", "similar", "cases", "lessons", "other_mode", "filtered", "note", "trunc")))
_reg(Tool("list_lessons", "Lesson hypotheses of the memory (status hyp/confirmed/..., n, confidence, template wording). Hypotheses to check, not rules. Read-only.",
          {"status": dict(_s("active | all | hyp | confirmed | rejected | expired | merged", 10, enum=("active", "all") + memlessons.STATUSES), default="active"),
           "limit": _i("Max rows 1..20.", 1, 20, 10)}, "memory.read", h_lessons, out=("status", "n", "warn", "lessons", "note", "trunc")))
_reg(Tool("add_case_review", "Write the reviewer's verdict (schema tb.casereview/1) onto a closed case. Only the reviewer role; one verdict per case; known_at_decision is "
          "checked against the case situation; only enum fields, alternative.detail is cleaned.",
          {"case_id": _s("Case id C-YYYYMMDD-SYM-xxxx.", 48, pattern=memcases.RE_CASE_ID.pattern), "review": _obj("tb.casereview/1 object.")},
          "memory.write", h_case_review, required=["case_id", "review"], out=("case_id", "rev", "verdict", "lesson"), write=True))


def finish(tool, res):
    """Белый список верхнего уровня + scrub."""
    if not isinstance(res, dict):
        return {"ok": False, "errors": [{"code": "E_INTERNAL", "path": "", "msg": "bad result"}]}
    res = {k: v for k, v in res.items() if k in tool.out}
    return scrub(res)
