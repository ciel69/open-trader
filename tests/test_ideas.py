"""Журнал идей, форвард-доходность, издержки и статистика. Без сети, во временном TB_ROOT."""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import ideas
from tb.dash import model
from tb.marketdata import outcomes

NOW = 1793106000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbideas_")
        for d in ("state", "runtime", "config"):
            os.makedirs(os.path.join(self.root, d))
        self.old = os.environ.get("TB_ROOT")
        os.environ["TB_ROOT"] = self.root
        model._CACHE.clear()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = self.old
        shutil.rmtree(self.root, ignore_errors=True)

    def wjson(self, sub, name, obj):
        with open(os.path.join(self.root, sub, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)


class TestJournal(Base):
    def test_add_list_mark_price_sources(self):
        self.wjson("runtime", "hot.json", {"ts_epoch": NOW - 5, "top": [{"sym": "AAA", "price": 2.5}], "blocked": []})
        r = ideas.add_idea("aaa", "long", "жду пробой", level=2.6, stop=2.4, tp=2.9, tag="t", now=NOW)
        self.assertEqual((r["id"], r["price"], r["price_src"], r["status"]), ("I1", 2.5, "hot.json", "idea"))
        self.wjson("runtime", "market.json", {"ts_epoch": NOW - 1, "symbols": {"AAA": {"price": 2.6}}})
        r2 = ideas.add_idea("AAA", "watch", "ещё", status="watching", now=NOW + 1)
        self.assertEqual((r2["id"], r2["price"], r2["price_src"]), ("I2", 2.6, "market.json"))     # свежий market.json главнее
        self.assertIsNone(ideas.add_idea("ZZZ", "short", "нет цены", now=NOW)["price"])
        self.assertIsNone(ideas.mark_idea("I99", "taken", now=NOW))
        ideas.mark_idea("I1", "taken", note="вошли", trade="T-1", now=NOW + 5)
        by = {i["id"]: i for i in ideas.load_ideas()}
        self.assertEqual((by["I1"]["status"], by["I1"]["trade"], by["I1"]["mark_notes"]), ("taken", "T-1", ["вошли"]))
        self.assertEqual(by["I2"]["status"], "watching")
        line = ideas.line_of(by["I1"], ideas.price_book(now=NOW), NOW)
        self.assertIn("I1", line)
        self.assertIn("trade T-1", line)

    def test_stale_price_ignored_and_garbage_rows(self):
        self.wjson("runtime", "market.json", {"ts_epoch": NOW - 1000, "symbols": {"AAA": {"price": 2.6}}})
        self.assertIsNone(ideas.add_idea("AAA", "long", "x", now=NOW)["price"])
        with open(ideas.ideas_path(), "a", encoding="utf-8") as f:
            f.write("мусор\n{\"ev\": \"idea\"\n")
        self.assertEqual(len(ideas.load_ideas()), 1)

    def test_costs_from_profile(self):
        self.wjson("config", "risk_profile.json", {"costs": {"bybit": {"fee": 0.001, "slip": 0.0002, "maker_fee": 0.00036}}})
        c = ideas.costs()
        self.assertAlmostEqual(c["taker_rt_pct"], 0.24)
        self.assertAlmostEqual(c["maker_rt_pct"], 0.072)
        os.remove(os.path.join(self.root, "config", "risk_profile.json"))
        self.assertAlmostEqual(ideas.costs()["taker_rt_pct"], 0.24)


def tk(p):
    return {"AAAUSDT": {"price": p}}


class TestTracker(Base):
    def mk(self, **kw):
        return outcomes.OutcomeTracker(ideas.outcomes_path(), ideas.pending_path(), ideas.ideas_path(), kw.get("h", (15, 60)), 30.0, 0.24)

    def rows(self):
        return ideas.read_rows(ideas.outcomes_path())

    def test_forward_return_and_direction(self):
        t = self.mk()
        t.add_event({"id": "E1", "type": "range_5m", "sym": "AAA", "price": 100.0, "direction": "down", "range_5m_pct": 1.7, "score": 5}, NOW)
        t.add_event({"id": "E2", "type": "hot_enter", "sym": "AAA", "price": 100.0, "direction": None}, NOW)
        t.update(tk(99.0), NOW + 600)
        self.assertEqual(self.rows(), [])
        t.update(tk(99.0), NOW + 15 * 60 + 10)
        r = {(x["id"], x["h"]): x for x in self.rows()}
        self.assertAlmostEqual(r[("E1", 15)]["ret_pct"], -1.0)
        self.assertAlmostEqual(r[("E1", 15)]["dir_pct"], 1.0)          # шорт-направление: цена упала = продолжение в плюс
        self.assertIsNone(r[("E2", 15)]["dir_pct"])
        self.assertEqual(r[("E1", 15)]["cost_pct"], 0.24)
        t.update(tk(98.0), NOW + 3600 + 5)
        self.assertEqual(len([x for x in self.rows() if x["h"] == 60]), 2)
        self.assertEqual(t.items, {})

    def test_late_horizon_dropped_and_restart_resume(self):
        t = self.mk()
        t.add_event({"id": "E1", "type": "range_5m", "sym": "AAA", "price": 100.0, "direction": "up"}, NOW)
        t2 = self.mk()                                                 # рестарт демона: очередь из файла
        self.assertIn("E1", t2.items)
        t2.update(tk(101.0), NOW + 15 * 60 + 20)                       # 15м доехал после рестарта
        self.assertEqual([x["h"] for x in self.rows()], [15])
        t3 = self.mk()
        t3.update(tk(103.0), NOW + 3600 + 3000)                        # 60м опоздал: пропущен без записи
        self.assertEqual([x["h"] for x in self.rows()], [15])
        self.assertEqual(t3.items, {})

    def test_missing_price_waits_until_grace(self):
        t = self.mk(h=(15,))
        t.add_event({"id": "E1", "type": "range_5m", "sym": "AAA", "price": 100.0, "direction": "up"}, NOW)
        t.update({}, NOW + 15 * 60 + 5)
        self.assertEqual(self.rows(), [])
        t.update(tk(100.5), NOW + 15 * 60 + 35)
        self.assertEqual(len(self.rows()), 1)

    def test_agent_idea_tracked_once(self):
        i = ideas.add_idea("AAA", "short", "x", now=NOW)               # цены нет -> берётся из первого скана
        t = self.mk(h=(15,))
        t.update(tk(50.0), NOW + 30)
        self.assertIn("idea:" + i["id"], t.items)
        t.update(tk(49.0), NOW + 15 * 60 + 40)
        r = self.rows()
        self.assertEqual((r[0]["kind"], r[0]["side"]), ("idea", "short"))
        self.assertAlmostEqual(r[0]["dir_pct"], 2.0)
        self.mk(h=(15,)).update(tk(49.0), NOW + 15 * 60 + 70)           # повторный запуск идею не дублирует
        self.assertEqual(len(self.rows()), 1)


class TestStats(Base):
    def put(self, rows):
        with open(ideas.outcomes_path(), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    @staticmethod
    def o(i, h, dir_pct, **kw):
        r = {"id": i, "kind": "event", "type": "range_5m", "sym": "A", "dir": "up", "h": h, "dir_pct": dir_pct, "cost_pct": 0.24, "range_5m_pct": 1.7, "turn_surge_5m": 5.0}
        r.update(kw)
        return r

    def test_cells_net_and_buckets(self):
        self.put([self.o("E1", 15, 1.0), self.o("E2", 15, -0.5), self.o("E3", 15, 0.2, type="hot_enter", range_5m_pct=0.5), self.o("E4", 15, None)])
        st = ideas.compute_stats(self.root)
        g = {(x["dim"], x["name"]): x for x in st["groups"]}
        c = g[("все", "все события")]["h"]["15"]
        self.assertEqual(c["n"], 3)                                    # без направления в продолжение/откат не входит
        self.assertAlmostEqual(c["cont_gross"], (1.0 - 0.5 + 0.2) / 3)
        self.assertAlmostEqual(c["cont_net"], (1.0 - 0.5 + 0.2) / 3 - 0.24)
        self.assertAlmostEqual(c["cont_win"], 1 / 3)                   # плюс после издержек только E1
        self.assertAlmostEqual(c["fade_net"], -(1.0 - 0.5 + 0.2) / 3 - 0.24)
        self.assertEqual(g[("диапазон 5м", "1.5-2.5%")]["h"]["15"]["n"], 2)
        self.assertEqual(g[("диапазон 5м", "<1.0%")]["h"]["15"]["n"], 1)
        self.assertEqual(g[("оборот 5м", ">=4x")]["h"]["15"]["n"], 3)
        self.assertEqual(g[("тип", "hot_enter")]["h"]["15"]["n"], 1)
        self.assertEqual(g[("все", "все события")]["h"]["60"]["n"], 0)
        txt = ideas.format_stats(st)
        self.assertIn("n=3*", txt)                                     # предупреждение о малой выборке
        self.assertIn("Сравнений", txt)

    def test_ideas_and_agent_counters(self):
        a = ideas.add_idea("A", "long", "x", now=NOW)
        ideas.add_idea("B", "short", "y", status="skipped", now=NOW)
        ideas.mark_idea(a["id"], "taken", trade="T1", now=NOW)
        self.put([self.o(a["id"], 60, 0.5, kind="idea", type="idea", side="long")])
        st = ideas.compute_stats(self.root)
        self.assertEqual(st["agent"]["by_status"], {"taken": 1, "skipped": 1})
        self.assertAlmostEqual(st["agent"]["taken_share"], 0.5)
        self.assertEqual(st["idea_groups"][0]["h"]["60"]["n"], 1)
        self.assertAlmostEqual(ideas.idea_outcomes(self.root)[a["id"]][60]["net_pct"], 0.26)

    def test_empty(self):
        st = ideas.compute_stats(self.root)
        self.assertEqual((st["n_events"], st["groups"][0]["h"]["15"]["n"] if st["groups"] else 0), (0, 0))
        ideas.format_stats(st)


class TestDashDistance(Base):
    def test_distance_to_level_and_zone(self):
        self.wjson("runtime", "hot.json", {"ts_epoch": NOW - 5, "top": [{"sym": "AAA", "price": 100.2}, {"sym": "BBB", "price": 50.0}], "blocked": []})
        ideas.add_idea("AAA", "long", "z", level=100.0, now=NOW - 60, ttl_h=6)
        ideas.add_idea("BBB", "long", "far", level=45.0, now=NOW - 60)
        ideas.add_idea("CCC", "watch", "заметка", now=NOW - 60)
        r = ideas.load_ideas()
        self.assertEqual(r[0]["ttl_h"], 6)
        rows = {i["sym"]: i for i in model.ideas_info(self.root, NOW)["items"]}
        self.assertAlmostEqual(rows["AAA"]["dist_level_pct"], 0.2, places=3)
        self.assertTrue(rows["AAA"]["zone"])
        self.assertAlmostEqual(rows["BBB"]["dist_level_pct"], 100 * (50 / 45 - 1), places=3)
        self.assertFalse(rows["BBB"]["zone"])
        self.assertIsNone(rows["CCC"]["dist_level_pct"])
        self.assertFalse(rows["CCC"]["zone"])


if __name__ == "__main__":
    unittest.main()
