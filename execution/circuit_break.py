# -*- coding: utf-8 -*-
"""
계좌 서킷 보조 — (1) 합산 고점 MDD (레거시), (2) **시장별 포트폴리오 비중** 하한.

시장별 `guard.check_mdd_break`(약 -5%)와 별개.
`run_bot`` Phase5 기본 모드는 **합산이 아니라** KR/US/COIN 각각의 전체 대비 비중(%)이
``config`` 하한 미만일 때 **해당 시장만** 청산한다 (API 한쪽 실패가 합산을 깨뜨리지 않도록).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping


def estimate_usdkrw(default: float = 1380.0) -> float:
    """USD→KRW 대략 환율(yfinance USDKRW=X). 실패 시 default."""
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker("USDKRW=X").history(period="5d")
        if hist is not None and not hist.empty:
            v = float(hist["Close"].iloc[-1])
            if v > 800.0:
                return v
    except Exception:
        pass
    return float(default)


def drawdown_from_peak_pct(peak_equity: float, current_equity: float) -> float:
    """고점 대비 하락률(%) — ``peak`` 가 0 이하면 0 반환."""
    if peak_equity <= 0:
        return 0.0
    return (peak_equity - float(current_equity)) / peak_equity * 100.0


def evaluate_total_account_circuit(
    peak_equity: float,
    current_equity: float,
    *,
    trigger_drawdown_pct: float = 15.0,
) -> Dict[str, Any]:
    """
    주차 트레일링 고점 대비 자산이 ``trigger_drawdown_pct``% 이상 감소하면 발동.

    조건: ``(peak - current) / peak * 100 >= thr`` (peak>0). 예: 15% → current < peak * 0.85.
    """
    peak = float(peak_equity)
    cur = float(current_equity)
    thr = max(0.0, float(trigger_drawdown_pct))
    floor = peak * (1.0 - thr / 100.0) if peak > 0 else 0.0
    dd = drawdown_from_peak_pct(peak, cur)
    triggered = peak > 0 and cur < floor
    return {
        "triggered": triggered,
        "peak": peak,
        "current": cur,
        "drawdown_pct": dd,
        "trigger_drawdown_pct": thr,
        "floor_equity": floor,
        "reason": (
            f"합산 고점 {peak:,.0f} 대비 {dd:.2f}% 하락 (임계 {thr:g}% — 서킷 발동)"
            if triggered
            else f"합산 고점 대비 {dd:.2f}% 하락 — 임계({thr:g}%) 이내"
        ),
    }


def _market_equity_krw(
    market: str,
    *,
    kr_krw: float,
    us_usd: float,
    coin_krw: float,
    usdkrw: float,
) -> float:
    m = str(market or "").strip().upper()
    if m == "KR":
        return float(kr_krw)
    if m == "US":
        return float(us_usd) * float(usdkrw)
    if m == "COIN":
        return float(coin_krw)
    return 0.0


def portfolio_total_krw_for_share(
    market_equity_krw: Mapping[str, float],
    market_ok: Mapping[str, bool],
) -> float:
    """비중 계산용 합계 — ``market_ok`` 가 True 인 시장만 합산(API 실패 시장 제외)."""
    total = 0.0
    for mk in ("KR", "US", "COIN"):
        if bool(market_ok.get(mk, False)):
            total += max(0.0, float(market_equity_krw.get(mk, 0.0) or 0.0))
    return float(total)


def evaluate_market_share_circuit(
    market: str,
    equity_krw: float,
    total_ok_krw: float,
    *,
    min_share_pct: float,
    anchor_share: float | None = None,
    anchor_min_ratio: float = 0.5,
) -> Dict[str, Any]:
    """
  시장별 포트폴리오 비중 서킷.

  * ``share_pct`` = ``equity_krw / total_ok_krw * 100`` (total_ok>0 일 때)
  * 발동: ``share_pct < min_share_pct`` **또는** (anchor 있을 때)
    ``share_pct < anchor_share * 100 * anchor_min_ratio``
  * ``min_share_pct <= 0`` 이면 해당 시장 서킷 비활성.
    """
    mk = str(market or "").strip().upper()
    eq = max(0.0, float(equity_krw))
    total = max(0.0, float(total_ok_krw))
    floor = max(0.0, float(min_share_pct))
    if floor <= 0.0:
        return {
            "market": mk,
            "triggered": False,
            "share_pct": 0.0,
            "min_share_pct": floor,
            "equity_krw": eq,
            "total_ok_krw": total,
            "reason": "비중 서킷 비활성(min_share_pct<=0)",
        }
    if total <= 0.0:
        return {
            "market": mk,
            "triggered": False,
            "share_pct": 0.0,
            "min_share_pct": floor,
            "equity_krw": eq,
            "total_ok_krw": total,
            "reason": "합산(OK 시장) 0 — 비중 판정 보류",
        }
    share_pct = eq / total * 100.0
    anchor = float(anchor_share) if anchor_share is not None else None
    anchor_floor_pct = None
    if anchor is not None and anchor > 0:
        anchor_floor_pct = anchor * 100.0 * max(0.0, min(1.0, float(anchor_min_ratio)))
    effective_floor = floor
    if anchor_floor_pct is not None:
        effective_floor = max(floor, anchor_floor_pct)
    triggered = share_pct < effective_floor
    reason = (
        f"{mk} 비중 {share_pct:.1f}% < 하한 {effective_floor:.1f}% "
        f"(설정 {floor:g}%"
        + (
            f", 앵커 {anchor * 100:.1f}%×{anchor_min_ratio:.2f}"
            if anchor_floor_pct is not None
            else ""
        )
        + ") — 시장 단위 서킷"
        if triggered
        else f"{mk} 비중 {share_pct:.1f}% (하한 {effective_floor:.1f}%) — 정상"
    )
    return {
        "market": mk,
        "triggered": triggered,
        "share_pct": float(share_pct),
        "min_share_pct": float(floor),
        "effective_floor_pct": float(effective_floor),
        "anchor_share": anchor,
        "equity_krw": eq,
        "total_ok_krw": total,
        "reason": reason,
    }


def evaluate_per_market_share_circuits(
    *,
    kr_krw: float,
    us_usd: float,
    coin_krw: float,
    usdkrw: float,
    market_ok: Mapping[str, bool],
    min_share_pct_by_market: Mapping[str, float],
    share_anchor: Mapping[str, float] | None = None,
    anchor_min_ratio: float = 0.5,
) -> Dict[str, Dict[str, Any]]:
    """KR/US/COIN 각각 비중 서킷 판정 (OK 시장만, 합산은 OK 시장끼리만)."""
    eq = {
        "KR": _market_equity_krw("KR", kr_krw=kr_krw, us_usd=us_usd, coin_krw=coin_krw, usdkrw=usdkrw),
        "US": _market_equity_krw("US", kr_krw=kr_krw, us_usd=us_usd, coin_krw=coin_krw, usdkrw=usdkrw),
        "COIN": _market_equity_krw("COIN", kr_krw=kr_krw, us_usd=us_usd, coin_krw=coin_krw, usdkrw=usdkrw),
    }
    total_ok = portfolio_total_krw_for_share(eq, market_ok)
    anchor = share_anchor if isinstance(share_anchor, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for mk in ("KR", "US", "COIN"):
        if not bool(market_ok.get(mk, False)):
            out[mk] = {
                "market": mk,
                "triggered": False,
                "share_pct": 0.0,
                "reason": "circuit_aux 미확인 — 비중 서킷 스킵",
            }
            continue
        min_pct = float(min_share_pct_by_market.get(mk, 0.0) or 0.0)
        anc = None
        try:
            if mk in anchor:
                anc = float(anchor.get(mk, 0.0) or 0.0)
        except (TypeError, ValueError):
            anc = None
        out[mk] = evaluate_market_share_circuit(
            mk,
            eq[mk],
            total_ok,
            min_share_pct=min_pct,
            anchor_share=anc,
            anchor_min_ratio=float(anchor_min_ratio),
        )
    return out
