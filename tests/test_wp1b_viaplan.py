"""WP1B: `bybit open --via-plan`, sync/close для плановых сделок (исполнитель жив/мёртв), гонки с symbol_busy.
Исполнитель настоящий (Executor на FakePort во временном корне, стенд tests/fakes/harness.py); поток-насос читает _inbox, как это делает демон.
Биржа клиента bybit — поддельная (LimitFake), реальный state/ и сеть не используются."""
import argparse
import json
import os
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from fakes.harness import Env, plan_long
from test_bybit_limit import LimitFake
from tb import paths
from tb.core import journal
from tb.venues import bybit as bb
from tb.venues import planlink


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jdump(path, d):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f)


class Pump(threading.Thread):
    """Как рабочий поток демона: опрашивает _inbox исполнителя, пока тест вызывает старые команды."""

    def __init__(self, ex):
        threading.Thread.__init__(self, daemon=True)
        self.ex, self.stop_ev, self.err = ex, threading.Event(), None

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.ex.poll_inbox()
            except Exception as e:      # pragma: no cover
                self.err = e
            time.sleep(0.03)

    def finish(self):
        self.stop_ev.set()
        self.join(5)


class NoExchange(object):
    """Любое обращение к бирже = провал теста (команда обязана идти через исполнителя)."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        raise AssertionError("обращение к бирже: %s %s" % (method, url))


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env(env="testnet")
        self.pumps = []

    def tearDown(self):
        for p in self.pumps:
            p.finish()
        self.e.close()

    def client(self, fake, env="testnet"):
        return bb.Client(env, "KEY", "SECRET", transport=fake, clock=lambda: 1700000000.0)

    def args(self, **kw):
        d = dict(sym="SOL", side="long", sl=99.0, tp=103.0, thesis="legacy вход через план на тесте", news="", score=0.6, regime="range", dry_run=False,
                 mainnet_confirm=False, reason="manual", exec="taker", timeout=30.0, limit_price=None, ttl=None, cancel_above=None, cancel_below=None,
                 snap=False, idea=None, via_plan=True, err=None, rule_followed=None, lesson=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def pump(self):
        p = Pump(self.e.ex)
        p.start()
        self.pumps.append(p)
        return p

    def alive(self):
        """Свежие heartbeat и снимок исполнителя на диске."""
        self.e.timer(2, dt=3)

    def inbox_files(self):
        d = paths.plans_inbox(root=self.e.root)
        return [n for n in os.listdir(d)] if os.path.isdir(d) else []

    def kill_heartbeat(self):
        os.remove(paths.exec_health_file(root=self.e.root))

    def market(self):
        return LimitFake(bid="99.99", ask="100.01", last="100.0")


class ViaPlanOpen(Base):
    def test_executor_down_is_clear_error_without_direct_order(self):
        self.kill_heartbeat()
        f = self.market()
        r = bb.cmd_open(self.client(f), self.args())
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "executor")
        self.assertIn("./tb exec run --profile testnet", r["error"])
        self.assertIn("НЕ ставится", r["error"])
        self.assertEqual(f.posts(), [])                      # ни create, ни set-leverage
        self.assertEqual(self.inbox_files(), [])             # и план не отправлен

    def test_wrong_env_executor_counts_as_down(self):
        self.alive()
        f = self.market()
        r = bb.cmd_open(self.client(f, "mainnet"), self.args(mainnet_confirm=True))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "executor")
        self.assertIn("mainnet", r["error"])
        self.assertEqual(f.posts(), [])

    def test_mainnet_still_needs_confirm(self):
        with self.assertRaises(bb.BybitError):
            bb.cmd_open(self.client(self.market(), "mainnet"), self.args())

    def test_dry_run_builds_legacy_plan_without_executor(self):
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True))
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["dry_run"] and r["via_plan"])
        pl = r["plan"]
        self.assertEqual((pl["symbol"], pl["side"], pl["mode"], pl["session_type"]), ("SOL", "long", "live", "intraday"))
        self.assertIn("legacy", pl["tags"]["extra"])
        self.assertEqual(pl["tags"]["setup"], "legacy")
        self.assertEqual(pl["entry"]["how"], {"type": "market"})
        self.assertEqual(pl["stop"]["price"], 99.0)
        self.assertTrue(pl["risk"]["confirm_waiver"])
        self.assertEqual(pl["risk"]["size_mult"], 0.5)           # правило E_NO_CONFIRM: вход без подтверждения вдвое меньше
        self.assertEqual(pl["targets"][0]["price"], 103.0)
        self.assertEqual(self.inbox_files(), [])

    def test_size_mult_option_lowers_but_never_exceeds_cap(self):
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, size_mult=0.1))
        self.assertEqual(r["plan"]["risk"]["size_mult"], 0.1)
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, size_mult=1.0))
        self.assertEqual(r["plan"]["risk"]["size_mult"], 0.5)
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, size_mult=0))
        self.assertFalse(r["ok"])

    def test_limit_args_become_limit_entry(self):
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, limit_price=99.5, ttl=45.0, cancel_below=98.5, cancel_above=101.0, snap=True))
        self.assertTrue(r["ok"], r)
        en = r["plan"]["entry"]
        self.assertEqual(en["how"], {"type": "limit", "price": 99.5, "post_only": True, "snap": True})
        self.assertEqual(en["valid"], {"ttl_min": 45, "cancel_below": 98.5, "cancel_above": 101.0})

    def test_maker_uses_passive_side_of_book(self):
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, exec="maker", timeout=30.0))
        self.assertEqual(r["plan"]["entry"]["how"]["price"], 99.99)
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, exec="maker", side="short", sl=101.0, tp=97.0))
        self.assertEqual(r["plan"]["entry"]["how"]["price"], 100.01)

    def test_invalid_plan_is_local_error(self):
        r = bb.cmd_open(self.client(self.market()), self.args(dry_run=True, thesis="коротко"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "plan")
        self.assertTrue(r["errors"])

    def test_submit_reaches_executor_and_trade_goes_through_plan(self):
        self.alive()
        f = self.market()
        self.pump()
        r = bb.cmd_open(self.client(f), self.args())
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["via_plan"])
        self.assertTrue(r["plan_id"].startswith("PL-"))
        self.assertEqual(f.posts(), [])                      # прямого ордера нет: всё делает исполнитель (свой порт)
        spec = self.e.spec(r["plan_id"])
        self.assertIn("legacy", spec["tags"]["extra"])
        self.assertEqual(spec["source"]["kind"], "manual")
        # исполнитель ведёт план дальше как обычный: тик -> вход -> журнал с plan_id
        self.e.tick(100.0)
        self.e.timer(2, dt=1)
        tr = [t for t in self.e.trades() if t.get("plan_id") == r["plan_id"]]
        self.assertEqual(len(tr), 1, self.e.rt(r["plan_id"])["status"])
        self.assertIn("plan:%s" % r["plan_id"], tr[0]["tags"])

    def test_symbol_busy_rejected_by_executor_not_bypassed(self):
        pid = self.e.enter()                                  # по SOL уже позиция плана
        self.alive()
        self.pump()
        r = bb.cmd_open(self.client(self.market()), self.args())
        self.assertFalse(r["ok"], r)
        self.assertTrue(r["via_plan"])
        self.assertIn("E_SYMBOL_BUSY", [x["code"] for x in r["errors"]])
        self.assertEqual([p for p in self.e.ex.plans if p != pid and self.e.ex.plans[p]["rt"]["status"] not in ("rejected",)], [])

    def test_no_reply_is_queued_not_fallback(self):
        self.alive()
        f = self.market()
        orig = planlink.WAIT_S
        try:
            import tb.venues.bybit as mod
            r = mod.open_via_plan(self.client(f), self.args(), wait=0.3)     # насоса нет: ответа не будет
        finally:
            planlink.WAIT_S = orig
        self.assertIsNone(r["ok"])
        self.assertEqual(r["status"], "queued")
        self.assertEqual(len(self.inbox_files()), 1)
        self.assertEqual(f.posts(), [])


class PlanTradeSync(Base):
    def rec(self, pid):
        return [t for t in self.e.trades() if t.get("plan_id") == pid][0]

    def test_sync_skips_plan_trade_when_executor_alive(self):
        pid = self.e.enter()
        self.alive()
        f = LimitFake(positions=[], closed=[{"updatedTime": "9999999999999", "avgExitPrice": "104", "openFee": "0.01", "closeFee": "0.01"}])
        out = bb.cmd_sync(self.client(f))
        self.assertEqual(out["closed"], [])
        self.assertEqual(out["plan_managed"][0]["plan_id"], pid)
        self.assertEqual(out["still_open"], [self.rec(pid)["id"]])
        self.assertNotIn("warnings", out)
        self.assertFalse(self.rec(pid).get("ts_close"))
        paths_called = [p for _, p, _ in f.calls]
        self.assertNotIn("/v5/order/cancel-all", paths_called)        # лесенка плана не снята
        self.assertNotIn("/v5/position/closed-pnl", paths_called)

    def test_sync_with_position_present_just_pending(self):
        pid = self.e.enter()
        self.alive()
        f = LimitFake(positions=[{"side": "Buy", "size": "1", "symbol": "SOLUSDT"}])
        out = bb.cmd_sync(self.client(f))
        self.assertEqual(out["still_open"], [self.rec(pid)["id"]])
        self.assertNotIn("plan_managed", out)

    def test_sync_legacy_when_executor_dead_warns(self):
        pid = self.e.enter()
        self.alive()
        self.kill_heartbeat()
        f = LimitFake(positions=[], closed=[{"updatedTime": "9999999999999", "avgExitPrice": "104", "openFee": "0.01", "closeFee": "0.01"}])
        out = bb.cmd_sync(self.client(f))
        self.assertEqual(len(out["closed"]), 1)
        self.assertEqual(out["closed"][0]["reason"], "tp")
        self.assertIn("исполнитель не запущен", out["warnings"][0])
        self.assertTrue(self.rec(pid).get("ts_close"))

    def test_sync_stale_heartbeat_counts_as_dead(self):
        pid = self.e.enter()
        self.alive()
        h = paths.exec_health_file(root=self.e.root)
        d = jload(h)
        d["ts_epoch"] = time.time() - 300
        jdump(h, d)
        out = bb.cmd_sync(self.client(LimitFake(positions=[], closed=[{"updatedTime": "9999999999999", "avgExitPrice": "104"}])))
        self.assertEqual(len(out["closed"]), 1)

    def test_old_trades_output_unchanged(self):
        """Запись без plan_id: прежнее поведение и прежние ключи вывода."""
        ns = argparse.Namespace(sym="SOL", side="long", entry=100.0, lev=5.0, margin=2.0, sl=99.0, tp=103.0, thesis="старая сделка", news="", regime="range",
                                tags="bybit,testnet", mode="testnet", id="BB-T-SOL-0101-000000", session=None, stype=None, venue="bybit", fee_open=0.01)
        journal.cmd_open(ns)
        self.alive()
        out = bb.cmd_sync(self.client(LimitFake(positions=[], closed=[{"updatedTime": "9999999999999", "avgExitPrice": "103", "openFee": "0.01", "closeFee": "0.01"}])))
        self.assertEqual(sorted(out), ["closed", "ok", "still_open"])
        self.assertEqual(out["closed"][0]["id"], "BB-T-SOL-0101-000000")

    def test_sync_race_executor_finalizes_first_no_duplicate_close(self):
        """sync (исполнитель жив) пропускает запись, исполнитель финализирует, второй sync ничего не делает: ровно одна запись close."""
        pid = self.e.enter()
        self.alive()
        f = LimitFake(positions=[])
        bb.cmd_sync(self.client(f))
        self.e.tick(102.6)                                    # обе цели лесенки плана на FakePort
        self.e.tick(104.1)
        self.e.timer(3, dt=3)
        out = bb.cmd_sync(self.client(f))
        self.assertEqual(out["closed"], [])
        self.assertEqual(self.e.rt(pid)["status"], "closed")
        self.assertTrue(self.rec(pid).get("ts_close"))
        closes = [t for t in self.e.trades() if t.get("plan_id") == pid and t.get("ts_close")]
        self.assertEqual(len(closes), 1)


class PlanTradeClose(Base):
    def test_close_delegated_to_executor_no_exchange_calls(self):
        pid = self.e.enter()
        self.alive()
        self.pump()
        nx = NoExchange()
        r = bb.cmd_close(self.client(nx), self.args(via_plan=False), sleep=lambda s: None)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["via"], "executor")
        self.assertEqual(r["plan_id"], pid)
        self.assertEqual(nx.calls, [])
        self.assertTrue(r["result"]["ok"])
        # исполнитель закрыл позицию и сам записал close ровно один раз
        for p in self.pumps:
            p.finish()
        self.e.pump()
        self.e.timer(3, dt=3)
        tr = [t for t in self.e.trades() if t.get("plan_id") == pid]
        self.assertEqual(len(tr), 1)
        self.assertTrue(tr[0].get("ts_close"))
        self.assertEqual(self.e.rt(pid)["status"], "closed")

    def test_close_dry_run_delegated_sends_nothing(self):
        self.e.enter()
        self.alive()
        before = self.inbox_files()
        r = bb.cmd_close(self.client(NoExchange()), self.args(via_plan=False, dry_run=True), sleep=lambda s: None)
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertEqual(self.inbox_files(), before)

    def test_close_err_flags_warn(self):
        self.e.enter()
        self.alive()
        self.pump()
        r = bb.cmd_close(self.client(NoExchange()), self.args(via_plan=False, err=["chase"]), sleep=lambda s: None)
        self.assertIn("не переданы исполнителю", r["warning"])

    def test_close_falls_back_with_warning_when_executor_dead(self):
        pid = self.e.enter()
        self.alive()
        self.kill_heartbeat()
        f = LimitFake(positions=[{"side": "Buy", "size": "1", "symbol": "SOLUSDT"}], last="104")
        r = bb.cmd_close(self.client(f), self.args(via_plan=False), sleep=lambda s: None)
        self.assertTrue(r["ok"])
        self.assertIn("исполнитель не запущен", r["plan_warning"])
        self.assertIn("/v5/order/create", [p for _, p, _ in f.calls])
        self.assertTrue([t for t in self.e.trades() if t.get("plan_id") == pid][0].get("ts_close"))

    def test_close_direct_when_snapshot_says_other_owner(self):
        pid = self.e.enter()
        self.alive()
        sp = paths.exec_snapshot_file(root=self.e.root)
        d = jload(sp)
        d["positions"][0]["owner"] = "unknown"
        jdump(sp, d)
        f = LimitFake(positions=[{"side": "Buy", "size": "1", "symbol": "SOLUSDT"}], last="104")
        r = bb.cmd_close(self.client(f), self.args(via_plan=False), sleep=lambda s: None)
        self.assertIn("не владеет", r["plan_warning"])
        self.assertNotIn("via", r)

    def test_old_trade_close_unchanged(self):
        ns = argparse.Namespace(sym="SOL", side="long", entry=100.0, lev=5.0, margin=2.0, sl=99.0, tp=103.0, thesis="старая сделка", news="", regime="range",
                                tags="bybit,testnet", mode="testnet", id="BB-T-SOL-0101-000001", session=None, stype=None, venue="bybit", fee_open=0.01)
        journal.cmd_open(ns)
        self.alive()
        f = LimitFake(positions=[{"side": "Buy", "size": "1", "symbol": "SOLUSDT"}], last="104")
        r = bb.cmd_close(self.client(f), self.args(via_plan=False), sleep=lambda s: None)
        self.assertTrue(r["ok"])
        self.assertNotIn("plan_warning", r)
        self.assertNotIn("via", r)
        self.assertEqual(r["journal_id"], "BB-T-SOL-0101-000001")


class Helpers(Base):
    def test_owns_position_requires_fresh_snapshot_and_owner(self):
        pid = self.e.enter()
        self.alive()
        self.assertTrue(planlink.owns_position("testnet", "SOL", pid, root=self.e.root))
        self.assertFalse(planlink.owns_position("testnet", "SOL", "PL-0101-SOL-99", root=self.e.root))
        self.assertFalse(planlink.owns_position("mainnet", "SOL", pid, root=self.e.root))
        self.assertFalse(planlink.owns_position("testnet", "SOL", pid, root=self.e.root, now=time.time() + 600))

    def test_argparse_has_via_plan_flag(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            bb.main(["open", "--help"])
        self.assertIn("--via-plan", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
