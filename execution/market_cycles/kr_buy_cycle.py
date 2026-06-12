# -*- coding: utf-8 -*-
"""KR 매수 루프 — ``run_bot._run_kr_buy_cycle`` 에서 분리 (로직 동일)."""
from __future__ import annotations

import traceback
from typing import Any

from execution.market_cycles.context import TradingCycleContext


def _rb():
    import run_bot as rb

    return rb


def run_kr_buy_cycle(
    ctx: TradingCycleContext,
    *,
    held_kr: Any,
    kr_cash: int,
    total_kr_equity: float,
    alpha_target_vol: float,
) -> int:
    """국장 매수 루프 — 헷지 유니버스·Phase4·MAX_POSITIONS·AI 예외 포함."""
    rb = _rb()
    state = ctx.state
    weather = ctx.weather
    macro_mult = ctx.macro_mult
    macro_snap = ctx.macro_snap
    buy_cycle_tag = ctx.buy_cycle_tag
    hedge_only = rb._phase4_hedge_only_active(macro_snap, "KR")
    buy_targets = rb._apply_phase4_hedge_buy_targets(
        rb._merge_hedge_into_buy_targets(ctx.final_targets_kr, "KR"),
        macro_snap,
        "KR",
    )
    if not buy_targets:
        if hedge_only:
            print(
                f"  -> 🚨 국장 Phase4 글로벌 방어막: 헷지 후보 없음 — 매수 중단. "
                f"({(macro_snap.get('market_buy_block_reason') or {}).get('KR', '')})"
            )
        return int(kr_cash)

    kr_index_change = rb.get_market_index_change("KR")
    print(f"  📊 [KOSPI 지수] 변화율: {kr_index_change:+.2f}% 날씨는 {weather['KR']}")
    if kr_index_change <= rb.INDEX_CRASH_KR:
        if hedge_only:
            print(
                f"  📌 [KR 헷지] KOSPI {kr_index_change:+.2f}% 급락 — "
                f"Phase4 헷지 전용 모드, 지수 차단 예외·매수 검토 계속"
            )
        else:
            print(f"  🚫 [KR 매수 중단] KOSPI {kr_index_change:+.2f}% 급락 (기준: {rb.INDEX_CRASH_KR}%)")
            return int(kr_cash)

    if weather["KR"] == rb.WEATHER_LABEL_BEAR:
        print("  📌 [KR] BEAR 날씨 — V8·SWING_FIB 일반 종목 매수 중단 (헷지만 검토)")

    total_kr = len(buy_targets)
    print(f"  -> 🇰🇷 국장 사냥감 {total_kr}개 정밀 분석 시작!")
    for idx, t in enumerate(buy_targets, 1):
        kr_name = rb.get_kr_company_name(t)
        if rb.in_ticker_cooldown(state, t):
            print(
                f"  ⏭️ {kr_name}({t}): 매도 후 쿨다운(톱날 방지) 만료 "
                f"{rb.ticker_cooldown_human(state, t)} 이전 (패스)"
            )
            continue
        if rb.in_cooldown(state, t):
            print(f"  ⏭️ {kr_name}({t}): 쿨다운 중 (패스)")
            continue
        if t in held_kr:
            print(f"  ⏭️ {kr_name}({t}): 이미 보유중 (패스)")
            continue
        if rb.order_idem.is_buy_inflight(state, "KR", t, buy_cycle_tag):
            print(f"  ⏭️ {kr_name}({t}): 매수 TWAP 진행 중(멱등 패스)")
            continue
        sector_ok_kr, sector_msg_kr = rb.allow_kr_sector_entry(
            t,
            state.get("positions", {}),
            rb.MAX_POSITIONS_KR,
            rb.normalize_ticker,
        )
        if not sector_ok_kr:
            print(f"  ⏭️ {kr_name}({t}): {sector_msg_kr} (패스)")
            continue

        try:
            ohlcv_200 = rb.get_cached_ohlcv(t, broker=rb.kis_api.broker_kr)
            if not ohlcv_200 or len(ohlcv_200) < 60:
                print(
                    f"  ⏭️ {kr_name}({t}): OHLCV 부족 "
                    f"({len(ohlcv_200) if ohlcv_200 else 0}봉, 스윙 60봉 필요) (패스)"
                )
                continue

            live_px_kr = 0.0
            try:
                _pr = rb.kis_api.broker_kr.fetch_price(t)
                if _pr and _pr.get("rt_cd") == "0":
                    live_px_kr = float((_pr.get("output") or {}).get("stck_prpr", 0) or 0)
            except Exception:
                pass

            strategy_type = "TREND_V8"
            entry_fib_level = 0.0
            if hedge_only and rb._is_hedge_ticker(t, "KR"):
                _ref = (
                    float(live_px_kr)
                    if live_px_kr > 0
                    else float(ohlcv_200[-1].get("c", 0) or 0)
                )
                if _ref <= 0:
                    print(f"  ⏭️ {kr_name}({t}): [KR 헷지] 현재가 없음 (패스)")
                    continue
                sl_p = float(_ref) * 0.95
                s_name = "HEDGE_PHASE4"
                print(
                    f"  🛡️ [HEDGE-BUY] {kr_name}({t}) Phase4 방어 — "
                    f"V8/스윙·BEAR·지수급락 예외, 손절 ~{int(sl_p):,}원"
                )
            else:
                entry_decision = rb.decide_entry_signals(
                    ohlcv_200,
                    weather["KR"],
                    t,
                    kr_name,
                    idx,
                    total_kr,
                    market="KR",
                    reference_close=live_px_kr if live_px_kr > 0 else None,
                )
                is_buy = entry_decision.is_buy
                sl_p = entry_decision.sl_p
                s_name = entry_decision.signal_name
                v8_ok = bool(is_buy) and rb._v8_trend_buy_allowed_in_weather(weather["KR"])
                if bool(is_buy) and not v8_ok:
                    print(
                        f"  ⏭️ {kr_name}({t}): BEAR 시장 — V8 추세 매수 차단"
                    )
                if v8_ok:
                    print(f"  ✅ [V8-BUY] {kr_name}({t}) 진입")
                else:
                    sw_ok = entry_decision.swing_ok
                    sw_fib = entry_decision.swing_fib
                    sw_why = entry_decision.swing_why
                    if sw_ok and not rb._swing_fib_buy_allowed_in_weather(weather["KR"]):
                        print(
                            f"  ⏭️ {kr_name}({t}): BEAR 시장 — SWING_FIB 눌림목 매수 차단 (헷지만 허용)"
                        )
                        continue
                    if sw_ok:
                        strategy_type = "SWING_FIB"
                        entry_fib_level = float(sw_fib)
                        _sw_o = float(ohlcv_200[-1].get("o", 0) or 0)
                        _sw_c = (
                            float(live_px_kr)
                            if live_px_kr > 0
                            else float(ohlcv_200[-1].get("c", 0) or 0)
                        )
                        sl_p = rb.swing_entry_sl_p(_sw_c, sw_fib)
                        s_name = "SWING_FIB"
                        _sw_src = "KIS실시간" if live_px_kr > 0 else "일봉종가"
                        print(
                            f"  ✅ [SWING-BUY] {kr_name}({t}) entry_fib={entry_fib_level:,.2f} "
                            f"| 양봉({_sw_src} 시가 {_sw_o:,.0f} < 종가 {_sw_c:,.0f})"
                        )
                    else:
                        _prog = f"[{idx}/{total_kr}]" if total_kr > 0 else ""
                        _disp = f"{kr_name}({t})" if kr_name and kr_name != t else t
                        print(f"   🔍 [스윙] {_prog} {_disp} ❌ 패스: {sw_why}")
                        continue

            base_ratio = 1.0 / max(1, int(rb.MAX_POSITIONS_KR))
            ratio, t_name = rb._position_ratio_with_vol_target(
                base_ratio,
                ohlcv_200,
                target_vol=alpha_target_vol,
                ticker=t,
            )

            _prospect_atr = rb.atr_pct_from_ohlcv(ohlcv_200, t)
            _mkt_heat, _heat_blocked = rb._portfolio_heat_snapshot(
                state,
                "KR",
                total_kr_equity,
                lambda tk, _b=rb.kis_api.broker_kr: rb.get_cached_ohlcv(tk, broker=_b),
                extra_weight=float(ratio),
                extra_atr_pct=float(_prospect_atr or 0.0),
            )
            if _heat_blocked:
                rb._log_portfolio_heat_block("KR", _mkt_heat, prospective=True)
                continue

            target_budget = total_kr_equity * ratio * macro_mult
            kr_min_budget = 50000.0

            if target_budget < kr_min_budget:
                print(
                    f"  ⏭️ {kr_name}({t}): [KR 예산 부족] 배정예산 {int(target_budget):,}원 < "
                    f"최소 {int(kr_min_budget):,}원 (총자산 {int(total_kr_equity):,}원×비중·macro, 예수금 {int(kr_cash):,}원)"
                )
                continue
            if kr_cash < kr_min_budget:
                print(
                    f"  ⏭️ {kr_name}({t}): [KR 예수금 부족] 가용 {int(kr_cash):,}원 < 최소 {int(kr_min_budget):,}원 — 매수 불가"
                )
                continue
            if kr_cash < target_budget:
                print(
                    f"  🧹 [예수금 영끌 발동] {kr_name}({t}): 예산({int(target_budget):,}원) 부족. "
                    f"지갑에 남은 전액({int(kr_cash):,}원) 풀매수 장전!"
                )
                target_budget = kr_cash

            if not rb._can_open_new_respecting_hedge_bypass(
                t, state, "KR", int(rb.MAX_POSITIONS_KR)
            ):
                print(
                    f"  ⏭️ {kr_name}({t}): [MAX_POSITIONS:KR] "
                    f"국장 한도 {rb.MAX_POSITIONS_KR}개 도달 (패스)"
                )
                continue

            try:
                if not (hedge_only and rb._is_hedge_ticker(t, "KR")):
                    if ohlcv_200 and len(ohlcv_200) >= 2:
                        last_close = float(ohlcv_200[-2]["c"])
                        today_open = float(ohlcv_200[-1]["o"])
                        if last_close > 0:
                            gap_ratio = ((today_open - last_close) / last_close) * 100
                            if gap_ratio >= 5.0:
                                print(
                                    f"  ⏭️ {kr_name}({t}): 갭상승 과다 ({gap_ratio:.2f}%) - 필터링 (패스)"
                                )
                                continue
            except Exception as gap_err:
                print(f"  ⚠️ 갭상승 체크 중 오류: {gap_err}")

            curr_p = float(live_px_kr) if live_px_kr > 0 else 0.0
            if curr_p <= 0 and ohlcv_200 and len(ohlcv_200) > 0:
                curr_p = float(ohlcv_200[-1]["c"])

            if curr_p <= 0:
                print(f"  ⏭️ {kr_name}({t}): 현재가 조회 실패 (패스)")
                continue
            qty = int(target_budget / curr_p)
            if qty <= 0:
                print(
                    f"  ⏭️ {kr_name}({t}): [KR 매수 스킵] 시그널 통과했으나 수량 0 — "
                    f"배정예산 {int(target_budget):,}원 < 1주 기준(~{int(curr_p):,}원) "
                    f"(총자산 {int(total_kr_equity):,}원, ratio={ratio:.4f}, macro×{macro_mult:.2f}, 예수금 {int(kr_cash):,}원)"
                )
                continue

            if not rb._ai_false_breakout_buy_gate(
                t,
                "KR",
                strategy_type,
                rb.AI_FALSE_BREAKOUT_THRESHOLD,
                f"{kr_name}({t})",
            ):
                continue

            kr_box = [float(kr_cash)]
            entry_atr = float(rb.get_safe_atr(t, ohlcv_200) or 0.0)
            ok_kr_buy = rb._execute_kr_market_buy_twap(
                t,
                kr_name,
                float(target_budget),
                curr_p,
                sl_p,
                entry_atr,
                t_name,
                s_name,
                state,
                kr_box,
                strategy_type=strategy_type,
                entry_fib_level=entry_fib_level,
            )
            if not ok_kr_buy:
                print(
                    f"  ⏭️ {kr_name}({t}): [KR 매수 미체결] 시그널·필터 통과 후 주문 없음 — "
                    f"예산 {int(target_budget):,}원, 현재가 {int(curr_p):,}원 (TWAP 슬라이스·KIS·예수 확인)"
                )
            else:
                ctx.buy_fills += 1
                rb._register_swing_risk_after_buy(state, t, ohlcv_200, "KR")
            kr_cash = int(kr_box[0])
        except Exception as e:
            print(f"  ❌ [KR BUY 예외] {t}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

    return int(kr_cash)
