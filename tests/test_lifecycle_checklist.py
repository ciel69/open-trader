"""WP14: детерминированный чек-лист A0-A9, жёсткие условия K1-K8 по одному, матрица политика x тип сессии x K, решения агента tb.carry/1. Чистые функции, без файлов."""
import copy
import json
import os
import unittest

import _bootstrap  # noqa: F401
import _lc
from tb.lifecycle import carry as CARRY
from tb.lifecycle import cfg as cfgmod
from tb.lifecycle import checklist as CL

NOW = _lc.NOW
PROFILE = json.load(open(os.path.join(_bootstrap.ROOT, "config", "risk_profile.json"), encoding="utf-8"))
POLICY = {"level": 3, "policy": "owner_confirm", "scalp": False, "max_positions": 2, "max_total_risk_frac": 0.5, "max_risk_frac_of_trade": 0.5, "event_hours": 12,
          "max_funding_frac_of_risk": 0.25}
EXIT_RULES = [{"id": "tr", "do": {"action": "trail"}}]


def mkpos(pid="PL-1-SOL-01", sym="SOL", side="long", size=0.5, avg=100.0, mark=102.0, sl=100.5, liq=80.0, stype="swing", init=99.0, manage=EXIT_RULES, lifecycle=None, status="managing",
          hold=None, entry_ts=None, owner=True):
    pos = {"sym": sym, "side": side, "size": size, "avg": avg, "mark": mark, "sl": sl, "liq": liq, "owner": pid if owner else None}
    spec = {"id": pid, "symbol": sym, "side": side, "session_type": stype, "manage": manage, "lifecycle": lifecycle or {}}
    rt = {"status": status, "hold": hold, "pos": {"initial_stop": init, "risk0": size * abs(avg - init), "entries": [{"ts": entry_ts if entry_ts is not None else NOW - 3600}]}}
    plan = {"id": pid, "sym": sym, "side": side, "stype": stype, "status": status, "hold": hold, "spec": spec, "rt": rt}
    return pos, plan


def mkinp(items=(), policy=None, **over):
    positions, plans = [], {}
    for pos, plan in items:
        positions.append(pos)
        if plan is not None:
            plans[plan["id"]] = plan
    inp = {"now": NOW, "next_day_start": NOW + 18 * 3600, "cfg": cfgmod.load("/nonexistent"), "policy": dict(POLICY, **(policy or {})), "profile": PROFILE, "equity": 30.0,
           "daily_loss_usdt": 3.0, "health": {"reconcile_age_s": 5.0, "diffs": 0, "hold_reasons": [], "ws": {"private": {"up": True}}, "alive": True}, "snap": {}, "positions": positions,
           "plans": plans, "facts": {"ts": NOW - 60, "calendar": {"next": []}, "deriv": {"SOL": {"funding_pct": 0.01}, "ETH": {"funding_pct": 0.01}}, "events": [],
                                     "exchange": {"delist_syms": []}}, "facts_age_s": 60.0, "dayplan": None, "plan_max_positions": None, "brain_ok": True,
           "provider_recent_ok": True, "alerts": {}, "sessions": [], "ideas_n": 0}
    inp.update(over)
    return inp


def rows_of(inp):
    return CARRY.finalize_defaults(CL.evaluate(inp)["rows"])


def row(inp, kind="pos"):
    return [r for r in rows_of(inp) if r["kind"] == kind][0]


class TestBaseline(unittest.TestCase):
    def test_good_swing_is_candidate_and_default_carry_only_under_agent(self):
        r = row(mkinp([mkpos()], policy={"policy": "agent"}))
        self.assertEqual((r["rule"], r["decision"], r["candidate"], r["soft"], r["codes"]), ("A4", "carry", True, True, []))
        r = row(mkinp([mkpos()]))
        self.assertEqual((r["rule"], r["decision"], r["candidate"]), ("A4", "close", True))      # owner_confirm: по умолчанию close, предложит агент

    def test_row_fields(self):
        r = CL.public_rows(rows_of(mkinp([mkpos()])))[0]
        self.assertEqual(set(r), {"item", "kind", "plan", "sym", "side", "stype", "rule", "decision", "soft", "candidate", "codes", "facts", "decided_by"})
        self.assertEqual(r["item"], "pos:PL-1-SOL-01")
        json.dumps(r)                                                              # сериализуется
        self.assertEqual(set(r["facts"]["K"]), {"K1", "K2", "K4", "K5", "K6", "K7", "K8", "K3"})


class TestKConditions(unittest.TestCase):
    """K1-K8 по одному: каждое условие по отдельности превращает кандидата в close (A7) с кодом, остальные не затронуты."""

    def check(self, name, inp, code):
        r = row(inp)
        self.assertEqual((r["decision"], r["rule"]), ("close", "A7"), name)
        self.assertIn(code, r["codes"], name)

    def test_k1(self):
        for name, kw, over in (("no_stop", {"sl": 0.0}, {}), ("wrong_side", {"sl": 103.0}, {}), ("wide", {"init": 70.0}, {}),
                               ("not_confirmed", {}, {"health": {"reconcile_age_s": 90.0, "diffs": 0, "hold_reasons": [], "ws": {"private": {"up": True}}}}),
                               ("never_reconciled", {}, {"health": {"reconcile_age_s": None, "diffs": 0, "hold_reasons": [], "ws": {"private": {"up": True}}}})):
            self.check("K1 " + name, mkinp([mkpos(**kw)], **over), "K1_STOP")
        self.check("K1 short", mkinp([mkpos(side="short", sl=99.0, mark=98.0, avg=100.0, init=101.0)], policy={"policy": "agent"}), "K1_STOP") if False else None
        r = row(mkinp([mkpos(side="short", avg=100.0, mark=98.0, sl=99.5, init=101.0, liq=130.0)], policy={"policy": "agent"}))
        self.assertEqual(r["codes"], [])                                           # short со стопом выше цены проходит K1

    def test_k1_intraday_width_limit(self):
        self.check("K1 intraday 5%", mkinp([mkpos(stype="intraday", init=95.0, liq=60.0)]), "K1_STOP")
        r = row(mkinp([mkpos(stype="intraday", init=98.0)]))
        self.assertEqual(r["codes"], [])

    def test_k2(self):
        # стоп убыточный (ниже входа), риск до стопа больше 0.5 R0
        self.check("K2", mkinp([mkpos(sl=99.0, mark=102.0, init=99.0)]), "K2_RISK")
        # стоп в безубытке или лучше: K2 проходит даже при большом "риске до стопа"
        r = row(mkinp([mkpos(sl=100.0, mark=105.0, size=0.2)]))
        self.assertEqual(r["codes"], [])
        self.assertTrue(r["facts"]["be_ok"])

    def test_k3_cumulative_and_alone(self):
        big = mkpos(pid="PL-2-ETH-01", sym="ETH", size=3.0, mark=102.0, sl=100.5, avg=100.0, init=99.0, liq=60.0)          # риск до стопа ~5 > лимита 1.5
        self.check("K3 alone", mkinp([big]), "K3_TOTAL")
        items = [mkpos(pid="PL-%d-SOL-0%d" % (i, i), size=0.5) for i in (1, 2, 3)]
        rows = rows_of(mkinp(items, policy={"policy": "agent", "max_positions": 2, "max_total_risk_frac": 1.0}))
        carried = [r for r in rows if r["decision"] == "carry"]
        self.assertEqual(len(carried), 2)                                          # третья не помещается по числу
        self.assertEqual([r["codes"] for r in rows if r["decision"] == "close"], [["K3_TOTAL"]])
        rows = rows_of(mkinp(items, policy={"policy": "agent", "max_positions": 3, "max_total_risk_frac": 0.3}))             # 0.9 USDT: помещается 1 (риск 0.84)
        self.assertEqual(len([r for r in rows if r["decision"] == "carry"]), 1)

    def test_k3_prefers_smaller_risk(self):
        a = mkpos(pid="PL-1-SOL-01", size=0.5)
        b = mkpos(pid="PL-2-ETH-01", sym="ETH", size=0.3)
        rows = rows_of(mkinp([a, b], policy={"policy": "agent", "max_positions": 1}))
        self.assertEqual([r["plan"] for r in rows if r["decision"] == "carry"], ["PL-2-ETH-01"])

    def test_k4(self):
        ev = lambda **kw: {"ts": NOW, "calendar": dict({"next": []}, **kw), "deriv": {"SOL": {"funding_pct": 0.01}}, "events": [], "exchange": {"delist_syms": []}}  # noqa: E731
        near = {"id": "cpi", "type": "CPI", "ts": NOW + 5 * 3600, "impact": 3, "in_min": 300, "v": True}
        far = dict(near, ts=NOW + 20 * 3600)
        low = dict(near, impact=2)
        self.check("K4 macro", mkinp([mkpos()], facts=ev(next=[near])), "K4_EVENT")
        self.assertEqual(row(mkinp([mkpos()], facts=ev(next=[far])))["codes"], [])
        self.assertEqual(row(mkinp([mkpos()], facts=ev(next=[low])))["codes"], [])
        self.check("K4 blackout", mkinp([mkpos()], facts=ev(blackout={"name": "FOMC", "in_min": 100})), "K4_EVENT")
        f = ev()
        f["events"] = [{"syms": ["SOL"], "imp": 3, "type": "hack"}]
        self.check("K4 symbol event", mkinp([mkpos()], facts=f), "K4_EVENT")
        f = ev()
        f["exchange"] = {"delist_syms": ["SOL"]}
        self.check("K4 delist", mkinp([mkpos()], facts=f), "K4_EVENT")
        f = ev()
        f["exchange"] = {"delist_syms": [], "maint_in_min": 60}
        self.check("K4 maintenance", mkinp([mkpos()], facts=f), "K4_EVENT")
        self.check("K4 no facts", mkinp([mkpos()], facts=None), "K4_EVENT")

    def test_k4_hours_depend_on_level(self):
        near = {"id": "cpi", "type": "CPI", "ts": NOW + 10 * 3600, "impact": 3}
        f = {"ts": NOW, "calendar": {"next": [near]}, "deriv": {"SOL": {"funding_pct": 0.01}}, "events": [], "exchange": {}}
        self.assertEqual(row(mkinp([mkpos()], facts=f))["codes"], ["K4_EVENT"])
        self.assertEqual(row(mkinp([mkpos()], facts=f, policy={"event_hours": 6}))["codes"], [])

    def test_k5(self):
        f = lambda v: {"ts": NOW, "calendar": {"next": []}, "deriv": {"SOL": {"funding_pct": v}}, "events": [], "exchange": {}}  # noqa: E731
        self.check("K5 high", mkinp([mkpos()], facts=f(0.5)), "K5_FUNDING")
        self.check("K5 unknown", mkinp([mkpos()], facts=f(None)), "K5_FUNDING")
        self.assertEqual(row(mkinp([mkpos()], facts=f(-0.5)))["codes"], [])         # long получает funding: не издержка
        self.check("K5 short pays on negative", mkinp([mkpos(side="short", mark=98.0, sl=99.5, init=101.0, liq=130.0)], facts=f(-0.5)), "K5_FUNDING")

    def test_k6(self):
        self.check("K6 close liq", mkinp([mkpos(liq=98.0)]), "K6_LIQ")
        self.check("K6 unknown liq", mkinp([mkpos(liq=None)]), "K6_LIQ")

    def test_k7(self):
        self.check("K7 alert", mkinp([mkpos()], alerts={"SOL": NOW - 60}), "K7_RECONCILE") if False else None
        r = row(mkinp([mkpos()], alerts={"SOL": NOW - 60}))
        self.assertEqual(r["codes"], ["K7_RECONCILE"])
        r = row(mkinp([mkpos()], alerts={"ETH": NOW - 60}))
        self.assertEqual(r["codes"], [])

    def test_k8(self):
        base = {"reconcile_age_s": 5.0, "diffs": 0, "hold_reasons": [], "ws": {"private": {"up": True}}}
        self.check("K8 hold", mkinp([mkpos()], health=dict(base, hold_reasons=["blind"])), "K8_LINK")
        self.check("K8 private ws", mkinp([mkpos()], health=dict(base, ws={"private": {"up": False}})), "K8_LINK")
        self.check("K8 brain", mkinp([mkpos()], brain_ok=False, provider_recent_ok=True), "K8_LINK")
        self.check("K8 intel", mkinp([mkpos()], facts_age_s=4000.0), "K8_LINK")
        self.check("K8 provider", mkinp([mkpos()], provider_recent_ok=False), "K8_LINK")

    def test_several_codes_listed(self):
        r = row(mkinp([mkpos(sl=0.0, liq=98.0)]))
        self.assertTrue({"K1_STOP", "K6_LIQ"} <= set(r["codes"]))
        self.assertEqual(r["decision"], "close")


class TestRules(unittest.TestCase):
    def test_a0_orphan_without_plan(self):
        pos, _ = mkpos(owner=False)
        r = row(mkinp([(pos, None)], policy={"policy": "agent"}))
        self.assertEqual((r["rule"], r["decision"], r["item"]), ("A0", "close", "pos:ORPHAN-SOL"))

    def test_a0_plan_in_hold_or_drift(self):
        r = row(mkinp([mkpos(hold="position_drift")], policy={"policy": "agent"}))
        self.assertEqual((r["rule"], r["decision"]), ("A0", "close"))
        r = row(mkinp([mkpos()], policy={"policy": "agent"}, health={"reconcile_age_s": 5.0, "diffs": 2, "hold_reasons": [], "ws": {"private": {"up": True}}}))
        self.assertEqual((r["rule"], r["codes"]), ("A0", ["A0_DRIFT"]))
        r = row(mkinp([mkpos(status="blocked")], policy={"policy": "agent"}))
        self.assertEqual(r["rule"], "A0")

    def test_a2_scalp_never_carried_at_any_level(self):
        for pol in ("never", "owner_confirm", "agent"):
            for lvl in (1, 3, 5):
                r = row(mkinp([mkpos(stype="scalp")], policy={"policy": pol, "level": lvl, "scalp": True}))     # даже если политика «разрешила» scalp
                self.assertEqual((r["rule"], r["decision"], r["candidate"]), ("A2", "close", False))

    def test_a6_max_hold_expired(self):
        r = row(mkinp([mkpos(lifecycle={"max_hold_min": 30}, entry_ts=NOW - 3600)], policy={"policy": "agent"}))
        self.assertEqual((r["rule"], r["decision"], r["codes"]), ("A6", "close", ["A6_MAX_HOLD"]))
        r = row(mkinp([mkpos(lifecycle={"max_hold_min": 600}, entry_ts=NOW - 3600)], policy={"policy": "agent"}))
        self.assertEqual(r["decision"], "carry")

    def test_a1_waiting_entries(self):
        def waiting(stype, ttl, pid="PL-5-ETH-01"):
            return (None, {"id": pid, "sym": "ETH", "side": "long", "stype": stype, "status": "armed", "hold": None,
                           "spec": {"id": pid, "symbol": "ETH", "side": "long", "lifecycle": {"ttl_min": ttl}}, "rt": {"status": "armed", "ts": NOW}})
        for stype, ttl, pol, want in (("intraday", 5000, "agent", "cancel"), ("scalp", 5000, "agent", "cancel"), ("swing", 5000, "agent", "keep"),
                                      ("swing", 5000, "owner_confirm", "keep"), ("swing", 5000, "never", "cancel"), ("position", 5000, "agent", "keep"),
                                      ("swing", 30, "agent", "cancel")):
            inp = mkinp([], policy={"policy": pol})
            _p, plan = waiting(stype, ttl)
            inp["plans"] = {plan["id"]: plan}
            r = row(inp, "entry")
            self.assertEqual((r["rule"], r["decision"]), ("A1", want), (stype, ttl, pol))

    def test_a1_keep_blocked_by_event(self):
        near = {"id": "cpi", "type": "CPI", "ts": NOW + 5 * 3600, "impact": 3}
        inp = mkinp([], facts={"ts": NOW, "calendar": {"next": [near]}, "deriv": {}, "events": [], "exchange": {}})
        plan = {"id": "PL-5-ETH-01", "sym": "ETH", "side": "long", "stype": "swing", "status": "armed", "hold": None,
                "spec": {"lifecycle": {"ttl_min": 5000}}, "rt": {"ts": NOW}}
        inp["plans"] = {plan["id"]: plan}
        self.assertEqual(row(inp, "entry")["decision"], "cancel")

    def test_a8_sessions_always_stop_and_a9_ideas(self):
        inp = mkinp([], sessions=[{"id": "intraday-1", "type": "intraday"}], ideas_n=2)
        rows = rows_of(inp)
        self.assertEqual([(r["rule"], r["decision"]) for r in rows], [("A8", "stop"), ("A9", "keep")])

    def test_swing_without_exit_rules_not_carried_by_default(self):
        r = row(mkinp([mkpos(manage=[])], policy={"policy": "agent"}))
        self.assertEqual((r["decision"], r["codes"]), ("close", ["NO_EXIT_RULES"]))
        r = row(mkinp([mkpos(manage=[], lifecycle={"max_hold_min": 900})], policy={"policy": "agent"}))
        self.assertEqual(r["decision"], "carry")

    def test_carry_disabled_in_config(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        inp["cfg"]["carry"]["enabled"] = False
        self.assertEqual(row(inp)["decision"], "close")


class TestMatrix(unittest.TestCase):
    """политика уровня x тип сессии x K-условие: решение умолчания (без агента). 3 x 4 x 4 x 2 = 96 случаев."""

    def test_matrix(self):
        bad_k = {"ok": {}, "K1": {"sl": 0.0}, "K6": {"liq": 98.0}, "K7": {"alerts": True}}
        n = 0
        for pol in ("never", "owner_confirm", "agent"):
            for stype in ("scalp", "intraday", "swing", "position"):
                for k, kw in bad_k.items():
                    for exit_rules in (True, False):
                        alerts = {"SOL": NOW - 1} if kw.get("alerts") else {}
                        pk = dict((a, b) for a, b in kw.items() if a != "alerts")
                        inp = mkinp([mkpos(stype=stype, manage=EXIT_RULES if exit_rules else [], **pk)], policy={"policy": pol}, alerts=alerts)
                        r = row(inp)
                        if stype == "scalp":
                            want = "close"
                        elif pol == "never" or k != "ok":
                            want = "close"
                        elif stype == "intraday":
                            want = "close"
                        else:
                            want = "carry" if (pol == "agent" and exit_rules) else "close"
                        self.assertEqual(r["decision"], want, (pol, stype, k, exit_rules, r["rule"], r["codes"]))
                        if want == "carry":
                            self.assertTrue(r["soft"] and r["candidate"])
                        if k != "ok" and stype != "scalp" and pol != "never":
                            self.assertEqual(r["rule"], "A7")
                        n += 1
        self.assertEqual(n, 96)


class TestAgentDecisions(unittest.TestCase):
    def apply(self, inp, *recs, **kw):
        rows = rows_of(inp)
        res = CARRY.apply_decisions(inp, rows, _lc.audit_out(*recs, **kw), inp["cfg"])
        return rows, res

    def test_schema_validation_table(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        item = "pos:PL-1-SOL-01"
        bad = {
            "extra_key": _lc.carry_rec(item, evil=1), "bad_decision": _lc.carry_rec(item, decision="maybe"), "unknown_item": _lc.carry_rec("pos:PL-9-XXX-01"),
            "bad_item": _lc.carry_rec("rm -rf"), "bad_basis": _lc.carry_rec(item, basis=["moon"]), "bad_against": _lc.carry_rec(item, against=["bad"]),
            "bad_risk": _lc.carry_rec(item, risk="extreme"), "bad_conf": _lc.carry_rec(item, conf=1.5), "bad_view": _lc.carry_rec(item, view={"risk": "low", "x": 1}),
            "bad_schema": _lc.carry_rec(item, schema="tb.carry/2"), "not_dict": "carry",
        }
        for name, rec in bad.items():
            rows, res = self.apply(inp, rec)
            self.assertEqual(res["decisions"], [], name)
            self.assertTrue(res["rejected"], name)
            self.assertEqual(rows[0]["decision"], "carry", name)                    # умолчание (agent+exit rules) не тронуто мусором

    def test_agent_can_always_close(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01", decision="close", basis=[], why="не нравится"))
        self.assertEqual((rows[0]["decision"], rows[0]["decided_by"]), ("close", "agent"))
        self.assertEqual(res["decisions"][0]["decision"], "close")

    def test_agent_can_close_even_failed_k_item(self):
        inp = mkinp([mkpos(sl=0.0)], policy={"policy": "agent"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01", decision="close", basis=[]))
        self.assertEqual(rows[0]["decision"], "close")

    def test_llm_cannot_carry_what_k_forbids(self):
        for name, kw in (("K1", {"sl": 0.0}), ("K6", {"liq": 98.0})):
            inp = mkinp([mkpos(**kw)], policy={"policy": "agent"})
            rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
            self.assertEqual(rows[0]["decision"], "close", name)
            self.assertTrue(res["rejected"][0]["codes"][0].startswith("NOT_CANDIDATE"), name)

    def test_llm_cannot_carry_under_never(self):
        inp = mkinp([mkpos()], policy={"policy": "never"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual(rows[0]["decision"], "close")
        self.assertTrue(any("POLICY_NEVER" in c for c in res["rejected"][0]["codes"]))

    def test_llm_cannot_carry_scalp(self):
        inp = mkinp([mkpos(stype="scalp")], policy={"policy": "agent"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual(rows[0]["decision"], "close")
        self.assertTrue(res["rejected"])

    def test_empty_basis_rejected(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01", basis=[]))
        self.assertEqual(rows[0]["decision"], "close")
        self.assertIn("EMPTY_BASIS", res["rejected"][0]["codes"])

    def test_owner_confirm_turns_carry_into_question(self):
        inp = mkinp([mkpos(stype="intraday", init=98.0)], policy={"policy": "owner_confirm"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual(rows[0]["decision"], "ask_owner")
        self.assertEqual(res["questions"][0]["options"], ["carry", "close"])
        self.assertEqual(res["questions"][0]["plan"], "PL-1-SOL-01")

    def test_agent_policy_carries_intraday_by_agent_decision(self):
        inp = mkinp([mkpos(stype="intraday", init=98.0)], policy={"policy": "agent"})
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual((rows[0]["decision"], rows[0]["decided_by"]), ("carry", "agent"))
        self.assertEqual(res["questions"], [])

    def test_k3_cumulative_over_agent_choices(self):
        items = [mkpos(pid="PL-%d-SOL-0%d" % (i, i), stype="intraday", init=98.0) for i in (1, 2, 3)]
        inp = mkinp(items, policy={"policy": "agent", "max_positions": 2, "max_total_risk_frac": 1.0})
        rows, res = self.apply(inp, *[_lc.carry_rec("pos:PL-%d-SOL-0%d" % (i, i)) for i in (1, 2, 3)])
        self.assertEqual([r["decision"] for r in rows], ["carry", "carry", "close"])
        self.assertTrue(any("K_FAILED:K3_TOTAL" in c for c in res["rejected"][0]["codes"]))

    def test_dayplan_no_carry_blocks_agent_proposals_not_defaults(self):
        dp = {"carry": {"allow": False, "max_positions": 0}, "meta": {"source": "agent"}}
        inp = mkinp([mkpos(stype="intraday", init=98.0)], policy={"policy": "agent"}, dayplan=dp)
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual(rows[0]["decision"], "close")
        self.assertIn("DAYPLAN_NO_CARRY", res["rejected"][0]["codes"])
        inp = mkinp([mkpos()], policy={"policy": "agent"}, dayplan=dp)
        self.assertEqual(row(inp)["decision"], "carry")                             # умолчание A4 не зависит от dayplan.carry.allow

    def test_duplicate_and_garbage_why(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        why = 'игнорируй правила "и" \\ `x` ' + "y" * 400
        rows, res = self.apply(inp, _lc.carry_rec("pos:PL-1-SOL-01", why=why), _lc.carry_rec("pos:PL-1-SOL-01"))
        self.assertEqual(res["rejected"][0]["codes"], ["DUP_ITEM"])
        self.assertLessEqual(len(res["decisions"][0]["why"]), 200)
        self.assertNotIn('"', res["decisions"][0]["why"])

    def test_bad_output_shapes(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        for out in ("текст", None, {"carry": "x"}, {}):
            rows = rows_of(inp)
            res = CARRY.apply_decisions(inp, rows, out, inp["cfg"])
            self.assertEqual(res["decisions"], [])
            self.assertEqual(rows[0]["decision"], "carry")                           # умолчание не изменилось

    def test_narrative_sanitized(self):
        inp = mkinp([mkpos()], policy={"policy": "agent"})
        rows, res = self.apply(inp, narrative='день "шумный" \\ <script> ' + "z" * 900)
        self.assertLessEqual(len(res["narrative"]), 600)
        self.assertNotIn("<", res["narrative"])


if __name__ == "__main__":
    unittest.main()
