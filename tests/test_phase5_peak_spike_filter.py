# -*- coding: utf-8 -*-
"""Phase5 peak_total_equity 상향 갱신 스파이크·미장 개장 동결 방어."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.guard import (
    PEAK_TOTAL_EQUITY_KEY,
    PHASE5_LAST_LOOP_TOTAL_KEY,
    apply_phase5_trailing_week_and_cooldown,
    get_phase5_peak_total_equity,
    is_us_regular_open_peak_freeze_kst,
    phase5_peak_raise_block_reason,
)


class TestUsOpenPeakFreeze(unittest.TestCase):
    def test_edt_open_window_blocks(self):
        # 2026-05-22 Fri EDT: US open ~ KST 22:32
        dt = datetime(2026, 5, 22, 22, 32, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertTrue(is_us_regular_open_peak_freeze_kst(dt))

    def test_edt_before_window_allows(self):
        dt = datetime(2026, 5, 22, 22, 29, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertFalse(is_us_regular_open_peak_freeze_kst(dt))

    def test_est_open_window_blocks(self):
        # 2026-01-15 Thu EST: US open ~ KST 23:33
        dt = datetime(2026, 1, 15, 23, 33, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertTrue(is_us_regular_open_peak_freeze_kst(dt))

    def test_weekend_skips_freeze(self):
        dt = datetime(2026, 5, 23, 22, 32, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertFalse(is_us_regular_open_peak_freeze_kst(dt))


class TestEquitySpikeBlock(unittest.TestCase):
    def test_spike_5pct_blocks(self):
        state = {PHASE5_LAST_LOOP_TOTAL_KEY: 4_000_000.0}
        reason = phase5_peak_raise_block_reason(state, 4_220_000.0)
        self.assertIn("equity_spike", reason)

    def test_spike_under_threshold_allows(self):
        state = {PHASE5_LAST_LOOP_TOTAL_KEY: 4_000_000.0}
        reason = phase5_peak_raise_block_reason(state, 4_150_000.0)
        self.assertEqual(reason, "")

    def test_drop_does_not_block(self):
        state = {PHASE5_LAST_LOOP_TOTAL_KEY: 6_000_000.0}
        reason = phase5_peak_raise_block_reason(state, 5_000_000.0)
        self.assertEqual(reason, "")


class TestApplyPhase5PeakRaise(unittest.TestCase):
    def test_spike_does_not_raise_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot_state.json"
            state = {
                PEAK_TOTAL_EQUITY_KEY: 4_500_000.0,
                PHASE5_LAST_LOOP_TOTAL_KEY: 4_360_000.0,
            }
            apply_phase5_trailing_week_and_cooldown(state, 6_204_446.0, path)
            self.assertEqual(get_phase5_peak_total_equity(state), 4_500_000.0)
            self.assertEqual(float(state[PHASE5_LAST_LOOP_TOTAL_KEY]), 6_204_446.0)

    def test_normal_raise_updates_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot_state.json"
            state = {
                PEAK_TOTAL_EQUITY_KEY: 4_500_000.0,
                PHASE5_LAST_LOOP_TOTAL_KEY: 4_480_000.0,
            }
            apply_phase5_trailing_week_and_cooldown(state, 4_520_000.0, path)
            self.assertEqual(get_phase5_peak_total_equity(state), 4_520_000.0)

    def test_freeze_window_blocks_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot_state.json"
            state = {
                PEAK_TOTAL_EQUITY_KEY: 4_360_000.0,
                PHASE5_LAST_LOOP_TOTAL_KEY: 4_360_000.0,
            }
            # 동결+스파이크 둘 다 해당: 22:31 KST 급등 시나리오
            seoul = datetime(2026, 5, 22, 22, 31, tzinfo=ZoneInfo("Asia/Seoul"))
            block = phase5_peak_raise_block_reason(state, 6_204_446.0, seoul)
            self.assertTrue(block)
            # apply는 _seoul_now() 사용 — freeze 테스트는 block_reason 단위로 검증


if __name__ == "__main__":
    unittest.main()
