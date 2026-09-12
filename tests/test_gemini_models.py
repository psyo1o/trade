# -*- coding: utf-8
"""Gemini 모델 후보·429 처리."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from strategy.ai_filter import (
    GEMINI_MODEL_DEFAULT,
    _gemini_model_candidates,
    _gemini_prompt_score,
)


def test_gemini_candidates_use_latest_models():
    cands = _gemini_model_candidates({}, "")
    assert cands[0] == GEMINI_MODEL_DEFAULT
    assert "gemini-flash-lite-latest" in cands
    assert "gemini-3.5-flash-lite" in cands
    assert "gemini-1.5-flash" not in cands
    assert "gemini-2.0-flash" not in cands


def test_gemini_429_stops_without_trying_all_models():
    prompt = "test"
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.json.return_value = {"error": {"message": "spending cap"}}

    with patch("strategy.ai_filter.requests.post", return_value=resp_429) as post:
        score, msg, ok = _gemini_prompt_score(prompt, {"GOOGLE_API_KEY": "fake-key"}, "")
    assert ok is False
    assert post.call_count == 1
    assert "429" in msg or "한도" in msg


def test_gemini_default_max_two_models_on_404():
    resp_404 = MagicMock()
    resp_404.status_code = 404

    with patch("strategy.ai_filter.requests.post", return_value=resp_404) as post:
        _gemini_prompt_score("test", {"GOOGLE_API_KEY": "fake-key"}, "")
    assert post.call_count == 2
