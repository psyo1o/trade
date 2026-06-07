# -*- coding: utf-8 -*-
"""시장별 사이클 스모크 — NameError·import·휴장 분기."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

os.environ.setdefault("BOT_DISABLE_NET_WATCH", "1")

ROOT = __file__
for _ in range(3):
    ROOT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from execution.market_cycles import (  # noqa: E402
    TradingCycleContext,
    run_coin_cycle,
    run_kr_cycle,
    run_us_cycle,
)


def _minimal_ctx() -> TradingCycleContext:
    return TradingCycleContext(
        state={
            "positions": {},
            "stats": {"wins": 0, "losses": 0, "total_profit": 0.0},
        },
        weather={"KR": "☀️ BULL", "US": "☀️ BULL", "COIN": "🌧️ BEAR"},
        macro_mult=1.0,
        macro_reason="smoke",
        macro_snap={
            "market_buy_allowed": {"KR": True, "US": True, "COIN": True},
            "market_buy_block_reason": {},
        },
        buy_cycle_tag="smoke_cycle",
        final_targets_kr=[],
    )


def smoke_closed_markets() -> None:
    import run_bot as rb

    ctx = _minimal_ctx()
    with patch.object(rb, "is_market_open", return_value=False):
        with patch.object(rb, "kis_equities_weekend_suppress_window_kst", return_value=False):
            run_kr_cycle(ctx)
            run_us_cycle(ctx)
            run_coin_cycle(ctx)
    print("OK closed-markets: kr/us/coin cycles (휴장 분기)")


def smoke_kr_open_mocked() -> None:
    """국장 장중 분기 — 매도 루프 0건 + parse_kr_cash_total 경로."""
    import run_bot as rb

    ctx = _minimal_ctx()

    def _closed(m):
        return m == "KR"

    with patch.object(rb, "is_market_open", side_effect=_closed):
        with patch.object(rb, "kis_equities_weekend_suppress_window_kst", return_value=False):
            with patch.object(
                rb,
                "_prepare_kr_market_cycle_inputs",
                return_value=(None, 1_000_000, 4_000_000, [], set()),
            ):
                with patch.object(rb, "_prefetch_kr_sell_ohlcv_if_needed"):
                    _now = datetime.now(pytz.timezone("Asia/Seoul"))
                    with patch.object(
                        rb, "_is_kr_buy_window_now", return_value=(False, _now, _now)
                    ):
                        with patch.object(
                            rb,
                            "ensure_dict",
                            return_value={"output2": [{"dnca_tot_amt": "1000000"}]},
                        ):
                            with patch.object(
                                rb, "parse_kr_cash_total", return_value=(1_000_000, 4_000_000)
                            ):
                                with patch.object(rb, "check_mdd_break", return_value=True):
                                    run_kr_cycle(ctx)
    print("OK kr-open-mocked: sell loop + parse_kr_cash_total path")


def smoke_wire_run_trading_bot() -> None:
    import run_bot as rb

    with patch.object(rb, "_prepare_cycle_state", return_value={"positions": {}}):
        with patch.object(rb, "_sync_positions_for_cycle"):
            with patch.object(
                rb,
                "_build_market_context",
                return_value=(
                    {"KR": "BULL", "US": "BULL", "COIN": "BEAR"},
                    1.0,
                    "smoke",
                    {"market_buy_allowed": {"KR": True, "US": True, "COIN": True}},
                ),
            ):
                with patch.object(rb, "load_state", return_value={"positions": {}}):
                    with patch.object(rb, "order_idem") as oid:
                        oid.cycle_tag_15m_kst.return_value = "smoke"
                        oid.reconcile_positions_for_cycle.return_value = 0
                        with patch.object(rb, "get_kis_market_cap_rank", return_value=[]):
                            with patch.object(rb, "time") as tmod:
                                tmod.sleep = MagicMock()
                                with patch.object(rb, "get_kis_top_trade_value", return_value=[]):
                                    with patch.object(rb, "_build_kr_targets", return_value=[]):
                                        with patch.object(rb, "_sort_buy_targets_by_rs", return_value=[]):
                                            with patch.object(rb, "is_market_open", return_value=False):
                                                with patch.object(
                                                    rb,
                                                    "kis_equities_weekend_suppress_window_kst",
                                                    return_value=False,
                                                ):
                                                    with patch.object(rb, "save_state"):
                                                        rb.run_trading_bot()
    print("OK run_trading_bot wire (mocked prep + closed markets)")


def main() -> int:
    try:
        smoke_closed_markets()
        smoke_kr_open_mocked()
        smoke_wire_run_trading_bot()
        print("\n=== market_cycles smoke: ALL PASSED ===")
        return 0
    except Exception as e:
        print(f"\nFAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
