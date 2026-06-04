# -*- coding: utf-8 -*-
"""execution/market_cycles — import·휴장 분기·KR parse_kr_cash_total 경로 스모크."""
from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

os.environ.setdefault("BOT_DISABLE_NET_WATCH", "1")

from execution.market_cycles import (
    TradingCycleContext,
    run_coin_cycle,
    run_kr_cycle,
    run_us_cycle,
)


def _ctx() -> TradingCycleContext:
    return TradingCycleContext(
        state={"positions": {}, "stats": {"wins": 0, "losses": 0, "total_profit": 0.0}},
        weather={"KR": "BULL", "US": "BULL", "COIN": "BEAR"},
        macro_mult=1.0,
        macro_reason="test",
        macro_snap={"market_buy_allowed": {"KR": True, "US": True, "COIN": True}},
        buy_cycle_tag="smoke",
        final_targets_kr=[],
    )


class TestMarketCyclesSmoke(unittest.TestCase):
    def test_closed_markets_no_name_error(self):
        import run_bot as rb

        ctx = _ctx()
        with patch.object(rb, "is_market_open", return_value=False):
            with patch.object(rb, "kis_equities_weekend_suppress_window_kst", return_value=False):
                run_kr_cycle(ctx)
                run_us_cycle(ctx)
                run_coin_cycle(ctx)

    def test_kr_open_parse_kr_cash_total_path(self):
        import run_bot as rb

        ctx = _ctx()
        _now = datetime.now(pytz.timezone("Asia/Seoul"))

        def _open(m):
            return m == "KR"

        with patch.object(rb, "is_market_open", side_effect=_open):
            with patch.object(rb, "kis_equities_weekend_suppress_window_kst", return_value=False):
                with patch.object(
                    rb,
                    "_prepare_kr_market_cycle_inputs",
                    return_value=(None, 1_000_000, 4_000_000, [], set()),
                ):
                    with patch.object(rb, "_prefetch_kr_sell_ohlcv_if_needed"):
                        with patch.object(
                            rb, "_is_kr_buy_window_now", return_value=(False, _now, _now)
                        ):
                            with patch.object(
                                rb,
                                "ensure_dict",
                                return_value={"output2": [{"prvs_rcdl_excc_amt": "1000000"}]},
                            ):
                                with patch.object(
                                    rb, "parse_kr_cash_total", return_value=(1_000_000, 4_000_000)
                                ):
                                    with patch.object(rb, "check_mdd_break", return_value=True):
                                        with patch.object(rb, "save_state"):
                                            run_kr_cycle(ctx)


if __name__ == "__main__":
    unittest.main()
