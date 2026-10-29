"""WP15b: периодический писатель случаев (memory.tick): закрытия, пропуски, итоги идей; идемпотентность, отсутствие дублей, final_only для идей, хук ctl."""
import json
import os
import shutil
import unittest

import _bootstrap  # noqa: F401
from _memory_helpers import FIX, TmpRoot
from tb.core import decisions
from tb.memory import cases, migrate, tick


def append(root, name, rows):
    with open(os.path.join(root, "state", name), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def closed_trade(tid, sym="ETH", mode="paper", r=0.8, t0="2026-10-28T10:00:00Z", t1="2026-10-28T10:30:00Z", **kw):
    op = {"ev": "open", "id": tid, "ts_open": t0, "sym": sym, "side": "long", "entry": 100.0, "sl": 99.0, "tp": 102.0, "mode": mode, "tags": ["setup:level_reclaim", "mem.on", "agent.api"]}
    op.update(kw)
    return [op, {"ev": "close", "id": tid, "ts_close": t1, "exit": 102.0, "reason": "tp", "pnl_usdt": 0.1, "r": r}]


class TestSync(TmpRoot):
    def setUp(self):
        TmpRoot.setUp(self)
        self.copy_fixtures()

    def count(self, **flt):
        return len([c for c in cases.load(self.root) if all(c.get(k) == v for k, v in flt.items())])

    def test_first_run_creates_cases_second_nothing(self):
        r1 = tick.sync_now(self.root, 1793200000.0)
        self.assertEqual(r1["written"], 13)
        r2 = tick.sync_now(self.root, 1793200100.0)
        self.assertEqual(r2["written"], 0)
        self.assertEqual(len(cases.load(self.root)), 13)
        self.assertTrue(cases.verify(self.root)["ok"])

    def test_closed_position_becomes_one_case_idempotently(self):
        tick.sync_now(self.root)
        n0 = len(cases.load(self.root))
        append(self.root, "trades.jsonl", closed_trade("BB-P-ETH-1028-1000"))
        self.assertEqual(tick.sync_now(self.root)["new"], {"trade": 1})
        for _ in range(3):
            self.assertEqual(tick.sync_now(self.root)["written"], 0)
        allc = cases.load(self.root)
        self.assertEqual(len(allc), n0 + 1)
        c = [x for x in allc if (x["refs"] or {}).get("journal_id") == "BB-P-ETH-1028-1000"]
        self.assertEqual(len(c), 1)
        c = c[0]
        self.assertEqual((c["type"], c["src"]["mode"], c["src"]["mem"], c["src"]["agent_id"]), ("trade", "paper", "on", "api"))
        self.assertEqual((c["result"]["status"], c["result"]["r"], c["situation"]["setup"]), ("closed", 0.8, "level_reclaim"))

    def test_open_position_not_recorded_until_close(self):
        tick.sync_now(self.root)
        op = closed_trade("BB-P-ETH-1028-1100")
        append(self.root, "trades.jsonl", [op[0]])
        self.assertEqual(tick.sync_now(self.root)["written"], 0)
        append(self.root, "trades.jsonl", [op[1]])
        self.assertEqual(tick.sync_now(self.root)["written"], 1)

    def test_no_money_or_keys_in_case(self):
        append(self.root, "trades.jsonl", closed_trade("BB-P-ETH-1028-1200", thesis="риск 5 USDT, ключ sk-ABCDEF0123456789ABCDEF012345", margin=4.0, notional=20.0))
        tick.sync_now(self.root)
        with open(cases.cases_path(self.root), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("sk-ABCDEF", raw)
        self.assertNotIn("USDT", raw)
        for k in ('"margin"', '"notional"', '"pnl_usdt"', '"equity"'):
            self.assertNotIn(k, raw)
        self.assertTrue(cases.verify(self.root)["ok"])

    def test_idea_recorded_only_when_final_then_whatif_added_once(self):
        tick.sync_now(self.root)
        n0 = len(cases.load(self.root))
        append(self.root, "ideas.jsonl", [{"ev": "idea", "id": "I90", "ts": "2026-10-28T09:00:00Z", "ts_epoch": 1793178000.0, "sym": "SOL", "side": "long", "note": "жду",
                                          "level": 150.0, "stop": 148.0, "tp": 155.0, "status": "watching", "src": "agent"}])
        self.assertEqual(tick.sync_now(self.root)["written"], 0)               # idea/watching ещё не решена: случая нет (decision неизменен после записи)
        self.assertEqual(len(cases.load(self.root)), n0)
        append(self.root, "ideas.jsonl", [{"ev": "mark", "id": "I90", "ts": "2026-10-28T10:00:00Z", "status": "expired"}])
        self.assertEqual(tick.sync_now(self.root)["new"], {"idea": 1})
        c = [x for x in cases.load(self.root) if (x["refs"] or {}).get("idea") == "I90"][0]
        self.assertEqual(c["decision"]["action"], "wait")
        self.assertIsNone(c["whatif"])
        append(self.root, "hot_outcomes.jsonl", [{"ts": "2026-10-28T10:20:00Z", "ts_epoch": 1793182800.0, "id": "I90", "kind": "idea", "type": "idea", "sym": "SOL", "dir": "up",
                                                   "h": 15, "p0": 150.0, "p1": 151.5, "ret_pct": 1.0, "dir_pct": 1.0, "cost_pct": 0.24, "side": "long"}])
        r = tick.sync_now(self.root)
        self.assertEqual(r["written"], 1)                                       # update-строка с whatif, не новый случай
        c = cases.get(c["case_id"], self.root)
        self.assertEqual(c["rev"], 2)
        self.assertEqual(c["whatif"]["h_min"], 15)
        self.assertEqual(tick.sync_now(self.root)["written"], 0)

    def test_skipped_idea_with_outcome_gets_case_and_whatif(self):
        append(self.root, "ideas.jsonl", [{"ev": "idea", "id": "I91", "ts": "2026-10-28T09:00:00Z", "sym": "ETH", "side": "short", "note": "н", "level": 2300.0, "stop": 2330.0,
                                          "status": "skipped", "src": "agent"}])
        append(self.root, "hot_outcomes.jsonl", [{"ts": "2026-10-28T09:20:00Z", "ts_epoch": 1793179200.0, "id": "I91", "kind": "idea", "type": "idea", "sym": "ETH", "dir": "down",
                                                   "h": 15, "p0": 2300.0, "p1": 2290.0, "ret_pct": -0.4, "dir_pct": 0.4, "cost_pct": 0.24, "side": "short"}])
        tick.sync_now(self.root)
        c = [x for x in cases.load(self.root) if (x["refs"] or {}).get("idea") == "I91"][0]
        self.assertEqual((c["type"], c["decision"]["action"]), ("idea", "skip"))
        self.assertAlmostEqual(c["whatif"]["net_dir_pct"], 0.16, places=2)

    def test_skip_decision_recorded_once(self):
        decisions.append({"actor": "brain:api", "kind": "skip", "what": "entry_skip", "why": "широкий стоп", "args": {"sym": "SOL", "side": "long", "skip_code": "WIDE_STOP"},
                          "plan": "PL-1"}, root=self.root)
        self.assertEqual(tick.sync_now(self.root)["new"].get("skip"), 1)
        self.assertEqual(tick.sync_now(self.root)["written"], 0)

    def test_final_only_migrate_vs_full(self):
        append(self.root, "ideas.jsonl", [{"ev": "idea", "id": "I92", "ts": "2026-10-28T09:00:00Z", "sym": "SOL", "side": "long", "note": "ж", "status": "watching"}])
        self.assertEqual(migrate.migrate(self.root, dry_run=True, final_only=True)["new"].get("idea"), 6)
        self.assertEqual(migrate.migrate(self.root, dry_run=True)["new"].get("idea"), 7)

    def test_parallel_writers_do_not_duplicate(self):
        append(self.root, "trades.jsonl", closed_trade("BB-P-ETH-1028-1300"))
        pl = migrate.plan(self.root, final_only=True)
        migrate.apply(pl, self.root)
        migrate.apply(pl, self.root)                  # второй писатель с тем же планом: дубли тихо пропускаются
        ids = [c["case_id"] for c in cases.load(self.root)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 14)

    def test_tick_throttle_and_failure_isolation(self):
        tick._LAST.clear()
        self.assertIsNotNone(tick.tick(self.root, 1000.0))
        self.assertIsNone(tick.tick(self.root, 1030.0))                     # < interval_s
        self.assertIsNotNone(tick.tick(self.root, 1061.0))
        self.assertIsNotNone(tick.tick(self.root, 1062.0, force=True))
        os.remove(os.path.join(self.root, "state", "trades.jsonl"))
        with open(os.path.join(self.root, "state", "ideas.jsonl"), "w", encoding="utf-8") as f:
            f.write("{не json\n")
        self.assertIsNotNone(tick.tick(self.root, 2000.0, force=True))     # битые источники не ломают шаг
        os.makedirs(os.path.join(self.root, "state", "cases.lock"), exist_ok=True)          # блокировка = каталог: запись невозможна, tick не бросает
        try:
            tick.tick(self.root, 3000.0, force=True)
        except Exception as e:      # pragma: no cover
            self.fail("tick не должен бросать: %r" % e)

    def test_disabled_by_config(self):
        with open(os.path.join(os.path.dirname(FIX), "..", "config", "memory.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["tick"] = {"enabled": False, "interval_s": 1}
        with open(os.path.join(self.root, "config", "memory.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        tick._LAST.clear()
        self.assertIsNone(tick.tick(self.root, 5000.0))
        self.assertFalse(os.path.exists(cases.cases_path(self.root)))


class TestCtlHook(unittest.TestCase):
    def test_service_calls_tick_in_loop(self):
        import inspect
        from tb.control import service
        src = inspect.getsource(service.run)
        self.assertIn("memtick.tick(root, clock())", src)
        self.assertIn("ctl.watch()", src)
        self.assertTrue(src.index("ctl.watch()") < src.index("memtick.tick"))


if __name__ == "__main__":
    unittest.main()
