# -*- coding: utf-8 -*-
"""분석 전용 경로 — 운영 봇 state/lock 과 분리."""
from __future__ import annotations

from pathlib import Path

ANALYSIS_ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ANALYSIS_ROOT.parent

TRADE_HISTORY_PATH = BOT_ROOT / "trade_history.json"
BOT_STATE_PATH = BOT_ROOT / "bot_state.json"
ERAS_PATH = ANALYSIS_ROOT / "strategy_eras.json"
OUTPUT_DIR = ANALYSIS_ROOT / "output"
CACHE_DIR = ANALYSIS_ROOT / "cache"

POST_EXIT_DAYS = (5, 10, 20, 60)
