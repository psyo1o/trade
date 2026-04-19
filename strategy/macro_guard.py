# -*- coding: utf-8 -*-
"""
Phase 4 — 거시 방어막 (VIX, Crypto Fear & Greed).

원시 수치는 ``api.macro_data`` 가 가져오고, 이 모듈은 **정책(차단/축소/정상)** 만 판정한다.
``run_bot`` 은 매 사이클 ``get_macro_guard_snapshot(config)`` 로 배수·모드를 받아 매수 예산에 곱한다.

규칙(기본)
    * VIX >= ``macro_vix_block_threshold`` → 신규 매수 차단 (예산 배수 0).
    * Fear & Greed >= ``macro_fgi_reduce_threshold`` → 예산 축소 (기본 ×0.5).
    * 그 외 정상 (×1.0).

``config.json`` 키 이름은 ``run_bot`` 상단 주석 블록(Phase4) 참고.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from api.macro_data import fetch_crypto_fear_greed_index, fetch_vix_close


def evaluate_macro_guard(
    vix_value: float,
    fear_greed_index: int,
    *,
    vix_block: float = 25.0,
    fgi_reduce: int = 80,
    reduce_mult: float = 0.5,
) -> Dict[str, Any]:
    vix = float(vix_value)
    fgi = int(max(0, min(100, int(fear_greed_index))))

    if vix >= float(vix_block):
        return {
            "mode": "block",
            "budget_multiplier": 0.0,
            "reason": f"VIX {vix:.2f} >= {vix_block:g} (고변동성)",
        }
    if fgi >= int(fgi_reduce):
        rm = float(reduce_mult)
        if rm < 0.0:
            rm = 0.0
        if rm > 1.0:
            rm = 1.0
        return {
            "mode": "reduce",
            "budget_multiplier": rm,
            "reason": f"Fear&Greed {fgi} >= {fgi_reduce} (극단 탐욕 → 예산 x{rm})",
        }
    return {
        "mode": "normal",
        "budget_multiplier": 1.0,
        "reason": f"VIX {vix:.2f}, Fear&Greed {fgi} (정상 범위)",
    }


def fetch_vix_yfinance() -> float:
    """하위 호환: `api.macro_data.fetch_vix_close` 위임."""
    return fetch_vix_close()


def fetch_fear_greed_index(timeout: float = 10.0) -> int:
    """하위 호환: `api.macro_data.fetch_crypto_fear_greed_index` 위임."""
    return fetch_crypto_fear_greed_index(timeout=timeout)


def _coerce_float(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _coerce_int(val: Any) -> Optional[int]:
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def get_macro_guard_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    운영/랩 공통: config 기반으로 VIX·FGI 조회 후 판단.

    config 키 (선택):
      macro_guard_enabled (기본 True)
      macro_vix_block_threshold (기본 25)
      macro_fgi_reduce_threshold (기본 80)
      macro_fgi_budget_multiplier (기본 0.5)
      macro_vix_fallback (기본 20)
      macro_fgi_fallback (기본 50)
      macro_vix_override / macro_fgi_override — 숫자면 조회 생략
    """
    enabled = bool(config.get("macro_guard_enabled", True))
    if not enabled:
        return {
            "enabled": False,
            "mode": "off",
            "budget_multiplier": 1.0,
            "vix": None,
            "fgi": None,
            "vix_source": "",
            "fgi_source": "",
            "reason": "macro_guard_enabled=false",
        }

    vix_block = float(config.get("macro_vix_block_threshold", 25.0))
    fgi_reduce = int(config.get("macro_fgi_reduce_threshold", 80))
    reduce_mult = float(config.get("macro_fgi_budget_multiplier", 0.5))
    vix_fb = float(config.get("macro_vix_fallback", 20.0))
    fgi_fb = int(config.get("macro_fgi_fallback", 50))

    vix_ov = _coerce_float(config.get("macro_vix_override"))
    fgi_ov = _coerce_int(config.get("macro_fgi_override"))

    if vix_ov is not None:
        vix_val = float(vix_ov)
        vix_src = "config_override"
    else:
        live = fetch_vix_close()
        if live > 0:
            vix_val = live
            vix_src = "yfinance(^VIX)"
        else:
            vix_val = float(vix_fb)
            vix_src = f"fallback({vix_fb})"

    if fgi_ov is not None:
        fgi_val = int(max(0, min(100, fgi_ov)))
        fgi_src = "config_override"
    else:
        fg = fetch_crypto_fear_greed_index()
        if fg >= 0:
            fgi_val = int(max(0, min(100, fg)))
            fgi_src = "alternative.me"
        else:
            fgi_val = int(max(0, min(100, fgi_fb)))
            fgi_src = f"fallback({fgi_fb})"

    decision = evaluate_macro_guard(
        vix_val,
        fgi_val,
        vix_block=vix_block,
        fgi_reduce=fgi_reduce,
        reduce_mult=reduce_mult,
    )
    return {
        "enabled": True,
        "vix": float(vix_val),
        "fgi": int(fgi_val),
        "vix_source": vix_src,
        "fgi_source": fgi_src,
        **decision,
    }
