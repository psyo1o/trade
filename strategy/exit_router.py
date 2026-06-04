# -*- coding: utf-8 -*-
"""청산 라우터 (B-2) — 기존 rules 호출을 한 곳으로 모음."""
from __future__ import annotations

from typing import Any

import pandas as pd

from strategy.rules import check_pro_exit, check_swing_exit


def decide_swing_exit(
    pos_info: dict[str, Any],
    ohlcv: list[dict[str, Any]],
    *,
    market: str,
    ticker: str,
    reference_price: float,
    trading_hours_held: float,
) -> tuple[str, str]:
    """스윙 전용 HALF/FULL/HOLD 판정."""
    return check_swing_exit(
        pos_info,
        pd.DataFrame(ohlcv),
        reference_price=float(reference_price),
        market=market,
        ticker=ticker,
        trading_hours_held=trading_hours_held,
    )


def decide_v8_exit(
    ticker: str,
    current_price: float,
    pos_info: dict[str, Any],
    ohlcv: list[dict[str, Any]],
) -> tuple[bool, str]:
    """V8 샹들리에 청산 판정."""
    return check_pro_exit(ticker, current_price, pos_info, ohlcv)
