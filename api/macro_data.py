# -*- coding: utf-8 -*-
"""
매크로 지표 수집 (통신/API 전담).

- VIX (^VIX, yfinance)
- 크립토 Fear & Greed (Alternative.me)

정책(차단/축소 비율)은 `strategy/macro_guard.py`에서 처리.
"""
from __future__ import annotations

from typing import Any, Dict

import requests


def fetch_vix_close() -> float:
    """^VIX 최근 종가. 실패 시 -1."""
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker("^VIX").history(period="5d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return -1.0


def fetch_crypto_fear_greed_index(timeout: float = 10.0) -> int:
    """Alternative.me Crypto Fear & Greed 0~100. 실패 시 -1."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=timeout)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or []
        if data:
            return int(str(data[0].get("value", "0")).strip())
    except Exception:
        pass
    return -1


def fetch_macro_raw(timeout_fgi: float = 10.0) -> Dict[str, Any]:
    """
    VIX·FGI를 한 번에 담은 dict. 실패 시 해당 필드는 None 이고 ``*_ok`` 플래그가 False.

    정책(차단/배수)은 여기서 하지 않는다 — ``strategy.macro_guard`` 가 처리.
    """
    vix = fetch_vix_close()
    fgi = fetch_crypto_fear_greed_index(timeout=timeout_fgi)
    return {
        "vix": vix if vix > 0 else None,
        "fear_greed": fgi if fgi >= 0 else None,
        "vix_ok": vix > 0,
        "fear_greed_ok": fgi >= 0,
    }
