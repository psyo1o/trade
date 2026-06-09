# -*- coding: utf-8 -*-
"""
하락장 헷지(안전자산) 유니버스 — **티커 하드코딩 단일 출처**.

grep 키워드
    ``HEDGE_UNIVERSE``, ``하락장 헷지``, ``hedge_tickers``, ``HEDGE_TICKERS``

수정 방법
    아래 ``HEDGE_ASSETS_KR`` / ``HEDGE_ASSETS_US`` / ``HEDGE_ASSETS_COIN`` 튜플만 고치면 됩니다.
    ``run_bot._run_kr_buy_cycle`` / ``_run_us_buy_cycle`` / ``coin_buy_cycle`` 이 자동 반영합니다.

연동 규칙 (``run_bot.py``)
    * 매수 후보에 헷지 티커 **무조건 포함** (``_merge_hedge_into_buy_targets``)
    * Phase4 ``market_buy_allowed == false`` → 일반 주식 제거, **헷지만** 매수 검토
    * ``MAX_POSITIONS`` 슬롯 **우회** (예수금·``portfolio_heat_max_pct`` 는 그대로)
    * Phase3 AI 필터 **생략** (``false_breakout_prob = 0``)

문서
    ``docs/HEDGE_UNIVERSE.md``, ``README.md`` Phase4·헷지 절
"""
from __future__ import annotations

from typing import NamedTuple


class HedgeAsset(NamedTuple):
    """종목코드 + 한글명(로그·주석용)."""

    code: str
    name_ko: str


# ---------------------------------------------------------------------------
# ★ 하락장 헷지 티커 — 여기만 수정 (grep: HEDGE_ASSETS)
# ---------------------------------------------------------------------------

HEDGE_ASSETS_KR: tuple[HedgeAsset, ...] = (
    # (코드,   한글명)
    HedgeAsset("261240", "KODEX 미국달러선물"),            # 달러 강세·환율 헷지
    HedgeAsset("411060", "ACE KRX금현물"),                 # 금 현물 추종
    HedgeAsset("304660", "KODEX 미국30년국채울트라선물(H)"),  # 장기 국채 선물(H)
)

HEDGE_ASSETS_US: tuple[HedgeAsset, ...] = (
    HedgeAsset("GLD", "SPDR Gold Shares"),                        # 금
    HedgeAsset("TLT", "iShares 20+ Year Treasury Bond ETF"),      # 장기 미국채
    HedgeAsset("UUP", "Invesco DB US Dollar Index Bullish Fund"),  # 달러 강세
)

# 바이낸스 USDT·업비트 KRW 마켓 — 베이스 심볼 (장부 키는 USDT-PAXG / KRW-PAXG)
HEDGE_ASSETS_COIN: tuple[HedgeAsset, ...] = (
    HedgeAsset("PAXG", "Paxos Gold (PAXG)"),
    HedgeAsset("XAUT", "Tether Gold (XAUT)"),
)

# 매수 루프·RS 정렬용 코드 리스트 (순서 = 위 튜플 순서)
HEDGE_TICKERS_KR: list[str] = [a.code for a in HEDGE_ASSETS_KR]
HEDGE_TICKERS_US: list[str] = [a.code for a in HEDGE_ASSETS_US]
HEDGE_TICKERS_COIN: list[str] = [a.code for a in HEDGE_ASSETS_COIN]


def coin_hedge_internal_tickers(*, is_binance: bool) -> list[str]:
    """거래소별 장부 티커 — ``USDT-PAXG`` 또는 ``KRW-PAXG``."""
    prefix = "USDT-" if is_binance else "KRW-"
    return [f"{prefix}{code}" for code in HEDGE_TICKERS_COIN]


def coin_hedge_base_from_internal(ticker: str) -> str:
    """``USDT-PAXG`` / ``PAXG`` → ``PAXG``."""
    t = str(ticker or "").strip().upper()
    if t.startswith("USDT-"):
        return t[5:]
    if t.startswith("KRW-"):
        return t[4:]
    return t


def is_coin_hedge_internal_ticker(ticker: str) -> bool:
    base = coin_hedge_base_from_internal(ticker)
    return base in {c.upper() for c in HEDGE_TICKERS_COIN}


def hedge_assets_for_market(market: str) -> tuple[HedgeAsset, ...]:
    mk = str(market or "").strip().upper()
    if mk == "KR":
        return HEDGE_ASSETS_KR
    if mk == "US":
        return HEDGE_ASSETS_US
    if mk == "COIN":
        return HEDGE_ASSETS_COIN
    return ()


def hedge_tickers_for_market(market: str) -> list[str]:
    mk = str(market or "").strip().upper()
    if mk == "COIN":
        return list(HEDGE_TICKERS_COIN)
    return [a.code for a in hedge_assets_for_market(market)]


def hedge_asset_label(ticker: str, market: str) -> str:
    """로그용 ``코드(한글명)`` — 미등록 코드는 코드만."""
    mk = str(market or "").strip().upper()
    code = str(ticker or "").strip().upper()
    if mk == "COIN":
        code = coin_hedge_base_from_internal(code)
    for asset in hedge_assets_for_market(market):
        if asset.code.upper() == code or asset.code == str(ticker or "").strip():
            return f"{asset.code}({asset.name_ko})"
    return code


def format_hedge_universe_summary(market: str) -> str:
    """한 줄 요약 — 사이클 로그·문서용."""
    parts = [f"{a.code}={a.name_ko}" for a in hedge_assets_for_market(market)]
    return ", ".join(parts) if parts else "(없음)"
