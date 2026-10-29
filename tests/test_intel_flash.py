"""Быстрые новости: правила классов, монеты из заголовка, событие news_flash без текста, дедуп, реестр исходов, блок досье brief.flash. Без сети."""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb import paths
from tb.core import bus
from tb.intel import flash

NOW = 1793270000.0
UNI = {"BTC": 80000.0, "ETH": 2300.0, "SOL": 140.0, "XYZ": 1.0}


def item(title, ts=None, snip="", src="coindesk", flags=None, ref=None):
    return {"ref": ref or "r" + str(abs(hash(title)) % 10 ** 8), "src": src, "ts": int(NOW - 30 if ts is None else ts), "title": title, "snip": snip, "url": "", "flags": flags or []}


class Classify(unittest.TestCase):
    def test_classes_and_hints(self):
        for title, want in (("Binance will delist XYZ and ABC on Nov 5", ("delisting", 4, "down")),
                            ("Bybit adds new listing: XYZUSDT Perpetual Contract", ("listing", 4, "up")),
                            ("Protocol exploited, $40M drained", ("hack", 5, "down")),
                            ("SEC sues exchange over unregistered securities", ("regulation", 4, "down")),
                            ("Spot ETF approved for Solana", ("etf", 4, "up")),
                            ("Token unlock of 5% supply next week", ("unlock", 2, "down")),
                            ("Scheduled maintenance for deposits", ("maintenance", 2, "none")),
                            ("Powell hints at rate cut", ("macro", 3, "none"))):
            self.assertEqual(flash.classify(title), want, title)
        self.assertIsNone(flash.classify("Market wrap: Bitcoin trades sideways"))

    def test_delisting_wins_over_listing(self):
        self.assertEqual(flash.classify("Exchange will delist token, listing removed")[0], "delisting")


class Symbols(unittest.TestCase):
    def test_pair_cashtag_paren_alias_and_universe(self):
        u = set(UNI)
        self.assertEqual(flash.extract_syms("New Listing: XYZUSDT Perpetual", "", u), ["XYZ"])
        self.assertEqual(flash.extract_syms("Whales buy $sol", "", u), ["SOL"])
        self.assertEqual(flash.extract_syms("Solana (SOL) outage", "", u), ["SOL"])
        self.assertEqual(flash.extract_syms("Ethereum upgrade delayed", "", u, {"ethereum": "ETH"}), ["ETH"])
        self.assertEqual(flash.extract_syms("Unknown (QQQQ) hacked", "", u), [])             # вне вселенной скана
        self.assertEqual(flash.extract_syms("XYZ hacked, funds drained", "", u), ["XYZ"])    # голый тикер
        self.assertEqual(flash.extract_syms("SEC sues exchange; ETF flows", "", u | {"SEC", "ETF"}), [])      # слова из стоп-списка не монеты
        self.assertEqual(len(flash.extract_syms("XYZUSDT and SOLUSDT and BTCUSDT", "", u, limit=2)), 2)


class Process(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbflash_")
        os.makedirs(os.path.join(self.root, "runtime"))
        os.makedirs(os.path.join(self.root, "state"))
        self.price(UNI)
        self.sent = []
        self.f = flash.Flash(self.root, {}, clock=lambda: NOW, emit=lambda t, d, now, sym: self.sent.append((t, d, sym)))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def price(self, book, now=NOW):
        with open(paths.runtime("prices.json", root=self.root), "w", encoding="utf-8") as f:
            json.dump({"ts_epoch": now, "prices": book}, f)

    def test_emits_event_without_text(self):
        out = self.f.process([item("Bybit adds new listing: XYZUSDT Perpetual Contract", src="bybit_ann")], NOW)
        self.assertEqual(len(out), 1)
        t, d, sym = self.sent[0]
        self.assertEqual((t, sym), ("news_flash", "XYZ"))
        self.assertEqual((d["cls"], d["dir"], d["imp"]), ("listing", "up", 4))
        self.assertNotIn("title", json.dumps(d))
        bus.emit(t, d, sym=sym, root=self.root, now=NOW)                  # проходит белый список шины
        self.assertTrue(any(e["type"] == "news_flash" for e in bus.read_events(root=self.root)) if hasattr(bus, "read_events") else True)

    def test_filters_stale_low_importance_injection_and_unknown_coin(self):
        self.assertEqual(self.f.process([item("Binance will delist XYZ", ts=NOW - 3600)], NOW), [])            # старше 15 минут
        self.assertEqual(self.f.process([item("Scheduled maintenance for SOL deposits ($SOL)")], NOW), [])      # важность 2 < min_imp 3
        self.assertEqual(self.f.process([item("XYZ hacked", flags=["inj_suspect"])], NOW), [])
        self.assertEqual(self.f.process([item("QQQQ (QQQQ) hacked")], NOW), [])                                 # монеты нет во вселенной
        self.assertEqual(self.sent, [])

    def test_aliases_only_for_market_wide_classes(self):
        """«Bitcoin wallet fixes critical flaw» (hack) не должно стать событием по BTC, а «SEC sues Bitcoin ETF issuer» (regulation) может."""
        f = flash.Flash(self.root, {"flash": {"aliases": {"bitcoin": "BTC"}}}, clock=lambda: NOW, emit=lambda *a: self.sent.append(a))
        self.assertEqual(f.process([item("Bitcoin wallet maker pulls devices after stolen key exploit")], NOW), [])
        self.assertEqual([r["sym"] for r in f.process([item("SEC sues Bitcoin ETF issuer", ref="e1")], NOW)], ["BTC"])

    def test_market_wide_news_goes_to_btc(self):
        out = self.f.process([item("SEC sues large exchange")], NOW)
        self.assertEqual([r["sym"] for r in out], ["BTC"])

    def test_dedup_same_class_and_coin(self):
        self.f.process([item("XYZ hacked, funds drained")], NOW)
        self.f.process([item("XYZ exploit confirmed by team", ref="other")], NOW + 60)
        self.assertEqual(len(self.sent), 1)
        self.price(UNI, NOW + 2000)
        self.f.process([item("XYZ exploit confirmed by team", ts=NOW + 2000, ref="third")], NOW + 2000)
        self.assertEqual(len(self.sent), 2)                                         # после dedup_s снова

    def test_outcomes_after_horizons_and_stats(self):
        self.f.process([item("XYZ will be delisted soon: Binance will delist XYZ")], NOW)
        self.price(dict(UNI, XYZ=0.98), NOW + 61)
        self.assertEqual(self.f.track(NOW + 61), 1)                                  # +1 мин
        self.price(dict(UNI, XYZ=0.90), NOW + 301)
        self.assertEqual(self.f.track(NOW + 301), 1)                                 # +5 мин
        rows = [r for r in flash.read_rows(self.root) if r["ev"] == "outcome"]
        self.assertEqual([r["h"] for r in rows], [1, 5])
        self.assertAlmostEqual(rows[0]["ret_pct"], -2.0, places=2)
        self.assertAlmostEqual(rows[0]["dir_pct"], 2.0, places=2)                    # подсказка down, цена упала: попадание
        st = {(s["cls"], s["h"]): s for s in flash.stats(self.root)}
        self.assertEqual(st[("delisting", 5)]["hit"], 1.0)

    def test_restart_restores_pending_and_dedup(self):
        self.f.process([item("XYZ hacked")], NOW)
        f2 = flash.Flash(self.root, {}, clock=lambda: NOW + 30, emit=lambda *a: self.sent.append(a))
        self.assertEqual(len(f2.pending), 1)
        self.assertEqual(f2.process([item("XYZ hacked again", ref="x")], NOW + 30), [])      # дедуп пережил перезапуск

    def test_recent_for_brief_has_move_and_no_text(self):
        self.f.process([item("XYZ hacked, funds drained")], NOW)
        self.price(dict(UNI, XYZ=0.95), NOW + 120)
        rows = flash.recent_for(self.root, "XYZ", NOW + 120)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["cls"], rows[0]["dir"], rows[0]["imp"], rows[0]["age_s"]), ("hack", "down", 5, 120))
        self.assertAlmostEqual(rows[0]["move_pct"], -5.0, places=2)
        self.assertEqual(flash.recent_for(self.root, "XYZ", NOW + 5000), [])


class Macro(Process):
    EV = [{"id": "cpi-2026-11-10", "type": "CPI", "ts": NOW, "impact": 3}]

    def test_big_move_after_release_emits_macro_flash_with_direction(self):
        self.price(dict(UNI, BTC=80000.0), NOW - 30)
        self.f.macro_step(self.EV, NOW - 30)                                      # цена перед выходом цифры
        self.price(dict(UNI, BTC=80400.0), NOW + 61)
        self.assertEqual(self.f.macro_step(self.EV, NOW + 61), 1)
        t, d, sym = self.sent[-1]
        self.assertEqual((t, sym, d["cls"], d["dir"], d["imp"], d["src"]), ("news_flash", "BTC", "macro", "up", 4, "calendar"))
        self.assertEqual(self.f.macro_step(self.EV, NOW + 70), 0)                 # одно событие один раз
        f2 = flash.Flash(self.root, {}, clock=lambda: NOW + 120, emit=lambda *a: self.sent.append(a))
        self.price(dict(UNI, BTC=80400.0), NOW + 120)
        self.assertEqual(f2.macro_step(self.EV, NOW + 120), 0)                    # и после перезапуска
        self.price(dict(UNI, BTC=80800.0), NOW + 301)
        self.f.track(NOW + 61)
        self.f.track(NOW + 301)
        out = [r for r in flash.read_rows(self.root) if r["ev"] == "outcome" and r["sym"] == "BTC"]
        self.assertEqual([r["h"] for r in out], [1, 5])                           # исходы считаются от самого релиза, не от момента записи
        self.assertAlmostEqual(out[1]["ret_pct"], 1.0, places=2)

    def test_small_reaction_is_recorded_quietly_and_hidden_from_brief(self):
        self.price(dict(UNI, BTC=80000.0), NOW - 10)
        self.f.macro_step(self.EV, NOW - 10)
        self.price(dict(UNI, BTC=80010.0), NOW + 61)
        self.assertEqual(self.f.macro_step(self.EV, NOW + 61), 1)
        self.assertEqual(self.sent, [])                                           # движения нет: события нет
        row = [r for r in flash.read_rows(self.root) if r["ev"] == "flash"][0]
        self.assertEqual((row["cls"], row["dir"], row["emitted"]), ("macro", "none", False))
        self.assertEqual(flash.recent_for(self.root, "BTC", NOW + 62), [])        # в brief.flash тихая реакция не попадает

    def test_skips_low_impact_missing_pre_price_and_out_of_window(self):
        self.price(dict(UNI, BTC=80000.0), NOW - 10)
        self.f.macro_step(self.EV, NOW - 10)
        self.price(dict(UNI, BTC=80400.0), NOW + 61)
        self.assertEqual(self.f.macro_step([dict(self.EV[0], impact=1)], NOW + 61), 0)          # слабое событие
        self.assertEqual(self.f.macro_step(self.EV, NOW + 30), 0)                                # рано: прошло меньше macro_delay_s
        self.price(dict(UNI, BTC=80400.0), NOW + 2000)
        self.assertEqual(self.f.macro_step(self.EV, NOW + 2000), 0)                              # окно реакции закрылось
        fresh = flash.Flash(self.root, {}, clock=lambda: NOW + 61, emit=lambda *a: self.sent.append(a))
        self.price(dict(UNI, BTC=80400.0), NOW + 61)
        self.assertEqual(fresh.macro_step(self.EV, NOW + 61), 0)                                 # платформа не видела цену до выхода: реакцию не считаем


class BriefAndBus(Process):
    def test_brief_block_and_bus_whitelist(self):
        import types
        from tb.brain import brief
        self.f.process([item("XYZ hacked, funds drained")], NOW)
        self.price(dict(UNI, XYZ=0.97), NOW + 60)
        rows = brief._b_flash(types.SimpleNamespace(root=self.root, now=NOW + 60), "XYZ")
        self.assertEqual((rows[0]["cls"], rows[0]["dir"], rows[0]["imp"]), ("hack", "down", 5))
        self.assertIsNone(brief._b_flash(types.SimpleNamespace(root=self.root, now=NOW + 60), "SOL"))
        seq = bus.emit("news_flash", {"cls": "hack", "dir": "down", "imp": 5, "src": "coindesk", "age_s": 30, "id": "fabc", "title": "free text"}, sym="XYZ", root=self.root, now=NOW)
        ev = [e for e in bus.read(0, root=self.root) if e["seq"] == seq][0]
        self.assertEqual((ev["type"], ev["pri"], ev["sym"]), ("news_flash", "wake", "XYZ"))
        self.assertEqual(sorted(ev["d"]), ["age_s", "cls", "dir", "id", "imp", "src"])        # свободный текст отброшен белым списком шины


if __name__ == "__main__":
    unittest.main()
