# -*- coding: utf-8 -*-
"""미장 매도 정산 지연 — prev_stock=0 에서도 stock_stable 인정."""
from __future__ import annotations

from run_bot import _reconcile_us_equity_after_sell


def test_settle_delay_when_prev_stock_zero_after_full_exit():
    """전량 매도 스냅샷(prev_stock=0) + 예수 API 미반영 → 직전 총평 유지."""
    cash, total = _reconcile_us_equity_after_sell(
        prev_cash=3514.85,
        prev_total=3514.85,
        prev_stock=0.0,
        cash=18.08,
        stock=0.0,
    )
    assert abs(cash - 3514.85) < 0.01
    assert abs(total - 3514.85) < 0.01


def test_settle_delay_partial_sell_still_works():
    cash, total = _reconcile_us_equity_after_sell(
        prev_cash=1000.0,
        prev_total=3000.0,
        prev_stock=2000.0,
        cash=1000.0,
        stock=500.0,
    )
    assert cash > 2000.0
    assert total > 2500.0


def test_no_false_positive_when_cash_ok():
    cash, total = _reconcile_us_equity_after_sell(
        prev_cash=3514.85,
        prev_total=3514.85,
        prev_stock=0.0,
        cash=3514.85,
        stock=0.0,
    )
    assert abs(cash - 3514.85) < 0.01
    assert abs(total - 3514.85) < 0.01
