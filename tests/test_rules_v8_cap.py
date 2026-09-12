# -*- coding: utf-8 -*-
"""V8·스윙 매도선 — 절대 손실 -4% 하드캡 + 기술선."""
from __future__ import annotations

import unittest

import strategy.rules as rules
from strategy.rules import (
    ABSOLUTE_STOP_MAX_LOSS_MULT,
    _v8_technical_stop_floor_from_ohlcv,
    get_final_exit_price,
    get_swing_hard_stop_floor,
)


def _ohlcv_rows(close: float, n: int = 60):
    return [{"o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1e6}] * n


class TestExitLineAbsoluteStopCap(unittest.TestCase):
    def test_absolute_stop_mult_is_96pct(self):
        self.assertAlmostEqual(ABSOLUTE_STOP_MAX_LOSS_MULT, 0.96)
        self.assertFalse(hasattr(rules, "_v8_max_stop_cap_floor"))
        self.assertFalse(hasattr(rules, "_swing_max_stop_cap_floor"))

    def test_v8_final_exit_raises_deep_technical_to_minus_4pct(self):
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
        raw = max(locked_chandelier, technical) if technical > 0 else locked_chandelier
        expected = max(raw, buy * ABSOLUTE_STOP_MAX_LOSS_MULT)
        line = get_final_exit_price("005930", cp, pos, ohlcv)
        self.assertAlmostEqual(line, expected, places=2)
        self.assertGreaterEqual(line, buy * ABSOLUTE_STOP_MAX_LOSS_MULT - 1e-9)

    def test_v8_no_cap_without_buy_p(self):
        """buy_p 없거나 0이면 하드캡 미적용(기존 경로)."""
        cp = 100.0
        pos = {
            "buy_p": 0,
            "max_p": cp,
            "sl_p": cp * 0.5,
            "current_atr": cp * 0.05,
        }
        line = get_final_exit_price("X", cp, pos, _ohlcv_rows(cp))
        # 캡 없이 기술/샹들리에만 — 0.96*cp 보다 낮을 수 있음
        self.assertGreater(line, 0)

    def test_v8_technical_stop_from_ohlcv(self):
        buy = 100.0
        tech = _v8_technical_stop_floor_from_ohlcv(_ohlcv_rows(buy), buy)
        self.assertGreater(tech, 0)
        self.assertLess(tech, buy)

    def test_v8_final_exit_not_above_entry_before_profit(self):
        """미익절·미락: 20MA−ATR이 평단 위여도 매도선은 평단 이하."""
        buy = 84.5567
        pos = {
            "buy_p": buy,
            "max_p": buy,
            "sl_p": 80.33,
            "current_atr": 0.55,
            "scale_out_done": False,
        }
        ohlcv = []
        for i in range(60):
            c = buy * (0.97 + i * 0.001)
            ohlcv.append(
                {"o": c * 0.999, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1e6}
            )
        line = get_final_exit_price("TLT", buy, pos, ohlcv)
        self.assertLessEqual(line, buy + 1e-9)
        self.assertGreaterEqual(line, buy * ABSOLUTE_STOP_MAX_LOSS_MULT - 1e-6)

    def test_v8_check_pro_exit_safety_net_blocks_instant_out(self):
        """평단 이상 최종선 → 비교 직전 ATR×1.5(없으면 -5%)로 내려 Instant Out 방지."""
        from strategy.rules import check_pro_exit

        buy = 100.0
        pos = {
            "buy_p": buy,
            "avg_price": buy,
            "max_p": buy,
            "sl_p": buy * 1.02,
            "current_atr": 2.0,
            "entry_atr": 4.0,
            "scale_out_done": False,
        }
        ohlcv = [{"o": buy, "h": buy * 1.01, "l": buy * 0.99, "c": buy, "v": 1e6}] * 60
        orig = rules.get_final_exit_price
        try:
            rules.get_final_exit_price = lambda *a, **k: buy * 1.02
            exited, _reason = check_pro_exit("TEST", buy, pos, ohlcv)
        finally:
            rules.get_final_exit_price = orig
        self.assertFalse(exited)
        # entry_atr 우선: 100 - 1.5*4 = 94  (하드캡은 get_final_exit_price 경로)
        self.assertAlmostEqual(pos["sl_p"], buy - 1.5 * 4.0, places=4)
        self.assertLess(pos["sl_p"], buy)

    def test_v8_initial_stop_safety_net_pct_fallback(self):
        from strategy.rules import V8_INITIAL_STOP_BELOW_ENTRY_MULT, _v8_initial_stop_safety_net

        buy = 50.0
        pos = {"max_p": buy, "scale_out_done": False}
        out = _v8_initial_stop_safety_net(buy, buy, pos)
        self.assertAlmostEqual(out, buy * V8_INITIAL_STOP_BELOW_ENTRY_MULT, places=6)

    def test_swing_hard_floor_raises_deep_fib_to_minus_4pct(self):
        buy = 100.0
        pos = {
            "buy_p": buy,
            "entry_fib_level": 88.0,
            "strategy_type": "SWING_FIB",
        }
        ohlcv = _ohlcv_rows(buy)
        floor = get_swing_hard_stop_floor(pos, ohlcv, market="KR", ticker="005930")
        self.assertAlmostEqual(floor, buy * ABSOLUTE_STOP_MAX_LOSS_MULT)
        self.assertGreater(floor, 88.0)

    def test_swing_hard_floor_keeps_shallow_fib(self):
        buy = 100.0
        pos = {
            "buy_p": buy,
            "entry_fib_level": 97.0,
            "strategy_type": "SWING_FIB",
        }
        floor = get_swing_hard_stop_floor(pos, _ohlcv_rows(buy), market="KR", ticker="005930")
        self.assertAlmostEqual(floor, 97.0)

    def test_v8_breakeven_lock_exit_label(self):
        import run_bot as rb

        buy = 140_100.0
        from strategy.rules import BREAKEVEN_LOCK_MULT

        lock = buy * BREAKEVEN_LOCK_MULT
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
