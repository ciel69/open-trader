"""WP1B: доработки исполнителя: keep-alive транспорт с полосами, страж демона (аварийный KILL flatten), быстрое завершение, guard_preview с size_mult,
теневые планы в бумажном профиле, событие kill ровно один раз. Без сети и без живых демонов; корень временный (стенд tests/fakes/harness.py)."""
import asyncio
import contextlib
import io
import json
import os
import threading
import time
import unittest

import _bootstrap  # noqa: F401
from fakes.harness import Env, plan_long
from tb.core import exec_cfg
from tb.executor import main as xmain
from tb.executor import transport as T
from tb.executor.daemon import Daemon, NullFeed
from tb.venues.bybit import BybitError


# ------------------------------------------------------------------ keep-alive транспорт
class Resp(object):
    def __init__(self, status=200, body=None, will_close=False):
        self.status, self.will_close = status, will_close
        self._b = json.dumps(body if body is not None else {"retCode": 0}).encode()

    def read(self):
        return self._b


class Conn(object):
    def __init__(self, script, log):
        self.script, self.log, self.closed, self.sock, self.timeout = list(script), log, False, None, None
        self.id = len(log["conns"])
        log["conns"].append(self)

    def request(self, method, target, body=None, headers=None):
        self.log["reqs"].append((self.id, method, target, self.timeout))

    def getresponse(self):
        r = self.script.pop(0) if self.script else Resp()
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


def make(scripts=(), **kw):
    log = {"conns": [], "reqs": []}
    scripts = list(scripts)

    def factory(scheme, host, timeout):
        return Conn(scripts.pop(0) if scripts else [], log)
    clock = [100.0]
    tr = T.make_transport(factory=factory, clock=lambda: clock[0], **kw)
    return tr, log, clock


URL = "https://api-testnet.bybit.com"


class KeepAliveTests(unittest.TestCase):
    def test_connection_reused_between_requests(self):
        tr, log, _ = make()
        for _ in range(3):
            self.assertEqual(tr("GET", URL + "/v5/market/time?a=1", {}, None), {"retCode": 0})
        self.assertEqual(len(log["conns"]), 1)
        self.assertEqual(tr.stats, {"connects": 1, "reused": 2})
        self.assertEqual(log["reqs"][0][2], "/v5/market/time?a=1")

    def test_timeouts_per_kind(self):
        tr, log, _ = make()
        tr("GET", URL + "/x", {}, None)
        tr("POST", URL + "/v5/order/create", {}, "{}")
        self.assertEqual([r[3] for r in log["reqs"]], [4.0, 9.0])

    def test_get_retried_once_on_fresh_connection(self):
        tr, log, _ = make([[TimeoutError("t")], [Resp(body={"retCode": 0, "x": 1})]])
        self.assertEqual(tr("GET", URL + "/x", {}, None)["x"], 1)
        self.assertEqual(len(log["conns"]), 2)
        self.assertTrue(log["conns"][0].closed)

    def test_get_gives_up_after_second_failure(self):
        tr, log, _ = make([[TimeoutError("t")], [TimeoutError("t")]])
        with self.assertRaises(TimeoutError):
            tr("GET", URL + "/x", {}, None)
        self.assertEqual(len(log["reqs"]), 2)

    def test_post_never_retried_and_connection_dropped(self):
        tr, log, _ = make([[ConnectionResetError("r")], [Resp()]])
        with self.assertRaises(ConnectionResetError):
            tr("POST", URL + "/v5/order/create", {}, "{}")
        self.assertEqual(len(log["reqs"]), 1)
        tr("POST", URL + "/v5/order/create", {}, "{}")           # следующий запрос идёт на новом соединении
        self.assertEqual(len(log["conns"]), 2)

    def test_lanes_do_not_share_connections(self):
        tr, log, _ = make()
        tr("GET", URL + "/v5/position/list?x=1", {}, None)
        tr("POST", URL + "/v5/order/create", {}, '{"category":"linear","price":"1"}')                     # обычная запись
        tr("POST", URL + "/v5/order/create", {}, '{"category":"linear","reduceOnly":true,"qty":"1"}')     # защитная: закрытие
        tr("POST", URL + "/v5/position/trading-stop", {}, '{"stopLoss":"1"}')                              # защитная: стоп
        ids = [r[0] for r in log["reqs"]]
        self.assertEqual(ids[0], 0)
        self.assertEqual(ids[1], 1)
        self.assertEqual(ids[2], ids[3])           # обе защитные в одной полосе
        self.assertNotIn(ids[2], (ids[0], ids[1]))

    def test_protect_lane_classification(self):
        self.assertTrue(T.is_protective("POST", URL + "/v5/position/trading-stop", "{}"))
        self.assertTrue(T.is_protective("POST", URL + "/v5/order/create", '{"reduceOnly": true}'))
        self.assertFalse(T.is_protective("POST", URL + "/v5/order/create", '{"reduceOnly":false}'))
        self.assertFalse(T.is_protective("POST", URL + "/v5/order/cancel", "{}"))
        self.assertFalse(T.is_protective("GET", URL + "/v5/position/list", None))

    def test_hung_read_connection_does_not_touch_protect_lane(self):
        tr, log, _ = make([[TimeoutError("t")], [TimeoutError("t")], [Resp()]])
        with self.assertRaises(TimeoutError):
            tr("GET", URL + "/v5/position/list", {}, None)           # обе попытки чтения зависли
        r = tr("POST", URL + "/v5/position/trading-stop", {}, "{}")  # защита идёт на своей полосе и своём соединении
        self.assertEqual(r["retCode"], 0)

    def test_idle_connection_not_reused_for_writes(self):
        tr, log, clock = make()
        tr("POST", URL + "/v5/order/create", {}, "{}")
        clock[0] += 5
        tr("POST", URL + "/v5/order/create", {}, "{}")
        self.assertEqual(len(log["conns"]), 1)
        clock[0] += 11                       # дольше idle_post: сервер мог закрыть соединение, исход записи был бы неизвестен
        tr("POST", URL + "/v5/order/create", {}, "{}")
        self.assertEqual(len(log["conns"]), 2)
        tr("GET", URL + "/x", {}, None)
        clock[0] += 20                       # чтение терпит дольше (30 с)
        tr("GET", URL + "/x", {}, None)
        self.assertEqual(len(log["conns"]), 3)

    def test_each_thread_has_own_connection(self):
        tr, log, _ = make()
        tr("GET", URL + "/x", {}, None)
        t = threading.Thread(target=lambda: tr("GET", URL + "/x", {}, None))
        t.start()
        t.join()
        self.assertEqual(len(log["conns"]), 2)

    def test_http_error_and_will_close(self):
        tr, log, _ = make([[Resp(status=403, will_close=True)]])
        with self.assertRaises(BybitError) as cm:
            tr("GET", URL + "/x", {}, None)
        self.assertEqual(cm.exception.http, 403)
        self.assertTrue(log["conns"][0].closed)
        tr("GET", URL + "/x", {}, None)
        self.assertEqual(len(log["conns"]), 2)

    def test_legacy_opener_path_still_works(self):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        tr = T.make_transport(opener=lambda req, timeout=None: R(b'{"retCode": 0}'))
        self.assertEqual(tr("GET", "http://x", {}, None), {"retCode": 0})


# ------------------------------------------------------------------ страж демона и завершение
class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.log = []
        self.d = Daemon(self.e.ex, NullFeed(), log=self.log.append, stuck_s=3.0)

    def tearDown(self):
        self.e.close()

    def test_guardian_flattens_when_worker_stuck_and_kill_flatten(self):
        self.e.enter()
        self.assertEqual(len(self.e.port.positions()), 1)
        exec_cfg.set_kill(flatten=True, reason="тест", root=self.e.root)
        self.assertEqual(self.d.guard_once(), 0)                # рабочий поток свободен: страж молчит
        self.d._busy = time.monotonic() - 10.0                  # завис в REST
        self.assertEqual(self.d.guard_once(), 1)
        self.assertIn("close_market", [c[0] for c in self.e.port.writes()])
        self.e.tick(100.25)                                     # биржа исполняет reduce-only
        self.assertEqual(self.e.port.positions(), [])
        self.assertEqual(len(self.e.decisions("protect", "emergency_flatten")), 1)
        self.assertEqual(self.d.guard_once(), 0)                # не чаще раза в 5 с

    def test_guardian_uses_plan_link_so_fill_belongs_to_plan(self):
        pid = self.e.enter()
        exec_cfg.set_kill(flatten=True, reason="тест", root=self.e.root)
        self.d._busy = time.monotonic() - 10.0
        self.d.guard_once()
        call = [c for c in self.e.port.calls if c[0] == "close_market"][-1]
        self.assertIn("tbx-%d-" % self.e.rt(pid)["plan_n"], str(call))

    def test_guardian_does_nothing_without_flatten_or_when_not_stuck(self):
        self.e.enter()
        self.d._busy = time.monotonic() - 10.0
        self.assertEqual(self.d.guard_once(), 0)                # KILL нет
        exec_cfg.set_kill(flatten=False, reason="тест", root=self.e.root)
        self.assertEqual(self.d.guard_once(), 0)                # KILL без flatten
        self.assertEqual(len(self.e.port.positions()), 1)

    def test_guardian_observe_mode_does_not_trade(self):
        e = Env(observe=True)
        try:
            d = Daemon(e.ex, NullFeed())
            exec_cfg.set_kill(flatten=True, reason="т", root=e.root)
            d._busy = time.monotonic() - 10.0
            self.assertEqual(d.guard_once(), 0)
        finally:
            e.close()

    def test_busy_flag_set_while_processing(self):
        seen = []
        self.e.ex.on_ticker = lambda *a: seen.append(self.d.stuck_for())
        self.d.q.put(("ticker", ("SOL", 1, 1, 1, 1)))
        self.d.q.put(None)
        self.d._work()
        self.assertEqual(len(seen), 1)
        self.assertIsNone(self.d._busy)

    def test_shutdown_drops_backlog_and_is_fast(self):
        self.e.ex.on_ticker = lambda *a: time.sleep(0.05)
        d = Daemon(self.e.ex, NullFeed(), max_seconds=0.4, log=self.log.append, join_s=2.0)
        for _ in range(400):                                     # 20 с работы, если их разбирать
            d.q.put(("ticker", ("SOL", 1, 1, 1, 1)))
        t0 = time.time()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(d.run())
        finally:
            loop.close()
        self.assertLess(time.time() - t0, 5.0)
        self.assertTrue(self.e.ex.stopped)

    def test_shutdown_feed_stop_has_limit(self):
        class Slow(NullFeed):
            async def stop(self):
                await asyncio.sleep(30)
        d = Daemon(self.e.ex, Slow(), max_seconds=0.2, feed_stop_s=0.3)
        t0 = time.time()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(d.run())
        finally:
            loop.close()
        self.assertLess(time.time() - t0, 5.0)


# ------------------------------------------------------------------ guard_preview и size_mult
class PreviewTests(unittest.TestCase):
    def test_guard_preview_scales_with_size_mult(self):
        e = Env()
        try:
            full = e.submit(plan_long(risk={"score": 0.6, "size_mult": 1.0}))
            self.assertTrue(full["ok"], full)
            e.command("cancel", full["plan_id"])
            half = e.submit(plan_long(risk={"score": 0.6, "size_mult": 0.5}))
            self.assertTrue(half["ok"], half)
            a, b = full["guard_preview"], half["guard_preview"]
            self.assertLess(b["qty"], a["qty"] * 0.6)
            self.assertLess(b["risk_usdt"], a["risk_usdt"] * 0.6)
            self.assertGreater(b["risk_usdt"], a["risk_usdt"] * 0.2)     # округление qty до шага биржи даёт не ровно 0.5
            self.assertLess(b["margin"], a["margin"] * 0.6)
            self.assertLessEqual(b["risk_usdt"], a["risk_usdt"])
        finally:
            e.close()


# ------------------------------------------------------------------ теневые планы
def shadow_plan(**over):
    p = plan_long(mode="paper", tags={"experiment": "t-exp", "variant": "a", "setup": "reclaim"},
                  shadow_variants=[{"name": "tight", "patch": {"stop": {"price": 99.4}}}, {"name": "far", "patch": {"targets": [{"id": "t1", "price": 106.0, "frac": 1.0}]}}])
    p.update(over)
    return p


class ShadowTests(unittest.TestCase):
    def test_shadow_plans_created_in_paper_profile(self):
        e = Env(env="paper", with_session=False)
        try:
            r = e.submit(shadow_plan())
            self.assertTrue(r["ok"], r)
            self.assertEqual(sorted(s["name"] for s in r["shadows"]), ["far", "tight"])
            for s in r["shadows"]:
                spec = e.spec(s["plan_id"])
                self.assertEqual(spec["mode"], "paper")
                self.assertEqual(spec["source"], {"kind": "shadow", "parent_plan": r["plan_id"]})
                self.assertEqual(spec["tags"]["variant"], s["name"])
                self.assertEqual(spec["tags"]["experiment"], "t-exp")
                self.assertEqual(e.rt(s["plan_id"])["status"], "armed")
            self.assertEqual(len(e.decisions("event", "shadow_spawn")), 2)
            self.assertEqual(len(e.bus("plan_armed")), 3)
        finally:
            e.close()

    def test_can_start_paper_limit_skips_shadows_parent_stays(self):
        e = Env(env="paper", with_session=False, cfg_over={"paper": {"max_active": 2}})
        try:
            r = e.submit(shadow_plan())
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(r["shadows"]), 1)               # родитель + 1 теневой = лимит 2
            self.assertEqual(len(e.decisions("event", "shadow_skip")), 1)
        finally:
            e.close()

    def test_no_shadows_without_experiment_or_in_live_profile(self):
        e = Env(env="paper", with_session=False)
        try:
            r = e.submit(shadow_plan(tags={"setup": "reclaim"}))
            self.assertTrue(r["ok"], r)
            self.assertNotIn("shadows", r)
        finally:
            e.close()
        e = Env(env="testnet")
        try:
            r = e.submit(shadow_plan(mode="live", session_type="intraday"))
            self.assertTrue(r["ok"], r)
            self.assertNotIn("shadows", r)
        finally:
            e.close()

    def test_invalid_variant_reported_not_fatal(self):
        e = Env(env="paper", with_session=False)
        try:
            p = shadow_plan(risk={"score": 0.6, "size_mult": 0.8},
                            shadow_variants=[{"name": "bad", "patch": {"risk": {"size_mult": 1.0}}}, {"name": "ok", "patch": {"stop": {"price": 99.4}}}])
            r = e.submit(p)
            self.assertTrue(r["ok"], r)
            self.assertEqual([s["name"] for s in r["shadows"]], ["ok"])
        finally:
            e.close()


# ------------------------------------------------------------------ событие kill
class KillOnceTests(unittest.TestCase):
    def halt(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return xmain.cmd_halt(list(args))

    def test_kill_event_once_when_executor_alive(self):
        e = Env()
        try:
            e.timer(2, dt=3)                                    # heartbeat свежий
            self.halt("--reason", "тест")
            e.timer(2, dt=3)
            self.assertEqual([x["d"]["state"] for x in e.bus("kill")], ["on"])
            with contextlib.redirect_stdout(io.StringIO()):
                xmain.cmd_resume([])
            e.timer(2, dt=3)
            self.assertEqual([x["d"]["state"] for x in e.bus("kill")], ["on", "off"])
        finally:
            e.close()

    def test_cli_writes_event_when_executor_dead(self):
        e = Env()
        try:
            from tb import paths
            if os.path.exists(paths.exec_health_file(root=e.root)):
                os.remove(paths.exec_health_file(root=e.root))
            self.halt()
            self.assertEqual([x["d"]["state"] for x in e.bus("kill")], ["on"])
        finally:
            e.close()


if __name__ == "__main__":
    unittest.main()
