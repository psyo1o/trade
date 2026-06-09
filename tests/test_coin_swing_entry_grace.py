# -*- coding: utf-8 -*-
"""COIN SWING_FIB 진입 초기 기술바닥 FULL 유예."""
from __future__ import annotations

import unittest

import run_bot as rb


class TestCoinSwingEntryGrace(unittest.TestCase):
    def test_defers_tech_floor_full_under_grace(self):
        self.assertTrue(
            rb._coin_swing_entry_noise_defers_tech_floor_full(
                sw_action="FULL",
                sw_reason="스윙 기술바닥 이탈 (현재가: 0.36 < 기준 0.36)",
                hours_held=0.5,
                profit_rate_pct=-0.47,
            )
        )

    def test_no_defer_after_grace_hours(self):
        self.assertFalse(
            rb._coin_swing_entry_noise_defers_tech_floor_full(
                sw_action="FULL",
                sw_reason="스윙 기술바닥 이탈 (현재가: 0.36 < 기준 0.36)",
                hours_held=2.0,
                profit_rate_pct=-0.47,
            )
        )

    def test_hard_cut_bypasses_grace(self):
        self.assertFalse(
            rb._coin_swing_entry_noise_defers_tech_floor_full(
                sw_action="FULL",
                sw_reason="스윙 기술바닥 이탈 (현재가: 0.35 < 기준 0.36)",
                hours_held=0.2,
                profit_rate_pct=-3.1,
            )
        )

    def test_runner_full_not_deferred(self):
        self.assertFalse(
            rb._coin_swing_entry_noise_defers_tech_floor_full(
                sw_action="FULL",
                sw_reason="스윙 5MA 러너 이탈 (현재가: 1 < 5MA: 2)",
                hours_held=0.5,
                profit_rate_pct=5.0,
            )
        )

    def test_half_not_deferred(self):
        self.assertFalse(
            rb._coin_swing_entry_noise_defers_tech_floor_full(
                sw_action="HALF",
                sw_reason="1.5R 1차 익절",
                hours_held=0.1,
                profit_rate_pct=2.0,
            )
        )


if __name__ == "__main__":
    unittest.main()
