"""Тесты core/plan_schema.py (WP2): нормализация, валидаторы (static/context/amend), merge_patch, diff, canon_hash, json_schema, describe_cond.

Примеры плана из docs/spec_v2/01_plan.md п. 1.9 и 1.10 вставлены как фикстуры. Тесты чистые: без файлов, сети и часов.
"""
import copy
import json
import math
import unittest

import _bootstrap  # noqa: F401
from tb.core import plan_schema as P

SCALP_JSON = r"""{
  "schema": "tb.plan/1",
  "symbol": "SOL", "side": "short", "mode": "live", "session_type": "scalp",
  "source": {"kind": "idea", "idea_id": "I7", "event_seq": 1842},
  "thesis": "Прокол сопротивления 142.5 и закрытие 5м обратно под уровень на повышенном объёме; цель откат к 140.2",
  "tags": {"experiment": "sweep-reject-v1", "variant": "A", "setup": "level_reject", "hypothesis": "H3"},
  "entry": {
    "when": {"all": [
      {"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "short", "wick_min_pct": 0.05},
      {"kind": "volume_spike", "tf": "5m", "mult": 1.8, "lookback": 20, "on": "close", "dir": "any"},
      {"kind": "spread_bps", "lte": 6}
    ]},
    "how": {"type": "market"},
    "ref_price": 142.3,
    "valid": {"ttl_min": 12, "cancel_above": 143.4, "cancel_below": 140.0, "max_chase_bps": 12}
  },
  "stop": {"price": 143.0, "trigger_by": "last"},
  "targets": [
    {"id": "t1", "price": 141.2, "frac": 0.5, "exec": "resting"},
    {"id": "t2", "price": 140.2, "frac": 0.5, "exec": "resting"}
  ],
  "risk": {"score": 0.7, "size_mult": 1.0},
  "manage": [
    {"id": "be_1r",   "when": {"kind": "pnl_r", "gte": 1.0},              "do": {"action": "move_stop", "to": "breakeven"}, "once": true},
    {"id": "be_t1",   "when": {"kind": "target_filled", "target": "t1"},  "do": {"action": "move_stop", "to": "lock_r", "r": 0.3}, "once": true},
    {"id": "trail",   "when": {"kind": "pnl_r", "gte": 1.3},              "do": {"action": "trail", "mode": "candle_extreme", "tf": "1m", "n": 3, "buffer_bps": 4}},
    {"id": "stale",   "when": {"all": [{"kind": "elapsed", "min": 10}, {"kind": "pnl_r", "lt": 0.3}]}, "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": true},
    {"id": "wake_m05","when": {"kind": "pnl_r", "lte": -0.5},             "do": {"action": "notify", "code": "R_MINUS"}, "once": true}
  ],
  "invalidation": [ {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 143.3, "n": 1} ],
  "lifecycle": {"ttl_min": 12, "max_hold_min": 15, "flatten_at_session_end": true}
}
"""

INTRADAY_JSON = r"""{
  "schema": "tb.plan/1",
  "symbol": "SOL", "side": "long", "mode": "live", "session_type": "intraday",
  "source": {"kind": "brain"},
  "thesis": "Снятие стопов под 144 и возврат выше 145 на 5м после дневного восходящего тренда; держим до 153",
  "tags": {"experiment": "sweep-reclaim-v1", "variant": "B", "setup": "reclaim", "hypothesis": "H5", "extra": ["trend-up-1h"]},
  "entry": {
    "when": {"kind": "seq", "within_min": 90, "steps": [
      {"kind": "price_beyond", "dir": "below", "level": 144.0},
      {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 145.0, "n": 1}
    ]},
    "how": {"type": "market"},
    "ref_price": 145.6,
    "valid": {"ttl_min": 180, "cancel_above": 150.0, "cancel_below": 142.0, "max_chase_bps": 20}
  },
  "stop": {"price": 143.2, "trigger_by": "last"},
  "targets": [
    {"id": "t1", "price": 148.6, "frac": 0.5, "exec": "resting"},
    {"id": "t2", "price": 153.0, "frac": 0.5, "exec": "resting"}
  ],
  "risk": {"score": 0.6, "size_mult": 1.0},
  "manage": [
    {"id": "be_1r",   "when": {"kind": "pnl_r", "gte": 1.0},             "do": {"action": "move_stop", "to": "breakeven"}, "once": true},
    {"id": "trail",   "when": {"kind": "target_filled", "target": "t1"}, "do": {"action": "trail", "mode": "atr", "tf": "15m", "n": 14, "mult": 2.0, "buffer_bps": 5}},
    {"id": "pyr",     "when": {"all": [{"kind": "pnl_r", "gte": 1.5}, {"kind": "candle_close", "tf": "15m", "dir": "above", "level": 148.0, "n": 1}]},
                      "do": {"action": "add", "risk_frac": 0.5, "how": "market"}, "once": true},
    {"id": "tmax",    "when": {"kind": "elapsed", "min": 600},           "do": {"action": "close", "frac": 1.0, "reason": "timeout"}, "once": true},
    {"id": "wake_t1", "when": {"kind": "near_target", "target": "t1", "frac": 0.8}, "do": {"action": "notify", "code": "NEAR_TARGET"}, "once": true}
  ],
  "invalidation": [
    {"kind": "candle_close", "tf": "15m", "dir": "below", "level": 144.6, "n": 1}
  ],
  "lifecycle": {"ttl_min": 180, "max_hold_min": 720, "flatten_at_session_end": false}
}
"""

PROFILE = {
    "taker_fee_pct": 0.0009,
    "costs": {"bybit": {"fee": 0.001, "slip": 0.0002, "maker_fee": 0.00036}},
    "session_types": {
        "scalp": {"max_stop_pct": 0.8, "min_rr": 1.0},
        "intraday": {"max_stop_pct": 3.0, "min_rr": 1.5},
    },
}
LIMITS = {
    "pyramid": {"enabled": False, "min_r": 1.0, "max_adds": 1, "max_add_risk_frac": 1.0},
    "entry": {"max_level_distance_pct": 25, "no_confirm_max_size_mult": 0.5, "max_chase_bps": 15},
    "protections": {"max_plans_active": 6, "max_submits_per_hour": 20},
    "connectivity": {"public_price_stale_s": 10},
    "paper": {"max_active": 20},
}


def scalp():
    return json.loads(SCALP_JSON)


def intraday():
    return json.loads(INTRADAY_JSON)


def norm(raw, **kw):
    p, iss = P.normalize(raw, **kw)
    return p


def codes(issues, severity=None):
    return sorted(set(i["code"] for i in issues if severity is None or i["severity"] == severity))


def errs(plan):
    """Коды ошибок validate_static после normalize."""
    p, iss = P.normalize(plan)
    return codes(iss + P.validate_static(p), "error")


def ctx(**kw):
    c = {"now": 1_000_000.0, "limits": LIMITS, "session": {"type": "scalp", "expires": 1_000_000.0 + 3600},
         "instrument": {"tick": "0.01", "qty_step": "0.1", "min_qty": "0.1", "min_notional": "5"}, "price": 142.3,
         "profile": PROFILE, "active_plans": [], "cooldowns": {}}
    c.update(kw)
    return c


def ok_scalp_plan():
    return norm(scalp())


# ---------------------------------------------------------------------------------------------------------------------
class TestExamples(unittest.TestCase):
    def test_scalp_valid(self):
        p, iss = P.normalize(scalp())
        self.assertEqual(iss, [])
        self.assertEqual(P.validate_static(p), [])

    def test_intraday_valid(self):
        p, iss = P.normalize(intraday())
        self.assertEqual(iss, [])
        self.assertEqual(P.validate_static(p), [])

    def test_scalp_context_ok(self):
        self.assertEqual(P.validate_context(ok_scalp_plan(), ctx()), [])

    def test_intraday_context_warns_add_disabled(self):
        c = ctx(session={"type": "intraday", "expires": 1_000_000.0 + 86400}, price=145.6)
        iss = P.validate_context(norm(intraday()), c)
        self.assertEqual(codes(iss, "error"), [])
        self.assertEqual(codes(iss, "warn"), ["W_ADD_DISABLED"])

    def test_intraday_context_pyramid_enabled_no_warn(self):
        lim = copy.deepcopy(LIMITS)
        lim["pyramid"]["enabled"] = True
        iss = P.validate_context(norm(intraday()), ctx(session={"type": "intraday", "expires": 1_000_000.0 + 86400}, price=145.6, limits=lim))
        self.assertEqual(iss, [])

    def test_rr_numbers_from_spec(self):
        pv = P.rr_preview(norm(scalp()), 142.3, PROFILE)
        self.assertAlmostEqual(pv["cost_pct"], 0.24, places=6)
        self.assertAlmostEqual(pv["net_rr"], 1.20, delta=0.02)
        pv = P.rr_preview(norm(intraday()), 145.6, PROFILE)
        self.assertAlmostEqual(pv["net_rr"], 1.76, delta=0.02)

    def test_intraday_with_old_targets_rejected_by_rr(self):
        raw = intraday()
        raw["targets"][0]["price"] = 148.0
        raw["targets"][1]["price"] = 151.5
        iss = P.validate_context(norm(raw), ctx(session={"type": "intraday", "expires": 1_000_000.0 + 86400}, price=145.6))
        self.assertIn("E_RR", codes(iss))

    def test_examples_do_not_mutate_input(self):
        raw = scalp()
        before = copy.deepcopy(raw)
        P.normalize(raw)
        self.assertEqual(raw, before)

    def test_describe_examples(self):
        p = norm(scalp())
        self.assertIn("прокол 142.5", P.describe_cond(p["entry"]["when"]))
        self.assertIn("затем", P.describe_cond(norm(intraday())["entry"]["when"]))


class TestNormalize(unittest.TestCase):
    def test_defaults(self):
        p = norm({"schema": "tb.plan/1", "symbol": "sol", "side": "Long", "thesis": "x" * 12})
        self.assertEqual((p["symbol"], p["side"], p["mode"]), ("SOL", "long", "live"))
        self.assertEqual((p["tags"], p["manage"], p["invalidation"], p["shadow_variants"]), ({}, [], [], []))
        self.assertEqual(p["risk"], {"score": 0.5, "size_mult": 1.0, "confirm_waiver": False})

    def test_stop_and_target_defaults(self):
        raw = scalp()
        del raw["stop"]["trigger_by"]
        del raw["targets"][0]["exec"]
        p = norm(raw)
        self.assertEqual(p["stop"]["trigger_by"], "last")
        self.assertEqual(p["targets"][0]["exec"], "resting")

    def test_cond_defaults(self):
        p = norm({"entry": {"when": {"kind": "volume_spike", "tf": "5M", "mult": "2"}}})
        c = p["entry"]["when"]
        self.assertEqual((c["tf"], c["mult"], c["lookback"], c["on"], c["dir"]), ("5m", 2.0, 20, "close", "any"))
        c = norm({"entry": {"when": {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 1}}})["entry"]["when"]
        self.assertEqual((c["n"], c["level"]), (1, 1.0))
        c = norm({"entry": {"when": {"kind": "price_beyond", "dir": "above", "level": 1}}})["entry"]["when"]
        self.assertIs(c["allow_already"], False)

    def test_flatten_default_depends_on_session_type(self):
        self.assertIs(norm({"session_type": "scalp"})["lifecycle"]["flatten_at_session_end"], True)
        self.assertIs(norm({"session_type": "intraday"})["lifecycle"]["flatten_at_session_end"], False)
        self.assertIs(norm({"session_type": "scalp", "lifecycle": {"flatten_at_session_end": False}})["lifecycle"]["flatten_at_session_end"], False)

    def test_paper_default_session_type(self):
        self.assertEqual(norm({"mode": "paper"})["session_type"], "intraday")
        self.assertNotIn("session_type", norm({"mode": "live"}))

    def test_default_ttl_only_when_not_set(self):
        self.assertEqual(norm({"session_type": "scalp"})["lifecycle"]["ttl_min"], 15)
        self.assertNotIn("ttl_min", norm({"session_type": "scalp", "entry": {"valid": {"ttl_min": 5}}})["lifecycle"])
        self.assertEqual(norm({"session_type": "scalp", "lifecycle": {"ttl_min": 7}})["lifecycle"]["ttl_min"], 7)

    def test_limit_defaults(self):
        p = norm({"entry": {"how": {"type": "limit", "price": 10}}})
        self.assertEqual((p["entry"]["how"]["post_only"], p["entry"]["how"]["snap"]), (True, False))

    def test_once_sets_max_times(self):
        p = norm({"manage": [{"id": "ab", "when": {"kind": "elapsed", "min": 1}, "do": {"action": "notify", "code": "time"}, "once": True}]})
        self.assertEqual(p["manage"][0]["max_times"], 1)
        self.assertEqual(p["manage"][0]["do"]["code"], "TIME")

    def test_action_defaults(self):
        p = norm({"manage": [{"id": "ab", "when": {"kind": "elapsed", "min": 1}, "do": {"action": "close"}}]})
        self.assertEqual(p["manage"][0]["do"], {"action": "close", "frac": 1.0, "reason": "rule"})
        self.assertEqual(p["manage"][0]["cooldown_s"], 0)

    def test_symbol_usdt_stripped(self):
        self.assertEqual(norm({"symbol": "solusdt"})["symbol"], "SOL")
        self.assertEqual(norm({"symbol": "USDT"})["symbol"], "USDT")

    def test_executor_fields_ignored_with_warning(self):
        raw = scalp()
        raw.update({"id": "PL-1028-SOL-01", "rev": 3, "created_by": "owner", "guard_preview": {"ok": True}, "prereg_hash": "0" * 16})
        p, iss = P.normalize(raw)
        self.assertEqual(codes(iss), ["W_FIELD_IGNORED"])
        self.assertEqual(len(iss), 5)
        for k in P.EXEC_FIELDS:
            self.assertNotIn(k, p)

    def test_keep_executor(self):
        raw = scalp()
        raw.update({"id": "PL-1028-SOL-01", "rev": 3})
        p, iss = P.normalize(raw, keep_executor=True)
        self.assertEqual(iss, [])
        self.assertEqual((p["id"], p["rev"]), ("PL-1028-SOL-01", 3))
        self.assertEqual(P.validate_static(p), [])

    def test_strings_cleaned(self):
        p = norm({"thesis": "  привет‮ мир\u0000 тест\n  "})
        self.assertNotIn("‮", p["thesis"])
        self.assertNotIn("\x00", p["thesis"])
        self.assertEqual(p["thesis"], p["thesis"].strip())
        self.assertNotIn("\n", p["thesis"])

    def test_numeric_coercion(self):
        p = norm({"stop": {"price": "143"}, "targets": [{"id": "t1", "price": 141, "frac": 1}]})
        self.assertEqual(p["stop"]["price"], 143.0)
        self.assertIsInstance(p["targets"][0]["price"], float)
        self.assertIs(norm({"stop": {"price": True}})["stop"]["price"], True)   # bool не превращается в число

    def test_non_dict_and_deep(self):
        for raw in (None, [], "x", 5):
            p, iss = P.normalize(raw)
            self.assertEqual((p, codes(iss)), ({}, ["E_TYPE"]))
        deep = cur = {}
        for _ in range(1000):
            cur["a"] = {}
            cur = cur["a"]
        p, iss = P.normalize(deep)
        self.assertEqual(codes(iss), ["E_TOO_LARGE"])

    def test_idempotent(self):
        p = norm(scalp())
        self.assertEqual(norm(p), p)


class TestStaticStructure(unittest.TestCase):
    def mod(self, fn, base=scalp):
        raw = base()
        fn(raw)
        return errs(raw)

    def test_e_schema_missing_required(self):
        self.assertIn("E_SCHEMA", self.mod(lambda r: r.pop("thesis")))
        self.assertIn("E_SCHEMA", self.mod(lambda r: r.pop("stop")))
        self.assertIn("E_SCHEMA", self.mod(lambda r: r.pop("targets")))
        self.assertIn("E_SCHEMA", self.mod(lambda r: r.pop("source")))

    def test_e_schema_live_needs_session_type(self):
        self.assertIn("E_SCHEMA", self.mod(lambda r: r.pop("session_type")))

    def test_e_schema_wrong_version(self):
        self.assertIn("E_RANGE", self.mod(lambda r: r.update(schema="tb.plan/2")))

    def test_e_unknown_field(self):
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r.update(typo=1)))
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r["entry"].update(whenn={})))
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r["entry"]["when"]["all"][0].update(wick=1)))
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r["entry"]["when"]["all"][0].update(kind="candle_rejekt")))
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r["manage"][0]["do"].update(action="teleport")))
        self.assertIn("E_UNKNOWN_FIELD", self.mod(lambda r: r["manage"][0]["do"].update(foo=1)))

    def test_e_type(self):
        self.assertIn("E_TYPE", self.mod(lambda r: r.update(thesis=5)))
        self.assertIn("E_TYPE", self.mod(lambda r: r["stop"].update(price=True)))
        self.assertIn("E_TYPE", self.mod(lambda r: r.update(targets={})))
        self.assertIn("E_TYPE", self.mod(lambda r: r.update(entry=[])))
        self.assertIn("E_TYPE", self.mod(lambda r: r["risk"].update(confirm_waiver="yes")))
        self.assertIn("E_TYPE", self.mod(lambda r: r["entry"]["when"]["all"][0].update(tf=5)))
        self.assertIn("E_TYPE", self.mod(lambda r: r["lifecycle"].update(ttl_min=1.5)))
        self.assertIn("E_TYPE", validate_raw({"schema": 1, "side": 5}))

    def test_e_range(self):
        for fn in (lambda r: r["risk"].update(score=1.5), lambda r: r["risk"].update(size_mult=0),
                   lambda r: r["risk"].update(size_mult=1.5), lambda r: r.update(side="sideways"),
                   lambda r: r["stop"].update(price=-1), lambda r: r["stop"].update(price=0),
                   lambda r: r["lifecycle"].update(ttl_min=0), lambda r: r["lifecycle"].update(ttl_min=1441),
                   lambda r: r["lifecycle"].update(max_hold_min=43201), lambda r: r.update(thesis="коротко"),
                   lambda r: r["entry"]["when"]["all"][0].update(tf="2m"),
                   lambda r: r["entry"]["when"]["all"][1].update(lookback=4),
                   lambda r: r["entry"]["valid"].update(max_chase_bps=0),
                   lambda r: r["targets"][0].update(frac=0), lambda r: r["targets"][0].update(frac=1.5),
                   lambda r: r["manage"][0].update(max_times=0),
                   lambda r: r["manage"][0]["do"].update(to="sideways")):
            raw = scalp()
            fn(raw)
            self.assertIn("E_RANGE", errs(raw))

    def test_e_too_large(self):
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(thesis="x" * 281)))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r["tags"].update(extra=["aa"] * 6)))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(targets=[dict(id="t%d" % (i + 1), price=140 - i, frac=0.2) for i in range(5)])))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(invalidation=[r["invalidation"][0]] * 5)))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(manage=[dict(r["manage"][0], id="r%d" % i) for i in range(13)])))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(shadow_variants=[{"name": "v%d" % i, "patch": {}} for i in range(4)])))
        self.assertIn("E_TOO_LARGE", self.mod(lambda r: r.update(tags={"extra": ["ab"] * 6})))

    def test_e_too_large_bytes(self):
        raw = scalp()
        raw["shadow_variants"] = [{"name": "v%d" % i, "patch": {"entry": {"x": "y" * 270}}} for i in range(3)]
        raw["shadow_variants"][0]["patch"] = {"manage": [{"s": "z" * 200}] * 80}
        self.assertIn("E_TOO_LARGE", errs(raw))

    def test_e_id(self):
        self.assertIn("E_ID", self.mod(lambda r: r["source"].update(parent_plan="PL-1-SOL-1")))
        raw = scalp()
        raw["manage"][0]["id"] = "Bad Id"
        self.assertIn("E_ID", errs(raw))
        raw = scalp()
        raw["targets"][0]["id"] = "t9"
        self.assertIn("E_ID", errs(raw))
        raw = scalp()
        raw["source"]["idea_id"] = "7"
        self.assertIn("E_ID", errs(raw))
        raw = scalp()
        raw["manage"][1]["when"]["target"] = "t3"
        self.assertIn("E_ID", errs(raw))
        raw = scalp()
        raw["manage"].append({"id": "mt", "when": {"kind": "elapsed", "min": 1}, "do": {"action": "move_target", "target": "t4", "price": 140.0}})
        self.assertIn("E_ID", errs(raw))

    def test_e_id_plan_id_with_keep_executor(self):
        raw = scalp()
        raw["id"] = "PL-1028-sol-01"
        p, _ = P.normalize(raw, keep_executor=True)
        self.assertIn("E_ID", codes(P.validate_static(p)))

    def test_e_dup_id(self):
        raw = scalp()
        raw["manage"][1]["id"] = raw["manage"][0]["id"]
        self.assertIn("E_DUP_ID", errs(raw))
        raw = scalp()
        raw["targets"][1]["id"] = "t1"
        self.assertIn("E_DUP_ID", errs(raw))
        raw = scalp()
        raw["shadow_variants"] = [{"name": "aa", "patch": {}}, {"name": "aa", "patch": {}}]
        self.assertIn("E_DUP_ID", errs(raw))

    def test_e_nonfinite(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            raw = scalp()
            raw["stop"]["price"] = bad
            self.assertIn("E_NONFINITE", errs(raw))
        raw = json.loads(SCALP_JSON.replace('"price": 143.0', '"price": 1e999'))
        self.assertIn("E_NONFINITE", errs(raw))

    def test_huge_numbers(self):
        raw = scalp()
        raw["stop"]["price"] = 10 ** 400
        self.assertIn("E_RANGE", errs(raw))
        raw["stop"]["price"] = 1e300
        self.assertIn("E_RANGE", errs(raw))

    def test_bad_string_chars_after_normalize_clean(self):
        raw = scalp()
        raw["thesis"] = "Нормальный тезис ‮ с RTL и \x07 звонком"
        p, _ = P.normalize(raw)
        self.assertEqual(P.validate_static(p), [])
        p["thesis"] = "тезис с ‮ RTL символом ok"
        self.assertIn("E_TYPE", codes(P.validate_static(p)))

    def test_garbage_inputs(self):
        for g in (None, 5, "x", [], [1, 2], True):
            self.assertEqual(codes(P.validate_static(g)), ["E_TYPE"])
        deep = cur = {}
        for _ in range(1000):
            cur["a"] = {}
            cur = cur["a"]
        self.assertEqual(codes(P.validate_static(deep)), ["E_TOO_LARGE"])
        self.assertEqual(codes(P.validate_static({"a": {1, 2}})), ["E_TYPE"])

    def test_garbage_in_nested_slots_do_not_raise(self):
        for slot in ("entry", "stop", "targets", "risk", "manage", "invalidation", "lifecycle", "tags", "source", "shadow_variants"):
            for val in (None, 5, "x", [], {}, [None], [[]], {"a": None}):
                raw = scalp()
                raw[slot] = val
                p, _ = P.normalize(raw)
                P.validate_static(p)
                P.validate_context(p, ctx())
                P.describe_cond(p.get("entry", {}) and p["entry"].get("when") if isinstance(p.get("entry"), dict) else None)


def validate_raw(raw):
    p, iss = P.normalize(raw)
    return codes(iss + P.validate_static(p), "error")


class TestMeaning(unittest.TestCase):
    def e(self, fn, base=scalp):
        raw = base()
        fn(raw)
        return errs(raw)

    def test_stop_side(self):
        self.assertIn("E_STOP_SIDE", self.e(lambda r: r["stop"].update(price=141.0)))                 # short: стоп ниже входа
        self.assertIn("E_STOP_SIDE", self.e(lambda r: r["stop"].update(price=146.0), intraday))      # long: стоп выше входа
        self.assertIn("E_STOP_SIDE", self.e(lambda r: r["entry"].update(ref_price=143.0)))           # ref на стопе

    def test_target_side(self):
        self.assertIn("E_TARGET_SIDE", self.e(lambda r: r["targets"][0].update(price=143.5)))
        raw = scalp()
        del raw["entry"]["ref_price"]
        raw["targets"][0]["price"] = 143.5   # без ref сравнение со стопом
        self.assertIn("E_TARGET_SIDE", errs(raw))

    def test_target_order(self):
        self.assertIn("E_TARGET_ORDER", self.e(lambda r: r["targets"][1].update(price=141.5)))       # для short цены должны убывать
        self.assertIn("E_TARGET_ORDER", self.e(lambda r: r["targets"][1].update(price=141.2)))       # равные
        self.assertIn("E_TARGET_ORDER", self.e(lambda r: r["targets"][0].update(id="t2")))
        raw = intraday()
        raw["targets"][1]["price"] = 147.0   # long: вторая цель ближе первой
        self.assertIn("E_TARGET_ORDER", errs(raw))
        raw = scalp()
        raw["targets"][1]["id"] = "t3"       # пропуск t2
        self.assertIn("E_TARGET_ORDER", errs(raw))

    def test_frac_sum(self):
        self.assertIn("E_FRAC_SUM", self.e(lambda r: r["targets"][0].update(frac=0.6)))
        self.assertNotIn("E_FRAC_SUM", self.e(lambda r: r["targets"][0].update(frac=0.4)))   # остаток = бегун

    def test_cancel_levels(self):
        self.assertIn("E_CANCEL_LEVELS", self.e(lambda r: r["entry"]["valid"].update(cancel_above=142.0)))
        self.assertIn("E_CANCEL_LEVELS", self.e(lambda r: r["entry"]["valid"].update(cancel_below=143.0)))
        self.assertIn("E_CANCEL_LEVELS", self.e(lambda r: r["entry"]["valid"].update(cancel_below=144.0, cancel_above=143.5)))

    def test_ttl_valid_vs_lifecycle(self):
        self.assertIn("E_TTL", self.e(lambda r: r["entry"]["valid"].update(ttl_min=30)))

    def test_no_confirm_static(self):
        def fn(r):
            r["entry"]["when"] = {"kind": "spread_bps", "lte": 5}
        self.assertIn("E_NO_CONFIRM", self.e(fn))

        def ok(r):
            fn(r)
            r["risk"].update(confirm_waiver=True, size_mult=0.5)
        self.assertNotIn("E_NO_CONFIRM", self.e(ok))

        def nowhen(r):
            del r["entry"]["when"]
        self.assertIn("E_NO_CONFIRM", self.e(nowhen))
        # any: подтверждение должно быть в каждой ветке
        def anyfn(r):
            r["entry"]["when"] = {"any": [{"kind": "candle_close", "tf": "5m", "dir": "below", "level": 142.0, "n": 1}, {"kind": "price_beyond", "dir": "below", "level": 142}]}
        self.assertIn("E_NO_CONFIRM", self.e(anyfn))

    def test_has_confirm(self):
        self.assertFalse(P.has_confirm(None))
        self.assertFalse(P.has_confirm({"not": {"kind": "candle_body", "tf": "5m", "dir": "up", "min_body_pct": 1}}))
        self.assertTrue(P.has_confirm({"kind": "seq", "within_min": 5, "steps": [{"kind": "price_beyond", "dir": "above", "level": 1},
                                                                                 {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 1}]}))

    def test_adopt_rules(self):
        def adopt(r):
            r["entry"] = {"how": {"type": "adopt"}}
            r["source"]["kind"] = "adopt"
        self.assertEqual(self.e(adopt), [])

        def adopt_when(r):
            adopt(r)
            r["entry"]["when"] = {"kind": "elapsed", "min": 1}
        self.assertIn("E_SCHEMA", self.e(adopt_when))

    def test_how_rules(self):
        self.assertIn("E_SCHEMA", self.e(lambda r: r["entry"]["how"].update(type="limit")))   # limit без цены
        self.assertIn("E_SCHEMA", self.e(lambda r: r["entry"]["how"].update(price=142.4)))    # market с ценой

    def test_limit_price_is_reference(self):
        def fn(r):
            r["entry"]["how"] = {"type": "limit", "price": 143.5}   # лимит-шорт выше стопа 143.0
        self.assertIn("E_STOP_SIDE", self.e(fn))

    def test_cond_depth(self):
        leaf = {"kind": "spread_bps", "lte": 5}
        c = leaf
        for key in ("all", "any", "all", "any"):
            c = {key: [c]}
        raw = scalp()
        raw["entry"]["when"] = c
        self.assertIn("E_COND_DEPTH", errs(raw))
        c = {"all": [{"any": [{"not": leaf}]}]}   # глубина 3 допустима
        raw["entry"]["when"] = c
        self.assertNotIn("E_COND_DEPTH", errs(raw))
        c = {"all": [{"any": [{"all": [leaf]}]}]}
        raw["entry"]["when"] = c
        self.assertNotIn("E_COND_DEPTH", errs(raw))

    def test_cond_leaves_limit(self):
        raw = scalp()
        raw["entry"]["when"] = {"all": [{"kind": "spread_bps", "lte": 5 + i} for i in range(9)]}
        self.assertIn("E_TOO_LARGE", errs(raw))

    def test_cond_kind_forbidden(self):
        raw = scalp()
        raw["entry"]["when"] = {"kind": "pnl_r", "gte": 1}
        self.assertIn("E_COND_KIND_FORBIDDEN", errs(raw))
        raw = scalp()
        raw["manage"][0]["when"] = {"kind": "spread_bps", "lte": 5}
        self.assertIn("E_COND_KIND_FORBIDDEN", errs(raw))
        raw = scalp()
        raw["invalidation"] = [{"kind": "candle_body", "tf": "5m", "dir": "up", "min_body_pct": 1}]
        self.assertIn("E_COND_KIND_FORBIDDEN", errs(raw))
        raw = scalp()
        raw["manage"][0]["when"] = {"kind": "seq", "within_min": 5, "steps": [{"kind": "elapsed", "min": 1}]}
        self.assertIn("E_COND_KIND_FORBIDDEN", errs(raw))
        raw = intraday()
        raw["entry"]["when"]["steps"].append({"kind": "seq", "within_min": 5, "steps": []})
        self.assertTrue(errs(raw))

    def test_cond_shape_errors(self):
        for c in ({"all": []}, {"all": [], "any": []}, {"not": {"all": [{"kind": "spread_bps", "lte": 1}]}}, {"kind": "pnl_r"},
                  {"kind": "pnl_r", "gte": 1, "lt": 2}, {"kind": "price_in_zone", "low": 5, "high": 4}, {"nokind": 1}, {"all": "x"}):
            raw = scalp()
            raw["manage"][0]["when"] = c
            self.assertTrue(errs(raw), c)

    def test_seq_shape(self):
        raw = intraday()
        raw["entry"]["when"]["within_min"] = 0
        self.assertIn("E_RANGE", errs(raw))
        raw = intraday()
        del raw["entry"]["when"]["within_min"]
        self.assertIn("E_SCHEMA", errs(raw))

    def test_stop_widen_in_rule(self):
        def fn(r):
            r["manage"][0]["do"] = {"action": "move_stop", "to": "price", "price": 143.5}   # short: стоп выше 143.0 = шире
        self.assertIn("E_STOP_WIDEN", self.e(fn))

        def good(r):
            r["manage"][0]["do"] = {"action": "move_stop", "to": "price", "price": 142.0}
        self.assertEqual(self.e(good), [])

    def test_stop_widen_trap_long(self):
        raw = intraday()
        raw["manage"][0]["do"] = {"action": "move_stop", "to": "price", "price": 140.0}
        self.assertIn("E_STOP_WIDEN", errs(raw))

    def test_move_stop_params(self):
        for do in ({"action": "move_stop", "to": "price"}, {"action": "move_stop", "to": "lock_r"}, {"action": "move_stop", "to": "breakeven", "price": 1},
                   {"action": "move_stop", "to": "breakeven", "r": 0.3}, {"action": "move_stop", "to": "lock_r", "r": 0.3, "offset_bps": 2},
                   {"action": "move_stop", "to": "lock_r", "r": -0.5}):
            raw = scalp()
            raw["manage"][0]["do"] = do
            self.assertTrue(errs(raw), do)

    def test_trail_params(self):
        for do in ({"action": "trail", "mode": "atr", "tf": "5m", "n": 14}, {"action": "trail", "mode": "percent"},
                   {"action": "trail", "mode": "step_r", "step_r": 0.5, "tf": "5m"}, {"action": "trail", "mode": "candle_extreme", "tf": "1m"}):
            raw = scalp()
            raw["manage"][2]["do"] = do
            self.assertTrue(errs(raw), do)
        for do in ({"action": "trail", "mode": "percent", "pct": 1.0}, {"action": "trail", "mode": "step_r", "step_r": 0.5, "min_step_ticks": 3},
                   {"action": "trail", "mode": "atr", "tf": "15m", "n": 14, "mult": 2.0}):
            raw = scalp()
            raw["manage"][2]["do"] = do
            self.assertEqual(errs(raw), [], do)

    def test_partial_and_close_params(self):
        raw = scalp()
        raw["manage"][3]["do"] = {"action": "partial_close", "exec": "market"}
        self.assertIn("E_SCHEMA", errs(raw))
        raw["manage"][3]["do"] = {"action": "partial_close", "frac_of_remaining": 0.5, "frac_of_initial": 0.5}
        self.assertIn("E_SCHEMA", errs(raw))
        raw["manage"][3]["do"] = {"action": "partial_close", "frac_of_initial": 0.5}
        self.assertEqual(errs(raw), [])
        raw["manage"][3]["do"] = {"action": "close", "reason": "nonsense"}
        self.assertIn("E_RANGE", errs(raw))

    def test_notify_code(self):
        raw = scalp()
        raw["manage"][4]["do"]["code"] = "NOPE"
        self.assertIn("E_RANGE", errs(raw))

    def test_once_max_times(self):
        raw = scalp()
        raw["manage"][0]["max_times"] = 3
        self.assertIn("E_RANGE", errs(raw))

    def test_add_gating(self):
        raw = intraday()
        raw["manage"][2]["when"] = {"kind": "candle_close", "tf": "15m", "dir": "above", "level": 148.0, "n": 1}
        self.assertIn("E_ADD_NOT_PROFIT_GATED", errs(raw))
        raw["manage"][2]["when"] = {"all": [{"kind": "pnl_r", "lte": 3}]}
        self.assertIn("E_ADD_NOT_PROFIT_GATED", errs(raw))
        raw["manage"][2]["when"] = {"any": [{"kind": "pnl_r", "gte": 2}, {"kind": "elapsed", "min": 5}]}
        self.assertIn("E_ADD_NOT_PROFIT_GATED", errs(raw))        # any не гарантирует прибыль
        raw["manage"][2]["when"] = {"kind": "pnl_r", "gte": 0}
        self.assertIn("E_ADD_NOT_PROFIT_GATED", errs(raw))
        raw["manage"][2]["when"] = {"kind": "pnl_r", "gte": 1.2}
        self.assertEqual(errs(raw), [])

    def test_add_risk_static(self):
        raw = intraday()
        raw["manage"][2]["do"]["risk_frac"] = 1.5
        self.assertTrue(set(errs(raw)) & {"E_ADD_RISK", "E_RANGE"})

    def test_warn_no_invalidation(self):
        raw = scalp()
        raw["invalidation"] = []
        p, _ = P.normalize(raw)
        self.assertEqual(codes(P.validate_static(p), "warn"), ["W_NO_INVALIDATION"])

    def test_warn_no_runner_rule(self):
        raw = scalp()
        raw["targets"][1]["frac"] = 0.25
        raw["manage"] = [m for m in raw["manage"] if m["do"]["action"] != "trail"]
        del raw["lifecycle"]["max_hold_min"]
        p, _ = P.normalize(raw)
        self.assertEqual(codes(P.validate_static(p), "warn"), ["W_NO_RUNNER_RULE"])
        raw["lifecycle"]["max_hold_min"] = 30
        p, _ = P.normalize(raw)
        self.assertEqual(P.validate_static(p), [])

    def test_shadow_variants(self):
        raw = scalp()
        raw["shadow_variants"] = [{"name": "wide-stop", "patch": {"stop": {"price": 143.4}}}]
        self.assertEqual(errs(raw), [])
        raw["shadow_variants"] = [{"name": "bad", "patch": {"symbol": "ETH"}}]
        self.assertIn("E_UNKNOWN_FIELD", errs(raw))
        raw["shadow_variants"] = [{"name": "bad", "patch": []}]
        self.assertIn("E_TYPE", errs(raw))
        raw["shadow_variants"] = [{"name": "Bad Name", "patch": {}}]
        self.assertIn("E_RANGE", errs(raw))

    def test_source_shadow_needs_parent(self):
        raw = scalp()
        raw["source"] = {"kind": "shadow"}
        self.assertIn("E_SCHEMA", errs(raw))
        raw["source"] = {"kind": "shadow", "parent_plan": "PL-1028-SOL-01"}
        self.assertEqual(errs(raw), [])

    def test_paper_plan_without_session(self):
        raw = scalp()
        raw["mode"] = "paper"
        del raw["session_type"]
        self.assertEqual(errs(raw), [])


class TestContext(unittest.TestCase):
    def v(self, plan=None, **kw):
        return P.validate_context(plan if plan is not None else ok_scalp_plan(), ctx(**kw))

    def test_no_session(self):
        self.assertIn("E_NO_SESSION", codes(self.v(session=None)))

    def test_session_mismatch(self):
        self.assertIn("E_SESSION_MISMATCH", codes(self.v(session={"type": "intraday", "expires": 1e9})))

    def test_paper_skips_session(self):
        raw = scalp()
        raw["mode"] = "paper"
        iss = P.validate_context(norm(raw), ctx(session=None))
        self.assertNotIn("E_NO_SESSION", codes(iss))

    def test_symbol_unknown(self):
        self.assertIn("E_SYMBOL_UNKNOWN", codes(self.v(instrument=None)))

    def test_session_has_no_expiry(self):
        self.assertEqual(codes(self.v(session={"type": "scalp", "expires": 1_000_000.0 + 300})), [])
        self.assertNotIn("E_TTL", codes(self.v(session={"type": "scalp", "expires": 1_000_000.0 - 1})))

    def test_price_stale(self):
        self.assertEqual(codes(self.v(price=None)), ["W_PRICE_STALE_AT_ACCEPT"])
        self.assertEqual(codes(self.v(price_age_s=60)), ["W_PRICE_STALE_AT_ACCEPT"])

    def test_level_distance(self):
        raw = scalp()
        raw["entry"]["when"]["all"][0]["level"] = 1425.0   # опечатка на порядок
        self.assertIn("E_LEVEL_DISTANCE", codes(self.v(norm(raw))))
        self.assertIn("E_LEVEL_DISTANCE", codes(self.v(price=500.0)))

    def test_live_price_sides(self):
        self.assertIn("E_STOP_SIDE", codes(self.v(price=144.0)))     # цена уже выше стопа шорта
        self.assertIn("E_TARGET_SIDE", codes(self.v(price=140.0)))   # цена уже ниже целей шорта
        self.assertIn("E_CANCEL_LEVELS", codes(self.v(price=139.0)))

    def test_stop_pct_and_rr(self):
        raw = scalp()
        raw["stop"]["price"] = 144.0    # 1.2% > 0.8%
        self.assertIn("E_STOP_PCT", codes(self.v(norm(raw))))
        raw = scalp()
        raw["targets"] = [{"id": "t1", "price": 142.0, "frac": 1.0}]
        self.assertIn("E_RR", codes(self.v(norm(raw))))

    def test_no_confirm_size_cap(self):
        raw = scalp()
        raw["entry"]["when"] = {"kind": "spread_bps", "lte": 5}
        raw["risk"].update(confirm_waiver=True, size_mult=0.8)
        self.assertIn("E_NO_CONFIRM", codes(self.v(norm(raw))))
        raw["risk"]["size_mult"] = 0.5
        self.assertNotIn("E_NO_CONFIRM", codes(self.v(norm(raw))))
        raw["risk"]["confirm_waiver"] = False
        self.assertIn("E_NO_CONFIRM", codes(self.v(norm(raw))))

    def test_chase_above_cap_is_not_error(self):
        raw = scalp()
        raw["entry"]["valid"]["max_chase_bps"] = 40   # потолок применяет исполнитель (пример интрадея: 20 при умолчании 15)
        self.assertNotIn("E_RANGE", codes(self.v(norm(raw))))

    def test_guard(self):
        iss = self.v(guard={"ok": False, "reasons": ["daily_loss_limit_reached", "net_rr"]})
        self.assertIn("E_GUARD", codes(iss))
        self.assertIn("daily_loss_limit_reached", [i for i in iss if i["code"] == "E_GUARD"][0]["msg"])
        self.assertNotIn("E_GUARD", codes(self.v(guard={"ok": True})))

    def test_cooldown(self):
        self.assertIn("E_COOLDOWN", codes(self.v(cooldowns={"symbols": {"SOL": 1_000_000.0 + 600}})))
        self.assertIn("E_COOLDOWN", codes(self.v(cooldowns={"global": 1_000_000.0 + 600})))
        self.assertNotIn("E_COOLDOWN", codes(self.v(cooldowns={"symbols": {"SOL": 1_000_000.0 - 1}, "global": None})))

    def test_limit_plans(self):
        many = [{"symbol": "S%d" % i, "mode": "live", "status": "armed"} for i in range(6)]
        self.assertIn("E_LIMIT_PLANS", codes(self.v(active_plans=many)))
        done = [{"symbol": "S%d" % i, "mode": "live", "status": "closed"} for i in range(6)]
        self.assertNotIn("E_LIMIT_PLANS", codes(self.v(active_plans=done)))
        self.assertIn("E_LIMIT_PLANS", codes(self.v(submits_1h=20)))

    def test_symbol_busy(self):
        self.assertIn("E_SYMBOL_BUSY", codes(self.v(active_plans=[{"symbol": "SOL", "mode": "live", "status": "managing"}])))
        self.assertNotIn("E_SYMBOL_BUSY", codes(self.v(active_plans=[{"symbol": "SOL", "mode": "live", "status": "closed"}])))
        self.assertNotIn("E_SYMBOL_BUSY", codes(self.v(active_plans=[{"symbol": "SOL", "mode": "paper", "status": "armed"}])))
        self.assertIn("E_SYMBOL_BUSY", codes(self.v(active_plans=[{"spec": {"symbol": "SOL", "mode": "live"}, "status": "armed"}])))

    def test_adopt(self):
        raw = scalp()
        raw["entry"] = {"how": {"type": "adopt"}}
        raw["source"]["kind"] = "adopt"
        p = norm(raw)
        self.assertIn("E_ADOPT_NO_POSITION", codes(P.validate_context(p, ctx(position=None))))
        self.assertIn("E_ADOPT_NO_STOP", codes(P.validate_context(p, ctx(position={"side": "short", "size": 1.0, "sl": 0.0}))))
        self.assertIn("E_STOP_WIDEN", codes(P.validate_context(p, ctx(position={"side": "short", "size": 1.0, "sl": 142.8}))))   # 143.0 слабее 142.8
        self.assertNotIn("E_STOP_WIDEN", codes(P.validate_context(p, ctx(position={"side": "short", "size": 1.0, "sl": 143.2}))))
        self.assertIn("E_ADOPT_NO_POSITION", codes(P.validate_context(p, ctx(position={"side": "long", "size": 1.0, "sl": 143.2}))))

    def test_pyramid_limits(self):
        lim = copy.deepcopy(LIMITS)
        lim["pyramid"].update(enabled=True, min_r=2.0, max_add_risk_frac=0.3)
        c = ctx(session={"type": "intraday", "expires": 1e9}, price=145.6, limits=lim)
        iss = P.validate_context(norm(intraday()), c)
        self.assertIn("E_ADD_NOT_PROFIT_GATED", codes(iss))   # 1.5 < min_r 2.0
        self.assertIn("E_ADD_RISK", codes(iss))               # 0.5 > 0.3

    def test_ladder_collapse(self):
        g = {"ok": True, "qty": 0.2}
        iss = self.v(guard=g)
        self.assertEqual(codes(iss), [])   # 0.2 -> 0.1 + 0.1: лесенка цела
        iss = self.v(guard={"ok": True, "qty": 0.1})
        self.assertEqual(codes(iss), ["W_LADDER_COLLAPSE"])

    def test_ladder_parts(self):
        self.assertEqual(P.ladder_parts(0.3, [0.5, 0.5], 0.1, 0.1), [0.1, 0.2])
        self.assertEqual(P.ladder_parts(0.1, [0.5, 0.5], 0.1, 0.1), [0.1])
        self.assertEqual(P.ladder_parts(1.0, [0.5, 0.5], 0.1, 0.1), [0.5, 0.5])
        self.assertEqual(P.ladder_parts(1.0, [0.3, 0.3], 0.1, 0.1), [0.3, 0.3])   # бегун остаётся в позиции
        self.assertEqual(P.ladder_parts(0.0, [1.0], 0.1), [])
        self.assertEqual(P.ladder_parts(0.5, [0.5, 0.5], 0.0), [])
        self.assertEqual(P.ladder_parts(1.0, [0.5, 0.5], 0.1, 0.1, 100.0, [10.0, 10.0]), [1.0])   # номинал < min_notional

    def test_context_garbage(self):
        self.assertEqual(codes(P.validate_context(None, {})), ["E_TYPE"])
        self.assertEqual(codes(P.validate_context({}, None)), ["E_TYPE"])
        P.validate_context({}, {})
        P.validate_context(scalp(), {"limits": 5, "profile": 5, "session": 5, "active_plans": 5, "cooldowns": 5})

    def test_missing_keys_skip_checks(self):
        self.assertEqual(P.validate_context(ok_scalp_plan(), {}), [])


class TestTransitions(unittest.TestCase):
    def test_table(self):
        ok = {("draft", "armed"), ("draft", "rejected"), ("draft", "cancelled"), ("armed", "entered"), ("armed", "cancelled"), ("armed", "expired"),
              ("armed", "invalidated"), ("entered", "managing"), ("entered", "closed"), ("managing", "closed")}
        for a in P.STATUSES:
            for b in P.STATUSES:
                self.assertEqual(P.can_transition(a, b), (a, b) in ok, (a, b))

    def test_check_transition(self):
        self.assertEqual(P.check_transition("armed", "entered"), [])
        self.assertEqual(codes(P.check_transition("managing", "armed")), ["E_TRANSITION"])
        self.assertEqual(codes(P.check_transition("closed", "armed")), ["E_TRANSITION"])
        self.assertEqual(codes(P.check_transition("nope", "armed")), ["E_TRANSITION"])
        self.assertEqual(codes(P.check_transition("armed", "managing")), ["E_TRANSITION"])


class TestAmend(unittest.TestCase):
    """Матрица validate_amend по статусам (01 п. 1.7)."""

    def setUp(self):
        self.old = norm(scalp())
        self.live = {"exch_stop": 143.0, "position": True, "stop_at_or_better_be": False}

    def new(self, fn):
        n = copy.deepcopy(self.old)
        fn(n)
        return n

    def am(self, fn, status, live=None):
        return P.validate_amend(self.old, self.new(fn), status, live if live is not None else self.live)

    def test_no_change_ok_everywhere(self):
        for st in P.STATUSES:
            self.assertEqual(P.validate_amend(self.old, copy.deepcopy(self.old), st, self.live), [])

    def test_terminal_forbids_any_change(self):
        for st in P.TERMINAL:
            self.assertEqual(codes(self.am(lambda n: n.update(thesis="новый тезис для плана"), st)), ["E_STATUS_FORBIDS"])

    def test_unknown_status(self):
        self.assertEqual(codes(self.am(lambda n: n.update(thesis="новый тезис для плана"), "weird")), ["E_STATUS_FORBIDS"])

    def test_immutable_fields(self):
        for k, v in (("symbol", "ETH"), ("side", "long"), ("mode", "paper"), ("session_type", "intraday"), ("source", {"kind": "brain"})):
            for st in ("draft", "armed", "managing"):
                self.assertIn("E_IMMUTABLE", codes(self.am(lambda n: n.update({k: v}), st)), (k, st))

    def test_thesis_and_tags_free(self):
        for st in ("draft", "armed", "entered", "managing"):
            self.assertEqual(self.am(lambda n: n.update(thesis="другой тезис, но с тем же смыслом"), st), [])
            self.assertEqual(self.am(lambda n: n["tags"].update(setup="new-setup"), st), [])

    def test_experiment_frozen_after_armed(self):
        f = lambda n: n["tags"].update(experiment="other-exp")
        self.assertEqual(self.am(f, "draft"), [])
        for st in ("armed", "managing"):
            self.assertIn("E_IMMUTABLE", codes(self.am(f, st)))

    def test_entry_only_before_position(self):
        f = lambda n: n["entry"].update(ref_price=142.4)
        self.assertEqual(self.am(f, "draft"), [])
        self.assertEqual(self.am(f, "armed"), [])
        for st in ("entered", "managing"):
            self.assertEqual(codes(self.am(f, st)), ["E_STATUS_FORBIDS"])

    def test_stop_widen_trap(self):
        """Тест-ловушка: расширение стопа в позиции запрещено любым способом (short: вверх = хуже)."""
        for st in ("entered", "managing"):
            for p in (143.01, 143.5, 150.0, 1e9):
                self.assertEqual(codes(self.am(lambda n: n["stop"].update(price=p), st)), ["E_STOP_WIDEN"], (st, p))
        long_old = norm(intraday())
        for p in (143.19, 100.0, 0.0001):
            n = copy.deepcopy(long_old)
            n["stop"]["price"] = p
            self.assertEqual(codes(P.validate_amend(long_old, n, "managing", {"exch_stop": 143.2})), ["E_STOP_WIDEN"], p)

    def test_stop_remove_trap(self):
        for st in ("entered", "managing"):
            self.assertEqual(codes(self.am(lambda n: n.pop("stop"), st)), ["E_STOP_WIDEN"])
            self.assertEqual(codes(self.am(lambda n: n["stop"].pop("price"), st)), ["E_STOP_WIDEN"])
        self.assertEqual(codes(self.am(lambda n: n.pop("stop"), "armed")), ["E_STOP_WIDEN"])

    def test_stop_tighten_ok_and_compares_with_exchange_stop(self):
        self.assertEqual(self.am(lambda n: n["stop"].update(price=142.5), "managing"), [])
        self.assertEqual(self.am(lambda n: n["stop"].update(price=143.0), "managing"), [])   # равный: no-op
        # биржевой стоп уже подтянут правилом до 142.4: spec (143.0) его не отражает, 142.9 был бы ослаблением
        live = dict(self.live, exch_stop=142.4)
        self.assertEqual(codes(self.am(lambda n: n["stop"].update(price=142.9), "managing", live)), ["E_STOP_WIDEN"])
        self.assertEqual(self.am(lambda n: n["stop"].update(price=142.3), "managing", live), [])

    def test_stop_without_exchange_stop_uses_spec(self):
        live = dict(self.live, exch_stop=None)
        self.assertEqual(codes(self.am(lambda n: n["stop"].update(price=143.5), "managing", live)), ["E_STOP_WIDEN"])

    def test_stop_free_before_position(self):
        for st in ("draft", "armed"):
            self.assertEqual(self.am(lambda n: n["stop"].update(price=143.6), st), [])

    def test_trigger_by_frozen_in_position(self):
        self.assertEqual(codes(self.am(lambda n: n["stop"].update(trigger_by="mark"), "managing")), ["E_STATUS_FORBIDS"])

    def test_targets_closer_free_farther_needs_be(self):
        closer = lambda n: n["targets"][1].update(price=140.6)
        farther = lambda n: n["targets"][1].update(price=139.0)
        self.assertEqual(self.am(closer, "managing"), [])
        self.assertEqual(codes(self.am(farther, "managing")), ["E_TARGET_FARTHER_NOT_RISKFREE"])
        self.assertEqual(self.am(farther, "managing", dict(self.live, stop_at_or_better_be=True)), [])
        self.assertEqual(self.am(farther, "armed"), [])
        self.assertEqual(self.am(farther, "draft"), [])

    def test_targets_farther_long(self):
        old = norm(intraday())
        n = copy.deepcopy(old)
        n["targets"][0]["price"] = 149.0
        self.assertEqual(codes(P.validate_amend(old, n, "managing", {"exch_stop": 143.2})), ["E_TARGET_FARTHER_NOT_RISKFREE"])
        n["targets"][0]["price"] = 148.0
        self.assertEqual(P.validate_amend(old, n, "managing", {"exch_stop": 143.2}), [])

    def test_target_removal_ok_filled_immutable(self):
        rm = lambda n: n.update(targets=n["targets"][:1])
        self.assertEqual(self.am(rm, "managing"), [])
        filled = dict(self.live, filled_targets=["t2"])
        self.assertEqual(codes(self.am(rm, "managing", filled)), ["E_IMMUTABLE"])
        self.assertEqual(codes(self.am(lambda n: n["targets"][1].update(price=140.6), "managing", filled)), ["E_IMMUTABLE"])

    def test_target_frac_increase_needs_be(self):
        def f(n):
            n["targets"][0]["frac"] = 0.5
            n["targets"][1]["frac"] = 0.5
        self.old["targets"][0]["frac"] = 0.4
        self.assertEqual(codes(self.am(f, "managing")), ["E_TARGET_FARTHER_NOT_RISKFREE"])

    def test_size_mult_only_decrease(self):
        up = lambda n: n["risk"].update(size_mult=1.0)
        self.old["risk"]["size_mult"] = 0.6
        self.assertEqual(self.am(up, "draft"), [])
        self.assertEqual(codes(self.am(up, "armed")), ["E_RISK_UP"])
        self.assertEqual(self.am(up, "armed", dict(self.live, has_order=False)), [])
        self.old["risk"]["size_mult"] = 1.0
        self.assertEqual(self.am(lambda n: n["risk"].update(size_mult=0.5), "armed"), [])

    def test_risk_score_waiver_free_before_entry_forbidden_after(self):
        f = lambda n: n["risk"].update(score=0.9, confirm_waiver=True)
        self.assertEqual(self.am(f, "armed"), [])
        self.assertEqual(codes(self.am(f, "managing")), ["E_STATUS_FORBIDS"])
        self.assertEqual(codes(self.am(lambda n: n["risk"].update(size_mult=0.5), "managing")), ["E_STATUS_FORBIDS"])

    def test_manage_and_invalidation_free(self):
        for st in ("draft", "armed", "entered", "managing"):
            self.assertEqual(self.am(lambda n: n.update(manage=n["manage"][:2]), st), [])
            self.assertEqual(self.am(lambda n: n.update(invalidation=[]), st), [])

    def test_hold_extend(self):
        longer = lambda n: n["lifecycle"].update(max_hold_min=60)
        shorter = lambda n: n["lifecycle"].update(max_hold_min=5)
        self.assertEqual(codes(self.am(longer, "managing")), ["E_HOLD_EXTEND"])
        self.assertEqual(codes(self.am(lambda n: n["lifecycle"].pop("max_hold_min"), "managing")), ["E_HOLD_EXTEND"])
        self.assertEqual(self.am(shorter, "managing"), [])
        self.assertEqual(self.am(longer, "managing", dict(self.live, stop_at_or_better_be=True)), [])
        self.assertEqual(self.am(longer, "armed"), [])
        self.assertEqual(codes(self.am(lambda n: n["lifecycle"].update(flatten_at_session_end=False), "managing")), ["E_HOLD_EXTEND"])

    def test_lifecycle_ttl(self):
        f = lambda n: n["lifecycle"].update(ttl_min=20)
        self.assertEqual(self.am(f, "draft"), [])
        self.assertEqual(self.am(f, "armed"), [])
        self.assertEqual(codes(self.am(f, "managing")), ["E_STATUS_FORBIDS"])

    def test_shadow_variants_frozen_in_position(self):
        f = lambda n: n.update(shadow_variants=[{"name": "aa", "patch": {}}])
        self.assertEqual(self.am(f, "armed"), [])
        self.assertEqual(codes(self.am(f, "managing")), ["E_STATUS_FORBIDS"])

    def test_ignored_rev_and_guard_preview(self):
        n = copy.deepcopy(self.old)
        n["rev"] = 9
        n["guard_preview"] = {"ok": False}
        self.assertEqual(P.validate_amend(self.old, n, "managing", self.live), [])

    def test_garbage(self):
        self.assertEqual(codes(P.validate_amend(None, {}, "armed", {})), ["E_TYPE"])
        self.assertEqual(codes(P.validate_amend({}, [], "armed", {})), ["E_TYPE"])
        P.validate_amend(self.old, copy.deepcopy(self.old), "armed", None)


class TestAmendPipeline(unittest.TestCase):
    def setUp(self):
        self.spec = norm(scalp(), keep_executor=True)
        self.spec["id"] = "PL-1028-SOL-01"
        self.spec["rev"] = 3
        self.live = {"exch_stop": 143.0}

    def test_ok(self):
        new, iss = P.amend(self.spec, {"stop": {"price": 142.8}, "thesis": "обновлённый тезис плана"}, "managing", self.live, 3)
        self.assertEqual(iss, [])
        self.assertEqual(new["stop"]["price"], 142.8)
        self.assertEqual(new["stop"]["trigger_by"], "last")
        self.assertEqual(new["rev"], 3)
        self.assertEqual(self.spec["stop"]["price"], 143.0)   # исходный не мутирован

    def test_rev_stale(self):
        for r in (2, 4, None, "3", True):
            new, iss = P.amend(self.spec, {}, "armed", self.live, r)
            self.assertEqual((new, codes(iss)), (None, ["E_REV_STALE"]))

    def test_terminal(self):
        new, iss = P.amend(self.spec, {"thesis": "обновлённый тезис плана"}, "closed", self.live, 3)
        self.assertEqual((new, codes(iss)), (None, ["E_STATUS_FORBIDS"]))

    def test_widen_rejected_atomically(self):
        new, iss = P.amend(self.spec, {"stop": {"price": 143.4}, "thesis": "обновлённый тезис плана"}, "managing", self.live, 3)
        self.assertIsNone(new)
        self.assertEqual(codes(iss), ["E_STOP_WIDEN"])

    def test_null_removes_stop(self):
        new, iss = P.amend(self.spec, {"stop": None}, "managing", self.live, 3)
        self.assertIsNone(new)
        self.assertIn("E_STOP_WIDEN", codes(iss))

    def test_patch_static_errors_reported(self):
        new, iss = P.amend(self.spec, {"targets": [{"id": "t1", "price": 141.0, "frac": 0.8}, {"id": "t2", "price": 140.0, "frac": 0.8}]}, "armed", {}, 3)
        self.assertIsNone(new)
        self.assertIn("E_FRAC_SUM", codes(iss))

    def test_immutable_via_patch(self):
        new, iss = P.amend(self.spec, {"side": "long"}, "armed", {}, 3)
        self.assertIsNone(new)
        self.assertIn("E_IMMUTABLE", codes(iss))

    def test_executor_fields_in_patch_ignored(self):
        new, iss = P.amend(self.spec, {"rev": 99, "id": "PL-0000-X-01", "thesis": "обновлённый тезис плана"}, "armed", {}, 3)
        self.assertEqual(codes(iss), ["W_FIELD_IGNORED"])
        self.assertEqual((new["rev"], new["id"]), (3, "PL-1028-SOL-01"))

    def test_patch_garbage(self):
        self.assertEqual(codes(P.amend(self.spec, [], "armed", {}, 3)[1]), ["E_TYPE"])
        deep = cur = {}
        for _ in range(200):
            cur["a"] = {}
            cur = cur["a"]
        new, iss = P.amend(self.spec, deep, "armed", {}, 3)
        self.assertEqual((new, codes(iss)), (None, ["E_TOO_LARGE"]))

    def test_manage_replaced_whole_and_add_rule_checked(self):
        bad = {"manage": [{"id": "pyr", "when": {"kind": "elapsed", "min": 1}, "do": {"action": "add", "risk_frac": 0.5}}]}
        new, iss = P.amend(self.spec, bad, "managing", self.live, 3)
        self.assertIn("E_ADD_NOT_PROFIT_GATED", codes(iss))
        good = {"manage": [{"id": "be", "when": {"kind": "pnl_r", "gte": 1}, "do": {"action": "move_stop", "to": "breakeven"}, "once": True}]}
        new, iss = P.amend(self.spec, good, "managing", self.live, 3)
        self.assertEqual(len(new["manage"]), 1)

    def test_check_rev(self):
        self.assertEqual(P.check_rev({"rev": 2}, 2), [])
        self.assertEqual(codes(P.check_rev({"rev": 2}, 1)), ["E_REV_STALE"])
        self.assertEqual(codes(P.check_rev(None, 1)), ["E_REV_STALE"])


class TestMergePatch(unittest.TestCase):
    """Примеры RFC 7386 (приложение A)."""
    CASES = [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ]

    def test_rfc_examples(self):
        for target, patch, want in self.CASES:
            self.assertEqual(P.merge_patch(target, patch), want, (target, patch))

    def test_no_mutation_and_no_aliasing(self):
        t, p = {"a": {"b": [1]}}, {"a": {"c": {"d": [2]}}}
        t0, p0 = copy.deepcopy(t), copy.deepcopy(p)
        r = P.merge_patch(t, p)
        self.assertEqual((t, p), (t0, p0))
        r["a"]["c"]["d"].append(9)
        r["a"]["b"].append(9)
        self.assertEqual((t, p), (t0, p0))

    def test_too_deep(self):
        deep = cur = {}
        for _ in range(100):
            cur["a"] = {}
            cur = cur["a"]
        with self.assertRaises(ValueError):
            P.merge_patch({}, deep)


class TestDiff(unittest.TestCase):
    def test_diff(self):
        d = P.diff({"a": 1, "b": {"c": 1, "d": 2}, "e": [1]}, {"a": 1, "b": {"c": 2, "x": 1}, "e": [1, 2]})
        by = dict((i["path"], i) for i in d)
        self.assertEqual(by["b.c"], {"path": "b.c", "op": "change", "old": 1, "new": 2})
        self.assertEqual(by["b.d"]["op"], "remove")
        self.assertEqual(by["b.x"]["op"], "add")
        self.assertEqual(by["e"]["op"], "change")
        self.assertNotIn("a", by)

    def test_diff_equal_and_numbers(self):
        self.assertEqual(P.diff({"a": 143}, {"a": 143.0}), [])
        self.assertEqual(P.diff({}, {}), [])

    def test_diff_of_amend(self):
        spec = norm(scalp())
        n = copy.deepcopy(spec)
        n["stop"]["price"] = 142.8
        self.assertEqual([i["path"] for i in P.diff(spec, n)], ["stop.price"])


class TestCanonHash(unittest.TestCase):
    def test_stable(self):
        a = norm(scalp())
        h = P.canon_hash(a)
        self.assertEqual(len(h), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))
        b = json.loads(json.dumps(a, sort_keys=True))   # другой порядок ключей
        self.assertEqual(P.canon_hash(b), h)
        c = json.loads(json.dumps(a, indent=4))         # другие пробелы
        self.assertEqual(P.canon_hash(c), h)

    def test_int_float_equal(self):
        a = {"symbol": "SOL", "side": "long", "stop": {"price": 143}}
        b = {"symbol": "SOL", "side": "long", "stop": {"price": 143.0}}
        self.assertEqual(P.canon_hash(a), P.canon_hash(b))

    def test_sensitive_to_prereg_fields_only(self):
        a = norm(scalp())
        h = P.canon_hash(a)
        for fn in (lambda p: p["stop"].update(price=143.1), lambda p: p["targets"][0].update(price=141.1), lambda p: p["manage"].pop(),
                   lambda p: p["invalidation"].clear(), lambda p: p["risk"].update(score=0.1), lambda p: p["entry"].update(ref_price=142.2),
                   lambda p: p.update(side="long"), lambda p: p.update(symbol="ETH")):
            b = copy.deepcopy(a)
            fn(b)
            self.assertNotEqual(P.canon_hash(b), h)
        for fn in (lambda p: p.update(thesis="совсем другой тезис, не влияет"), lambda p: p["tags"].update(variant="zz"), lambda p: p.update(rev=5),
                   lambda p: p["lifecycle"].update(ttl_min=3)):
            b = copy.deepcopy(a)
            fn(b)
            self.assertEqual(P.canon_hash(b), h)

    def test_garbage(self):
        self.assertEqual(len(P.canon_hash(None)), 16)
        self.assertEqual(P.canon_hash({}), P.canon_hash(None))

    def test_unicode_stable(self):
        a = {"symbol": "SOL", "entry": {"note": "привет"}}
        self.assertEqual(P.canon_hash(a), P.canon_hash(json.loads(json.dumps(a, ensure_ascii=True))))


class TestJsonSchema(unittest.TestCase):
    """Простой обходчик draft-07 (подмножество, которое использует json_schema())."""

    def resolve(self, root, ref):
        assert ref.startswith("#/")
        node = root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def valid(self, root, schema, v):
        if "$ref" in schema:
            return self.valid(root, self.resolve(root, schema["$ref"]), v)
        if "oneOf" in schema:
            return sum(1 for s in schema["oneOf"] if self.valid(root, s, v)) == 1
        if "enum" in schema and v not in schema["enum"]:
            return False
        t = schema.get("type")
        if t == "object":
            if not isinstance(v, dict):
                return False
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in v:
                    return False
            for k, x in v.items():
                if k not in props:
                    if schema.get("additionalProperties") is False:
                        return False
                    continue
                if not self.valid(root, props[k], x):
                    return False
            return True
        if t == "array":
            if not isinstance(v, list):
                return False
            if len(v) < schema.get("minItems", 0) or len(v) > schema.get("maxItems", 10 ** 9):
                return False
            return all(self.valid(root, schema["items"], x) for x in v) if "items" in schema else True
        if t == "string":
            import re
            if not isinstance(v, str):
                return False
            if len(v) < schema.get("minLength", 0) or len(v) > schema.get("maxLength", 10 ** 9):
                return False
            return not ("pattern" in schema and not re.fullmatch(schema["pattern"], v))
        if t in ("number", "integer"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return False
            if t == "integer" and not isinstance(v, int):
                return False
            if "minimum" in schema and v < schema["minimum"]:
                return False
            if "maximum" in schema and v > schema["maximum"]:
                return False
            if "exclusiveMinimum" in schema and v <= schema["exclusiveMinimum"]:
                return False
            return True
        if t == "boolean":
            return isinstance(v, bool)
        return True

    def test_schema_is_json_serializable(self):
        s = P.json_schema()
        json.dumps(s)
        self.assertEqual(s["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertFalse(s["additionalProperties"])
        self.assertIn("schema", s["required"])

    def test_examples_validate(self):
        s = P.json_schema()
        for raw in (scalp(), intraday(), norm(scalp()), norm(intraday())):
            self.assertTrue(self.valid(s, s, raw))

    def test_bad_plans_rejected(self):
        s = P.json_schema()
        for fn in (lambda r: r.update(typo=1), lambda r: r["stop"].update(price=-1), lambda r: r["entry"]["when"]["all"][0].update(tf="2m"),
                   lambda r: r["manage"][0]["do"].update(action="teleport"), lambda r: r.pop("targets"), lambda r: r["targets"][0].update(id="t9"),
                   lambda r: r["entry"]["when"]["all"].append({"kind": "pnl_r", "gte": 1}),   # pnl_r не разрешён во входе
                   lambda r: r.update(side="x")):
            raw = scalp()
            fn(raw)
            self.assertFalse(self.valid(s, s, raw))

    def test_seq_only_in_entry(self):
        s = P.json_schema()
        raw = intraday()
        self.assertTrue(self.valid(s, s, raw))
        raw["manage"][0]["when"] = raw["entry"]["when"]
        self.assertFalse(self.valid(s, s, raw))

    def test_table_matches_constants(self):
        self.assertEqual(set(P.COND_FIELDS), set(P.COND_KINDS))
        for k, spec in P.COND_FIELDS.items():
            self.assertEqual(set(spec), set(P.COND_KINDS[k]["fields"]), k)
        self.assertEqual(set(P.ACTION_FIELDS), set(P.ACTIONS))
        for k, spec in P.ACTION_FIELDS.items():
            self.assertEqual(set(spec), set(P.ACTIONS[k]["params"]), k)

    def test_every_error_code_has_a_producer(self):
        """Все E_/W_ из 1.8 встречаются в тексте реализации (защита от забытого кода)."""
        import inspect
        src = inspect.getsource(P)
        for c in P.ERROR_CODES + P.WARN_CODES:
            if c == "W_TTL_CLAMPED":        # код в контракте (07/06), но у сессии нет срока: больше не выдаётся
                continue
            self.assertGreaterEqual(src.count('"%s"' % c), 2, c)   # объявление в кортеже + хотя бы одно использование


class TestDescribe(unittest.TestCase):
    def test_all_kinds(self):
        samples = [
            ({"kind": "price_beyond", "dir": "above", "level": 142.5, "allow_already": True}, "цена выше 142.5"),
            ({"kind": "candle_close", "tf": "1h", "dir": "below", "level": 10, "n": 2}, "1ч закрытие ниже 10 (n=2)"),
            ({"kind": "candle_reject", "tf": "5m", "level": 142.5, "dir": "long", "wick_min_pct": 0.1}, "вниз"),
            ({"kind": "candle_body", "tf": "15m", "dir": "up", "min_body_pct": 60, "min_close_pos": 0.8}, "15м свеча вверх"),
            ({"kind": "volume_spike", "tf": "3m", "mult": 2, "lookback": 20, "on": "live", "dir": "up"}, "на формирующейся"),
            ({"kind": "price_in_zone", "low": 1, "high": 2}, "цена в зоне 1..2"),
            ({"kind": "spread_bps", "lte": 6}, "спред <= 6 бп"),
            ({"kind": "pnl_r", "gte": 1}, "результат >= 1 R"),
            ({"kind": "pnl_r", "lt": -0.5}, "результат < -0.5 R"),
            ({"kind": "elapsed", "min": 10}, "прошло >= 10 мин"),
            ({"kind": "target_filled", "target": "t1"}, "цель t1 исполнена"),
            ({"kind": "near_target", "target": "t1", "frac": 0.8}, "80% пути до цели t1"),
            ({"not": {"kind": "elapsed", "min": 1}}, "не ("),
            ({"any": [{"kind": "elapsed", "min": 1}, {"kind": "elapsed", "min": 2}]}, "ИЛИ"),
            ({"all": [{"kind": "elapsed", "min": 1}, {"kind": "elapsed", "min": 2}]}, " И "),
        ]
        for c, frag in samples:
            self.assertIn(frag, P.describe_cond(c), c)

    def test_spec_example_string(self):
        self.assertEqual(P.describe_cond({"kind": "candle_close", "tf": "5m", "dir": "above", "level": 142.5, "n": 1}), "5м закрытие выше 142.5 (n=1)")

    def test_garbage(self):
        for g in (None, 5, "x", [], {}, {"kind": "zzz"}, {"kind": "candle_close"}, {"all": [None]}):
            self.assertIsInstance(P.describe_cond(g), str)


class TestFuzz(unittest.TestCase):
    """Мутационный прогон с фиксированным seed: ни одна функция не бросает исключение на мусоре."""

    JUNK = [None, 0, -1, 1e308, "x", "", [], {}, [None], {"a": 1}, True, float("nan"), "A\u202e", 10 ** 30, {"kind": "candle_close"},
            {"all": [{}]}, {"not": 5}, {"action": "add"}, ["a"], "short", "market"]

    def mutate(self, rnd, o):
        if isinstance(o, dict) and o:
            k = rnd.choice(list(o))
            if rnd.random() < 0.3:
                o[k] = rnd.choice(self.JUNK)
            elif rnd.random() < 0.1:
                del o[k]
            else:
                self.mutate(rnd, o[k])
        elif isinstance(o, list) and o:
            i = rnd.randrange(len(o))
            if rnd.random() < 0.3:
                o[i] = rnd.choice(self.JUNK)
            else:
                self.mutate(rnd, o[i])

    def test_no_exceptions(self):
        import random
        rnd = random.Random(20261028)
        old = norm(scalp())
        for _ in range(1500):
            raw = copy.deepcopy(rnd.choice([scalp(), intraday()]))
            for _ in range(rnd.randint(1, 4)):
                self.mutate(rnd, raw)
            p, _i = P.normalize(raw)
            P.validate_static(p)
            P.validate_context(p, ctx(position={"side": "short", "size": 1, "sl": 143}, guard={"ok": True, "qty": 0.3}))
            st = rnd.choice(P.STATUSES)
            P.validate_amend(old, p, st, {"exch_stop": 143.0})
            P.validate_amend(p, old, st, {"exch_stop": 143.0})
            P.canon_hash(p)
            P.diff(old, p)
            P.amend(old, raw, "managing", {"exch_stop": 143.0}, old.get("rev"))
            if isinstance(p.get("entry"), dict):
                P.describe_cond(p["entry"].get("when"))


class TestPurity(unittest.TestCase):
    def test_imports_only_allowed(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(P))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        self.assertTrue(mods <= {"copy", "hashlib", "json", "math", "re"}, mods)
        import tb.core.linkid as L
        tree = ast.parse(inspect.getsource(L))
        mods = set(a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names)
        self.assertEqual(mods, {"re"})

    def test_validators_do_not_mutate(self):
        p = norm(scalp())
        before = copy.deepcopy(p)
        P.validate_static(p)
        P.validate_context(p, ctx())
        P.validate_amend(p, copy.deepcopy(p), "managing", {})
        P.canon_hash(p)
        P.describe_cond(p["entry"]["when"])
        P.diff(p, before)
        self.assertEqual(p, before)

    def test_deterministic(self):
        a = P.validate_static(norm(scalp()))
        self.assertEqual(a, P.validate_static(norm(scalp())))

    def test_nan_never_crashes(self):
        raw = scalp()
        raw["entry"]["when"]["all"][2]["lte"] = float("nan")
        raw["targets"][0]["frac"] = float("nan")
        raw["entry"]["ref_price"] = float("inf")
        p = norm(raw)
        self.assertIn("E_NONFINITE", codes(P.validate_static(p)))
        P.validate_context(p, ctx())
        self.assertFalse(math.isnan(0))


if __name__ == "__main__":
    unittest.main()


class TestDaySession(unittest.TestCase):
    """Сессия-день: план любого горизонта проходит (E_SESSION_MISMATCH только у сессии одного горизонта); ждущий swing/position переживает день."""

    def test_day_session_accepts_any_horizon(self):
        iss = P.validate_context(ok_scalp_plan(), ctx(session={"type": "day", "expires": 1_000_000.0 + 3600}))
        self.assertNotIn("E_SESSION_MISMATCH", codes(iss))
        iss = P.validate_context(ok_scalp_plan(), ctx(session={"type": "intraday", "expires": 1_000_000.0 + 3600}))
        self.assertIn("E_SESSION_MISMATCH", codes(iss))

    def test_ttl_not_clamped_by_session(self):
        raw = scalp()
        raw["session_type"] = "swing"
        raw["entry"]["valid"] = dict(raw["entry"].get("valid") or {}, ttl_min=1440)
        plan = norm(raw)
        iss = P.validate_context(plan, ctx(session={"type": "day", "expires": 1_000_000.0 + 3600}))
        self.assertNotIn("W_TTL_CLAMPED", codes(iss))
        iss = P.validate_context(plan, ctx(session={"type": "swing", "expires": 1_000_000.0 + 3600}))
        self.assertNotIn("W_TTL_CLAMPED", codes(iss))

