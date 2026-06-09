# -*- coding: utf-8 -*-
"""Phase4 KR 환율 Z-Score + 방향성 차단."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from strategy.macro_guard import DEFAULT_KRW_FX_ZSCORE_BLOCK, evaluate_market_macro_buy_permission


class TestMacroFxZScore(unittest.TestCase):
    def test_kr_block_only_when_z_high_and_rising(self):
        blocked = evaluate_market_macro_buy_permission(
            "KR",
            us_put_call_ratio=1.0,
            coin_whale_long_short_ratio=1.0,
            usd_krw_fx={"z_score": 2.1, "is_rising": True},
            krw_fx_zscore_block=DEFAULT_KRW_FX_ZSCORE_BLOCK,
        )
        self.assertFalse(blocked["allowed"])
        self.assertIn("환율 발작 감지", blocked["reason"])
        self.assertIn("Z-Score 2.10", blocked["reason"])

    def test_kr_allow_when_z_high_but_falling(self):
        allowed = evaluate_market_macro_buy_permission(
            "KR",
            us_put_call_ratio=1.0,
            coin_whale_long_short_ratio=1.0,
            usd_krw_fx={"z_score": 2.5, "is_rising": False},
        )
        self.assertTrue(allowed["allowed"])

    def test_kr_allow_when_z_below_threshold(self):
        allowed = evaluate_market_macro_buy_permission(
            "KR",
            us_put_call_ratio=1.0,
            coin_whale_long_short_ratio=1.0,
            usd_krw_fx={"z_score": 1.2, "is_rising": True},
        )
        self.assertTrue(allowed["allowed"])

    def test_kr_pass_when_fx_missing(self):
        allowed = evaluate_market_macro_buy_permission(
            "KR",
            us_put_call_ratio=1.0,
            coin_whale_long_short_ratio=1.0,
            usd_krw_fx=None,
        )
        self.assertTrue(allowed["allowed"])

    @patch("api.macro_data._fetch_realtime_usdkrw_spot")
    @patch("api.macro_data._daily_usdkrw_closes")
    def test_fetch_usd_krw_momentum_zscore(self, mock_daily, mock_spot):
        import pandas as pd

        from api.macro_data import fetch_usd_krw_momentum

        mock_daily.return_value = (
            pd.Series([1400 + i for i in range(25)], dtype=float),
            "USDKRW=X",
        )
        mock_spot.return_value = (1455.0, "USDKRW_1m")

        out = fetch_usd_krw_momentum()
        self.assertIsNotNone(out)
        assert out is not None
        self.assertAlmostEqual(out["prev_spot"], 1424.0)
        self.assertEqual(out["spot"], 1455.0)
        self.assertTrue(out["is_rising"])
        self.assertGreater(out["z_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
