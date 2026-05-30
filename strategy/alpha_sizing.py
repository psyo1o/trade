# -*- coding: utf-8 -*-
"""Phase 2 보조 — 상대강도(RS) 정렬·변동성 타겟 비중·포트폴리오 Heat."""
from __future__ import annotations

from typing import Callable, Sequence

from strategy.indicators import get_safe_atr


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


def atr_pct_from_ohlcv(ohlcv: Sequence[dict], ticker: str = "") -> float:
    """종가 대비 ATR% (소수). 산출 불가 시 0."""
    if not ohlcv:
        return 0.0
    try:
        atr_val = float(get_safe_atr(ticker, ohlcv) or 0.0)
        close_px = float(ohlcv[-1].get("c", 0) or 0.0)
    except (TypeError, ValueError, IndexError, KeyError):
        return 0.0
    if atr_val <= 0 or close_px <= 0:
        return 0.0
    return atr_val / close_px


def position_heat_contribution(weight_ratio: float, atr_pct: float) -> float:
    """단일 종목 Heat = 배분 비중 × ATR%."""
    w = float(weight_ratio)
    a = float(atr_pct)
    if w <= 0 or a <= 0:
        return 0.0
    return w * a


def compute_market_portfolio_heat(
    positions: dict,
    market: str,
    market_equity: float,
    *,
    resolve_market: Callable[[str], str],
    position_qty: Callable[[str, dict], float],
    fetch_ohlcv: Callable[[str], list],
    extra_weight: float = 0.0,
    extra_atr_pct: float = 0.0,
) -> float:
    """
    시장별 Portfolio Heat = Σ (종목 비중 × ATR%).

    ``extra_weight`` × ``extra_atr_pct`` — 신규 매수 1건 가산(사전 검증용).
    """
    eq = float(market_equity)
    if eq <= 0:
        return 0.0
    m = str(market or "").strip().upper()
    total = 0.0
    if not isinstance(positions, dict):
        positions = {}
    for ticker, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        if resolve_market(str(ticker)) != m:
            continue
        try:
            buy_p = float(pos.get("buy_p", 0) or pos.get("avg_price", 0) or 0)
            qty = float(position_qty(str(ticker), pos) or 0)
        except (TypeError, ValueError):
            continue
        if buy_p <= 0 or qty <= 0:
            continue
        weight = (buy_p * qty) / eq
        ohlcv = fetch_ohlcv(str(ticker)) or []
        ap = atr_pct_from_ohlcv(ohlcv, str(ticker))
        total += position_heat_contribution(weight, ap)
    if extra_weight > 0 and extra_atr_pct > 0:
        total += position_heat_contribution(extra_weight, extra_atr_pct)
    return float(total)


def portfolio_heat_blocks_entry(
    heat_pct: float,
    max_heat_pct: float,
) -> bool:
    """Heat가 임계 이상이면 신규 매수 차단."""
    return float(heat_pct) >= float(max_heat_pct)
