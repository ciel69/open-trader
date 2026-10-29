"""Gate: единственная дверь к бирже (02 п. 2.5). Владелец: WP5.

Каждое действие проходит Gate ДО отправки. Порядок проверок (первое «нет» останавливает): KILL (читается файл при каждой проверке) ->
observe/порт/hold -> класс действия -> данные (возраст цены, реконсиляция) -> guard/защиты/занятость монеты (вход) или монотонность и
зазоры (стоп) -> скорость. Ответ: Verdict(allow=True, **params) либо Verdict(allow=False, code, detail).

Классы: RISK_UP (вход, добор, входной лимит), RISK_DOWN (снять ордер, закрыть, подтянуть стоп, придвинуть тейк), NEUTRAL (лесенка reduce-only).
RISK_DOWN проходит с минимумом проверок (kill, observe, монотонность стопа). Коды отказов: DENY_CODES.

Монотонность стопа проверяется независимо в четырёх местах: правила (logic.rules.enforce_tighter), Gate (здесь), Actions (перед каждым
вызовом порта, свежее чтение биржевого стопа) и порт (STOP_NOT_TIGHTER); биржа ослабление стопа РАЗРЕШАЕТ, поэтому локальная проверка обязательна.
"""
import argparse
import math

from tb.core import blackout, exec_cfg, guard, plan_schema, risklevels, session
from tb.executor import protect

def agent_limits_of(spec):
    """Лимиты агента (agents.json limits) для плана, поставленного агентом шлюза: тег `agent.<id>` в tags.extra ставит ТОЛЬКО шлюз (WP11-C2), модель его не задаёт.
    Нет тега (прямые планы владельца/чата) или файл нечитаем -> None (guard применяет уровень риска без сужения агентом). Только сужение: guard берёт минимум."""
    aid = None
    for t in ((spec.get("tags") or {}).get("extra") or []):
        if isinstance(t, str) and t.startswith("agent."):
            aid = t[len("agent."):]
    if not aid:
        return None
    try:
        import json
        from tb import paths
        with open(paths.config("agents.json"), encoding="utf-8") as f:
            ag = ((json.load(f).get("agents") or {}).get(aid) or {})
        lim = ag.get("limits")
        return dict(lim) if isinstance(lim, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


RISK_UP, RISK_DOWN, NEUTRAL = "RISK_UP", "RISK_DOWN", "NEUTRAL"
CLASSES = (RISK_UP, RISK_DOWN, NEUTRAL)
DENY_CODES = ("KILL", "HOLD", "STALE_PRICE", "GUARD", "MIN_SIZE", "SYMBOL_BUSY", "COOLDOWN", "RATE", "STOP_NOT_TIGHTER", "INVARIANT_VIOLATION",
              "OBSERVE", "SESSION", "LIMIT_PLANS", "PRICE_JUMP", "NO_CONFIRM", "LIMIT_HOUR", "LIMIT_DAY", "ANOMALY", "NOT_TIGHTER",
              "STOP_TOO_CLOSE", "STOP_STEP", "STOP_INTERVAL", "STOP_RATE", "NO_PORT", "ADD_DISABLED", "ADD_REJECTED", "NO_POSITION", "E_BLACKOUT")
# коды отказа, которые считаются попыткой входа (блокировка входа) и коды, при которых вход просто ждёт следующего тика (не попытка)
# E_BLACKOUT тоже ждёт: план остаётся armed (не попытка, не ENTRY_BLOCKED) и войдёт после окна, если ttl не истёк
TRANSIENT = ("KILL", "HOLD", "STALE_PRICE", "RATE", "OBSERVE", "NO_PORT", "PRICE_JUMP", "E_BLACKOUT")
# коды, при которых мозг получает action_rejected (будящее событие)
WAKE_CODES = ("GUARD", "MIN_SIZE", "SYMBOL_BUSY", "COOLDOWN", "NO_CONFIRM", "LIMIT_HOUR", "LIMIT_DAY", "ANOMALY", "SESSION", "INVARIANT_VIOLATION",
              "STOP_NOT_TIGHTER", "ADD_REJECTED", "ADD_DISABLED", "STOP_TOO_CLOSE")


class Verdict(object):
    """Результат Gate. allow=True: params несут итоговые параметры; allow=False: code из DENY_CODES и detail."""

    def __init__(self, allow, code=None, detail="", **params):
        self.allow = bool(allow)
        self.code = code
        self.detail = detail
        self.params = params

    def __bool__(self):
        return self.allow

    def __repr__(self):
        return "Verdict(allow)" if self.allow else "Verdict(deny %s: %s)" % (self.code, self.detail)


def allow(**params):
    return Verdict(True, **params)


def deny(code, detail=""):
    return Verdict(False, code, detail)


def tighter(side, new, cur):
    """Независимая проверка: стоп `new` СТРОГО строже `cur` (long: выше, short: ниже). cur <= 0 или None = стопа ещё нет: любой положительный строже.

    >>> tighter("long", 101.0, 100.0), tighter("long", 100.0, 100.0), tighter("short", 99.0, 100.0), tighter("long", 99.0, 0.0)
    (True, False, True, True)
    """
    try:
        n = float(new)
        c = float(cur) if cur is not None else 0.0
    except (TypeError, ValueError):
        return False
    if not math.isfinite(n) or n <= 0 or side not in ("long", "short"):
        return False
    if not math.isfinite(c) or c <= 0:
        return True
    return n > c if side == "long" else n < c


def floor_step(x, step):
    """Округление вниз к шагу без накопления ошибки float."""
    step = float(step or 0)
    if step <= 0:
        return float(x)
    return round(math.floor(float(x) / step + 1e-9) * step, 12)


def weighted_tp(spec):
    """Средневзвешенная цель по frac; бегун (1 - sum frac) считается на ПОСЛЕДНЕЙ цели (01 п. 1.4)."""
    tg = [t for t in spec.get("targets") or [] if isinstance(t, dict)]
    if not tg:
        return None
    fr = [float(t["frac"]) for t in tg]
    px = [float(t["price"]) for t in tg]
    runner = max(0.0, 1.0 - sum(fr))
    tot = sum(f * p for f, p in zip(fr, px)) + runner * px[-1]
    den = sum(fr) + runner
    return tot / den if den > 0 else None


class Gate(object):
    def __init__(self, ex):
        self.ex = ex
        self._acts = {}      # sym -> [ts] отправок за 10 с (для max_actions_per_symbol_per_10s)

    # ---- общие проверки ----
    def base(self, klass, sym=None, plan=None, what=""):
        """KILL -> observe -> порт -> hold. -> Verdict (deny) или None."""
        ex = self.ex
        kill = exec_cfg.kill_state(ex.root)
        if kill is not None and klass in (RISK_UP, NEUTRAL):
            return deny("KILL", kill.get("reason") or "state/KILL")
        if ex.observe:
            return deny("OBSERVE", "режим --observe: записи на биржу запрещены")
        if ex.port is None:
            return deny("NO_PORT", "порт биржи недоступен")
        if klass == RISK_UP:
            if ex.holds:
                return deny("HOLD", ",".join(sorted(ex.holds)))
            if plan is not None and (plan["rt"].get("hold")):
                return deny("HOLD", "hold плана: %s" % plan["rt"].get("hold_reason"))
        elif klass == NEUTRAL and "reduce_only" in ex.holds:
            return deny("HOLD", "reduce_only")
        return None

    def _sym_rate(self, sym, now, limit):
        lst = [t for t in self._acts.get(sym, []) if now - t < 10.0]
        self._acts[sym] = lst
        return len(lst) >= limit

    def note_send(self, sym):
        self._acts.setdefault(sym, []).append(self.ex.clock())

    # ---- вход ----
    def entry(self, p, view):
        """RISK_UP: вход по плану p по живому виду view. allow(...): qty, lev, margin, px, tp, guard, otype, price, post_only, sl, trigger."""
        ex, spec, rt = self.ex, p["spec"], p["rt"]
        sym, side, now = spec["symbol"], spec["side"], ex.clock()
        v = self.base(RISK_UP, sym, p, "place_entry")
        if v is not None:
            return v
        cfg = ex.cfg
        last = view.last
        if last is None or last <= 0 or view.price_age() > float(cfg["entry"]["max_price_age_s"]):
            return deny("STALE_PRICE", "цена старше %s с" % cfg["entry"]["max_price_age_s"])
        if ex.last_reconcile_ok is None or now - ex.last_reconcile_ok > 2.0 * float(cfg["connectivity"]["reconcile_every_s"]):
            return deny("HOLD", "реконсиляция устарела")
        mode = spec.get("mode", "live")
        sess = None
        if mode == "live" and ex.port.env != "paper":
            sess = session.active()
            if not sess:
                return deny("SESSION", "нет активной торговой сессии")
            if sess.get("type") not in (spec.get("session_type"), session.DAY):       # сессия-день: горизонт у плана свой
                return deny("SESSION", "тип сессии %s != плана %s" % (sess.get("type"), spec.get("session_type")))
        bd = self.blackout_deny(now)
        if bd is not None:
            return bd
        busy = ex.symbol_busy(sym, p)
        if busy:
            return deny("SYMBOL_BUSY", busy)
        if mode == "live" and ex.port.env != "paper":
            has_conf = plan_schema.has_confirm(spec.get("entry", {}).get("when"))
            ok, code, detail = protect.check_entry(ex.trades(), sym, now, cfg["protections"], has_conf,
                                                   float((spec.get("risk") or {}).get("size_mult", 1.0)), cfg["entry"]["no_confirm_max_size_mult"])
            if not ok:
                return deny(code, detail)
        if self._sym_rate(sym, now, int(cfg["protections"]["max_actions_per_symbol_per_10s"])):
            return deny("RATE", "частота действий по %s" % sym)
        how = spec["entry"]["how"]
        otype = how.get("type", "market")
        if otype == "limit":
            px = float(how["price"])
        else:
            bid, ask = view.bid, view.ask
            px = (ask if side == "long" else bid) if (ask and bid and ask > 0 and bid > 0) else last
        ins = ex.instrument(sym)
        if ins is None:
            return deny("NO_PORT", "нет данных инструмента %s" % sym)
        paper = mode == "paper" or ex.port.env == "paper"
        bo, cap = self.blackout_warn(now), None
        if bo:
            cap = bo["size_mult"]
        g, err = self.size(spec, px, ins, paper, size_cap=cap)
        if err is not None:
            return err
        return allow(blackout=bo, qty=g["qty"], lev=g["leverage"], margin=g["margin_q"], px=px, tp=weighted_tp(spec), guard=g, otype=otype,
                     price=(float(how["price"]) if otype == "limit" else None), post_only=bool(how.get("post_only", True)),
                     sl=float(spec["stop"]["price"]), trigger=spec["stop"].get("trigger_by", "last"), session=sess)

    def blackout_deny(self, now):
        """ЖЁСТКИЙ отказ по макро-блэкауту для RISK_UP (вход, лимитный вход, добор): live/testnet всегда, paper при blackout_mode=block (tb.core.blackout.check).
        paper при warn -> None (там blackout_warn). Защитные действия (stop/reduce/ladder) сюда не вызываются. -> Verdict(E_BLACKOUT) или None."""
        ex = self.ex
        env = getattr(ex.port, "env", None)
        mode = "paper" if env == "paper" else (env if isinstance(env, str) and env else "live")
        res = blackout.check(ex.root, mode, now, cfg=ex.cfg)
        if not res["blocked"]:
            return None
        return deny(blackout.DENY_CODE, "; ".join(res["reasons"]))

    def blackout_warn(self, now):
        """Макро-окно как ПРЕДУПРЕЖДЕНИЕ: только на площадке paper при paper.blackout_mode=warn (tb.core.blackout.policy); live/testnet/observe -> None
        (там окно по-прежнему запрещает мозг: план дня, валидатор, preflight). -> {"name","in_min","size_mult","code"} внутри окна/за LEAD_MIN до него или None."""
        ex = self.ex
        if getattr(ex.port, "env", None) != "paper" or ex.observe:
            return None
        pol = blackout.policy(ex.root, "paper", cfg=ex.cfg)
        if pol["mode"] != "warn":
            return None
        st = blackout.state(ex.root, now)
        if not st:
            return None
        return dict(st, size_mult=pol["size_mult"], code=blackout.WARN_CODE)

    def size(self, spec, px, ins, paper=False, size_cap=None):
        """guard.evaluate + округление: -> (g с qty, margin_q, None) или (None, Verdict deny). Размер и плечо считает guard, не план.
        size_cap: потолок size_mult плана (макро-окно в paper при warn)."""
        side, sym = spec["side"], spec["symbol"]
        otype = spec["entry"]["how"].get("type", "market")
        ns = argparse.Namespace(sym=sym, side=side, entry=px, sl=float(spec["stop"]["price"]), tp=weighted_tp(spec), equity=None, free=None, no_session=False,
                                score=float((spec.get("risk") or {}).get("score", 0.5)), regime="unknown", min_quote=0.0,
                                contract_max_lev=float(ins.get("max_leverage") or 10), profile=None, exec="maker" if otype == "limit" else "taker",
                                paper=paper, stype=spec.get("session_type"), setup=(spec.get("tags") or {}).get("setup"),
                                agent_limits=agent_limits_of(spec))
        try:
            g = guard.evaluate(ns)
        except Exception as e:
            return None, deny("GUARD", "guard упал: %s" % type(e).__name__)
        if not g.get("ok"):
            return None, deny("GUARD", "; ".join(str(x) for x in (g.get("reasons") or ["без причины"]))[:200])
        step, minq, minn = float(ins["qty_step"]), float(ins["min_qty"]), float(ins.get("min_notional") or 0)
        notional = float(g.get("notional") or g["margin"] * g["leverage"])
        qty = floor_step(notional / px, step)
        sm = float((spec.get("risk") or {}).get("size_mult", 1.0))
        if size_cap is not None:
            sm = min(sm, float(size_cap))
        qty = floor_step(qty * sm, step)
        if qty < minq - 1e-12 or qty * px < minn - 1e-9 or qty <= 0:
            return None, deny("MIN_SIZE", "qty %s < min %s (или номинал %.2f < %s)" % (qty, minq, qty * px, minn))
        g = dict(g, qty=qty, margin_q=round(qty * px / int(g["leverage"]), 4))
        full = notional / px if px > 0 else 0.0
        if full > 0 and g.get("risk_usdt") is not None:     # риск пересчитан на фактический размер (size_mult и округление), а не размер guard
            g["risk_full_usdt"] = g["risk_usdt"]
            g["risk_usdt"] = round(float(g["risk_usdt"]) * min(1.0, qty / full), 4)
        return g, None

    def preview(self, spec, px, ins, paper=False):
        """Для submit: -> {"ok","reasons","qty",...} (guard_preview)."""
        g, err = self.size(spec, px, ins, paper)
        if err is not None:
            return {"ok": err.code != "GUARD", "reasons": [err.detail]} if err.code == "MIN_SIZE" else {"ok": False, "reasons": [err.detail]}
        return g

    # ---- добор (пирамидинг) ----
    def add(self, p, view, rule):
        """RISK_UP: добор по правилу `rule` (action add). guard.evaluate_add + logic.pyramid.check_add. allow(qty, px, guard)."""
        from tb.logic import pyramid
        ex, spec, rt = self.ex, p["spec"], p["rt"]
        sym, side = spec["symbol"], spec["side"]
        v = self.base(RISK_UP, sym, p, "add")
        if v is not None:
            return v
        v = self.blackout_deny(ex.clock())
        if v is not None:
            return v
        cfg = ex.cfg
        if not cfg["pyramid"]["enabled"]:
            return deny("ADD_DISABLED", "pyramid.enabled=false")
        try:
            pol = risklevels.pyramid_policy(None, exec_limits=cfg, agent_limits=agent_limits_of(spec))
        except Exception:       # политика недоступна -> добор запрещён (fail closed)
            return deny("ADD_DISABLED", "политика пирамидинга не прочитана")
        if not pol["level_allows"] or not pol["agent_allows"]:
            return deny("ADD_DISABLED", "добор не разрешён политикой (уровень риска %s%s)" % (pol.get("level"), "" if pol["agent_allows"] else ", allow_add агента=false"))
        pos = rt.get("pos") or {}
        ins = ex.instrument(sym)
        last = view.last
        if ins is None or not last:
            return deny("STALE_PRICE", "нет цены/инструмента")
        exch = ex.exch_pos.get(sym) or {}
        stop = float(exch.get("sl") or pos.get("cur_stop") or 0)
        lim = ex.limits_for(p)
        px = last
        ents = [e for e in pos.get("entries") or [] if e.get("kind", "entry") == "entry"]
        r0 = float(pos.get("risk0") or 0)
        ns = argparse.Namespace(sym=sym, side=side, px=px, stop=stop, qty_open=float(pos.get("qty") or 0), avg_open=float(pos.get("avg") or 0),
                                plan_risk0=r0, risk_add_cap=None, score=float((spec.get("risk") or {}).get("score", 0.5)),
                                contract_max_lev=float(ins.get("max_leverage") or 10), exec="taker", qty_step=ins["qty_step"], min_qty=ins["min_qty"],
                                min_r=float(cfg["pyramid"]["min_r"]), lev_open=int(pos.get("lev") or 1), pyramid_enabled=True,
                                realized_net=float((rt.get("realized") or {}).get("pnl", 0.0)) - float((rt.get("realized") or {}).get("fees", 0.0)),
                                no_session=(spec.get("mode") == "paper"))
        try:
            ga = guard.evaluate_add(ns)
        except Exception as e:
            return deny("ADD_REJECTED", "guard.evaluate_add упал: %s" % type(e).__name__)
        ga = dict(ga, plan_risk0=r0)
        res = pyramid.check_add(spec, rt, ex.view(sym), lim, rule, px, ga)
        if not res["ok"]:
            return deny("ADD_REJECTED", ",".join(res["codes"]) or "guard: " + "; ".join(ga.get("reasons") or []))
        return allow(qty=res["qty_max"], px=px, guard=ga, risk_add=res["risk_add"], stop=stop)

    # ---- стоп ----
    def stop(self, p, to, by="rule", price=None):
        """RISK_DOWN: подтянуть стоп до `to`. Монотонность относительно БИРЖЕВОГО стопа, шаг, интервал, зазор от цены, частота."""
        ex, spec, rt = self.ex, p["spec"], p["rt"]
        sym, side, now = spec["symbol"], spec["side"], ex.clock()
        v = self.base(RISK_DOWN, sym, p, "set_stop")
        if v is not None:
            return v
        pos = rt.get("pos") or {}
        exch = float((ex.exch_pos.get(sym) or {}).get("sl") or 0)
        mine = float(pos.get("cur_stop") or 0)
        cur = exch if exch > 0 else mine
        # нарушение монотонности относительно собственного представления плана: ошибка движка правил (защита в глубину)
        if by == "rule" and mine > 0 and not tighter(side, to, mine) and to != mine:
            return deny("INVARIANT_VIOLATION", "новый стоп %s слабее стопа плана %s" % (to, mine))
        if not tighter(side, to, cur):
            return deny("NOT_TIGHTER" if to == cur else "STOP_NOT_TIGHTER", "новый %s, биржевой %s" % (to, cur))
        sm = ex.cfg["stop_moves"]
        if by == "rule":
            tick = float((ex.instrument(sym) or {}).get("tick") or 0)
            if tick > 0 and cur > 0 and abs(to - cur) < float(sm["min_step_ticks"]) * tick - 1e-12:
                return deny("STOP_STEP", "шаг меньше %s тиков" % sm["min_step_ticks"])
            last_mv = (rt.get("counters") or {}).get("last_stop_move")
            if last_mv is not None and now - float(last_mv) < float(sm["min_interval_s"]):
                return deny("STOP_INTERVAL", "интервал меньше %s с" % sm["min_interval_s"])
        hist = [t for t in (rt.get("counters") or {}).get("stop_moves_1h", []) if now - float(t) < 3600.0]
        if by == "rule" and len(hist) >= int(sm["max_moves_per_hour"]):
            return deny("STOP_RATE", "ходов стопа за час %d" % len(hist))
        last = price if price is not None else (ex.px.get(sym) or {}).get("last")
        if last and by != "protect":
            gap = float(sm["min_gap_bps_from_price"]) / 1e4
            if (side == "long" and to > last * (1 - gap) + 1e-12) or (side == "short" and to < last * (1 + gap) - 1e-12):
                return deny("STOP_TOO_CLOSE", "стоп %s ближе %s бп к цене %s" % (to, sm["min_gap_bps_from_price"], last))
        return allow(cur=cur, to=to)

    # ---- прочее ----
    def reduce(self, p, what):
        """RISK_DOWN: закрыть / снять ордер / придвинуть тейк. Только kill-проверка нужна лишь для классов выше; здесь observe и порт."""
        v = self.base(RISK_DOWN, p["spec"]["symbol"] if p else None, p, what)
        return v if v is not None else allow()

    def ladder(self, p):
        """NEUTRAL: постановка лесенки reduce-only."""
        v = self.base(NEUTRAL, p["spec"]["symbol"], p, "place_target")
        return v if v is not None else allow()
