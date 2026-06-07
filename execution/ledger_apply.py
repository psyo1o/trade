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


def _trade_count(stats: dict | None) -> int:
    if not isinstance(stats, dict):
        return 0
    return int(stats.get("wins", 0) or 0) + int(stats.get("losses", 0) or 0)


def merge_stats_monotonic(state: dict, disk: dict) -> None:
    """``stats`` 는 승/패·누적 수익률이 **역행하지 않도록** 병합한다.

    GUI ``max_p`` 저장 등으로 ``state_gen`` 만 올라간 디스크가 봇의 청산 stats 를 덮어쓰지 않게 한다.
    """
    mem = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    dsk = disk.get("stats") if isinstance(disk.get("stats"), dict) else {}
    mc, dc = _trade_count(mem), _trade_count(dsk)
    if dc > mc:
        merged = dict(dsk)
    elif mc > dc:
        merged = dict(mem)
    else:
        merged = dict(dsk)
        for key in ("wins", "losses", "total_profit"):
            if key in mem:
                merged[key] = mem[key]
        mp = float(mem.get("manual_partial_total_profit_pct", 0.0) or 0.0)
        dp = float(dsk.get("manual_partial_total_profit_pct", 0.0) or 0.0)
        merged["manual_partial_total_profit_pct"] = max(mp, dp)
    state["stats"] = merged


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
        "peak_equity_KR",
        "peak_equity_US",
        "peak_equity_COIN",
        "peak_total_equity",
    ):
        if k in disk:
            state[k] = disk[k]
    merge_stats_monotonic(state, disk)
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
    mutate_fn: Callable[[dict], None] | None = None,
) -> bool:
    """``save_state`` + reload. ``verify_ticker`` 있으면 positions 포함/삭제 여부 확인."""
    path = Path(state_path)
    ctx = str(context or "장부").strip()
    tk = str(verify_ticker or "").strip()
    pending_pos: dict | None = None
    if tk and not verify_removed:
        positions = state.get("positions")
        if isinstance(positions, dict) and tk in positions and isinstance(positions[tk], dict):
            pending_pos = dict(positions[tk])

    for attempt in range(1, 4):
        merge_disk_if_newer(state, path)
        if tk:
            if verify_removed:
                state.setdefault("positions", {}).pop(tk, None)
            elif pending_pos is not None:
                state.setdefault("positions", {})[tk] = pending_pos
        if mutate_fn is not None:
            mutate_fn(state)
        try:
            save_state(path, state)
            latest = load_state(path)
            if not isinstance(latest, dict):
                print(f"  ⚠️ [{ctx}] 저장 후 로드 실패 (시도 {attempt}/3)")
            elif tk:
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
    return save_state_verified(
        state,
        state_path,
        context=context or "장부 등록",
        verify_ticker=tk,
        verify_removed=False,
        mutate_fn=mutate_fn,
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
    return save_state_verified(
        state,
        state_path,
        context=context or "장부 삭제",
        verify_ticker=tk,
        verify_removed=True,
        mutate_fn=mutate_fn,
    )
