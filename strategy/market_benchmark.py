# -*- coding: utf-8 -*-
"""시장별 벤치마크 — 매수(날씨·RS·급락)·Phase5(청산) 공통."""
from __future__ import annotations

import pandas as pd

_BENCHMARK_YF: dict[str, str] = {
    "KR": "069500.KS",
    "US": "SPY",
}


def normalize_market(market: str) -> str:
    return str(market or "").strip().upper()


def benchmark_ticker(market: str) -> str:
    mk = normalize_market(market)
    if mk == "COIN":
        from api import coin_config
        return coin_config.btc_benchmark_ticker()
    return _BENCHMARK_YF.get(mk, "")


def benchmark_label(market: str) -> str:
    mk = normalize_market(market)
    return {"KR": "KODEX200", "US": "SPY", "COIN": "BTC"}.get(mk, benchmark_ticker(mk))


def _period_to_bars(period: str) -> int:
    p = str(period or "6mo").lower()
    if p in ("6mo", "6m"):
        return 130
    if p in ("2mo", "2m"):
        return 45
    if p in ("5d", "5day"):
        return 5
    return 130


def _fetch_yf_closes(symbol: str, *, period: str = "6mo") -> pd.Series | None:
    from utils.yfinance_guard import yf_call
    sym = str(symbol or "").strip()
    if not sym:
        return None
    df = yf_call(
        lambda: __import__("yfinance", fromlist=["Ticker"]).Ticker(sym).history(period=period),
        label=f"benchmark_{sym}",
        ticker=sym,
    )
    if df is None or df.empty or "Close" not in df.columns:
        return None
    closes = df["Close"].dropna()
    if len(closes) < 2:
        return None
    return closes


def _fetch_coin_closes(*, period: str = "6mo") -> pd.Series | None:
    from api import coin_broker
    ticker = benchmark_ticker("COIN")
    rows = coin_broker.fetch_ohlcv(ticker, "day", _period_to_bars(period))
    if not rows or len(rows) < 5:
        return None
    vals = [float(r["c"]) for r in rows if float(r.get("c") or 0) > 0]
    if len(vals) < 5:
        return None
    return pd.Series(vals)


def fetch_benchmark_closes(market: str, *, period: str = "6mo") -> pd.Series | None:
    mk = normalize_market(market)
    if mk == "COIN":
        return _fetch_coin_closes(period=period)
    sym = benchmark_ticker(mk)
    if not sym:
        return None
    return _fetch_yf_closes(sym, period=period)


def daily_change_pct(market: str) -> float:
    mk = normalize_market(market)
    try:
        if mk == "COIN":
            from api import coin_broker
            ticker = benchmark_ticker("COIN")
            oc = coin_broker.fetch_ohlcv(ticker, "day", 3)
            if oc and len(oc) >= 2:
                prev_close = float(oc[-2]["c"])
                curr_close = float(oc[-1]["c"])
                if prev_close > 0:
                    return ((curr_close - prev_close) / prev_close) * 100.0
            return 0.0
        closes = _fetch_yf_closes(benchmark_ticker(mk), period="5d")
        if closes is not None and len(closes) >= 2:
            prev_close = float(closes.iloc[-2])
            curr_close = float(closes.iloc[-1])
            if prev_close > 0:
                return ((curr_close - prev_close) / prev_close) * 100.0
    except Exception:
        return 0.0
    return 0.0
