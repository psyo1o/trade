# -*- coding: utf-8 -*-
"""국장 스윙 보유 종목 매도선 역산 (시간가중 on/off 비교)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_bot
from execution.guard import load_state
from strategy.rules import (
    SWING_TIME_DECAY_GAP_CLOSE_PER_24H,
    SWING_TIME_DECAY_START_TRADING_HOURS,
    _apply_swing_time_decaying_stop,
    _swing_base_hard_stop_floor,
    get_swing_exit_display_price,
    get_swing_hard_stop_floor,
    get_swing_profit_lock_floor,
)

STATE = ROOT / "bot_state.json"


def main() -> None:
    st = load_state(STATE)
    pos_map = st.get("positions") or {}
    kr = {k: v for k, v in pos_map.items() if str(k).isdigit()}

    if not kr:
        print("국장(숫자 티커) 보유 없음")
        return

    for t, p in kr.items():
        m = "KR"
        buy_p = float(p.get("buy_p", 0))
        sl_stored = float(p.get("sl_p", 0))
        max_p = float(p.get("max_p", buy_p))
        curr_p = float(p.get("curr_p", buy_p) or buy_p)
        _, cal_h, trading_h, buy_log = run_bot._compute_holding_time_info(p, m)

        ohlcv = run_bot.get_cached_ohlcv(t, broker=run_bot.kis_api.broker_kr)
        if not ohlcv:
            from strategy.rules import get_ohlcv_yfinance

            ohlcv = get_ohlcv_yfinance(t) or []

        if not ohlcv or len(ohlcv) < 60:
            print(f"{t}: OHLCV 부족 ({len(ohlcv) if ohlcv else 0}봉)")
            continue

        base = _swing_base_hard_stop_floor(p, ohlcv, market=m, ticker=t)
        hard_no = get_swing_hard_stop_floor(
            p, ohlcv, market=m, ticker=t, trading_hours_held=None
        )
        hard_yes = get_swing_hard_stop_floor(
            p, ohlcv, market=m, ticker=t, trading_hours_held=trading_h
        )
        disp_no = get_swing_exit_display_price(
            curr_p, p, ohlcv, market=m, ticker=t, trading_hours_held=None
        )
        disp_yes = get_swing_exit_display_price(
            curr_p, p, ohlcv, market=m, ticker=t, trading_hours_held=trading_h
        )
        bot_line = run_bot._resolve_exit_display_price(
            t,
            curr_p,
            p,
            ohlcv,
            "SWING_FIB",
            state=st,
            trading_hours_held=trading_h,
        )
        decayed = _apply_swing_time_decaying_stop(buy_p, base, trading_h)
        profit_lock = get_swing_profit_lock_floor(buy_p, max_p)
        extra = max(0.0, trading_h - SWING_TIME_DECAY_START_TRADING_HOURS)
        steps = extra / 24.0
        close_pct = min(1.0, steps * SWING_TIME_DECAY_GAP_CLOSE_PER_24H)

        try:
            name = run_bot.get_kr_company_name(t)
        except Exception:
            name = t
        fib = float(p.get("entry_fib_level", 0) or 0)

        print("=" * 60)
        print(f"{name}({t}) — SWING_FIB")
        print(f"  매수일시: {buy_log}")
        print(f"  달력보유: {cal_h:.1f}h | 영업보유(trading_h): {trading_h:.1f}h")
        print(f"  평단: {buy_p:,.0f} | 현재: {curr_p:,.0f} | max_p: {max_p:,.0f}")
        print(f"  장부 sl_p(저장): {sl_stored:,.0f}")
        print(f"  entry_fib: {fib:,.0f}")
        print(f"  기술바닥(피보·구름, 조임 전): {base:,.0f}")
        print(
            f"  시간가중: {SWING_TIME_DECAY_START_TRADING_HOURS:.0f}h 후 "
            f"24h당 {SWING_TIME_DECAY_GAP_CLOSE_PER_24H*100:.0f}% "
            f"→ gap {close_pct*100:.1f}% 닫힘 (스텝 {steps:.2f})"
        )
        print(f"  조임 후 바닥: {decayed:,.0f}")
        print(f"  하드스탑 시간X / 시간O: {hard_no:,.0f} / {hard_yes:,.0f}")
        print(f"  수익락(>3%): {profit_lock:,.0f}")
        print(f"  매도선 표시 시간X / 시간O: {disp_no:,.0f} / {disp_yes:,.0f}")
        print(f"  run_bot 연동(시간O): {bot_line:,.0f}")


if __name__ == "__main__":
    main()
