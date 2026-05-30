# -*- coding: utf-8 -*-
"""
Yahoo Finance(yfinance) 401·크럼 오류 완화.

* stderr 흡수 → GUI/텔레 스팸 감소
* 전역 차단 없음 — 티커별 짧은 백오프만(200봉은 KIS·Stooq·디스크 캐시로 확보)
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys
import time
from typing import Any, Callable, TypeVar

# 티커별 401 백오프(초) — 같은 종목만 잠깐 yfinance 스킵
_YF_TICKER_UNTIL: dict[str, float] = {}
_YF_TICKER_BACKOFF_SEC = 90.0

for _lg in ("yfinance", "urllib3", "peewee"):
    logging.getLogger(_lg).setLevel(logging.ERROR)

T = TypeVar("T")


def is_yahoo_noise_line(text: str) -> bool:
    """GUI·텔레그램 알림에서 제외할 야후/캘린더 노이즈."""
    s = (text or "").strip().lower()
    if not s:
        return False
    if "http error 401" in s and ("yahoo" in s or "finance" in s or "unauthorized" in s):
        return True
    if "unauthorized" in s and "finance" in s:
        return True
    if "invalid crumb" in s:
        return True
    if "pandas_market_calendars" in s and "break_start" in s and "discontinued" in s:
        return True
    if "_prepare_regular_market_times" in s:
        return True
    if "discontinued_market_times" in s and "break_start" in s:
        return True
    return False


def _yf_auth_failure(exc_or_text: Any) -> bool:
    s = str(exc_or_text or "").lower()
    return "401" in s or "unauthorized" in s or "invalid crumb" in s


def yf_ticker_allowed(ticker: str) -> bool:
    """이 티커에 yfinance 재시도 가능 여부."""
    key = str(ticker or "").strip().upper()
    return time.time() >= _YF_TICKER_UNTIL.get(key, 0.0)


def yf_ticker_backoff(ticker: str, *, sec: float | None = None) -> None:
    key = str(ticker or "").strip().upper()
    wait = float(_YF_TICKER_BACKOFF_SEC if sec is None else sec)
    _YF_TICKER_UNTIL[key] = time.time() + max(15.0, wait)


def yf_is_blocked() -> bool:
    """하위 호환 — 전역 차단은 사용하지 않음."""
    return False


def yf_note_auth_block(cooldown_sec: float | None = None) -> None:
    """하위 호환 no-op (전역 차단 제거)."""
    return


@contextlib.contextmanager
def yf_suppress_stderr():
    """yfinance가 stderr에 찍는 HTTP 401 등을 흡수."""
    buf = io.StringIO()
    old_err = sys.stderr
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = old_err


def yf_call(fn: Callable[[], T], *, label: str = "yfinance", ticker: str = "") -> T | None:
    """stderr 억제 후 호출. 401이면 None (전역 차단 없음)."""
    if ticker and not yf_ticker_allowed(ticker):
        return None
    try:
        with yf_suppress_stderr() as cap:
            out = fn()
        if cap.getvalue() and _yf_auth_failure(cap.getvalue()):
            if ticker:
                yf_ticker_backoff(ticker)
            return None
        return out
    except Exception as e:
        if _yf_auth_failure(e):
            if ticker:
                yf_ticker_backoff(ticker)
            return None
        raise
