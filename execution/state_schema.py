# -*- coding: utf-8 -*-
"""
``bot_state.json`` 스키마 검증 — **읽기 전용**. positions 를 지우거나 덮어쓰지 않는다.

C-1: load/save 전후 관측·경고. 저장 시 ``.bak`` sidecar 는 ``guard.save_state`` 가 담당.
"""
from __future__ import annotations

from typing import Any

TOP_LEVEL_DICT_KEYS = frozenset(
    {
        "positions",
        "cooldown",
        "ticker_cooldowns",
        "order_idempotency",
        "buy_inflight",
        "sell_inflight",
        "last_kis_display_snapshot",
        "last_coin_display_snapshot",
    }
)

POSITION_RECOMMENDED = frozenset(
    {"buy_p", "sl_p", "max_p", "qty", "buy_time", "strategy_type", "scale_out_done"}
)

# path_label 별 마지막 로드 요약 — 동일하면 재출력 안 함 (GUI 다중 load_state 스팸 방지)
_last_load_log_sig: dict[str, tuple[int, int, tuple[str, ...]]] = {}


def count_positions(state: dict[str, Any] | None) -> int:
    if not isinstance(state, dict):
        return 0
    pos = state.get("positions")
    if not isinstance(pos, dict):
        return 0
    return len(pos)


def validate_state(state: dict[str, Any] | None) -> list[str]:
    """구조 이상만 경고 목록으로 반환. **state 를 수정하지 않음.**"""
    warnings: list[str] = []
    if not isinstance(state, dict):
        return ["state 가 dict 가 아님"]

    pos = state.get("positions")
    if pos is None:
        warnings.append("positions 키 없음 (load_state 보강 예정)")
    elif not isinstance(pos, dict):
        warnings.append(f"positions 가 dict 가 아님: {type(pos).__name__}")

    for key in TOP_LEVEL_DICT_KEYS:
        val = state.get(key)
        if val is not None and not isinstance(val, dict):
            warnings.append(f"{key} 가 dict 가 아님: {type(val).__name__}")

    if isinstance(pos, dict):
        for ticker, entry in pos.items():
            if not isinstance(entry, dict):
                warnings.append(f"positions[{ticker!r}] 가 dict 가 아님")
                continue
            missing = POSITION_RECOMMENDED - set(entry.keys())
            if missing and entry.get("buy_p") is not None:
                warnings.append(
                    f"positions[{ticker!r}] 권장 필드 누락: {', '.join(sorted(missing))}"
                )
            try:
                bp = float(entry.get("buy_p", 0) or 0)
                q = float(entry.get("qty", 0) or 0)
                if bp <= 0 and q > 0:
                    warnings.append(f"positions[{ticker!r}] buy_p<=0 인데 qty>0")
            except (TypeError, ValueError):
                warnings.append(f"positions[{ticker!r}] buy_p/qty 숫자 변환 실패")

    return warnings


def log_load_summary(state: dict[str, Any], *, path_label: str = "bot_state") -> None:
    """로드 직후 한 줄 요약 (장부 유실 조기 발견).

    GUI·워커가 ``load_state`` 를 자주 호출하므로 **positions·state_gen·경고가
    이전과 같으면 로그 생략** (동일 스팸 방지). 값이 바뀔 때만 다시 출력.
    """
    n = count_positions(state)
    try:
        gen = int(state.get("state_gen", 0) or 0)
    except (TypeError, ValueError):
        gen = 0
    warnings = tuple(validate_state(state)[:3])
    key = str(path_label or "bot_state")
    sig = (n, gen, warnings)
    prev = _last_load_log_sig.get(key)
    if prev == sig:
        return
    _last_load_log_sig[key] = sig

    if n > 0:
        print(f"  📒 [state_schema] {path_label} 로드 — positions {n}종 · state_gen={gen}")
    else:
        print(
            f"  📒 [state_schema] {path_label} 로드 — positions 0종 · state_gen={gen} "
            f"(신규 장부이거나 백업 복구 필요할 수 있음)"
        )
    for w in warnings:
        print(f"  ⚠️ [state_schema] {w}")


def warn_positions_drop(*, prev_count: int, new_count: int, context: str = "") -> None:
    """저장 직전 positions 급감 경고 (전량 청산·실수 구분용)."""
    if prev_count >= 1 and new_count == 0:
        tag = f" ({context})" if context else ""
        print(
            f"  🚨 [state_schema] positions {prev_count}→0 저장{tag} — "
            "Phase5 전량청산·수동 정리가 아니면 bot_state.bak 확인"
        )
    elif prev_count >= 3 and new_count < prev_count // 2:
        tag = f" ({context})" if context else ""
        print(
            f"  ⚠️ [state_schema] positions {prev_count}→{new_count} 급감{tag}"
        )
