# -*- coding: utf-8 -*-
"""
매크로 지표 수집 (통신/API 전담).

- US: SPY Put/Call OI 비율
- COIN: 바이낸스 상위 트레이더 롱/숏 비율
- KR: 원/달러 5일 이평 대비 모멘텀

정책(차단)은 `strategy/macro_guard.py`에서 처리.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests


def _yf_history_close_series(symbol: str, period: str = "10d"):
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(symbol).history(period=period)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        return hist["Close"]
    except Exception:
        return None


def fetch_us_put_call_ratio(symbol: str = "SPY") -> Optional[float]:
    """SPY 최근 만기 옵션 Put/Call OI 비율. 실패 시 None."""
    sym = str(symbol or "SPY").strip().upper() or "SPY"
    try:
        import pandas as pd  # type: ignore
        import yfinance as yf  # type: ignore

        tk = yf.Ticker(sym)
        expiries = list(getattr(tk, "options", []) or [])
        if not expiries:
            return None
        expiry = sorted(expiries)[0]
        chain = tk.option_chain(expiry)
        calls = chain.calls if hasattr(chain, "calls") else pd.DataFrame()
        puts = chain.puts if hasattr(chain, "puts") else pd.DataFrame()
        call_oi = 0.0
        put_oi = 0.0
        if isinstance(calls, pd.DataFrame) and not calls.empty and "openInterest" in calls.columns:
            call_oi = float(pd.to_numeric(calls["openInterest"], errors="coerce").fillna(0).sum())
        if isinstance(puts, pd.DataFrame) and not puts.empty and "openInterest" in puts.columns:
            put_oi = float(pd.to_numeric(puts["openInterest"], errors="coerce").fillna(0).sum())
        if call_oi <= 0 and put_oi <= 0:
            return None
        return float(put_oi / max(call_oi, 1.0))
    except Exception:
        return None


def fetch_coin_whale_short_ratio(symbol: str = "BTCUSDT", period: str = "1d") -> Optional[float]:
    """바이낸스 선물 상위 트레이더 BTCUSDT 롱/숏 비율(1d). 실패 시 None."""
    sym = str(symbol or "BTCUSDT").strip().upper() or "BTCUSDT"
    url = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
    params = {"symbol": sym, "period": str(period or "1d"), "limit": 1}
    try:
        res = requests.get(url, params=params, timeout=10.0)
        if res.status_code >= 400:
            return None
        rows = res.json()
        if not isinstance(rows, list) or not rows:
            return None
        latest = rows[-1] if isinstance(rows[-1], dict) else {}
        ratio = float(latest.get("longShortRatio", 0) or 0)
        return ratio if ratio > 0 else None
    except Exception:
        return None


def fetch_usd_krw_momentum() -> Optional[Dict[str, float]]:
    """원/달러 (현재가 / 5일 이평) 이격도. 실패 시 None."""
    for symbol in ("KRW=X", "USDKRW=X"):
        closes = _yf_history_close_series(symbol, period="10d")
        if closes is None or closes.empty:
            continue
        try:
            spot = float(closes.iloc[-1])
            ma5 = float(closes.tail(5).mean())
            if spot <= 0 or ma5 <= 0:
                continue
            if spot < 100.0:
                spot = 1.0 / spot
                ma5 = 1.0 / ma5
            return {"momentum_ratio": spot / ma5, "symbol": symbol}
        except Exception:
            continue
    return None


def fetch_macro_raw() -> Dict[str, Any]:
    """글로벌 알파 원시 지표. 실패 필드는 None."""
    pcr = fetch_us_put_call_ratio()
    whale = fetch_coin_whale_short_ratio()
    fx = fetch_usd_krw_momentum()
    return {
        "us_put_call_ratio": pcr,
        "coin_whale_long_short_ratio": whale,
        "usd_krw_momentum_ratio": (fx or {}).get("momentum_ratio") if isinstance(fx, dict) else None,
        "usd_krw_symbol": (fx or {}).get("symbol") if isinstance(fx, dict) else None,
    }
