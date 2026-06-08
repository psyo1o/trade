# -*- coding: utf-8 -*-
"""잔고 TTL 캐시 — balance_read."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from execution import balance_read as br


class TestBalanceReadCache(unittest.TestCase):
    def setUp(self):
        br.invalidate()
        self._ledger_patcher = patch.object(br, "_ledger_only", return_value=False)
        self._ledger_patcher.start()

    def tearDown(self):
        self._ledger_patcher.stop()
        br.invalidate()

    def test_kr_cache_hits_fetch_once(self):
        bal = {"output1": [{"pdno": "005930", "hldg_qty": "10"}]}
        with patch("api.kis_api._fetch_kr_balance_with_backoff", return_value=bal) as m:
            q1 = br.kr_stock_qty("005930", refresh=False)
            q2 = br.kr_stock_qty("005930", refresh=False)
        self.assertEqual(q1, 10.0)
        self.assertEqual(q2, 10.0)
        self.assertEqual(m.call_count, 1)

    def test_refresh_fetches_again(self):
        b1 = {"output1": [{"pdno": "005930", "hldg_qty": "10"}]}
        b2 = {"output1": [{"pdno": "005930", "hldg_qty": "8"}]}
        with patch(
            "api.kis_api._fetch_kr_balance_with_backoff",
            side_effect=[b1, b2],
        ) as m:
            self.assertEqual(br.kr_stock_qty("005930", refresh=False), 10.0)
            br._last_api_mono.pop("KR", None)
            self.assertEqual(br.kr_stock_qty("005930", refresh=True), 8.0)
        self.assertEqual(m.call_count, 2)

    def test_invalidate_clears(self):
        br._cache["KR"] = (0.0, {"output1": []})
        br.invalidate("KR")
        self.assertNotIn("KR", br._cache)

    def test_report_helpers_delegate(self):
        bal = {"output1": []}
        with patch("api.kis_api._fetch_kr_balance_with_backoff", return_value=bal) as m:
            br.kr_balance_for_report(refresh=True)
            br.kr_balance_for_report(refresh=False)
        self.assertEqual(m.call_count, 1)

    def test_stale_on_rate_limit(self):
        good = {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "10"}]}
        bad = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}
        with patch(
            "api.kis_api._fetch_kr_balance_with_backoff",
            side_effect=[good, bad],
        ) as m:
            br.kr_balance_raw(refresh=True)
            br.kr_balance_raw(refresh=False)
            q = br.kr_stock_qty("005930", refresh=False)
        self.assertEqual(q, 10.0)
        self.assertEqual(m.call_count, 1)

    def test_min_interval_reuses_cache_on_non_refresh(self):
        bal = {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "3"}]}
        with patch("api.kis_api._fetch_kr_balance_with_backoff", return_value=bal) as m:
            br.kr_balance_raw(refresh=True)
            br.kr_balance_raw(refresh=False)
        self.assertEqual(m.call_count, 1)

    def test_refresh_bypasses_min_interval(self):
        b1 = {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "3"}]}
        b2 = {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "1"}]}
        with patch(
            "api.kis_api._fetch_kr_balance_with_backoff",
            side_effect=[b1, b2],
        ) as m:
            br.kr_balance_raw(refresh=True)
            br.kr_balance_raw(refresh=True)
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
