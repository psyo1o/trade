# -*- coding: utf-8 -*-
"""잔고 TTL 캐시 — balance_read."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from execution import balance_read as br


class TestBalanceReadCache(unittest.TestCase):
    def setUp(self):
        br.invalidate()

    def test_kr_cache_hits_fetch_once(self):
        bal = {"output1": [{"pdno": "005930", "hldg_qty": "10"}]}
        with patch("api.kis_api.get_balance_with_retry", return_value=bal) as m:
            q1 = br.kr_stock_qty("005930", refresh=False)
            q2 = br.kr_stock_qty("005930", refresh=False)
        self.assertEqual(q1, 10.0)
        self.assertEqual(q2, 10.0)
        self.assertEqual(m.call_count, 1)

    def test_refresh_fetches_again(self):
        b1 = {"output1": [{"pdno": "005930", "hldg_qty": "10"}]}
        b2 = {"output1": [{"pdno": "005930", "hldg_qty": "8"}]}
        with patch(
            "api.kis_api.get_balance_with_retry",
            side_effect=[b1, b2],
        ) as m:
            self.assertEqual(br.kr_stock_qty("005930", refresh=False), 10.0)
            self.assertEqual(br.kr_stock_qty("005930", refresh=True), 8.0)
        self.assertEqual(m.call_count, 2)

    def test_invalidate_clears(self):
        br._cache["KR"] = (0.0, {"output1": []})
        br.invalidate("KR")
        self.assertNotIn("KR", br._cache)

    def test_report_helpers_delegate(self):
        bal = {"output1": []}
        with patch("api.kis_api.get_balance_with_retry", return_value=bal) as m:
            br.kr_balance_for_report(refresh=True)
            br.kr_balance_for_report(refresh=False)
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
