# -*- coding: utf-8 -*-
"""
코인 거래소 단일 진입점 — ``market_preference`` 에 따라 업비트 또는 바이낸스(CCXT).

* 예산·서킷·Phase5 는 기존처럼 **원화(KRW) 환산** 기준을 유지한다.
* 바이낸스 현물은 주문·호가·캔들이 USDT 이며, 매수 예산만 ``krw_per_usdt`` 로 환산한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import binance_api
from api import coin_config
from api import upbit_api
from utils.helpers import coin_holding_meets_min_notional

log = logging.getLogger(__name__)


def get_krw_per_usdt() -> float:
    """1 USDT당 원화. ``config.json`` 의 ``krw_per_usdt`` 우선, 없으면 USDKRW 추정."""
    manual = coin_config.get("krw_per_usdt")
    if manual is not None and float(manual) > 0:
        return float(manual)
    try:
        import yfinance as yf

        df = yf.download(
            "USDKRW=X",
            period="5d",
            interval="1d",
            progress=False,
            threads=False,
        )
        if df is not None and len(df) > 0:
            if hasattr(df.columns, "levels"):
                df = df.copy()
                df.columns = df.columns.get_level_values(0)
            close_col = "Close" if "Close" in df.columns else df.columns[0]
            v = float(df[close_col].iloc[-1])
            if v > 800:
                return v
    except Exception as e:
        log.debug("USDKRW 추정 실패: %s", e)
    return 1350.0


def should_include_coin_balance_row(b: dict) -> bool:
    """
    GUI·동기화·매매 루프 공통: 표시통화·먼지 제외.

    기본 ``config.coin_min_notional_usd``(달러) 미만 명목은 제외(현재가 조회 실패 시 수량 폴백).
    """
    cur = str(b.get("currency") or "").upper()
    if cur in ("KRW", "VTHO"):
        return True
    if coin_config.is_binance() and cur == "USDT":
        return True
    t = held_ticker_row(b)
    if not t:
        return False
    qty = _float_bal(b.get("balance"))
    min_usd = float(coin_config.get("coin_min_notional_usd") or 1.0)
    px = get_current_price(t)
    return coin_holding_meets_min_notional(
        qty,
        px,
        is_binance=coin_config.is_binance(),
        min_usd=min_usd,
        krw_per_usdt=get_krw_per_usdt(),
    )


def get_balances() -> list[dict[str, Any]]:
    if coin_config.is_binance():
        raw = binance_api.get_balances_like_upbit()
    elif upbit_api.upbit is None:
        return []
    else:
        raw = upbit_api.upbit.get_balances() or []
    return [b for b in raw if should_include_coin_balance_row(b)]


def quote_symbol() -> str:
    return "USDT" if coin_config.is_binance() else "KRW"


def _float_bal(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def quote_spendable(balances: list | None) -> float:
    """주문 가능 **표시 통화**(KRW 또는 USDT)."""
    q = quote_symbol()
    for b in balances or []:
        if str(b.get("currency", "")).upper() == q:
            total = _float_bal(b.get("balance"))
            locked = _float_bal(b.get("locked"))
            return max(0.0, total - locked)
    return 0.0


def get_quote_balance_direct() -> float | None:
    """거래소 API 직접 조회(주문 직전 가용)."""
    try:
        if coin_config.is_binance():
            ex = binance_api.ensure_exchange()
            bal = ex.fetch_balance()
            us = bal.get("USDT") or {}
            return float(us.get("free") or 0.0)
        if upbit_api.upbit is None:
            return None
        return upbit_api.upbit.get_balance("KRW")
    except Exception:
        return None


def get_current_price(internal_ticker: str) -> float | None:
    try:
        if coin_config.is_binance():
            p = binance_api.fetch_last_price(internal_ticker)
            return float(p) if p and p > 0 else None
        import pyupbit

        if upbit_api.upbit is None:
            return None
        p = pyupbit.get_current_price(str(internal_ticker))
        return float(p) if p and p > 0 else None
    except Exception:
        return None


def fetch_ohlcv(internal_ticker: str, interval_key: str, count: int) -> list[dict[str, float]]:
    """
    interval_key: 업비트 스타일 ``minute15``, ``day`` 또는 CCXT ``15m``, ``1d``.
    """
    ik = str(interval_key or "").strip().lower()
    if coin_config.is_binance():
        tf = "15m" if ik in ("minute15", "15m") else ("1d" if ik in ("day", "1d", "daily") else "1d")
        return binance_api.fetch_ohlcv_to_dict_list(internal_ticker, tf, int(count))
    import pyupbit

    interval = "minute15" if ik in ("minute15", "15m") else "day"
    df = pyupbit.get_ohlcv(str(internal_ticker), interval=interval, count=int(count))
    if df is None or df.empty:
        return []
    rows: list[dict[str, float]] = []
    for _, r in df.iterrows():
        rows.append(
            {
                "o": float(r["open"]),
                "h": float(r["high"]),
                "l": float(r["low"]),
                "c": float(r["close"]),
                "v": float(r["volume"]),
            }
        )
    return rows


def orderbook_summary(internal_ticker: str) -> dict[str, float]:
    if coin_config.is_binance():
        return binance_api.fetch_order_book_summary(internal_ticker)
    import pyupbit

    try:
        ob = pyupbit.get_orderbook(str(internal_ticker))
        units = []
        if isinstance(ob, list) and ob:
            units = ob[0].get("orderbook_units", [])
        elif isinstance(ob, dict):
            units = ob.get("orderbook_units", [])
        bid_total = 0.0
        ask_total = 0.0
        for u in units or []:
            bid_total += float(u.get("bid_size", 0.0))
            ask_total += float(u.get("ask_size", 0.0))
        return {"bid_size_total": bid_total, "ask_size_total": ask_total}
    except Exception:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}


def sell_market(internal_ticker: str, qty: float):
    if coin_config.is_binance():
        cp = float(get_current_price(internal_ticker) or 0.0)
        q2, err = binance_api.clamp_qty_and_check_min_notional(internal_ticker, float(qty), cp)
        if err:
            log.warning("[BINANCE SELL] %s 스킵: %s", internal_ticker, err)
            return None
        order = binance_api.market_sell_base(internal_ticker, q2)
        avg, filled = binance_api.order_avg_fill_usdt(order)
        tot = avg * filled if avg > 0 and filled > 0 else 0.0
        print(
            f"  [BINANCE SELL] {internal_ticker} | Qty: {q2} | Price: ~{avg:.8f} USDT | Total: ~{tot:.4f} USDT"
        )
        return order
    if upbit_api.upbit is None:
        return None
    return upbit_api.upbit.sell_market_order(internal_ticker, qty)


def buy_market_budget_krw(internal_ticker: str, budget_krw: float) -> Any:
    """
    시장가 매수. 업비트: KRW 금액. 바이낸스: ``budget_krw / krw_per_usdt`` 만큼 USDT 매수.
    """
    if not coin_config.is_binance():
        if upbit_api.upbit is None:
            return None
        return upbit_api.upbit.buy_market_order(internal_ticker, int(max(0, budget_krw)))

    rate = get_krw_per_usdt()
    spend_usdt = float(budget_krw) / float(rate)
    if spend_usdt < binance_api.min_cost_usdt():
        log.warning(
            "[BINANCE BUY] %s 스킵: USDT %.4f < 최소 %.4f",
            internal_ticker,
            spend_usdt,
            binance_api.min_cost_usdt(),
        )
        return None
    order = binance_api.market_buy_usdt(internal_ticker, spend_usdt)
    return order


def held_ticker_row(b: dict) -> str | None:
    """balance row → 장부 티커 ``KRW-XXX`` 또는 ``USDT-XXX``."""
    cur = str(b.get("currency", "") or "").upper()
    if not cur or cur in ("KRW", "VTHO"):
        return None
    if coin_config.is_binance():
        if cur == "USDT":
            return None
        return f"USDT-{cur}"
    return f"KRW-{cur}"


def min_order_budget_krw() -> float:
    """매수 최소 예산(원화 환산)."""
    if coin_config.is_binance():
        return float(binance_api.min_cost_usdt()) * float(get_krw_per_usdt())
    return 5000.0


def scale_out_min_notional_ok(sell_qty: float, curr_p: float) -> bool:
    """분할 익절 매도분 최소 명목 검사."""
    if sell_qty <= 0 or curr_p <= 0:
        return False
    if coin_config.is_binance():
        notion_usdt = float(sell_qty) * float(curr_p)
        return notion_usdt + 1e-12 >= float(binance_api.min_cost_usdt())
    return float(sell_qty) * float(curr_p) >= 5000.0


async def prefetch_daily_ohlcv_many(internal_tickers: list[str], limit: int = 250) -> dict[str, list[dict[str, float]]]:
    """스캔 다종목 일봉 선조회(바이낸스). 업비트는 호출부에서 단건 유지 가능."""
    if not coin_config.is_binance():
        return {}
    chunks = await binance_api.async_fetch_ohlcv_many(internal_tickers, "1d", limit)
    out: dict[str, list[dict[str, float]]] = {}
    for i, t in enumerate(internal_tickers):
        if i < len(chunks):
            out[t] = chunks[i]
    return out


def run_prefetch_daily_sync(tickers: list[str], limit: int = 250) -> dict[str, list[dict[str, float]]]:
    if not tickers:
        return {}
    try:
        return asyncio.run(prefetch_daily_ohlcv_many(tickers, limit))
    except RuntimeError:
        return {}
