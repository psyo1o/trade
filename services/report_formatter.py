# -*- coding: utf-8 -*-
"""텔레그램·로그용 문구 조립 — 트레이딩 로직 없음."""
from __future__ import annotations


def format_hold_roi(roi) -> str:
    if roi is None:
        return "보유없음"
    return f"{float(roi):+.2f}%"


def format_holdings_block(lines: list[str], *, empty_label: str = "  (보유 없음)") -> str:
    return "\n".join(lines) if lines else empty_label


def format_coin_summary_line(
    *,
    weather_coin: str,
    krw_bal: int,
    coin_total: int,
    coin_roi_str: str,
    coin_display_fallback: bool,
    is_binance: bool,
    binance_cash_total_usdt,
) -> str:
    if is_binance:
        if coin_display_fallback:
            from api import coin_broker

            kpx = float(coin_broker.get_krw_per_usdt() or 0.0) or 1.0
            cash_u = float(krw_bal) / kpx
            tot_u = float(coin_total) / kpx
        else:
            try:
                cash_u, tot_u = binance_cash_total_usdt()
            except Exception:
                from api import coin_broker

                kpx = float(coin_broker.get_krw_per_usdt() or 0.0) or 1.0
                cash_u = float(krw_bal) / kpx
                tot_u = float(coin_total) / kpx
        return (
            f"{weather_coin} 🪙 코인 | 예수금: {cash_u:,.2f} USDT | "
            f"총평가: {tot_u:,.2f} USDT | 수익률: {coin_roi_str}"
        )
    return (
        f"{weather_coin} 🪙 코인 | 예수금: {int(krw_bal):,}원 | "
        f"총평가: {int(coin_total):,}원 | 수익률: {coin_roi_str}"
    )


def format_survival_telegram_message(
    snap: dict,
    *,
    weekend_kis_suppress: bool,
) -> str:
    """30분 생존신고 텔레그램 본문."""
    weather = snap["weather"]
    labels = snap.get("labels") or {}
    kr = labels.get("kr") or {}
    us = labels.get("us") or {}
    coin = labels.get("coin") or {}

    kr_cash = int(kr.get("cash", 0))
    kr_total = int(kr.get("total", 0))
    us_cash = float(us.get("cash", 0))
    us_total = float(us.get("total", 0))
    krw_bal = int(coin.get("cash", 0))
    coin_total = int(coin.get("total", 0))

    kr_roi_str = format_hold_roi(kr.get("roi"))
    us_roi_str = format_hold_roi(us.get("roi"))
    coin_roi_str = format_hold_roi(coin.get("roi"))

    holdings = snap.get("holdings") or {}
    kr_holdings_str = format_holdings_block(list(holdings.get("kr") or []))
    us_holdings_str = format_holdings_block(list(holdings.get("us") or []))
    coin_holdings_str = format_holdings_block(list(holdings.get("coin") or []))

    from api import coin_broker, coin_config

    coin_summary_line = format_coin_summary_line(
        weather_coin=str(weather.get("COIN", "")),
        krw_bal=krw_bal,
        coin_total=coin_total,
        coin_roi_str=coin_roi_str,
        coin_display_fallback=bool(coin.get("display_fallback")),
        is_binance=bool(coin_config.is_binance()),
        binance_cash_total_usdt=coin_broker.binance_display_cash_and_total_usdt,
    )

    msg = f"""💓 [3콤보 생존신고]
{weather['KR']} 🇰🇷 국장 | 예수금: {kr_cash:,}원 | 총평가: {kr_total:,}원 | 수익률: {kr_roi_str}
[국장 보유]
{kr_holdings_str}

{weather['US']} 🇺🇸 미장 | 예수금: ${us_cash:,.2f} | 총평가: ${us_total:,.2f} | 수익률: {us_roi_str}
[미장 보유]
{us_holdings_str}

{coin_summary_line}
[코인 보유]
{coin_holdings_str}"""

    if weekend_kis_suppress:
        sat = str(snap.get("snapshot_saved_at") or "").strip()
        if sat:
            msg += f"\n📌 국·미 평가는 저장된 직전 조회({sat}) 기준입니다."
    return msg
