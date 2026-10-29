import unittest

import _bootstrap  # noqa: F401
from tb.marketdata import funding_history as fh
from tb.marketdata import history


def page(rows):
    return {"result": {"list": [{"symbol": "ETHUSDT", "fundingRate": str(r), "fundingRateTimestamp": str(ts * 1000)}
                                for ts, r in rows]}}


class FundingTest(unittest.TestCase):
    def test_parse_and_bad_rows(self):
        resp = page([(100, 0.0001)])
        resp["result"]["list"].append({"fundingRate": "x"})
        rows, bad = fh.parse(resp)
        self.assertEqual(rows, [(100, 0.0001)])
        self.assertEqual(bad, 1)

    def test_unexpected_response_raises(self):
        with self.assertRaises(history.FetchError):
            fh.parse({"foo": 1})

    def test_pagination_dedup_sorted(self):
        data = [(t * 28800, 0.0001 * t) for t in range(1, 21)]  # старые -> новые
        calls = []

        def fetcher(url, params):
            calls.append(params)
            end = int(params["endTime"]) // 1000
            chunk = [x for x in data if x[0] <= end][-5:]  # по 5 новейших <= end, в обратном порядке
            return page(list(reversed(chunk)))

        rows, bad, pages = fh.fetch("ETH", 0, 20 * 28800, fetcher=fetcher, sleep=lambda s: None)
        self.assertEqual([r[0] for r in rows], [x[0] for x in data])
        self.assertEqual(len(rows), len(set(r[0] for r in rows)))
        self.assertGreaterEqual(pages, 4)

    def test_stops_on_empty(self):
        rows, bad, pages = fh.fetch("ETH", 0, 1000, fetcher=lambda u, p: page([]), sleep=lambda s: None)
        self.assertEqual(rows, [])
        self.assertEqual(pages, 1)


if __name__ == "__main__":
    unittest.main()
