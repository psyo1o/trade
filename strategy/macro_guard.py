# -*- coding: utf-8 -*-
"""
Phase 4 — 거시 방어막 (시장별 글로벌 알파).

원시 수치는 ``api.macro_data`` 가 가져오고, 이 모듈은 **시장별 신규 매수 차단** 만 판정한다.
``run_bot`` 은 매 사이클 ``get_macro_guard_snapshot(config)`` 로 스냅샷을 받는다.

규칙(기본)
    * **US** — ``us_put_call_ratio`` >= 1.2 → KR/US/COIN 공통 US 경로에서 차단
    * **COIN** — ``coin_whale_long_short_ratio`` <= 0.8 → 차단
    * **KR** — ``usd_krw_momentum_ratio`` >= 1.015 (5일 이평 대비 1.5% 급등) → 차단

VIX·Crypto Fear&Greed 및 환율 절대값(1500원) 차단은 제거됨.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from api.macro_data import (
    fetch_coin_whale_short_ratio,
    fetch_us_put_call_ratio,
    fetch_usd_krw_momentum,
)


def evaluate_market_macro_buy_permission(
    market: str,
    *,
    us_put_call_ratio: float | None,
    coin_whale_long_short_ratio: float | None,
    usd_krw_momentum_ratio: float | None,
    us_pcr_block: float = 1.2,
    coin_whale_block: float = 0.8,
    krw_fx_momentum_block: float = 1.015,
) -> Dict[str, Any]:
    """시장별 글로벌 알파 차단. 지표 미수집 시 통과."""
    mk = str(market or "").strip().upper()
    pcr = float(us_put_call_ratio) if us_put_call_ratio is not None else None
    whale = float(coin_whale_long_short_ratio) if coin_whale_long_short_ratio is not None else None
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
        if fx_mom is not None and fx_mom >= float(krw_fx_momentum_block):
            return {
                "allowed": False,
                "reason": f"환율 모멘텀 {fx_mom:.4f} >= {krw_fx_momentum_block:g}",
            }
        return {"allowed": True, "reason": "KR 글로벌 지표 정상"}

    return {"allowed": True, "reason": "unknown market"}


def _coerce_float(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def get_macro_guard_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    운영/랩 공통: config 기반 글로벌 알파 조회 후 시장별 매수 허용 판정.

    config 키 (선택):
      macro_guard_enabled (기본 True)
      macro_us_put_call_block_threshold, macro_us_put_call_symbol
      macro_coin_whale_long_short_block_threshold, macro_coin_whale_symbol, macro_coin_whale_period
      macro_krw_fx_momentum_block_threshold
    """
    enabled = bool(config.get("macro_guard_enabled", True))
    if not enabled:
        return {
            "enabled": False,
            "mode": "off",
            "budget_multiplier": 1.0,
            "us_put_call_ratio": None,
            "coin_whale_long_short_ratio": None,
            "usd_krw_momentum_ratio": None,
            "market_buy_allowed": {"KR": True, "US": True, "COIN": True},
            "market_buy_block_reason": {"KR": "", "US": "", "COIN": ""},
            "reason": "macro_guard_enabled=false",
        }

    us_pcr_block = float(config.get("macro_us_put_call_block_threshold", 1.2))
    coin_whale_block = float(config.get("macro_coin_whale_long_short_block_threshold", 0.8))
    krw_fx_mom_block = float(config.get("macro_krw_fx_momentum_block_threshold", 1.015))

    us_pcr = fetch_us_put_call_ratio(str(config.get("macro_us_put_call_symbol", "SPY")))
    whale_ratio = fetch_coin_whale_short_ratio(
        str(config.get("macro_coin_whale_symbol", "BTCUSDT")),
        str(config.get("macro_coin_whale_period", "1d")),
    )
    fx_pack = fetch_usd_krw_momentum() or {}
    usd_krw_momentum_ratio = _coerce_float(fx_pack.get("momentum_ratio"))

    market_buy_allowed: Dict[str, bool] = {}
    market_buy_block_reason: Dict[str, str] = {}
    for mk in ("KR", "US", "COIN"):
        perm = evaluate_market_macro_buy_permission(
            mk,
            us_put_call_ratio=us_pcr,
            coin_whale_long_short_ratio=whale_ratio,
            usd_krw_momentum_ratio=usd_krw_momentum_ratio,
            us_pcr_block=us_pcr_block,
            coin_whale_block=coin_whale_block,
            krw_fx_momentum_block=krw_fx_mom_block,
        )
        market_buy_allowed[mk] = bool(perm.get("allowed", True))
        market_buy_block_reason[mk] = str(perm.get("reason", "") or "")

    blocked = [mk for mk in ("KR", "US", "COIN") if not market_buy_allowed.get(mk, True)]
    if blocked:
        reasons = [
            f"{mk}:{market_buy_block_reason.get(mk, '')}"
            for mk in blocked
        ]
        summary = " | ".join(reasons)
    else:
        summary = "글로벌 알파 정상 (US PCR·코인 고래·KR 환율 모멘텀)"

    return {
        "enabled": True,
        "mode": "global_alpha",
        "budget_multiplier": 1.0,
        "us_put_call_ratio": us_pcr,
        "coin_whale_long_short_ratio": whale_ratio,
        "usd_krw_momentum_ratio": usd_krw_momentum_ratio,
        "market_buy_allowed": market_buy_allowed,
        "market_buy_block_reason": market_buy_block_reason,
        "reason": summary,
    }
