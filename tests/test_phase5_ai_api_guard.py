# -*- coding: utf-8 -*-
"""Gemini·Phase5 AI API 낭비 방지."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from execution import phase5_ops as p5
from execution.phase5_ai_liquidation import (
    _llm_failure_non_retryable,
    evaluate_phase5_ai_liquidation,
)
from strategy.ai_filter import _gemini_prompt_score


def test_gemini_max_model_attempts_caps_404_fanout():
    """404 연속 시에도 시도 횟수 상한."""
    calls = {"n": 0}

    def _post(*_a, **_k):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 404
        return resp

    with patch("strategy.ai_filter.requests.post", side_effect=_post):
        score, msg, ok = _gemini_prompt_score(
            "test",
            {"GOOGLE_API_KEY": "fake"},
            "",
            max_model_attempts=1,
        )
    assert ok is False
    assert calls["n"] == 1
    assert "404" in msg


def test_phase5_skips_retry_on_404():
    calls = {"n": 0}

    def _fail(*_a, **_k):
        calls["n"] += 1
        return 0, "skip:Gemini 실패 (gemini-x:404)", False, "gemini_fail"

    with patch(
        "execution.phase5_ai_liquidation.evaluate_llm_json_prompt",
        side_effect=_fail,
    ):
        out = evaluate_phase5_ai_liquidation(
            {"positions": {}},
            "US",
            {"benchmark": "SPY"},
            max_retries=3,
            retry_delay_sec=0,
        )
    assert calls["n"] == 1
    assert out["llm_success"] is False
    assert out["attempts"] == 1


def test_llm_failure_non_retryable():
    assert _llm_failure_non_retryable("skip:Gemini 실패 (x:404)")
    assert _llm_failure_non_retryable("429 spending cap")
    assert not _llm_failure_non_retryable("timeout")


def test_phase5_ai_llm_cooldown_skips_api():
    state = {
        "positions": {"AAPL": {"qty": 5, "buy_p": 1, "curr_p": 1}},
        p5.PHASE5_AI_LLM_COOLDOWN_KEY: {"US": time.time() + 3600},
    }
    circuits = {"US": {"triggered": True, "benchmark": "SPY", "reason": "test"}}

    class _Rb:
        PHASE5_AI_LIQUIDATION_ENABLED = True
        PHASE5_AI_LIQUIDATION_THRESHOLD = 70
        PHASE5_AI_LIQUIDATION_PROVIDER = "gemini"
        PHASE5_AI_LIQUIDATION_MAX_RETRIES = 1
        PHASE5_AI_LIQUIDATION_RETRY_DELAY_SEC = 0
        PHASE5_AI_LLM_COOLDOWN_SEC = 3600
        PHASE5_AI_GEMINI_MAX_MODEL_ATTEMPTS = 1
        config = {}

        @staticmethod
        def send_telegram(_m: str) -> None:
            pass

    with patch("execution.phase5_ai_liquidation.evaluate_phase5_ai_liquidation") as ev:
        confirmed = p5._ai_gate_phase5_liquidation(_Rb(), state, circuits, ["US"])
    ev.assert_not_called()
    assert confirmed == []
    assert circuits["US"]["triggered"] is False
