# -*- coding: utf-8 -*-
"""
대액 시장가 주문 **분할 계획**과 **순차 실행** 헬퍼 (Phase2 TWAP).

설정은 ``run_bot`` 이 ``config.json`` 에서 읽은 뒤 ``threshold``·``delay`` 만 넘기고,
이 모듈은 **순수하게 금액/수량 쪼개기 + sleep + 콜백** 만 담당한다.

상수
    * ``DEFAULT_THRESHOLD_KRW`` / ``DEFAULT_THRESHOLD_USD`` — 이 금액을 넘기면 분할 후보.
    * ``DEFAULT_MAX_SLICES`` — 최대 조각 수(기본 5). ``plan_*`` 의 ``ceil(총액/threshold)`` 와 함께 쓰인다.

실제 브로커 주문은 ``run_krw_slices`` / ``run_qty_slice_sells`` 에 넘기는 ``Callable`` 이 수행한다.
"""
from __future__ import annotations

import math
import time
from typing import Callable, List, Sequence

DEFAULT_THRESHOLD_KRW = 5_000_000.0
DEFAULT_THRESHOLD_USD = 5_000.0
DEFAULT_MAX_SLICES = 5


def plan_krw_slices(
    total_krw: float,
    *,
    threshold_krw: float = DEFAULT_THRESHOLD_KRW,
    max_slices: int = DEFAULT_MAX_SLICES,
) -> List[float]:
    """
    threshold 미만이면 [total] 한 방.
    이상이면 max_slices 이하로 균등 분할(원 단위, 마지막 조각으로 잔차 흡수).
    """
    total = float(total_krw)
    if total <= 0:
        return []
    if total <= float(threshold_krw):
        return [total]

    n = min(int(max_slices), max(2, math.ceil(total / float(threshold_krw))))
    part = total / n
    out = [part] * n
    out[-1] = total - part * (n - 1)
    return [round(x, 2) for x in out]


def plan_usd_slices(
    total_usd: float,
    *,
    threshold_usd: float = DEFAULT_THRESHOLD_USD,
    max_slices: int = DEFAULT_MAX_SLICES,
) -> List[float]:
    """미장 등 달러 예산: threshold_usd(기본 5,000$) 미만이면 한 방, 이상이면 분할."""
    total = float(total_usd)
    if total <= 0:
        return []
    if total <= float(threshold_usd):
        return [round(total, 2)]

    n = min(int(max_slices), max(2, math.ceil(total / float(threshold_usd))))
    part = total / n
    out = [part] * n
    out[-1] = total - part * (n - 1)
    return [round(x, 2) for x in out]


def run_krw_slices(
    slices: Sequence[float],
    execute_one: Callable[[float], bool],
    delay_sec: float = 2.0,
) -> bool:
    """슬라이스 순서대로 실행. 한 번 False면 즉시 중단."""
    for i, amt in enumerate(slices):
        if amt <= 0:
            continue
        ok = execute_one(float(amt))
        if not ok:
            return False
        if delay_sec > 0 and i < len(slices) - 1:
            time.sleep(delay_sec)
    return True


def plan_sell_qty_twap(
    total_qty: int,
    notional_krw: float,
    *,
    threshold_krw: float = DEFAULT_THRESHOLD_KRW,
    max_parts: int = DEFAULT_MAX_SLICES,
) -> List[int]:
    """
    평가금(원)이 threshold 이상이면 주문 수량을 여러 덩어리로 나눈다(정수 주·코인 수량).
    미만이면 [total_qty] 한 번.
    """
    q = int(total_qty)
    if q <= 0:
        return []
    notion = float(notional_krw)
    if notion < float(threshold_krw):
        return [q]
    n = min(int(max_parts), max(2, int(math.ceil(notion / float(threshold_krw)))))
    base = q // n
    if base <= 0:
        return [q]
    last = q - base * (n - 1)
    return [base] * (n - 1) + [last]


def run_qty_slice_sells(
    chunks: Sequence[int],
    execute_qty: Callable[[int], bool],
    delay_sec: float = 90.0,
) -> bool:
    """수량 청산 TWAP: 덩어리마다 시장가 콜백, 기본 90초 간격."""
    chunks_list = [int(c) for c in chunks if int(c) > 0]
    for i, q in enumerate(chunks_list):
        if not execute_qty(q):
            return False
        if delay_sec > 0 and i < len(chunks_list) - 1:
            time.sleep(delay_sec)
    return True
