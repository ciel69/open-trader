"""Действия исполнителя: перевод намерения в вызовы порта с WAL и идемпотентностью (02 п. 2.6, 2.12). Владелец: WP5.

Единая точка записи на биржу: Actions.call(). Перед ЛЮБОЙ записью: KILL для действий, увеличивающих риск (файл читается заново), режим observe,
темп записей (rest_write_rps). Перед отправкой Actions.begin() дописывает в журнал решений `intent` с fsync и сохраняет `actions.pending` в
runtime.json плана; после ответа Actions.end() пишет `result` и снимает pending. Слепых повторов нет: ошибка `unknown` оставляет pending до проверки
по link_id (order_by_link), `rejected` повторяется только по новому решению правил.

Монотонность стопа здесь проверяется ЕЩЁ РАЗ независимо от Gate и правил: перед каждым set_stop читается свежая позиция с биржи (REST) и новый стоп
обязан быть строже биржевого (биржа сама ослабление РАЗРЕШАЕТ). Любые массовые отмены только reduce-only ордеров по одному или с orderFilter=Order
(cancel_all порта): без этого Bybit снимает стоп позиции.
"""
import math
import time

from tb.core import linkid
from tb.executor import gate as G
from tb.executor.ports import (KIND_NETWORK, KIND_POSITION_GONE, KIND_RATE, KIND_REJECTED, KIND_UNKNOWN, PortError)

ORD_TERMINAL = ("Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled", "Expired")
ORD_LIVE = ("New", "PartiallyFilled", "Untriggered")
NOT_SENT_AFTER_S = 15.0       # intent без результата и без ордера на бирже дольше этого = не отправлено (02 п. 2.6)
EPS = 1e-9


class WalError(Exception):
    """Не удалось записать намерение в журнал решений: вход/добор не отправляются (закрытая дверь)."""


class Denied(Exception):
    """Запись на биржу запрещена в точке отправки (KILL/observe); code из gate.DENY_CODES."""

    def __init__(self, code, detail=""):
        Exception.__init__(self, "%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def close_side(side):
    """Сторона закрывающего ордера для позиции side."""
    return "sell" if side == "long" else "buy"


def ladder_split(qty, targets, ins):
    """Раскладка количества по целям (01 п. 1.4), согласована с plan_schema.ladder_parts: -> [(target_id, qty)] (слитые в следующую цели: qty 0.0).

    >>> ladder_split(0.3, [{"id": "t1", "frac": 0.5, "price": 1.0}, {"id": "t2", "frac": 0.5, "price": 2.0}], {"qty_step": "0.1", "min_qty": "0.1", "min_notional": "0"})
    [('t1', 0.1), ('t2', 0.2)]
    """
    step, minq, minn = float(ins["qty_step"]), float(ins.get("min_qty") or 0), float(ins.get("min_notional") or 0)
    total = int(math.floor(qty / step + 1e-9))
    fr = [float(t["frac"]) for t in targets]
    steps = [int(math.floor(total * f + 1e-9)) for f in fr]
    if fr and sum(fr) >= 1 - 1e-9:
        steps[-1] = total - sum(steps[:-1])
    mq = int(math.ceil(minq / step - 1e-9))
    out, carry = [], 0
    for i, s in enumerate(steps):
        s += carry
        carry = 0
        px = float(targets[i].get("price") or 0)
        small = s < mq or (px and minn and s * step * px < minn)
        if small and i < len(steps) - 1:
            carry = s
            out.append([targets[i]["id"], 0])
            continue
        out.append([targets[i]["id"], s])
    nz = [i for i, x in enumerate(out) if x[1] > 0]
    if len(nz) > 1 and (out[nz[-1]][1] < mq or out[nz[-1]][1] <= 0):
        out[nz[-2]][1] += out[nz[-1]][1]
        out[nz[-1]][1] = 0
    return [(tid, round(s * step, 12)) for tid, s in out]


class Actions(object):
    def __init__(self, ex):
        self.ex = ex
        self._last_write = 0.0

    # ------------------------------------------------------------------ WAL
    def begin(self, p, what, args, klass, why="", link=True, basis=None):
        """Резервирует action_id/link_id, сохраняет pending (sync) и пишет intent (fsync) ДО отправки. -> A.
        WalError для RISK_UP/NEUTRAL, если intent не записан; для RISK_DOWN действие всё равно выполняется (защита важнее аудита)."""
        ex, rt = self.ex, p["rt"]
        act = rt.setdefault("actions", {"seq": 0, "pending": []})
        act["seq"] = int(act.get("seq", 0)) + 1
        n = int(rt.get("plan_n") or 0)
        aid = "a-%d-%03d" % (n, act["seq"])
        lnk = linkid.make(n, act["seq"], ex.venue_id) if link else None
        act.setdefault("pending", []).append(aid)
        info = act.setdefault("info", {})
        info[aid] = {"what": what, "link": lnk, "t": round(ex.clock(), 3), "klass": klass}
        for k in ("to", "tid", "qty", "sym"):
            if k in (args or {}):
                info[aid][k] = args[k]
        ex.save(p, sync=True)
        A = {"id": aid, "link": lnk, "what": what, "klass": klass, "t0": ex.clock()}
        try:
            ex.decide("intent", what, plan=p["spec"].get("id"), rev=rt.get("rev_applied"), args=args, why=why, action_id=aid, fsync=True,
                      res={"link_id": lnk} if lnk else None, basis=basis)
        except Exception as e:      # журнал решений недоступен
            if klass != G.RISK_DOWN:
                act["pending"].remove(aid)
                info.pop(aid, None)
                ex.save(p)
                raise WalError("intent не записан: %s" % type(e).__name__)
        return A

    def end(self, p, A, ok, code="OK", res=None, order_id=None, keep_pending=False):
        """Результат действия: запись `result` и снятие pending (кроме keep_pending: исход неизвестен, проверяется по link_id)."""
        ex, rt = self.ex, p["rt"]
        r = {"ok": ok, "code": code, "exch_ms": int((ex.clock() - A["t0"]) * 1000)}
        if A.get("link"):
            r["link_id"] = A["link"]
        if order_id:
            r["order_id"] = order_id
        if res:
            r.update(res)
        try:
            ex.decide("result", A["what"], plan=p["spec"].get("id"), rev=rt.get("rev_applied"), res=r, action_id=A["id"])
        except Exception:
            pass
        if not keep_pending:
            act = rt.get("actions") or {}
            if A["id"] in act.get("pending", []):
                act["pending"].remove(A["id"])
            (act.get("info") or {}).pop(A["id"], None)
        ex.save(p, sync=True)

    # ------------------------------------------------------------------ единая точка записи
    def call(self, op, klass, *args, **kw):
        """Запись/чтение порта с проверками точки отправки. Побочные эффекты только через port.<op>."""
        ex = self.ex
        if klass in (G.RISK_UP, G.NEUTRAL):
            from tb.core import exec_cfg
            k = exec_cfg.kill_state(ex.root)
            if k is not None:
                raise Denied("KILL", k.get("reason") or "state/KILL")
        if ex.observe:
            raise Denied("OBSERVE", "режим --observe: %s не отправляется" % op)
        if ex.port is None:
            raise Denied("NO_PORT", "порт биржи недоступен")
        self.pace()
        try:
            r = getattr(ex.port, op)(*args, **kw)
        except PortError as e:
            ex.rest_event(e)
            raise
        ex.rest_event(None)
        return r

    def pace(self):
        """Токен-бакет записей: не чаще rest_write_rps (между записями минимум 1/rps с)."""
        ex = self.ex
        rps = float(ex.cfg["rate"]["rest_write_rps"])
        wait = self._last_write + 1.0 / rps - ex.clock()
        if wait > 0:
            ex.sleep(min(wait, 1.0))
        self._last_write = ex.clock()

    # ------------------------------------------------------------------ вход
    def place_entry(self, p, vd, why=""):
        """Отправка входного ордера по вердикту Gate. -> ("placed"|"denied"|"rejected"|"transient"|"unknown", info)."""
        ex, spec, rt = self.ex, p["spec"], p["rt"]
        sym, side, pr = spec["symbol"], spec["side"], vd.params
        ent = rt["entry"]
        now = ex.clock()
        ent.update({"state": "placing", "placed_ts": round(now, 3), "qty": pr["qty"], "lev": pr["lev"], "margin": pr["margin"], "otype": pr["otype"],
                    "fills": [], "first_fill_ts": None, "unknown": False, "order_id": None, "px_plan": pr["px"], "partial": False})
        args = {"qty": pr["qty"], "lev": pr["lev"], "px": pr["px"], "sl": pr["sl"], "margin": pr["margin"], "type": pr["otype"]}
        try:
            A = self.begin(p, "place_entry", args, G.RISK_UP, why=why, basis={"px": pr["px"]})
        except WalError as e:
            self._revert_entry(p)
            return "denied", {"code": "HOLD", "detail": str(e)}
        ent["link_id"] = A["link"]
        ex.save(p, sync=True)
        try:
            self.call("set_leverage", G.RISK_UP, sym, pr["lev"])
            r = self.call("place_entry", G.RISK_UP, sym, side, pr["qty"], A["link"], pr["otype"], price=pr["price"], post_only=pr["post_only"],
                          sl=pr["sl"], sl_trigger=pr["trigger"])
        except Denied as d:
            self.end(p, A, False, d.code)
            self._revert_entry(p)
            return "denied", {"code": d.code, "detail": d.detail}
        except PortError as e:
            return self._entry_error(p, A, e)
        ex.gate.note_send(sym)
        oid = (r or {}).get("order_id")
        ent["order_id"] = oid
        ent["state"] = "placing" if pr["otype"] == "market" else "resting"
        self.end(p, A, True, "OK", order_id=oid)
        return "placed", {"link": A["link"], "order_id": oid}

    def _revert_entry(self, p):
        ent = p["rt"]["entry"]
        ent.update({"state": "waiting", "link_id": None, "order_id": None, "fills": [], "first_fill_ts": None, "unknown": False})
        self.ex.save(p, sync=True)

    def _entry_error(self, p, A, e):
        ent = p["rt"]["entry"]
        if e.kind == KIND_UNKNOWN:
            ent["unknown"] = True
            self.end(p, A, None, "UNKNOWN", res={"err": str(e.code or "")}, keep_pending=True)
            return "unknown", {"code": str(e.code or "")}
        self.end(p, A, False, str(e.code or e.kind), res={"kind": e.kind})
        self._revert_entry(p)
        if e.kind in (KIND_RATE, KIND_NETWORK):
            return "transient", {"code": str(e.code or e.kind)}
        return "rejected", {"code": str(e.code or e.kind), "detail": e.msg}

    def poll_entry(self, p):
        """Проверка входного ордера по link_id (REST), независимо от WS. -> ("wait"|"filled"|"failed", info).
        filled: info = {"fills": [...], "partial": bool}. Вход рынком завершён, когда ордер Filled или после fill_wait_s с первого исполнения."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym, ent = spec["symbol"], rt["entry"]
        link, now = ent.get("link_id"), ex.clock()
        if not link or ent.get("state") not in ("placing", "resting"):
            return "wait", {}
        try:
            o = ex.port.order_by_link(sym, link)
            exs = ex.port.executions(int((float(ent.get("placed_ts") or now) - 5) * 1000), sym)
            ex.rest_event(None)
        except PortError as e:
            ex.rest_event(e)
            return "wait", {}
        fills = {f["exec_id"]: f for f in ent.get("fills") or []}
        for e_ in exs:
            if e_.get("link_id") == link and e_.get("exec_type", "Trade") in ("Trade", "", None) and not float(e_.get("closed_size") or 0) > 0:
                fills[e_["exec_id"]] = {"exec_id": e_["exec_id"], "px": float(e_["price"]), "qty": float(e_["qty"]), "fee": float(e_.get("fee") or 0),
                                        "ts": float(e_["ts_ms"]) / 1000.0}
        ent["fills"] = sorted(fills.values(), key=lambda f: f["ts"])
        filled = sum(f["qty"] for f in ent["fills"])
        status = (o or {}).get("status")
        age = now - float(ent.get("placed_ts") or now)
        if o is None and not filled:
            if age > NOT_SENT_AFTER_S:
                self._clear_pending_for(p, link, "NOT_SENT")
                self._revert_entry(p)
                return "failed", {"code": "NOT_SENT"}
            return "wait", {}
        if filled > 0:
            if not ent.get("first_fill_ts"):
                ent["first_fill_ts"] = ent["fills"][0]["ts"]
            waited = now - float(ent["first_fill_ts"])
            done = status in ORD_TERMINAL or waited >= float(ex.cfg["entry"]["fill_wait_s"]) or (status == "Filled")
            if o and status in ORD_LIVE and (ent.get("otype") == "limit" or waited >= float(ex.cfg["entry"]["fill_wait_s"])):
                try:      # остаток снять: усреднения нет (как bybit_limit.process_one); потом перечитать (гонка заполнения)
                    self.call("cancel_order", G.RISK_DOWN, sym, link)
                except (PortError, Denied):
                    pass
                try:
                    o2 = ex.port.order_by_link(sym, link)
                    exs2 = ex.port.executions(int((float(ent.get("placed_ts") or now) - 5) * 1000), sym)
                    for e_ in exs2:
                        if e_.get("link_id") == link and not float(e_.get("closed_size") or 0) > 0:
                            fills[e_["exec_id"]] = {"exec_id": e_["exec_id"], "px": float(e_["price"]), "qty": float(e_["qty"]),
                                                    "fee": float(e_.get("fee") or 0), "ts": float(e_["ts_ms"]) / 1000.0}
                    ent["fills"] = sorted(fills.values(), key=lambda f: f["ts"])
                    filled = sum(f["qty"] for f in ent["fills"])
                    o = o2 or o
                except PortError:
                    pass
                done = True
            if done:
                partial = filled < float(ent.get("qty") or 0) - 1e-9
                self._clear_pending_for(p, link, "FILLED")
                return "filled", {"fills": ent["fills"], "partial": partial}
            return "wait", {}
        if status in ORD_TERMINAL:      # отменён/отклонён без заполнения (post-only пересёк стакан, ручная отмена)
            self._clear_pending_for(p, link, status)
            self._revert_entry(p)
            return "failed", {"code": (o or {}).get("cancel_type") or status}
        return "wait", {}

    def _clear_pending_for(self, p, link, code):
        """Снять pending-действие входа с этим link (после проверки исхода)."""
        rt = p["rt"]
        act = rt.get("actions") or {}
        for aid, inf in list((act.get("info") or {}).items()):
            if inf.get("link") == link:
                if aid in act.get("pending", []):
                    act["pending"].remove(aid)
                act["info"].pop(aid, None)
                try:
                    self.ex.decide("result", inf.get("what", "place_entry"), plan=p["spec"].get("id"), res={"ok": code == "FILLED", "code": code, "link_id": link,
                                   "recovered": True}, action_id=aid)
                except Exception:
                    pass
        self.ex.save(p, sync=True)

    def cancel_entry_order(self, p, why=""):
        """Снять ждущий входной ордер плана (RISK_DOWN). -> "cancelled"|"gone"|"filled"|"error". 'filled': ордер успел заполниться (гонка)."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        ent, sym = rt["entry"], spec["symbol"]
        link = ent.get("link_id")
        if not link or ent.get("state") not in ("placing", "resting"):
            return "gone"
        try:
            A = self.begin(p, "cancel_order", {"qty": ent.get("qty")}, G.RISK_DOWN, why=why)
        except WalError:
            return "error"
        try:
            self.call("cancel_order", G.RISK_DOWN, sym, link)
            self.end(p, A, True)
        except (PortError, Denied) as e:
            self.end(p, A, False, str(getattr(e, "code", None) or getattr(e, "kind", "ERR")))
            return "error"
        try:
            o = ex.port.order_by_link(sym, link)
            if o and float(o.get("filled") or 0) > 0:
                return "filled"
        except PortError:
            pass
        return "cancelled"

    # ------------------------------------------------------------------ лесенка
    def place_ladder(self, p, why="LADDER"):
        """После входа: reduce-only лимиты по целям exec=resting; exec=touch только фиксируются. Размеры уточняются по open_orders биржи."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym, side = spec["symbol"], spec["side"]
        ins = ex.instrument(sym)
        pos = rt["pos"]
        parts = ladder_split(float(pos["qty"]), spec["targets"], ins)
        tg = rt.setdefault("targets", {})
        placed = 0
        for t, (tid, q) in zip(spec["targets"], parts):
            cur = tg.get(tid) or {}
            if cur.get("state") in ("resting", "filled", "touch") and cur.get("qty"):
                continue
            if q <= 0:
                tg[tid] = {"state": "cancelled", "qty": 0.0, "price": t["price"], "link": None, "filled_qty": 0.0}
                continue
            if t.get("exec", "resting") == "touch":
                tg[tid] = {"state": "touch", "qty": q, "price": t["price"], "link": None, "filled_qty": 0.0}
                continue
            tg[tid] = {"state": "pending", "qty": q, "price": t["price"], "link": None, "filled_qty": 0.0}
            vd = ex.gate.ladder(p)
            if not vd:
                continue
            try:
                A = self.begin(p, "place_target", {"tid": tid, "qty": q, "px": t["price"]}, G.NEUTRAL, why=why)
            except WalError:
                continue
            try:
                self.call("place_reduce_only", G.NEUTRAL, sym, close_side(side), q, A["link"], price=t["price"], post_only=False)
                tg[tid].update({"state": "resting", "link": A["link"]})
                self.end(p, A, True)
                placed += 1
            except Denied as d:
                self.end(p, A, False, d.code)
            except PortError as e:
                self.end(p, A, False, str(e.code or e.kind), res={"kind": e.kind})
                if e.kind == KIND_POSITION_GONE:
                    break
        ex.save(p, sync=True)
        if placed:
            self.trim_from_exchange(p)
        return placed

    def trim_from_exchange(self, p):
        """Биржа молча обрезает reduce-only до свободного остатка: фактические qty ордеров лесенки берутся из open_orders."""
        ex, rt = self.ex, p["rt"]
        try:
            orders = {o["link_id"]: o for o in ex.port.open_orders(p["spec"]["symbol"])}
        except PortError as e:
            ex.rest_event(e)
            return
        for tid, t in (rt.get("targets") or {}).items():
            o = orders.get(t.get("link"))
            if o and t.get("state") == "resting" and abs(float(o["qty"]) - float(t["qty"])) > 1e-9:
                t["qty"] = float(o["qty"])
        ex.save(p)

    def resync_ladder(self, p, why="RESIZE"):
        """Размер позиции изменился: суммарный остаток лесенки не должен превышать позицию. Лишнее снимаем с дальних целей (cancel/amend)."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym = spec["symbol"]
        remaining = float(rt["pos"].get("qty") or 0)
        tg = rt.get("targets") or {}
        resting = [(tid, t) for tid, t in sorted(tg.items()) if t.get("state") == "resting"]
        total = sum(float(t["qty"]) - float(t.get("filled_qty") or 0) for _, t in resting)
        excess = total - remaining
        if excess <= 1e-9:
            return 0
        ins = ex.instrument(sym)
        step = float(ins["qty_step"])
        n = 0
        for tid, t in reversed(resting):
            if excess <= 1e-9:
                break
            left = float(t["qty"]) - float(t.get("filled_qty") or 0)
            try:
                A = self.begin(p, "cancel_order" if left <= excess + 1e-9 else "amend_order", {"tid": tid, "qty": left}, G.RISK_DOWN, why=why, link=False)
            except WalError:
                break
            try:
                if left <= excess + 1e-9:
                    self.call("cancel_order", G.RISK_DOWN, sym, t["link"])
                    t["state"] = "cancelled"
                    excess -= left
                else:
                    newq = G.floor_step(left - excess, step)
                    self.call("amend_order", G.RISK_DOWN, sym, t["link"], qty=newq + float(t.get("filled_qty") or 0))
                    t["qty"] = newq + float(t.get("filled_qty") or 0)
                    excess = 0.0
                self.end(p, A, True)
                n += 1
            except (PortError, Denied) as e:
                self.end(p, A, False, str(getattr(e, "code", None) or getattr(e, "kind", "ERR")))
        ex.save(p, sync=True)
        return n

    def move_target(self, p, tid, price, why=""):
        """Придвинуть/отодвинуть цель (правило move_target): amend цены reduce-only лимита. -> bool."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        t = (rt.get("targets") or {}).get(tid)
        if not t:
            return False
        vd = ex.gate.reduce(p, "amend_order")
        if not vd:
            return False
        if t.get("state") != "resting":
            t["price"] = price
            ex.save(p)
            return True
        try:
            A = self.begin(p, "amend_order", {"tid": tid, "px": price}, G.RISK_DOWN, why=why, link=False)
        except WalError:
            return False
        try:
            self.call("amend_order", G.RISK_DOWN, spec["symbol"], t["link"], price=price)
            t["price"] = price
            self.end(p, A, True)
            return True
        except (PortError, Denied) as e:
            self.end(p, A, False, str(getattr(e, "code", None) or getattr(e, "kind", "ERR")))
            return False

    def cancel_target(self, p, tid, why=""):
        t = (p["rt"].get("targets") or {}).get(tid)
        if not t or t.get("state") != "resting":
            return False
        try:
            A = self.begin(p, "cancel_order", {"tid": tid}, G.RISK_DOWN, why=why, link=False)
        except WalError:
            return False
        try:
            self.call("cancel_order", G.RISK_DOWN, p["spec"]["symbol"], t["link"])
            t["state"] = "cancelled"
            self.end(p, A, True)
            return True
        except (PortError, Denied) as e:
            self.end(p, A, False, str(getattr(e, "code", None) or getattr(e, "kind", "ERR")))
            return False

    # ------------------------------------------------------------------ стоп
    def move_stop(self, p, to, by="rule", why="", price=None, vd=None):
        """Подтянуть стоп. -> ("ok"|"unchanged"|"denied"|"gone"|"transient"|"error", info). Проверки: Gate, затем СВОЯ независимая монотонность
        по свежему чтению позиции с биржи, затем монотонность порта."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym, side = spec["symbol"], spec["side"]
        vd = vd or ex.gate.stop(p, to, by=by, price=price)
        if not vd:
            return "denied", {"code": vd.code, "detail": vd.detail}
        prev = vd.params.get("cur")
        try:
            A = self.begin(p, "set_stop", {"to": to, "prev": prev, "sym": sym}, G.RISK_DOWN, why=why, link=False, basis={"px": price})
        except WalError:
            return "error", {"code": "WAL"}
        try:
            cur_pos = [x for x in ex.port.positions() if x["sym"] == sym and x["size"] > 0]
            ex.rest_event(None)
        except PortError as e:
            ex.rest_event(e)
            self.end(p, A, False, "NO_READ", res={"kind": e.kind})
            return "transient", {"code": "NO_READ"}
        if not cur_pos:
            self.end(p, A, False, "position_gone")
            return "gone", {}
        fresh = float(cur_pos[0].get("sl") or 0)
        ex.exch_pos[sym] = dict(cur_pos[0])
        if not G.tighter(side, to, fresh) and fresh > 0:
            self.end(p, A, False, "STOP_NOT_TIGHTER", res={"cur": fresh})
            if fresh > 0 and to == fresh:
                return "unchanged", {"stop": fresh}
            return "denied", {"code": "STOP_NOT_TIGHTER", "detail": "биржевой стоп %s строже %s" % (fresh, to)}
        try:
            r = self.call("set_stop", G.RISK_DOWN, sym, side, to, trigger_by=spec["stop"].get("trigger_by", "last"))
        except Denied as d:
            self.end(p, A, False, d.code)
            return "denied", {"code": d.code}
        except PortError as e:
            return self._stop_error(p, A, e, to)
        new = float((r or {}).get("stop") or to)
        unchanged = bool((r or {}).get("unchanged"))
        self._stop_done(p, A, new, unchanged, by, prev)
        return ("unchanged" if unchanged else "ok"), {"stop": new}

    def _stop_done(self, p, A, new, unchanged, by, prev):
        ex, rt = self.ex, p["rt"]
        sym = p["spec"]["symbol"]
        pos = rt["pos"]
        pos["cur_stop"] = new
        if sym in ex.exch_pos:
            ex.exch_pos[sym]["sl"] = new
        if not unchanged:
            c = rt.setdefault("counters", {})
            now = ex.clock()
            c["stop_moves_1h"] = [t for t in c.get("stop_moves_1h", []) if now - float(t) < 3600.0] + [round(now, 3)]
            c["last_stop_move"] = round(now, 3)
        self.end(p, A, True, "OK", res={"stop": new, "unchanged": unchanged})
        if not unchanged:
            ex.on_stop_moved(p, prev, new, by)

    def _stop_error(self, p, A, e, to):
        ex = self.ex
        sym, side = p["spec"]["symbol"], p["spec"]["side"]
        code = str(e.code or e.kind)
        if e.kind == KIND_POSITION_GONE:
            self.end(p, A, False, "position_gone")
            return "gone", {}
        if e.kind == KIND_UNKNOWN:      # исход неизвестен: сверяем со свежей позицией; слепого повтора нет
            try:
                ps = [x for x in ex.port.positions() if x["sym"] == sym and x["size"] > 0]
            except PortError:
                ps = None
            if ps and abs(float(ps[0].get("sl") or 0) - to) < 1e-9 * max(1.0, to) + 1e-9:
                self._stop_done(p, A, float(ps[0]["sl"]), False, "rule", None)
                return "ok", {"stop": float(ps[0]["sl"]), "recovered": True}
            self.end(p, A, None, "UNKNOWN", keep_pending=False)
            return "transient", {"code": "UNKNOWN"}
        if e.kind in (KIND_RATE, KIND_NETWORK):
            self.end(p, A, False, code, res={"kind": e.kind})
            return "transient", {"code": code}
        self.end(p, A, False, code, res={"kind": e.kind})
        if code == "STOP_NOT_TIGHTER":
            return "denied", {"code": "STOP_NOT_TIGHTER"}
        return "error", {"code": code, "detail": e.msg}

    # ------------------------------------------------------------------ закрытие
    CLOSE_RETRY_S = 10.0     # полное закрытие уже отправлено: повтор раньше этого срока снимал бы живой ордер (cancel_all) и ставил новый, который не успевал исполниться

    def close_inflight(self, p):
        """True, если полное закрытие отправлено меньше CLOSE_RETRY_S секунд назад и ещё не завершено (rt.close.pending)."""
        c = p["rt"].get("close") or {}
        return bool(c.get("pending")) and self.ex.clock() - float(c.get("ts") or 0.0) < self.CLOSE_RETRY_S

    def close(self, p, qty, reason, why="", full=False):
        """Закрыть позицию (reduce-only market). full: сначала снять лесенку (только reduce-only ордера), затем закрыть всю позицию.
        -> ("ok"|"gone"|"denied"|"error"|"transient", info)."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym, side = spec["symbol"], spec["side"]
        if full and self.close_inflight(p):
            return "transient", {"code": "CLOSE_PENDING"}
        vd = ex.gate.reduce(p, "close")
        if not vd:
            return "denied", {"code": vd.code}
        try:
            A = self.begin(p, "close", {"qty": qty if qty else rt["pos"].get("qty"), "sym": sym}, G.RISK_DOWN, why=why)
        except WalError:
            return "error", {"code": "WAL"}
        if full:
            rt.setdefault("close", {})["reason"] = reason
            rt["close"]["pending"] = True
            rt["close"]["ts"] = ex.clock()
            try:
                self.call("cancel_all", G.RISK_DOWN, sym, reduce_only_only=True)
                for t in (rt.get("targets") or {}).values():
                    if t.get("state") == "resting":
                        t["state"] = "cancelled"
            except (PortError, Denied):
                pass     # ладдер снимется биржей при закрытии позиции сама
        try:
            r = self.call("close_market", G.RISK_DOWN, sym, close_side(side), None if full else qty, A["link"])
        except Denied as d:
            self.end(p, A, False, d.code)
            return "denied", {"code": d.code}
        except PortError as e:
            if e.kind == KIND_POSITION_GONE:
                self.end(p, A, False, "position_gone")
                return "gone", {}
            if e.kind == KIND_UNKNOWN:
                self.end(p, A, None, "UNKNOWN", keep_pending=True)
                return "transient", {"code": "UNKNOWN"}
            self.end(p, A, False, str(e.code or e.kind), res={"kind": e.kind})
            return ("transient" if e.kind in (KIND_RATE, KIND_NETWORK) else "error"), {"code": str(e.code or e.kind), "detail": e.msg}
        self.end(p, A, True, "OK", order_id=(r or {}).get("order_id"))
        return "ok", {"link": A["link"]}

    def close_by_protect(self, p, why):
        """Аварийное закрытие без стопа/по защите."""
        ex = self.ex
        ex.decide("protect", "close_no_stop", plan=p["spec"].get("id"), why=why)
        return self.close(p, None, "risk", why=why, full=True)

    # ------------------------------------------------------------------ добор
    def add(self, p, vd, why=""):
        """Добор по пирамидингу market со СТОПОМ, равным текущему (стоп не расширяется). -> ("ok"|"denied"|"error"|"transient", info)."""
        ex, rt, spec = self.ex, p["rt"], p["spec"]
        sym, side = spec["symbol"], spec["side"]
        qty, stop = vd.params["qty"], vd.params["stop"]
        try:
            A = self.begin(p, "add", {"qty": qty, "px": vd.params["px"], "sl": stop}, G.RISK_UP, why=why)
        except WalError:
            return "denied", {"code": "HOLD"}
        try:
            r = self.call("place_entry", G.RISK_UP, sym, side, qty, A["link"], "market", sl=stop, sl_trigger=spec["stop"].get("trigger_by", "last"))
        except Denied as d:
            self.end(p, A, False, d.code)
            return "denied", {"code": d.code}
        except PortError as e:
            if e.kind == KIND_UNKNOWN:
                self.end(p, A, None, "UNKNOWN", keep_pending=True)
                return "transient", {"code": "UNKNOWN"}
            self.end(p, A, False, str(e.code or e.kind), res={"kind": e.kind})
            return ("transient" if e.kind in (KIND_RATE, KIND_NETWORK) else "error"), {"code": str(e.code or e.kind)}
        ex.gate.note_send(sym)
        self.end(p, A, True, "OK", order_id=(r or {}).get("order_id"))
        return "ok", {"link": A["link"], "qty": qty}
