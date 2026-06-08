# -*- coding: utf-8 -*-
"""
잔고 조회 — TTL 스냅샷·최소 호출 간격으로 KIS API 폭주(EGW00201) 완화.

* **조회(GET) 자체는 멱등** — 같은 요청을 여러 번 해도 계좌 상태를 바꾸지 않음.
* 문제는 **한 슬라이스 안에서 before/after 를 서로 다른 시점 API로 비교**할 때 생김.
* 이 모듈은 시장별 잔고 응답을 **수 초 TTL** 로 묶어, facade·GUI·run_bot 이 **같은 스냅샷**을 쓰게 한다.
* KIS 한도/일시 오류 시 **직전 정상 응답(stale)** 을 최대 90초까지 재사용한다.

``run_bot`` TWAP·``idempotency`` 체결 보정은 ``kr_stock_qty`` / ``us_stock_qty`` / ``coin_stock_qty`` 를 쓴다.
사이클 시작·주문 성공 직후에는 ``invalidate()`` 로 캐시를 비운다.

환경 변수(선택): ``BOT_KIS_BALANCE_CACHE_TTL_SEC``, ``BOT_KIS_BALANCE_MIN_INTERVAL_SEC``,
``BOT_KIS_BALANCE_STALE_SEC``.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from execution import idempotency as idem

# market upper -> (monotonic_ts, raw payload) — 마지막 **캐시 가능** 응답
_cache: dict[str, tuple[float, Any]] = {}
# market -> last **실제 API** 호출 시각
_last_api_mono: dict[str, float] = {}
_stale_log_mono: dict[str, float] = {}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def cache_ttl_sec() -> float:
    return max(8.0, _env_float("BOT_KIS_BALANCE_CACHE_TTL_SEC", 20.0))


def min_api_interval_sec() -> float:
    return max(1.5, _env_float("BOT_KIS_BALANCE_MIN_INTERVAL_SEC", 4.0))


def stale_ok_sec() -> float:
    return max(cache_ttl_sec(), _env_float("BOT_KIS_BALANCE_STALE_SEC", 90.0))


def invalidate(market: str | None = None) -> None:
    """캐시 무효화 — 주문 체결 직후·사이클 시작 시 호출."""
    if market is None:
        _cache.clear()
        _last_api_mono.clear()
        _clear_us_cash_cache()
        return
    key = str(market).strip().upper()
    _cache.pop(key, None)
    _last_api_mono.pop(key, None)
    if key == "US":
        _clear_us_cash_cache()


def _clear_us_cash_cache() -> None:
    try:
        from api.kis_api import get_us_cash_real

        if hasattr(get_us_cash_real, "_us_cash_cache"):
            delattr(get_us_cash_real, "_us_cash_cache")
    except Exception:
        pass


def _kis_balance_cacheable(raw: Any, market: str) -> bool:
    if not isinstance(raw, dict) or not raw:
        return False
    try:
        from api.kis_parsers import kis_response_rate_limited

        if kis_response_rate_limited(raw):
            return False
    except Exception:
        pass
    rt = str(raw.get("rt_cd", raw.get("RT_CD", "0")) or "0").strip()
    if rt and rt != "0":
        return False
    mk = str(market).strip().upper()
    if mk in ("KR", "US"):
        return ("output1" in raw) or ("output2" in raw)
    return True


def _stale_entry(market: str) -> Any | None:
    key = str(market).strip().upper()
    ent = _cache.get(key)
    if not ent:
        return None
    ts, raw = ent
    if (time.monotonic() - ts) > stale_ok_sec():
        return None
    if not _kis_balance_cacheable(raw, key):
        return None
    return raw


def _log_stale_once(market: str, reason: str) -> None:
    key = str(market).strip().upper()
    now = time.monotonic()
    if now - float(_stale_log_mono.get(key, 0.0)) < 45.0:
        return
    _stale_log_mono[key] = now
    print(f"  [잔고 캐시-{key}] {reason} / 직전 정상 응답 재사용")


def _ledger_only(refresh: bool) -> bool:
    try:
        import run_bot as rb
        from execution.balance_policy import should_use_ledger_only

        st = rb.load_state(rb.STATE_PATH)
        return should_use_ledger_only(st, rb.config, force=bool(refresh))
    except Exception:
        return False


def _persist_live_cash(market: str, raw: Any) -> None:
    if not _kis_balance_cacheable(raw, market):
        return
    try:
        import run_bot as rb
        from services import ledger_valuation as lv

        st = rb.load_state(rb.STATE_PATH)
        mk = str(market).strip().upper()
        if mk == "KR":
            lv.persist_kr_cash_from_balance(raw, st)
        elif mk == "US":
            lv.persist_us_cash_from_balance(raw, st)
        rb.save_state(rb.STATE_PATH, st)
    except Exception:
        pass


def _ledger_balance(market: str) -> Any:
    import run_bot as rb
    from services import ledger_valuation as lv

    st = rb.load_state(rb.STATE_PATH)

    def _kr_price(code: str, pos: dict, buy_p: float) -> float:
        return float(
            rb.resolve_holding_display_price("KR", code, buy_p, None, pos)
        )

    def _us_price(ticker: str, pos: dict, buy_p: float) -> float:
        return float(
            rb.resolve_holding_display_price("US", ticker, buy_p, None, pos)
        )

    mk = str(market).strip().upper()
    if mk == "KR":
        return lv.synthetic_kr_balance_dict(st, resolve_kr_price=_kr_price)
    if mk == "US":
        return lv.synthetic_us_balance_dict(st, resolve_us_price=_us_price)
    return {}


def _get_raw(
    market: str,
    fetcher: Callable[[], Any],
    *,
    ttl_sec: float | None = None,
    refresh: bool = False,
) -> Any:
    if _ledger_only(refresh):
        return _ledger_balance(market)

    key = str(market).strip().upper()
    ttl = max(8.0, float(ttl_sec if ttl_sec is not None else cache_ttl_sec()))
    min_iv = min_api_interval_sec()
    now = time.monotonic()

    if refresh:
        _cache.pop(key, None)

    if not refresh and key in _cache:
        ts, raw = _cache[key]
        if (now - ts) < ttl and _kis_balance_cacheable(raw, key):
            return raw

    last_fetch = float(_last_api_mono.get(key, 0.0))
    if not refresh and (now - last_fetch) < min_iv:
        ent = _cache.get(key)
        if ent and _kis_balance_cacheable(ent[1], key):
            return ent[1]
        stale = _stale_entry(key)
        if stale is not None:
            _log_stale_once(key, f"호출 간격 {min_iv:g}s 미만")
            return stale

    _last_api_mono[key] = now
    raw = fetcher()

    if _kis_balance_cacheable(raw, key):
        _cache[key] = (now, raw)
        _persist_live_cash(key, raw)
        return raw

    stale = _stale_entry(key)
    if stale is not None:
        _log_stale_once(key, "KIS 한도/오류 응답(stale)")
        return stale

    return raw


def kr_balance_raw(*, refresh: bool = False, ttl_sec: float | None = None) -> Any:
    from api import kis_api

    return _get_raw(
        "KR",
        kis_api._fetch_kr_balance_with_backoff,
        ttl_sec=ttl_sec,
        refresh=refresh,
    )


def us_balance_raw(*, refresh: bool = False, ttl_sec: float | None = None) -> Any:
    from api import kis_api

    return _get_raw(
        "US",
        kis_api._fetch_us_positions_with_backoff,
        ttl_sec=ttl_sec,
        refresh=refresh,
    )


def coin_balances_raw(*, refresh: bool = False, ttl_sec: float | None = None) -> Any:
    from api import coin_broker

    return _get_raw(
        "COIN",
        coin_broker.get_balances,
        ttl_sec=ttl_sec or 12.0,
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
