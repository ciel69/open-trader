import json
import os
import unittest

import _bootstrap  # noqa: F401
from fakes.harness import Env, iso, plan_long
from tb.executor import protect

NOW = 1793200000.0


def tr(sym, reason, tclose, topen=None, mode="testnet"):
    return {"sym": sym, "reason": reason, "ts_close": iso(tclose), "ts_open": iso(topen if topen is not None else tclose - 600), "mode": mode}


PROT = {"symbol_cooldown_after_stop_min": 30, "pause_after_consecutive_stops": {"n": 2, "minutes": 60, "require_confirm": True},
        "max_entries_per_hour": 4, "max_entries_per_day": 12, "max_actions_per_symbol_per_10s": 4}


class ProtectPure(unittest.TestCase):
    def test_symbol_cooldown(self):
        t = [tr("SOL", "sl", NOW - 600)]
        self.assertIsNotNone(protect.symbol_cooldown(t, "SOL", NOW, 30))
        self.assertIsNone(protect.symbol_cooldown(t, "ETH", NOW, 30))
        self.assertIsNone(protect.symbol_cooldown(t, "SOL", NOW + 3000, 30))
        self.assertIsNone(protect.symbol_cooldown([tr("SOL", "tp", NOW - 600)], "SOL", NOW, 30))

    def test_paper_trades_ignored(self):
        self.assertIsNone(protect.symbol_cooldown([tr("SOL", "sl", NOW - 60, mode="paper")], "SOL", NOW, 30))

    def test_global_pause_after_two_stops(self):
        t = [tr("SOL", "sl", NOW - 1000), tr("ETH", "sl", NOW - 500)]
        gp = protect.global_pause(t, NOW, 2, 60)
        self.assertTrue(gp["active"])
        self.assertFalse(gp["confirm_needed"])
        gp = protect.global_pause(t, NOW + 4000, 2, 60)
        self.assertFalse(gp["active"])
        self.assertTrue(gp["confirm_needed"])
        t.append(tr("BTC", "tp", NOW - 100))
        self.assertEqual(protect.global_pause(t, NOW, 2, 60)["streak"], 0)

    def test_check_entry_codes(self):
        t = [tr("SOL", "sl", NOW - 600)]
        self.assertEqual(protect.check_entry(t, "SOL", NOW, PROT, True)[1], "COOLDOWN")
        ok = protect.check_entry([], "SOL", NOW, PROT, True)
        self.assertTrue(ok[0])
        self.assertEqual(protect.check_entry([], "SOL", NOW, PROT, False, size_mult=1.0)[1], "NO_CONFIRM")
        self.assertTrue(protect.check_entry([], "SOL", NOW, PROT, False, size_mult=0.5)[0])
        many = [tr("X%d" % i, "tp", NOW - 100 * i - 100, topen=NOW - 100 * i - 200) for i in range(4)]
        self.assertEqual(protect.check_entry(many, "SOL", NOW, PROT, True)[1], "LIMIT_HOUR")
        t2 = [tr("A", "sl", NOW - 1000), tr("B", "sl", NOW - 500)]
        self.assertEqual(protect.check_entry(t2, "SOL", NOW, PROT, True)[1], "COOLDOWN")
        self.assertEqual(protect.check_entry(t2, "SOL", NOW + 4000, PROT, False, size_mult=0.5)[1], "COOLDOWN")
        self.assertTrue(protect.check_entry(t2, "SOL", NOW + 4000, PROT, True)[0])

    def test_anomaly(self):
        day = [tr("A%d" % i, "tp", NOW - 1000 - i, topen=NOW - 3600 * 5 - i) for i in range(8)]
        self.assertTrue(protect.anomaly(day, NOW))
        week = day + [tr("W%d" % i, "tp", NOW - 90000, topen=NOW - 90000 - i * 3600) for i in range(40)]
        self.assertFalse(protect.anomaly(week, NOW))

    def test_pure_no_mutation(self):
        t = [tr("SOL", "sl", NOW - 600)]
        snap = json.dumps(t)
        protect.cooldowns(t, NOW, PROT)
        self.assertEqual(json.dumps(t), snap)


class ProtectEndToEnd(unittest.TestCase):
    def test_stop_then_cooldown_blocks_submit_and_survives_restart(self):
        e = Env()
        try:
            pid = e.enter()
            e.tick(98.9)             # стоп
            self.assertEqual(e.rt(pid)["close"]["reason"], "sl")
            r = e.submit()
            self.assertFalse(r["ok"])
            self.assertIn("E_COOLDOWN", [x["code"] for x in r["errors"]])
        finally:
            e.close()


if __name__ == "__main__":
    unittest.main()
