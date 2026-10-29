"""Оснастка тестов WP15a: временный корень (state/config), фабрика случаев, 40 детерминированных синтетических случаев."""
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.memory import cases, cfg as memcfg, similar

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_memory")
T0 = "2026-10-20T10:00:00Z"
NOW = similar.ts_epoch("2026-10-28T00:00:00Z")


def ts_at(i):
    return cases.iso(similar.ts_epoch(T0) + i * 3600)


def mk(i=0, sym="DOOD", side="long", r=-1.0, confirmed=False, setup="level_reclaim", regime="range", mode="live", rules_ver="2026-10-27", err=None,
       type="trade", whatif=None, role="chat", exclude=False, ts=None, flags=None, rule_followed=None, exit_reason="sl", mem=None, **sit):
    """Валидный случай (без seq/prev/h). Числовые признаки через **sit (range_5m_pct, stop_pct, ...)."""
    s = {"sym": sym, "side": side, "setup": setup, "regime": regime, "confirmed": confirmed, "coin_class": "meme", "range_5m_pct": 1.8, "stop_pct": 2.2,
         "utc_session": "us", "utc_hour": 17, "dow": 1, "turn_surge_5m": 4.0, "spread_bps": 5.0}
    s.update(sit)
    t = ts or ts_at(i)
    src = {"actor": "agent", "role": role, "rules_ver": rules_ver, "mode": mode}
    if mem:
        src["mem"] = mem
    c = {"case_id": cases.case_id_for(t, sym, "t%d-%s" % (i, sym)), "type": type, "ts": t, "refs": {"journal_id": "J%d" % i}, "src": src, "situation": s,
         "decision": {"action": "enter" if type == "trade" else "skip", "why": "тест"},
         "result": ({"status": "closed", "r": r, "exit_reason": exit_reason} if type == "trade" else {"status": "na"}), "whatif": whatif,
         "tags": {"err": list(err or []), "rule_followed": rule_followed}, "flags": list(flags or []), "exclude_from_stats": exclude}
    return c


def synthetic(n=40):
    """n случаев: чередование сторон, сетапов, режимов; R детерминированы. Подтверждение влияет на R (нет подтверждения -> хуже)."""
    out = []
    setups = ("level_reclaim", "sweep_reject", "breakout_retest", "range_fade")
    regs = ("range", "trend", "volatile")
    for i in range(n):
        conf = i % 3 != 0
        r = (0.8 + (i % 4) * 0.3) if conf else (-1.0 - (i % 5) * 0.1)
        out.append(mk(i, sym="SYM%d" % (i % 7), side="long" if i % 2 == 0 else "short", r=round(r, 2), confirmed=conf, setup=setups[i % 4], regime=regs[i % 3],
                      err=[] if conf else ["no-confirm"], rule_followed="yes" if conf else "partial", range_5m_pct=round(0.6 + (i % 5) * 0.5, 2),
                      stop_pct=round(1.0 + (i % 4) * 0.5, 2), coin_class="meme" if i % 2 else "alt_mid", utc_session=("asia", "europe", "us")[i % 3],
                      exit_reason="sl" if not conf else "tp"))
    return out


class TmpRoot(unittest.TestCase):
    """Временный TB_ROOT-подобный корень: self.root с state/ и config/ (memory.json берётся из проекта)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbmem_")
        os.makedirs(os.path.join(self.root, "state"))
        os.makedirs(os.path.join(self.root, "config"))
        self.cfg = memcfg.load()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, lst):
        for c in lst:
            cases.append_new(c, root=self.root)

    def copy_fixtures(self):
        for src, dst in (("trades_snapshot.jsonl", "trades.jsonl"), ("ideas_snapshot.jsonl", "ideas.jsonl"), ("hot_outcomes_snapshot.jsonl", "hot_outcomes.jsonl")):
            shutil.copy(os.path.join(FIX, src), os.path.join(self.root, "state", dst))
