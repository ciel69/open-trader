"""WP15b: сквозной разбор закрытия ревьюером на ФЕЙКОВОМ LLM и настоящем шлюзе: position_closed -> случай (писатель) -> вход ревьюера -> валидатор -> add_case_review -> память.
Сети и живых моделей нет; временный корень."""
import json
import os
import shutil

import _bootstrap  # noqa: F401
from _brain import BrainBase, jdump
from _memory_helpers import FIX
from tb.core import bus
from tb.gateway.rpc import Gateway
from tb.memory import cases, privacy

DOOD = "BB-M-DOOD-1027-143314"           # вход без подтверждения, -1.07R (фикстура снимка журнала)
RV = {"schema": "tb.casereview/1", "verdict": "bad_process", "right": [], "improve": ["no_confirm"], "alternative": {"action": "wait_confirm", "detail": "ждать закрытия 5м свечи"},
      "known_at_decision": [], "lesson_hint": {"code": "L-no_confirm", "cond": "entry_without_confirm"}, "conf": 0.6}


class Flow(BrainBase):
    def setUp(self):
        BrainBase.setUp(self)
        for src, dst in (("trades_snapshot.jsonl", "trades.jsonl"), ("ideas_snapshot.jsonl", "ideas.jsonl"), ("hot_outcomes_snapshot.jsonl", "hot_outcomes.jsonl")):
            os.makedirs(self.p("state"), exist_ok=True)
            shutil.copy(os.path.join(FIX, src), self.p("state", dst))
        ag = self.make_agents()
        ag["api"]["tools"] = ag["api"]["tools"] + ["@memory.read", "@memory.write"]
        ag["api"]["auth"] = "local"
        jdump({"agents": ag}, self.p("config", "agents.json"))
        self.real = Gateway(self.root)

    def closed(self, jid=DOOD, **kw):
        seq = bus.emit("position_closed", {"reason": "sl", "r": -1.07, "journal_id": jid}, plan="PL-1027-DOOD-01", sym="DOOD", root=self.root, now=self.t)
        return self.job("J-9", seqs=[seq], types=("position_closed",), plan="PL-1027-DOOD-01", **kw)

    def case_of(self, jid=DOOD):
        return [c for c in cases.load(self.root) if (c.get("refs") or {}).get("journal_id") == jid and c["type"] == "trade"][0]

    def test_position_closed_to_reviewed_case(self):
        self.closed()
        r = self.runner({"ds-flash": [RV]}, gw=self.real)
        r.step()
        res = self.get_job("J-9")["result"]["roles"][0]
        self.assertEqual((res["role"], res["outcome"], res["code"]), ("reviewer", "decision", "OK"))
        c = self.case_of()
        self.assertEqual(c["review"]["verdict"], "bad_process")
        self.assertEqual(c["review"]["lesson_hint"]["code"], "L-NO_CONFIRM")
        self.assertIn("no-confirm", c["tags"]["err"])
        self.assertEqual(c["rev"], 2)
        d = [x for x in self.decisions() if x["what"] == "brain_decision"]
        self.assertEqual([x["res"]["code"] for x in d], ["OK"])
        self.assertTrue(cases.verify(self.root)["ok"])
        # модель получила <case> (известное на момент решения) и <after_decision> (исход) раздельно, без записи памяти из её инструментов
        req = self.factory.clients["ds-flash"].calls[0]["req"]
        txt = req.messages[0]["content"]
        self.assertIn("<case>", txt)
        self.assertIn("<after_decision>", txt)
        self.assertLess(txt.index("<case>"), txt.index("<after_decision>"))
        head, after = txt.split("<after_decision>")
        self.assertNotIn('"r":-1.07', head)
        self.assertIn('"r":-1.07', after)
        self.assertNotIn('"confirmed":false', head)                      # подтверждение известно только из разбора после сделки: в <case> его нет
        self.assertIn('"learned_after"', after)
        self.assertNotIn("add_case_review", [t["name"] for t in (req.tools or [])])

    def test_second_close_event_does_not_review_twice(self):
        self.closed()
        r = self.runner({"ds-flash": [RV, RV]}, gw=self.real)
        r.step()
        j = self.job("J-10", seqs=[bus.emit("position_closed", {"reason": "sl", "journal_id": DOOD}, root=self.root, now=self.t)], types=("position_closed",), key="k2")
        r.step()
        self.assertEqual(self.get_job("J-10")["result"]["roles"][0]["code"], "NO_CASE_ID")        # уже разобран: нового случая для разбора нет, запись не повторяется
        self.assertEqual(self.case_of()["rev"], 2)

    def test_false_known_claim_never_reaches_memory(self):
        self.closed()
        bad = dict(RV, known_at_decision=["no_confirm"], lesson_hint=None)         # у DOOD отсутствие подтверждения известно только из разбора ПОСЛЕ сделки (retro)
        r = self.runner({"ds-flash": [bad, bad, bad]}, gw=self.real)
        r.step()
        self.assertEqual(self.get_job("J-9")["result"]["roles"][0]["code"], "BAD_FORMAT")
        self.assertIn("KNOWN_FALSE:no_confirm", " ".join(self.factory.clients["ds-flash"].format_errors[0]))
        self.assertIsNone(self.case_of()["review"])

    def test_unlucky_only_when_code_suggests(self):
        self.closed()
        r = self.runner({"ds-flash": [dict(RV, verdict="unlucky", improve=["none"], known_at_decision=[], lesson_hint=None), dict(RV, verdict="unlucky", improve=["none"], known_at_decision=[], lesson_hint=None),
                                      dict(RV, verdict="unlucky", improve=["none"], known_at_decision=[], lesson_hint=None)]}, gw=self.real)
        r.step()
        self.assertIsNone(self.case_of()["review"])
        self.assertIn("VERDICT_NOT_SUPPORTED", " ".join(self.factory.clients["ds-flash"].format_errors[0]))

    def test_agent_without_memory_write_keeps_output_in_decisions_only(self):
        ag = self.make_agents()
        jdump({"agents": ag}, self.p("config", "agents.json"))
        self.closed()
        r = self.runner({"ds-flash": [RV]}, gw=Gateway(self.root))
        r.step()
        self.assertEqual(self.get_job("J-9")["result"]["roles"][0]["code"], "NO_MEMORY_RIGHT")
        self.assertIsNone(self.case_of()["review"])

    def test_no_case_for_event_without_journal_id(self):
        seq = bus.emit("position_closed", {"reason": "sl", "r": -1.0}, plan="PL-1027-DOOD-01", sym="DOOD", root=self.root, now=self.t)
        self.job("J-9", seqs=[seq], types=("position_closed",))
        r = self.runner({"ds-flash": [RV]}, gw=self.real)
        r.step()
        self.assertEqual(self.get_job("J-9")["result"]["roles"][0]["code"], "NO_CASE_ID")
        self.assertIsNone(self.case_of()["review"])
        self.assertEqual(len(self.gw_calls()), 0)

    def gw_calls(self):
        """Записи аудита шлюза по add_case_review: без case_id в память ничего не пишется."""
        try:
            with open(self.p("state", "gateway_audit.jsonl"), encoding="utf-8") as f:
                return [l for l in f if '"add_case_review"' in l]
        except OSError:
            return []

    def test_forged_case_id_in_job_cannot_hit_other_case(self):
        """Задание несёт case_id: шлюз сверяет его с meta раннера, модель case_id не выбирает (аргументы собирает код)."""
        self.closed(case_id=self.case_id_for_beat())
        r = self.runner({"ds-flash": [RV]}, gw=self.real)
        r.step()
        beat = self.case_of("BB-M-BEAT-1027-154603")
        self.assertEqual(beat["review"]["verdict"], "bad_process")          # разбирается именно случай из задания
        self.assertIsNone(self.case_of(DOOD)["review"])

    def case_id_for_beat(self):
        from tb.memory import tick
        tick.sync_now(self.root)
        return self.case_of("BB-M-BEAT-1027-154603")["case_id"]

    def test_privacy_scan_clean_after_flow(self):
        self.closed()
        r = self.runner({"ds-flash": [dict(RV, alternative={"action": "wait_confirm", "detail": "потеряно 1.2 USDT; sk-ABCDEF0123456789ABCDEF012345"})]}, gw=self.real)
        r.step()
        c = self.case_of()
        self.assertEqual(privacy.privacy_scan(c), [])
        self.assertNotIn("sk-ABCDEF", json.dumps(c, ensure_ascii=False))
