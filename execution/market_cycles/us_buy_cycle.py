# -*- coding: utf-8 -*-
"""US 매수 루프 — ``run_bot._run_us_buy_cycle`` 에서 분리 (로직 동일)."""
from __future__ import annotations

import traceback
from typing import Any

from execution.market_cycles.context import TradingCycleContext


def _rb():
    import run_bot as rb

    return rb


def run_us_buy_cycle(
    ctx: TradingCycleContext,
    *,
    held_us: Any,
    us_cash: float,
    total_us_equity: float,
    alpha_target_vol: float,
) -> float:
    """미장 매수 루프 — 헷지 유니버스·Phase4·MAX_POSITIONS·AI 예외 포함."""
    rb = _rb()
    state = ctx.state
    weather = ctx.weather
    macro_mult = ctx.macro_mult
    macro_snap = ctx.macro_snap
    buy_cycle_tag = ctx.buy_cycle_tag
    hedge_only = rb._phase4_hedge_only_active(macro_snap, "US")
    buy_targets = rb._merge_hedge_into_buy_targets(rb.get_top_market_cap_tickers(150), "US")
    buy_targets = rb._apply_phase4_hedge_buy_targets(buy_targets, macro_snap, "US")
    if not buy_targets:
        if hedge_only:
            print(
                f"  -> 🚨 미장 Phase4 글로벌 방어막: 헷지 후보 없음 — 매수 중단. "
                f"({(macro_snap.get('market_buy_block_reason') or {}).get('US', '')})"
            )
        return float(us_cash)

    us_index_change = rb.get_market_index_change("US")
    print(f"  📊 [S&P500 지수] 변화율: {us_index_change:+.2f}% 날씨는 {weather['US']}")
    if us_index_change <= rb.INDEX_CRASH_US:
        if hedge_only:
            print(
                f"  📌 [US 헷지] S&P500 {us_index_change:+.2f}% 급락 — "
                f"Phase4 헷지 전용 모드, 지수 차단 예외·매수 검토 계속"
            )
        else:
            print(f"  🚫 [US 매수 중단] S&P500 {us_index_change:+.2f}% 급락 (기준: {rb.INDEX_CRASH_US}%)")
            return float(us_cash)

    if weather["US"] == rb.WEATHER_LABEL_BEAR:
        print("  📌 [US] BEAR 날씨 — V8 추세 매수만 중단, SWING_FIB 스윙 후보는 계속 분석")

    buy_targets = rb._sort_buy_targets_by_rs(buy_targets, "US")
    total_us = len(buy_targets)
    print(f"  -> 🇺🇸 미장 유니버스(고베타·섹터분산) {total_us}개 정밀 분석 시작!")

    for idx, t in enumerate(buy_targets, 1):
        try:
            us_name = rb.get_us_company_name(t)
            if rb.in_ticker_cooldown(state, t):
                print(
                    f"  ⏭️ {us_name}({t}): 매도 후 쿨다운(톱날 방지) 만료 "
                    f"{rb.ticker_cooldown_human(state, t)} 이전 (패스)"
                )
                continue
            if rb.in_cooldown(state, t):
                print(f"  ⏭️ {us_name}({t}): 쿨다운 중 (패스)")
                continue
            if t in held_us:
                print(f"  ⏭️ {us_name}({t}): 이미 보유중 (패스)")
                continue
            if rb.order_idem.is_buy_inflight(state, "US", t, buy_cycle_tag):
                print(f"  ⏭️ {us_name}({t}): 매수 TWAP 진행 중(멱등 패스)")
                continue
            sector_ok_us, sector_msg_us = rb.allow_us_sector_entry(
                t,
                state.get("positions", {}),
                rb.MAX_POSITIONS_US,
                rb.normalize_ticker,
            )
            if not sector_ok_us:
                print(f"  ⏭️ {us_name}({t}): {sector_msg_us} (패스)")
                continue

            ohlcv = rb.get_ohlcv_yfinance(t)
            if not ohlcv:
                print(f"  ⏭️ {us_name}({t}): OHLCV 데이터 부족 (패스)")
                continue

            live_px_us = 0.0
            try:
                _pr_us = rb.kis_api.broker_us.fetch_price(t)
                if _pr_us and _pr_us.get("rt_cd") == "0":
                    live_px_us = float((_pr_us.get("output") or {}).get("last", 0) or 0)
            except Exception:
                pass

            strategy_type = "TREND_V8"
            entry_fib_level = 0.0
            if hedge_only and rb._is_hedge_ticker(t, "US"):
                _ref = (
                    float(live_px_us)
                    if live_px_us > 0
                    else float(ohlcv[-1].get("c", 0) or 0)
                )
                if _ref <= 0:
                    print(f"  ⏭️ {us_name}({t}): [US 헷지] 현재가 없음 (패스)")
                    continue
                sl_p = float(_ref) * 0.95
                s_name = "HEDGE_PHASE4"
                print(
                    f"  🛡️ [HEDGE-BUY] {us_name}({t}) Phase4 방어 — "
                    f"V8/스윙·BEAR·지수급락 예외, 손절 ~${sl_p:.2f}"
                )
            else:
                entry_decision = rb.decide_entry_signals(
                    ohlcv,
                    weather["US"],
                    t,
                    us_name,
                    idx,
                    total_us,
                    market="US",
                    reference_close=live_px_us if live_px_us > 0 else None,
                )
                is_buy = entry_decision.is_buy
                sl_p = entry_decision.sl_p
                s_name = entry_decision.signal_name
                v8_ok = bool(is_buy) and rb._v8_trend_buy_allowed_in_weather(weather["US"])
                if bool(is_buy) and not v8_ok:
                    print(
                        f"  ⏭️ {us_name}({t}): BEAR 시장 — V8 신호 통과했으나 추세 매수 차단 (스윙만 허용)"
                    )
                if v8_ok:
                    print(f"  ✅ [V8-BUY] {us_name}({t}) 진입")
                else:
                    sw_ok = entry_decision.swing_ok
                    sw_fib = entry_decision.swing_fib
                    sw_why = entry_decision.swing_why
                    if sw_ok:
                        strategy_type = "SWING_FIB"
                        entry_fib_level = float(sw_fib)
                        _sw_o = float(ohlcv[-1].get("o", 0) or 0)
                        _sw_c = float(live_px_us) if live_px_us > 0 else float(ohlcv[-1].get("c", 0) or 0)
                        sl_p = rb.swing_entry_sl_p(_sw_c, sw_fib)
                        s_name = "SWING_FIB"
                        _sw_src = "KIS실시간" if live_px_us > 0 else "일봉종가"
                        print(
                            f"  ✅ [SWING-BUY] {us_name}({t}) entry_fib={entry_fib_level:.2f} "
                            f"| 양봉({_sw_src} 시가 {_sw_o:.2f} < 종가 {_sw_c:.2f})"
                            f"{' | BEAR 시장 스윙 예외' if weather['US'] == rb.WEATHER_LABEL_BEAR else ''}"
                        )
                    else:
                        _prog = f"[{idx}/{total_us}]" if total_us > 0 else ""
                        _disp = f"{us_name}({t})" if us_name and us_name != t else t
                        print(f"   🔍 [스윙] {_prog} {_disp} ❌ 패스: {sw_why}")
                        continue

            base_ratio = 1.0 / max(1, int(rb.MAX_POSITIONS_US))
            ratio, t_name = rb._position_ratio_with_vol_target(
                base_ratio,
                ohlcv,
                target_vol=alpha_target_vol,
                ticker=t,
            )

            _prospect_atr = rb.atr_pct_from_ohlcv(ohlcv, t)
            _mkt_heat, _heat_blocked = rb._portfolio_heat_snapshot(
                state,
                "US",
                total_us_equity,
                rb.get_cached_ohlcv,
                extra_weight=float(ratio),
                extra_atr_pct=float(_prospect_atr or 0.0),
            )
            if _heat_blocked:
                rb._log_portfolio_heat_block("US", _mkt_heat, prospective=True)
                continue

            target_budget = total_us_equity * ratio * macro_mult
            us_min_budget = 50.0

            if target_budget < us_min_budget:
                print(
                    f"  ⏭️ {us_name}({t}): [US 예산 부족] 배정예산 ${target_budget:.2f} < "
                    f"최소 ${us_min_budget:.0f} (총자산 ${total_us_equity:.2f}×비중·macro, 예수금 ${us_cash:.2f})"
                )
                continue
            if us_cash < us_min_budget:
                print(
                    f"  ⏭️ {us_name}({t}): [US 예수금 부족] 가용 ${us_cash:.2f} < 최소 ${us_min_budget:.0f} — 매수 불가"
                )
                continue
            if us_cash < target_budget:
                print(
                    f"  🧹 [미장 영끌 발동] {us_name}({t}): 예산(${target_budget:.2f}) 부족. "
                    f"지갑에 남은 전액(${us_cash:.2f}) 풀매수 장전!"
                )
                target_budget = us_cash
            if not rb._can_open_new_respecting_hedge_bypass(
                t, state, "US", int(rb.MAX_POSITIONS_US)
            ):
                print(f"  ⏭️ {us_name}({t}): 포지션 개수 초과 ({rb.MAX_POSITIONS_US}개) (패스)")
                continue

            curr_p = float(ohlcv[-1]["c"])
            qty = int(target_budget / curr_p) if curr_p > 0 else 0
            if qty <= 0:
                print(
                    f"  ⏭️ {us_name}({t}): [US 매수 스킵] 시그널 통과했으나 정수주 0주 — "
                    f"배정예산 ${target_budget:.2f} < 종가기준 1주(~${curr_p:.2f}) "
                    f"(총자산 ${total_us_equity:.2f}, 비중캡 후 ratio={ratio:.4f}, macro×{macro_mult:.2f}, 예수금 ${us_cash:.2f})"
                )
                continue
            if not rb._ai_false_breakout_buy_gate(
                t,
                "US",
                strategy_type,
                rb.AI_FALSE_BREAKOUT_THRESHOLD,
                f"{us_name}({t})",
            ):
                continue

            us_box = [float(us_cash)]
            entry_atr = float(rb.get_safe_atr(t, ohlcv) or 0.0)
            ok_us_buy = rb._execute_us_market_buy_twap(
                t,
                us_name,
                float(target_budget),
                curr_p,
                sl_p,
                entry_atr,
                t_name,
                s_name,
                state,
                us_box,
                strategy_type=strategy_type,
                entry_fib_level=entry_fib_level,
            )
            if not ok_us_buy:
                print(
                    f"  ⏭️ {us_name}({t}): [US 매수 미체결] 시그널·필터 통과 후 주문 없음 — "
                    f"배정 ${target_budget:.2f}, 종가 ${curr_p:.2f} (TWAP·KIS·예수 확인)"
                )
            else:
                ctx.buy_fills += 1
                rb._register_swing_risk_after_buy(state, t, ohlcv, "US")
            us_cash = float(us_box[0])
        except Exception as e:
            print(f"  ❌ [US BUY 예외] {t}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

    return float(us_cash)
