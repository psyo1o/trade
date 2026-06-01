# -*- coding: utf-8 -*-
"""스윙 러너 5MA 트레일링 — 발동·매도선·청산."""
from __future__ import annotations

import unittest

import pandas as pd

from strategy.rules import (
    SWING_MA5_TRAIL_HIGH_KEY,
    SWING_RUNNER_TRAIL_EXIT_REASON,
    SWING_RUNNER_TRAIL_EXIT_REASON_LOG,
    SWING_SCALE_OUT_R_MULT,
    check_swing_exit,
    check_swing_profit_lock_trailing_exit,
    get_swing_exit_display_price,
    get_swing_ma5_price,
    get_swing_ma5_trail_floor,
    is_swing_runner_state,
)


def _ohlcv_closes(closes: list[float]) -> list[dict]:
    return [
        {"o": c, "h": c, "l": c, "c": c, "v": 1_000_000.0}
        for c in closes
    ]


def _pad_to_60(closes: list[float]) -> list[dict]:
    if len(closes) >= 60:
        return _ohlcv_closes(closes[-60:])
    pad = [closes[0]] * (60 - len(closes)) + closes
    return _ohlcv_closes(pad)


class TestSwingRunnerTrailing(unittest.TestCase):
    def test_is_runner_when_scale_out_done(self):
        pos = {"buy_p": 100.0, "max_p": 105.0, "scale_out_done": True}
        self.assertTrue(is_swing_runner_state(pos))

    def test_is_runner_when_max_p_reaches_1_5r(self):
        buy = 100.0
        one_r = 10.0
        pos = {
            "buy_p": buy,
            "max_p": buy + SWING_SCALE_OUT_R_MULT * one_r,
            "entry_initial_risk_1r": one_r,
            "scale_out_done": False,
        }
        self.assertTrue(is_swing_runner_state(pos))

    def test_ma5_last_bar(self):
        closes = [float(x) for x in range(100, 110)]
        ohlcv = _ohlcv_closes(closes)
        ma5 = get_swing_ma5_price(ohlcv)
        self.assertAlmostEqual(ma5, sum(closes[-5:]) / 5.0, places=4)

    def test_exit_display_includes_ma5_for_runner(self):
        closes = [float(100 + i) for i in range(60)]
        ohlcv = _ohlcv_closes(closes)
        buy = 100.0
        curr = float(closes[-1])
        pos = {
            "buy_p": buy,
            "max_p": 120.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": True,
            "entry_initial_risk_1r": 5.0,
        }
        ma5 = get_swing_ma5_price(ohlcv, reference_price=curr)
        line = get_swing_exit_display_price(
            curr, pos, ohlcv, market="KR", ticker="005930"
        )
        self.assertGreaterEqual(line, ma5)
        self.assertGreater(ma5, buy)

    def test_check_swing_exit_full_on_ma5_break(self):
        closes = [100.0] * 55 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        ohlcv = _ohlcv_closes(closes)
        curr = 101.0
        ma5 = get_swing_ma5_price(ohlcv, reference_price=curr)
        pos = {
            "buy_p": 100.0,
            "max_p": 120.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": True,
            "entry_initial_risk_1r": 5.0,
        }
        self.assertGreater(ma5, curr)
        action, reason = check_swing_exit(
            pos,
            pd.DataFrame(ohlcv),
            reference_price=curr,
            market="KR",
            ticker="005930",
        )
        self.assertEqual(action, "FULL")
        self.assertIn(SWING_RUNNER_TRAIL_EXIT_REASON, reason)

    def test_profit_lock_trailing_runner_uses_ma5_not_breakeven(self):
        closes = [100.0] * 55 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        ohlcv = _ohlcv_closes(closes)
        pos = {
            "buy_p": 100.0,
            "max_p": 120.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": True,
            "entry_initial_risk_1r": 5.0,
        }
        hit, reason = check_swing_profit_lock_trailing_exit(
            101.0, pos, ohlcv=ohlcv, market="KR", ticker="005930"
        )
        self.assertTrue(hit)
        self.assertIn(SWING_RUNNER_TRAIL_EXIT_REASON_LOG, reason)

    def test_ma5_trail_ratchet_does_not_drop(self):
        closes = [float(100 + i) for i in range(60)]
        ohlcv = _ohlcv_closes(closes)
        pos = {
            "buy_p": 100.0,
            "max_p": 120.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": True,
            "entry_initial_risk_1r": 5.0,
        }
        high_px = float(closes[-1])
        high_trail = get_swing_ma5_trail_floor(pos, ohlcv, reference_price=high_px)
        self.assertGreater(high_trail, 0)
        low_px = high_px - 5.0
        low_trail = get_swing_ma5_trail_floor(pos, ohlcv, reference_price=low_px)
        self.assertEqual(low_trail, high_trail)
        self.assertEqual(float(pos[SWING_MA5_TRAIL_HIGH_KEY]), high_trail)

    def test_exit_display_ratchet_never_below_prior_sl(self):
        closes = [float(100 + i) for i in range(60)]
        ohlcv = _ohlcv_closes(closes)
        pos = {
            "buy_p": 100.0,
            "max_p": 120.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": True,
            "entry_initial_risk_1r": 5.0,
        }
        high = float(closes[-1])
        line_hi = get_swing_exit_display_price(
            high, pos, ohlcv, market="KR", ticker="005930"
        )
        line_lo = get_swing_exit_display_price(
            high - 8.0, pos, ohlcv, market="KR", ticker="005930"
        )
        self.assertGreaterEqual(line_lo, line_hi)

    def test_profit_lock_trailing_non_runner_unchanged(self):
        buy = 100.0
        pos = {
            "buy_p": buy,
            "max_p": 110.0,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "scale_out_done": False,
            "entry_initial_risk_1r": 20.0,
        }
        ohlcv = _pad_to_60([float(100 + i % 3) for i in range(60)])
        hit, _ = check_swing_profit_lock_trailing_exit(
            buy * 1.004, pos, ohlcv=ohlcv, market="KR", ticker="005930"
        )
        self.assertTrue(hit)


if __name__ == "__main__":
    unittest.main()
