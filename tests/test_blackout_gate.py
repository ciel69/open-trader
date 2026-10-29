"""Жёсткий отказ Gate по макро-блэкауту (E_BLACKOUT): live/testnet всегда, paper при blackout_mode=block; paper при warn не меняется.

Защитные действия в окне не блокируются. Нет данных об окнах: live/testnet отказ, paper по blackout.on_missing. План, поданный в обход мозга
(`./tb plan submit` -> очередь -> исполнитель), останавливается тем же Gate и остаётся armed: входит после окна, если ttl не истёк.
"""
import json
import os
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
from fakes.harness import Env, plan_long
from tb import paths
from tb.core import blackout
from tb.brain import plan_cli
from tb.executor import gate as G


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def put(root, windows, ts=None):
    p = paths.runtime("intel", "blackouts.json", root=root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    doc = {"windows": windows}
    if ts is not None:
        doc["ts"] = ts
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def win(t, a_min, z_min, name="FOMC", v=True):
    return {"from": iso(t + a_min * 60), "to": iso(t + z_min * 60), "name": name, "src": "calendar", "v": v}


def mk(env, mode="block", windows=(), **blk):
    over = {"paper": {"blackout_mode": mode}}
    if blk:
        over["blackout"] = blk
    e = Env(env=env, with_session=(env != "paper"), cfg_over=over)
    if windows is not None:
        put(e.root, [w(e.clock.time()) if callable(w) else w for w in windows])
    return e


def inwin(t):
    return win(t, -10, 50)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbbg_")
        self.t = 1800000000.0

    def chk(self, mode="live", **cfg):
        return blackout.check(self.root, mode, self.t, cfg=dict({"paper": {"blackout_mode": "block"}}, **cfg))

    def test_inside_before_after_and_boundaries(self):
        t = self.t
        put(self.root, [win(t, 30, 90)])                         # окно начнётся через 30 минут
        self.assertFalse(self.chk()["blocked"])
        for sec, want in ((-1, False), (0, True), (60 * 60, True), (60 * 60 + 1, False)):
            self.assertEqual(blackout.check(self.root, "live", t + 30 * 60 + sec, cfg={"paper": {}})["blocked"], want, sec)
        self.assertTrue(blackout.check(self.root, "live", t + 90 * 60, cfg={"paper": {}})["blocked"])       # конец включительно
        r = blackout.check(self.root, "live", t + 60 * 60, cfg={"paper": {}})
        self.assertEqual((r["code"], r["name"], r["end_min"]), ("E_BLACKOUT", "FOMC", 30))
        self.assertIn("FOMC", r["reasons"][0])
        self.assertIn("30 мин", r["reasons"][0])

    def test_pre_min_extends_before(self):
        put(self.root, [win(self.t, 30, 90)])
        self.assertFalse(self.chk()["blocked"])
        self.assertTrue(self.chk(blackout={"pre_min": 30})["blocked"])
        self.assertFalse(self.chk(blackout={"pre_min": 29})["blocked"])

    def test_unverified_candidate_is_window(self):
        put(self.root, [win(self.t, -5, 60, v=False)])
        r = self.chk()
        self.assertTrue(r["blocked"])
        self.assertFalse(r["verified"])
        self.assertIn("не проверена", r["reasons"][0])

    def test_missing_data(self):
        self.assertTrue(self.chk("live")["blocked"])                                    # файла нет
        self.assertTrue(self.chk("live")["missing"])
        self.assertTrue(self.chk("testnet", blackout={"on_missing": "allow"})["blocked"])  # allow не ослабляет live/testnet
        self.assertFalse(self.chk("paper")["blocked"])                                  # paper: auto = allow
        self.assertTrue(self.chk("paper", blackout={"on_missing": "block"})["blocked"])
        self.assertFalse(self.chk("paper", blackout={"on_missing": "allow"})["blocked"])
        p = paths.runtime("intel", "blackouts.json", root=self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{bad")
        self.assertTrue(self.chk("live")["blocked"])
        put(self.root, [], ts=self.t - blackout.STALE_S - 10)                           # устарел
        self.assertTrue(self.chk("live")["blocked"])
        put(self.root, [], ts=self.t - 3600)                                            # свежий и пустой = окон нет
        self.assertFalse(self.chk("live")["blocked"])

    def test_paper_warn_never_blocks(self):
        put(self.root, [inwin(self.t)])
        cfg = {"paper": {"blackout_mode": "warn"}}
        self.assertFalse(blackout.check(self.root, "paper", self.t, cfg=cfg)["blocked"])
        self.assertTrue(blackout.check(self.root, "live", self.t, cfg=cfg)["blocked"])
        self.assertTrue(blackout.check(self.root, "testnet", self.t, cfg=cfg)["blocked"])
        self.assertTrue(blackout.check(self.root, "paper", self.t, cfg={"paper": {"blackout_mode": "block"}})["blocked"])

    def test_config_validation(self):
        from tb.core import exec_cfg
        root = tempfile.mkdtemp(prefix="tbbg_")
        os.makedirs(os.path.join(root, "config"))
        with open(paths.exec_limits_file(root=root), "w") as f:
            json.dump({"blackout": {"on_missing": "maybe"}}, f)
        with self.assertRaises(ValueError):
            exec_cfg.load(root)
        with open(paths.exec_limits_file(root=root), "w") as f:
            json.dump({"blackout": {"pre_min": 15, "on_missing": "block"}}, f)
        self.assertEqual(exec_cfg.load(root)["blackout"], {"pre_min": 15, "on_missing": "block"})
        self.assertIn("E_BLACKOUT", G.DENY_CODES)
        self.assertIn("E_BLACKOUT", G.TRANSIENT)


class TestGateEntry(unittest.TestCase):
    def test_live_and_testnet_denied_then_enter_after_window(self):
        for env in ("testnet", "mainnet"):
            e = mk(env, mode="warn", windows=[])
            try:
                put(e.root, [win(e.clock.time(), -10, 50)])
                pid = e.enter()
                self.assertNotIn("SOL", e.port.pos, env)
                self.assertEqual(e.trades(), [], env)
                self.assertEqual(e.rt(pid)["entry"].get("attempts") or 0, 0, env)        # не попытка: ENTRY_BLOCKED не копится
                self.assertNotEqual(e.rt(pid)["status"], "invalidated")
                dec = [d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]]
                self.assertTrue(dec, env)
                self.assertIn("FOMC", dec[0]["why"])
                ev = [x for x in e.bus("action_rejected") if x["d"].get("code") == "E_BLACKOUT"]
                self.assertTrue(ev, env)
                for _ in range(10):                                                    # окно кончилось, план всё ещё armed и входит
                    e.clock.advance(600)
                    put(e.root, [win(e.clock.time(), -70, -10)])
                    e.timer(2)
                    e.tick(100.25)
                    e.candle(100.3)
                    e.tick(100.25)
                    if "SOL" in e.port.pos:
                        break
                self.assertIn("SOL", e.port.pos, env)
            finally:
                e.close()

    def test_paper_warn_unchanged_and_block_denies(self):
        e = mk("paper", mode="warn", windows=[])
        try:
            put(e.root, [inwin(e.clock.time())])
            e.enter()
            self.assertIn("SOL", e.port.pos)
            self.assertEqual(len(e.decisions("event", "blackout_warn")), 1)
        finally:
            e.close()
        e = mk("paper", mode="block", windows=[])
        try:
            put(e.root, [inwin(e.clock.time())])
            e.enter()
            self.assertNotIn("SOL", e.port.pos)
            self.assertTrue([d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]])
        finally:
            e.close()

    def test_unverified_candidate_denies_live(self):
        e = mk("testnet", windows=[])
        try:
            put(e.root, [win(e.clock.time(), -5, 60, v=False)])
            e.enter()
            self.assertNotIn("SOL", e.port.pos)
            self.assertIn("не проверена", [d for d in e.decisions("skip", "place_entry")][0]["why"])
        finally:
            e.close()

    def test_missing_data_live_denied_paper_allowed(self):
        e = mk("testnet", windows=[])
        try:
            os.remove(paths.runtime("intel", "blackouts.json", root=e.root))
            e.enter()
            self.assertNotIn("SOL", e.port.pos)
            self.assertIn("нет данных", [d for d in e.decisions("skip", "place_entry")][0]["why"])
        finally:
            e.close()
        e = mk("testnet", windows=[], on_missing="allow")
        try:
            os.remove(paths.runtime("intel", "blackouts.json", root=e.root))
            e.enter()
            self.assertNotIn("SOL", e.port.pos)                                       # allow в live не работает
        finally:
            e.close()
        e = mk("paper", mode="block", windows=[])
        try:
            os.remove(paths.runtime("intel", "blackouts.json", root=e.root))
            e.enter(plan_long(mode="paper"))
            self.assertIn("SOL", e.port.pos)                                          # paper: нет данных = allow
        finally:
            e.close()

    def test_limit_entry_denied(self):
        e = mk("testnet", windows=[])
        try:
            put(e.root, [inwin(e.clock.time())])
            pl = plan_long()
            pl["entry"]["how"] = {"type": "limit", "price": 100.0, "post_only": True}
            e.enter(pl)
            self.assertEqual(e.port.pos, {})
            self.assertTrue([d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]])
        finally:
            e.close()

    def test_bypass_brain_via_plan_cli(self):
        e = mk("testnet", windows=[])
        try:
            put(e.root, [inwin(e.clock.time())])
            f = os.path.join(e.root, "plan.json")
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(plan_long(), fh)
            code = plan_cli.main(["submit", f, "--root", e.root, "--wait", "0", "--reason", "тест обхода мозга"])
            self.assertIn(code, (0, 3))      # 3 = queued (ответ ещё не готов)
            e.ex.poll_inbox()
            e.pump()
            self.assertEqual(len(e.ex.plans), 1)
            e.tick(100.1)
            e.candle(100.3)
            e.tick(100.25)
            self.assertNotIn("SOL", e.port.pos)
            self.assertTrue([d for d in e.decisions("skip", "place_entry") if "E_BLACKOUT" in d["why"]])
        finally:
            e.close()


class TestGateAdd(unittest.TestCase):
    def test_add_denied_in_window_only(self):
        e = mk("testnet", windows=[], )
        try:
            e.cfg["pyramid"]["enabled"] = True
            pid = e.enter()
            self.assertIn("SOL", e.port.pos)
            p = e.ex.plans[pid]
            view = e.ex.view("SOL")
            g = e.ex.gate
            rule = {"id": "add1", "do": {"risk_frac": 0.5}, "when": None}
            self.assertNotEqual(g.add(p, view, rule).code, "E_BLACKOUT")
            put(e.root, [inwin(e.clock.time())])
            v = g.add(p, view, rule)
            self.assertFalse(v.allow)
            self.assertEqual(v.code, "E_BLACKOUT")
        finally:
            e.close()


class TestDefensiveNotBlocked(unittest.TestCase):
    def test_defensive_actions_in_window(self):
        e = mk("testnet", windows=[])
        try:
            pid = e.enter()
            self.assertIn("SOL", e.port.pos)
            put(e.root, [inwin(e.clock.time())])
            p, g = e.ex.plans[pid], e.ex.gate
            self.assertTrue(g.stop(p, 99.5, by="rule"))
            self.assertTrue(g.reduce(p, "close"))
            self.assertTrue(g.reduce(p, "cancel"))
            self.assertTrue(g.ladder(p))
            rev = e.spec(pid)["rev"]
            r = e.command("stop", pid, {"to": 99.5}, rev_expected=rev)
            self.assertTrue(r["ok"], r)
            self.assertEqual(e.port.pos["SOL"]["sl"], 99.5)
            r = e.command("hold", pid)
            self.assertTrue(r["ok"], r)
            r = e.command("release", pid)
            self.assertTrue(r["ok"], r)
            r = e.command("close", pid)
            self.assertTrue(r["ok"], r)
            e.pump()
            self.assertNotIn("SOL", e.port.pos)
        finally:
            e.close()

    def test_cancel_of_armed_plan_in_window(self):
        e = mk("testnet", windows=[])
        try:
            put(e.root, [inwin(e.clock.time())])
            r = e.submit()
            self.assertTrue(r["ok"], r)
            r = e.command("cancel", r["plan_id"])
            self.assertTrue(r["ok"], r)
        finally:
            e.close()

    def test_no_blackout_gate_calls_in_defensive_methods(self):
        import inspect
        for fn in (G.Gate.stop, G.Gate.reduce, G.Gate.ladder):
            self.assertNotIn("blackout", inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
