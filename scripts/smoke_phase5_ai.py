# -*- coding: utf-8 -*-
"""Phase5 AI vs 매수 AI 라이브 스모크 — ``python scripts/smoke_phase5_ai.py``"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

config: dict = {}
cfg_path = ROOT / "config.json"
if cfg_path.is_file():
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    print("config.json OK")
else:
    print("config.json 없음 — ai_keys.txt / env 만 사용")

from strategy.ai_filter import _get_secret, evaluate_false_breakout_filter
from execution.phase5_ai_liquidation import evaluate_phase5_ai_liquidation

has_google = bool(_get_secret("GOOGLE_API_KEY", config))
has_openai = bool(_get_secret("OPENAI_API_KEY", config))
print(f"keys: GOOGLE={has_google} OPENAI={has_openai}")

provider = str(
    config.get("phase5_ai_liquidation_provider")
    or config.get("ai_false_breakout_provider", "gemini")
).strip().lower()

print("\n=== 1) 매수 AI (AAPL) ===")
buy = evaluate_false_breakout_filter(
    "AAPL",
    "US",
    threshold=70,
    use_ai=True,
    ai_provider=provider,
    config=config,
    strategy_type="TREND_V8",
)
print("llm_success:", buy.get("llm_success"))
print("engine:", buy.get("evaluation_engine"))
print("prob:", buy.get("false_breakout_prob"))
print("rationale:", str(buy.get("rationale", ""))[:200])

print("\n=== 2) Phase5 AI (US) ===")
state = {
    "positions": {"AAPL": {"qty": 5, "buy_p": 180.0, "curr_p": 170.0}},
    "last_kis_display_snapshot": {"us": {"cash": 2000.0, "total": 2850.0}},
}
ev = {
    "reason": "US SPY smoke test",
    "benchmark": "SPY",
    "benchmark_range": "2026-03-01~2026-08-31",
    "peak": 600.0,
    "current": 500.0,
    "drawdown_pct": 16.7,
    "trigger_drawdown_pct": 15.0,
}
p5 = evaluate_phase5_ai_liquidation(
    state,
    "US",
    ev,
    threshold=int(config.get("phase5_ai_liquidation_threshold", 70)),
    provider=provider,
    config=config,
    max_retries=1,
    retry_delay_sec=0,
)
print("llm_success:", p5.get("llm_success"))
print("engine:", p5.get("evaluation_engine"))
print("score:", p5.get("liquidation_score"))
print("proceed:", p5.get("proceed"))
print("rationale:", str(p5.get("rationale_short") or p5.get("rationale") or "")[:200])

if not buy.get("llm_success") and not p5.get("llm_success"):
    sys.exit(2)
if buy.get("llm_success") and not p5.get("llm_success"):
    print("\nFAIL: 매수 AI OK but Phase5 AI failed — 코드 경로 불일치")
    sys.exit(1)
print("\nOK: Phase5 AI 응답 정상")
sys.exit(0)
