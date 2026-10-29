"""Дашборд v2 (WP8): полоса состояния, доска планов, позиции с правилами, лента решений, досье, эксперименты, устойчивость к мусору,
журнал с add/part/adjust, API и сервер. Источники только файлы во временном корне (фикстуры `fixtures_v2.gen`), без сети и без биржи."""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401
from fixtures_v2 import gen
from tb import paths
from tb.dash import model, model2, server

NOW = 1793106000.0  # 2026-10-27T12:20:00Z (середина суток UTC, чтобы «сутки» не делили события)
P1, P2, P3, P4, P5 = "PL-1028-SOL-01", "PL-1028-ETH-02", "PL-1028-BTC-03", "PL-1028-AVAX-04", "PL-1028-LINK-05"


def api(path, query=None, root=None):
    return server.api_payload(path, query or {}, root, NOW)


class Fx(unittest.TestCase):
    """Фикстурный корень на момент NOW; самодельные файлы добавляются поверх."""
    build_kw = {}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbdash2_")
        gen.build(self.root, NOW, **self.build_kw)
        model._CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, rel, obj, raw=False):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(obj if raw else json.dumps(obj, ensure_ascii=False))
        model._CACHE.clear()

    def v2(self, mode="all"):
        model._CACHE.clear()
        return api("/api/state", {"mode": [mode]}, self.root)[1]["v2"]

    def card(self, pid):
        return [c for c in self.v2()["plans"]["cards"] if c["id"] == pid][0]


class TestStrip(Fx):
    def item(self, v, id_):
        return [i for i in v["strip"]["items"] if i["id"] == id_][0]

    def test_no_executor_files_is_quiet_off_state(self):
        shutil.rmtree(self.root)
        os.makedirs(os.path.join(self.root, "state"))
        v = api("/api/v2/state", {}, self.root)[1]
        self.assertFalse(v["available"])
        self.assertEqual(self.item(v, "exec")["level"], "off")
        self.assertEqual(v["strip"]["level"], "ok")           # отсутствие исполнителя не тревога
        self.assertIsNone(v["strip"]["kill"])
        self.assertEqual((v["plans"]["total"], v["positions"], v["decisions_day"]["n"]), (0, [], 0))

    def test_alive_heartbeat(self):
        v = self.v2()
        self.assertEqual(self.item(v, "exec")["level"], "ok")
        self.assertEqual(self.item(v, "net")["level"], "ok")
        self.assertEqual(v["strip"]["exec"]["env"], "mainnet")

    def test_heartbeat_stale_and_dead(self):
        h = json.load(open(os.path.join(self.root, "runtime", "exec_health.json")))
        for age, lvl in ((15, "warn"), (60, "bad")):
            h["ts_epoch"] = NOW - age
            self.put("runtime/exec_health.json", h)
            self.assertEqual(self.item(self.v2(), "exec")["level"], lvl, age)

    def test_observe_hold_and_kill(self):
        h = json.load(open(os.path.join(self.root, "runtime", "exec_health.json")))
        h.update({"observe": True, "hold": "stale_price", "hold_reasons": ["stale_price", "private_ws"]})
        self.put("runtime/exec_health.json", h)
        v = self.v2()
        self.assertEqual(self.item(v, "mode")["value"], "observe")
        self.assertEqual(self.item(v, "hold")["level"], "warn")
        self.assertIn("private_ws", self.item(v, "hold")["text"])
        self.put("state/KILL", {"flatten": True, "reason": "owner halt", "ts": gen.iso(NOW - 30)})
        v = self.v2()
        self.assertEqual(self.item(v, "kill")["level"], "bad")
        self.assertTrue(v["strip"]["kill"]["flatten"])
        self.assertEqual(v["strip"]["level"], "bad")

    def test_kill_file_plain_text_or_empty(self):
        self.put("state/KILL", "остановлено вручную\n", raw=True)
        self.assertIn("вручную", self.v2()["strip"]["kill"]["reason"])
        self.put("state/KILL", "", raw=True)
        self.assertEqual(self.v2()["strip"]["kill"]["reason"], "")

    def conn_event(self, age, state="up", ch="public", down=1):
        row = {"seq": 7, "ts": gen.iso(NOW - age), "type": "connectivity", "pri": "wake", "d": {"channel": ch, "state": state, "down_s": down}}
        self.put("state/bus/events.jsonl", json.dumps(row), raw=True)

    def test_paper_has_no_private_channel(self):
        """Недочёт 6: в paper приватного канала нет, up=false там не сбой: чип «Связь» зелёный."""
        h = json.load(open(os.path.join(self.root, "runtime", "exec_health.json")))
        h.update({"env": "paper", "profile": "paper"})
        h["ws"]["private"] = {"up": False, "age_s": 0.0}
        self.put("runtime/exec_health.json", h)
        net = self.item(self.v2(), "net")
        self.assertEqual(net["level"], "ok")
        self.assertIn("paper", net["text"])

    def test_connectivity_recovery_window(self):
        """После connectivity state=up «Связь» жёлтая («восстановлена», причина в тексте) только 60 с, затем зелёная."""
        self.conn_event(20)
        net = self.item(self.v2(), "net")
        self.assertEqual((net["level"], net["value"]), ("warn", "восстановлена"))
        self.assertIn("обрыв 1 с", net["text"])
        self.conn_event(90)
        self.assertEqual(self.item(self.v2(), "net")["level"], "ok")

    def test_ws_private_down_and_rest_errors(self):
        h = json.load(open(os.path.join(self.root, "runtime", "exec_health.json")))
        h["ws"]["private"] = {"up": False, "down_s": 5}
        self.put("runtime/exec_health.json", h)
        self.assertEqual(self.item(self.v2(), "net")["level"], "warn")
        h["ws"]["private"] = {"up": False, "down_s": 45}
        self.put("runtime/exec_health.json", h)
        self.assertEqual(self.item(self.v2(), "net")["level"], "bad")
        h["ws"]["private"] = {"up": True}
        h["rest"] = {"ok": False, "last_ok_s": 90}
        self.put("runtime/exec_health.json", h)
        self.assertEqual(self.item(self.v2(), "net")["level"], "bad")

    def test_backlog_unacked_and_oldest(self):
        b = self.v2()["strip"]["backlog"]
        self.assertEqual((b["last_seq"], b["consumers"]["chat"]["unacked"]), (1854, 3))
        self.assertGreater(b["oldest_age_s"], 100)
        self.put("state/bus/cursors/chat.json", {"delivered": 1854, "acked": 1854})
        self.assertEqual(self.v2()["strip"]["backlog"]["n"], 0)

    def test_old_unacked_wake_warns(self):
        self.put("state/bus/events.jsonl", json.dumps({"seq": 7, "ts": gen.iso(NOW - 3600), "type": "plan_armed", "pri": "wake", "d": {}}) + "\n", raw=True)
        self.put("state/bus/cursors/chat.json", {"delivered": 7, "acked": 0})
        self.assertEqual(self.item(self.v2(), "events")["level"], "warn")

    def test_garbage_health_does_not_crash(self):
        self.put("runtime/exec_health.json", {"ts_epoch": "abc", "ws": "x", "rest": [1], "plans": [1, 2], "hold_reasons": "oops", "profile": {"a": 1}})
        v = self.v2()
        self.assertIn("strip", v)
        self.put("runtime/exec_health.json", "[1,2,3]", raw=True)
        self.assertEqual(self.item(self.v2(), "exec")["level"], "off")

    def test_pauses_from_limit_hit_events(self):
        p = self.v2()["pauses"]
        self.assertEqual(p[0]["which"], "cooldown")
        self.assertEqual(p[0]["sym"], "BEAT")
        self.assertTrue(0 < p[0]["left_s"] <= 18 * 60)


class TestBoard(Fx):
    def test_columns_and_counts(self):
        v = self.v2()
        c = v["plans"]["counts"]
        self.assertEqual(c, {"watching": 2, "waiting": 2, "in_trade": 2, "closed": 5})
        self.assertEqual(v["plans"]["broken"], 1)
        ids = {x["id"] for x in v["plans"]["cards"]}
        self.assertNotIn("PL-1026-SOL-99", ids)                    # закрыт больше суток назад
        self.assertEqual({x["status"] for x in v["plans"]["cards"] if x["col"] == "closed"}, {"closed", "cancelled", "expired", "invalidated", "rejected"})

    def test_all_statuses_have_a_column(self):
        for st in ("draft", "armed", "entered", "managing", "closed", "cancelled", "expired", "invalidated", "rejected"):
            spec = gen.spec("PL-1028-T%s-01" % st[:2].upper(), "SOL", "long", NOW)
            self.put("state/plans/%s/spec.json" % spec["id"], spec)
            self.put("state/plans/%s/runtime.json" % spec["id"], gen.rt(spec["id"], st, NOW, ts={"created": NOW, "armed": NOW, "entered": None, "closed": NOW - 60}))
            self.assertIn(self.card(spec["id"])["col"], model2.COLUMNS)

    def test_near_level_goes_to_waiting_far_to_watching(self):
        self.assertEqual(self.card(P1)["col"], "waiting")          # 0.13% до уровня
        self.assertEqual(self.card(P2)["col"], "watching")         # 1.0%
        self.assertLessEqual(self.card(P1)["trigger"]["dist_pct"], model2.NEAR_PCT)

    def test_seq_step_makes_waiting(self):
        c = self.card(P3)
        self.assertEqual(c["col"], "waiting")
        self.assertEqual(c["trigger"]["seq"], {"done": 1, "total": 2})

    def test_resting_limit_is_waiting_even_when_far(self):
        s = gen.spec("PL-1028-SOL-20", "SOL", "long", NOW, entry={"how": {"type": "limit", "price": 130.0, "post_only": True}, "valid": {"ttl_min": 60}})
        self.put("state/plans/PL-1028-SOL-20/spec.json", s)
        self.put("state/plans/PL-1028-SOL-20/runtime.json", gen.rt("PL-1028-SOL-20", "armed", NOW, entry={"state": "resting"}))
        c = self.card("PL-1028-SOL-20")
        self.assertEqual(c["col"], "waiting")
        self.assertIn("лимит стоит", c["trigger"]["text"])
        self.assertAlmostEqual(c["trigger"]["dist_pct"], abs(130 - 142.31) / 142.31 * 100, places=2)

    def test_nearest_trigger_distance_and_candle_eta(self):
        t = self.card(P1)["trigger"]
        self.assertAlmostEqual(t["dist_pct"], (142.5 - 142.31) / 142.31 * 100, places=2)
        self.assertEqual(t["level"], 142.5)
        self.assertEqual(t["eta_tf"], "5м")
        self.assertEqual(t["eta_s"], int(300 - (NOW % 300)))
        self.assertIn("прокол", t["text"])
        self.assertEqual(t["met_n"], 1)                            # c1 (объём) защёлкнут исполнителем

    def test_candle_condition_with_price_already_beyond_level(self):
        """Цена уже за уровнем (расстояние ~0), но условие свечное: карточка помечает beyond и тип условия, чтобы не выглядело как «сработало, а входа нет»."""
        s = gen.spec("PL-1028-SOL-22", "SOL", "long", NOW, entry={"when": {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 142.0}, "how": {"type": "market"},
                                                                  "valid": {"ttl_min": 30}})
        self.put("state/plans/PL-1028-SOL-22/spec.json", s)
        self.put("state/plans/PL-1028-SOL-22/runtime.json", gen.rt("PL-1028-SOL-22", "armed", NOW))
        t = self.card("PL-1028-SOL-22")["trigger"]
        self.assertEqual((t["kind"], t["dir"], t["beyond"], t["eta_tf"]), ("candle_close", "above", True, "5м"))

    def test_chase_blocked_card_reports_distance_and_tolerance(self):
        s = gen.spec("PL-1028-SOL-23", "SOL", "long", NOW, entry={"when": {"kind": "candle_close", "tf": "5m", "dir": "above", "level": 142.0}, "how": {"type": "market"},
                                                                  "ref_price": 142.0, "valid": {"ttl_min": 30}})
        self.put("state/plans/PL-1028-SOL-23/spec.json", s)
        self.put("state/plans/PL-1028-SOL-23/runtime.json", gen.rt("PL-1028-SOL-23", "armed", NOW, entry={"state": "blocked", "blocked_why": "CHASE", "attempts": 0}))
        c = self.card("PL-1028-SOL-23")
        self.assertEqual(c["blocked_why"], "CHASE")
        self.assertAlmostEqual(c["chase_bps"], (142.31 - 142.0) / 142.0 * 1e4, places=0)
        self.assertEqual(c["chase_max_bps"], 15.0)

    def test_latched_leaf_counts_as_met(self):
        self.assertEqual(self.card(P1)["trigger"]["leaf_n"], 2)
        # c1 = объёмное условие защёлкнуто исполнителем -> не «не хватает»
        self.assertEqual(self.card(P1)["trigger"]["missing"].__len__(), 1)

    def test_price_beyond_met_by_price(self):
        s = gen.spec("PL-1028-SOL-21", "SOL", "long", NOW, entry={"when": {"kind": "price_beyond", "dir": "above", "level": 142.0}, "how": {"type": "market"}, "valid": {"ttl_min": 30}})
        self.put("state/plans/PL-1028-SOL-21/spec.json", s)
        self.put("state/plans/PL-1028-SOL-21/runtime.json", gen.rt("PL-1028-SOL-21", "armed", NOW))
        t = self.card("PL-1028-SOL-21")["trigger"]
        self.assertEqual((t["met_n"], t["dist_pct"]), (1, None))

    def test_ttl_and_guard_preview_fields(self):
        c = self.card(P1)
        self.assertEqual((c["risk_usdt"], c["net_rr"], c["leverage"]), (0.46, 1.2, 14))
        self.assertEqual(c["exp"], "sweep-reject-v1")
        self.assertEqual(c["var"], "A")
        self.assertTrue(0 < c["ttl_left_s"] <= 45 * 60)
        self.assertEqual(self.card(P3)["mode"], "paper")
        self.assertIn("W_NO_INVALIDATION", self.card(P3)["warnings"])

    def test_draft_without_runtime(self):
        c = self.card("PL-1028-ADA-97")
        self.assertEqual((c["status"], c["col"]), ("draft", "watching"))

    def test_entry_text_human_readable(self):
        self.assertIn("5м прокол 142.50 и закрытие обратно", self.card(P1)["entry_text"])
        self.assertIn("объём 5м ≥ x1.8", self.card(P1)["entry_text"])
        self.assertIn("по порядку", self.card(P3)["entry_text"])

    def test_describe_cond_variants(self):
        d = model2.describe_cond
        self.assertEqual(d({"kind": "price_beyond", "dir": "below", "level": 100}), "цена ниже 100.00")
        self.assertIn("(3 подряд)", d({"kind": "candle_close", "tf": "15m", "dir": "above", "level": 10, "n": 3}))
        self.assertEqual(d({"any": [{"kind": "elapsed", "min": 5}, {"not": {"kind": "spread_bps", "lte": 6}}]}), "прошло ≥ 5 мин или не спред ≤ 6 бп")
        self.assertIn("R", d({"kind": "pnl_r", "gte": 1.0}))
        self.assertEqual(d({"kind": "target_filled", "target": "t1"}), "цель t1 исполнена")
        self.assertEqual(d("мусор"), "?")
        self.assertEqual(d({"kind": "ВЗЛОМ<script>"}), "ВЗЛОМ<script>")      # неизвестное — просто текст (страница вставит через textContent)

    def test_text_is_cleaned_and_truncated(self):
        s = gen.spec("PL-1028-SOL-22", "SOL", "long", NOW, thesis="тезис\x00\x07 с мусором " + "я" * 5000)
        self.put("state/plans/PL-1028-SOL-22/spec.json", s)
        self.put("state/plans/PL-1028-SOL-22/runtime.json", gen.rt("PL-1028-SOL-22", "armed", NOW))
        th = self.card("PL-1028-SOL-22")["thesis"]
        self.assertLessEqual(len(th), 280)
        self.assertNotIn("\x00", th)

    def test_huge_and_nonfinite_numbers(self):
        s = gen.spec("PL-1028-SOL-23", "SOL", "long", NOW, stop=10 ** 400, entry={"when": {"kind": "price_beyond", "dir": "above", "level": 1e999}, "how": {"type": "market"}})
        self.put("state/plans/PL-1028-SOL-23/spec.json", json.dumps(s).replace("1e+999", "1e999"), raw=True)
        self.put("state/plans/PL-1028-SOL-23/runtime.json", gen.rt("PL-1028-SOL-23", "armed", NOW))
        c = self.card("PL-1028-SOL-23")
        self.assertIsNone(c["stop"])
        self.assertIsNone(c["trigger"]["dist_pct"])

    def test_bad_files_are_skipped_not_fatal(self):
        self.put("state/plans/PL-1028-SOL-24/spec.json", "[1, 2]", raw=True)
        self.put("state/plans/PL-1028-SOL-25/spec.json", gen.spec("PL-1028-SOL-25", "SOL", "long", NOW))
        self.put("state/plans/PL-1028-SOL-25/runtime.json", "[]", raw=True)            # runtime не словарь -> {}
        self.put("state/plans/not_a_plan/spec.json", {"symbol": "X"})
        self.put("state/plans/_archive/PL-1020-SOL-01/spec.json", gen.spec("PL-1020-SOL-01", "SOL", "long", NOW))
        v = self.v2()
        self.assertEqual(v["plans"]["broken"], 2)
        ids = {c["id"] for c in v["plans"]["cards"]}
        self.assertIn("PL-1028-SOL-25", ids)
        self.assertNotIn("PL-1020-SOL-01", ids)
        self.assertNotIn("not_a_plan", ids)

    def test_plans_endpoint_filters(self):
        _, o = api("/api/v2/plans", {"status": ["armed"], "mode": ["paper"]}, self.root)
        self.assertEqual([c["id"] for c in o["plans"]], [P3])
        _, o = api("/api/v2/plans", {"status": ["closed,cancelled"]}, self.root)
        self.assertEqual({c["status"] for c in o["plans"]}, {"closed", "cancelled"})


class TestPositions(Fx):
    def pos(self, pid=P4):
        return [p for p in self.v2()["positions"] if p["plan"] == pid][0]

    def test_exchange_stop_and_r(self):
        p = self.pos()
        self.assertEqual((p["cur_stop"], p["stop_src"], p["initial_stop"]), (28.10, "биржа", 27.80))
        self.assertAlmostEqual(p["r_now"], (28.26 - 28.10) / 0.30, places=2)
        self.assertAlmostEqual(p["mfe_r"], (28.49 - 28.10) / 0.30, places=2)
        self.assertAlmostEqual(p["mae_r"], (27.95 - 28.10) / 0.30, places=2)
        self.assertEqual(p["journal_id"], "BB-M-AVAX-1028-101502")

    def test_rule_states_codes_and_details(self):
        r = {x["id"]: x for x in self.pos()["rules"]}
        self.assertEqual((r["be_1r"]["state"], r["be_1r"]["detail"]), ("done", "27.950 → 28.100"))
        self.assertEqual((r["pyr"]["state"], r["pyr"]["code"]), ("skipped", "ADD_DISABLED"))
        self.assertEqual(r["lock_t1"]["state"], "waiting")
        self.assertIn("ждёт исполнения t1", r["lock_t1"]["need"])
        self.assertIn("нужно ≥+1.5R", r["trail"]["need"])
        self.assertIn("сейчас +0.53R", r["trail"]["need"])
        self.assertEqual(r["trail"]["code"], "NOT_TIGHTER")

    def test_next_stop_is_tighter_target_of_waiting_rule(self):
        n = self.pos()["next_stop"]
        self.assertEqual(n["rule"], "lock_t1")
        self.assertAlmostEqual(n["to"], 28.10 + 0.5 * 0.30, places=4)

    def test_next_stop_never_widens(self):
        h = json.load(open(os.path.join(self.root, "runtime", "exec_snapshot.json")))
        h["positions"][0]["sl"] = 28.40                            # стоп уже выше цели правила
        self.put("runtime/exec_snapshot.json", h)
        self.assertIsNone(self.pos()["next_stop"])

    def test_targets_and_history(self):
        p = self.pos()
        self.assertEqual([(t["id"], t["state"]) for t in p["targets"]], [("t1", "resting"), ("t2", "pending")])
        self.assertEqual(len(p["history"]), 10)
        self.assertGreater(p["history"][0]["t"], p["history"][-1]["t"])        # новые сверху
        self.assertTrue(any(h["denied"] for h in p["history"]))

    def test_paper_position_without_snapshot_uses_runtime(self):
        p = self.pos(P5)
        self.assertEqual((p["stop_src"], p["cur_stop"], p["mode"]), ("runtime", 14.30, "paper"))
        self.assertIsNone(p["snap_age_s"])
        self.assertAlmostEqual(p["next_stop"]["to"], 14.52, places=2)

    def test_max_hold_left(self):
        self.assertTrue(0 < self.pos()["hold_left_s"] <= 240 * 60)

    def test_position_with_missing_pos_block(self):
        self.put("state/plans/%s/runtime.json" % P5, gen.rt(P5, "managing", NOW, paper=True))
        p = self.pos(P5)
        self.assertIsNone(p["r_now"])

    def test_foreign_position_listed_not_planned(self):
        sn = json.load(open(os.path.join(self.root, "runtime", "exec_snapshot.json")))
        sn["positions"].append({"sym": "ETH", "side": "short", "size": 0.1, "avg": 2300, "mark": 2310, "sl": 0, "owner": "legacy"})
        self.put("runtime/exec_snapshot.json", sn)
        self.assertEqual([p["sym"] for p in self.v2()["foreign_positions"]], ["ETH"])


class TestDecisions(Fx):
    def feed(self, **q):
        return api("/api/v2/decisions", {k: [str(v)] for k, v in q.items()}, self.root)[1]

    def test_newest_first_and_broken_tail_skipped(self):
        o = self.feed()
        seqs = [d["seq"] for d in o["decisions"]]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertEqual(o["total"], 21)
        self.assertEqual(o["last_seq"], seqs[0])

    def test_filter_plan_actor_kind(self):
        self.assertTrue(all(d["plan"] == P4 for d in self.feed(plan=P4)["decisions"]))
        self.assertTrue(all(d["actor"].startswith("brain") for d in self.feed(actor="brain")["decisions"]))
        self.assertTrue(all(d["actor"] == "owner" for d in self.feed(actor="owner")["decisions"]))
        self.assertEqual({d["kind"] for d in self.feed(kind="skip")["decisions"]}, {"skip"})
        self.assertTrue(all(d["what"] == "set_stop" for d in self.feed(kind="set_stop")["decisions"]))

    def test_only_fail_shows_refusals_and_skips(self):
        rows = self.feed(only="fail")["decisions"]
        self.assertTrue(rows)
        self.assertTrue(all(d["denied"] or d["noop"] for d in rows))
        self.assertIn("E_STOP_WIDEN", {d["code"] for d in rows})
        self.assertIn("NOT_TIGHTER", {d["code"] for d in rows})

    def test_mode_filter_uses_plan_mode(self):
        self.assertEqual({d["mode"] for d in self.feed(mode="paper")["decisions"]}, set())
        self.assertEqual({d["mode"] for d in self.feed(mode="live")["decisions"]} - {None}, {"live"})

    def test_after_before_and_limit(self):
        o = self.feed()
        mid = o["decisions"][5]["seq"]
        self.assertTrue(all(d["seq"] > mid for d in self.feed(after=mid)["decisions"]))
        self.assertTrue(all(d["seq"] < mid for d in self.feed(before=mid)["decisions"]))
        self.assertEqual(len(self.feed(limit=3)["decisions"]), 3)
        self.assertLessEqual(len(self.feed(limit=999999)["decisions"]), model2.DEC_LIMIT)
        self.assertEqual(len(self.feed(limit="abc")["decisions"]), 21)

    def test_action_text_for_stop_move_and_entry(self):
        rows = {d["what"] + ":" + d["kind"]: d for d in self.feed(plan=P4)["decisions"]}
        self.assertEqual(rows["set_stop:result"]["action"], "стоп 27.950 → 27.950".replace("27.950 → 27.950", "27.800 → 27.950"))
        self.assertIn("вход 1.5 @ 28.100", rows["place_entry:result"]["action"])

    def test_result_flags_and_links(self):
        rows = {(d["what"], d["kind"]): d for d in self.feed(plan=P4)["decisions"]}
        ok = rows[("place_entry", "result")]
        self.assertTrue(ok["ok"] and not ok["denied"])
        self.assertEqual((ok["link_id"], ok["exch_ms"]), ("tbx-4-001", 240))
        cmd = rows[("submit", "cmd")]
        self.assertEqual(cmd["brief_id"], "B-20261028-1015-02")
        self.assertTrue(rows[("amend", "reject")]["denied"])
        self.assertTrue(rows[("move_stop", "skip")]["noop"])

    def test_day_summary(self):
        d = self.feed()["day"]
        self.assertEqual(d["brain"], 5)
        self.assertEqual(d["rejects"], 5)
        codes = {c["code"]: c["n"] for c in d["reject_codes"]}
        self.assertEqual((codes["E_STOP_WIDEN"], codes["DENY_GUARD"], codes["DENY_SPREAD"]), (2, 1, 1))
        self.assertEqual(d["protect"], 1)

    def test_garbage_lines_and_missing_file(self):
        self.put("state/decisions.jsonl", '{"seq": 1, "actor": "exec", "kind": "note", "what": "note"}\nмусор\n[1]\n{"seq": "x"}\n{"seq": 2, "why": ' + json.dumps("\x01" * 10) + '}\n', raw=True)
        o = self.feed()
        self.assertEqual([d["seq"] for d in o["decisions"]], [2, 1])
        os.remove(os.path.join(self.root, "state", "decisions.jsonl"))
        model._CACHE.clear()
        self.assertEqual(self.feed()["decisions"], [])

    def test_text_fields_are_bounded(self):
        self.put("state/decisions.jsonl", json.dumps({"seq": 1, "actor": "brain:chat", "why": "я" * 9999, "args": {"x": "я" * 9999, "y": [1] * 999}, "res": {"code": "К" * 999}}) + "\n", raw=True)
        d = self.feed()["decisions"][0]
        self.assertLessEqual(len(d["why"]), 240)
        self.assertLessEqual(len(d["args"]["x"]), 80)
        self.assertLessEqual(len(d["code"]), 40)


class TestBriefAndEvents(Fx):
    def test_brief_last_and_by_id(self):
        code, o = api("/api/v2/brief", {}, self.root)
        self.assertEqual((code, o["id"], o["total"]), (200, "B-20261028-1100-03", 2))
        code, o = api("/api/v2/brief", {"id": ["B-20261028-1015-02"]}, self.root)
        self.assertEqual(o["brief"]["scope"], "start")
        self.assertEqual(o["brief"]["levels"]["SOL"]["day"]["hi"], 143.6)

    def test_brief_bad_and_missing(self):
        self.assertEqual(api("/api/v2/brief", {"id": ["../../etc/passwd"]}, self.root)[0], 400)
        code, o = api("/api/v2/brief", {"id": ["B-20990101-0000-01"]}, self.root)
        self.assertEqual((code, o["available"]), (200, False))
        shutil.rmtree(os.path.join(self.root, "state", "briefs"))
        code, o = api("/api/v2/brief", {}, self.root)
        self.assertEqual((o["available"], o["ids"]), (False, []))

    def test_brief_whitelist_scrub_and_own_thesis(self):
        b = json.load(open(os.path.join(self.root, "state", "briefs", "B-20261028-1015-02.json")))
        b["internal_dump"] = "ОПАСНО"
        b["plans"][0]["own_thesis"] = "т" * 900
        b["plans"][0]["token_hint"] = "SENTINEL_X"
        self.put("state/briefs/B-20261028-1015-02.json", b)
        o = api("/api/v2/brief", {"id": ["B-20261028-1015-02"]}, self.root)[1]
        self.assertNotIn("internal_dump", o["brief"])
        self.assertNotIn("api_key", json.dumps(o))
        self.assertNotIn("SENTINEL", json.dumps(o))
        self.assertLessEqual(len(o["brief"]["plans"][0]["own_thesis"]), 160)

    def test_broken_brief_file(self):
        self.put("state/briefs/B-20261028-1200-04.json", "{не json", raw=True)
        o = api("/api/v2/brief", {}, self.root)[1]
        self.assertFalse(o["available"])
        self.assertEqual(o["missing"], "B-20261028-1200-04")

    def test_events_feed_after_and_limit(self):
        o = api("/api/v2/events", {"limit": ["3"]}, self.root)[1]
        self.assertEqual([e["seq"] for e in o["events"]], [1854, 1853, 1852])
        o = api("/api/v2/events", {"after": ["1852"]}, self.root)[1]
        self.assertEqual([e["seq"] for e in o["events"]], [1854, 1853])

    def test_wake_events_in_state_only_wake(self):
        w = self.v2()["wake_events"]
        self.assertTrue(w and all(e["pri"] == "wake" for e in w))


class TestPlanDetail(Fx):
    def test_detail_has_spec_runtime_revs_and_decisions(self):
        code, o = api("/api/v2/plan", {"id": [P2]}, self.root)
        self.assertEqual(code, 200)
        self.assertEqual(o["spec"]["rev"], 2)
        self.assertEqual([r["rev"] for r in o["revs"]], [1, 2])
        self.assertEqual(o["revs"][1]["changes"], ["stop.price: 2358.0 → 2362.0"])
        code, o = api("/api/v2/plan", {"id": [P4]}, self.root)
        self.assertEqual(o["runtime"]["pos"]["initial_stop"], 27.8)
        self.assertGreaterEqual(len(o["decisions"]), 10)
        self.assertTrue(all(d["plan"] == P4 for d in o["decisions"]))

    def test_detail_errors_and_traversal(self):
        self.assertEqual(api("/api/v2/plan", {"id": ["PL-0000-NONE-01"]}, self.root)[0], 404)
        for bad in ("../state", "PL-1/../../x", "", "PL-" + "a" * 80, "/etc/passwd"):
            self.assertEqual(api("/api/v2/plan", {"id": [bad]}, self.root)[0], 400, bad)

    def test_revs_with_patch_rows(self):
        self.put("state/plans/%s/revs.jsonl" % P1, json.dumps({"rev": 2, "ts": gen.iso(NOW), "reason": "сдвиг", "patch": {"stop": {"price": 143.2}}}) + "\nмусор\n", raw=True)
        o = api("/api/v2/plan", {"id": [P1]}, self.root)[1]
        self.assertEqual(o["revs"][0]["changes"], ["stop.price → 143.2"])
        self.assertEqual(o["revs"][0]["why"], "сдвиг")


class TestExperiments(Fx):
    def exp(self, **q):
        return api("/api/v2/exp", {k: [v] for k, v in q.items()}, self.root)[1]

    def test_by_variant_stars_and_k(self):
        o = self.exp(by="variant", mode="paper")
        rows = {r["name"]: r for r in o["rows"]}
        self.assertEqual((rows["sweep-reject-v1/A"]["n"], rows["sweep-reject-v1/A"]["star"]), (33, False))
        self.assertEqual((rows["sweep-reject-v1/B"]["n"], rows["sweep-reject-v1/B"]["star"]), (3, True))
        self.assertEqual(o["k"], 2)
        self.assertAlmostEqual(o["alpha"], 0.025)
        self.assertEqual(o["meta"]["sweep-reject-v1"]["min_n"], 30)

    def test_group_stats_fields(self):
        g = self.exp(by="exp", mode="paper")["rows"][0]
        for k in ("n", "winrate", "avg_r", "r_se", "pf", "fees", "fees_share", "mae_win", "mfe_loss", "by_rule_share", "plan_n", "modes"):
            self.assertIn(k, g)
        self.assertEqual(g["modes"], ["paper"])

    def test_mode_filter_and_other_cuts(self):
        self.assertEqual(self.exp(by="exp", mode="live")["rows"], [])        # плановых live-сделок с exp в фикстуре нет закрытых
        self.assertEqual({r["name"] for r in self.exp(by="mode", mode="all")["rows"]}, {"live", "paper"})
        self.assertTrue(self.exp(by="setup", mode="paper")["rows"])
        self.assertEqual(self.exp(by="bogus")["by"], "exp")

    def test_no_data(self):
        os.remove(os.path.join(self.root, "state", "trades.jsonl"))
        o = self.exp()
        self.assertEqual((o["rows"], o["k"], o["alpha"]), ([], 0, None))


class TestJournalV2Events(unittest.TestCase):
    """add/part/adjust (06_contracts 6.10) не считаются битыми строками и корректно меняют позицию."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbdash2j_")
        os.makedirs(os.path.join(self.root, "state"))
        model._CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def jl(self, rows):
        with open(os.path.join(self.root, "state", "trades.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        model._CACHE.clear()

    OPEN = {"ev": "open", "id": "T1", "ts_open": "2026-10-27T12:00:00Z", "sym": "SOL", "side": "long", "entry": 100.0, "lev": 10.0, "margin": 2.0, "notional": 20.0,
            "sl": 99.0, "tp": 103.0, "risk_usdt": 0.2, "fee_open": 0.01, "plan_id": "PL-1-SOL-01", "exp": "e1", "var": "A", "venue": "bybit"}

    def test_add_recomputes_average_size_margin_keeps_risk(self):
        self.jl([self.OPEN, {"ev": "add", "id": "T1", "ts": "2026-10-27T12:05:00Z", "px": 102.0, "qty": 0.1, "fee": 0.005}])
        tr, w = model.load_trades(self.root)
        t = tr[0]
        self.assertEqual(sum(w.values()), 0)
        self.assertAlmostEqual(t["qty"], 0.3)
        self.assertAlmostEqual(t["entry"], (0.2 * 100 + 0.1 * 102) / 0.3)
        self.assertAlmostEqual(t["notional"], 0.3 * t["entry"])
        self.assertAlmostEqual(t["margin"], 2.0 + 0.1 * 102 / 10)
        self.assertEqual(t["risk_usdt"], 0.2)                      # R относится к исходному риску
        self.assertAlmostEqual(t["fee_open"], 0.015)
        self.assertEqual(len(t["adds"]), 1)

    def test_partial_close_reduces_position_and_accumulates_realized(self):
        self.jl([self.OPEN, {"ev": "part", "id": "T1", "ts": "2026-10-27T12:05:00Z", "px": 101.0, "qty": 0.1, "fee": 0.004, "pnl": 0.1}])
        t = model.load_trades(self.root)[0][0]
        self.assertAlmostEqual(t["qty"], 0.1)
        self.assertAlmostEqual(t["notional"], 10.0)
        self.assertAlmostEqual(t["margin"], 1.0)
        self.assertEqual(t["realized_parts"], 0.1)
        self.assertAlmostEqual(t["fee_open"], 0.005)                  # комиссия входа (0.01) делится по долям: остаток несёт половину
        self.assertAlmostEqual(t["fees_parts"], 0.004 + 0.005)        # вторая половина уходит в зафиксированное, сумма комиссий не меняется
        self.assertEqual(t["status"], "open")

    def test_open_pnl_includes_parts(self):
        self.jl([self.OPEN, {"ev": "part", "id": "T1", "ts": "2026-10-27T12:05:00Z", "px": 101.0, "qty": 0.1, "fee": 0.004, "pnl": 0.1}])
        os.makedirs(os.path.join(self.root, "runtime"))
        with open(os.path.join(self.root, "runtime", "prices.json"), "w") as f:
            json.dump({"ts_epoch": NOW, "prices": {"SOL": 101.0}}, f)
        st = model.build_state(self.root, "all", "all", NOW)
        t = st["open_trades"][0]
        self.assertAlmostEqual(st["open_pnl"], t["calc"]["pnl"] + 0.1 - 0.009)
        self.assertEqual(t["calc"]["realized_parts"], 0.1)
        # итог не зависит от распределения комиссии входа: остаток + зафиксированное = как при полной комиссии на остатке
        self.assertAlmostEqual(t["calc"]["pnl"] + 0.1 - 0.009, (t["calc"]["pnl"] - 0.005) + 0.1 - 0.004)

    def test_adjust_keeps_initial_stop(self):
        self.jl([self.OPEN, {"ev": "adjust", "id": "T1", "ts": "2026-10-27T12:06:00Z", "sl": 100.0, "tp": 104.0, "by": "rule"},
                 {"ev": "adjust", "id": "T1", "ts": "2026-10-27T12:07:00Z", "sl": -5, "tp": "x", "by": "cmd"}])
        t = model.load_trades(self.root)[0][0]
        self.assertEqual((t["sl"], t["tp"], t["sl0"]), (100.0, 104.0, 99.0))
        self.assertEqual(len(t["adjusts"]), 2)

    def test_close_after_events_and_plan_fields_survive(self):
        self.jl([self.OPEN, {"ev": "add", "id": "T1", "px": 101, "qty": 0.1, "fee": 0}, {"ev": "part", "id": "T1", "px": 102, "qty": 0.1, "fee": 0, "pnl": 0.2},
                 {"ev": "close", "id": "T1", "ts_close": "2026-10-27T12:30:00Z", "exit": 103.0, "reason": "tp", "pnl_usdt": 0.9, "fees_usdt": 0.05}])
        t = model.load_trades(self.root)[0][0]
        self.assertEqual((t["status"], t["pnl_usdt"], t["plan_id"], t["exp"]), ("closed", 0.9, "PL-1-SOL-01", "e1"))

    def test_events_for_unknown_or_closed_trade(self):
        self.jl([{"ev": "add", "id": "NOPE", "px": 1, "qty": 1}, self.OPEN,
                 {"ev": "close", "id": "T1", "ts_close": "2026-10-27T12:30:00Z", "exit": 103.0, "reason": "tp", "pnl_usdt": 0.9},
                 {"ev": "part", "id": "T1", "px": 1, "qty": 1, "pnl": 5}, {"ev": "mystery", "id": "T1"}])
        tr, w = model.load_trades(self.root)
        self.assertEqual((w["orphan_close"], w["bad_rows"]), (1, 1))
        self.assertIsNone(tr[0].get("realized_parts"))

    def test_garbage_values_in_events(self):
        self.jl([self.OPEN, {"ev": "add", "id": "T1", "px": "abc", "qty": None}, {"ev": "part", "id": "T1", "px": 0, "qty": -1, "pnl": "x"},
                 {"ev": "add", "id": "T1", "px": 10 ** 400, "qty": 1}])
        t = model.load_trades(self.root)[0][0]
        self.assertEqual(t["entry"], 100.0)

    def test_breakdown_by_experiment_keys(self):
        self.jl([self.OPEN, {"ev": "close", "id": "T1", "ts_close": "2026-10-27T12:30:00Z", "exit": 103.0, "reason": "tp", "pnl_usdt": 0.9}])
        bd = model.build_state(self.root, "all", "all", NOW)["breakdown"]
        self.assertEqual((bd["exp"][0]["name"], bd["var"][0]["name"], bd["setup"][0]["name"]), ("e1", "A", "-"))


class TestApiAndServer(Fx):
    OLD_KEYS = {"now", "now_iso", "filter", "account", "session", "sessions", "learning", "open_trades", "open_pnl", "pending", "price_feed", "metrics", "curve", "breakdown",
                "summary", "risk_room", "status", "history", "history_total", "market", "hot", "ideas", "events", "health", "warnings"}

    def test_old_state_keys_unchanged_and_v2_added(self):
        st = api("/api/state", {"mode": ["all"]}, self.root)[1]
        self.assertTrue(self.OLD_KEYS <= set(st))
        self.assertEqual(set(st) - self.OLD_KEYS, {"v2"})
        for k in ("strip", "plans", "positions", "decisions_day", "pauses", "wake_events", "last_decisions"):
            self.assertIn(k, st["v2"])

    def test_v2_failure_does_not_break_old_state(self):
        orig = model2.v2_state
        model2.v2_state = lambda *a, **k: 1 / 0
        try:
            st = api("/api/state", {}, self.root)[1]
        finally:
            model2.v2_state = orig
        self.assertEqual(st["v2"]["available"], False)
        self.assertTrue(self.OLD_KEYS <= set(st))

    def test_unknown_v2_endpoint_404(self):
        self.assertEqual(api("/api/v2/nope", {}, self.root)[0], 404)

    def test_legacy_endpoints_still_work(self):
        for p in ("/api/market", "/api/hot", "/api/health", "/api/trades", "/api/ideas", "/api/ping"):
            self.assertEqual(api(p, {}, self.root)[0], 200, p)

    def test_real_empty_root_gives_empty_states(self):
        empty = tempfile.mkdtemp(prefix="tbdash2e_")
        try:
            for p in ("/api/v2/state", "/api/v2/plans", "/api/v2/decisions", "/api/v2/brief", "/api/v2/events", "/api/v2/exp"):
                code, o = api(p, {}, empty)
                self.assertEqual(code, 200, p)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_http_get_ok_post_405_host_403_and_static(self):
        srv = server.make_server(0, self.root)
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            for p in ("/api/v2/state", "/api/v2/plans?status=armed", "/api/v2/plan?id=" + P1, "/api/v2/decisions?only=fail&limit=5", "/api/v2/brief", "/api/v2/events", "/api/v2/exp?by=variant&mode=paper"):
                r = opener.open(base + p, timeout=5)
                self.assertEqual(r.status, 200, p)
                json.loads(r.read().decode("utf-8"))
            for n in ("v-strip.js", "v-plans.js", "v-decisions.js", "v-brief.js", "v-exp.js"):
                self.assertEqual(opener.open(base + "/" + n, timeout=5).status, 200, n)
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                req = urllib.request.Request(base + "/api/v2/state", data=b"{}", method=method)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    opener.open(req, timeout=5)
                self.assertEqual(cm.exception.code, 405)
            req = urllib.request.Request(base + "/api/v2/state", headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                opener.open(req, timeout=5)
            self.assertEqual(cm.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                opener.open(base + "/api/v2/plan?id=../../state", timeout=5)
            self.assertEqual(cm.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()


class TestSafety(Fx):
    def test_no_secret_in_any_v2_response(self):
        sec = "SENTINEL_SECRET_999"
        s = gen.spec("PL-1028-SOL-30", "SOL", "long", NOW)
        s["api_key"] = sec
        s["tags"]["token_x"] = sec
        self.put("state/plans/PL-1028-SOL-30/spec.json", s)
        self.put("state/plans/PL-1028-SOL-30/runtime.json", dict(gen.rt("PL-1028-SOL-30", "armed", NOW), secret=sec))
        self.put("state/decisions.jsonl", json.dumps({"seq": 1, "actor": "exec", "plan": "PL-1028-SOL-30", "kind": "note", "what": "note", "args": {"api_secret": sec}, "res": {"password": sec}}) + "\n", raw=True)
        h = json.load(open(os.path.join(self.root, "runtime", "exec_health.json")))
        h["api_key"] = sec
        self.put("runtime/exec_health.json", h)
        for p, q in (("/api/state", {"mode": ["all"]}), ("/api/v2/state", {}), ("/api/v2/plans", {}), ("/api/v2/plan", {"id": ["PL-1028-SOL-30"]}), ("/api/v2/decisions", {}),
                     ("/api/v2/brief", {}), ("/api/v2/events", {}), ("/api/v2/exp", {})):
            self.assertNotIn(sec, json.dumps(api(p, q, self.root)[1], ensure_ascii=False), p)

    def test_model2_source_is_read_only_and_isolated(self):
        src = open(model2.__file__, encoding="utf-8").read()
        for bad in ("/.config", "launcher", "BYBIT_API", "urlopen", "tb.venues", "tb.executor", "tb.brain", "tb.logic", "os.environ", "expanduser", "subprocess", "socket",
                    "os.remove", "os.replace", "os.rename", "shutil", ".write(", "os.makedirs", "unlink"):
            self.assertNotIn(bad, src, bad)
        self.assertIsNone(re.search(r"open\([^)]*[\"'][wax+]b?[\"']", src))

    def test_server_has_no_write_handlers(self):
        src = open(server.__file__, encoding="utf-8").read()
        # WP12 (решение владельца 2026-10-28): POST разрешён только под /api/ctl/*, остальные методы по-прежнему 405
        self.assertIn("do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny", src)
        self.assertIn('not path.startswith("/api/ctl/")', src)
        self.assertEqual(src.count("def do_POST"), 1)

    def test_scripts_listed_and_included(self):
        html = open(os.path.join(server.STATIC, "index.html"), encoding="utf-8").read()
        for n in ("v-strip.js", "v-plans.js", "v-decisions.js", "v-brief.js", "v-exp.js"):
            self.assertIn(n, server.SCRIPTS)
            self.assertIn('src="/%s"' % n, html)
            self.assertTrue(os.path.exists(os.path.join(server.STATIC, n)))

    def test_static_js_uses_text_nodes_only(self):
        for n in server.SCRIPTS:
            txt = open(os.path.join(server.STATIC, n), encoding="utf-8").read()
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(bad, txt, "%s: %s" % (n, bad))
            self.assertNotIn("method:", txt.replace('method: "GET"', "").replace('method: "POST"' if n == "v-control.js" else "\0", ""), n)

    def test_page_has_no_control_buttons(self):
        html = open(os.path.join(server.STATIC, "index.html"), encoding="utf-8").read()
        self.assertNotIn("<form", html)
        self.assertNotIn("/api/kill", html)
        for n in server.SCRIPTS:
            txt = open(os.path.join(server.STATIC, n), encoding="utf-8").read()
            self.assertNotIn("/api/kill", txt)


class TestPerformance(unittest.TestCase):
    def test_200_plans_and_1000_decisions_under_300ms(self):
        root = tempfile.mkdtemp(prefix="tbdash2p_")
        try:
            gen.build(root, NOW, many=200)
            model._CACHE.clear()
            t = time.time()
            code, st = api("/api/v2/state", {}, root)
            dt1 = time.time() - t
            self.assertEqual(code, 200)
            self.assertGreaterEqual(st["plans"]["total"], 200)
            self.assertLessEqual(len(st["plans"]["cards"]), model2.PLANS_LIMIT)
            model._CACHE.clear()
            t = time.time()
            o = api("/api/v2/decisions", {"limit": ["200"]}, root)[1]
            dt2 = time.time() - t
            self.assertGreaterEqual(o["total"], 1000)
            self.assertEqual(len(o["decisions"]), 200)
            self.assertLess(dt1, 0.3, "state %.3f" % dt1)
            self.assertLess(dt2, 0.3, "decisions %.3f" % dt2)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_warm_cache_state_fast(self):
        root = tempfile.mkdtemp(prefix="tbdash2p_")
        try:
            gen.build(root, NOW, many=200)
            api("/api/state", {"mode": ["all"]}, root)
            t = time.time()
            api("/api/state", {"mode": ["all"]}, root)
            self.assertLess(time.time() - t, 0.3)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestKillScenario(Fx):
    build_kw = {"kill": True}

    def test_kill_flag_in_fixture(self):
        v = self.v2()
        self.assertIsNotNone(v["strip"]["kill"])
        self.assertTrue(v["strip"]["exec"]["present"])


if __name__ == "__main__":
    unittest.main()
