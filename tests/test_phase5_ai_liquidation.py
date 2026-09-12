# -*- coding: utf-8 -*-
"""Phase5 AI 청산 심사."""
from __future__ import annotations

from unittest.mock import patch

from execution.phase5_ai_liquidation import (
    build_phase5_liquidation_context,
    evaluate_phase5_ai_liquidation,
)


def test_context_includes_holdings_and_index():
    state = {
        "positions": {
            "AAPL": {"qty": 10, "buy_p": 100.0, "curr_p": 90.0},
        },
        "last_kis_display_snapshot": {"us": {"cash": 500.0, "total": 1400.0}},
    }
    ev = {
        "reason": "US SPY -16%",
        "benchmark": "SPY",
        "benchmark_range": "2026-03-01~2026-08-28",
        "peak": 600.0,
        "current": 500.0,
        "drawdown_pct": 16.7,
        "trigger_drawdown_pct": 15.0,
    }
    ctx = build_phase5_liquidation_context(state, "US", ev)
    assert "AAPL" in ctx
    assert "SPY" in ctx
    assert "account_drawdown_pct=16.7" in ctx
    assert "circuit_basis=account_equity_peak_mdd" in ctx
    assert "circuit_uses=account_equity_peak_mdd_not_index" in ctx
    assert "data_error_suspect=" in ctx
    assert "risk_vs_snap_divergence_pct=" in ctx


def test_ai_proceeds_when_score_above_threshold():
    state = {"positions": {}}
    ev = {"benchmark": "SPY", "peak": 600, "current": 500, "drawdown_pct": 17.0}

    def _fake_llm(*_a, **_k):
        return 85, "지수 폭락 지속, 청산 합리", True, "openai"

    with patch("execution.phase5_ai_liquidation.evaluate_llm_json_prompt", side_effect=_fake_llm):
        out = evaluate_phase5_ai_liquidation(
            state, "US", ev, threshold=70, max_retries=2, retry_delay_sec=0
        )
    assert out["llm_success"] is True
    assert out["proceed"] is True
    assert out["liquidation_score"] == 85


def test_ai_blocks_when_score_below_threshold():
    state = {"positions": {}}
    ev = {"benchmark": "SPY", "peak": 600, "current": 500, "drawdown_pct": 17.0}

    with patch(
        "execution.phase5_ai_liquidation.evaluate_llm_json_prompt",
        return_value=(30, "데이터 오류 의심", True, "gemini"),
    ):
        out = evaluate_phase5_ai_liquidation(state, "US", ev, threshold=70)
    assert out["proceed"] is False


def test_ai_retries_until_success():
    state = {"positions": {}}
    ev = {"benchmark": "SPY"}
    calls = {"n": 0}

    def _flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            return 0, "fail", False, "gemini_fail"
        return 75, "ok", True, "openai"

    with patch("execution.phase5_ai_liquidation.evaluate_llm_json_prompt", side_effect=_flaky):
        out = evaluate_phase5_ai_liquidation(
            state, "US", ev, threshold=70, max_retries=3, retry_delay_sec=0
        )
    assert calls["n"] == 3
    assert out["llm_success"] is True
    assert out["proceed"] is True


def test_ai_uses_same_llm_path_as_buy_filter():
    """Phase5·매수 AI 모두 evaluate_llm_json_prompt 경유."""
    from strategy.ai_filter import evaluate_llm_json_prompt

    state = {"positions": {"AAPL": {"qty": 1, "buy_p": 1, "curr_p": 1}}}
    ev = {"benchmark": "SPY", "peak": 600, "current": 500, "drawdown_pct": 17.0}

    with patch(
        "execution.phase5_ai_liquidation.evaluate_llm_json_prompt",
        return_value=(75, "ok", True, "openai"),
    ) as mocked:
        out = evaluate_phase5_ai_liquidation(state, "US", ev, threshold=70, max_retries=1)
    mocked.assert_called_once()
    assert out["llm_success"] is True
    assert out["evaluation_engine"] == "openai"


def test_ai_openai_fallback_when_gemini_fails():
    state = {"positions": {"AAPL": {"qty": 1, "buy_p": 1, "curr_p": 1}}}
    ev = {"benchmark": "SPY"}

    with patch(
        "execution.phase5_ai_liquidation.evaluate_llm_json_prompt",
        return_value=(80, "[Gemini 실패→OpenAI 폴백] ok", True, "openai"),
    ):
        out = evaluate_phase5_ai_liquidation(
            state, "US", ev, threshold=70, max_retries=1, retry_delay_sec=0
        )
    assert out["llm_success"] is True
    assert out["evaluation_engine"] == "openai"
    assert out["proceed"] is True


def test_ai_gate_integration():
    from execution import phase5_ops as p5

    circuits = {
        "US": {
            "triggered": True,
            "drawdown_pct": 18.0,
            "benchmark": "SPY",
            "reason": "test",
        }
    }

    class _Rb:
        PHASE5_AI_LIQUIDATION_ENABLED = True
        PHASE5_AI_LIQUIDATION_THRESHOLD = 70
        PHASE5_AI_LIQUIDATION_PROVIDER = "gemini"
        PHASE5_AI_LIQUIDATION_MAX_RETRIES = 1
        PHASE5_AI_LIQUIDATION_RETRY_DELAY_SEC = 0
        config = {}

        @staticmethod
        def send_telegram(_m: str) -> None:
            pass

    ok_ai = {
        "proceed": True,
        "liquidation_score": 90,
        "threshold": 70,
        "llm_success": True,
        "rationale_short": "청산",
    }
    state = {"positions": {"AAPL": {"qty": 5, "buy_p": 100, "curr_p": 90}}}
    with patch(
        "execution.phase5_ai_liquidation.evaluate_phase5_ai_liquidation",
        return_value=ok_ai,
    ):
        confirmed = p5._ai_gate_phase5_liquidation(_Rb(), state, circuits, ["US"])
    assert confirmed == ["US"]
