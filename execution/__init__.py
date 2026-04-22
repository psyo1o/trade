# -*- coding: utf-8 -*-
"""
실행(Execution) 레이어 — 주문 분할·장부·리스크 가드.

이 패키지는 **비즈니스 규칙을 직접 정하지 않고**, ``run_bot`` 등 상위 모듈이
붙여 쓰기 좋은 **저수준 블록**을 모아 둔다.

- ``execution.guard`` : ``bot_state`` 로드/저장, 쿨다운, 시장별 MDD, 합산 서킷 쿨다운.
- ``execution.sync_positions`` : 실계좌 vs ``positions`` 자동복구·유령 삭제·평단 보정.
- ``execution.order_twap`` : 대액 분할 매수/매도 슬라이스 계획·순차 실행.
- ``execution.circuit_break`` : 합산 자산 고점 대비 드로다운 판정(킬스위치 보조).
"""
from execution.guard import (
    apply_phase5_trailing_week_and_cooldown,
    can_open_new,
    check_mdd_break,
    get_peak_equity_total_krw,
    get_phase5_peak_total_equity,
    in_account_circuit_cooldown,
    in_cooldown,
    load_state,
    save_state,
    set_account_circuit_cooldown,
    set_cooldown,
    update_peak_equity_total_krw,
    week_label_seoul,
)
from execution.sync_positions import sync_all_positions
from execution.circuit_break import drawdown_from_peak_pct, evaluate_total_account_circuit, estimate_usdkrw
from execution.order_twap import (
    plan_krw_slices,
    plan_usd_slices,
    run_krw_slices,
    plan_sell_qty_twap,
    run_qty_slice_sells,
)

__all__ = [
    "load_state",
    "save_state",
    "in_cooldown",
    "set_cooldown",
    "can_open_new",
    "check_mdd_break",
    "in_account_circuit_cooldown",
    "set_account_circuit_cooldown",
    "apply_phase5_trailing_week_and_cooldown",
    "get_phase5_peak_total_equity",
    "week_label_seoul",
    "update_peak_equity_total_krw",
    "get_peak_equity_total_krw",
    "sync_all_positions",
    "drawdown_from_peak_pct",
    "evaluate_total_account_circuit",
    "estimate_usdkrw",
    "plan_krw_slices",
    "plan_usd_slices",
    "run_krw_slices",
    "plan_sell_qty_twap",
    "run_qty_slice_sells",
]
