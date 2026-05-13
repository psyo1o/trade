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

from api.macro_data import (
    fetch_coin_whale_short_ratio,
    fetch_crypto_fear_greed_index,
    fetch_us_put_call_ratio,
    fetch_usd_krw_momentum,
    fetch_vix_close,
)


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


def evaluate_market_macro_buy_permission(
    market: str,
    *,
    us_put_call_ratio: float | None,
    coin_whale_long_short_ratio: float | None,
    usd_krw_spot: float | None,
    usd_krw_momentum_ratio: float | None,
    us_pcr_block: float = 1.2,
    coin_whale_block: float = 0.8,
    krw_fx_momentum_block: float = 1.015,
    krw_fx_spot_block: float = 1500.0,
) -> Dict[str, Any]:
    """시장별 글로벌 알파 차단(US PCR·코인 고래·원화 환율). 지표 미수집 시 통과."""
    mk = str(market or "").strip().upper()
    pcr = float(us_put_call_ratio) if us_put_call_ratio is not None else None
    whale = float(coin_whale_long_short_ratio) if coin_whale_long_short_ratio is not None else None
    fx_spot = float(usd_krw_spot) if usd_krw_spot is not None else None
    fx_mom = float(usd_krw_momentum_ratio) if usd_krw_momentum_ratio is not None else None

    if mk == "US":
        if pcr is not None and pcr >= float(us_pcr_block):
            return {
                "allowed": False,
                "reason": f"SPY Put/Call {pcr:.3f} >= {us_pcr_block:g}",
            }
        return {"allowed": True, "reason": "US 글로벌 지표 정상"}

    if mk == "COIN":
        if whale is not None and whale <= float(coin_whale_block):
            return {
                "allowed": False,
                "reason": f"BTC 고래 롱숏 {whale:.3f} <= {coin_whale_block:g}",
            }
        return {"allowed": True, "reason": "COIN 글로벌 지표 정상"}

    if mk == "KR":
        reasons: list[str] = []
        if pcr is not None and pcr >= float(us_pcr_block):
            reasons.append(f"SPY Put/Call {pcr:.3f} >= {us_pcr_block:g}")
        if fx_mom is not None and fx_mom >= float(krw_fx_momentum_block):
            reasons.append(f"환율 모멘텀 {fx_mom:.4f} >= {krw_fx_momentum_block:g}")
        if fx_spot is not None and fx_spot >= float(krw_fx_spot_block):
            reasons.append(f"환율 {fx_spot:.2f} >= {krw_fx_spot_block:g}")
        if reasons:
            return {"allowed": False, "reason": " | ".join(reasons)}
        return {"allowed": True, "reason": "KR 글로벌 지표 정상"}

    return {"allowed": True, "reason": "unknown market"}


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
            "us_put_call_ratio": None,
            "coin_whale_long_short_ratio": None,
            "usd_krw_spot": None,
            "usd_krw_momentum_ratio": None,
            "market_buy_allowed": {"KR": True, "US": True, "COIN": True},
            "market_buy_block_reason": {"KR": "", "US": "", "COIN": ""},
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

    us_pcr_block = float(config.get("macro_us_put_call_block_threshold", 1.2))
    coin_whale_block = float(config.get("macro_coin_whale_long_short_block_threshold", 0.8))
    krw_fx_mom_block = float(config.get("macro_krw_fx_momentum_block_threshold", 1.015))
    krw_fx_spot_block = float(config.get("macro_krw_fx_spot_block_threshold", 1500.0))

    us_pcr = fetch_us_put_call_ratio(str(config.get("macro_us_put_call_symbol", "SPY")))
    whale_ratio = fetch_coin_whale_short_ratio(
        str(config.get("macro_coin_whale_symbol", "BTCUSDT")),
        str(config.get("macro_coin_whale_period", "1d")),
    )
    fx_pack = fetch_usd_krw_momentum() or {}
    usd_krw_spot = _coerce_float(fx_pack.get("spot"))
    usd_krw_momentum_ratio = _coerce_float(fx_pack.get("momentum_ratio"))

    market_buy_allowed: Dict[str, bool] = {}
    market_buy_block_reason: Dict[str, str] = {}
    for mk in ("KR", "US", "COIN"):
        perm = evaluate_market_macro_buy_permission(
            mk,
            us_put_call_ratio=us_pcr,
            coin_whale_long_short_ratio=whale_ratio,
            usd_krw_spot=usd_krw_spot,
            usd_krw_momentum_ratio=usd_krw_momentum_ratio,
            us_pcr_block=us_pcr_block,
            coin_whale_block=coin_whale_block,
            krw_fx_momentum_block=krw_fx_mom_block,
            krw_fx_spot_block=krw_fx_spot_block,
        )
        market_buy_allowed[mk] = bool(perm.get("allowed", True))
        market_buy_block_reason[mk] = str(perm.get("reason", "") or "")

    return {
        "enabled": True,
        "vix": float(vix_val),
        "fgi": int(fgi_val),
        "vix_source": vix_src,
        "fgi_source": fgi_src,
        "us_put_call_ratio": us_pcr,
        "coin_whale_long_short_ratio": whale_ratio,
        "usd_krw_spot": usd_krw_spot,
        "usd_krw_momentum_ratio": usd_krw_momentum_ratio,
        "market_buy_allowed": market_buy_allowed,
        "market_buy_block_reason": market_buy_block_reason,
        **decision,
    }
