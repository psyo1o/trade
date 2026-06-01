# -*- coding: utf-8 -*-
"""
잔고 조회 — 짧은 TTL 스냅샷으로 API 폭주·체결 검증 불일치 완화.

* **조회(GET) 자체는 멱등** — 같은 요청을 여러 번 해도 계좌 상태를 바꾸지 않음.
* 문제는 **한 슬라이스 안에서 before/after 를 서로 다른 시점 API로 비교**할 때 생김.
* 이 모듈은 시장별 잔고 응답을 **수 초 TTL** 로 묶어, 체결 검증·파싱이 **같은 스냅샷 계열**을 쓰게 한다.

``run_bot`` TWAP·``idempotency`` 체결 보정은 ``kr_stock_qty`` / ``us_stock_qty`` / ``coin_stock_qty`` 를 쓴다.
사이클 시작·주문 성공 직후에는 ``invalidate()`` 로 캐시를 비운다.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from execution import idempotency as idem

_DEFAULT_TTL_SEC = 12.0
# market upper -> (monotonic_ts, raw payload)
_cache: dict[str, tuple[float, Any]] = {}


def invalidate(market: str | None = None) -> None:
    """캐시 무효화 — 주문 체결 직후·사이클 시작 시 호출."""
    if market is None:
        _cache.clear()
        return
    _cache.pop(str(market).strip().upper(), None)


def _get_raw(
    market: str,
    fetcher: Callable[[], Any],
    *,
    ttl_sec: float = _DEFAULT_TTL_SEC,
    refresh: bool = False,
) -> Any:
    key = str(market).strip().upper()
    now = time.monotonic()
    if not refresh and key in _cache:
        ts, raw = _cache[key]
        if (now - ts) < max(1.0, float(ttl_sec)):
            return raw
    raw = fetcher()
    _cache[key] = (now, raw)
    return raw


def kr_balance_raw(*, refresh: bool = False, ttl_sec: float = _DEFAULT_TTL_SEC) -> Any:
    from api import kis_api

    return _get_raw(
        "KR",
        kis_api.get_balance_with_retry,
        ttl_sec=ttl_sec,
        refresh=refresh,
    )


def us_balance_raw(*, refresh: bool = False, ttl_sec: float = _DEFAULT_TTL_SEC) -> Any:
    from api import kis_api

    return _get_raw(
        "US",
        kis_api.get_us_positions_with_retry,
        ttl_sec=ttl_sec,
        refresh=refresh,
    )


def coin_balances_raw(*, refresh: bool = False, ttl_sec: float = _DEFAULT_TTL_SEC) -> Any:
    from api import coin_broker

    return _get_raw(
        "COIN",
        coin_broker.get_balances,
        ttl_sec=ttl_sec,
        refresh=refresh,
    )


def kr_stock_qty(ticker: str, *, refresh: bool = False) -> float | None:
    """국장 종목 보유 수량 — 캐시된 잔고 응답에서 파싱."""
    return idem.kis_balance_stock_qty(kr_balance_raw(refresh=refresh), ticker)


def us_stock_qty(ticker: str, *, refresh: bool = False) -> float | None:
    """미장 종목 보유 수량."""
    return idem.kis_balance_stock_qty(us_balance_raw(refresh=refresh), ticker)


def coin_stock_qty(ticker: str, *, refresh: bool = False) -> float | None:
    """코인 base 수량."""
    return idem.coin_base_qty_from_balances(coin_balances_raw(refresh=refresh), ticker)


def stock_qty(market: str, ticker: str, *, refresh: bool = False) -> float | None:
    """시장 코드 → 수량. ``KR`` / ``US`` / ``COIN``."""
    m = str(market).strip().upper()
    if m == "KR":
        return kr_stock_qty(ticker, refresh=refresh)
    if m == "US":
        return us_stock_qty(ticker, refresh=refresh)
    if m == "COIN":
        return coin_stock_qty(ticker, refresh=refresh)
    return None


def kr_balance_for_report(*, refresh: bool = False) -> Any:
    """heartbeat·GUI 스냅샷용 국장 잔고 (TTL 공유)."""
    return kr_balance_raw(refresh=refresh)


def us_balance_for_report(*, refresh: bool = False) -> Any:
    """heartbeat·GUI 스냅샷용 미장 잔고."""
    return us_balance_raw(refresh=refresh)


def coin_balances_for_report(*, refresh: bool = False) -> Any:
    """heartbeat·GUI 스냅샷용 코인 잔고 목록."""
    return coin_balances_raw(refresh=refresh)


