"""CLI мозга `./tb plan submit|check|amend|stop|close|cancel|hold|release|adopt|list|show|schema|template|from-idea` (03 п. 3.1). Владелец: WP6.

Пишет только в _inbox через PlanStore.inbox_put (единственная запись CLI мозга). Результат читает из _outbox/<cmd_id>.json без блокировок.
Валидацию и guard делает исполнитель (WP5) и схема плана (WP2); здесь только предварительная проверка `plan_schema` (если она реализована)
и честный ответ `queued`, когда исполнитель не отвечает (команда остаётся в очереди, повторно её слать не нужно).

Идемпотентность: `cmd_id` = C-<время>-<отпечаток содержимого, 6 hex>. Та же команда (op, план, rev, payload, reason), отправленная повторно
в течение 10 минут, находит прежний файл в _inbox/_outbox по отпечатку и не создаёт второй.
Код выхода: 0 при ok:true, 1 при ok:false (отказ), 2 ошибка использования или модуль ещё не готов, 3 ok:null (queued).
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time

from tb import paths
from tb.brain import common
from tb.core import bus
from tb.core import ideas as ideas_core
from tb.core import plan_schema

DEFAULT_WAIT_S = 10.0
POLL_S = 0.2
DEDUP_WINDOW_S = 600.0
ACTOR = "brain:chat"
LIST_FIELDS = ("id", "sym", "side", "mode", "status", "rev", "hold")
DECISION_KEYS = ("seq", "ts", "actor", "plan", "rev", "kind", "what", "args", "basis", "res", "action_id")
SHOW_EVENTS = 12
SHOW_DECISIONS = 40

_SCALP = {
    "schema": "tb.plan/1", "symbol": "SOL", "side": "short", "mode": "live", "session_type": "scalp",
    "source": {"kind": "idea", "idea_id": "I7", "event_seq": 1842},
    "thesis": "Прокол сопротивления 142.5 и закрытие 5м обратно под уровень на повышенном объёме; цель откат к 140.2",
    "tags": {"experiment": "sweep-reject-v1", "variant": "A", "setup": "level_reject", "hypothesis": "H3"},
    "entry": {"when": {"all": [{"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.05},
                               {"kind": "volume_spike", "tf": "5m", "mult": 1.8, "lookback": 20, "on": "close", "dir": "any"},
                               {"kind": "spread_bps", "lte": 6}]},
              "how": {"type": "market"}, "ref_price": 142.3, "valid": {"ttl_min": 12, "cancel_above": 143.4, "cancel_below": 140.0, "max_chase_bps": 12}},
    "stop": {"price": 143.0, "trigger_by": "last"},
    "targets": [{"id": "t1", "price": 141.2, "frac": 0.5, "exec": "resting"}, {"id": "t2", "price": 140.2, "frac": 0.5, "exec": "resting"}],
    "risk": {"score": 0.7, "size_mult": 1.0},
    "manage": [{"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True},
               {"id": "be_t1", "when": {"kind": "target_filled", "target": "t1"}, "do": {"action": "move_stop", "to": "lock_r", "r": 0.3}, "once": True},
               {"id": "trail", "when": {"kind": "pnl_r", "gte": 1.3}, "do": {"action": "trail", "mode": "candle_extreme", "tf": "1m", "n": 3, "buffer_bps": 4}},
               {"id": "stale", "when": {"all": [{"kind": "elapsed", "min": 10}, {"kind": "pnl_r", "lt": 0.3}]},
                "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": True},
               {"id": "wake_m05", "when": {"kind": "pnl_r", "lte": -0.5}, "do": {"action": "notify", "code": "R_MINUS"}, "once": True}],
    "invalidation": [{"kind": "candle_close", "tf": "5m", "dir": "above", "level": 143.3, "n": 1}],
    "lifecycle": {"ttl_min": 12, "max_hold_min": 15, "flatten_at_session_end": True},
}
_INTRADAY = {
    "schema": "tb.plan/1", "symbol": "SOL", "side": "long", "mode": "live", "session_type": "intraday",
    "source": {"kind": "brain"},
    "thesis": "Снятие стопов под 144 и возврат выше 145 на 5м после дневного восходящего тренда; держим до 153",
    "tags": {"experiment": "sweep-reclaim-v1", "variant": "B", "setup": "reclaim", "hypothesis": "H5", "extra": ["trend-up-1h"]},
    "entry": {"when": {"kind": "seq", "within_min": 90, "steps": [{"kind": "price_beyond", "dir": "below", "level": 144.0},
                                                                    {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 145.0, "n": 1}]},
              "how": {"type": "market"}, "ref_price": 145.6, "valid": {"ttl_min": 180, "cancel_above": 150.0, "cancel_below": 142.0, "max_chase_bps": 20}},
    "stop": {"price": 143.2, "trigger_by": "last"},
    "targets": [{"id": "t1", "price": 148.6, "frac": 0.5, "exec": "resting"}, {"id": "t2", "price": 153.0, "frac": 0.5, "exec": "resting"}],
    "risk": {"score": 0.6, "size_mult": 1.0},
    "manage": [{"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True},
               {"id": "trail", "when": {"kind": "target_filled", "target": "t1"},
                "do": {"action": "trail", "mode": "atr", "tf": "15m", "n": 14, "mult": 2.0, "buffer_bps": 5}},
               {"id": "tmax", "when": {"kind": "elapsed", "min": 600}, "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": True},
               {"id": "wake_t1", "when": {"kind": "near_target", "target": "t1", "frac": 0.8}, "do": {"action": "notify", "code": "NEAR_TARGET"}, "once": True}],
    "invalidation": [{"kind": "candle_close", "tf": "15m", "dir": "below", "level": 144.6, "n": 1}],
    "lifecycle": {"ttl_min": 180, "max_hold_min": 720, "flatten_at_session_end": False},
}
TEMPLATES = {"scalp": _SCALP, "intraday": _INTRADAY}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


# ------------------------------------------------------------------ вывод и ошибки
def _err(code, msg, path="", status="rejected_local", ok=False):
    return {"ok": ok, "status": status, "errors": [{"code": code, "path": path, "msg": common.clean_text(msg, 200)}]}


def _finish(resp):
    """Печатает ответ и возвращает код выхода по ok."""
    common.out(resp)
    ok = resp.get("ok")
    return common.EXIT_OK if ok is True else common.EXIT_QUEUED if ok is None and resp.get("status") == common.STATUS_QUEUED else common.EXIT_FAIL


W_BYPASS = {"code": "W_CLI_BYPASS", "path": "", "msg": "прямой путь ./tb plan идёт мимо шлюза агента и торгового флага дня (запись в аудит шлюза)"}


def _platform_active(root):
    """Платформа ведёт день (state/control/platform.json: state не stopped) -> True; нет файла/нечитаемо -> False (прежний чат-путь без платформы не ломаем)."""
    try:
        with open(paths.state("control", "platform.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return isinstance(d, dict) and d.get("state") not in (None, "stopped")
    except (OSError, ValueError):
        return False


def bypass_guard(op, raw, root=None, now=None):
    """Прямой чат-путь (WP11-C2 п. 5): запись в аудит шлюза (agent=cli) + проверка торгового флага для НЕ paper плана, но только пока платформа ведёт день.
    -> None (можно) | ответ-отказ E_TRADING_*. paper не проверяется никогда."""
    now = time.time() if now is None else now
    mode = raw.get("mode", "live") if isinstance(raw, dict) else "live"
    code = None
    if op == "submit" and mode != "paper" and _platform_active(root):
        try:
            from tb.gateway import registry
            ok, code = registry.trading_allowed(root, mode, now)
            code = None if ok else code
        except Exception:
            code = "E_TRADING_FLAG"
    try:
        from tb.gateway import audit
        audit.record("cli", "plan." + op, {"mode": mode, "plan": raw.get("id") if isinstance(raw, dict) else None}, code is None, code, 0, via="cli", root=root, now=now)
    except Exception:
        pass
    if code:
        return _err(code, "платформа ведёт день, торговый флаг не разрешает %s-план (флаг пишет старт дня)" % mode)
    return None


def _with_bypass_warning(resp):
    if isinstance(resp, dict):
        resp["warnings"] = list(resp.get("warnings") or []) + [dict(W_BYPASS)]
    return resp


def _store(root):
    from tb.core import plan_store
    return plan_store.PlanStore(root)


def _read_input(spec):
    """FILE или '-' (stdin) -> (объект, None) либо (None, текст ошибки)."""
    try:
        if spec in (None, "-"):
            text = sys.stdin.read()
        else:
            with open(spec, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        return None, "не прочитать файл: %s" % e.__class__.__name__
    if len(text.encode("utf-8")) > plan_schema.MAX_PLAN_BYTES * 2:
        return None, "вход слишком большой"
    try:
        return json.loads(text), None
    except ValueError:
        return None, "не JSON"


# ------------------------------------------------------------------ очередь команд
def fingerprint(cmd):
    """Отпечаток содержимого команды (без cmd_id, ts, actor) -> 6 hex."""
    body = {k: cmd.get(k) for k in ("op", "plan_id", "rev_expected", "payload", "reason", "event_seq", "brief_id")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()[:6]


def _cmd_ts(cmd_id):
    try:
        import calendar
        return float(calendar.timegm(time.strptime(cmd_id.split("-")[1], "%Y%m%dT%H%M%S")))
    except (ValueError, IndexError, AttributeError):
        return None


def _find_previous(fp, root, now):
    """Команда с тем же отпечатком в _inbox/_outbox за последние 10 минут -> cmd_id или None."""
    for d in (paths.plans_inbox(root=root), paths.plans_outbox(root=root)):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in sorted(names, reverse=True):
            if n.startswith("C-") and n.endswith("-%s.json" % fp):
                t = _cmd_ts(n)
                if t is not None and now - t <= DEDUP_WINDOW_S:
                    return n[:-5]
    return None


def _outbox(cmd_id, root):
    rec = common.read_json(paths.plans_outbox(cmd_id + ".json", root=root))
    return rec if isinstance(rec, dict) else None


def _items(rows, keys):
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            out.append({k: (common.clean_text(r.get(k), 200) if k == "msg" else r.get(k)) for k in keys if k in r})
    return out


def _answer(rec, cmd_id):
    """Ответ исполнителя из _outbox -> стабильный формат 3.1 (только известные поля)."""
    out = {"ok": rec.get("ok"), "cmd_id": cmd_id, "status": common.ident(rec.get("status"), "unknown")}
    if common.ident(rec.get("plan_id")):
        out["plan_id"] = rec["plan_id"]
    if isinstance(rec.get("rev"), int) and not isinstance(rec.get("rev"), bool):
        out["rev"] = rec["rev"]
    out["warnings"] = _items(rec.get("warnings"), ("code", "path", "msg"))
    gp = rec.get("guard_preview")
    if isinstance(gp, dict):
        out["guard_preview"] = common.clean_struct(gp)
    out["errors"] = _items(rec.get("errors"), ("code", "path", "msg"))
    return out


def send(cmd, wait, root=None, now=None, store=None, sleep=time.sleep):
    """Кладёт команду в _inbox (идемпотентно) и ждёт ответ до `wait` секунд -> dict ответа (ok true|false|null)."""
    now = time.time() if now is None else now
    fp = fingerprint(cmd)
    cmd_id = _find_previous(fp, root, now)
    if cmd_id is None:
        cmd = dict(cmd, cmd_id="C-%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)), fp), ts=common.iso(now))
        try:
            cmd_id = (store or _store(root)).inbox_put(cmd) or cmd["cmd_id"]
        except NotImplementedError:
            return _err("E_STORE", "хранилище планов (WP4) ещё не реализовано", status="error")
        except (OSError, ValueError) as e:
            return _err("E_STORE", "не удалось записать команду: %s" % e.__class__.__name__, status="error")
    h = common.exec_health(root=root, now=now)
    deadline = time.time() + (wait if h["alive"] else 0.0)
    while True:
        rec = _outbox(cmd_id, root)
        if rec is not None:
            return _answer(rec, cmd_id)
        if time.time() >= deadline:
            break
        sleep(POLL_S)
    return {"ok": None, "cmd_id": cmd_id, "status": common.STATUS_QUEUED, "executor": "alive" if h["alive"] else "down"}


def _envelope(op, a, plan_id=None, rev=None, payload=None, event_seq=None):
    cmd = {"actor": os.environ.get("TB_ACTOR") or ACTOR, "op": op}
    if plan_id:
        cmd["plan_id"] = plan_id
    if rev is not None:
        cmd["rev_expected"] = rev
    if payload is not None:
        cmd["payload"] = payload
    cmd["reason"] = common.clean_text(getattr(a, "reason", None) or "", common.MAX_REASON_LEN)
    es = event_seq if event_seq is not None else getattr(a, "event_seq", None)
    if es is not None:
        cmd["event_seq"] = es
    if getattr(a, "brief", None):
        cmd["brief_id"] = a.brief
    return cmd


def _common_args(p, reason_required=True):
    p.add_argument("--reason", default=None)
    p.add_argument("--brief", default=None)
    p.add_argument("--event-seq", type=int, default=None, dest="event_seq")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT_S)
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)


def _need_reason(a, optional_if=False):
    if not optional_if and not common.clean_text(a.reason or "", common.MAX_REASON_LEN):
        return _err("E_REASON", "reason обязателен (<= 200 символов)", path="reason")
    if a.reason and len(a.reason) > 2000:
        return _err("E_REASON", "reason слишком длинный (сохраняется первые %d символов)" % common.MAX_REASON_LEN, path="reason")
    if a.wait < 0 or a.wait > 120:
        return _err("E_RANGE", "--wait 0..120", path="wait")
    return None


def _check_plan_id(pid):
    if not common.RE_PLAN_ID.match(pid or ""):
        return _err("E_ID", "id плана: PL-MMDD-SYM-NN", path="id")
    return None


# ------------------------------------------------------------------ предварительная проверка (plan_schema, если готова)
def precheck(raw, now=None):
    """-> (plan, errors, warnings, ready). ready=False, если WP2 ещё не реализован (проверка пропущена)."""
    try:
        plan, issues = plan_schema.normalize(raw, now)
        issues = list(issues) + list(plan_schema.validate_static(plan))
    except NotImplementedError:
        return raw, [], [], False
    errs = [i for i in issues if i.get("severity") == "error"]
    warns = [i for i in issues if i.get("severity") != "error"]
    return plan, errs, warns, True


def _context(root, now, plan):
    """ctx для plan_schema.validate_context из локальных файлов (инструменты, цены, сессия, лимиты, активные планы)."""
    from tb.dash import model
    sym = plan.get("symbol")
    instr = common.read_json(paths.instruments_file(root=root))
    instr = (instr.get("symbols") if isinstance(instr, dict) and isinstance(instr.get("symbols"), dict) else instr) if isinstance(instr, dict) else None
    sp = model.scan_prices(root, now)
    mkt = model.market_info(root, now)
    sessions = model.load_sessions(root)
    active = next((s for s in reversed(sessions) if not s.get("stopped")), None)
    limits = common.read_json(paths.exec_limits_file(root=root)) or {}
    prof = common.read_json(paths.config("risk_profile.json", root=root)) or {}
    plans, _ = common.load_plans(root)
    return {"now": now, "limits": limits, "session": active, "instrument": (instr or {}).get(sym) if isinstance(instr, dict) else None,
            "price": model.resolve_price(mkt, sp, sym, None)["price"], "profile": prof,
            "active_plans": [x["spec"] for x in plans if common.plan_status(x) not in plan_schema.TERMINAL], "cooldowns": {}}


def guard_preview(plan, root=None):
    """guard.evaluate с no_session по ref_price/стопу/цели плана -> {"ok","leverage","margin","risk_usdt","net_rr","stop_pct", "reasons"} или None."""
    try:
        from tb.core import guard
        entry = plan.get("entry") or {}
        how = entry.get("how") or {}
        px = entry.get("ref_price") or how.get("price")
        tg = plan.get("targets") or []
        tot = sum(t.get("frac", 0) for t in tg) or 1.0
        tp = sum(t["price"] * t.get("frac", 0) for t in tg) / tot if tg else None
        a = argparse.Namespace(sym=plan["symbol"], side=plan["side"], entry=float(px), sl=float(plan["stop"]["price"]), tp=tp, equity=None, free=None,
                               no_session=True, score=float((plan.get("risk") or {}).get("score", 0.5)), regime="unknown", min_quote=1.0,
                               contract_max_lev=10, profile=None, exec="taker", paper=False,
                               setup=(plan.get("tags") or {}).get("setup"))
        r = guard.evaluate(a)
    except Exception:       # предпросмотр не должен ронять проверку (нет цены, нет профиля, guard меняется WP9)
        return None
    out = {k: r.get(k) for k in ("ok", "leverage", "margin", "risk_usdt", "net_rr", "stop_pct") if k in r}
    out["reasons"] = [common.clean_text(x, 100) for x in (r.get("reasons") or [])[:4]]
    return out


# ------------------------------------------------------------------ команды
def cmd_submit(argv):
    p = _Parser(prog="tb plan submit")
    p.add_argument("file", nargs="?", default="-")
    _common_args(p)
    a = p.parse_args(argv)
    raw, why = _read_input(a.file)
    if raw is None:
        return _finish(_err("E_SCHEMA", why))
    if not isinstance(raw, dict):
        return _finish(_err("E_SCHEMA", "план должен быть JSON-объектом"))
    has_thesis = isinstance(raw.get("thesis"), str) and len(raw["thesis"].strip()) >= 10
    bad = _need_reason(a, optional_if=has_thesis)
    if bad:
        return _finish(bad)
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > plan_schema.MAX_PLAN_BYTES:
        return _finish(_err("E_TOO_LARGE", "план больше %d байт" % plan_schema.MAX_PLAN_BYTES))
    if a.event_seq is not None and isinstance(raw.get("source"), dict) and "event_seq" not in raw["source"]:
        raw["source"]["event_seq"] = a.event_seq
    _, errs, _, _ = precheck(raw)
    if errs:
        return _finish({"ok": False, "status": "rejected_local", "errors": _items(errs, ("code", "path", "msg"))})
    pid = raw.get("id") if isinstance(raw.get("id"), str) and common.RE_PLAN_ID.match(raw["id"]) else None
    deny = bypass_guard("submit", raw, root=a.root)
    if deny:
        return _finish(_with_bypass_warning(deny))
    return _finish(_with_bypass_warning(send(_envelope("submit", a, plan_id=pid, payload=raw), a.wait, root=a.root)))


def cmd_check(argv):
    p = _Parser(prog="tb plan check")
    p.add_argument("file", nargs="?", default="-")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    raw, why = _read_input(a.file)
    if not isinstance(raw, dict):
        return _finish(_err("E_SCHEMA", why or "план должен быть JSON-объектом"))
    now = time.time()
    plan, errs, warns, ready = precheck(raw, now)
    if not ready:
        return _finish(_err("E_NOT_READY", "схема плана (WP2) ещё не реализована: проверка недоступна", status="unavailable"))
    checked = ["static"]
    try:
        ctx = _context(a.root, now, plan)
        issues = plan_schema.validate_context(plan, ctx)
        errs += [i for i in issues if i.get("severity") == "error"]
        warns += [i for i in issues if i.get("severity") != "error"]
        checked.append("context")
    except (NotImplementedError, KeyError, TypeError, ValueError, AttributeError, ImportError):
        pass
    gp = guard_preview(plan, a.root) if not errs else None
    resp = {"ok": not errs, "status": "ok" if not errs else "invalid", "checked": checked, "warnings": _items(warns, ("code", "path", "msg")),
            "errors": _items(errs, ("code", "path", "msg"))}
    if gp is not None:
        resp["guard_preview"] = gp
    return _finish(resp)


def cmd_amend(argv):
    p = _Parser(prog="tb plan amend")
    p.add_argument("id")
    p.add_argument("--rev", type=int, required=True)
    p.add_argument("--patch", default=None)
    p.add_argument("--file", default=None)
    _common_args(p)
    a = p.parse_args(argv)
    bad = _check_plan_id(a.id) or _need_reason(a)
    if bad:
        return _finish(bad)
    if (a.patch is None) == (a.file is None):
        return _finish(_err("E_SCHEMA", "нужен ровно один из --patch JSON или --file F"))
    if a.patch is not None:
        try:
            patch = json.loads(a.patch)
        except ValueError:
            return _finish(_err("E_SCHEMA", "--patch не JSON"))
    else:
        patch, why = _read_input(a.file)
        if patch is None:
            return _finish(_err("E_SCHEMA", why))
    if not isinstance(patch, dict) or not patch:
        return _finish(_err("E_SCHEMA", "патч должен быть непустым JSON-объектом"))
    return _finish(send(_envelope("amend", a, plan_id=a.id, rev=a.rev, payload=patch), a.wait, root=a.root))


def cmd_stop(argv):
    p = _Parser(prog="tb plan stop")
    p.add_argument("id")
    p.add_argument("--rev", type=int, required=True)
    p.add_argument("--to", required=True, help="цена или be")
    _common_args(p)
    a = p.parse_args(argv)
    bad = _check_plan_id(a.id) or _need_reason(a)
    if bad:
        return _finish(bad)
    if a.to == "be":
        to = "be"
    else:
        to = common.num(a.to)
        if to is None or to <= 0:
            return _finish(_err("E_RANGE", "--to: положительная цена или be", path="to"))
    return _finish(send(_envelope("stop", a, plan_id=a.id, rev=a.rev, payload={"to": to}), a.wait, root=a.root))


def cmd_close(argv):
    p = _Parser(prog="tb plan close")
    p.add_argument("id")
    p.add_argument("--frac", type=float, default=1.0)
    _common_args(p)
    a = p.parse_args(argv)
    bad = _check_plan_id(a.id) or _need_reason(a)
    if bad:
        return _finish(bad)
    if not (0 < a.frac <= 1.0):
        return _finish(_err("E_RANGE", "--frac в (0, 1]", path="frac"))
    return _finish(send(_envelope("close", a, plan_id=a.id, payload={"frac": a.frac}), a.wait, root=a.root))


def _simple(op):
    def run(argv):
        p = _Parser(prog="tb plan " + op)
        p.add_argument("id")
        _common_args(p)
        a = p.parse_args(argv)
        bad = _check_plan_id(a.id) or _need_reason(a)
        if bad:
            return _finish(bad)
        return _finish(send(_envelope(op, a, plan_id=a.id), a.wait, root=a.root))
    return run


def cmd_adopt(argv):
    p = _Parser(prog="tb plan adopt")
    p.add_argument("sym")
    p.add_argument("--manage-file", default=None, dest="manage_file")
    _common_args(p)
    a = p.parse_args(argv)
    bad = _need_reason(a)
    if bad:
        return _finish(bad)
    if not common.RE_SYMBOL.match(a.sym or ""):
        return _finish(_err("E_ID", "символ: ^[A-Z0-9]{2,20}$", path="symbol"))
    payload = {"symbol": a.sym}
    if a.manage_file:
        m, why = _read_input(a.manage_file)
        if m is None:
            return _finish(_err("E_SCHEMA", why))
        payload["manage"] = m
    return _finish(send(_envelope("adopt", a, payload=payload), a.wait, root=a.root))


# ------------------------------------------------------------------ чтение (без блокировок)
def _plan_row(it):
    spec, rt = it["spec"], it["rt"]
    hold = rt.get("hold")
    return {"id": it["id"], "sym": spec.get("symbol") if common.RE_SYMBOL.match(str(spec.get("symbol"))) else None,
            "side": common.ident(spec.get("side")), "mode": common.ident(spec.get("mode")), "status": common.plan_status(it),
            "rev": spec.get("rev") if isinstance(spec.get("rev"), int) else None, "hold": common.ident(hold) if hold not in (None, False) else None}


def _queued(root):
    out = []
    d = paths.plans_inbox(root=root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("C-") and n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        rec = common.read_json(os.path.join(d, n))
        if isinstance(rec, dict):
            out.append({"cmd_id": n[:-5], "op": common.ident(rec.get("op")), "plan_id": common.ident(rec.get("plan_id"))})
    return out


def cmd_list(argv):
    p = _Parser(prog="tb plan list")
    p.add_argument("--status", default=None)
    p.add_argument("--mode", default=None, choices=["live", "paper"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--text", action="store_true")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    want = set(s for s in (a.status or "").split(",") if s)
    bad = want - set(plan_schema.STATUSES)
    if bad:
        return _finish(_err("E_RANGE", "статусы: " + ",".join(plan_schema.STATUSES), path="status"))
    items, nbad = common.load_plans(a.root)
    rows = [_plan_row(i) for i in items]
    rows = [r for r in rows if (not want or r["status"] in want) and (not a.mode or r["mode"] == a.mode)]
    queued = _queued(a.root)
    if a.text and not a.json:
        for r in rows:
            print("%-18s %-8s %-5s %-9s %-5s rev%s%s" % (r["id"], r["sym"] or "?", r["side"] or "?", r["status"], r["mode"] or "?", r["rev"], " hold" if r["hold"] else ""))
        for q in queued:
            print("в очереди: %s %s %s" % (q["cmd_id"], q["op"], q["plan_id"] or ""))
        return common.EXIT_OK
    resp = {"ok": True, "plans": rows, "queued": queued}
    if nbad:
        resp["skipped_bad"] = nbad
    common.out(resp)
    return common.EXIT_OK


def _revision(root, plan_id, rev):
    for r in common.read_jsonl(os.path.join(paths.plans(plan_id, root=root), "revs.jsonl")):
        if r.get("rev") == rev:
            return r.get("spec") if isinstance(r.get("spec"), dict) else r
    return None


def _decisions(root, plan_id, limit):
    rows = [r for r in common.read_jsonl(paths.decisions_file(root=root)) if r.get("plan") == plan_id]
    out = []
    for r in rows[-limit:]:
        d = {k: common.clean_struct(r.get(k)) for k in DECISION_KEYS if k in r and k != "why"}
        d["why"] = common.clean_text(r.get("why"), common.MAX_REASON_LEN)
        out.append(d)
    return out


def cmd_show(argv):
    p = _Parser(prog="tb plan show")
    p.add_argument("id")
    p.add_argument("--rev", type=int, default=None)
    p.add_argument("--events", action="store_true")
    p.add_argument("--decisions", action="store_true")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    bad = _check_plan_id(a.id)
    if bad:
        return _finish(bad)
    spec, rt = common.load_plan(a.id, root=a.root)
    if spec is None:
        return _finish(_err("E_NOT_FOUND", "план не найден или spec.json нечитаем", status="not_found"))
    if a.rev is not None:
        old = _revision(a.root, a.id, a.rev)
        if old is None:
            return _finish(_err("E_NOT_FOUND", "ревизия %d не найдена" % a.rev, status="not_found"))
        spec = old
    thesis = spec.get("thesis")
    body = {k: v for k, v in spec.items() if k != "thesis"}
    resp = {"ok": True, "id": a.id, "spec": common.clean_struct(body), "own_thesis": common.clean_text(thesis, 280) or None, "rt": common.clean_struct(rt or {})}
    if a.events:
        evs = [bus.clean_out(e) for e in bus.read(after_seq=0, limit=0, root=a.root) if e.get("plan") == a.id]
        resp["events"] = [e for e in evs if e][-SHOW_EVENTS:]
    if a.decisions:
        resp["decisions"] = _decisions(a.root, a.id, SHOW_DECISIONS)
    resp = {k: v for k, v in resp.items() if v is not None}
    common.out(resp)
    return common.EXIT_OK


def cmd_schema(argv):
    try:
        schema = plan_schema.json_schema()
    except NotImplementedError:
        return _finish(_err("E_NOT_READY", "json_schema (WP2) ещё не реализована", status="unavailable"))
    common.out(schema)
    return common.EXIT_OK


def cmd_template(argv):
    p = _Parser(prog="tb plan template")
    p.add_argument("name", choices=sorted(TEMPLATES))
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    common.out(copy.deepcopy(TEMPLATES[a.name]))
    return common.EXIT_OK


def plan_from_idea(idea, mode="live", template="scalp", price=None):
    """Заготовка плана по идее: уровень, стоп, тейк из идеи + правила шаблона. -> (plan, None) или (None, текст ошибки)."""
    side, lvl, stop, tp = idea.get("side"), common.num(idea.get("level")), common.num(idea.get("stop")), common.num(idea.get("tp"))
    sym = idea.get("sym")
    if side not in ("long", "short") or not isinstance(sym, str) or not common.RE_SYMBOL.match(sym):
        return None, "в идее нет стороны long/short или символа"
    if lvl is None or stop is None:
        return None, "в идее нет level и stop (задай их через ideas add --level --stop)"
    if (side == "long" and stop >= lvl) or (side == "short" and stop <= lvl):
        return None, "стоп идеи с неверной стороны от уровня"
    plan = copy.deepcopy(TEMPLATES[template])
    plan.update({"symbol": sym, "side": side, "mode": mode})
    plan["source"] = {"kind": "idea", "idea_id": idea["id"]}
    note = common.clean_text(idea.get("note"), 200)
    plan["thesis"] = common.clean_text("Идея %s у уровня %g: %s" % (idea["id"], lvl, note), 280)
    tag = idea.get("tag")
    plan["tags"] = {"setup": tag} if isinstance(tag, str) and common.RE_IDENT.match(tag) and len(tag) >= 2 else {}
    rej = "long" if side == "long" else "short"
    plan["entry"] = {"when": {"kind": "candle_reject", "tf": "5m", "level": lvl, "dir": rej, "wick_min_pct": 0.05},
                     "how": {"type": "market"}, "ref_price": price or lvl, "valid": {"ttl_min": plan["lifecycle"]["ttl_min"], "max_chase_bps": 12}}
    plan["stop"] = {"price": stop, "trigger_by": "last"}
    if tp is not None:
        plan["targets"] = [{"id": "t1", "price": tp, "frac": 1.0, "exec": "resting"}]
    else:
        risk = abs(lvl - stop)
        plan["targets"] = [{"id": "t1", "price": round(lvl + (2.0 if side == "long" else -2.0) * risk, 8), "frac": 1.0, "exec": "resting"}]
    plan["manage"] = [m for m in plan["manage"] if m["id"] != "be_t1"]   # единственная цель закрывает позицию целиком
    plan["invalidation"] = [{"kind": "candle_close", "tf": "5m", "dir": "below" if side == "long" else "above", "level": stop, "n": 1}]
    plan["risk"] = {"score": 0.5, "size_mult": 1.0}
    return plan, None


def cmd_from_idea(argv):
    p = _Parser(prog="tb plan from-idea")
    p.add_argument("idea")
    p.add_argument("--mode", default="live", choices=["live", "paper"])
    p.add_argument("--template", default="scalp", choices=sorted(TEMPLATES))
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if not common.RE_IDEA_ID.match(a.idea or ""):
        return _finish(_err("E_ID", "идея: I<число>", path="idea"))
    idea = next((i for i in ideas_core.load_ideas(a.root) if i.get("id") == a.idea), None)
    if idea is None:
        return _finish(_err("E_NOT_FOUND", "идея не найдена", status="not_found"))
    book = ideas_core.price_book(a.root, time.time())
    hit = book.get(idea.get("sym"))
    plan, why = plan_from_idea(idea, a.mode, a.template, price=hit[0] if hit else None)
    if plan is None:
        return _finish(_err("E_IDEA", why))
    common.out(plan)
    return common.EXIT_OK


COMMANDS = {
    "submit": cmd_submit, "check": cmd_check, "amend": cmd_amend, "stop": cmd_stop, "close": cmd_close, "cancel": _simple("cancel"),
    "hold": _simple("hold"), "release": _simple("release"), "adopt": cmd_adopt, "list": cmd_list, "show": cmd_show,
    "schema": cmd_schema, "template": cmd_template, "from-idea": cmd_from_idea,
}


def main(argv=None):
    common.setup_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print("использование: tb plan <%s> ..." % "|".join(COMMANDS), file=sys.stderr)
        return common.EXIT_USAGE
    try:
        return COMMANDS[argv[0]](argv[1:])
    except ValueError as e:
        return common.fail(str(e))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
