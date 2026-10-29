"""Фейки порта мозга и порта исполнителя для тестов жизненного цикла (WP14). Без сети, без файлов исполнителя: только память."""
import copy

from tb.lifecycle.ports import ExecPort, LifecycleBrain


class FakeBrain(LifecycleBrain):
    """responses: роль -> ответ (dict output | callable(payload) -> output | "hang" (молчит) | ("err", код) | список таких, берутся по очереди)."""

    def __init__(self, avail=True, responses=None):
        self.avail = avail
        self.responses = dict(responses or {})
        self.calls = []
        self.jobs = {}
        self.cancelled = []

    def set(self, role, resp):
        self.responses[role] = resp

    def available(self, root, now):
        return self.avail, "fake"

    def submit(self, role, payload, timeout_s, now, meta=None):
        jid = "J-fake-%d" % (len(self.calls) + 1)
        self.calls.append({"role": role, "payload": copy.deepcopy(payload), "meta": meta, "timeout_s": timeout_s})
        r = self.responses.get(role)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else (r[0] if r else None)
        if callable(r):
            r = r(payload)
        if r == "hang" or r is None:
            self.jobs[jid] = None
        elif isinstance(r, tuple) and r[0] == "err":
            self.jobs[jid] = {"ok": False, "output": None, "err": r[1]}
        else:
            self.jobs[jid] = {"ok": True, "output": r, "err": None}
        return jid

    def poll(self, job_id, now):
        return self.jobs.get(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def roles(self):
        return [c["role"] for c in self.calls]


class FakeExec(ExecPort):
    """Исполнитель в памяти. Команды plan close/cancel/adopt применяются при pump() (или сразу, auto=True). health/snapshot настраиваются полями."""

    def __init__(self, now=1793106000.0, auto=True):
        self.now = now
        self.auto = auto
        self.pos = {}              # plan_id -> позиция снапшота
        self.plan_map = {}         # plan_id -> план
        self.orphans = []          # позиции без владельца
        self.sent = []             # [{"cmd","op","plan","payload","reason"}]
        self.results = {}
        self.fail_ops = set()      # операции, на которые исполнитель отвечает отказом
        self.silent_ops = set()    # операции, которые исполнитель молча не выполняет (но принимает)
        self.alive = True
        self.diffs = 0
        self.reconcile_age_s = 5.0
        self.hold_reasons = []
        self.private_up = True
        self.equity = 30.0
        self.free = 30.0
        self.profile = "paper"
        self.observe = False
        self.snap_present = True

    # ------------------------------------------------------------ наполнение
    def add_position(self, plan_id, sym="SOL", side="long", size=0.5, avg=100.0, mark=102.0, sl=100.5, liq=80.0, stype="intraday", init_stop=99.0, manage=None, lifecycle=None,
                     status="managing", hold=None, risk0=None, entry_ts=None):
        long_ = side == "long"
        spec = {"id": plan_id, "symbol": sym, "side": side, "mode": "live", "session_type": stype, "stop": {"price": init_stop}, "manage": manage if manage is not None else [],
                "lifecycle": lifecycle if lifecycle is not None else {}}
        rt = {"plan_id": plan_id, "status": status, "hold": hold, "ts": self.now - 7200, "pos": {"qty": size, "avg": avg, "initial_stop": init_stop, "cur_stop": sl,
                                                                                                "risk0": risk0 if risk0 is not None else size * abs(avg - init_stop),
                                                                                                "entries": [{"ts": entry_ts if entry_ts is not None else self.now - 3600, "px": avg, "qty": size}]}}
        self.plan_map[plan_id] = {"id": plan_id, "sym": sym, "side": side, "mode": "live", "stype": stype, "status": status, "hold": hold, "spec": spec, "rt": rt}
        self.pos[plan_id] = {"sym": sym, "side": side, "size": size, "avg": avg, "mark": mark, "sl": sl, "tp": None, "liq": liq, "upnl": (mark - avg) * size * (1 if long_ else -1),
                             "lev": 5, "margin": 10.0, "owner": plan_id}
        return plan_id

    def add_waiting(self, plan_id, sym="ETH", side="long", stype="intraday", ttl_min=60, status="armed"):
        spec = {"id": plan_id, "symbol": sym, "side": side, "mode": "live", "session_type": stype, "lifecycle": {"ttl_min": ttl_min}}
        self.plan_map[plan_id] = {"id": plan_id, "sym": sym, "side": side, "mode": "live", "stype": stype, "status": status, "hold": None, "spec": spec,
                                  "rt": {"plan_id": plan_id, "status": status, "ts": self.now}}

    def add_orphan(self, sym="BTC", side="long", size=0.01, avg=60000.0, sl=59000.0):
        self.orphans.append({"sym": sym, "side": side, "size": size, "avg": avg, "mark": avg, "sl": sl, "tp": None, "liq": avg * 0.5, "upnl": 0.0, "lev": 3, "margin": 5.0, "owner": None})

    # ------------------------------------------------------------ порт
    def snapshot(self, now):
        if not self.snap_present:
            return {"present": False, "age_s": None, "ts": None, "equity": None, "free": None, "positions": [], "orders": []}
        return {"present": True, "ts": now, "age_s": 0.0, "equity": self.equity, "free": self.free, "positions": copy.deepcopy(list(self.pos.values()) + self.orphans), "orders": []}

    def health(self, now):
        d = {"profile": self.profile, "observe": self.observe, "mode": "observe" if self.observe else "paper", "alive": self.alive, "hold_reasons": list(self.hold_reasons),
             "reconcile_age_s": self.reconcile_age_s, "diffs": self.diffs, "ws": {"public": {"up": True, "age_s": 0}, "private": {"up": self.private_up, "age_s": 0}}}
        return {"present": True, "alive": self.alive, "age_s": 1.0, "data": d}

    def plans(self):
        return copy.deepcopy([p for p in self.plan_map.values() if p["status"] in ("draft", "armed", "entered", "managing")])

    def send(self, op, plan_id, reason, payload=None, now=None, tag=""):
        cid = "C-fake-%d" % (len(self.sent) + 1)
        self.sent.append({"cmd": cid, "op": op, "plan": plan_id, "payload": payload, "reason": reason, "tag": tag})
        if self.auto:
            self._apply(self.sent[-1])
        return cid

    def result(self, cmd_id):
        return self.results.get(cmd_id)

    # ------------------------------------------------------------ применение
    def pump(self):
        for c in self.sent:
            if c["cmd"] not in self.results:
                self._apply(c)

    def _apply(self, c):
        op, pid = c["op"], c["plan"]
        if op in self.fail_ops:
            self.results[c["cmd"]] = {"ok": False, "status": "rejected", "errors": [{"code": "E_STATUS_FORBIDS"}]}
            return
        if op in self.silent_ops:
            return
        self.results[c["cmd"]] = {"ok": True, "status": "ok"}
        if op == "close" and pid in self.pos:
            del self.pos[pid]
            self.plan_map[pid]["status"] = "closed"
        elif op == "cancel" and pid in self.plan_map:
            self.plan_map[pid]["status"] = "cancelled"
        elif op == "adopt":
            sym = (c["payload"] or {}).get("symbol")
            for o in list(self.orphans):
                if o["sym"] == sym:
                    self.orphans.remove(o)
                    new = "PL-9999-%s-01" % sym
                    self.add_position(new, sym=sym, side=o["side"], size=o["size"], avg=o["avg"], mark=o["mark"], sl=o["sl"], liq=o["liq"], stype="intraday", init_stop=o["sl"])

    def ops(self, op=None):
        return [(c["op"], c["plan"]) for c in self.sent if op is None or c["op"] == op]
