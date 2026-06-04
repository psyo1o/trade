# -*- coding: utf-8 -*-
"""Phase5 시장별 포트폴리오 비중 서킷 단위 테스트."""
from __future__ import annotations

from execution.circuit_break import (
    evaluate_market_share_circuit,
    evaluate_per_market_share_circuits,
    portfolio_total_krw_for_share,
)


def test_portfolio_total_excludes_failed_markets():
    eq = {"KR": 10_000_000, "US": 5_000_000, "COIN": 2_000_000}
    ok = {"KR": True, "US": False, "COIN": True}
    assert portfolio_total_krw_for_share(eq, ok) == 12_000_000


def test_share_circuit_triggers_below_floor():
    ev = evaluate_market_share_circuit(
        "COIN",
        400_000,
        12_000_000,
        min_share_pct=5.0,
    )
    assert ev["share_pct"] < 5.0
    assert ev["triggered"] is True


def test_share_circuit_skips_when_market_not_ok():
    out = evaluate_per_market_share_circuits(
        kr_krw=10_000_000,
        us_usd=0,
        coin_krw=500_000,
        usdkrw=1400,
        market_ok={"KR": True, "US": False, "COIN": True},
        min_share_pct_by_market={"KR": 8, "US": 8, "COIN": 5},
    )
    assert out["US"]["triggered"] is False
    assert "스킵" in out["US"]["reason"]
    assert out["COIN"]["share_pct"] == 500_000 / (10_000_000 + 500_000) * 100


def test_anchor_raises_effective_floor():
    ev = evaluate_market_share_circuit(
        "KR",
        1_000_000,
        10_000_000,
        min_share_pct=5.0,
        anchor_share=0.30,
        anchor_min_ratio=0.5,
    )
    assert ev["effective_floor_pct"] == 15.0
    assert ev["triggered"] is True
