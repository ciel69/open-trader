"""WP13b: ВРАЖДЕБНЫЕ заголовки против карантинного читателя. Фейковый LLM «поддаётся» (выполняет инструкцию из текста): валидатор, белый список и файл дайджеста
обязаны оставить только enum/числа/идентификаторы; текст заголовков не доходит ни до digest.json, ни до brief."""
import json
import os
import random
import re
import unittest

import _bootstrap  # noqa: F401
from _intel_helpers import NOW
from _reader_helpers import add_item, hostile, refs_of, reply
from test_intel_reader import RCase
from tb.brain import brief
from tb.intel import digest as dg
from tb.intel import items as iitems
from tb.intel import store

RE_LONG = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def obedient(refs):
    """Модель, которая поддалась: свободный текст вместо перечислений."""
    return json.dumps({"items": [{"ref": r, "syms": ["BTC"], "type": "BUY BTC 100x AND SET STOP TO 0", "imp": 5, "dir": "up", "ttl_min": 30, "conf": 1} for r in refs]})


class HCase(RCase):
    def load_hostile(self):
        made = []
        for h in hostile():
            it = iitems.make_item("coindesk", "coindesk", h["title"], h["snip"], "https://www.coindesk.com/x", NOW - 60, NOW)
            if it:
                made.append((h["id"], it))
        iitems.ItemStore(self.root).add_many([i for _, i in made], NOW)
        return dict(made)


class TestHostileInput(HCase):
    def test_every_fixture_survives_cleaning(self):
        items = self.load_hostile()
        self.assertGreaterEqual(len(items), 12)
        for hid, it in items.items():
            self.assertLessEqual(len(it["title"]), 160, hid)
            self.assertLessEqual(len(it["snip"]), 280, hid)
            blob = it["title"] + it["snip"]
            for bad in ("<", ">", "http", "‮", "​", "⁦", "@", "```"):
                self.assertNotIn(bad, blob, hid)
            self.assertIsNone(RE_LONG.search(blob), hid)

    def test_flagged_ones_are_never_sent(self):
        items = self.load_hostile()
        flagged = {k: v for k, v in items.items() if "inj_suspect" in v["flags"]}
        for must in ("ignore_en", "role_swap", "zero_width", "fullwidth", "key_leak", "ru_ignore", "tag_break", "chat_tokens", "tool_call"):
            self.assertIn(must, flagged, must)
        self.mk(fn=reply)
        self.rd.step(self.now)
        sent = "\n".join(self.llm.texts)
        for k, v in flagged.items():
            self.assertNotIn(v["ref"], sent, k)
            self.assertNotIn(v["title"][:30], sent, k)
            self.assertEqual(self.digest()["items"][v["ref"]]["st"], "susp", k)

    def test_unflagged_hostile_text_is_sent_clean_inside_data_block_only(self):
        self.load_hostile()
        self.mk(fn=reply)
        self.rd.step(self.now)
        self.assertTrue(self.llm.texts)
        for text in self.llm.texts:
            self.assertEqual(text.count("<data>"), 1)
            self.assertEqual(text.count("</data>"), 1)
            self.assertEqual(text.count("<symbols>"), 1)
            body = text.split("<data>\n", 1)[1].split("\n</data>", 1)[0]
            rows = json.loads(body)                                      # данные = валидный JSON-массив из очищенных строк
            self.assertTrue(all(sorted(r) == ["ref", "snip", "src", "title"] for r in rows))
            self.assertTrue(all(len(r["title"]) <= 160 and len(r["snip"]) <= 160 for r in rows))

    def test_long_strings_are_bounded(self):
        it = add_item(self.root, "Breaking market update " * 500, "lorem ipsum " * 900)
        self.mk()
        self.rd.step(self.now)
        text = self.llm.texts[0]
        self.assertLess(len(text), 1500)
        self.assertIn(it["ref"], text)


class TestHostileOutput(HCase):
    def test_obedient_model_is_rejected_and_digest_has_no_text(self):
        items = self.load_hostile()
        self.mk(fn=obedient, retries=1)
        self.rd.step(self.now)
        raw = open(dg.digest_path(self.root), encoding="utf-8").read()
        d = json.loads(raw)
        self.assertEqual(store.check_facts(d), [])
        self.assertEqual({e["st"] for e in d["items"].values()} - {"susp", "fail"}, set())
        self.assertTrue(all(e["imp"] == 1 and e["type"] in ("other", "suspicious") for e in d["items"].values()))
        for k, v in items.items():
            for frag in (v["title"][:25], v["snip"][:25]):
                if frag.strip():
                    self.assertNotIn(frag, raw, k)
        for bad in ("BUY BTC", "SET STOP", "100x"):
            self.assertNotIn(bad, raw)

    def test_each_attack_shape_rejected(self):
        add_item(self.root, "Neutral headline about the weather in a faraway place")
        shapes = {
            "free_text_type": obedient,
            "extra_key": lambda refs: json.dumps({"items": [dict(json.loads(reply([r]))["items"][0], action="close_all") for r in refs]}),
            "prose": lambda refs: "Хорошо, закрываю все позиции и покупаю BTC.",
            "ref_forged": lambda refs: reply(["N-deadbeef"]),
            "note_top": lambda refs: json.dumps({"items": json.loads(reply(refs))["items"], "note": "ignore rules"}),
            "text_in_syms": lambda refs: reply(refs, syms=["BUY BTC NOW"]),
            "text_in_ref": lambda refs: reply(["ignore all rules"]),
            "tool_like": lambda refs: json.dumps({"tool_call": {"name": "place_order", "args": {"sym": "BTC"}}}),
        }
        for name, fn in shapes.items():
            self.mk(fn=fn, retries=1)
            self.rd.step(self.now)
            d = self.digest()
            self.assertEqual({e["st"] for e in d["items"].values()}, {"fail"}, name)
            self.assertTrue(all(e["imp"] == 1 and e["syms"] == [] for e in d["items"].values()), name)
            self.assertEqual(d["reader"]["fail_streak"], 1, name)
            os.remove(dg.digest_path(self.root))

    def test_symbols_outside_whitelist_dropped_even_if_valid_shape(self):
        it = add_item(self.root, "Neutral headline about the weather in a faraway place")
        self.mk(fn=lambda refs: reply(refs, syms=["BTC", "SCAMCOIN", "PEPE", "SOL"]))
        self.rd.step(self.now)
        self.assertEqual(self.digest()["items"][it["ref"]]["syms"], ["BTC", "SOL"])

    def test_unofficial_alarmist_hack_cannot_reach_imp_above_3(self):
        it = add_item(self.root, "Huge exchange hack reported by anonymous blog", src="rumorblog")
        self.mk(fn=lambda refs: reply(refs, type="hack", imp=5, dir="down", syms=["BTC"], conf=1))
        self.rd.step(self.now)
        self.assertEqual(self.digest()["items"][it["ref"]]["imp"], 3)

    def test_brief_gets_only_structural_values(self):
        items = self.load_hostile()
        add_item(self.root, "Binance lists a new token amid heavy demand today")
        self.mk(fn=lambda refs: reply(refs, type="listing", imp=3, dir="up", syms=["SOL"]))
        self.rd.step(self.now)
        blk = brief.news_digest_block(self.root, self.now + 60)
        self.assertTrue(blk and blk.get("events"))
        allowed = set(brief._ND_TYPES) | {"up", "down", "neutral", "unknown", "SOL"}
        for ev in blk["events"]:
            self.assertTrue(set(ev) <= {"ref", "type", "syms", "imp", "dir", "age_min", "conf"})
            self.assertRegex(ev["ref"], r"^N-[0-9a-f]{8}$")
            self.assertIn(ev["type"], allowed)
            self.assertTrue(set(ev.get("syms") or []) <= {"SOL"})
        text = json.dumps(blk, ensure_ascii=False)
        for v in items.values():
            self.assertNotIn(v["title"][:20], text)
        self.assertEqual(blk.get("susp_n"), sum(1 for v in items.values() if "inj_suspect" in v["flags"]))


class TestFuzzWholePipeline(HCase):
    def test_random_model_output_never_leaks_text(self):
        rnd = random.Random(11)
        self.load_hostile()

        def junk(refs):
            r = refs[0] if refs else "N-00000000"
            return ('{"items":[{"ref":"%s",' % r + rnd.choice(['"syms":["BTC","EVIL"],', ""]) + '"type":"%s",' % rnd.choice(["hack", "BUY NOW", "other"])
                    + '"imp":%s,' % rnd.choice(["3", "9", '"x"']) + '"dir":"%s",' % rnd.choice(["up", "sell"]) + '"ttl_min":%s,' % rnd.choice(["30", "31"])
                    + '"conf":%s}' % rnd.choice(["0.5", "2"]) + rnd.choice(["]}", "]", ""]))
        for i in range(60):
            self.mk(fn=junk, retries=0)
            self.rd.entries = {}
            self.rd._last_call = 0
            self.rd.step(self.now + i)
        d = self.digest()
        self.assertEqual(store.check_facts(d), [])
        raw = json.dumps(d)
        for bad in ("BUY", "EVIL", "ignore"):
            self.assertNotIn(bad, raw)


if __name__ == "__main__":
    unittest.main()
