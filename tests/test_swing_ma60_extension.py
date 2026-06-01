# -*- coding: utf-8 -*-
"""스윙 진입 — 시장별 60MA 이격 상한."""
from __future__ import annotations

import unittest

import pandas as pd

from strategy.rules import (
    SWING_MA60_MAX_EXTENSION_PCT_COIN,
    SWING_MA60_MAX_EXTENSION_PCT_KR,
    SWING_MA60_MAX_EXTENSION_PCT_US,
    check_swing_entry,
    swing_ma60_max_extension_pct,
)


def _df_flat_closes(closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        o = c * 0.995
        rows.append({"o": o, "h": c * 1.002, "l": o, "c": c, "v": 500_000.0})
    return pd.DataFrame(rows)


class TestSwingMa60ExtensionByMarket(unittest.TestCase):
    def test_constants_and_helper(self):
        self.assertEqual(swing_ma60_max_extension_pct("US"), SWING_MA60_MAX_EXTENSION_PCT_US)
        self.assertEqual(swing_ma60_max_extension_pct("KR"), SWING_MA60_MAX_EXTENSION_PCT_KR)
        self.assertEqual(swing_ma60_max_extension_pct("COIN"), SWING_MA60_MAX_EXTENSION_PCT_COIN)
        self.assertEqual(SWING_MA60_MAX_EXTENSION_PCT_US, 15.0)
        self.assertEqual(SWING_MA60_MAX_EXTENSION_PCT_KR, 20.0)
        self.assertEqual(SWING_MA60_MAX_EXTENSION_PCT_COIN, 30.0)

    def _df_with_extension_pct(self, ext_pct: float) -> pd.DataFrame:
        """마지막 봉만 60MA 대비 ext_pct 이격 (앞 59봉은 100 고정)."""
        base = [100.0] * 59
        ma60_approx = 100.0
        last = ma60_approx * (1.0 + ext_pct / 100.0)
        return _df_flat_closes(base + [last])

    def test_us_blocks_above_15pct(self):
        df = self._df_with_extension_pct(18.0)
        ok, _, reason = check_swing_entry(df, market="US", reference_close=float(df.iloc[-1]["c"]))
        self.assertFalse(ok)
        self.assertIn("60MA 이격 과다", reason)
        self.assertIn("US", reason)
        self.assertIn(">15", reason)

    def test_kr_blocks_above_20pct(self):
        df = self._df_with_extension_pct(22.0)
        ok, _, reason = check_swing_entry(df, market="KR", reference_close=float(df.iloc[-1]["c"]))
        self.assertFalse(ok)
        self.assertIn("60MA 이격 과다", reason)
        self.assertIn("KR", reason)
        self.assertIn(">20", reason)

    def test_coin_allows_25pct_rejects_35pct(self):
        df_ok = self._df_with_extension_pct(25.0)
        ok25, _, r25 = check_swing_entry(
            df_ok, market="COIN", reference_close=float(df_ok.iloc[-1]["c"])
        )
        if not ok25:
            self.assertNotIn(">30", r25)

        df_bad = self._df_with_extension_pct(35.0)
        ok35, _, reason = check_swing_entry(
            df_bad, market="COIN", reference_close=float(df_bad.iloc[-1]["c"])
        )
        self.assertFalse(ok35)
        self.assertIn("60MA 이격 과다", reason)
        self.assertIn(">30", reason)


if __name__ == "__main__":
    unittest.main()
