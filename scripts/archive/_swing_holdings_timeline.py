# -*- coding: utf-8 -*-
"""보유 SWING_FIB 종목 — 시간가중 매도선·현재가 도달 예상 시각."""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_bot
from execution.guard import load_state
from strategy.rules import (
    BREAKEVEN_LOCK_MULT,
    SWING_PROFIT_LOCK_ACTIVATE_PCT,
    SWING_TIME_DECAY_GAP_CLOSE_PER_24H,
    SWING_TIME_DECAY_START_TRADING_HOURS,
    _apply_swing_time_decaying_stop,
    _swing_base_hard_stop_floor,
    get_swing_exit_display_price,
    get_swing_hard_stop_floor,
    get_swing_profit_lock_floor,
)

STATE = ROOT / "bot_state.json"


def _market(ticker: str) -> str:
    t = str(ticker)
    if t.startswith("USDT-") or t.startswith("KRW-"):
        return "COIN"
    if t.isdigit():
        return "KR"
    return "US"


def _name(ticker: str, market: str) -> str:
    try:
        if market == "KR":
            return run_bot.get_kr_company_name(ticker)
        if market == "US":
            return run_bot.get_us_company_name(ticker)
        return run_bot.get_coin_name(ticker)
    except Exception:
        return ticker


def _fetch_ohlcv(ticker: str, market: str):
    if market == "KR":
        o = run_bot.get_cached_ohlcv(ticker, broker=run_bot.kis_api.broker_kr)
        if not o:
            from strategy.rules import get_ohlcv_yfinance

            o = get_ohlcv_yfinance(ticker) or []
        return o
    if market == "US":
        from strategy.rules import get_ohlcv_yfinance

        return get_ohlcv_yfinance(ticker) or []
    return run_bot.coin_broker.fetch_ohlcv(ticker, "day", 250) or []


def _max_profit_pct(buy: float, max_p: float) -> float:
    if buy <= 0:
        return 0.0
    return (max_p - buy) / buy * 100.0


def _hours_to_reach_floor(
    buy: float,
    base: float,
    trading_h: float,
    target: float,
    *,
    coin: bool,
) -> float | None:
    """시간가중만으로 하드 바닥이 target 에 도달하는 추가 영업/연속 시간."""
    if buy <= 0 or base <= 0 or target <= base:
        return 0.0 if _apply_swing_time_decaying_stop(buy, base, trading_h) >= target else None
    gap = max(0.0, buy - base)
    if gap <= 0:
        return None
    cur = _apply_swing_time_decaying_stop(buy, base, trading_h)
    if cur >= target:
        return 0.0
    need_close = (target - base) / gap
    if need_close > 1.0:
        return None
    need_steps = need_close / SWING_TIME_DECAY_GAP_CLOSE_PER_24H
    need_extra_h = need_steps * 24.0
    if trading_h < SWING_TIME_DECAY_START_TRADING_HOURS:
        total_extra_from_buy = (
            SWING_TIME_DECAY_START_TRADING_HOURS - trading_h + need_extra_h
        )
    else:
        extra_now = trading_h - SWING_TIME_DECAY_START_TRADING_HOURS
        total_extra_from_buy = max(0.0, need_extra_h - extra_now)
    return total_extra_from_buy


def _project_when(trading_h: float, add_h: float, market: str) -> str:
    """대략적 도달 시각(코인=연속, 주식=영업시간 근사 1:1)."""
    if add_h <= 0:
        return "이미 도달(시간가중만 기준)"
    now = datetime.now()
    if market == "COIN":
        return (now + timedelta(hours=add_h)).strftime("%Y-%m-%d %H:%M")
    # KR/US: 영업시간 누적 ≈ 달력의 평일 비율 (~5/7) 보정은 거칠게 1.4x
    cal_h = add_h * 1.35
    return f"약 {(now + timedelta(hours=cal_h)).strftime('%Y-%m-%d %H:%M')} (영업 {add_h:.0f}h 추가 가정)"


def _fmt_price(market: str, p: float) -> str:
    if market == "US":
        return f"${p:,.2f}"
    if market == "COIN" and 0 < p < 100:
        return f"{p:,.4f}"
    if market == "COIN":
        return f"{p:,.2f}"
    return f"{p:,.0f}"


def analyze(ticker: str, pos: dict) -> None:
    m = _market(ticker)
    buy_p = float(pos.get("buy_p", 0))
    sl_stored = float(pos.get("sl_p", 0))
    max_p = float(pos.get("max_p", buy_p))
    curr_p = float(pos.get("curr_p", buy_p) or buy_p)
    _, cal_h, trading_h, buy_log = run_bot._compute_holding_time_info(pos, m)
    ohlcv = _fetch_ohlcv(ticker, m)
    if not ohlcv or len(ohlcv) < 60:
        print(f"\n{ticker}: OHLCV 부족")
        return

    base = _swing_base_hard_stop_floor(pos, ohlcv, market=m, ticker=ticker)
    hard_0 = get_swing_hard_stop_floor(
        pos, ohlcv, market=m, ticker=ticker, trading_hours_held=0.0
    )
    hard_t = get_swing_hard_stop_floor(
        pos, ohlcv, market=m, ticker=ticker, trading_hours_held=trading_h
    )
    profit_lock = get_swing_profit_lock_floor(buy_p, max_p)
    disp = get_swing_exit_display_price(
        curr_p, pos, ohlcv, market=m, ticker=ticker, trading_hours_held=trading_h
    )
    bot_line = run_bot._resolve_exit_display_price(
        ticker, curr_p, dict(pos), ohlcv, "SWING_FIB", trading_hours_held=trading_h
    )
    decayed = _apply_swing_time_decaying_stop(buy_p, base, trading_h)
    gap = max(0.0, buy_p - base)
    extra = max(0.0, trading_h - SWING_TIME_DECAY_START_TRADING_HOURS)
    steps = extra / 24.0
    close_pct = min(1.0, steps * SWING_TIME_DECAY_GAP_CLOSE_PER_24H)
    max_profit = _max_profit_pct(buy_p, max_p)

    binder = "시간가중 하드"
    if profit_lock > 0 and profit_lock >= hard_t:
        binder = "수익락(본절+0.5%) — 시간가중이 매도선에 안 보임"

    h_to_curr = _hours_to_reach_floor(
        buy_p, base, trading_h, curr_p, coin=(m == "COIN")
    )
    h_to_pl = None
    if profit_lock > hard_t:
        h_to_pl = _hours_to_reach_floor(
            buy_p, base, trading_h, profit_lock, coin=(m == "COIN")
        )

    print("=" * 72)
    print(f"{_name(ticker, m)} ({ticker}) — SWING_FIB [{m}]")
    print(f"  매수: {buy_log} | 달력 {cal_h:.1f}h | 영업/연속 {trading_h:.1f}h")
    print(
        f"  평단 {_fmt_price(m, buy_p)} | 현재 {_fmt_price(m, curr_p)} "
        f"({(curr_p - buy_p) / buy_p * 100:+.2f}%) | max {_fmt_price(m, max_p)} (최고수익 {max_profit:.2f}%)"
    )
    print(f"  장부 sl_p: {_fmt_price(m, sl_stored)} | 엔진 매도선: {_fmt_price(m, bot_line)}")
    print(f"  기술바닥(조임 전): {_fmt_price(m, base)} | gap(평단−바닥): {_fmt_price(m, gap)}")
    print(
        f"  시간가중: {SWING_TIME_DECAY_START_TRADING_HOURS:.0f}h 후 "
        f"24h당 {SWING_TIME_DECAY_GAP_CLOSE_PER_24H*100:.0f}% "
        f"→ 지금 gap {close_pct*100:.1f}% 닫힘 (스텝 {steps:.2f})"
    )
    print(
        f"  하드: 시간0={_fmt_price(m, hard_0)} → 지금={_fmt_price(m, hard_t)} "
        f"(순수조임 { _fmt_price(m, decayed)})"
    )
    print(
        f"  수익락(>{SWING_PROFIT_LOCK_ACTIVATE_PCT:.0f}%): "
        f"{_fmt_price(m, profit_lock) if profit_lock > 0 else '미발동'} "
        f"(평단×{BREAKEVEN_LOCK_MULT})"
    )
    print(f"  ★ 실제 매도선 = {_fmt_price(m, disp)}  ← 지배: {binder}")

    if profit_lock > 0 and profit_lock >= hard_t:
        delta = profit_lock - hard_t
        print(
            f"  ⚠ 매도선이 수익락에 고정됨 — 시간가중으로 하드가 { _fmt_price(m, delta)} "
            f"더 올라야 매도선이 움직임"
        )

    if curr_p > disp:
        print(f"  현재가가 매도선 위 — 이탈 시 수익락/하드 청산 판정 구간")
    elif curr_p < disp:
        margin = (curr_p - disp) / disp * 100 if disp else 0
        print(f"  현재가 vs 매도선: 매도선이 {abs(margin):.2f}% {'위' if margin < 0 else '아래'}")

    if h_to_curr is not None:
        print(
            f"  [가정: 가격 고정] 시간가중 하드만 현재가까지: "
            f"영업/연속 +{h_to_curr:.1f}h → {_project_when(trading_h, h_to_curr, m)}"
        )
    else:
        print("  [가정: 가격 고정] 시간가중만으로는 현재가에 도달 불가(평단 상한 또는 gap=0)")

    if h_to_pl is not None and h_to_pl > 0:
        print(
            f"  [가정: 가격 고정] 시간가중이 수익락선까지: +{h_to_pl:.1f}h → "
            f"{_project_when(trading_h, h_to_pl, m)}"
        )

    # 다음 24h 스텝마다 하드 예측
    print("  --- 향후 시간가중 하드 (가격·max_p 고정) ---")
    for add in (0, 24, 48, 72, 96, 120):
        th2 = trading_h + add
        h2 = _apply_swing_time_decaying_stop(buy_p, base, th2)
        d2 = max(h2, profit_lock) if profit_lock > 0 else h2
        tag = "←지금" if add == 0 else f"+{add}h"
        print(
            f"    영업/연속 {th2:6.0f}h {tag:6s} "
            f"하드 {_fmt_price(m, h2)} | 표시매도선 {_fmt_price(m, d2)}"
        )


def main() -> None:
    st = load_state(STATE)
    pos_map = st.get("positions") or {}
    swings = {
        k: v
        for k, v in pos_map.items()
        if str(v.get("strategy_type", v.get("tier", ""))).upper() in ("SWING_FIB", "SWING")
    }
    if not swings:
        print("SWING_FIB 보유 없음")
        return
    print(f"기준 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"정책: {SWING_TIME_DECAY_START_TRADING_HOURS:.0f}h 후 "
        f"24h당 gap {SWING_TIME_DECAY_GAP_CLOSE_PER_24H*100:.0f}% 상향"
    )
    for t, p in sorted(swings.items()):
        analyze(t, p)


if __name__ == "__main__":
    main()
