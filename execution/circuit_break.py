# -*- coding: utf-8 -*-
"""
전체 계좌(또는 합산 자산) 기준 서킷 브레이커 — 고점 대비 MDD 임계 초과 시 킬스위치.

시장별 `guard.check_mdd_break`(약 -5%)와 별개로, **합산 자산**에 쓸 때 사용.
`run_bot` 연동 시 `bot_state.json`에 peak 저장 키를 정해 일관되게 갱신하면 됨.
"""
from __future__ import annotations

from typing import Any, Dict


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
