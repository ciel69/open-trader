import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401
from tb.legacy_engine import risk  # noqa: E402
from tb.legacy_engine import state as S  # noqa: E402

with open(os.path.join(ROOT, "config", "config.json")) as _f:
    CFG = json.load(_f)

NOW = S.parse_ts("2026-10-27T10:00:00Z")
CONTRACTS = {"ETH": {"contract_id": 0, "min_size_quote": "1.0", "max_leverage": 50, "taker_fee_pct": "0.0009"},
             "BTC": {"contract_id": 1, "min_size_quote": "1.0", "max_leverage": 50, "taker_fee_pct": "0.0009"}}


def mk_state(**kw):
    st = S.default_state()
    st.update(session_active=True, day="2026-10-27", day_start_equity=30.0)
    st.update(kw)
    return st


def mk_snap(**kw):
    s = {"now": NOW, "wall_now": NOW, "prices": {"ETH": 2000.0, "BTC": 80000.0}, "price_ts": {},
         "balance": 30.0, "positions": [], "contracts": copy.deepcopy(CONTRACTS), "planned": []}
    s.update(kw)
    return s


def mk_pos(cid=0, side="long", entry="2000", margin="6", lev="3", stop=None, tp=None,
           created="2026-10-27T09:30:00Z", status="open", liq="1400"):
    ac = {}
    if tp:
        ac["take_profit"] = {"trigger_price_quote": tp}
    if stop:
        ac["stop_loss"] = {"trigger_price_quote": stop}
    return {"contract_id": cid, "side": side, "status": status, "leverage": lev, "value_base": "0.009",
            "margin_quote": margin, "avg_entry_price": entry, "liquidation_price": liq,
            "funding_paid_quote": "0", "auto_close": ac, "created_at": created}


def prop(sym="ETH", side="long", **kw):
    p = {"symbol": sym, "side": side, "contract_id": CONTRACTS[sym]["contract_id"], "ref_price": 2000.0,
         "stop_pct": 1.0, "tp_pct": 2.0}
    p.update(kw)
    return p


class TestLevels(unittest.TestCase):
    def test_long(self):
        s, t = risk.compute_levels("long", 2000.0, 1.0, 2.0)
        self.assertAlmostEqual(s, 1980.0)
        self.assertAlmostEqual(t, 2040.0)

    def test_short(self):
        s, t = risk.compute_levels("short", 2000.0, 1.0, 2.0)
        self.assertAlmostEqual(s, 2020.0)
        self.assertAlmostEqual(t, 1960.0)
        self.assertGreater(s, 2000.0)
        self.assertLess(t, 2000.0)

    def test_bad_side(self):
        with self.assertRaises(ValueError):
            risk.compute_levels("flat", 1.0, 1.0, 1.0)


class TestCheckOpen(unittest.TestCase):
    def ok(self, **kw):
        st = kw.pop("st", mk_state())
        snap = kw.pop("snap", mk_snap())
        return risk.check_open(st, snap, kw.pop("proposal", prop()), CFG)

    def test_happy(self):
        r = self.ok()
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["margin"], 6.0)

    def test_session_inactive(self):
        r = self.ok(st=mk_state(session_active=False))
        self.assertFalse(r["ok"])
        self.assertIn("session_inactive", r["reasons"])

    def test_stale_blocks(self):
        r = self.ok(snap=mk_snap(wall_now=NOW + 151))
        self.assertFalse(r["ok"])
        self.assertTrue(any("stale" in x for x in r["reasons"]))
        r = self.ok(snap=mk_snap(price_ts={"ETH": NOW - 200}))
        self.assertFalse(r["ok"])
        # свежие данные -- ок
        self.assertTrue(self.ok(snap=mk_snap(wall_now=NOW + 100))["ok"])

    def test_missing_or_bad_price(self):
        for bad in (None, 0, -1, "abc", float("nan")):
            r = self.ok(snap=mk_snap(prices={"ETH": bad}))
            self.assertFalse(r["ok"])

    def test_reserve(self):
        # 30 - 6 - fee(0.0162) = 23.98 >= 8 ok; balance 14 -> 7.98 < 8 blocked
        self.assertTrue(self.ok(snap=mk_snap(balance=14.1), st=mk_state(day_start_equity=14.1))["ok"])
        r = self.ok(snap=mk_snap(balance=14.0, ), st=mk_state(day_start_equity=14.0))
        self.assertFalse(r["ok"])
        self.assertTrue(any("reserve" in x for x in r["reasons"]))

    def test_reserve_counts_planned(self):
        r = self.ok(snap=mk_snap(balance=20.0, planned=[{"symbol": "BTC", "margin": 6.0}]),
                    st=mk_state(day_start_equity=20.0))
        self.assertFalse(r["ok"])

    def test_max_trades(self):
        r = self.ok(st=mk_state(trades_today=12))
        self.assertIn("max_trades_per_day", r["reasons"])
        self.assertTrue(self.ok(st=mk_state(trades_today=11))["ok"])

    def test_cooldown_after_stop(self):
        r = self.ok(st=mk_state(last_stop_ts={"ETH": NOW - 10 * 60}))
        self.assertTrue(any("cooldown" in x for x in r["reasons"]))
        self.assertTrue(self.ok(st=mk_state(last_stop_ts={"ETH": NOW - 21 * 60}))["ok"])
        # другой контракт не затронут
        self.assertTrue(self.ok(st=mk_state(last_stop_ts={"BTC": NOW - 60}))["ok"])

    def test_no_second_position_same_contract(self):
        r = self.ok(snap=mk_snap(positions=[mk_pos(0, stop="1980")]))
        self.assertIn("position_exists_on_contract", r["reasons"])

    def test_closed_position_not_counted(self):
        self.assertTrue(self.ok(snap=mk_snap(positions=[mk_pos(0, status="closed")]))["ok"])

    def test_max_open_positions(self):
        snap = mk_snap(positions=[mk_pos(1, entry="80000", stop="79000")],
                       planned=[{"symbol": "SOL", "margin": 1.0}])
        r = self.ok(snap=snap)
        self.assertIn("max_open_positions", r["reasons"])

    def test_halted_and_daily_loss(self):
        self.assertFalse(self.ok(st=mk_state(halted=True, halt_reason="x"))["ok"])
        self.assertFalse(self.ok(snap=mk_snap(balance=26.9), st=mk_state(day_start_equity=30.0))["ok"])

    def test_min_size_quote(self):
        snap = mk_snap()
        snap["contracts"]["ETH"]["min_size_quote"] = "7.0"
        r = self.ok(snap=snap)
        self.assertFalse(r["ok"])
        snap["contracts"]["ETH"]["min_size_quote"] = "garbage"
        self.assertIn("min_size_quote_unknown", self.ok(snap=snap)["reasons"])

    def test_liq_distance(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["leverage"] = 10  # 100/10 - 1 = 9% < 15%
        r = risk.check_open(mk_state(), mk_snap(), prop(), cfg)
        self.assertFalse(r["ok"])
        self.assertTrue(any("liq_distance" in x for x in r["reasons"]))

    def test_leverage_capped_by_contract(self):
        snap = mk_snap()
        snap["contracts"]["ETH"]["max_leverage"] = 2
        r = self.ok(snap=snap)
        self.assertEqual(r["leverage"], 2)

    def test_unknown_equity_or_day_start(self):
        self.assertFalse(self.ok(snap=mk_snap(balance=None))["ok"])
        self.assertFalse(self.ok(st=mk_state(day_start_equity=None))["ok"])

    def test_garbage_proposal(self):
        for p in ({}, {"symbol": "ETH"}, prop(stop_pct="x"), prop(side="up"), prop(ref_price=None),
                  prop(tp_pct=-1), prop(ref_price=3000.0)):
            self.assertFalse(self.ok(proposal=p)["ok"], p)

    def test_pending_open_and_recent_close_block(self):
        r = self.ok(st=mk_state(pending_open={"ETH": {"ts": NOW - 30}}))
        self.assertIn("pending_open", r["reasons"])
        r = self.ok(st=mk_state(last_close_ts={"ETH": NOW - 30}))
        self.assertIn("recently_closed", r["reasons"])


class TestKillSwitch(unittest.TestCase):
    def test_equity_drop_halts(self):
        ks = risk.kill_switch(mk_state(), mk_snap(balance=26.99), CFG)
        self.assertTrue(ks["halt"])
        self.assertIn("daily_loss_limit", ks["reason"])

    def test_below_limit_ok(self):
        self.assertFalse(risk.kill_switch(mk_state(), mk_snap(balance=27.5), CFG)["halt"])

    def test_unrealized_loss_counts_with_margin(self):
        # баланс 24 + маржа 6 = 30, цена -> лонг 3x, -20% цены = -3.6 pnl => equity 26.4
        pos = mk_pos(0, entry="2000", margin="6", lev="3", stop="1000")
        snap = mk_snap(balance=24.0, positions=[pos], prices={"ETH": 1600.0, "BTC": 80000.0})
        eq, info = risk.compute_equity(snap)
        self.assertAlmostEqual(eq, 26.4)
        self.assertTrue(risk.kill_switch(mk_state(), snap, CFG)["halt"])

    def test_short_pnl_sign(self):
        pos = mk_pos(0, side="short", entry="2000", stop="2100")
        self.assertAlmostEqual(risk.position_pnl(risk.parse_position(pos), 2200.0), 6 * 3 * -0.1)
        self.assertAlmostEqual(risk.position_pnl(risk.parse_position(pos), 1800.0), 6 * 3 * 0.1)

    def test_no_baseline_uses_unrealized(self):
        pos = mk_pos(0, entry="2000", stop="1000")
        snap = mk_snap(balance=24.0, positions=[pos], prices={"ETH": 1600.0})
        self.assertTrue(risk.kill_switch(mk_state(day_start_equity=None), snap, CFG)["halt"])

    def test_sticky(self):
        self.assertTrue(risk.kill_switch(mk_state(halted=True, halt_reason="r"), mk_snap(), CFG)["halt"])

    def test_garbage_positions(self):
        snap = mk_snap(positions=["x", None, {"contract_id": 0, "margin_quote": "abc"}])
        ks = risk.kill_switch(mk_state(), snap, CFG)
        self.assertIn("halt", ks)


class TestManage(unittest.TestCase):
    def manage(self, positions, st=None, **snapkw):
        return risk.manage_positions(st or mk_state(), mk_snap(positions=positions, **snapkw), CFG)

    def test_max_hold_closes(self):
        d = self.manage([mk_pos(0, stop="1950", created="2026-10-27T05:59:00Z")])
        self.assertEqual([x["op"] for x in d], ["close"])
        self.assertEqual(d[0]["code"], "max_hold")

    def test_not_yet_max_hold(self):
        self.assertEqual(self.manage([mk_pos(0, stop="1950", tp="2100", created="2026-10-27T06:30:00Z")]), [])

    def test_missing_stop_gives_set_auto_close_long(self):
        d = self.manage([mk_pos(0, side="long", entry="2000")])
        self.assertEqual(d[0]["op"], "set_auto_close")
        self.assertLess(d[0]["stop_price"], 2000)
        self.assertGreater(d[0]["tp_price"], 2000)

    def test_missing_stop_short_directions(self):
        d = self.manage([mk_pos(0, side="short", entry="2000")])
        self.assertEqual(d[0]["op"], "set_auto_close")
        self.assertGreater(d[0]["stop_price"], 2000)
        self.assertLess(d[0]["tp_price"], 2000)

    def test_missing_stop_but_price_already_beyond_closes(self):
        d = self.manage([mk_pos(0, side="long", entry="2000")], prices={"ETH": 1900.0})
        self.assertEqual(d[0]["op"], "close")

    def test_stop_breach_closes(self):
        d = self.manage([mk_pos(0, stop="1980", tp="2100")], prices={"ETH": 1979.0})
        self.assertEqual((d[0]["op"], d[0]["code"]), ("close", "stop"))
        d = self.manage([mk_pos(0, side="short", entry="2000", stop="2020", tp="1900")],
                        prices={"ETH": 2021.0})
        self.assertEqual(d[0]["code"], "stop")

    def test_tp_breach_closes(self):
        d = self.manage([mk_pos(0, stop="1980", tp="2100")], prices={"ETH": 2101.0})
        self.assertEqual(d[0]["code"], "tp")

    def test_stale_price_no_price_based_close_but_max_hold_works(self):
        snap_kw = dict(wall_now=NOW + 1000, prices={"ETH": 1000.0})
        self.assertEqual(self.manage([mk_pos(0, stop="1980", tp="2100")], **snap_kw), [])
        d = self.manage([mk_pos(0, stop="1980", created="2026-10-27T01:00:00Z")], **snap_kw)
        self.assertEqual(d[0]["code"], "max_hold")

    def test_manual_position_adopted_stop_from_tracking(self):
        st = mk_state(positions={"0": {"stop": 1990.0, "tp": 2050.0, "first_seen": "2026-10-27T09:59:00Z"}})
        d = self.manage([mk_pos(0, created="")], st=st)
        self.assertEqual(d[0]["op"], "set_auto_close")
        self.assertEqual(d[0]["stop_price"], 1990.0)

    def test_liq_risk(self):
        d = self.manage([mk_pos(0, stop="1500", liq="2050")])
        self.assertEqual(d[0]["code"], "liq_risk")

    def test_garbage_positions_tolerated(self):
        self.assertEqual(self.manage(["x", None, 5, {"contract_id": 99}]), [])


if __name__ == "__main__":
    unittest.main()
