# -*- coding: utf-8 -*-
"""스윙 매수 보호·평단 위 손절선 -3% 클램프 (KR/US/COIN 공통)."""
from __future__ import annotations

import time
import unittest

from run_bot import _new_buy_sell_protection_blocks, _resolve_sell_loop_strategy_type
from strategy.rules import (
    BREAKEVEN_LOCK_MULT,
    SWING_STOP_ABOVE_ENTRY_FALLBACK_MULT,
    get_swing_exit_display_price,
    get_swing_hard_stop_floor,
    get_swing_profit_lock_activate_pct,
    get_swing_profit_lock_floor,
    swing_entry_sl_p,
)


class TestSellLoopStrategyIsolation(unittest.TestCase):
    def test_tier_swing_fib_without_strategy_type(self):
        pos = {"tier": "SWING_FIB", "buy_p": 100.0}
        self.assertEqual(_resolve_sell_loop_strategy_type(pos), "SWING_FIB")

    def test_missing_fields_defaults_v8(self):
        self.assertEqual(_resolve_sell_loop_strategy_type({}), "TREND_V8")

    def test_explicit_v8(self):
        pos = {"strategy_type": "TREND_V8", "tier": "1/3"}
        self.assertEqual(_resolve_sell_loop_strategy_type(pos), "TREND_V8")


class TestBuySellProtection(unittest.TestCase):
    def test_blocks_loss_within_15min(self):
        buy_time = time.time() - 60
        self.assertTrue(_new_buy_sell_protection_blocks(-0.5, buy_time))

    def test_blocks_small_profit_within_15min(self):
        buy_time = time.time() - 60
        self.assertTrue(_new_buy_sell_protection_blocks(0.5, buy_time))

    def test_allows_after_15min(self):
        buy_time = time.time() - 901
        self.assertFalse(_new_buy_sell_protection_blocks(-0.5, buy_time))

    def test_allows_big_profit_within_15min(self):
        buy_time = time.time() - 60
        self.assertFalse(_new_buy_sell_protection_blocks(1.5, buy_time))


class TestSwingStopAboveEntry(unittest.TestCase):
    def test_hard_floor_clamps_fib_above_entry(self):
        buy = 0.1583
        pos = {"buy_p": buy, "entry_fib_level": 0.20, "strategy_type": "SWING_FIB"}
        ohlcv = [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}] * 60
        floor = get_swing_hard_stop_floor(pos, ohlcv, market="COIN", ticker="USDT-ZBT")
        self.assertAlmostEqual(floor, buy * SWING_STOP_ABOVE_ENTRY_FALLBACK_MULT, places=6)

    def test_swing_entry_sl_p(self):
        buy = 1.0
        raw = 1.2
        self.assertAlmostEqual(swing_entry_sl_p(buy, raw), 0.97, places=6)
        self.assertAlmostEqual(swing_entry_sl_p(buy, 0.9), 0.9, places=6)
        # 평단과 동일해도 Instant Out 방지용 -3%
        self.assertAlmostEqual(swing_entry_sl_p(buy, 1.0), 0.97, places=6)

    def test_swing_check_exit_safety_net_before_hard_compare(self):
        """하드스탑이 평단과 같으면 비교 직전 -3%로 내려 FULL Instant Out 방지."""
        from strategy.rules import check_swing_exit
        import pandas as pd

        buy = 100.0
        pos = {
            "buy_p": buy,
            "avg_price": buy,
            "max_p": buy,
            "entry_fib_level": buy,  # 평단과 동일 → Instant Out 위험
            "strategy_type": "SWING_FIB",
            "scale_out_done": False,
        }
        rows = [{"o": buy, "h": buy * 1.01, "l": buy * 0.99, "c": buy, "v": 1e6}] * 60
        df = pd.DataFrame(rows)
        action, reason = check_swing_exit(
            pos, df, reference_price=buy, market="US", ticker="AAPL"
        )
        self.assertEqual(action, "HOLD")
        self.assertEqual(reason, "")

    def test_profit_lock_inactive_at_or_below_dynamic_threshold(self):
        buy = 100.0
        pos = {"buy_p": buy, "entry_atr": 2.0}
        threshold = get_swing_profit_lock_activate_pct(pos)
        at_threshold = buy * (1 + threshold / 100.0)
        self.assertEqual(get_swing_profit_lock_floor(pos, at_threshold), 0.0)
        self.assertEqual(get_swing_profit_lock_floor(pos, at_threshold + 0.01), buy * BREAKEVEN_LOCK_MULT)

    def test_exit_display_uses_hard_floor_when_lock_inactive(self):
        buy = 100.0
        max_p = 102.0
        pos = {
            "buy_p": buy,
            "max_p": max_p,
            "entry_fib_level": 95.0,
            "entry_atr": 2.0,
            "strategy_type": "SWING_FIB",
        }
        ohlcv = [{"o": h, "h": h, "l": h - 2, "c": h, "v": 1e6} for h in range(100, 160)]
        line = get_swing_exit_display_price(101.0, pos, ohlcv, market="KR", ticker="005930")
        self.assertAlmostEqual(line, 96.0)  # 절대손실 하드캡 buy*0.96

    def test_exit_display_breakeven_lock_above_threshold(self):
        buy = 100.0
        max_p = 110.0
        pos = {
            "buy_p": buy,
            "max_p": max_p,
            "entry_fib_level": 95.0,
            "entry_atr": 2.0,
            "strategy_type": "SWING_FIB",
            "entry_initial_risk_1r": 20.0,
            "scale_out_done": False,
        }
        ohlcv = [{"o": h, "h": h, "l": h - 2, "c": h, "v": 1e6} for h in range(100, 160)]
        line = get_swing_exit_display_price(108.0, pos, ohlcv, market="KR", ticker="005930")
        self.assertAlmostEqual(line, buy * BREAKEVEN_LOCK_MULT)
        threshold = get_swing_profit_lock_activate_pct(pos)
        self.assertGreater(max_p, buy * (1 + threshold / 100.0))


if __name__ == "__main__":
    unittest.main()
