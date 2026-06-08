# -*- coding: utf-8 -*-
"""
KIS Open API 전역 호출 간격 제어 — EGW00201(초당 거래건수 초과) 완화.

한국투자 Open API는 **계정·키 단위**로 초당 호출 상한이 있다(실전 약 20건/초, 모의 약 1건/초).
잔고·시세·일봉·주문이 각각 다른 모듈에서 나가면 슬라이스 안에서 한도를 넘기기 쉽다.

환경 변수(선택):
    ``BOT_KIS_MAX_CALLS_PER_SEC`` — 실전 기본 12 (20건 한도 대비 여유)
    ``BOT_KIS_MAX_CALLS_PER_SEC_MOCK`` — 모의 기본 0.8
    ``BOT_KIS_RATE_LIMIT_COOLDOWN_SEC`` — EGW00201 직후 권장 대기(기본 6초)
"""
from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_last_mono: float = 0.0
_window_hits: list[float] = []


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _detect_mock() -> bool:
    try:
        from api import kis_api

        for b in (kis_api.broker_kr, kis_api.broker_us):
            if b is None:
                continue
            base = str(getattr(b, "base_url", "") or "").lower()
            if "vps" in base or "vts" in base:
                return True
    except Exception:
        pass
    return False


def max_calls_per_sec() -> float:
    """초당 허용 호출 수(보수적 상한)."""
    if _detect_mock():
        return max(0.3, _env_float("BOT_KIS_MAX_CALLS_PER_SEC_MOCK", 0.8))
    return max(1.0, _env_float("BOT_KIS_MAX_CALLS_PER_SEC", 12.0))


def min_interval_sec() -> float:
    """연속 호출 최소 간격."""
    return 1.0 / max_calls_per_sec()


def rate_limit_cooldown_sec() -> float:
    """EGW00201 등 한도 응답 후 재시도 전 대기."""
    base = _env_float("BOT_KIS_RATE_LIMIT_COOLDOWN_SEC", 6.0)
    return max(base, min_interval_sec() * 3.0)


def wait_for_slot(*, label: str = "") -> None:
    """모든 KIS HTTP 호출 직전에 호출 — 슬라이딩 1초 윈도우 내 상한 유지."""
    global _last_mono, _window_hits
    mps = max_calls_per_sec()
    window = 1.0
    min_gap = min_interval_sec()
    with _lock:
        now = time.monotonic()
        _window_hits = [t for t in _window_hits if now - t < window]
        if len(_window_hits) >= int(mps):
            oldest = _window_hits[0]
            sleep_for = window - (now - oldest) + 0.02
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
                _window_hits = [t for t in _window_hits if now - t < window]
        gap = now - _last_mono
        if _last_mono > 0 and gap < min_gap:
            time.sleep(min_gap - gap)
            now = time.monotonic()
        _last_mono = now
        _window_hits.append(now)
