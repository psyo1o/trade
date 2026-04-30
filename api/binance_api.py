# -*- coding: utf-8 -*-
"""
바이낸스 현물(Spot) — CCXT.

설정 (``config.json``)
    * ``binance_access``, ``binance_secret``
    * ``binance_min_cost_usdt`` — 최소 주문 명목(USDT), 기본 10
    * ``krw_per_usdt`` — 선택; 없으면 시세 추정
    * ``binance_recv_window`` — 선택

수수료 BNB 차감은 **바이낸스 웹 계정 설정**(BNB로 수수료 결제)에서 켜두면 자동 반영된다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import ccxt  # type: ignore

exchange: ccxt.binance | None = None
_cfg: dict = {}

log = logging.getLogger(__name__)


def init_binance(config: dict) -> ccxt.binance:
    global exchange, _cfg
    _cfg = dict(config or {})
    api_key = str(_cfg.get("binance_access") or "").strip()
    secret = str(_cfg.get("binance_secret") or "").strip()
    opts: dict[str, Any] = {
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    rw = int(_cfg.get("binance_recv_window") or 60000)
    if rw > 0:
        opts["recvWindow"] = rw
    exchange = ccxt.binance(opts)
    exchange.load_markets()
    log.info("바이낸스 현물 마켓 로드 완료 (%s개)", len(exchange.markets))
    return exchange


def ensure_exchange() -> ccxt.binance:
    if exchange is None:
        raise RuntimeError("binance_api.init_binance(config) 가 아직 호출되지 않았습니다.")
    return exchange


def internal_to_ccxt(symbol: str) -> str:
    """``USDT-BTC`` → ``BTC/USDT``."""
    s = str(symbol or "").strip().upper()
    if s.startswith("USDT-"):
        base = s.split("-", 1)[1].strip()
        if not base:
            raise ValueError(symbol)
        return f"{base}/USDT"
    raise ValueError(f"바이낸스 티커 형식 아님: {symbol}")


def ccxt_symbol_to_internal(sym: str) -> str:
    """``BTC/USDT`` → ``USDT-BTC``."""
    s = str(sym or "").strip().upper()
    if s.endswith("/USDT"):
        base = s.replace("/USDT", "").strip()
        return f"USDT-{base}"
    raise ValueError(sym)


def min_cost_usdt() -> float:
    return float(_cfg.get("binance_min_cost_usdt", 10.0) or 10.0)


def get_balances_like_upbit() -> list[dict[str, Any]]:
    """
    업비트 ``get_balances()`` 와 유사한 리스트.

    * ``currency``, ``balance``, ``locked``, ``avg_buy_price`` (없으면 '0')
    """
    ex = ensure_exchange()
    bal = ex.fetch_balance()
    out: list[dict[str, Any]] = []
    totals = bal.get("total") or {}
    for cur, total_amt in totals.items():
        if total_amt is None:
            continue
        try:
            t = float(total_amt)
        except (TypeError, ValueError):
            continue
        if t <= 0 and (bal.get(cur) or {}).get("free", 0) in (None, 0):
            continue
        free = float((bal.get(cur) or {}).get("free") or 0)
        used = float((bal.get(cur) or {}).get("used") or 0)
        out.append(
            {
                "currency": cur,
                "balance": str(free + used),
                "locked": str(used),
                "avg_buy_price": "0",
            }
        )
    return out


def fetch_last_price(internal_ticker: str) -> float:
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    t = ex.fetch_ticker(sym)
    last = t.get("last") or t.get("close")
    return float(last or 0.0)


def fetch_ohlcv_to_dict_list(internal_ticker: str, timeframe: str, limit: int) -> list[dict[str, float]]:
    """timeframe: ccxt 규격 ``15m``, ``1d`` 등."""
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    rows = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=int(limit))
    out: list[dict[str, float]] = []
    for ohlcv in rows:
        out.append(
            {
                "o": float(ohlcv[1]),
                "h": float(ohlcv[2]),
                "l": float(ohlcv[3]),
                "c": float(ohlcv[4]),
                "v": float(ohlcv[5]),
            }
        )
    return out


def fetch_order_book_summary(internal_ticker: str) -> dict[str, float]:
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    ob = ex.fetch_order_book(sym, limit=50)
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    bid_total = sum(float(x[1]) for x in bids[:20])
    ask_total = sum(float(x[1]) for x in asks[:20])
    return {"bid_size_total": bid_total, "ask_size_total": ask_total}


def clamp_qty_and_check_min_notional(internal_ticker: str, qty: float, price: float) -> tuple[float, str | None]:
    """수량 정밀도 반올림 + 최소 명목(USDT) 검사. 불가 시 (0, reason)."""
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    q = float(ex.amount_to_precision(sym, qty))
    p = float(price or 0.0)
    if q <= 0:
        return 0.0, "수량 0"
    notional = q * p
    mkt = ex.market(sym)
    min_cost = None
    try:
        lim = (mkt.get("limits") or {}).get("cost") or {}
        min_cost = lim.get("min")
    except Exception:
        min_cost = None
    mc = float(min_cost) if min_cost is not None else min_cost_usdt()
    if notional + 1e-12 < mc:
        return 0.0, f"명목 {notional:.4f} USDT < 최소 {mc}"
    return q, None


def market_buy_usdt(internal_ticker: str, spend_usdt: float) -> dict[str, Any]:
    """
    시장가 매수(지출 USDT). 바이낸스는 ``quoteOrderQty`` 로 USDT 금액 매수.
    """
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    spend = float(spend_usdt)
    if spend <= 0:
        raise ValueError("spend_usdt<=0")
    spend_adj = float(ex.cost_to_precision(sym, spend))
    try:
        order = ex.create_order(sym, "market", "buy", spend_adj, None, {"quoteOrderQty": spend_adj})
    except Exception:
        order = ex.create_market_buy_order(sym, spend_adj)
    return order if isinstance(order, dict) else {"info": order}


def market_sell_base(internal_ticker: str, qty: float) -> dict[str, Any]:
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    q = float(ex.amount_to_precision(sym, qty))
    if q <= 0:
        raise ValueError("qty<=0")
    order = ex.create_market_sell_order(sym, q)
    return order if isinstance(order, dict) else {"info": order}


def order_avg_fill_usdt(order: dict[str, Any]) -> tuple[float, float]:
    """체결 평균가(USDT), 체결 수량(base). 정보 부족 시 0."""
    try:
        filled = float(order.get("filled") or order.get("amount") or 0)
        cost = float(order.get("cost") or 0)
        if filled > 0 and cost > 0:
            return cost / filled, filled
        avg = float(order.get("average") or 0)
        if avg > 0 and filled > 0:
            return avg, filled
    except Exception:
        pass
    return 0.0, 0.0


def top_usdt_symbols_by_quote_volume(limit: int = 20) -> list[str]:
    """거래대금(quote) 상위 → 내부 티커 ``USDT-XXX``."""
    ex = ensure_exchange()
    tickers = ex.fetch_tickers()
    scored: list[tuple[float, str]] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT"):
            continue
        base = sym.split("/")[0].upper()
        if base in ("USDC", "FDUSD", "TUSD", "USDP", "DAI"):
            continue
        qv = float(t.get("quoteVolume") or t.get("quote_volume") or 0)
        scored.append((qv, sym))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    for _, sym in scored[: max(1, int(limit))]:
        try:
            out.append(ccxt_symbol_to_internal(sym))
        except Exception:
            continue
    return out


async def async_fetch_ohlcv_many(
    internal_tickers: list[str],
    timeframe: str,
    limit: int,
) -> list[list[dict[str, float]]]:
    """여러 심볼 OHLCV 병렬 조회(스레드 풀)."""

    def one(it: str) -> list[dict[str, float]]:
        try:
            return fetch_ohlcv_to_dict_list(it, timeframe, limit)
        except Exception:
            return []

    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, one, t) for t in internal_tickers]
    return await asyncio.gather(*tasks)
