import copy
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401
from tb.legacy_engine import state as S  # noqa: E402
from tb.legacy_engine import tick  # noqa: E402
from test_risk import CONTRACTS, mk_pos  # noqa: E402

TS = "2026-10-27T10:00:00Z"
NOW = S.parse_ts(TS)

SIGNAL = {"action": "flat", "score": 0.0, "stop_pct": 1.0, "tp_pct": 2.0, "atr_pct": 0.3,
          "reasons": ["fake"], "warmup": False}
SIGNALS = {}


def fake_modules():
    d = types.ModuleType("data")

    def append_tick(path, ts_iso, price):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("%s,%s\n" % (ts_iso, price))

    def load_ticks(path, max_rows=None):
        out = []
        if os.path.exists(path):
            with open(path) as fh:
                for line in fh:
                    a, b = line.strip().split(",")
                    out.append((S.parse_ts(a), float(b)))
        return out[-max_rows:] if max_rows else out

    def to_bars(ticks, bs):
        bars = {}
        for t, p in ticks:
            k = (t // bs) * bs
            b = bars.setdefault(k, {"ts": k, "o": p, "h": p, "l": p, "c": p})
            b["h"], b["l"], b["c"] = max(b["h"], p), min(b["l"], p), p
        return [bars[k] for k in sorted(bars)]

    d.append_tick, d.load_ticks, d.to_bars = append_tick, load_ticks, to_bars
    s = types.ModuleType("signals")

    def compute_signal(bars, funding, cfg):
        s.calls.append(len(bars))
        return dict(SIGNALS.get("sig", SIGNAL))

    s.calls = []
    s.compute_signal = compute_signal
    return d, s


class TickBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "config"))
        shutil.copy(os.path.join(ROOT, "config", "config.json"), os.path.join(self.root, "config"))
        d, s = fake_modules()
        self.sigmod = s
        p = mock.patch.dict(sys.modules, {"tb.marketdata.data": d, "tb.legacy_engine.signals": s})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        SIGNALS.clear()

    def inp(self, **kw):
        d = {"ts": TS, "prices": {"ETH": 2000.0, "BTC": 80000.0},
             "funding_hourly": {"ETH": 0.0001, "BTC": 0.0001}, "balance_usdt": 30.0,
             "positions": [], "contracts": copy.deepcopy(CONTRACTS)}
        d.update(kw)
        return d

    def run_tick(self, argv=(), data=None, wall=NOW, raw=None):
        text = raw if raw is not None else json.dumps(data if data is not None else self.inp())
        return tick.process(["--root", self.root] + list(argv), text, wall_now=wall)

    def start(self, **kw):
        return self.run_tick(["--start"], kw.pop("data", self.inp()))

    def state(self):
        return S.load_state(os.path.join(self.root, "state", "state.json"))

    def journal(self):
        return S.read_journal(os.path.join(self.root, "state", "journal.jsonl"))

    def long_signal(self, **kw):
        SIGNALS["sig"] = dict(SIGNAL, action="long", score=0.8, **kw)


class TestTick(TickBase):
    def test_no_start_no_open(self):
        self.long_signal()
        out = self.run_tick()
        self.assertEqual(out["decisions"][0]["op"], "hold")
        self.assertIn("session_inactive", out["diagnostics"]["open_rejections"]["ETH"])

    def test_start_then_open_long_levels(self):
        st = self.start()
        self.assertTrue(st["started"])
        self.assertEqual(st["day_start_equity"], 30.0)
        self.long_signal()
        out = self.run_tick(data=self.inp(prices={"ETH": 2000.0}))
        ops = [d for d in out["decisions"] if d["op"] == "open"]
        self.assertEqual(len(ops), 1)
        o = ops[0]
        self.assertEqual((o["symbol"], o["side"], o["contract_id"]), ("ETH", "long", 0))
        self.assertAlmostEqual(o["stop_price"], 1980.0)
        self.assertAlmostEqual(o["tp_price"], 2040.0)
        self.assertEqual(o["margin"], 6.0)
        self.assertEqual(self.state()["trades_today"], 1)
        self.assertIn("open_decision", [e["type"] for e in self.journal()])

    def test_open_short_levels(self):
        self.start()
        SIGNALS["sig"] = dict(SIGNAL, action="short", score=0.7)
        out = self.run_tick(data=self.inp(prices={"ETH": 2000.0}))
        o = [d for d in out["decisions"] if d["op"] == "open"][0]
        self.assertGreater(o["stop_price"], 2000)
        self.assertLess(o["tp_price"], 2000)

    def test_max_two_opens_in_one_tick_and_reserve(self):
        self.start()
        self.long_signal()
        out = self.run_tick()
        opens = [d for d in out["decisions"] if d["op"] == "open"]
        self.assertEqual(len(opens), 2)  # ETH + BTC; 30-12-fees >= 8
        self.assertEqual(self.state()["trades_today"], 2)

    def test_reserve_blocks_second(self):
        self.start(data=self.inp(balance_usdt=20.0))
        self.long_signal()
        out = self.run_tick(data=self.inp(balance_usdt=20.0))
        opens = [d for d in out["decisions"] if d["op"] == "open"]
        self.assertGreaterEqual(len(opens), 1)
        # динамический размер ужимает входы, но свободный остаток после них не ниже резерва (20 - 8 = 12)
        self.assertLessEqual(sum(d["margin"] for d in opens), 12.0)

    def test_stale_blocks_open(self):
        self.start()
        self.long_signal()
        out = self.run_tick(wall=NOW + 500)
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])
        self.assertTrue(any("stale" in r for r in out["diagnostics"]["open_rejections"]["ETH"]))

    def test_session_inactive_after_stop_no_open(self):
        self.start()
        self.run_tick(["--stop"], data=self.inp())
        self.long_signal()
        out = self.run_tick()
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_stop_flatten(self):
        self.start()
        pos = mk_pos(0, stop="1980")
        out = self.run_tick(["--stop"], data=self.inp(positions=[pos]))
        self.assertTrue(out["flatten"])
        self.assertEqual([d["op"] for d in out["decisions"]], ["close"])
        self.assertFalse(self.state()["session_active"])
        out = self.run_tick(["--stop"], raw="")
        self.assertTrue(out["flatten"])

    def test_kill_switch_closes_all(self):
        self.start()
        self.long_signal()
        pos = mk_pos(0, entry="2000", stop="500")
        out = self.run_tick(data=self.inp(balance_usdt=24.0, positions=[pos], prices={"ETH": 1600.0, "BTC": 80000.0}))
        self.assertTrue(out["halt"])
        self.assertEqual([d["op"] for d in out["decisions"]], ["close"])
        self.assertTrue(self.state()["halted"])
        # на следующем тике halt липкий и открытий нет
        out = self.run_tick(data=self.inp(ts="2026-10-27T10:01:00Z"), wall=NOW + 60)
        self.assertTrue(out["halt"])
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_start_refuses_override_of_loss_halt_same_day(self):
        self.start()
        self.run_tick(data=self.inp(balance_usdt=26.0))
        out = self.run_tick(["--start"], data=self.inp(balance_usdt=26.0))
        self.assertFalse(out["started"])
        out = self.run_tick(["--start", "--force"], data=self.inp(balance_usdt=26.0))
        self.assertTrue(out["started"])

    def test_missing_stop_set_auto_close_and_adoption(self):
        self.start()
        pos = mk_pos(0, entry="2000")
        out = self.run_tick(data=self.inp(positions=[pos]))
        self.assertEqual(out["decisions"][0]["op"], "set_auto_close")
        self.assertIn("position_adopted", [e["type"] for e in self.journal()])
        self.assertEqual(self.state()["trades_today"], 0)

    def test_max_hold_closes(self):
        self.start()
        pos = mk_pos(0, entry="2000", stop="1950", tp="2100", created="2026-10-27T05:00:00Z")
        out = self.run_tick(data=self.inp(positions=[pos]))
        self.assertEqual(out["decisions"][0]["op"], "close")

    def test_cooldown_after_stop(self):
        self.start()
        pos = mk_pos(0, entry="2000", stop="1980", tp="2100")
        self.run_tick(data=self.inp(positions=[pos], prices={"ETH": 1985.0, "BTC": 80000.0}))
        # позиция исчезла (сработал стоп), pnl был отрицательный
        self.long_signal()
        out = self.run_tick(data=self.inp(ts="2026-10-27T10:05:00Z", balance_usdt=29.5), wall=NOW + 300)
        self.assertEqual(S.parse_ts(self.state()["last_stop_ts"]["ETH"]), NOW + 300)
        rej = out["diagnostics"]["open_rejections"]["ETH"]
        self.assertTrue(any("cooldown" in r for r in rej), rej)

    def test_no_open_after_pending_not_yet_visible(self):
        self.start()
        self.long_signal()
        self.run_tick()
        out = self.run_tick(data=self.inp(ts="2026-10-27T10:01:00Z"), wall=NOW + 60)
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_max_trades_per_day(self):
        self.start()
        st = self.state()
        st["trades_today"] = 12
        S.save_state(os.path.join(self.root, "state", "state.json"), st)
        self.long_signal()
        out = self.run_tick()
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_new_utc_day_resets_counters(self):
        self.start()
        st = self.state()
        st.update(trades_today=12, realized_pnl_today=-2.0)
        S.save_state(os.path.join(self.root, "state", "state.json"), st)
        out = self.run_tick(data=self.inp(ts="2026-10-28T00:00:30Z", balance_usdt=29.0),
                            wall=S.parse_ts("2026-10-28T00:00:30Z"))
        st = self.state()
        self.assertEqual(st["day"], "2026-10-28")
        self.assertEqual(st["trades_today"], 0)
        self.assertEqual(st["day_start_equity"], 29.0)
        self.assertFalse(out["halt"])

    def test_bad_inputs_halt_json(self):
        for raw in ("", "   ", "not json", "[]", "null", '{"ts": "x"}', '{"ts": "%s"}' % TS,
                    json.dumps(self.inp(positions="oops"))):
            out = self.run_tick(raw=raw)
            self.assertTrue(out["halt"], raw)
            self.assertTrue(out["halt_reason"].startswith("error"), raw)
            self.assertEqual(out["decisions"], [])
            json.dumps(out)

    def test_garbage_fields_do_not_open(self):
        self.start()
        self.long_signal()
        d = self.inp(prices={"ETH": "abc", "BTC": None}, balance_usdt="x", contracts={"ETH": 5},
                     positions=[None, "x", {"contract_id": "?"}], funding_hourly=[1])
        out = self.run_tick(data=d)
        self.assertFalse([x for x in out["decisions"] if x["op"] == "open"])
        json.dumps(out, allow_nan=False)

    def test_nan_price_json_safe(self):
        self.start()
        out = self.run_tick(raw='{"ts": "%s", "prices": {"ETH": NaN}, "positions": [], "balance_usdt": Infinity}' % TS)
        json.dumps(out, allow_nan=False)
        self.assertFalse([x for x in out["decisions"] if x["op"] == "open"])

    def test_signal_exception_means_flat(self):
        self.start()
        self.sigmod.compute_signal = mock.Mock(side_effect=RuntimeError("x"))
        out = self.run_tick()
        self.assertFalse(out["halt"])
        self.assertEqual(out["decisions"][0]["op"], "hold")

    def test_missing_modules_gives_error_halt(self):
        with mock.patch.dict(sys.modules, {"tb.marketdata.data": None}):
            out = self.run_tick()
        self.assertTrue(out["halt"])

    def test_duplicate_tick_no_open(self):
        self.start()
        self.run_tick()
        self.long_signal()
        out = self.run_tick()
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_bars_drop_forming_bar(self):
        self.start()
        self.run_tick()
        self.assertEqual(self.sigmod.calls[-1], 0)  # единственный бар ещё формируется

    def test_report(self):
        self.start()
        self.long_signal()
        self.run_tick()
        rep = self.run_tick(["--report"], raw="")
        self.assertEqual(rep["trades_today"], 2)
        self.assertEqual(len(rep["opens"]), 2)
        json.dumps(rep)

    def test_corrupt_state_halts_trading(self):
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        with open(os.path.join(self.root, "state", "state.json"), "w") as f:
            f.write("{{")
        self.long_signal()
        out = self.run_tick()
        self.assertTrue(out["halt"])
        self.assertFalse([d for d in out["decisions"] if d["op"] == "open"])

    def test_bad_args(self):
        out = tick.process(["--root", self.root, "--bogus"], "{}", wall_now=NOW)
        self.assertTrue(out["halt"])


if __name__ == "__main__":
    unittest.main()
