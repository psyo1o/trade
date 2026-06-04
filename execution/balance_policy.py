# -*- coding: utf-8 -*-
"""KIS 잔고 API 호출 정책 — 봇 전용 매매 시 평상시는 장부+시세만 사용."""
from __future__ import annotations

from pathlib import Path

BALANCE_LIVE_SYNC_KEY = "balance_live_sync_required"
# GUI 입출금(고점 보정) 직후 1회 — 비장중 KIS 라벨 급변 방어를 건너뛰고 실조회 반영
CAPITAL_LABEL_REFRESH_KEY = "capital_label_refresh_once"


def kis_balance_sync_mode(config: dict | None) -> str:
    """``on_trade``(기본) | ``always``."""
    cfg = config if isinstance(config, dict) else {}
    return str(cfg.get("kis_balance_sync_mode", "on_trade")).strip().lower()


def is_on_trade_balance_mode(config: dict | None) -> bool:
    return kis_balance_sync_mode(config) != "always"


def wants_live_kis_balance(state: dict, config: dict | None, *, force: bool = False) -> bool:
    """
    True 이면 KIS 잔고 API(실조회)를 허용한다.

    * ``force`` — 주문 직후 ``refresh=True``, GUI 강제 새로고침, 입출금 보정
    * ``always`` 모드 — 매 사이클·표시마다 실조회(레거시)
    * ``balance_live_sync_required`` — 입출금·강제 새로고침 플래그
    """
    if force:
        return True
    if kis_balance_sync_mode(config) == "always":
        return True
    return bool(state.get(BALANCE_LIVE_SYNC_KEY))


def should_use_ledger_only(state: dict, config: dict | None, *, force: bool = False) -> bool:
    """봇 전용(on_trade)이고 실조회 트리거가 없을 때."""
    return is_on_trade_balance_mode(config) and not wants_live_kis_balance(
        state, config, force=force
    )


def mark_balance_live_sync(state: dict, path: Path) -> None:
    from execution.guard import save_state

    state[BALANCE_LIVE_SYNC_KEY] = True
    save_state(path, state)


def clear_balance_live_sync(state: dict, path: Path) -> None:
    from execution.guard import save_state

    if BALANCE_LIVE_SYNC_KEY in state:
        state.pop(BALANCE_LIVE_SYNC_KEY, None)
        save_state(path, state)


def mark_capital_label_refresh(state: dict, path: Path) -> None:
    """입출금 보정 직후 GUI KIS 라벨 1회 실반영(비장중 급변 방어 스킵)."""
    from execution.guard import save_state

    state[CAPITAL_LABEL_REFRESH_KEY] = True
    save_state(path, state)


def consume_capital_label_refresh(state: dict, path: Path) -> bool:
    """``True`` 이면 이번 스냅샷만 비장중 라벨 급변 방어를 적용하지 않는다."""
    from execution.guard import save_state

    if not state.pop(CAPITAL_LABEL_REFRESH_KEY, None):
        return False
    save_state(path, state)
    return True
