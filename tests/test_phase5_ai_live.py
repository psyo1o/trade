# -*- coding: utf-8
"""Phase5 AI 라이브 스모크 — ``pytest tests/test_phase5_ai_live.py -q`` (네트워크·API 키 필요)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
def test_phase5_ai_live_matches_buy_ai_path():
    cfg_path = ROOT / "config.json"
    config = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    provider = str(
        config.get("phase5_ai_liquidation_provider")
        or config.get("ai_false_breakout_provider", "gemini")
    ).strip().lower()

    from strategy.ai_filter import evaluate_false_breakout_filter, evaluate_llm_json_prompt
    from execution.phase5_ai_liquidation import (
        _build_liquidation_prompt,
        build_phase5_liquidation_context,
        evaluate_phase5_ai_liquidation,
    )

    buy = evaluate_false_breakout_filter(
        "AAPL",
        "US",
        threshold=70,
        use_ai=True,
        ai_provider=provider,
        config=config,
        strategy_type="TREND_V8",
    )
    assert buy.get("llm_success") is True, buy.get("rationale")

    state = {
        "positions": {"AAPL": {"qty": 5, "buy_p": 180.0, "curr_p": 170.0}},
        "last_kis_display_snapshot": {"us": {"cash": 2000.0, "total": 2850.0}},
    }
    ev = {
        "reason": "US SPY pytest live",
        "benchmark": "SPY",
        "benchmark_range": "live-test",
        "peak": 600.0,
        "current": 500.0,
        "drawdown_pct": 16.7,
        "trigger_drawdown_pct": 15.0,
    }
    p5 = evaluate_phase5_ai_liquidation(
        state,
        "US",
        ev,
        threshold=70,
        provider=provider,
        config=config,
        max_retries=1,
        retry_delay_sec=0,
    )
    assert p5.get("llm_success") is True, p5.get("rationale")
    assert 0 <= int(p5.get("liquidation_score", -1)) <= 100

    ctx = build_phase5_liquidation_context(state, "US", ev)
    prompt = _build_liquidation_prompt(ctx)
    score, rationale, ok, engine = evaluate_llm_json_prompt(prompt, config, provider=provider)
    assert ok is True, rationale
    assert engine in ("gemini", "openai")
