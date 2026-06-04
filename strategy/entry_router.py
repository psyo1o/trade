# -*- coding: utf-8 -*-
"""진입 라우터 (B-2) — 기존 rules 호출을 한 곳으로 모음."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from strategy.rules import calculate_pro_signals, check_swing_entry


@dataclass
class EntryDecision:
    is_buy: bool
    sl_p: float
    signal_name: str
    swing_ok: bool
    swing_fib: float
    swing_why: str


def decide_entry_signals(
    ohlcv: list[dict[str, Any]],
    weather_label: str,
    ticker: str,
    display_name: str,
    idx: int,
    total_count: int,
    *,
    market: str,
    reference_close: float | None = None,
) -> EntryDecision:
    """
    V8 진입 신호 + 스윙 보조 진입 신호를 함께 계산한다.

    주의: 기존 동작 동일성을 위해 “날씨(BEAR) 기반 V8 차단”은 호출부에서 그대로 처리한다.
    """
    is_buy, sl_p, signal_name = calculate_pro_signals(
        ohlcv, weather_label, ticker, display_name, idx, total_count
    )
    sw_ok, sw_fib, sw_why = check_swing_entry(
        pd.DataFrame(ohlcv),
        market=market,
        reference_close=reference_close,
    )
    return EntryDecision(
        is_buy=bool(is_buy),
        sl_p=float(sl_p or 0.0),
        signal_name=str(signal_name or ""),
        swing_ok=bool(sw_ok),
        swing_fib=float(sw_fib or 0.0),
        swing_why=str(sw_why or ""),
    )
