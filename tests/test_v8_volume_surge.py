# -*- coding: utf-8 -*-
"""V8 Volume Surge (0순위) 게이트 — calculate_pro_signals 전용."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy.rules import V8_VOLUME_SURGE_FAIL_MSG, calculate_pro_signals


def _ohlcv_rows(
    n: int = 130,
    *,
    prior_vol: float = 100.0,
    today_vol: float = 250.0,
    prior_nan: bool = False,
) -> list[dict]:
    """합성 상승 추세 OHLCV (o/h/l/c/v). 직전 20봉 거래량=prior_vol, 당일=today_vol."""
    rng = np.random.default_rng(42)
    closes = 100.0 + np.cumsum(rng.normal(0.15, 0.4, size=n))
    rows: list[dict] = []
    for i, c in enumerate(closes):
        o = float(c - 0.2)
        h = float(c + 0.8)
        l = float(c - 0.8)
        if i == n - 1:
            v = float(today_vol)
        elif i >= n - 21 and i < n - 1:
            v = float("nan") if prior_nan else float(prior_vol)
        else:
            v = float(prior_vol)
        rows.append({"o": o, "h": h, "l": l, "c": float(c), "v": v})
    # 당일 양봉 (그린 캔들 게이트 통과용)
    last = rows[-1]
    last["o"] = last["c"] - 1.0
    last["l"] = min(last["l"], last["o"] - 0.1)
    last["h"] = max(last["h"], last["c"] + 0.1)
    return rows


def test_volume_surge_fails_when_today_below_2x():
    """Case A: 직전20 평균 100, 당일 150 → 2배 미달 → early fail."""
    ohlcv = _ohlcv_rows(today_vol=150.0, prior_vol=100.0)
    is_buy, score, reason = calculate_pro_signals(ohlcv, {}, ticker="TEST", name="Test")
    assert is_buy is False
    assert score == 0.0
    assert reason == V8_VOLUME_SURGE_FAIL_MSG


def test_volume_surge_passes_gate_when_today_ge_2x():
    """Case B: 직전20 평균 100, 당일 250 → surge 통과 (다른 필터로 탈락 가능)."""
    ohlcv = _ohlcv_rows(today_vol=250.0, prior_vol=100.0)
    is_buy, score, reason = calculate_pro_signals(ohlcv, {}, ticker="TEST", name="Test")
    assert reason != V8_VOLUME_SURGE_FAIL_MSG


def test_volume_surge_fails_when_prior_volumes_nan():
    """Case C: 직전 20봉 거래량이 NaN이면 surge 계산 불가 → fail."""
    ohlcv = _ohlcv_rows(today_vol=500.0, prior_vol=100.0, prior_nan=True)
    is_buy, score, reason = calculate_pro_signals(ohlcv, {}, ticker="TEST", name="Test")
    assert is_buy is False
    assert reason == V8_VOLUME_SURGE_FAIL_MSG
