# -*- coding: utf-8 -*-
"""
매크로 지표 수집 (통신/API 전담).

- US: SPY Put/Call OI 비율
- COIN: 바이낸스 상위 트레이더 롱/숏 비율
- KR: 원/달러 Z-Score(20일) + 실시간 spot + 당일 방향

정책(차단)은 `strategy/macro_guard.py`에서 처리.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)

USD_KRW_YF_SYMBOLS = ("USDKRW=X", "KRW=X")
USD_KRW_ETF_PROXY_CODE = "261240"
USD_KRW_ETF_YF_SYMBOL = "261240.KS"


def _yf_history_close_series(symbol: str, period: str = "10d"):
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(symbol).history(period=period)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        return hist["Close"]
    except Exception:
        return None


def _normalize_usdkrw_rate(raw: float) -> float:
    """yfinance KRW=X(역수) / USDKRW=X 를 KRW/USD 로 통일."""
    v = float(raw or 0)
    if v <= 0:
        return 0.0
    if v < 100.0:
        return 1.0 / v
    return v


def _daily_usdkrw_closes(min_bars: int = 21) -> tuple[Optional[Any], str]:
    """일봉 종가 시리즈(KRW/USD). 최소 ``min_bars`` 확보 시 반환."""
    for symbol in USD_KRW_YF_SYMBOLS:
        closes = _yf_history_close_series(symbol, period="3mo")
        if closes is None or closes.empty:
            continue
        try:
            normed = closes.apply(_normalize_usdkrw_rate)
            normed = normed[normed > 0]
            if len(normed) < min_bars:
                continue
            return normed, symbol
        except Exception:
            continue
    return None, ""


def _yf_intraday_spot_usdkrw() -> Optional[float]:
    """yfinance 1분봉 최신가 — 지연 일봉 종가 대신 실시간 근사."""
    for symbol in USD_KRW_YF_SYMBOLS:
        try:
            import yfinance as yf  # type: ignore

            hist = yf.Ticker(symbol).history(period="1d", interval="1m")
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            raw = float(hist["Close"].dropna().iloc[-1])
            spot = _normalize_usdkrw_rate(raw)
            if spot > 0:
                return spot
        except Exception as e:
            log.debug("USDKRW 1m spot 실패 (%s): %s", symbol, e)
    return None


def _kis_etf_proxy_spot(*, prev_fx: float) -> Optional[float]:
    """
    KODEX 미국달러선물(261240) 당일 변동률로 전일 환율 종가를 스케일.

    ETF 가격 자체는 KRW/USD 가 아니므로, 전일 ETF 종가 대비 현재가 비율만 사용한다.
    """
    if prev_fx <= 0:
        return None
    etf_prev = _etf_last_settled_close()
    if etf_prev is None or etf_prev <= 0:
        return None
    etf_now = _kis_etf_live_price()
    if etf_now is None or etf_now <= 0:
        return None
    return float(prev_fx) * (float(etf_now) / float(etf_prev))


def _etf_last_settled_close() -> Optional[float]:
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(USD_KRW_ETF_YF_SYMBOL).history(period="10d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception as e:
        log.debug("261240 일봉 종가 실패: %s", e)
        return None


def _kis_etf_live_price() -> Optional[float]:
    try:
        from api import kis_api

        broker = getattr(kis_api, "broker_kr", None)
        if broker is None:
            return None
        resp = broker.fetch_price(USD_KRW_ETF_PROXY_CODE)
        if not isinstance(resp, dict) or str(resp.get("rt_cd", "")) != "0":
            return None
        out = resp.get("output") if isinstance(resp.get("output"), dict) else {}
        px = float(out.get("stck_prpr", 0) or 0)
        return px if px > 0 else None
    except Exception as e:
        log.debug("KIS 261240 실시간 실패: %s", e)
        return None


def _fetch_realtime_usdkrw_spot(*, prev_fx: float) -> tuple[Optional[float], str]:
    """
    실시간 USD/KRW spot.

    우선순위: yfinance 1분봉 → KIS 261240 비율 프록시.
    """
    spot = _yf_intraday_spot_usdkrw()
    if spot is not None and spot > 0:
        return spot, "USDKRW_1m"
    proxy = _kis_etf_proxy_spot(prev_fx=prev_fx)
    if proxy is not None and proxy > 0:
        return proxy, f"ETF_{USD_KRW_ETF_PROXY_CODE}"
    return None, ""


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


def fetch_usd_krw_momentum() -> Optional[Dict[str, float | bool | str]]:
    """
    원/달러 동적 변동성(Z-Score) + 실시간 spot + 당일 방향.

    * ``ma20`` / ``std20``: 최근 20거래일 일봉 종가
    * ``prev_spot``: 직전 일봉 종가(전일 환율)
    * ``spot``: 1분봉 또는 261240 프록시 (지연 일봉 종가 사용 안 함)
    * ``z_score``: (spot - ma20) / std20
    * ``is_rising``: spot > prev_spot
    """
    closes, symbol = _daily_usdkrw_closes(min_bars=21)
    if closes is None or closes.empty:
        return None

    window = closes.tail(20)
    ma20 = float(window.mean())
    std20 = float(window.std(ddof=0))
    prev_spot = float(closes.iloc[-1])
    if ma20 <= 0 or prev_spot <= 0 or std20 <= 0:
        return None

    spot, spot_src = _fetch_realtime_usdkrw_spot(prev_fx=prev_spot)
    if spot is None or spot <= 0:
        return None

    z_score = float((spot - ma20) / std20)
    is_rising = bool(spot > prev_spot)
    return {
        "z_score": z_score,
        "is_rising": is_rising,
        "spot": float(spot),
        "ma20": ma20,
        "std20": std20,
        "prev_spot": prev_spot,
        "symbol": symbol,
        "spot_source": spot_src,
    }


def fetch_macro_raw() -> Dict[str, Any]:
    """글로벌 알파 원시 지표. 실패 필드는 None."""
    pcr = fetch_us_put_call_ratio()
    whale = fetch_coin_whale_short_ratio()
    fx = fetch_usd_krw_momentum()
    fx_dict = fx if isinstance(fx, dict) else {}
    return {
        "us_put_call_ratio": pcr,
        "coin_whale_long_short_ratio": whale,
        "usd_krw_fx": fx_dict or None,
        "usd_krw_z_score": fx_dict.get("z_score"),
        "usd_krw_is_rising": fx_dict.get("is_rising"),
        "usd_krw_spot": fx_dict.get("spot"),
        "usd_krw_ma20": fx_dict.get("ma20"),
        "usd_krw_symbol": fx_dict.get("symbol"),
    }
