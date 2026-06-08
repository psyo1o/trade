# -*- coding: utf-8 -*-
"""V8·스윙 매도선 — 절대 손절 캡 제거 후 기술선만 반영."""
from __future__ import annotations

import unittest

import strategy.rules as rules
from strategy.rules import (
    _v8_technical_stop_floor_from_ohlcv,
    get_final_exit_price,
    get_swing_hard_stop_floor,
)


def _ohlcv_rows(close: float, n: int = 60):
    return [{"o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1e6}] * n


class TestExitLineNoPctCap(unittest.TestCase):
    def test_v8_cap_helpers_removed(self):
        self.assertFalse(hasattr(rules, "_v8_max_stop_cap_floor"))
        self.assertFalse(hasattr(rules, "_swing_max_stop_cap_floor"))
        self.assertFalse(hasattr(rules, "V8_MAX_STOP_CAP_MULT_EQUITY"))

    def test_v8_final_exit_chandelier_and_technical_only(self):
        buy = 10_000.0
        cp = buy
        pos = {
            "buy_p": buy,
            "max_p": buy,
            "sl_p": buy * 0.5,
            "current_atr": buy * 0.05,
        }
        ohlcv = _ohlcv_rows(buy)
        sl_fb = buy * 0.5
        locked_chandelier = max(buy - buy * 0.05 * 2.5, sl_fb)
        technical = _v8_technical_stop_floor_from_ohlcv(ohlcv, cp)
        expected = max(locked_chandelier, technical) if technical > 0 else locked_chandelier
        line = get_final_exit_price("005930", cp, pos, ohlcv)
        self.assertAlmostEqual(line, expected, places=2)

    def test_v8_technical_stop_from_ohlcv(self):
        buy = 100.0
        tech = _v8_technical_stop_floor_from_ohlcv(_ohlcv_rows(buy), buy)
        self.assertGreater(tech, 0)
        self.assertLess(tech, buy)

    def test_swing_hard_floor_fib_cloud_only(self):
        buy = 100.0
        pos = {
            "buy_p": buy,
            "entry_fib_level": 88.0,
            "strategy_type": "SWING_FIB",
        }
        ohlcv = _ohlcv_rows(buy)
        floor = get_swing_hard_stop_floor(pos, ohlcv, market="KR", ticker="005930")
        self.assertAlmostEqual(floor, 88.0)
        self.assertLess(floor, buy * 0.95)

    def test_v8_breakeven_lock_exit_label(self):
        import run_bot as rb

        buy = 140_100.0
        lock = buy * 1.005
        pos_done = {"scale_out_done": True}
        pos_open = {"scale_out_done": False}
        self.assertTrue(rb._v8_loss_stop_is_breakeven_lock(buy, pos_done, lock))
        reason, log = rb._v8_loss_zone_exit_meta(buy, pos_done, lock, 139_000.0, market="KR")
        self.assertIn("본절락", reason)
        self.assertIn("본절락", log)
        self.assertFalse(rb._v8_loss_stop_is_breakeven_lock(buy, pos_open, lock))
        reason2, _ = rb._v8_loss_zone_exit_meta(buy, pos_open, buy * 0.9, 130_000.0, market="KR")
        self.assertIn("하드스탑", reason2)


if __name__ == "__main__":
    unittest.main()
