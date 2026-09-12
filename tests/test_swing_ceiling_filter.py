# -*- coding: utf-8 -*-
"""스윙 전고점 이격도 차단(Ceiling) — V8에는 미적용."""
from __future__ import annotations

import inspect
import unittest

import pandas as pd

from strategy.rules import (
    SWING_CEILING_LOOKBACK,
    SWING_CEILING_NEAR_PCT,
    _swing_ceiling_block,
    calculate_pro_signals,
    check_swing_entry,
)


def _df_with_peak(peak_high: float, last_close: float, n: int = 60) -> pd.DataFrame:
    rows = []
    for i in range(n - 1):
        c = 80.0
        h = peak_high if i == 10 else c * 1.01
        rows.append({"o": c * 0.99, "h": h, "l": c * 0.98, "c": c, "v": 400_000.0})
    rows.append(
        {
            "o": last_close * 0.99,
            "h": max(last_close * 1.002, last_close),
            "l": last_close * 0.98,
            "c": last_close,
            "v": 200_000.0,
        }
    )
    return pd.DataFrame(rows)


class TestSwingCeilingFilter(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(SWING_CEILING_LOOKBACK, 60)
        self.assertEqual(SWING_CEILING_NEAR_PCT, 5.0)

    def test_blocks_within_5pct_of_60d_high(self):
        df = _df_with_peak(100.0, 96.0)
        ok, fib, reason = check_swing_entry(df, market="KR", reference_close=96.0)
        self.assertFalse(ok)
        self.assertEqual(fib, 0.0)
        self.assertIn("전고점 바짝 근접", reason)
        self.assertIn("상투 잡기 방지", reason)
        self.assertIn("현재가: 96", reason)
        self.assertIn("전고점: 100", reason)

    def test_allows_when_more_than_5pct_below_high(self):
        df = _df_with_peak(100.0, 90.0)
        blocked, why = _swing_ceiling_block(df, 90.0)
        self.assertFalse(blocked)
        self.assertEqual(why, "")
        ok, _, reason = check_swing_entry(df, market="KR", reference_close=90.0)
        self.assertNotIn("전고점 바짝 근접", reason)
        self.assertFalse(ok)  # 다른 스윙 조건은 일부러 안 맞춤

    def test_exact_95pct_is_blocked(self):
        blocked, why = _swing_ceiling_block(_df_with_peak(100.0, 95.0), 95.0)
        self.assertTrue(blocked)
        self.assertIn("전고점 바짝 근접", why)

    def test_v8_pro_signals_does_not_use_ceiling(self):
        src = inspect.getsource(calculate_pro_signals)
        self.assertNotIn("_swing_ceiling_block", src)
        self.assertNotIn("전고점 바짝 근접", src)
        self.assertNotIn("SWING_CEILING", src)


if __name__ == "__main__":
    unittest.main()
