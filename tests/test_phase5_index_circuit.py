# -*- coding: utf-8 -*-
"""Phase5 지수 MDD 서킷."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from execution.phase5_index_circuit import (
    evaluate_index_circuit_for_market,
    evaluate_per_market_index_circuits,
)


def _fake_closes(prices: list[float]):
    idx = pd.date_range("2026-06-01", periods=len(prices), freq="B")
    return pd.Series(prices, index=idx)


def test_index_triggers_at_15pct_drawdown():
    # peak 600, current 500 → 16.67%
    closes = _fake_closes([500.0, 520.0, 600.0, 580.0, 500.0])
    with patch(
        "execution.phase5_index_circuit.fetch_benchmark_closes",
        return_value=closes,
    ):
        ev = evaluate_index_circuit_for_market("US", trigger_drawdown_pct=15.0)
    assert ev["triggered"] is True
    assert ev["benchmark"] == "SPY"
    assert ev["drawdown_pct"] > 15.0


def test_index_no_trigger_within_threshold():
    closes = _fake_closes([500.0, 520.0, 600.0, 580.0, 560.0])
    with patch(
        "execution.phase5_index_circuit.fetch_benchmark_closes",
        return_value=closes,
    ):
        ev = evaluate_index_circuit_for_market("US", trigger_drawdown_pct=15.0)
    assert ev["triggered"] is False
    assert ev["drawdown_pct"] < 15.0


def test_per_market_skips_closed_session():
    out = evaluate_per_market_index_circuits(
        market_ok={"KR": False, "US": True, "COIN": True},
        trigger_drawdown_pct=15.0,
    )
    assert out["KR"]["triggered"] is False
    assert "스킵" in out["KR"]["reason"]


def test_reconfirm_rechecks_index():
    from pathlib import Path
    from unittest.mock import patch

    from execution import phase5_ops as p5

    circuits = {
        "US": {
            "triggered": True,
            "drawdown_pct": 20.0,
            "benchmark": "SPY",
            "reason": "test",
        }
    }

    class _Rb:
        STATE_PATH = Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0

    triggered_first = {"triggered": True, "drawdown_pct": 20.0, "benchmark": "SPY"}
    triggered_second = {"triggered": False, "drawdown_pct": 10.0, "benchmark": "SPY"}

    with patch(
        "execution.phase5_index_circuit.evaluate_index_circuit_for_market",
        side_effect=[triggered_first, triggered_second],
    ):
        confirmed = p5._reconfirm_phase5_triggers_before_liquidation(
            _Rb(),
            {},
            {},
            circuits,
            ["US"],
        )
    assert confirmed == ["US"]

    with patch(
        "execution.phase5_index_circuit.evaluate_index_circuit_for_market",
        return_value=triggered_second,
    ):
        confirmed2 = p5._reconfirm_phase5_triggers_before_liquidation(
            _Rb(),
            {},
            {},
            {"US": triggered_first},
            ["US"],
        )
    assert confirmed2 == []
