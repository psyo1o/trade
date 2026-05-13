# -*- coding: utf-8 -*-
"""Phase 2 보조 — 상대강도(RS) 정렬·변동성 타겟 비중."""
from __future__ import annotations

from typing import Callable, Sequence


def _pct_return_last_n(ohlcv: Sequence[dict], n: int = 10) -> float | None:
    if not ohlcv or len(ohlcv) < n + 1:
        return None
    try:
        c0 = float(ohlcv[-(n + 1)].get("c", 0) or 0)
        c1 = float(ohlcv[-1].get("c", 0) or 0)
    except (TypeError, ValueError, IndexError):
        return None
    if c0 <= 0 or c1 <= 0:
        return None
    return (c1 / c0 - 1.0) * 100.0


def relative_strength_10d(
    ticker_ohlcv: Sequence[dict],
    benchmark_ohlcv: Sequence[dict],
    *,
    lookback: int = 10,
) -> float:
    """(종목 N일 수익률 − 벤치마크 N일 수익률). 데이터 부족 시 0."""
    stock_ret = _pct_return_last_n(ticker_ohlcv, lookback)
    bench_ret = _pct_return_last_n(benchmark_ohlcv, lookback)
    if stock_ret is None or bench_ret is None:
        return 0.0
    return float(stock_ret - bench_ret)


def sort_targets_by_relative_strength(
    tickers: list[str],
    market: str,
    *,
    fetch_ohlcv: Callable[[str], list],
    fetch_benchmark_ohlcv: Callable[[str], list],
    benchmark_ticker: str,
) -> list[str]:
    """V8/SWING 후보 리스트를 10일 RS 내림차순 정렬."""
    if not tickers:
        return []
    bench = fetch_benchmark_ohlcv(benchmark_ticker) or []
    scored: list[tuple[float, str]] = []
    for t in tickers:
        ohlcv = fetch_ohlcv(t) or []
        rs = relative_strength_10d(ohlcv, bench)
        scored.append((rs, str(t)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


def volatility_target_ratio(
    base_ratio: float,
    atr: float,
    close: float,
    *,
    target_vol: float = 0.02,
) -> float:
    """``min(base_ratio, target_vol / ATR%)`` — ATR%는 종가 대비 소수."""
    br = float(base_ratio)
    if br <= 0:
        return br
    atr_f = float(atr or 0.0)
    close_f = float(close or 0.0)
    if atr_f <= 0 or close_f <= 0:
        return br
    atr_pct = atr_f / close_f
    if atr_pct <= 0:
        return br
    vol_ratio = float(target_vol) / atr_pct
    return min(br, vol_ratio)
