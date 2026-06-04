# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from strategy.entry_router import decide_entry_signals


class TestStrategyRouter(unittest.TestCase):
    def test_v8_and_swing_both_evaluated(self):
        ohlcv = [{"o": 1, "c": 2}]
        with patch(
            "strategy.entry_router.calculate_pro_signals",
            return_value=(True, 123.4, "TREND_V8"),
        ) as m_v8:
            with patch(
                "strategy.entry_router.check_swing_entry",
                return_value=(True, 111.1, "ok"),
            ) as m_sw:
                d = decide_entry_signals(
                    ohlcv,
                    "BULL",
                    "AAPL",
                    "AAPL",
                    1,
                    10,
                    market="US",
                    reference_close=222.2,
                )
        self.assertTrue(d.is_buy)
        self.assertEqual(d.signal_name, "TREND_V8")
        self.assertTrue(d.swing_ok)
        self.assertAlmostEqual(d.swing_fib, 111.1)
        m_v8.assert_called_once()
        m_sw.assert_called_once()

    def test_swing_fallback_data_preserved(self):
        ohlcv = [{"o": 10, "c": 9}]
        with patch(
            "strategy.entry_router.calculate_pro_signals",
            return_value=(False, 0.0, ""),
        ):
            with patch(
                "strategy.entry_router.check_swing_entry",
                return_value=(False, 0.0, "no setup"),
            ):
                d = decide_entry_signals(
                    ohlcv,
                    "BEAR",
                    "005930",
                    "삼성전자",
                    3,
                    20,
                    market="KR",
                    reference_close=None,
                )
        self.assertFalse(d.is_buy)
        self.assertFalse(d.swing_ok)
        self.assertEqual(d.swing_why, "no setup")


if __name__ == "__main__":
    unittest.main()
