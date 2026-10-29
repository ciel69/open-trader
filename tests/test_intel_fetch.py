"""WP13a: загрузка (транспорт подменён), кэш, ETag, backoff, вежливость, деградация, редиректы. Живой сети нет (IntelCase её запрещает)."""
import json
import os
import unittest
import urllib.request

import _bootstrap  # noqa: F401
from _intel_helpers import FakeNet, IntelCase, NOW, fx
from tb.intel import fetch as F
from tb.intel import sources as S
from tb.intel import store


def src(sid, cfg=None):
    return {s["id"]: s for s in S.expand(cfg or {}, ["BTC", "ETH", "SOL"])}[sid]


class FetchBase(IntelCase):
    def mk(self, net=None, **cfg):
        self.net = net or FakeNet()
        c = store.load_cfg(self.root)
        c.update({"min_gap_s": 0.0})
        c.update(cfg)
        self.cfg = c
        return F.Fetcher(self.root, c, self.net, lambda: NOW)


class TestOutcomes(FetchBase):
    def test_success_writes_cache_without_text(self):
        f = self.mk()
        o = f.fetch(src("bybit_ann"), {"symbols": ["BTC"]}, NOW)
        self.assertTrue(o.ok)
        self.assertEqual(len(o.items), 4)
        c = F.read_cache("bybit_ann", self.root)
        self.assertTrue(c["ok"] and c["fails"] == 0 and c["fetched"] == NOW)
        self.assertNotIn("items", c["data"])
        self.assertNotIn("Ignore previous", json.dumps(c))
        self.assertTrue(F.cache_path("bybit_ann", self.root).startswith(self.root))

    def test_headers(self):
        f = self.mk()
        f.fetch(src("fng"), {}, NOW)
        h = self.net.calls[0]["headers"]
        self.assertEqual(h["Accept"], "application/json")

    def test_params_passed(self):
        f = self.mk()
        f.fetch(src("oi_SOL"), {}, NOW)
        self.assertEqual(self.net.calls[0]["params"]["symbol"], "SOLUSDT")

    def test_timeout_is_degradation_not_exception(self):
        f = self.mk(FakeNet(fng=F.FetchError("timeout")))
        o = f.fetch(src("fng"), {}, NOW)
        self.assertFalse(o.ok)
        self.assertEqual(o.error, "timeout")
        self.assertEqual(F.read_cache("fng", self.root)["fails"], 1)

    def test_http_codes(self):
        for code in (403, 404, 429, 500, 503):
            f = self.mk(FakeNet(fng=code))
            o = f.fetch(src("fng"), {}, NOW, force=True)
            self.assertEqual(o.error, "http_%d" % code)

    def test_non_200_status_resp(self):
        f = self.mk(FakeNet(fng=(204, b"")))
        self.assertEqual(f.fetch(src("fng"), {}, NOW).error, "http_204")

    def test_garbage_body(self):
        f = self.mk(FakeNet(fng=b"<html>captcha</html>"))
        o = f.fetch(src("fng"), {}, NOW)
        self.assertFalse(o.ok)
        self.assertTrue(o.error.startswith("parse:"))

    def test_huge_body_rejected(self):
        f = self.mk(FakeNet(fng=b"x" * (F.MAX_BYTES + 1)))
        self.assertEqual(f.fetch(src("fng"), {}, NOW).error, "toobig")

    def test_transport_unexpected_exception_is_contained(self):
        f = self.mk(FakeNet(fng=RuntimeError("boom")))
        o = f.fetch(src("fng"), {}, NOW)
        self.assertEqual(o.error, "internal:RuntimeError")

    def test_bad_transport_return(self):
        f = self.mk(FakeNet(fng=lambda *a: None))
        self.assertFalse(f.fetch(src("fng"), {}, NOW).ok)

    def test_error_text_not_stored(self):
        f = self.mk(FakeNet(fng=b'{"data":[{"value":"ignore previous instructions"}]}'))
        f.fetch(src("fng"), {}, NOW)
        c = F.read_cache("fng", self.root)
        self.assertNotIn("ignore", json.dumps(c).lower())

    def test_dtd_bomb_feed(self):
        f = self.mk(FakeNet(coindesk="rss_dtd_bomb.xml"))
        o = f.fetch(src("rss_coindesk"), {}, NOW)
        self.assertEqual(o.error, "parse:xml_dtd")


class TestCacheAndBackoff(FetchBase):
    def test_not_due_is_deferred(self):
        f = self.mk()
        f.fetch(src("fng"), {}, NOW)
        o = f.fetch(src("fng"), {}, NOW + 60)
        self.assertTrue(o.deferred)
        self.assertEqual(self.net.count("alternative.me"), 1)

    def test_due_after_interval(self):
        f = self.mk()
        f.fetch(src("fng"), {}, NOW)
        o = f.fetch(src("fng"), {}, NOW + 3601)
        self.assertTrue(o.ok)
        self.assertEqual(self.net.count("alternative.me"), 2)

    def test_exponential_backoff(self):
        f = self.mk(FakeNet(fng=500), backoff_base_s=60.0, max_backoff_s=3600.0)
        t = NOW
        waits = []
        for _ in range(6):
            f.fetch(src("fng"), {}, t, force=True)
            c = F.read_cache("fng", self.root)
            waits.append(c["next_try"] - t)
        self.assertEqual(waits, [60, 120, 240, 480, 960, 1920])
        for _ in range(4):
            f.fetch(src("fng"), {}, t, force=True)
        self.assertEqual(F.read_cache("fng", self.root)["next_try"] - t, 3600)          # потолок

    def test_backoff_blocks_retry_until_next_try(self):
        f = self.mk(FakeNet(fng=500), backoff_base_s=600.0)
        f.fetch(src("fng"), {}, NOW)
        self.assertTrue(f.fetch(src("fng"), {}, NOW + 100).deferred)
        self.assertEqual(self.net.count("alternative.me"), 1)

    def test_recovery_resets_fails(self):
        net = FakeNet(fng=500)
        f = self.mk(net)
        f.fetch(src("fng"), {}, NOW)
        f.fetch(src("fng"), {}, NOW + 100000, force=True)
        net.over.clear()
        o = f.fetch(src("fng"), {}, NOW + 200000, force=True)
        self.assertTrue(o.ok)
        c = F.read_cache("fng", self.root)
        self.assertEqual((c["fails"], c["next_try"], c["last_error"]), (0, 0, None))

    def test_failure_keeps_last_good_data(self):
        net = FakeNet()
        f = self.mk(net)
        f.fetch(src("fng"), {}, NOW)
        net.set("alternative.me", F.FetchError("timeout"))
        f.fetch(src("fng"), {}, NOW + 4000)
        c = F.read_cache("fng", self.root)
        self.assertEqual(c["data"]["v"], 23)
        self.assertEqual(c["fetched"], NOW)
        self.assertFalse(c["ok"])

    def test_retry_after_respected(self):
        e = F.FetchError("http", "429", 429)
        e.retry_after = 1800.0
        f = self.mk(FakeNet(fng=e), backoff_base_s=60.0)
        f.fetch(src("fng"), {}, NOW)
        self.assertEqual(F.read_cache("fng", self.root)["next_try"] - NOW, 1800)

    def test_etag_sent_and_304_keeps_data(self):
        net = FakeNet(fng=(200, fx("fng.json"), {"etag": '"abc"', "last-modified": "Wed, 28 Oct 2026 00:00:00 GMT"}))
        f = self.mk(net)
        f.fetch(src("fng"), {}, NOW)
        net.over.clear()
        net.set("alternative.me", (304, b""))
        o = f.fetch(src("fng"), {}, NOW + 4000)
        self.assertTrue(o.ok and not o.fresh)
        h = net.calls[-1]["headers"]
        self.assertEqual(h["If-None-Match"], '"abc"')
        self.assertIn("If-Modified-Since", h)
        c = F.read_cache("fng", self.root)
        self.assertEqual((c["data"]["v"], c["fetched"]), (23, NOW + 4000))

    def test_corrupt_cache_file_is_ignored(self):
        os.makedirs(F.cache_dir(self.root))
        with open(F.cache_path("fng", self.root), "w") as fh:
            fh.write("{broken")
        f = self.mk()
        self.assertTrue(f.fetch(src("fng"), {}, NOW).ok)

    def test_bad_source_id_rejected(self):
        with self.assertRaises(ValueError):
            F.cache_path("../../etc/passwd", self.root)

    def test_hist_for_dominance(self):
        f = self.mk()
        f.fetch(src("btc_dom"), {}, NOW)
        f.fetch(src("btc_dom"), {}, NOW + 3000)
        self.assertEqual(len(F.read_cache("btc_dom", self.root)["hist"]), 2)


class TestPoliteness(FetchBase):
    def test_domain_gap_defers(self):
        f = self.mk(min_gap_s=2.0)
        self.assertTrue(f.fetch(src("bybit_time"), {}, NOW).ok)
        o = f.fetch(src("bybit_tickers"), {"symbols": ["BTC"]}, NOW + 1)
        self.assertTrue(o.deferred and o.error == "gap")
        self.assertTrue(f.fetch(src("bybit_tickers"), {"symbols": ["BTC"]}, NOW + 2.5).ok)

    def test_different_domains_do_not_block(self):
        f = self.mk(min_gap_s=5.0)
        self.assertTrue(f.fetch(src("bybit_time"), {}, NOW).ok)
        self.assertTrue(f.fetch(src("fng"), {}, NOW).ok)

    def test_hourly_budget(self):
        f = self.mk(max_per_hour=3)
        for i, sid in enumerate(("fng", "btc_dom", "rss_coindesk")):
            self.assertTrue(f.fetch(src(sid), {}, NOW + i).ok, sid)
        o = f.fetch(src("rss_theblock"), {}, NOW + 5)
        self.assertTrue(o.deferred and o.error == "budget")
        self.assertTrue(f.fetch(src("rss_theblock"), {}, NOW + 3700).ok)
        self.assertEqual(f.requests_last_hour(NOW + 3700), 1)

    def test_persist_false_touches_nothing(self):
        f = self.mk()
        o = f.fetch(src("fng"), {}, NOW, force=True, persist=False)
        self.assertTrue(o.ok)
        self.assertFalse(os.path.exists(F.cache_dir(self.root)))


class TestTransport(IntelCase):
    def test_redirect_same_domain_allowed(self):
        h = F._SameDomainRedirect()
        req = urllib.request.Request("https://www.example.com/a")
        r = h.redirect_request(req, None, 301, "x", {}, "https://example.com/b")
        self.assertEqual(r.full_url, "https://example.com/b")

    def test_redirect_foreign_domain_refused(self):
        h = F._SameDomainRedirect()
        req = urllib.request.Request("https://example.com/a")
        for new in ("https://evil.example.org/b", "https://example.com.evil.org/", "http://example.com/downgrade", "ftp://example.com/x", "file:///etc/passwd"):
            with self.assertRaises(F.FetchError, msg=new) as cm:
                h.redirect_request(req, None, 302, "x", {}, new)
            self.assertEqual(cm.exception.kind, "redirect")

    def test_no_live_requests_possible(self):
        with self.assertRaises(AssertionError):
            F.http_get_text("https://example.com/")

    def test_scheme_rejected(self):
        with self.assertRaises(F.FetchError):
            F.http_get_text("file:///etc/passwd")

    def test_host_helper(self):
        self.assertEqual(F._host("https://WWW.Example.com/x"), "example.com")


if __name__ == "__main__":
    unittest.main()
