import json
import os
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import data  # noqa: E402
from tb.marketdata import history  # noqa: E402

T0 = 1793000040  # кратно 60


def okx_resp(ts_list, price=100.0, confirm="1"):
    rows = [[str((t) * 1000), str(price), str(price + 1), str(price - 1), str(price), "5", "5", "5", confirm]
            for t in sorted(ts_list, reverse=True)]
    return {"code": "0", "data": rows}


class FakeOkx(object):
    """Эмулирует OKX: отдаёт до limit свечей строго старше after, новые первыми."""

    def __init__(self, ts_all, price=lambda t: 100.0):
        self.ts_all = sorted(ts_all)
        self.calls = []
        self.price = price

    def __call__(self, url, params, timeout=10):
        self.calls.append(dict(params))
        after = int(params["after"])
        limit = int(params["limit"])
        sel = [t for t in self.ts_all if t * 1000 < after][-limit:]
        rows = [[str(t * 1000), str(self.price(t)), str(self.price(t) + 1), str(self.price(t) - 1),
                 str(self.price(t)), "5", "5", "5", "1"] for t in sorted(sel, reverse=True)]
        return {"code": "0", "data": rows}


class Base(unittest.TestCase):
    def setUp(self):
        self._orig = (history.http_get_json, history._sleep)
        self.sleeps = []
        history._sleep = self.sleeps.append
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        history.http_get_json, history._sleep = self._orig
        self.tmp.cleanup()


class TestFetch(Base):
    def test_pagination_complete_sorted_no_dupes(self):
        ts = [T0 + 60 * i for i in range(250)]
        fake = FakeOkx(ts)
        history.http_get_json = fake
        rows, bad, pages = history.fetch_range("okx", "ETH", ts[0], ts[-1])
        self.assertEqual([r[0] for r in rows], ts)
        self.assertEqual(pages, 3)
        self.assertEqual(bad, 0)
        self.assertEqual(len(self.sleeps), 2)  # пауза между страницами

    def test_429_backoff_then_success(self):
        ts = [T0 + 60 * i for i in range(10)]
        fake = FakeOkx(ts)
        state = {"n": 0}

        def flaky(url, params, timeout=10):
            state["n"] += 1
            if state["n"] <= 2:
                raise history.HttpError(429, "too many")
            return fake(url, params, timeout)
        history.http_get_json = flaky
        rows, _, _ = history.fetch_range("okx", "ETH", ts[0], ts[-1])
        self.assertEqual(len(rows), 10)
        self.assertEqual(self.sleeps[:2], [1.0, 2.0])  # экспоненциальный backoff

    def test_retry_after_respected_and_5xx(self):
        calls = {"n": 0}

        def f(url, params, timeout=10):
            calls["n"] += 1
            if calls["n"] == 1:
                raise history.HttpError(429, "", retry_after=7.0)
            if calls["n"] == 2:
                raise history.HttpError(503, "")
            return {"ok": 1}
        history.http_get_json = f
        self.assertEqual(history.fetch_json("u", {}), {"ok": 1})
        self.assertEqual(self.sleeps, [7.0, 2.0])

    def test_4xx_not_retried(self):
        def f(url, params, timeout=10):
            raise history.HttpError(403, "")
        history.http_get_json = f
        with self.assertRaises(history.FetchError):
            history.fetch_json("u", {})
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_retries(self):
        def f(url, params, timeout=10):
            raise history.HttpError(500, "")
        history.http_get_json = f
        with self.assertRaises(history.FetchError):
            history.fetch_json("u", {}, retries=3)
        self.assertEqual(len(self.sleeps), 3)

    def test_okx_body_rate_limit_code(self):
        seq = [{"code": "50011", "msg": "rate"}, {"code": "0", "data": []}]
        history.http_get_json = lambda u, p, t=10: seq.pop(0)
        self.assertEqual(history.fetch_json("u", {})["code"], "0")

    def test_unconfirmed_candle_and_garbage_skipped(self):
        resp = okx_resp([T0], confirm="0")
        resp["data"].append(["bad"])
        rows, bad = history.parse_okx(resp)
        self.assertEqual(rows, [])
        self.assertEqual(bad, 1)

    def test_okx_error_code_raises(self):
        with self.assertRaises(history.FetchError):
            history.parse_okx({"code": "51001", "msg": "x", "data": []})

    def test_parse_bybit(self):
        resp = {"retCode": 0, "result": {"list": [[str(T0 * 1000), "1", "2", "0.5", "1.5", "9", "9"], ["x"]]}}
        rows, bad = history.parse_bybit(resp)
        self.assertEqual(rows, [(T0, 1.0, 2.0, 0.5, 1.5, 9.0)])
        self.assertEqual(bad, 1)

    def test_bybit_pagination(self):
        ts = [T0 + 60 * i for i in range(30)]

        def f(url, params, timeout=10):
            end = int(params["end"]) // 1000
            sel = [t for t in ts if t <= end][-10:]  # страница по 10 для теста
            return {"retCode": 0, "result": {"list": [[str(t * 1000), "1", "2", "0.5", "1", "1", "1"]
                                                     for t in sorted(sel, reverse=True)]}}
        history.http_get_json = f
        rows, _, pages = history.fetch_range("bybit", "BTC", ts[0], ts[-1])
        self.assertEqual([r[0] for r in rows], ts)
        self.assertEqual(pages, 3)


class TestValidateAndStore(Base):
    def test_validate_outlier_flagged_not_removed(self):
        rows = [(T0, 100, 101, 99, 100.0, 1), (T0 + 60, 100, 140, 99, 130.0, 1), (T0 + 120, 130, 131, 129, 130.0, 1)]
        clean, rep = history.validate(rows)
        self.assertEqual(len(clean), 3)
        self.assertEqual(rep["outliers"], 1)
        self.assertEqual(rep["dropped"], 0)

    def test_validate_zero_price_dropped_and_reported(self):
        rows = [(T0, 100, 101, 99, 100.0, 1), (T0 + 60, 0, 0, 0, 0.0, 0), (T0 + 120, 100, 101, 99, 100.0, 1)]
        clean, rep = history.validate(rows)
        self.assertEqual(len(clean), 2)
        self.assertEqual(rep["dropped"], 1)
        self.assertEqual(rep["gaps"], 1)
        self.assertEqual(rep["missing_bars_total"], 1)

    def test_validate_inconsistent_ohlc(self):
        clean, rep = history.validate([(T0, 100, 90, 99, 100.0, 1)])
        self.assertEqual(clean, [])
        self.assertEqual(rep["dropped"], 1)

    def test_merge_dedup_later_wins(self):
        a = [(T0, 1, 1, 1, 1.0, 1), (T0 + 60, 1, 1, 1, 1.0, 1)]
        b = [(T0 + 60, 2, 2, 2, 2.0, 1), (T0 - 60, 3, 3, 3, 3.0, 1)]
        m = history.merge_candles(a, b)
        self.assertEqual([r[0] for r in m], [T0 - 60, T0, T0 + 60])
        self.assertEqual(m[2][4], 2.0)

    def test_outputs_compatible_with_load_ticks(self):
        rows = [(T0 + 60 * i, 100 + i, 102 + i, 99 + i, 101.0 + i, 3.0) for i in range(5)]
        c, t = history.write_outputs(self.out, "ETH", rows)
        ticks = data.load_ticks(t)
        self.assertEqual(len(ticks), 5)
        self.assertEqual(ticks[0], (float(T0), 101.0))
        self.assertEqual(len(history.read_candles(c)), 5)
        with open(c) as f:
            self.assertEqual(f.readline().strip(), "ts_iso,o,h,l,c,volume")
        bars = data.to_bars(ticks, 60)
        self.assertEqual(len(bars), 5)
        self.assertFalse(os.path.exists(os.path.join(self.out, "candles_ETH.csv.tmp")))

    def test_run_resume_and_live_file_untouched(self):
        live = os.path.join(self.out, "ticks_ETH.csv")
        data.append_tick(live, "2026-10-27T06:30:00Z", 2300.0)
        with open(live) as f:
            before = f.read()
        now = T0 + 60 * 200 + 30
        until = int(now // 60) * 60 - 60
        ts_all = list(range(until - 60 * 150, until + 1, 60))
        fake = FakeOkx(ts_all)
        history.http_get_json = fake
        info = history.run("ETH", 100 / 1440.0, self.out, "okx", now=now)
        self.assertTrue(info["ok"], info)
        n1 = len(history.read_candles(os.path.join(self.out, "candles_ETH.csv")))
        self.assertGreater(n1, 90)
        calls1 = len(fake.calls)
        # докачка: появились новые свечи, запрос идёт только с последней метки
        now2 = now + 600
        until2 = int(now2 // 60) * 60 - 60
        fake2 = FakeOkx(list(range(until - 60 * 150, until2 + 1, 60)))
        history.http_get_json = fake2
        info2 = history.run("ETH", 100 / 1440.0, self.out, "okx", now=now2)
        self.assertTrue(info2["resume"])
        self.assertEqual(len(fake2.calls), 1)
        self.assertLessEqual(len(fake2.calls), calls1)
        rows = history.read_candles(os.path.join(self.out, "candles_ETH.csv"))
        self.assertEqual(rows[-1][0], until2)
        self.assertEqual(len(rows), len(set(r[0] for r in rows)))  # без дублей
        with open(live) as f:
            self.assertEqual(f.read(), before)

    def test_fallback_to_bybit(self):
        now = T0 + 60 * 20
        until = int(now // 60) * 60 - 60
        ts = list(range(until - 600, until + 1, 60))

        def f(url, params, timeout=10):
            if "okx" in url:
                raise history.HttpError(403, "geo")
            end = int(params["end"]) // 1000
            sel = [t for t in ts if t <= end]
            return {"retCode": 0, "result": {"list": [[str(t * 1000), "1", "2", "0.5", "1", "1", "1"]
                                                     for t in sorted(sel, reverse=True)]}}
        history.http_get_json = f
        info = history.run("BTC", 600 / 86400.0, self.out, "okx", now=now)
        self.assertTrue(info["ok"])
        self.assertEqual(info["used_source"], "bybit")
        self.assertEqual(len(info["errors"]), 1)

    def test_dry_run_no_network_no_write(self):
        def boom(*a, **k):
            raise AssertionError("сеть в dry-run")
        history.http_get_json = boom
        info = history.run("ETH", 1, self.out, "okx", dry_run=True, now=T0)
        self.assertTrue(info["dry_run"])
        self.assertEqual(os.listdir(self.out), [])

    def test_cli_dry_run(self):
        self.assertEqual(history.main(["--sym", "ETH", "--days", "1", "--out", self.out, "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
