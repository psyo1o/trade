# -*- coding: utf-8 -*-
"""
``positions`` 장부 저장 — 재시도·검증·디스크와 병합.

* ``persist_position_registration`` 과 동일한 **저장 3회 + reload 검증** 패턴.
* 체결(멱등 filled) 후 ``save_state`` 만 실패하는 구멍을 줄인다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from execution.guard import load_state, save_state


def merge_disk_if_newer(state: dict, state_path: str | Path) -> bool:
    """
    디스크 ``state_gen`` 이 메모리보다 크면 GUI 등 외부 저장분을 병합.

    ``order_idempotency`` / ``buy_inflight`` / ``sell_inflight`` 는 **메모리(봇 사이클)** 유지.
    """
    path = Path(state_path)
    try:
        disk = load_state(path)
    except Exception:
        return False
    if not isinstance(disk, dict):
        return False
    try:
        dg = int(disk.get("state_gen", 0) or 0)
        mg = int(state.get("state_gen", 0) or 0)
    except (TypeError, ValueError):
        return False
    if dg <= mg:
        return False
    for k in (
        "positions",
        "cooldown",
        "ticker_cooldowns",
        "stats",
        "peak_equity_KR",
        "peak_equity_US",
        "peak_equity_COIN",
        "peak_total_equity",
    ):
        if k in disk:
            state[k] = disk[k]
    state["state_gen"] = dg
    print(f"  📎 [장부 병합] 디스크 state_gen={dg} > 메모리 {mg} — GUI·외부 저장 반영")
    return True


def save_state_verified(
    state: dict,
    state_path: str | Path,
    *,
    context: str = "",
    verify_ticker: str | None = None,
    verify_removed: bool = False,
) -> bool:
    """``save_state`` + reload. ``verify_ticker`` 있으면 positions 포함/삭제 여부 확인."""
    path = Path(state_path)
    merge_disk_if_newer(state, path)
    ctx = str(context or "장부").strip()

    for attempt in range(1, 4):
        try:
            save_state(path, state)
            latest = load_state(path)
            if not isinstance(latest, dict):
                print(f"  ⚠️ [{ctx}] 저장 후 로드 실패 (시도 {attempt}/3)")
            elif verify_ticker:
                tk = str(verify_ticker).strip()
                pos = (latest.get("positions") or {}) if isinstance(latest.get("positions"), dict) else {}
                if verify_removed:
                    if tk not in pos:
                        print(f"  ✅ [{ctx}] 삭제 확인: {tk} (시도 {attempt}/3)")
                        state["state_gen"] = latest.get("state_gen", state.get("state_gen"))
                        return True
                elif tk in pos:
                    print(f"  ✅ [{ctx}] 저장 확인: {tk} (시도 {attempt}/3)")
                    state["state_gen"] = latest.get("state_gen", state.get("state_gen"))
                    return True
                print(f"  ⚠️ [{ctx}] 저장 후 미반영: {tk} (시도 {attempt}/3)")
            else:
                state["state_gen"] = latest.get("state_gen", state.get("state_gen"))
                return True
        except Exception as e:
            print(f"  ⚠️ [{ctx}] 저장 예외 (시도 {attempt}/3): {e}")
        if attempt < 3:
            time.sleep(0.2)
    print(f"  ❌ [{ctx}] 저장 최종 실패")
    return False


def persist_position_set(
    state: dict,
    ticker: str,
    position_payload: dict,
    *,
    context: str = "",
    state_path: str | Path,
    mutate_fn: Callable[[dict], None] | None = None,
) -> bool:
    """positions[ticker] 설정 후 검증 저장. ``mutate_fn`` 으로 cooldown 등 추가 가능."""
    tk = str(ticker or "").strip()
    if not tk:
        return False
    payload = dict(position_payload)
    state.setdefault("positions", {})[tk] = payload
    if mutate_fn is not None:
        mutate_fn(state)
    return save_state_verified(
        state,
        state_path,
        context=context or "장부 등록",
        verify_ticker=tk,
        verify_removed=False,
    )


def persist_position_remove(
    state: dict,
    ticker: str,
    *,
    context: str = "",
    state_path: str | Path,
    mutate_fn: Callable[[dict], None] | None = None,
) -> bool:
    """positions 에서 티커 삭제 후 검증 저장."""
    tk = str(ticker or "").strip()
    if not tk:
        return False
    state.get("positions", {}).pop(tk, None)
    if mutate_fn is not None:
        mutate_fn(state)
    return save_state_verified(
        state,
        state_path,
        context=context or "장부 삭제",
        verify_ticker=tk,
        verify_removed=True,
    )
