"""Стенд для тестов исполнителя: временный корень (config/state/runtime), подмена путей журнала/портфеля/сессий, FakeClock/FakePort,
готовый Executor. Реальный state/ проекта не используется (TB_ROOT указывает на временную папку)."""
import json
import os
import shutil
import tempfile
import time

from fakes.clock import FakeClock
from fakes.fake_port import FakePort

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def plan_long(**over):
    """SOL long от 100: подтверждение закрытием 1м выше 100.2; стоп 99; цели 102/103 (50/50); BE на +1R."""
    p = {"schema": "tb.plan/1", "symbol": "SOL", "side": "long", "mode": "live", "session_type": "intraday", "source": {"kind": "brain"},
         "thesis": "Тестовый план: возврат выше 100.2 после прокола", "tags": {"experiment": "t-exp", "variant": "A", "setup": "reclaim"},
         "entry": {"when": {"kind": "candle_close", "tf": "1m", "dir": "above", "level": 100.2, "n": 1}, "how": {"type": "market"}, "ref_price": 100.2,
                   "valid": {"ttl_min": 60, "cancel_below": 95.0, "max_chase_bps": 20}},
         "stop": {"price": 99.0, "trigger_by": "last"},
         "targets": [{"id": "t1", "price": 102.5, "frac": 0.5, "exec": "resting"}, {"id": "t2", "price": 104.0, "frac": 0.5, "exec": "resting"}],
         "risk": {"score": 0.6, "size_mult": 1.0},
         "manage": [{"id": "be_1r", "when": {"kind": "pnl_r", "gte": 1.0}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True}],
         "invalidation": [{"kind": "candle_close", "tf": "5m", "dir": "below", "level": 98.5, "n": 1}],
         "lifecycle": {"ttl_min": 60, "max_hold_min": 600}}
    p.update(over)
    return p


class Env(object):
    def __init__(self, observe=False, env="testnet", cfg_over=None, with_session=True, port=None, start=True):
        from tb import paths
        from tb.core import journal, learning, pending, portfolio, session
        from tb.executor.core import Executor
        self.root = tempfile.mkdtemp(prefix="tbexec_")
        os.makedirs(os.path.join(self.root, "config"))
        os.makedirs(os.path.join(self.root, "state"))
        os.makedirs(os.path.join(self.root, "runtime"))
        for n in ("risk_profile.json", "exec_limits.json"):
            shutil.copy(os.path.join(ROOT, "config", n), os.path.join(self.root, "config", n))
        self._old_env = os.environ.get("TB_ROOT")
        os.environ["TB_ROOT"] = self.root
        self._old = (journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH)
        journal.PATH = os.path.join(self.root, "state", "trades.jsonl")
        portfolio.PATH = os.path.join(self.root, "state", "portfolio.json")
        session.PATH = os.path.join(self.root, "state", "sessions.jsonl")
        learning.PATH = os.path.join(self.root, "state", "learning.json")
        pending.PATH = os.path.join(self.root, "state", "pending_orders.json")
        self.clock = FakeClock(time.time())
        t = self.clock.time()
        with open(portfolio.PATH, "w", encoding="utf-8") as f:
            json.dump({"venues": {"bybit": {"equity": 40.0, "free": 38.0, "ts": iso(t)}}, "hwm": 40.0}, f)
        os.makedirs(os.path.join(self.root, "runtime", "intel"))        # окон блэкаута нет, но данные есть (иначе live/testnet Gate отказывает E_BLACKOUT)
        with open(os.path.join(self.root, "runtime", "intel", "blackouts.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": "tb.blackouts/1", "ts": int(t), "windows": []}, f)
        if with_session:
            with open(session.PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ev": "start", "id": "intraday-t", "type": "intraday", "venue": "bybit", "thesis": "t", "ts": iso(t), "start": t,
                                    "expires": t + 36000, "budget_usdt": 1.2, "equity_at_start": 40.0}) + "\n")
        if cfg_over:
            from tb.core import exec_cfg
            cfg = exec_cfg.load(self.root)
            for k, v in cfg_over.items():
                cfg[k].update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
            self.cfg = cfg
        else:
            self.cfg = None
        if port is not None and not hasattr(port, "positions"):
            port = port(self.clock.time, self.root)
        self.port = port or FakePort(env=env, clock=self.clock.time)
        self.port.set_price("SOL", 100.0, spread=0.04)
        self.ex = Executor(self.port, root=self.root, clock=self.clock.time, sleep=self.clock.advance, profile="testnet", observe=observe, cfg=self.cfg)
        self.q = []
        self.port.sink = self.q.append
        if start:
            self.ex.start()

    def close(self):
        from tb.core import journal, learning, pending, portfolio, session
        journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH = self._old
        if self._old_env is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = self._old_env
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- помощники сценариев
    def pump(self):
        """Доставка накопленных приватных событий (как очередь рабочего потока: не внутри вызова порта)."""
        while self.q:
            self.ex.on_private(self.q.pop(0))

    def tick(self, price, sym="SOL", spread=0.04, dt=0.2):
        """Тик цены: порт-фейк, затем Executor.on_ticker; срабатывание лимитов/стопа на бирже."""
        self.clock.advance(dt)
        self.port.set_price(sym, price, spread=spread)
        for p in getattr(self.port, "pos", {}).values():
            if p["sym"] == sym:
                p["mark"] = price
        self.ex.on_ticker(sym, price, price - spread / 2, price + spread / 2, price, 0.0, self.clock.time())
        if hasattr(self.port, "match"):
            self.port.match(sym)
        self.pump()

    def candle(self, close, tf=1, sym="SOL", confirm=True):
        ms = int(self.clock.time() * 1000)
        self.ex.on_kline(sym, tf, ms, close, close + 0.1, close - 0.1, close, 100.0, 10000.0, confirm)
        self.pump()

    def timer(self, n=1, dt=1.0):
        for _ in range(n):
            self.clock.advance(dt)
            self.ex.on_timer(self.clock.time())
            self.pump()

    def submit(self, plan=None, actor="brain:chat", op="submit", **kw):
        cmd = {"actor": actor, "op": op, "payload": plan if plan is not None else plan_long(), "reason": "test"}
        cmd.update(kw)
        cid = self.ex.store.inbox_put(cmd)
        self.ex.poll_inbox()
        self.pump()
        return self.ex.store.outbox_get(cid)

    def command(self, op, plan_id, payload=None, **kw):
        cmd = {"actor": "brain:chat", "op": op, "plan_id": plan_id, "reason": "test"}
        if payload is not None:
            cmd["payload"] = payload
        cmd.update(kw)
        cid = self.ex.store.inbox_put(cmd)
        self.ex.poll_inbox()
        self.pump()
        return self.ex.store.outbox_get(cid)

    def rt(self, pid):
        return self.ex.store.load(pid)[1]

    def spec(self, pid):
        return self.ex.store.load(pid)[0]

    def bus(self, type_=None):
        from tb.core import bus
        evs = bus.read(0, limit=1000, root=self.root)
        return [e for e in evs if type_ is None or e["type"] == type_]

    def decisions(self, kind=None, what=None):
        from tb.core import decisions
        return [r for r in decisions.read(0, limit=5000, root=self.root) if (kind is None or r["kind"] == kind) and (what is None or r["what"] == what)]

    def trades(self):
        from tb.core import journal
        tr, order = journal._load()
        return [tr[i] for i in order]

    def enter(self, plan=None):
        """submit + вход: тик 100.1, свеча 1м выше 100.2, тик 100.25."""
        r = self.submit(plan)
        assert r["ok"], r
        self.tick(100.1)
        self.candle(100.3)
        self.tick(100.25)
        return r["plan_id"]
