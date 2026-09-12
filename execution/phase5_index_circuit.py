# -*- coding: utf-8 -*-
"""Phase5 시장별 서킷 — 벤치마크 지수 고점 대비 MDD (계좌 총평 미사용)."""
from __future__ import annotations

from datetime import date
from typing import Any

from execution.circuit_break import drawdown_from_peak_pct, evaluate_total_account_circuit
from strategy.market_benchmark import benchmark_ticker, fetch_benchmark_closes, normalize_market

DEFAULT_LOOKBACK = "6mo"

# yfinance 이상치(단일 스파이크) — 99%分位 고점으로 DD 완화
_PEAK_QUANTILE = 0.99


def _robust_peak(closes) -> float:
    vals = [float(x) for x in closes.tolist() if float(x) > 0]
    if not vals:
        return 0.0
    vals.sort()
    idx = min(len(vals) - 1, max(0, int(len(vals) * _PEAK_QUANTILE)))
    q_peak = float(vals[idx])
    raw_max = float(max(vals))
    med = float(vals[len(vals) // 2])
    if med > 0 and raw_max > med * 2.0 and q_peak < raw_max * 0.85:
        return q_peak
    return raw_max


def evaluate_index_circuit_for_market(
    market: str,
    *,
    trigger_drawdown_pct: float = 15.0,
    lookback: str = DEFAULT_LOOKBACK,
) -> dict[str, Any]:
    """단일 시장 지수 MDD 판정. 실패 시 triggered=False."""
    mk = normalize_market(market)
    sym = benchmark_ticker(mk)
    if not sym:
        return {
            "market": mk,
            "triggered": False,
            "reason": f"{mk} 벤치마크 없음",
            "benchmark": "",
            "peak": 0.0,
            "current": 0.0,
            "drawdown_pct": 0.0,
            "trigger_drawdown_pct": float(trigger_drawdown_pct),
        }

    closes = fetch_benchmark_closes(mk, period=lookback)
    if closes is None:
        return {
            "market": mk,
            "triggered": False,
            "reason": f"{sym} 시세 조회 실패 — 이번 루프 스킵",
            "benchmark": sym,
            "peak": 0.0,
            "current": 0.0,
            "drawdown_pct": 0.0,
            "trigger_drawdown_pct": float(trigger_drawdown_pct),
        }

    peak = _robust_peak(closes)
    current = float(closes.iloc[-1])
    d0 = closes.index[0] if hasattr(closes, "index") and len(closes.index) else None
    d1 = closes.index[-1] if hasattr(closes, "index") and len(closes.index) else None
    try:
        d0s = d0.date() if hasattr(d0, "date") else date.today()
        d1s = d1.date() if hasattr(d1, "date") else date.today()
    except Exception:
        d0s = d1s = date.today()
    range_s = f"{d0s}~{d1s}"

    ev = evaluate_total_account_circuit(peak, current, trigger_drawdown_pct=trigger_drawdown_pct)
    triggered = bool(ev["triggered"])
    dd = float(ev["drawdown_pct"])
    if triggered:
        reason = (
            f"{mk} {sym} 고점 ${peak:,.2f} 대비 {dd:.2f}% 하락 "
            f"(임계 {trigger_drawdown_pct:g}%, 구간 {range_s})"
            if mk == "US"
            else (
                f"{mk} {sym} 고점 {peak:,.0f} 대비 {dd:.2f}% 하락 "
                f"(임계 {trigger_drawdown_pct:g}%, 구간 {range_s})"
            )
        )
    else:
        reason = (
            f"{mk} {sym} 고점 대비 {dd:.2f}% — 임계({trigger_drawdown_pct:g}%) 이내 "
            f"({range_s})"
        )

    return {
        "market": mk,
        "triggered": triggered,
        "reason": reason,
        "benchmark": sym,
        "benchmark_range": range_s,
        "peak": peak,
        "current": current,
        "drawdown_pct": dd,
        "trigger_drawdown_pct": float(trigger_drawdown_pct),
        "floor_equity": float(ev.get("floor_equity", 0) or 0),
    }


def evaluate_per_market_index_circuits(
    *,
    market_ok: dict[str, bool],
    trigger_drawdown_pct: float = 15.0,
    lookback: str = DEFAULT_LOOKBACK,
) -> dict[str, dict[str, Any]]:
    """KR/US/COIN 각각 벤치마크 지수 MDD."""
    out: dict[str, dict[str, Any]] = {}
    for mk in ("KR", "US", "COIN"):
        if not bool(market_ok.get(mk, False)):
            out[mk] = {
                "market": mk,
                "triggered": False,
                "reason": "장외·aux 미확인 — 지수 MDD 서킷 스킵",
            }
            continue
        out[mk] = evaluate_index_circuit_for_market(
            mk,
            trigger_drawdown_pct=trigger_drawdown_pct,
            lookback=lookback,
        )
    return out
