# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from strategy.exit_router import decide_swing_exit, decide_v8_exit


class TestExitRouting(unittest.TestCase):
    def test_swing_exit_uses_swing_checker(self):
        with patch(
            "strategy.exit_router.check_swing_exit",
            return_value=("HALF", "1.5R"),
        ) as m_sw:
            out = decide_swing_exit(
                {"strategy_type": "SWING_FIB"},
                [{"o": 1, "c": 2}],
                market="COIN",
                ticker="USDT-BTC",
                reference_price=100.0,
                trading_hours_held=3.5,
            )
        self.assertEqual(out, ("HALF", "1.5R"))
        m_sw.assert_called_once()

    def test_v8_exit_uses_v8_checker(self):
        with patch(
            "strategy.exit_router.check_pro_exit",
            return_value=(True, "샹들리에 이탈"),
        ) as m_v8:
            out = decide_v8_exit("AAPL", 100.0, {"strategy_type": "TREND_V8"}, [{"c": 100.0}])
        self.assertEqual(out, (True, "샹들리에 이탈"))
        m_v8.assert_called_once_with("AAPL", 100.0, {"strategy_type": "TREND_V8"}, [{"c": 100.0}])


if __name__ == "__main__":
    unittest.main()
