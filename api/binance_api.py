# -*- coding: utf-8 -*-
"""
바이낸스 현물(Spot) — CCXT.

설정 (``config.json``)
    * ``binance_access``, ``binance_secret``
    * ``binance_min_cost_usdt`` — 최소 주문 명목(USDT), 기본 10
    * ``krw_per_usdt`` — 선택; 없으면 시세 추정
    * ``binance_recv_window`` — 선택

수수료 BNB 차감은 **바이낸스 웹 계정 설정**(BNB로 수수료 결제)에서 켜두면 자동 반영된다.

비동기 확장: 장기적으로 ``ccxt.async_support.binance`` 전환 시 동일 시그니처의 비동기 주문 래퍼를
두면 된다. 현재 엔진·GUI는 동기 CCXT 로 단일 스레드 호환을 유지한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import ccxt  # type: ignore

from utils.helpers import ensure_binance_order_precision

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
        total_free_used = free + used
        if total_free_used <= 0:
            continue
        out.append(
            {
                "currency": cur,
                "balance": str(total_free_used),
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
    """수량·명목 보정(helpers ``ensure_binance_order_precision``) + 최소 명목(USDT) 검사."""
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    q_adj, _ = ensure_binance_order_precision(internal_ticker, float(qty), None)
    q = float(q_adj) if q_adj is not None else float(ex.amount_to_precision(sym, float(qty)))
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
    시장가 매수만 사용(지정가 없음). 지출 USDT — ``quoteOrderQty``.
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
    od = order if isinstance(order, dict) else {"info": order}
    avg, filled = order_avg_fill_usdt(od)
    tot = float(od.get("cost") or 0) or (avg * filled if avg > 0 and filled > 0 else spend_adj)
    log.info(
        "[BINANCE MARKET BUY] %s | Qty: %.8f | Total: %.4f USDT",
        internal_ticker,
        filled,
        tot,
    )
    print(
        f"[BINANCE MARKET BUY] {internal_ticker} | Qty: {filled:.8f} | Total: {tot:.4f} USDT"
    )
    return od


def market_sell_base(internal_ticker: str, qty: float) -> dict[str, Any]:
    """시장가 매도만 사용(지정가 없음). 수량은 ``ensure_binance_order_precision`` 경유."""
    ex = ensure_exchange()
    sym = internal_to_ccxt(internal_ticker)
    q_adj, _ = ensure_binance_order_precision(internal_ticker, float(qty), None)
    q = float(q_adj) if q_adj is not None else float(ex.amount_to_precision(sym, float(qty)))
    if q <= 0:
        raise ValueError("qty<=0")
    order = ex.create_market_sell_order(sym, q)
    od = order if isinstance(order, dict) else {"info": order}
    avg, filled = order_avg_fill_usdt(od)
    tot = float(od.get("cost") or 0) or (avg * filled if avg > 0 and filled > 0 else 0.0)
    log.info(
        "[BINANCE MARKET SELL] %s | Qty: %.8f | Total: %.4f USDT",
        internal_ticker,
        filled,
        tot,
    )
    print(
        f"[BINANCE MARKET SELL] {internal_ticker} | Qty: {filled:.8f} | Total: {tot:.4f} USDT"
    )
    return od


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


# 알려진 USD 페그 스테이블 — 트레이딩 유니버스에서 항상 제외.
# (변동성이 거의 0이라 우리 V8/SWING 하드스탑에 즉시 걸려 손실만 누적된다.)
USD_STABLE_DENYLIST: frozenset[str] = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "BUSD", "GUSD", "LUSD", "MIM", "FRAX", "CRVUSD", "SUSD",
    "USDD", "USDE", "USDB", "USTC", "USDX", "USDS",
    "PYUSD", "USDT0", "USR", "USD0",
})


def is_usd_pegged_ticker(ticker_data: dict, base_symbol: str) -> bool:
    """24h 가격 범위로 모르는 USD 페그/스테이블도 동적으로 감지.

    * ``last`` 가 0.97~1.03 USDT 안이고 24h 고저 스프레드가 last 의 0.5% 미만 → 페그로 간주.
    * ``last`` 가 정확히 1.0000 으로 떨어져 있는 경우(소수 4째자리 일치)도 페그 의심.
    * 두 조건 중 하나라도 충족하면 ``True``.
    """
    try:
        last = float(ticker_data.get("last") or ticker_data.get("close") or 0.0)
        high = float(ticker_data.get("high") or 0.0)
        low = float(ticker_data.get("low") or 0.0)
    except Exception:
        return False
    if last <= 0:
        return False
    if not (0.97 <= last <= 1.03):
        return False
    if high > 0 and low > 0:
        spread = (high - low) / last
        if spread < 0.005:  # 0.5% 미만 — 트레이딩 의미 없는 평탄 자산
            return True
    if abs(last - 1.0) < 0.0005:
        return True
    base = str(base_symbol or "").upper()
    if base.startswith("USD") or base.endswith("USD"):
        return True
    return False


def top_usdt_symbols_by_quote_volume(limit: int = 50) -> list[str]:
    """거래대금(quote) 상위 → 내부 티커 ``USDT-XXX``.

    USD 페그/스테이블 자산은 정적 denylist 와 24h 가격 범위 기반 동적 감지로 같이 걸러낸다
    (페그 자산은 변동성이 거의 0이라 매수 즉시 하드스탑에 걸리는 함정이라).
    """
    ex = ensure_exchange()
    tickers = ex.fetch_tickers()
    scored: list[tuple[float, str]] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT"):
            continue
        base = sym.split("/")[0].upper()
        if base in USD_STABLE_DENYLIST:
            continue
        if is_usd_pegged_ticker(t, base):
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
