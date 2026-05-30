# -*- coding: utf-8 -*-
"""V8·스윙 본절(Breakeven) 수익 락 — 임계치 활성화."""
from __future__ import annotations

import unittest

from strategy.rules import (
    BREAKEVEN_LOCK_MULT,
    V8_PROFIT_LOCK_ACTIVATE_PCT,
    get_final_exit_price,
    get_v8_profit_lock_floor,
)


def _ohlcv_rows(close: float, n: int = 60):
    return [{"o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1e6}] * n


class TestV8BreakevenLock(unittest.TestCase):
    def test_v8_lock_inactive_below_4pct(self):
        buy = 100.0
        self.assertEqual(get_v8_profit_lock_floor(buy, 103.9), 0.0)
        self.assertEqual(get_v8_profit_lock_floor(buy, 104.0), buy * BREAKEVEN_LOCK_MULT)

    def test_final_exit_includes_breakeven_when_activated(self):
        buy = 10_000.0
        max_p = 10_500.0
        pos = {
            "buy_p": buy,
            "max_p": max_p,
            "sl_p": buy * 0.5,
            "current_atr": buy * 0.02,
        }
        line = get_final_exit_price("005930", buy, pos, _ohlcv_rows(buy))
        self.assertGreaterEqual(line, buy * BREAKEVEN_LOCK_MULT)
        self.assertGreater(max_p, buy * (1 + V8_PROFIT_LOCK_ACTIVATE_PCT / 100))


if __name__ == "__main__":
    unittest.main()
