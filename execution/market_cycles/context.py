# -*- coding: utf-8 -*-
"""한 사이클 공통 컨텍스트 — 시장별 엔진이 공유·갱신."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradingCycleContext:
    state: dict
    weather: dict
    macro_mult: float
    macro_reason: str
    macro_snap: dict
    buy_cycle_tag: str
  # KR 전용 (타겟은 사이클 시작 시 조립)
    final_targets_kr: list = field(default_factory=list)
  # 누적 (사이클 말 텔레·로그)
    buy_fills: int = 0
    buy_zone_kr: bool = False
    buy_zone_us: bool = False
    buy_zone_coin: bool = False
