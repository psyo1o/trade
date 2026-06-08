# -*- coding: utf-8 -*-
"""V8·스윙 본절(Breakeven) 수익 락 — 전략별 활성화 조건."""
from __future__ import annotations

import unittest

from strategy.rules import (
    BREAKEVEN_LOCK_MULT,
    SWING_PROFIT_LOCK_ACTIVATE_PCT,
    get_final_exit_price,
    get_swing_profit_lock_floor,
    get_v8_profit_lock_floor,
)


def _ohlcv_rows(close: float, n: int = 60):
    return [{"o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1e6}] * n


class TestV8BreakevenLock(unittest.TestCase):
    def test_v8_lock_inactive_before_scale_out(self):
        buy = 100.0
        pos = {"scale_out_done": False}
        self.assertEqual(get_v8_profit_lock_floor(buy, pos), 0.0)
        pos_high = {"scale_out_done": False, "max_p": 110.0}
        self.assertEqual(get_v8_profit_lock_floor(buy, pos_high), 0.0)

    def test_v8_lock_active_after_scale_out(self):
        buy = 100.0
        pos = {"scale_out_done": True}
        self.assertEqual(get_v8_profit_lock_floor(buy, pos), buy * BREAKEVEN_LOCK_MULT)

    def test_final_exit_includes_breakeven_after_scale_out(self):
        buy = 10_000.0
        pos = {
            "buy_p": buy,
            "max_p": buy,
            "sl_p": buy * 0.5,
            "current_atr": buy * 0.02,
            "scale_out_done": True,
        }
        line = get_final_exit_price("005930", buy, pos, _ohlcv_rows(buy))
        self.assertGreaterEqual(line, buy * BREAKEVEN_LOCK_MULT)

    def test_final_exit_no_breakeven_before_scale_out(self):
        buy = 10_000.0
        max_p = 10_500.0
        pos = {
            "buy_p": buy,
            "max_p": max_p,
            "sl_p": buy * 0.5,
            "current_atr": buy * 0.02,
            "scale_out_done": False,
        }
        line = get_final_exit_price("005930", buy, pos, _ohlcv_rows(buy))
        self.assertLess(line, buy * BREAKEVEN_LOCK_MULT)
        self.assertGreater(max_p, buy * (1 + SWING_PROFIT_LOCK_ACTIVATE_PCT / 100))


class TestSwingBreakevenLockUnchanged(unittest.TestCase):
    def test_swing_still_uses_max_p_threshold(self):
        buy = 100.0
        self.assertEqual(get_swing_profit_lock_floor(buy, 103.0), 0.0)
        self.assertEqual(get_swing_profit_lock_floor(buy, 103.1), buy * BREAKEVEN_LOCK_MULT)


if __name__ == "__main__":
    unittest.main()
