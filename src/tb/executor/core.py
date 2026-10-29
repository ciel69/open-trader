"""Executor: СИНХРОННАЯ машина состояний исполнителя (02 п. 2.3). Владелец: WP5.

Никаких await и собственных сетевых вызовов: побочные эффекты только через VenuePort (записи через Actions/Gate), PlanStore, журнал решений,
шину и журнал сделок. События обрабатываются по одному (daemon.py держит один рабочий поток и очередь): on_ticker, on_kline, on_private,
on_state, on_timer(now), on_command(cmd), poll_inbox(). Время только через self.clock(), поэтому с FakeClock/FakePort/FakeFeed тесты детерминированы.

Раскладка: gate.py (допуск), actions.py (WAL и вызовы порта), reconcile.py (сверка с биржей), commands.py (submit/amend/stop/close/cancel/hold/
release/adopt), protect.py (паузы из журнала), mdsvc.py (heartbeat/snapshot/levels/instruments), scanbridge.py (идеи сканера -> шина).
Монотонность стопа: правила -> Gate -> Actions (свежее чтение биржи) -> порт; нарушение это не игнор, а INVARIANT_VIOLATION (событие error + запись).
"""
import argparse
import collections
import contextlib
import hashlib
import io
import json
import os
import time

from tb import paths
from tb.core import bus, decisions, exec_cfg, ideas, journal, linkid, plan_schema, portfolio, session, tradepath
from tb.core.plan_store import PlanStore
from tb.executor import actions as A
from tb.executor import commands, gate as G, mdsvc, protect, reconcile
from tb.executor.ports import KIND_UNKNOWN, PortError
from tb.logic import rules
from tb.logic.candles import CandleBook, MarketView, tf_min

VERSION = "v2.0-wp5"
ACTIVE = ("armed", "entered", "managing")
IN_POS = ("entered", "managing")
EPS = 1e-9
SEEN_MAX = 5000
KLINE_TFS = (1, 5, 15, 60)
EVAL_MIN_GAP_S = 0.1
ENTRY_POLL_S = 0.5
LIMIT_POLL_S = 2.0
WHY_BUS = {"GUARD": "GUARD", "CHASE": "CHASE", "COOLDOWN": "COOLDOWN", "STALE": "STALE", "SPREAD": "SPREAD", "MIN_SIZE": "MIN_SIZE"}


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dmerge(dst, patch):
    """Глубокое слияние словарей (rt_patch правил): вложенные словари сливаются, остальное заменяется."""
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            dmerge(dst[k], v)
        else:
            dst[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
    return dst


def new_runtime(now):
    """Начальный runtime.json плана (06 п. 6.7)."""
    return {"status": "draft", "rev_applied": 1, "hold": None, "hold_reason": None,
            "ts": {"created": round(now, 3), "armed": None, "entered": None, "closed": None},
            "entry": {"state": "waiting", "link_id": None, "order_id": None, "attempts": 0, "last_attempt": None, "blocked_why": None,
                      "cond_state": {}, "already": {}, "ref_px": None, "fired_px": None},
            "pos": {"qty": 0.0, "avg": None, "entries": [], "initial_stop": None, "cur_stop": None, "r_unit": None, "best_px": None, "worst_px": None,
                    "t_best": None, "t_worst": None, "journal_id": None},
            "targets": {}, "rules": {}, "adds": {"n": 0, "risk_used": 0.0},
            "realized": {"pnl": 0.0, "fees": 0.0, "parts": []}, "close": {"reason": None, "px": None, "pnl": None},
            "actions": {"seq": 0, "pending": []}, "counters": {"stop_moves_1h": [], "blocked_attempts": 0}, "paper": False}


MIN_SIZE_TTL_S = exec_cfg.MIN_SIZE_TTL_S


class Executor(object):
    def __init__(self, port, root=None, clock=time.time, sleep=None, profile="testnet", observe=False, cfg=None, keep_entries=False, venue_id="bybit"):
        self.port = port
        self.venue_id = venue_id or "bybit"      # биржа этого исполнителя (параллельные исполнители: по одному на биржу, docs/EXCHANGES.md)
        self.root = root
        self.clock = clock
        self.sleep = sleep or time.sleep
        self.profile = profile
        self.observe = bool(observe)
        self.keep_entries = bool(keep_entries)
        self.cfg = cfg if cfg is not None else exec_cfg.load(root)
        self.store = PlanStore(root)
        self.books = {}
        self.px = {}
        self.plans = {}
        self.exch_pos = {}
        self.exch_orders = []
        self.holds = collections.OrderedDict()
        self.seen_exec = collections.OrderedDict()
        self.last_exec_ts_ms = 0
        self.ws = {"public": {"up": None, "since": None, "down_s": 0.0}, "private": {"up": None, "since": None, "down_s": 0.0}}
        self.rest = {"last_ok": clock(), "fails": 0, "errors": collections.deque(maxlen=200)}
        self.last_reconcile_ok = None
        self.last_reconcile_ts = None
        self.diffs = []
        self.need_reconcile = False
        self.bad_ticks = 0
        self.bad_streak = 0
        self.instruments = {}
        self.seeded = set()
        self.alerts = {}
        self._last_eval = {}
        self._t = {}
        self._kill_on = False
        self._flat_t = 0.0
        self._tr_cache = (None, [])
        self.started = clock()
        self.acct = None
        self._state_dirty = False
        self._load_state()
        self.stopped = False
        self.gate = G.Gate(self)
        self.act = A.Actions(self)
        self.profile_hash = self._profile_hash()
        self.last_event_seq = None

    # ------------------------------------------------------------------ служебное
    def _profile_hash(self):
        try:
            with open(paths.config("risk_profile.json", root=self.root), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            return None

    def decide(self, kind, what, plan=None, rev=None, args=None, why=None, basis=None, res=None, action_id=None, fsync=False, actor="exec"):
        rec = {"actor": actor, "kind": kind, "what": what, "t": self.clock()}
        for k, v in (("plan", plan), ("rev", rev), ("args", args), ("why", why), ("basis", basis), ("res", res), ("action_id", action_id)):
            if v is not None:
                rec[k] = v
        if self.profile_hash:
            rec["profile_hash"] = self.profile_hash
        return decisions.append(rec, root=self.root, fsync=fsync)

    def emit(self, type_, d=None, plan=None, sym=None, pri=None):
        try:
            seq = bus.emit(type_, d, plan=plan, sym=sym, pri=pri, root=self.root, now=self.clock())
            self.last_event_seq = seq
            return seq
        except Exception:        # шина вторична: сбой записи события не должен останавливать защиту
            return None

    def save(self, p, sync=False):
        pid = p["spec"]["id"]
        try:
            self.store.save_rt(pid, p["rt"], sync=sync)
        except (OSError, ValueError, KeyError) as e:
            self.error("save_rt", type(e).__name__)

    def error(self, where, code="ERR"):
        """Событие error (без текста исключения) + запись в журнал решений."""
        self.emit("error", {"where": where, "code": code})
        try:
            self.decide("event", "error", why="%s:%s" % (where, code))
        except Exception:
            pass

    def rest_event(self, exc):
        """Учёт успехов/сбоев REST для детектора слепоты (hold: blind)."""
        now = self.clock()
        if exc is None:
            self.rest["last_ok"] = now
            self.rest["fails"] = 0
        elif exc.kind in ("network", "unknown", "rate"):
            self.rest["fails"] += 1
            self.rest["errors"].append(now)

    def trades(self):
        """Список сделок журнала (кэш по mtime/размеру файла)."""
        try:
            st = os.stat(journal.PATH)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._tr_cache[0] and stamp is not None:
            return self._tr_cache[1]
        try:
            tr, order = journal._load()
            lst = [tr[i] for i in order]
        except Exception:
            lst = []
        self._tr_cache = (stamp, lst)
        return lst

    def instrument(self, sym):
        hit = self.instruments.get(sym)
        now = self.clock()
        if hit and now - hit[0] < 3600.0:
            return hit[1]
        try:
            ins = self.port.instrument(sym)
            self.rest_event(None)
        except PortError as e:
            self.rest_event(e)
            return hit[1] if hit else None
        except Exception:
            return hit[1] if hit else None
        self.instruments[sym] = (now, ins)
        return ins

    def active_plans(self, sym=None):
        out = [p for p in self.plans.values() if p["rt"].get("status") in ACTIVE]
        return [p for p in out if sym is None or p["spec"]["symbol"] == sym]

    def plan_by_n(self, plan_n):
        for p in self.plans.values():
            if p["rt"].get("plan_n") == plan_n:
                return p
        return None

    def view(self, sym):
        t = self.px.get(sym) or {}
        return MarketView(sym, t.get("last"), t.get("bid"), t.get("ask"), t.get("mark"), t.get("ts"), self.books.get(sym), self.clock())

    def limits_for(self, p):
        """Копия exec_limits с подложенными данными символа (tick, qty_step, min_qty, session_end_ts, kill) для logic.rules/pyramid."""
        lim = json.loads(json.dumps(self.cfg))
        ins = self.instrument(p["spec"]["symbol"]) or {}
        for k_src, k_dst in (("tick", "tick"), ("qty_step", "qty_step"), ("min_qty", "min_qty"), ("min_notional", "min_notional")):
            if ins.get(k_src) is not None:
                lim[k_dst] = float(ins[k_src])
        if exec_cfg.kill_state(self.root) is not None:
            lim["kill"] = True
        return lim

    def symbol_busy(self, sym, p):
        """Причина занятости монеты или None: позиция, чужой ордер, другой активный план (усреднения нет)."""
        if sym in self.exch_pos and (self.exch_pos[sym].get("size") or 0) > 0:
            return "по %s есть позиция на бирже" % sym
        mine = p["rt"]["entry"].get("link_id") if p else None
        for o in self.exch_orders:
            if o.get("sym") == sym and o.get("link_id") != mine and o.get("status", "New") in ("New", "PartiallyFilled") and not o.get("reduce_only"):
                return "по %s есть чужой ордер" % sym
        for q in self.active_plans(sym):
            if p is None or q["spec"]["id"] != p["spec"]["id"]:
                return "по %s уже есть активный план %s" % (sym, q["spec"]["id"])
        return None

    # ------------------------------------------------------------------ старт и завершение
    def start(self):
        """Последовательность старта (02 п. 2.2): KILL, планы, WAL, реконсиляция (блокирующая), inbox, событие exec_started.
        Подписку на WS и подкачку свечей делает daemon по desired() и seed_requests()."""
        now = self.clock()
        self.holds["reconciling"] = now
        k = exec_cfg.kill_state(self.root)
        self._kill_on = k is not None
        self._load_plans()
        self._recover_wal()
        reconcile.run(self, startup=True)
        for sym in self.desired():
            self.instrument(sym)
        self.poll_inbox()
        self.emit("exec_started", {"version": VERSION, "env": self.port.env if self.port is not None else "none"})
        self.decide("event", "exec_started", why="PROFILE:%s observe=%s" % (self.profile, self.observe), res={"ok": True},
                    args={"plans": len(self.plans), "positions": len(self.exch_pos)})
        mdsvc.write_all(self, force=True)

    def _load_plans(self):
        self.plans = {}
        for it in self.store.list():
            st = it["rt"].get("status")
            if (it["spec"].get("venue") or "bybit") != self.venue_id:       # план другой биржи ведёт её исполнитель
                continue
            if st in ACTIVE or st == "draft":
                pid = it["spec"]["id"]
                self.plans[pid] = {"spec": it["spec"], "rt": it["rt"]}
                if st == "draft":      # недооформленный (упали между create и arm): оформляем заново
                    self._arm(self.plans[pid], recovered=True)

    def stop(self):
        """Корректное завершение: снять ЖДУЩИЕ входные ордера (планы остаются armed), стопы/позиции/лесенку не трогать. Событие exec_stopped."""
        if self.stopped:
            return
        self.stopped = True
        if not self.observe and not self.keep_entries:
            for p in list(self.active_plans()):
                ent = p["rt"].get("entry") or {}
                if p["rt"]["status"] == "armed" and ent.get("state") in ("placing", "resting"):
                    r = self.act.cancel_entry_order(p, why="SHUTDOWN")
                    if r == "filled":
                        self._poll_entry(p)
                    elif r in ("cancelled", "gone"):
                        ent.update({"state": "waiting", "link_id": None, "order_id": None, "fills": [], "first_fill_ts": None})
                        self.save(p, sync=True)
        self.emit("exec_stopped", {"version": VERSION, "env": self.port.env if self.port is not None else "none"})
        try:
            self.decide("event", "exec_stopped", why="SHUTDOWN")
        except Exception:
            pass
        self.save_state(force=True)
        mdsvc.write_all(self, force=True, final=True)

    # ------------------------------------------------------------------ состояние между запусками (runtime/exec_state.json)
    def _state_path(self):
        return paths.exec_state_file(self.root, self.venue_id)

    def _load_state(self):
        """last_exec_ts_ms и окно виденных exec_id переживают рестарт (02 п. 2.4)."""
        try:
            d = _load_json(self._state_path())
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.last_exec_ts_ms = int(d.get("last_exec_ts_ms") or 0)
            for i in d.get("seen_exec") or []:
                self.seen_exec[i] = 1

    def save_state(self, force=False):
        if self.observe or not (self._state_dirty or force):
            return
        from tb.core.filelock import atomic_write_json
        try:
            os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
            atomic_write_json(self._state_path(), {"last_exec_ts_ms": self.last_exec_ts_ms, "seen_exec": list(self.seen_exec)[-SEEN_MAX:]})
            self._state_dirty = False
        except OSError:
            pass

    # ------------------------------------------------------------------ WAL: восстановление (02 п. 2.6)
    def _recover_wal(self):
        now = self.clock()
        for p in list(self.plans.values()):
            rt = p["rt"]
            act = rt.get("actions") or {}
            pend = list(act.get("pending") or [])
            if not pend:
                continue
            info = act.get("info") or {}
            sym = p["spec"]["symbol"]
            for aid in pend:
                inf = info.get(aid) or {}
                what, link = inf.get("what"), inf.get("link")
                res = {"recovered": True}
                try:
                    if what == "place_entry":
                        ent = rt["entry"]
                        if ent.get("state") == "waiting" or not ent.get("link_id"):
                            ent.update({"state": "placing", "link_id": link, "placed_ts": inf.get("t") or now, "fills": [], "unknown": True})
                        continue        # исход выяснит poll_entry (ордер найден / не отправлен по истечении 15 с)
                    if what == "place_target" and link:
                        o = self.port.order_by_link(sym, link) if self.port else None
                        t = (rt.get("targets") or {}).get(inf.get("tid"))
                        if o and o.get("status") in ("New", "PartiallyFilled") and t:
                            t.update({"state": "resting", "link": link})
                            res["ok"] = True
                        elif o and o.get("status") == "Filled" and t:
                            t["state"] = "filled"
                        elif now - float(inf.get("t") or 0) <= A.NOT_SENT_AFTER_S and not o:
                            continue
                        res["found"] = bool(o)
                    elif what == "set_stop":
                        ps = [x for x in (self.port.positions() if self.port else []) if x["sym"] == sym]
                        cur = float(ps[0]["sl"]) if ps else 0.0
                        if ps and cur > 0:
                            rt["pos"]["cur_stop"] = cur
                        res["ok"] = bool(ps) and G.tighter(p["spec"]["side"], cur, float(inf.get("prev") or 0)) is not False
                except PortError as e:
                    self.rest_event(e)
                    continue
                act["pending"].remove(aid)
                info.pop(aid, None)
                try:
                    self.decide("result", what or "unknown", plan=p["spec"]["id"], res=res, action_id=aid, why="RECOVERY")
                except Exception:
                    pass
            self.save(p, sync=True)

    # ------------------------------------------------------------------ подписки и свечи
    def tfs_for(self, sym):
        tfs = set([1, 5])
        for p in self.active_plans(sym):
            blob = json.dumps(p["spec"])
            for t in (15, 60):
                if ("%dm" % t if t < 60 else "1h") in blob.replace('"', ' ').replace(" ", "") or ('"tf": "%s"' % ("1h" if t == 60 else "%dm" % t)) in blob:
                    tfs.add(t)
        if self.exch_pos.get(sym) or self.active_plans(sym):
            tfs.update((15, 60))
        return tfs

    def desired(self):
        """-> {sym: set(tf)} для подписки: символы активных планов и позиций, затем watch_symbols; не больше connectivity.max_symbols."""
        out = collections.OrderedDict()
        for p in self.active_plans():
            out[p["spec"]["symbol"]] = self.tfs_for(p["spec"]["symbol"])
        for s in self.exch_pos:
            out.setdefault(s, self.tfs_for(s))
        for s in self.cfg.get("watch_symbols") or []:
            out.setdefault(s, set([5, 60]))
        cap = int(self.cfg["connectivity"]["max_symbols"])
        return collections.OrderedDict(list(out.items())[:cap])

    def seed_requests(self):
        """-> [(sym, tf)]: свечные книги, которые нужно подкачать REST (первый раз или после дыры). Вызывающий (daemon) тянет klines
        отдельным пулом и отдаёт on_seed(...)."""
        out = []
        for sym, tfs in self.desired().items():
            for tf in sorted(tfs):
                if (sym, tf) not in self.seeded:
                    out.append((sym, tf))
        return out

    def on_seed(self, sym, tf, rows):
        book = self.books.setdefault(sym, CandleBook())
        book.seed(tf, rows, now=self.clock())
        self.seeded.add((sym, tf))

    def seed_sync(self):
        """Подкачка свечей синхронно (для тестов и режима без пула)."""
        for sym, tf in self.seed_requests():
            try:
                rows = self.port.klines(sym, tf, 200)
                self.rest_event(None)
            except PortError as e:
                self.rest_event(e)
                continue
            self.on_seed(sym, tf, rows)

    def on_gap(self, sym, tf, from_ms, to_ms):
        """Пропуск подтверждённых свечей после обрыва WS: перезапросить историю."""
        self.seeded.discard((sym, tf))

    # ------------------------------------------------------------------ входящие данные
    def on_ticker(self, sym, last, bid, ask, mark, funding=0.0, ts=0.0):
        now = self.clock()
        try:
            last = float(last)
            bid = float(bid or 0)
            ask = float(ask or 0)
            mark = float(mark or 0)
        except (TypeError, ValueError):
            last = 0.0
        if not (last > 0) or last != last or last == float("inf"):
            self._bad_tick()
            return
        prev = self.px.get(sym)
        jump = float(self.cfg["connectivity"]["max_tick_jump_pct"])
        if prev and prev.get("last") and abs(last - prev["last"]) / prev["last"] * 100.0 > jump:
            ok = False
            try:        # второй источник: REST quote должен подтвердить скачок
                q = self.port.quote(sym)
                ok = q and q[2] and abs(float(q[2]) - last) / last * 100.0 < jump / 3.0
                self.rest_event(None)
            except PortError as e:
                self.rest_event(e)
            if not ok:
                self._bad_tick()
                return
        self.bad_streak = 0
        self.holds.pop("bad_data", None)
        if hasattr(self.port, "on_ticker"):      # бумажный порт кормится тиками фида (WP7)
            try:
                self.port.on_ticker(sym, last, bid, ask, mark, funding, ts if ts and ts > 0 else now)
            except Exception:
                self.error("paper_tick", "ERR")
        self.px[sym] = {"last": last, "bid": bid if bid > 0 else last, "ask": ask if ask > 0 else last, "mark": mark if mark > 0 else last,
                        "funding": funding, "ts": ts if ts and ts > 0 else now}
        self._eval_symbol(sym)

    def _bad_tick(self):
        self.bad_ticks += 1
        self.bad_streak += 1
        if self.bad_streak >= 5 and "bad_data" not in self.holds:
            self.holds["bad_data"] = self.clock()
            self.emit("error", {"where": "bad_data", "code": "BAD_TICKS"})

    def on_kline(self, sym, tf_min_, start_ms, o, h, l, c, vol=0.0, turn=0.0, confirm=False):
        if hasattr(self.port, "on_kline"):
            try:
                self.port.on_kline(sym, tf_min_, start_ms, o, h, l, c, vol, turn, confirm)
            except Exception:
                self.error("paper_kline", "ERR")
        book = self.books.setdefault(sym, CandleBook())
        closed = book.on_kline(tf_min_, (start_ms, o, h, l, c, vol, turn), confirm)
        if book.gaps.get(tf_min_):
            self.seeded.discard((sym, tf_min_))
        if closed is not None:
            self._eval_symbol(sym, force=True)

    def on_state(self, channel, up, down_s=0.0):
        """Состояние каналов WS (фид). Падение приватного канала > private_ws_down_hold_s даёт hold: private_ws; возврат = полная реконсиляция."""
        w = self.ws.setdefault(channel, {"up": None, "since": None, "down_s": 0.0})
        now = self.clock()
        was = w["up"]
        w["up"] = bool(up)
        w["down_s"] = float(down_s or 0.0)
        if not up:
            if w["since"] is None:
                w["since"] = now
            if down_s >= 20.0:
                self.emit("connectivity", {"channel": channel, "state": "down", "down_s": round(down_s, 1)})
        else:
            if was is False:
                self.emit("connectivity", {"channel": channel, "state": "up", "down_s": round(w["down_s"] or (now - (w["since"] or now)), 1)})
                self.need_reconcile = True
            w["since"] = None
            w["down_s"] = 0.0

    def on_private(self, ev):
        k = ev.get("kind")
        if k == "execution":
            p = self.apply_execution(ev)
            if p is not None and p["rt"]["status"] == "armed" and (p["rt"]["entry"].get("state") in ("placing", "resting")):
                self._poll_entry(p)
        elif k == "position":
            sym = ev.get("sym")
            if (ev.get("size") or 0) > 0:
                self.exch_pos[sym] = dict(ev)
                for p in self.active_plans(sym):
                    if p["rt"]["status"] in IN_POS and ev.get("sl"):
                        p["rt"]["pos"]["cur_stop"] = float(ev["sl"]) if G.tighter(p["spec"]["side"], ev["sl"], p["rt"]["pos"].get("cur_stop")) else p["rt"]["pos"].get("cur_stop")
            else:
                self.exch_pos.pop(sym, None)
                for p in self.active_plans(sym):
                    if p["rt"]["status"] in IN_POS:
                        self.sync_position(p)
        elif k == "wallet":
            self.acct = {"equity": ev.get("equity"), "free": ev.get("free"), "ts": self.clock()}

    # ------------------------------------------------------------------ исполнения
    def _owner_plan(self, ev):
        """Чьё это исполнение. Своя ссылка tbx-N-M -> план N (если он активен); чужая непустая ссылка (старые команды, ручные ордера с ID) -> ничьё;
        пустая ссылка (стоп/ликвидация/ручное закрытие в приложении) -> план с позицией по символу, но только исполнения после входа плана
        (старая история биржи не должна попадать в сегодняшний план)."""
        sym = ev.get("sym")
        link = ev.get("link_id") or ""
        if link:
            pn = linkid.parse(link)
            return self.plan_by_n(pn[0]) if pn else None
        ts_ms = int(ev.get("ts_ms") or 0)
        for p in self.active_plans(sym):
            if p["rt"]["status"] in IN_POS:
                ent = float(p["rt"]["ts"].get("entered") or 0)
                if ts_ms and ts_ms < (ent - 5.0) * 1000.0:
                    continue
                return p
        return None

    def apply_execution(self, ev):
        """Одно исполнение (WS или REST): дедупликация по exec_id, учёт в плане. -> план или None. Идемпотентно."""
        eid = ev.get("exec_id")
        if eid:
            if eid in self.seen_exec:
                self.last_exec_ts_ms = max(self.last_exec_ts_ms, int(ev.get("ts_ms") or 0))
                self._state_dirty = True
                return None
            self.seen_exec[eid] = 1
            while len(self.seen_exec) > SEEN_MAX:
                self.seen_exec.popitem(last=False)
        self.last_exec_ts_ms = max(self.last_exec_ts_ms, int(ev.get("ts_ms") or 0))
        self._state_dirty = True
        p = self._owner_plan(ev)
        if p is None:
            return None
        rt, spec = p["rt"], p["spec"]
        link = ev.get("link_id") or ""
        if rt["status"] == "armed":
            ent = rt["entry"]
            if link and link == ent.get("link_id") and not float(ev.get("closed_size") or 0) > 0:
                if not any(f["exec_id"] == eid for f in ent.setdefault("fills", [])):
                    ent["fills"].append({"exec_id": eid, "px": float(ev["price"]), "qty": float(ev["qty"]), "fee": float(ev.get("fee") or 0),
                                         "ts": float(ev.get("ts_ms") or self.clock() * 1000) / 1000.0})
            return p
        if rt["status"] not in IN_POS:
            return p
        want_close = "sell" if spec["side"] == "long" else "buy"
        closing = float(ev.get("closed_size") or 0) > 0 or str(ev.get("side", "")).lower() == want_close
        if closing:
            self._record_close(p, ev)
        elif link.startswith("tbx-") and link != rt["entry"].get("link_id"):
            self._record_add(p, ev)
        return p

    def _record_close(self, p, ev):
        rt, spec = p["rt"], p["spec"]
        pos, real = rt["pos"], rt["realized"]
        sign = 1.0 if spec["side"] == "long" else -1.0
        q = min(float(ev["qty"]), float(pos.get("qty") or 0))
        if q <= 0:
            return
        px, fee = float(ev["price"]), float(ev.get("fee") or 0)
        gross = sign * (px - float(pos["avg"])) * q
        real["pnl"] = round(real["pnl"] + gross, 8)
        real["fees"] = round(real["fees"] + fee, 8)
        part = {"ts": round(float(ev.get("ts_ms") or self.clock() * 1000) / 1000.0, 3), "px": px, "qty": q, "fee": fee, "pnl": round(gross, 8),
                "exec_id": ev.get("exec_id"), "stop_type": ev.get("stop_type") or "", "link": ev.get("link_id") or "", "exec_type": ev.get("exec_type") or ""}
        real["parts"].append(part)
        pos["qty"] = round(float(pos["qty"]) - q, 12)
        link = ev.get("link_id") or ""
        for tid, t in (rt.get("targets") or {}).items():
            if link and t.get("link") == link:
                t["filled_qty"] = round(float(t.get("filled_qty") or 0) + q, 12)
                if t["filled_qty"] >= float(t["qty"]) - 1e-9:
                    t["state"] = "filled"
                    self.emit("milestone", {"name": "target_filled", "target": tid, "px": px}, plan=spec["id"], sym=spec["symbol"])
        if pos["qty"] > 1e-9:
            self._journal_part(p, part)
        self.save(p)
        if pos["qty"] <= 1e-9:
            self._finalize_close(p)

    def _record_add(self, p, ev):
        rt = p["rt"]
        pos = rt["pos"]
        q, px, fee = float(ev["qty"]), float(ev["price"]), float(ev.get("fee") or 0)
        if any(e.get("exec_id") == ev.get("exec_id") for e in pos["entries"]):
            return
        tot = float(pos["qty"]) + q
        pos["avg"] = (float(pos["avg"]) * float(pos["qty"]) + px * q) / tot
        pos["qty"] = round(tot, 12)
        pos["entries"].append({"ts": round(self.clock(), 3), "px": px, "qty": q, "fee": fee, "link": ev.get("link_id"), "exec_id": ev.get("exec_id"), "kind": "add"})
        rt["realized"]["fees"] = round(rt["realized"]["fees"] + fee, 8)
        rt["adds"]["n"] = int(rt["adds"].get("n", 0)) + 1
        rt["adds"]["last_ts"] = round(self.clock(), 3)
        self._journal_call("cmd_add", id=pos.get("journal_id"), px=px, qty=q, fee=fee)
        self.save(p)

    # ------------------------------------------------------------------ журнал сделок (с идемпотентностью)
    def _journal_call(self, fn, **kw):
        if self.observe or kw.get("id") is None and fn != "cmd_open":
            return False
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                getattr(journal, fn)(argparse.Namespace(**kw))
            return True
        except SystemExit as e:
            self.error("journal_" + fn, "REFUSED")
            return False
        except Exception as e:
            self.error("journal_" + fn, type(e).__name__)
            return False

    def _journal_part(self, p, part):
        self._journal_call("cmd_part", id=p["rt"]["pos"].get("journal_id"), px=part["px"], qty=part["qty"], fee=part["fee"], pnl=part["pnl"])

    def _journal_open_exists(self, plan_id):
        try:
            tr, order = journal._load()
        except Exception:
            return None
        for i in order:
            if tr[i].get("plan_id") == plan_id:
                return i
        return None

    def _journal_id(self, sym):
        env = (self.port.env or "x")[:1].upper()
        base = "BB-%s-%s-%s" % (env, sym, time.strftime("%m%d-%H%M%S", time.gmtime(self.clock())))
        try:
            ids = journal._load()[0]
        except Exception:
            ids = {}
        jid, n = base, 1
        while jid in ids:
            n += 1
            jid = "%s-%d" % (base, n)
        return jid

    # ------------------------------------------------------------------ оценка планов
    def _eval_symbol(self, sym, force=False):
        for p in self.active_plans(sym):
            self.eval_plan(p, force=force)

    def eval_plan(self, p, force=False):
        now = self.clock()
        pid = p["spec"]["id"]
        if not force and now - self._last_eval.get(pid, 0.0) < EVAL_MIN_GAP_S:
            return
        self._last_eval[pid] = now
        st = p["rt"].get("status")
        if st == "armed":
            self._eval_armed(p, now)
        elif st in IN_POS:
            self._eval_position(p, now)

    def _lifecycle(self, p, now):
        """Жизненный цикл (ttl, инвалидация, cancel_above/below, max_hold, конец сессии). -> True, если план завершился/закрывается."""
        view = self.view(p["spec"]["symbol"])
        acts = rules.evaluate_lifecycle(p["spec"], p["rt"], view, now, self.limits_for(p))
        for a in acts:
            if self._apply(p, a, view):
                if p["rt"]["status"] not in ACTIVE:
                    return True
        return p["rt"]["status"] not in ACTIVE

    def _eval_armed(self, p, now):
        rt, spec = p["rt"], p["spec"]
        ent = rt["entry"]
        if self._lifecycle(p, now):
            return
        if rt["status"] != "armed":
            return
        if ent.get("state") in ("placing", "resting"):
            return        # входной ордер в работе: его ведёт _poll_entry
        retry = float(self.cfg["entry"]["retry_after_s"])
        if ent.get("state") == "blocked" and ent.get("last_attempt") is not None and now - float(ent["last_attempt"]) < retry:
            return
        view = self.view(spec["symbol"])
        res = rules.evaluate_entry(spec, rt, view, now, self.limits_for(p))
        if res.get("rt_patch"):
            dmerge(rt, res["rt_patch"])
        if res["fire"]:
            self._attempt_entry(p, view, res)
        elif res.get("blocked") and res["blocked"] not in ("STALE", "HOLD"):
            self._entry_blocked(p, res["blocked"], "")
        elif res.get("blocked") == "HOLD":
            pass

    def _attempt_entry(self, p, view, res):
        spec, rt = p["spec"], p["rt"]
        ent = rt["entry"]
        now = self.clock()
        cond = (spec["entry"].get("when") or {}).get("kind") or "when"
        self.emit("trigger_fired", {"cond": cond if isinstance(cond, str) else "when", "px": view.last}, plan=spec["id"], sym=spec["symbol"])
        vd = self.gate.entry(p, view)
        if not vd:
            self._gate_denied(p, "place_entry", vd, entry=True)
            return
        ent["risk_usdt"] = (vd.params["guard"] or {}).get("risk_usdt")
        ent["blocked_why"] = None
        bo = vd.params.get("blackout")
        if bo:       # paper + paper.blackout_mode=warn: вход в макро-окне пропущен с предупреждением, тегом и урезанным размером (tb.core.blackout)
            ent["blackout"] = {"name": bo["name"], "in_min": bo["in_min"], "size_mult": bo["size_mult"]}
            self.decide("event", "blackout_warn", plan=spec["id"], args={"event": bo["name"], "in_min": bo["in_min"], "size_mult_cap": bo["size_mult"]},
                        why="%s: вход в макро-окне, тег blackout, size_mult<=%s" % (bo["code"], bo["size_mult"]))
        why = "COND"
        kind, info = self.act.place_entry(p, vd, why=why)
        if kind == "placed":
            self.decide("rule", "fire_entry", plan=spec["id"], why="ENTRY:COND", args={"px": vd.params["px"], "qty": vd.params["qty"]})
            self._poll_entry(p)
        elif kind == "denied":
            self._gate_denied(p, "place_entry", G.deny(info.get("code", "HOLD"), info.get("detail", "")), entry=True)
        elif kind == "rejected":
            self._entry_blocked(p, "GUARD", info.get("code", ""))
        elif kind == "unknown":
            self.error("place_entry", "UNKNOWN")
        else:
            ent["last_attempt"] = now

    def _gate_denied(self, p, what, vd, entry=False):
        """Отказ Gate: запись в журнал решений (с троттлингом), событие action_rejected для будящих кодов; для входа блокировка/ожидание."""
        spec, rt = p["spec"], p["rt"]
        key = (spec["id"], what, vd.code)
        now = self.clock()
        if now - self.alerts.get(key, 0.0) >= 30.0 or vd.code in ("INVARIANT_VIOLATION",):
            self.alerts[key] = now
            self.decide("skip", what, plan=spec["id"], res={"ok": False, "code": vd.code}, why=("DENY:%s %s" % (vd.code, vd.detail))[:200])
            if (vd.code in G.WAKE_CODES and not entry) or vd.code == "E_BLACKOUT":      # E_BLACKOUT будит мозг и на входе (раз в 30 с на план)
                self.emit("action_rejected", {"plan": spec["id"], "what": what, "code": vd.code}, plan=spec["id"], sym=spec["symbol"])
        if vd.code == "INVARIANT_VIOLATION":
            self.error("stop_monotonic", "INVARIANT_VIOLATION")
        if entry and vd.code not in G.TRANSIENT:
            self._entry_blocked(p, vd.code, vd.detail)

    def _note_min_size(self, sym, now):
        """Запоминает монету, на которой размер по риску не дотянул до минимального лота (runtime/min_size.json: {монета: ts}); досье мозга по ней не предлагает входы
        MIN_SIZE_TTL_S. Сбой записи не мешает торговле."""
        try:
            from tb.core.filelock import atomic_write_json
            path = paths.runtime("min_size.json", root=self.root)
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            d = {k: v for k, v in (d if isinstance(d, dict) else {}).items() if isinstance(v, (int, float)) and now - v < MIN_SIZE_TTL_S}
            d[sym] = round(now, 1)
            atomic_write_json(path, d)
        except Exception:
            pass

    def _entry_blocked(self, p, why, detail):
        """Вход не состоялся (попытка): счётчик, событие entry_blocked, исчерпание попыток -> invalidated ENTRY_BLOCKED."""
        spec, rt = p["spec"], p["rt"]
        ent = rt["entry"]
        now = self.clock()
        if why == "CHASE":
            # цена ушла от уровня дальше допуска: это не провал входа. План остаётся armed, вход повторяется каждые retry_after_s и пройдёт при откате к уровню;
            # от убегания цены защищают cancel_above и TTL. В попытки (blocked_attempts_max) не считается, событие и запись решения не чаще раза в 5 минут.
            ent["chase_n"] = int(ent.get("chase_n") or 0) + 1
            ent["last_attempt"] = round(now, 3)
            ent["blocked_why"] = why
            ent["state"] = "blocked"
            self.save(p)
            key = (spec["id"], "chase_log")
            if now - self.alerts.get(key, 0.0) >= 300.0:
                self.alerts[key] = now
                self.emit("entry_blocked", {"why": "CHASE", "attempt": ent["chase_n"]}, plan=spec["id"], sym=spec["symbol"])
                self.decide("skip", "place_entry", plan=spec["id"], res={"ok": False, "code": why}, why="BLOCKED:CHASE цена дальше допуска от уровня, ждём отката")
            return
        if why == "MIN_SIZE":
            self._note_min_size(spec["symbol"], now)
        ent["attempts"] = int(ent.get("attempts") or 0) + 1
        rt.setdefault("counters", {})["blocked_attempts"] = ent["attempts"]
        ent["last_attempt"] = round(now, 3)
        ent["blocked_why"] = why
        ent["state"] = "blocked"
        self.save(p)
        bw = WHY_BUS.get(why) or ("MIN_SIZE" if why == "MIN_SIZE" else ("COOLDOWN" if why in ("COOLDOWN", "LIMIT_HOUR", "LIMIT_DAY", "ANOMALY", "NO_CONFIRM") else "GUARD"))
        self.emit("entry_blocked", {"why": bw, "attempt": ent["attempts"]}, plan=spec["id"], sym=spec["symbol"])
        self.decide("skip", "place_entry", plan=spec["id"], res={"ok": False, "code": why}, why=("BLOCKED:%s %s" % (why, detail))[:200])
        if ent["attempts"] >= int(self.cfg["entry"]["blocked_attempts_max"]):
            self.end_plan(p, "invalidated", "ENTRY_BLOCKED")

    # ------------------------------------------------------------------ вход: опрос и завершение
    def _poll_entry(self, p):
        kind, info = self.act.poll_entry(p)
        if kind == "filled":
            self._finalize_entry(p, info)
        elif kind == "failed":
            code = info.get("code", "")
            self._entry_blocked(p, "CHASE" if code in ("EC_PostOnlyWillTakeLiquidity", "CancelByPostOnly", "CancelByUser") or "PostOnly" in str(code) else "GUARD", code)

    def _finalize_entry(self, p, info):
        """Вход заполнен: защита -> журнал -> идея -> лесенка -> managing -> entry_filled (02 п. 2.12)."""
        spec, rt = p["spec"], p["rt"]
        sym, side = spec["symbol"], spec["side"]
        fills = info["fills"]
        qty_f = sum(f["qty"] for f in fills)
        avg_f = sum(f["px"] * f["qty"] for f in fills) / qty_f
        fees = sum(f["fee"] for f in fills)
        try:
            ps = [x for x in self.port.positions() if x["sym"] == sym and x["size"] > 0]
            self.rest_event(None)
        except PortError as e:
            self.rest_event(e)
            return            # повторим на следующем опросе (состояние placing/pending сохранено)
        ent, pos = rt["entry"], rt["pos"]
        stop0 = float(spec["stop"]["price"])
        now = self.clock()
        ent["partial"] = bool(info.get("partial"))
        pos.update({"qty": round(qty_f, 12), "avg": avg_f, "initial_stop": stop0, "cur_stop": stop0, "r_unit": abs(avg_f - stop0), "best_px": avg_f,
                    "worst_px": avg_f, "t_best": round(now, 3), "t_worst": round(now, 3), "lev": ent.get("lev"), "path_n": 0, "path_cov": 0.0,
                    "entries": [{"ts": round(now, 3), "px": avg_f, "qty": qty_f, "fee": fees, "link": ent.get("link_id"), "exec_id": fills[0]["exec_id"], "kind": "entry"}],
                    "risk0": float(ent.get("risk_usdt") or 0.0) * (qty_f / float(ent.get("qty") or qty_f))})
        rt["realized"]["fees"] = round(rt["realized"]["fees"] + fees, 8)
        for f in fills:
            self.seen_exec.setdefault(f["exec_id"], 1)
            self.last_exec_ts_ms = max(self.last_exec_ts_ms, int(f["ts"] * 1000))
        self._state_dirty = True
        if not ps:
            rt["ts"]["entered"] = round(now, 3)
            self._to_status(p, "entered", "FILLED_THEN_GONE")
            ent["state"] = "filled"
            self._journal_open(p)
            self.save(p, sync=True)
            self.sync_position(p)
            return
        ex_pos = ps[0]
        self.exch_pos[sym] = dict(ex_pos)
        pos["qty"] = float(ex_pos["size"])
        if ex_pos.get("sl") and float(ex_pos["sl"]) > 0:
            pos["cur_stop"] = float(ex_pos["sl"])
        else:
            if not self._ensure_stop(p, stop0):
                return
        rt["ts"]["entered"] = round(now, 3)
        ent["state"] = "filled"
        self._to_status(p, "entered", "ENTRY_FILLED")
        self._journal_open(p)
        self._idea_mark(p, "taken")
        self.save(p, sync=True)
        self.act.place_ladder(p)
        self._to_status(p, "managing", "PROTECTED")
        self.emit("entry_filled", {"avg": avg_f, "qty": pos["qty"], "partial": bool(info.get("partial")), "sl": pos["cur_stop"],
                                   "risk_usdt": rt["pos"].get("risk0"), "journal_id": pos.get("journal_id")}, plan=spec["id"], sym=sym)
        mdsvc.write_all(self, force=True)

    def _ensure_stop(self, p, stop0):
        """На бирже нет стопа после входа: выставить аварийно; не вышло -> закрыть по рынку. -> True, если защита есть."""
        spec = p["spec"]
        self.decide("protect", "entry_without_stop", plan=spec["id"], why="PROTECT:NO_STOP")
        kind, info = self.act.move_stop(p, stop0, by="protect", why="PROTECT:NO_STOP", vd=G.allow(cur=0.0, to=stop0))
        if kind in ("ok", "unchanged"):
            return True
        self.act.close_by_protect(p, "PROTECT:NO_STOP")
        self.sync_position(p)
        return False

    def _journal_open(self, p):
        spec, rt = p["spec"], p["rt"]
        pos, ent = rt["pos"], rt["entry"]
        pid = spec["id"]
        jid = self._journal_open_exists(pid)
        if jid:
            pos["journal_id"] = jid
            return
        if self.observe:
            return
        tags = spec.get("tags") or {}
        sess = session.active() if spec.get("mode", "live") == "live" else None
        mode = {"mainnet": "live", "testnet": "testnet", "paper": "paper"}.get(self.port.env, "testnet")
        jid = self._journal_id(spec["symbol"])
        lev = float(ent.get("lev") or 1)
        qty0 = float(pos["entries"][0]["qty"])
        tag_list = [self.venue_id, self.port.env or "", "plan"]
        if not plan_schema.has_confirm((spec.get("entry") or {}).get("when")):
            tag_list.append("no-confirm")
        tag_list += [t for t in ((spec.get("tags") or {}).get("extra") or []) if isinstance(t, str) and t not in tag_list]      # agent./model. (шлюз), атрибуция в журнале
        if spec.get("mode") == "paper" or self.port.env == "paper":
            tag_list.append("paper")
        if (ent.get("blackout") or {}).get("name") and self.port.env == "paper":
            tag_list.append("blackout")          # вход в макро-окне (paper, warn): отдельный срез статистики `exp stats --by blackout`
        if (spec.get("source") or {}).get("kind") == "shadow":
            tag_list.append("shadow")
        view = self.view(spec["symbol"])
        c5 = []
        if view.book is not None:
            c5 = [[c.o, c.h, c.l, c.c, c.vol] for c in view.book.last_closed(5, 3)]
        plan_ctx = {"c5": c5, "atr_5m": view.book.atr(5) if view.book is not None else None,
                    "cond": plan_schema.describe_cond((spec["entry"].get("when") or {})) if spec["entry"].get("when") else None}
        kw = dict(id=jid, sym=spec["symbol"], side=spec["side"], entry=pos["avg"], lev=lev, margin=round(qty0 * pos["avg"] / lev, 6), sl=pos["initial_stop"],
                  tp=G.weighted_tp(spec), thesis=spec.get("thesis", ""), news="", regime="unknown", tags=",".join(t for t in tag_list if t), mode=mode,
                  fee_open=sum(e.get("fee", 0.0) for e in pos["entries"]), session=sess["id"] if sess else None, stype=spec.get("session_type"), venue=self.venue_id,
                  idea=(spec.get("source") or {}).get("idea_id"), plan_id=pid, plan_rev=spec.get("rev"), exp=tags.get("experiment"), var=tags.get("variant"),
                  setup=tags.get("setup"), hyp=tags.get("hypothesis"), plan_ctx=plan_ctx,
                  parent_plan=(spec.get("source") or {}).get("parent_plan"))
        if self._journal_call("cmd_open", **kw):
            pos["journal_id"] = jid
            self.decide("event", "journal_open", plan=pid, res={"ok": True}, why="JOURNAL:%s" % jid)

    def _idea_mark(self, p, status):
        iid = (p["spec"].get("source") or {}).get("idea_id")
        if not iid or self.observe:
            return
        try:
            if status == "taken":
                ideas.mark_idea(iid, "taken", note="plan %s" % p["spec"]["id"], trade=p["rt"]["pos"].get("journal_id"), root=self.root, now=self.clock())
            else:
                ideas.mark_if_active(iid, status, note="plan %s" % p["spec"]["id"], root=self.root, now=self.clock())
        except Exception:
            pass

    def _to_status(self, p, new, why):
        rt = p["rt"]
        old = rt.get("status")
        if old == new:
            return True
        if not plan_schema.can_transition(old, new):
            self.error("transition", "E_TRANSITION")
            return False
        rt["status"] = new
        if new in plan_schema.TERMINAL:
            rt["ts"]["closed"] = round(self.clock(), 3)
        try:
            self.store.save_rt(p["spec"]["id"], rt, sync=True)
        except (OSError, ValueError, KeyError):
            self.error("save_rt", "TRANSITION")
        self.decide("transition", new, plan=p["spec"]["id"], why="%s %s->%s" % (why, old, new))
        if new in plan_schema.TERMINAL:
            self.plans.pop(p["spec"]["id"], None)
        return True

    def _arm(self, p, recovered=False):
        rt = p["rt"]
        now = self.clock()
        rt["ts"]["armed"] = round(now, 3)
        px = (self.px.get(p["spec"]["symbol"]) or {}).get("last")
        rt["entry"]["ref_px"] = px
        self._to_status(p, "armed", "ACCEPT")
        self._eval_armed(p, now)

    def end_plan(self, p, to, reason, cmd=False):
        """Завершение плана без позиции (armed -> cancelled|expired|invalidated). Гонка с заполнением: ордер снимается, перечитывается;
        если успело заполниться, план идёт в entered и функция возвращает False."""
        spec, rt = p["spec"], p["rt"]
        ent = rt["entry"]
        if ent.get("state") in ("placing", "resting"):
            r = self.act.cancel_entry_order(p, why="END:%s" % reason)
            if r == "filled":
                self._poll_entry(p)
                return False
            if r == "error":
                return False
        ent["state"] = "waiting"
        if not self._to_status(p, to, "END:%s" % reason):
            return False
        self.emit("plan_ended", {"status": to, "why": reason.split(":")[0].replace(" ", "_")[:30]}, plan=spec["id"], sym=spec["symbol"])
        self._idea_mark(p, "expired" if to in ("expired", "cancelled") else "invalidated")
        return True

    # ------------------------------------------------------------------ позиция: оценка
    def _track_path(self, p, view):
        pos = p["rt"]["pos"]
        last = view.last
        if not last or pos.get("avg") is None or view.price_age() > 5.0:
            return
        s = 1.0 if p["spec"]["side"] == "long" else -1.0
        now = view.now
        if pos.get("best_px") is None or s * (last - pos["best_px"]) > 0:
            pos["best_px"], pos["t_best"] = last, round(now, 3)
        if pos.get("worst_px") is None or s * (last - pos["worst_px"]) < 0:
            pos["worst_px"], pos["t_worst"] = last, round(now, 3)
        prev = pos.get("path_last")
        pos["path_n"] = int(pos.get("path_n", 0)) + 1
        if prev is not None and 0 < now - prev <= 30.0:
            pos["path_cov"] = round(float(pos.get("path_cov", 0.0)) + (now - prev), 3)
        pos["path_last"] = round(now, 3)

    def _eval_position(self, p, now):
        spec, rt = p["spec"], p["rt"]
        sym = spec["symbol"]
        view = self.view(sym)
        self._track_path(p, view)
        if self._lifecycle(p, now):
            return
        if rt["status"] == "entered":
            return        # лесенка ещё ставится (секунды)
        self._eval_touch(p, view)
        lim = self.limits_for(p)
        res = rules.evaluate_manage(spec, rt, view, now, lim)
        patch = res.get("rt_patch") or {}
        before = json.loads(json.dumps(rt.get("rules") or {}))
        if patch:
            dmerge(rt, patch)
        for ev in res.get("events") or []:
            self._milestone(p, ev)
        for a in res.get("actions") or []:
            ok = self._apply(p, a, view)
            if ok is False and a.get("rule") and a["type"] in ("move_stop", "partial_close", "close", "add", "move_target"):
                if a["rule"] in before:
                    rt["rules"][a["rule"]] = before[a["rule"]]
                else:
                    rt["rules"].pop(a["rule"], None)
            if rt["status"] not in ACTIVE:
                return
        for sk in res.get("skipped") or []:
            key = (spec["id"], sk["rule"], sk["code"])
            if now - self.alerts.get(key, 0.0) >= 60.0:
                self.alerts[key] = now
                self.decide("skip", "rule", plan=spec["id"], res={"ok": False, "code": sk["code"]}, why="RULE:%s" % sk["rule"])
        if patch or res.get("actions"):
            self.save(p)

    def _milestone(self, p, ev):
        name = ev.get("name")
        name = {"r_minus_0_5": "r_minus_05"}.get(name, name)
        d = {"name": name, "r": ev.get("r"), "px": ev.get("px")}
        if ev.get("target"):
            d["target"] = ev["target"]
        self.emit("milestone", {k: v for k, v in d.items() if v is not None}, plan=p["spec"]["id"], sym=p["spec"]["symbol"])

    def _eval_touch(self, p, view):
        """Цели exec=touch: цена коснулась уровня -> reduce-only market на долю."""
        rt, spec = p["rt"], p["spec"]
        last = view.last
        if not last or view.price_age() > float(self.cfg["connectivity"]["public_price_stale_s"]):
            return
        for tid, t in sorted((rt.get("targets") or {}).items()):
            if t.get("state") != "touch":
                continue
            hit = last >= t["price"] if spec["side"] == "long" else last <= t["price"]
            if hit:
                q = min(float(t["qty"]), float(rt["pos"]["qty"]))
                t["state"] = "pending"
                if q >= float(rt["pos"]["qty"]) - 1e-9:
                    kind, _ = self.act.close(p, None, "rule", why="TOUCH:%s" % tid, full=True)
                else:
                    kind, _ = self.act.close(p, q, "rule", why="TOUCH:%s" % tid)
                if kind == "ok":
                    t["state"] = "filled"
                    t["filled_qty"] = q
                    self.emit("milestone", {"name": "target_filled", "target": tid, "px": last}, plan=spec["id"], sym=spec["symbol"])
                    self.sync_position(p)
                else:
                    t["state"] = "touch"

    def _apply(self, p, a, view):
        """Одно действие движка правил -> исполнение через Gate/Actions. -> True выполнено / False отказ / None нейтрально."""
        typ = a.get("type")
        spec, rt = p["spec"], p["rt"]
        why = a.get("why") or ""
        pid = spec["id"]
        if typ == "cancel_entry":
            return self.act.cancel_entry_order(p, why=why) in ("cancelled", "gone")
        if typ == "transition":
            if self.end_plan(p, a["to"], a.get("reason") or why):
                return True
            return False
        if typ == "move_stop":
            kind, info = self.act.move_stop(p, float(a["to"]), by="rule", why=why, price=view.last)
            self.decide("rule", "move_stop", plan=pid, args={"to": a["to"]}, why=why, res={"ok": kind in ("ok", "unchanged"), "code": info.get("code", "OK")})
            if kind == "denied":
                self._gate_denied(p, "set_stop", G.deny(info.get("code", "STOP_NOT_TIGHTER"), info.get("detail", "")))
            elif kind == "gone":
                self.sync_position(p)
            if kind in ("ok", "unchanged"):
                self.emit("rule_fired", {"rule": a.get("rule") or "x", "action": "move_stop", "ok": True, "code": "OK"}, plan=pid, sym=spec["symbol"])
            return kind in ("ok", "unchanged")
        if typ in ("partial_close", "close"):
            return self._do_close(p, a, view)
        if typ == "move_target":
            ok = self.act.move_target(p, a["target"], float(a["price"]), why=why)
            self.decide("rule", "move_target", plan=pid, args={"px": a["price"]}, why=why, res={"ok": ok})
            return ok
        if typ == "add":
            return self._do_add(p, a, view)
        if typ == "notify":
            self.emit("brain_wake_request", {"code": a.get("code"), "plan": pid}, plan=pid, sym=spec["symbol"])
            self.emit("rule_fired", {"rule": a.get("rule") or "x", "action": "notify", "ok": True, "code": "OK"}, plan=pid, sym=spec["symbol"])
            return True
        return None

    def _do_close(self, p, a, view):
        rt, spec = p["rt"], p["spec"]
        pos = rt["pos"]
        remaining = float(pos["qty"])
        ins = self.instrument(spec["symbol"]) or {"qty_step": "0.001", "min_qty": "0"}
        step = float(ins["qty_step"])
        if a["type"] == "close":
            frac = float(a.get("frac", 1.0))
            reason = a.get("reason") or "rule"
            qty = remaining * frac
        else:
            reason = "rule"
            if a.get("frac_remaining") is not None:
                qty = remaining * float(a["frac_remaining"])
            else:
                qty = float(pos["entries"][0]["qty"]) * float(a.get("frac_initial", 0))
        qty = min(remaining, G.floor_step(qty, step))
        full = qty >= remaining - step / 2.0 or (a["type"] == "close" and float(a.get("frac", 1.0)) >= 0.999)
        if qty <= 0 and not full:
            return False
        kind, info = self.act.close(p, None if full else qty, reason, why=a.get("why", ""), full=full)
        self.decide("rule", a["type"], plan=p["spec"]["id"], args={"qty": remaining if full else qty}, why=a.get("why", ""),
                    res={"ok": kind == "ok", "code": info.get("code", "OK")})
        if kind == "ok":
            if full:
                rt["close"]["reason"] = reason
            self.sync_position(p)
            self.emit("rule_fired", {"rule": a.get("rule") or "x", "action": a["type"], "ok": True, "code": "OK"}, plan=p["spec"]["id"], sym=p["spec"]["symbol"])
            if not full and rt["status"] in IN_POS:
                self.act.resync_ladder(p)
            return True
        if kind == "gone":
            self.sync_position(p)
            return True
        if kind == "denied":
            self._gate_denied(p, "close", G.deny(info.get("code", "HOLD")))
        return False

    def _do_add(self, p, a, view):
        spec = p["spec"]
        rule = next((r for r in spec.get("manage") or [] if r.get("id") == a.get("rule")), {"do": {"risk_frac": a.get("risk_frac")}, "when": None})
        vd = self.gate.add(p, view, rule)
        if not vd:
            self.decide("skip", "add", plan=spec["id"], res={"ok": False, "code": vd.code}, why=("DENY:%s %s" % (vd.code, vd.detail))[:200])
            self.emit("action_rejected", {"plan": spec["id"], "what": "add", "code": vd.code}, plan=spec["id"], sym=spec["symbol"])
            return False
        kind, info = self.act.add(p, vd, why=a.get("why", ""))
        if kind == "ok":
            self.sync_position(p)
            return True
        return False

    def on_stop_moved(self, p, prev, new, by):
        spec = p["spec"]
        self.emit("stop_moved", {"from": prev if prev else new, "to": new, "by": by if by in ("rule", "cmd", "protect", "owner") else "rule"},
                  plan=spec["id"], sym=spec["symbol"])
        jid = p["rt"]["pos"].get("journal_id")
        if jid:
            self._journal_call("cmd_adjust", id=jid, sl=new, tp=None, by=by)

    # ------------------------------------------------------------------ синхронизация позиции и финализация
    def sync_position(self, p):
        """Свести план с биржей: применить неучтённые исполнения (REST), обновить размер и стоп по позиции; нет позиции -> финализация."""
        spec, rt = p["spec"], p["rt"]
        sym = spec["symbol"]
        if rt.get("status") not in IN_POS:
            return
        since = int(max(0.0, float(rt["ts"].get("entered") or self.clock()) - 10.0) * 1000)
        try:
            for e in self.port.executions(since, sym):
                self.apply_execution(e)
            ps = [x for x in self.port.positions() if x["sym"] == sym and x["size"] > 0]
            self.rest_event(None)
        except PortError as e:
            self.rest_event(e)
            return
        if rt.get("status") not in IN_POS:
            return
        if not ps:
            # Пустой ответ позиций без объясняющих исполнений может быть сбоем биржи (на testnet встречается): сразу не финализируем.
            # Закрытие признаётся, если исполнения покрывают вход, либо пустой ответ повторился 3 раза подряд за 5 с и дольше.
            real = rt["realized"]
            qty0 = sum(float(e["qty"]) for e in rt["pos"].get("entries") or [])
            explained = sum(float(x["qty"]) for x in real.get("parts") or []) >= qty0 - 1e-9
            c = rt.setdefault("counters", {})
            now = self.clock()
            if not explained:
                c["gone_reads"] = int(c.get("gone_reads", 0)) + 1
                c.setdefault("gone_since", round(now, 3))
                self.save(p)
                if c["gone_reads"] < 3 or now - float(c["gone_since"]) < 5.0:
                    return
            self.exch_pos.pop(sym, None)
            self._finalize_close(p)
            return
        (rt.get("counters") or {}).pop("gone_reads", None)
        (rt.get("counters") or {}).pop("gone_since", None)
        self.exch_pos[sym] = dict(ps[0])
        if ps[0].get("sl") and G.tighter(spec["side"], ps[0]["sl"], rt["pos"].get("cur_stop")):
            rt["pos"]["cur_stop"] = float(ps[0]["sl"])      # шире ожидаемого НЕ принимаем: это восстановит реконсиляция
        self.save(p)

    def _finalize_close(self, p):
        """Позиция закрыта (стоп/тейк/ликвидация/вручную/по нашему действию): итог, журнал, события, статус closed. Идемпотентно."""
        spec, rt = p["spec"], p["rt"]
        if rt.get("status") not in IN_POS:
            return
        sym = spec["symbol"]
        real, pos = rt["realized"], rt["pos"]
        parts = real.get("parts") or []
        qty0 = sum(float(e["qty"]) for e in pos.get("entries") or [])
        done_q = sum(float(x["qty"]) for x in parts)
        if done_q < qty0 - 1e-9 and pos.get("avg"):     # исполнения не дошли (потеря WS): добираем из closed-pnl
            self._fallback_from_closed_pnl(p)
            parts = real.get("parts") or []
            done_q = sum(float(x["qty"]) for x in parts)
        if parts:
            exit_px = sum(x["px"] * x["qty"] for x in parts) / max(done_q, 1e-12)
        else:
            exit_px = (self.px.get(sym) or {}).get("last") or pos.get("avg") or 0.0
        pnl = round(real["pnl"] - real["fees"], 6)
        reason = self._close_reason(p, parts, exit_px)
        rt["close"] = {"reason": reason, "px": exit_px, "pnl": pnl}
        now = self.clock()
        if not self.observe:
            try:
                self.port.cancel_all(sym, reduce_only_only=True)
            except Exception:
                pass
        jid = pos.get("journal_id")
        mae = mfe = None
        if jid:
            path = self._build_path(p, exit_px, now)
            if path:
                mae, mfe = path.get("mae_r"), path.get("mfe_r")
            tr = None
            try:
                tr = journal._load()[0].get(jid)
            except Exception:
                tr = None
            if tr is not None and not tr.get("ts_close"):
                self._journal_call("cmd_close", id=jid, exit=exit_px, reason=reason, funding=0.0, note="exec plan %s" % spec["id"], path=path,
                                   pnl_override=pnl, fees_usdt=round(real["fees"], 6), rule_followed=None, err=None, lesson=None)
        for t in (rt.get("targets") or {}).values():
            if t.get("state") in ("resting", "pending", "touch"):
                t["state"] = "cancelled"
        pos["qty"] = 0.0
        self._to_status(p, "closed", "POSITION_CLOSED:%s" % reason)
        r_unit = float(pos.get("r_unit") or 0)
        risk0 = float(pos.get("risk0") or 0)
        r = round(pnl / risk0, 3) if risk0 > 0 else None
        d = {"reason": reason, "pnl_usdt": pnl, "fees": round(real["fees"], 6)}
        if r is not None:
            d["r"] = r
        if jid:
            d["journal_id"] = jid
        if mae is not None:
            d["mae_r"] = mae
        if mfe is not None:
            d["mfe_r"] = mfe
        self.emit("position_closed", d, plan=spec["id"], sym=sym)
        self._idea_mark(p, "expired") if False else None
        self.exch_pos.pop(sym, None)
        mdsvc.write_all(self, force=True)

    def _fallback_from_closed_pnl(self, p):
        spec, rt = p["spec"], p["rt"]
        real = rt["realized"]
        since = int(max(0.0, float(rt["ts"].get("entered") or 0) - 10.0) * 1000)
        try:
            rows = self.port.closed_pnl(spec["symbol"], since)
        except PortError:
            return
        have = sum(float(x["qty"]) for x in real["parts"])
        total = sum(float(e["qty"]) for e in rt["pos"].get("entries") or [])
        for r in rows:
            if have >= total - 1e-9:
                break
            q = min(float(r["qty"]), total - have)
            fee = float(r.get("close_fee") or 0)
            real["parts"].append({"ts": round(float(r["ts_ms"]) / 1000.0, 3), "px": float(r["exit"]), "qty": q, "fee": fee, "pnl": float(r["pnl"]) + fee,
                                  "exec_id": "cpnl-%s" % r["ts_ms"], "stop_type": "", "link": "", "exec_type": "closed_pnl"})
            real["pnl"] = round(real["pnl"] + float(r["pnl"]) + fee, 8)
            real["fees"] = round(real["fees"] + fee, 8)
            have += q

    def _close_reason(self, p, parts, exit_px):
        rt, spec = p["rt"], p["spec"]
        pre = (rt.get("close") or {}).get("reason")
        if pre:
            return pre
        # последний по ВРЕМЕНИ фрагмент закрытия, а не по порядку записи: сверка дописывает ранние фрагменты (например сработавший тейк) уже после стопа,
        # и стоп оставался «не последним» (закрытие помечалось manual). Цена проверки близости к стопу: цена этого фрагмента, не средняя по всем
        last = max(parts, key=lambda x: float(x.get("ts") or 0.0)) if parts else {}
        ref_px = float(last.get("px") or exit_px or 0.0)
        st = last.get("stop_type") or ""
        if st in ("StopLoss", "PartialStopLoss", "TrailingStop"):
            return "sl"
        if st in ("TakeProfit", "PartialTakeProfit"):
            return "tp"
        if last.get("exec_type") in ("BustTrade", "AdlTrade"):
            return "liquidation"
        link = last.get("link") or ""
        if link and any(t.get("link") == link for t in (rt.get("targets") or {}).values()):
            return "tp"
        stop = float(rt["pos"].get("cur_stop") or rt["pos"].get("initial_stop") or 0)
        s = 1.0 if spec["side"] == "long" else -1.0
        if stop and s * (ref_px - stop) <= 0.002 * stop and not link:
            return "sl"
        return "manual"

    def _build_path(self, p, exit_px, now):
        pos = p["rt"]["pos"]
        if pos.get("best_px") is None:
            return None
        s = 1.0 if p["spec"]["side"] == "long" else -1.0
        hi, lo = (pos["best_px"], pos["worst_px"]) if s > 0 else (pos["worst_px"], pos["best_px"])
        t_hi, t_lo = (pos.get("t_best"), pos.get("t_worst")) if s > 0 else (pos.get("t_worst"), pos.get("t_best"))
        t0 = float(p["rt"]["ts"].get("entered") or now)
        st = {"entry": float(pos["entries"][0]["px"]), "min": lo, "max": hi, "t_min": t_lo, "t_max": t_hi, "ts_open": t0, "n": pos.get("path_n", 0),
              "covered_s": pos.get("path_cov", 0.0), "prev_ts": pos.get("path_last") or t0, "last_ts": pos.get("path_last") or t0}
        try:
            out = tradepath.compute(st, p["spec"]["side"], exit_px, now, pos.get("initial_stop"), G.weighted_tp(p["spec"]))
        except Exception:
            return None
        if out:
            out["src"] = "exec"
        return out

    def adopt_position(self, p, pos):
        """plan adopt: взять под управление существующую позицию без изменения размера. Журнал: открытая запись без plan_id получает связь,
        иначе создаётся запись open (adopt). Лесенка не ставится (цель touch), стоп уже на бирже."""
        spec, rt = p["spec"], p["rt"]
        now = self.clock()
        sym = spec["symbol"]
        avg, qty = float(pos["avg"]), float(pos["size"])
        stop0 = float(pos["sl"])
        rt["pos"].update({"qty": qty, "avg": avg, "initial_stop": stop0, "cur_stop": stop0, "r_unit": abs(avg - stop0), "best_px": avg, "worst_px": avg,
                          "t_best": round(now, 3), "t_worst": round(now, 3), "lev": pos.get("lev"), "path_n": 0, "path_cov": 0.0,
                          "entries": [{"ts": round(now, 3), "px": avg, "qty": qty, "fee": 0.0, "link": None, "exec_id": "adopt", "kind": "entry"}],
                          "risk0": qty * abs(avg - stop0)})
        rt["ts"]["armed"] = rt["ts"]["entered"] = round(now, 3)
        rt["entry"]["state"] = "filled"
        self.exch_pos[sym] = dict(pos)
        self._to_status(p, "armed", "ADOPT")
        self._to_status(p, "entered", "ADOPT")
        legacy = None
        try:
            tr, order = journal._load()
            for i in order:
                t = tr[i]
                if t.get("sym") == sym and not t.get("ts_close") and not t.get("plan_id") and t.get("venue") == self.venue_id:
                    legacy = i
        except Exception:
            pass
        if legacy:
            rt["pos"]["journal_id"] = legacy
        else:
            rt["entry"]["lev"] = pos.get("lev") or 1
            self._journal_open(p)
        self.save(p, sync=True)
        for tid, t in [("t1", spec["targets"][0])]:
            rt["targets"][tid] = {"state": "touch", "qty": qty, "price": t["price"], "link": None, "filled_qty": 0.0}
        self._to_status(p, "managing", "ADOPT")
        self.emit("entry_filled", {"avg": avg, "qty": qty, "partial": False, "sl": stop0, "journal_id": rt["pos"].get("journal_id")}, plan=spec["id"], sym=sym)

    # ------------------------------------------------------------------ таймер и команды
    def on_timer(self, now=None):
        """Раз в ~1 с: KILL, связь/hold, опрос входов, жизненный цикл, закрытия, реконсиляция, equity_sync, heartbeat/снимок."""
        now = self.clock() if now is None else now
        self._check_kill(now)
        self._update_holds(now)
        for sym in self.desired():
            self.instrument(sym)
        for p in list(self.active_plans()):
            st = p["rt"]["status"]
            ent = p["rt"].get("entry") or {}
            if st == "armed" and ent.get("state") in ("placing", "resting"):
                gap = ENTRY_POLL_S if ent.get("state") == "placing" else LIMIT_POLL_S
                if now - self._t.get(("poll", p["spec"]["id"]), 0.0) >= gap:
                    self._t[("poll", p["spec"]["id"])] = now
                    self._poll_entry(p)
            if p["rt"]["status"] in ACTIVE and p["rt"]["status"] != "armed" or (st == "armed" and ent.get("state") not in ("placing", "resting")):
                self.eval_plan(p, force=True)
            elif p["rt"]["status"] == "armed":
                self._lifecycle(p, now)
        for p in list(self.active_plans()):
            if (p["rt"].get("counters") or {}).get("gone_reads") and p["rt"]["status"] in IN_POS:
                self.sync_position(p)       # подтверждение пустого ответа позиций (3 чтения за 5 с)
        self._rest_quotes(now)
        if not (self.ws["private"].get("up")) and now - self._t.get("pos_poll", 0.0) >= 3.0:
            self._t["pos_poll"] = now
            for p in list(self.active_plans()):
                if p["rt"]["status"] in IN_POS:
                    self.sync_position(p)
        every = float(self.cfg["connectivity"]["reconcile_every_s"])
        if self.need_reconcile or now - self._t.get("reconcile", -1e9) >= every:
            self._t["reconcile"] = now
            self.need_reconcile = False
            reconcile.run(self)
        if now - self._t.get("equity", -1e9) >= 60.0:
            self._t["equity"] = now
            self._equity_sync(now)
        if now - self._t.get("state", 0.0) >= 5.0:
            self._t["state"] = now
            self.save_state()
        mdsvc.write_all(self)

    def _rest_quotes(self, now):
        """Публичный WS молчит: REST-подстраховка цены (quote раз в 2 с) по символам с позициями."""
        stale = float(self.cfg["connectivity"]["public_price_stale_s"])
        if now - self._t.get("quote", 0.0) < 2.0:
            return
        for sym in list(self.exch_pos):
            t = self.px.get(sym)
            if t is None or now - t["ts"] > stale:
                self._t["quote"] = now
                try:
                    b, a, l = self.port.quote(sym)
                    self.rest_event(None)
                    if l and l > 0:
                        self.px[sym] = {"last": l, "bid": b or l, "ask": a or l, "mark": l, "funding": 0.0, "ts": now}
                        self._eval_symbol(sym, force=True)
                except PortError as e:
                    self.rest_event(e)

    def _check_kill(self, now):
        k = exec_cfg.kill_state(self.root)
        if k is not None and not self._kill_on:
            self._kill_on = True
            self.emit("kill", {"state": "on", "flatten": bool(k.get("flatten"))})
            self.decide("protect", "kill", why="PROTECT:KILL %s" % (k.get("reason") or "")[:120], res={"ok": True})
            for p in list(self.active_plans()):
                if p["rt"]["status"] == "armed":
                    self.end_plan(p, "cancelled", "KILL")
        elif k is None and self._kill_on:
            self._kill_on = False
            self.emit("kill", {"state": "off", "flatten": False})
        if k is not None and k.get("flatten") and not self.observe and now - self._flat_t >= 2.0:
            self._flat_t = now
            self._flatten_all()

    def _flatten_all(self):
        """KILL с flatten: закрыть все позиции (reduce-only рынок), сначала позиции планов, затем чужие."""
        for p in list(self.active_plans()):
            if p["rt"]["status"] in IN_POS:
                kind, _ = self.act.close(p, None, "kill", why="PROTECT:KILL", full=True)
                self.sync_position(p)
        plan_syms = set(p["spec"]["symbol"] for p in self.active_plans())
        try:
            ps = self.port.positions()
        except PortError as e:
            self.rest_event(e)
            return
        for x in ps:
            if x["sym"] in plan_syms or x["size"] <= 0:
                continue
            try:
                self.decide("protect", "flatten", why="PROTECT:KILL", args={"qty": x["size"]})
                self.act.call("close_market", G.RISK_DOWN, x["sym"], A.close_side(x["side"]), None, "tbx-0-%d" % int(self.clock() % 100000))
            except (PortError, A.Denied):
                pass

    def emergency_flatten(self):
        """Вызывает страж демона (другой поток), когда рабочий поток завис в REST, а владелец требует KILL flatten (WP1B).
        Только reduce-only закрытия рынком по данным биржи через порт (свои соединения транспорта, полоса protect); состояние планов и WAL не трогает:
        его сведёт рабочий поток после возвращения (реконсиляция, финализация по исполнениям). Идемпотентно: reduce-only сверх остатка биржа обрезает.
        Ссылка принадлежит плану монеты (если он есть), чтобы исполнение учлось ему. -> число отправленных закрытий."""
        if self.observe or self.port is None:
            return 0
        k = exec_cfg.kill_state(self.root)
        if k is None or not k.get("flatten"):
            return 0
        try:
            ps = self.port.positions()
        except PortError:
            return 0
        n = 0
        for x in ps:
            if x["size"] <= 0:
                continue
            plan_n = 0
            try:
                for p in list(self.plans.values()):
                    if p["spec"]["symbol"] == x["sym"] and p["rt"].get("status") in IN_POS:
                        plan_n = int(p["rt"].get("plan_n") or 0)
            except (RuntimeError, KeyError, TypeError):
                pass
            link = linkid.make(plan_n, 900000 + int(self.clock()) % 99999, self.venue_id)
            try:
                self.port.cancel_all(x["sym"], reduce_only_only=True)    # лесенка держит остаток позиции: reduce-only сверх свободного биржа обрезает
            except PortError:
                pass
            try:
                self.port.close_market(x["sym"], A.close_side(x["side"]), x["size"], link)
                n += 1
            except PortError:
                pass
        try:
            self.decide("protect", "emergency_flatten", why="PROTECT:KILL рабочий поток завис в REST", args={"closed": n})
        except Exception:
            pass
        return n

    def _update_holds(self, now):
        cfg = self.cfg["connectivity"]
        pw = self.ws["private"]
        if pw["up"] is False and pw.get("since") is not None and now - pw["since"] >= float(cfg["private_ws_down_hold_s"]):
            self.holds.setdefault("private_ws", now)
        elif pw["up"]:
            if "private_ws" in self.holds and self.last_reconcile_ok is not None and self.last_reconcile_ok >= (pw.get("since") or 0):
                self.holds.pop("private_ws", None)
        if now - self.rest["last_ok"] >= float(cfg["rest_blind_s"]):
            if "blind" not in self.holds:
                self.holds["blind"] = now
                self.emit("connectivity", {"channel": "rest", "state": "down", "down_s": round(now - self.rest["last_ok"], 1)})
        elif "blind" in self.holds:
            self.holds.pop("blind", None)
            self.emit("connectivity", {"channel": "rest", "state": "up", "down_s": 0})
            self.need_reconcile = True
        if self.diffs:
            self.holds.setdefault("reconcile_diffs", now)
        else:
            self.holds.pop("reconcile_diffs", None)
        pub = self.ws["public"]
        if pub["up"] is False and pub.get("since") is not None and now - pub["since"] >= 20.0 and not self.alerts.get(("pubdown",)):
            self.alerts[("pubdown",)] = now

    def _equity_sync(self, now):
        """Баланс биржи -> portfolio.json (02 п. 2.9). Малое расхождение или устаревший файл: set_venue; большое: hold equity_mismatch."""
        if self.observe or self.port.env == "paper":
            return
        try:
            acc = self.port.account()
            self.rest_event(None)
        except PortError as e:
            self.rest_event(e)
            return
        eq, free = float(acc["equity"]), float(acc["free"])
        self.acct = {"equity": eq, "free": free, "ts": now}
        eqc = self.cfg["equity_sync"]
        try:
            pf = portfolio.load()
            v = (pf.get("venues") or {}).get(self.venue_id)
            age = None
            if v and v.get("ts"):
                t0 = protect.parse_ts(v["ts"])
                age = None if t0 is None else now - t0
            dev = abs(eq - float(v["equity"])) / max(float(v["equity"]), 1e-9) if v and float(v.get("equity") or 0) > 0 else 0.0
            if not v or float(v.get("equity") or 0) <= 0 or (age is not None and age > float(eqc["stale_after_s"])) or dev <= float(eqc["max_dev_frac"]):
                if eqc.get("auto_set_portfolio", True):
                    portfolio.set_venue(self.venue_id, eq, free, pf)
                self.holds.pop("equity_mismatch", None)
            else:
                if "equity_mismatch" not in self.holds:
                    self.holds["equity_mismatch"] = now
                    self.emit("reconcile_alert", {"kind": "equity_mismatch", "sym": "BTC"})
                    self.decide("reconcile", "equity_mismatch", why="RECON:equity_mismatch", res={"ok": False}, args={"exch": eq, "file": float(v["equity"])})
            prof = _load_json(paths.config("risk_profile.json", root=self.root))
            dm = portfolio.dd_mult(portfolio.load(), prof)
            if dm <= 0:
                if "reduce_only" not in self.holds:
                    self.holds["reduce_only"] = now
                    self.emit("limit_hit", {"which": "drawdown_halt"})
            else:
                self.holds.pop("reduce_only", None)
        except Exception as e:
            self.error("equity_sync", type(e).__name__)

    def poll_inbox(self):
        """Команды из state/plans/_inbox по порядку имён; результат в _outbox (идемпотентно по cmd_id)."""
        n = 0
        for cmd in self.store.inbox_list():
            cid = cmd.get("cmd_id")
            if not cmd.get("_broken") and self._cmd_venue(cmd) != self.venue_id:       # команда плана другой биржи: её заберёт исполнитель той биржи
                continue
            try:
                if cmd.get("_broken"):
                    res = {"ok": False, "status": "rejected", "errors": [{"code": "E_SCHEMA", "path": "", "msg": "команда нечитаема"}]}
                elif self.store.seen_cmd(cid):
                    res = self.store.outbox_get(cid) or {"ok": True, "status": "duplicate"}
                else:
                    res = self.on_command(cmd)
            except Exception as e:      # команда не должна ронять исполнитель
                self.error("command", type(e).__name__)
                res = {"ok": False, "status": "error", "errors": [{"code": "E_INTERNAL", "path": "", "msg": type(e).__name__}]}
            try:
                self.store.inbox_done(cid, res)
            except (OSError, ValueError):
                self.error("inbox_done", "IO")
            n += 1
        return n

    def _cmd_venue(self, cmd):
        """Биржа команды: payload.venue (создание плана) или биржа существующего плана (plan_id); иначе основная (bybit)."""
        pl = cmd.get("payload")
        if isinstance(pl, dict) and isinstance(pl.get("venue"), str) and pl["venue"]:
            return pl["venue"].strip().lower()
        pid = cmd.get("plan_id")
        if isinstance(pid, str) and pid.startswith("PL-"):
            p = self.plans.get(pid)
            if p is None:
                try:
                    spec, _rt = self.store.load(pid)
                except Exception:
                    spec = None
                return ((spec or {}).get("venue") or "bybit")
            return p["spec"].get("venue") or "bybit"
        return "bybit"

    def on_command(self, cmd):
        return commands.handle(self, cmd)
