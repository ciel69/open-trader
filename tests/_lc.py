"""Стенд тестов жизненного цикла (WP14): временный корень (как у тестов ctl), подмена путей журнала/сессий, фейковые часы/мозг/исполнитель,
хелперы «всё готово для старта» и позиции для закрытия. Реальный state/, ~/.config/traider-bot и биржа не затрагиваются."""
import json
import os
import time

import _bootstrap  # noqa: F401
import _ctl
from fakes.fake_lifecycle import FakeBrain, FakeExec
from tb import paths
from tb.control import ctlstate
from tb.core import journal, learning, pending, portfolio, session
from tb.lifecycle import api, days, runtime

NOW = _ctl.NOW           # 2026-10-27T12:20:00Z
iso = _ctl.iso

DAYPLAN_OK = {"schema": "tb.dayplan/1", "regime": "range", "bias": "neutral", "session": {"type": "intraday", "minutes": 360},
              "watch": [{"sym": "SOL", "side": "long", "level": 144.0, "setup": "level_reclaim", "note": "уровень"}], "allowed_setups": ["level_reclaim", "sweep_reject"],
              "avoid": ["BEAT"], "limits": {"max_new_plans": 3, "max_risk_usdt": 0.5, "symbols": ["SOL", "ETH"], "size_mult": 1.0}, "carry": {"allow": True, "max_positions": 1},
              "risks": ["fomc_in_min_340"], "questions": [], "why": "диапазон, торгуем от уровней"}


def carry_rec(item, decision="carry", basis=("trend_intact", "level_held"), against=(), risk="low", why="тренд сохранён | цена выше уровня | риск низкий", conf=0.6, **over):
    r = {"schema": "tb.carry/1", "item": item, "decision": decision, "basis": list(basis), "against": list(against), "view": {"risk": risk, "horizon_h": 12, "recheck_min": 240},
         "why": why, "conf": conf}
    r.update(over)
    return r


def audit_out(*recs, **kw):
    out = {"schema": "tb.audit/1", "day_id": "x", "carry": list(recs), "changes": [], "owner_questions": [], "narrative": kw.get("narrative", "день спокойный")}
    return out


class LC(_ctl.Env):
    """_ctl.Env + настоящий tb.lifecycle.api + FakeBrain/FakeExec/часы. cfg_over: переопределения config/lifecycle.json."""

    def __init__(self, test, mode="paper", cfg_over=None, **kw):
        _ctl.Env.__init__(self, test, real_lifecycle=True, **kw)
        self.mode = mode
        self.keys_dir = self.keys         # каталог ключей теста (TB_KEYS_DIR): поддельные файлы биржи, чтобы P6 (launcher.key_status) не заглядывал в настоящий ~/.config/traider-bot
        for n in ("bybit_mainnet_trade.env", "bybit_testnet.env", "bybit_mainnet.env"):
            with open(os.path.join(self.keys, n), "w", encoding="utf-8") as f:
                f.write("BYBIT_API_KEY=fake\nBYBIT_API_SECRET=fake\n")
        self._old_paths = (journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH)
        journal.PATH = self.p("state", "trades.jsonl")
        portfolio.PATH = self.p("state", "portfolio.json")
        session.PATH = self.p("state", "sessions.jsonl")
        learning.PATH = self.p("state", "learning.json")
        pending.PATH = self.p("state", "pending_orders.json")
        test.addCleanup(self._restore_paths)
        self.t = self.clock_t = [NOW]          # одни часы для lifecycle (runtime) и для ctl/команд (_ctl.Env)
        self.brain = FakeBrain(avail=True)
        self.ex = FakeExec(now=NOW)
        runtime.configure(clock=lambda: self.t[0], sleep=self.advance_t, brain=self.brain, execport=self.ex, sync=True)
        test.addCleanup(runtime.reset)
        cfg = {"preflight": {"require_supervisor": False}}
        for k, v in (cfg_over or {}).items():
            cfg.setdefault(k, {}).update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
        self.w("config/lifecycle.json", cfg)
        self.w("config/agents.json", {"agents": {"api": {"role": "brain-api", "enabled": True, "tools": ["@read"],
                                                        "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "modes": ["paper", "live"], "max_size_mult": 1.0,
                                                                   "allow_add": False}}}})
        self.w("config/watch_config.json", {"symbols": ["BTC", "ETH", "SOL"]})
        self.w("state/portfolio.json", {"venues": {"bybit": {"equity": 30.0, "free": 30.0, "ts": iso(NOW)}}, "hwm": 30.0})
        self.ready()

    def _restore_paths(self):
        journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH = self._old_paths

    def advance(self, s):                      # _ctl.Env.advance (ожидания challenge) двигает те же часы
        self.advance_t(s)

    def ctl(self, clock=None, reconf=None, sync=True):
        from tb.control import handlers
        return handlers.Ctl(self.root, clock=clock or (lambda: self.t[0]), sleep=self.advance_t, sync=sync, reconf=reconf)

    def advance_t(self, s):
        self.t[0] += s
        self.now = self.t[0]

    # ---- окружение «всё готово для старта»
    def ready(self, mode=None):
        mode = mode or self.mode
        self.ex.profile = {"live": "trade", "testnet": "testnet"}.get(mode, "paper")
        self.ex.observe = mode == "observe"
        now = self.t[0]
        self.good_exec(profile=self.ex.profile, observe=self.ex.observe, now=now)
        self.w("state/portfolio.json", {"venues": {"bybit": {"equity": 30.0, "free": 30.0, "ts": iso(now)}}, "hwm": 30.0})
        self.w("runtime/exec_snapshot.json", {"ts_epoch": now, "env": "mainnet", "account": {"equity": 30.0, "free": 30.0}, "positions": [], "orders": []})
        for rel in ("runtime/market.json", "runtime/context.json", "runtime/levels.json"):
            self.w(rel, {"ts_epoch": now})
            os.utime(self.p(*rel.split("/")), (now, now))
        self.facts(now)
        self.w("runtime/instruments.json", {"ts_epoch": now, "symbols": {"SOL": {}, "ETH": {}, "BTC": {}}})
        self.w("runtime/platform/brain.hb.json", {"ts_epoch": now, "pid": 7, "service": "brain"})
        if not os.path.exists(self.p("state", "trades.jsonl")):
            self.w("state/trades.jsonl", "", raw=True)

    def facts(self, now=None, **over):
        now = self.t[0] if now is None else now
        f = {"schema": "tb.facts/1", "ts": int(now) - 60, "degraded": [], "calendar": {"next": []}, "deriv": {"SOL": {"funding_pct": 0.01}, "ETH": {"funding_pct": 0.01}},
             "events": [], "exchange": {"status": "ok", "delist_syms": []}}
        f.update(over)
        self.w("runtime/intel/facts.json", f)
        return f

    def enter_day(self, mode=None, session_type="intraday", minutes=360, **extra):
        a = {"mode": mode or self.mode, "session": session_type, "minutes": minutes, "allow_amber": True}
        a.update(extra)
        return api.start_day(self.root, a, self.t[0])

    def day(self, day_id=None):
        d = days.current(self.root) if day_id is None else days.load(self.root, day_id)
        return d

    def start(self, **kw):
        self.brain.set("morning_analyst", DAYPLAN_OK)
        r = self.enter_day(**kw)
        self.test.assertTrue(r["ok"], r)
        return r["data"]["day_id"]

    def stop(self, **kw):
        return api.stop_day(self.root, kw, self.t[0])

    def step(self, d, name, phase):
        return days.get_step(d, name, phase)

    def bus_events(self, type_=None):
        from tb.core import bus
        return [e for e in bus.read(0, limit=2000, root=self.root) if type_ is None or e["type"] == type_]

    def rd_day(self, day_id, name):
        return ctlstate.read_json(os.path.join(days.day_dir(self.root, day_id), name))
