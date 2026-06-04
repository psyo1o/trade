# -*- coding: utf-8 -*-
"""US 시장 매매 사이클 — ``run_trading_bot`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from execution.market_cycles.context import TradingCycleContext

import time
from datetime import datetime

import pytz
import traceback


def _rb():
    import run_bot as rb
    return rb


def run_us_cycle(ctx: TradingCycleContext) -> None:
    rb = _rb()
    state = ctx.state
    weather = ctx.weather
    macro_mult = ctx.macro_mult
    macro_reason = ctx.macro_reason
    macro_snap = ctx.macro_snap
    _buy_cycle_tag = ctx.buy_cycle_tag
    _alpha_target_vol = float(rb.config.get("alpha_target_vol", 0.02))

    if rb.is_market_open("US") and not rb.kis_equities_weekend_suppress_window_kst():
        print("▶️ [🇺🇸 미장] 매매 엔진 시작...")
        us_cash = float(rb.get_us_cash_real(rb.kis_api.broker_us) or 0.0)
        us_bal = rb.ensure_dict(rb.get_us_positions_with_retry())
        out2 = rb._get_us_output2(us_bal)
        # =====================================================================
        # 🔥 [핵심 수술] KIS 야간 API 예수금 0원 증발 버그 치료 (GUI 로직 이식)
        # =====================================================================
        us_cash = rb._recover_us_cash_from_output2_if_needed(us_cash, out2)

        # 진짜 예수금 + 주식 평가금 = 진짜 총평가금 완료!
        us_output1 = rb._get_us_output1(us_bal)
        us_stock_value = rb._compute_us_stock_value_from_output(us_bal, out2)

        total_us_equity = us_cash + us_stock_value
        us_cash_snap, total_us_equity_snap = us_cash, total_us_equity
        print(f"  💰 [미장 자산 최종] 총자산: ${total_us_equity:.2f} (현금: ${us_cash:.2f} + 주식: ${us_stock_value:.2f})")

        state["circuit_aux_last_usd_total"] = float(total_us_equity)
        rb.save_state(rb.STATE_PATH, state)
    
        # ✅ [버그 수정] us_output1 정의 및 수량이 0보다 큰 종목만 held_us에 포함시킵니다.
        held_us = rb._extract_held_us_codes_from_output1(us_output1)

        # 디버깅: 보유 종목이 인식되었는지 확인
        rb._log_us_holdings_debug(held_us, us_bal)

        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        sell_candidates = rb._collect_us_sell_candidates(held_us, state.get("positions", {}))
        positions_count = len(sell_candidates)
    
        print(f"  🔍 [미장 매도 루프] 매도 대상 포지션 {positions_count}개 손익 체크 시작...")
        rb._prefetch_us_sell_ohlcv_if_needed(sell_candidates)
        
        for stock in us_output1:
            t_raw = stock.get('ovrs_pdno', stock.get('pdno', ''))
            t = rb.normalize_ticker(t_raw)
            if not t:
                continue
            qty_holding = rb._to_float(stock.get('ovrs_cblc_qty', stock.get('ccld_qty_smtl1', stock.get('hldg_qty', 0))), 0.0)
            if qty_holding <= 0:
                 continue

            if t not in state.get("positions", {}):
                avg_p = rb._to_float(stock.get('ovrs_avg_unpr', stock.get('ovrs_avg_pric', stock.get('ovrs_now_prc2', 0))), 0.0)
                if avg_p <= 0:
                    avg_p = rb._to_float(stock.get('ovrs_now_prc2', 0), 0.0)
                if avg_p > 0:
                    payload = {
                        'buy_p': float(avg_p),
                        'sl_p': float(avg_p * 0.9),
                        'max_p': float(avg_p),
                        'tier': '자동등록(보유종목)',
                        'buy_time': time.time(),
                        'buy_date': datetime.now().isoformat(),
                        'scale_out_done': False,
                        'entry_atr': float(0.0),
                    }
                    state.setdefault("positions", {})[t] = payload
                    rb.save_state(rb.STATE_PATH, state)
                    print(f"  🚨 [{t}] positions 미조회 → 즉시 자동등록 (buy_p=${avg_p:,.2f}, sl_p=${avg_p*0.9:,.2f})")
                else:
                    print(f"  ⏭️  [{t}] positions 미조회 + 평단/현재가 없음 - 스킵")
                    continue
            print(f"  🔍 [{t}] 매도 루프 진입 (장부 확인 완료, max_p 갱신 체크)")
            try:
                ohlcv = rb.get_cached_ohlcv(t)
            
                if not ohlcv or not isinstance(ohlcv, list) or not ohlcv[-1] or 'c' not in ohlcv[-1]:
                    print(f"  ❌ [US 매도 루프 예외] {t}: OHLCV 데이터 또는 종가(c) 정보 부족. 건너뜁니다.")
                    continue

                pos_info = state.get("positions", {}).get(t, {})
                atr_val = rb.get_safe_atr(t, ohlcv)
                rb._update_position_current_atr_if_changed(state, t, pos_info, atr_val)
            
                curr_p = float(ohlcv[-1]['c'])
                try:
                    _price_resp = rb.kis_api.broker_us.fetch_price(t)
                    if _price_resp and _price_resp.get('rt_cd') == '0':
                        _realtime_p = float(_price_resp.get('output', {}).get('last', 0))
                        if _realtime_p > 0:
                            curr_p = _realtime_p
                except Exception:
                    pass
                curr_p = rb._resolve_curr_price_with_gui_override(pos_info, float(curr_p))

                strategy_type = rb._resolve_sell_loop_strategy_type(pos_info)
                rb._update_position_max_p(state, t, pos_info, float(curr_p))
                pos_info = state.get("positions", {}).get(t, pos_info)
                buy_p = pos_info.get('buy_p', curr_p)
                max_p = pos_info.get('max_p', curr_p)
                now_str, hours_held, trading_h, buy_time_log = rb._compute_holding_time_info(
                    pos_info, "US"
                )
                exit_line = rb._resolve_exit_display_price(
                    t,
                    curr_p,
                    pos_info,
                    ohlcv,
                    strategy_type,
                    state=state,
                    trading_hours_held=trading_h,
                )
                rb._persist_exit_line_sl_p(state, t, pos_info, exit_line)
                hard_stop = rb._calc_hard_stop(
                    pos_info,
                    float(buy_p),
                    ohlcv=ohlcv,
                    strategy_type=strategy_type,
                    ticker=t,
                    trading_hours_held=trading_h,
                )
                profit_rate_now = rb._calc_profit_rate_pct(float(curr_p), float(buy_p))

                # 📊 [상태 로그] 한눈에 보기
                us_name = rb.get_us_company_name(t)
                _exit_tag = "스윙" if strategy_type == "SWING_FIB" else "V8"
                _sw_suffix = (
                    rb._format_swing_exit_log_suffix("US", pos_info, ohlcv, float(curr_p), float(buy_p))
                    if strategy_type == "SWING_FIB"
                    else ""
                )
                print(
                    f"  📊 [US 보유] {us_name}({t}) | 현재가: ${curr_p:.2f} | 매수가: ${buy_p:.2f} | "
                    f"최고가: ${max_p:.2f} | 매도선({_exit_tag}): ${exit_line:.2f} | "
                    f"수익률: {profit_rate_now:+.2f}%{_sw_suffix}"
                )

                # 수익률 +1% 미만·매수 후 15분: 매도 보류 (손실 구간 포함)
                buy_time = pos_info.get('buy_time', 0)
                if rb._new_buy_sell_protection_blocks(profit_rate_now, buy_time):
                    remain_sec = rb._new_buy_protection_remaining_sec(buy_time)
                    print(
                        f"  ⏭️ {t}: 신규 매수 보호 구간 "
                        f"({remain_sec}초 남음, 수익률 {profit_rate_now:+.2f}%)"
                    )
                    continue

                if strategy_type == "SWING_FIB":
                    sw_action, sw_reason = rb.decide_swing_exit(
                        pos_info,
                        ohlcv,
                        market="US",
                        ticker=t,
                        reference_price=float(curr_p),
                        trading_hours_held=trading_h,
                    )
                    if sw_action == "HALF":
                        if rb.order_idem.lane_has_filled_sell(
                            state, "US", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag
                        ):
                            rb.order_idem.reconcile_ticker_lane(
                                state, "US", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag, rb.STATE_PATH
                            )
                            continue
                        sq = rb.compute_stock_scale_out_qty(int(float(qty_holding)))
                        if not sq:
                            print(f"  ⏭️ [SWING-SELL] {us_name}({t}) HALF 수량 0 (패스)")
                            continue
                        sp_half = round(float(curr_p) * 0.98, 2)

                        def _us_swing_half_place():
                            return rb.execute_us_order_direct(
                                rb.kis_api.broker_us, "sell", t, int(sq), sp_half
                            )

                        fill_half = rb._idempotent_kis_sell(
                            state,
                            market="US",
                            ticker=t,
                            lane=rb.order_idem.LANE_SWING_HALF,
                            qty=int(sq),
                            fallback_price=float(curr_p),
                            place_order=_us_swing_half_place,
                            cycle_tag=_buy_cycle_tag,
                        )
                        if fill_half.ok:
                            new_half = rb.post_partial_ledger(
                                pos_info,
                                float(sq),
                                float(curr_p),
                                float(qty_holding),
                                set_scale_out_done=True,
                            )
                            new_half["strategy_type"] = "SWING_FIB"
                            new_half["entry_fib_level"] = float(pos_info.get("entry_fib_level", 0.0) or 0.0)
                            rb.ledger_apply.persist_position_set(
                                state, t, new_half, context="SWING HALF US", state_path=rb.STATE_PATH
                            )
                            rb._record_trade_event(
                                "US",
                                t,
                                "SELL",
                                int(sq),
                                price=float(curr_p),
                                profit_rate=float(profit_rate_now),
                                reason=f"[SWING-SELL] {sw_reason}",
                            )
                            print(f"  ✅ [SWING-SELL] {us_name}({t}) HALF | {sw_reason}")
                            rb._telegram_swing_sell(
                                "US",
                                t,
                                name=us_name,
                                half=True,
                                qty_label=f"{int(sq)}주",
                                profit_rate=float(profit_rate_now),
                                reason=sw_reason,
                            )
                        else:
                            print(
                                f"  ❌ [SWING-SELL] {us_name}({t}) HALF 실패: {fill_half.note}"
                            )
                        continue
                    if sw_action == "FULL":
                        qty_full = int(float(qty_holding))
                        if qty_full <= 0:
                            continue
                        sp_full = round(float(curr_p) * 0.98, 2)

                        def _us_swing_full_place():
                            return rb.execute_us_order_direct(
                                rb.kis_api.broker_us, "sell", t, qty_full, sp_full
                            )

                        fill_full = rb._idempotent_kis_sell(
                            state,
                            market="US",
                            ticker=t,
                            lane=rb.order_idem.LANE_SWING_FULL,
                            qty=qty_full,
                            fallback_price=float(curr_p),
                            place_order=_us_swing_full_place,
                            cycle_tag=_buy_cycle_tag,
                        )
                        if fill_full.ok:
                            p_full = ((float(sp_full) - float(buy_p)) / float(buy_p) * 100) if float(buy_p) > 0 else 0.0
                            rb._record_trade_event("US", t, "SELL", qty_full, price=float(sp_full), profit_rate=float(p_full), reason=f"[SWING-SELL] {sw_reason}")
                            print(f"  ✅ [SWING-SELL] {us_name}({t}) FULL | {sw_reason}")
                            rb._telegram_swing_sell(
                                "US",
                                t,
                                name=us_name,
                                half=False,
                                qty_label=f"{qty_full}주",
                                profit_rate=float(p_full),
                                reason=sw_reason,
                            )
                            def _mut_us_swing_full(st: dict) -> None:
                                rb.set_cooldown(st, t)
                                rb.set_ticker_cooldown_after_sell(
                                    st,
                                    t,
                                    sw_reason,
                                    profit_rate=float(p_full),
                                    strategy_type="SWING_FIB",
                                    market="US",
                                    remaining_qty=0.0,
                                )

                            rb.ledger_apply.persist_position_remove(
                                state, t, context="SWING FULL US", state_path=rb.STATE_PATH, mutate_fn=_mut_us_swing_full
                            )
                        else:
                            print(
                                f"  ❌ [SWING-SELL] {us_name}({t}) FULL 실패: {fill_full.note}"
                            )
                        continue
                    # HOLD: 절반 익절은 check_swing_exit 1.5R HALF만 — V8 Scale-Out 블록 진입 금지

                # V7.1: V8 분할 익절 1차(3×ATR)·2차(6×ATR) — TREND_V8 전용 (SWING_FIB 격리)
                if strategy_type == "TREND_V8":
                    def _us_so_slice(qq: int):
                        sp = round(float(curr_p) * 0.98, 2)
                        return rb.execute_us_order_direct(
                            rb.kis_api.broker_us, "sell", t, int(qq), sp
                        )

                    so_cont, pos_info = rb._try_v8_scale_out_kr_us(
                        state,
                        market="US",
                        ticker=t,
                        pos_info=pos_info,
                        qty=int(float(qty_holding)),
                        buy_p=float(buy_p),
                        curr_p=float(curr_p),
                        profit_rate_now=float(profit_rate_now),
                        cycle_tag=_buy_cycle_tag,
                        is_us=True,
                        place_slice=_us_so_slice,
                        display_name=us_name,
                    )
                    if so_cont:
                        continue

                reason = ""
                is_exit = False

                rb._print_position_hold_status(
                    now_str,
                    t,
                    buy_time_log,
                    hours_held,
                    line_prefix="  ",
                    trading_hours=trading_h,
                    market="US",
                )

                ts_exit, ts_reason, ts_exempt = rb._evaluate_time_stop(
                    market="US",
                    strategy_type=strategy_type,
                    hours_held=float(trading_h),
                    profit_rate_now=float(profit_rate_now),
                )
                if ts_exit:
                    is_exit = True
                    reason = ts_reason
                    print(f"  ⏰ {reason}")
                elif ts_exempt:
                    _ts_tag, _ts_min_h, _ts_exempt_pct = rb._time_stop_params("US", strategy_type)
                    print(
                        f"     ✅ 타임스탑 유예 {_ts_tag} — 보유 {trading_h:.1f}h (≥{_ts_min_h:.0f}h), "
                        f"수익률 {profit_rate_now:+.2f}% ≥ {_ts_exempt_pct:.1f}%"
                    )

                # 🛑 [매도 로직 2] 하드스탑 (SWING_FIB는 check_swing_exit 피보·구름 FULL 전담)
                if not is_exit and profit_rate_now < 0 and strategy_type != "SWING_FIB":
                    print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: ${curr_p:.2f} / 손절가: ${hard_stop:.2f})")
                    if curr_p <= hard_stop:
                        is_exit = True
                        reason = "하드스탑 이탈 (손실구간 방어)"
                        print(f"  🔴 [하드스탑 발동] {t} - 현재가: ${curr_p:.2f} <= 손절가: ${hard_stop:.2f}. 강제 청산!")

                # 🛑 [매도 로직 3] 수익 구간 트레일링 (스윙: 수익 락만 / V8: 샹들리에)
                if not is_exit and profit_rate_now >= 0:
                    if strategy_type == "SWING_FIB":
                        is_exit, reason = rb._check_swing_trailing_exit(
                            float(curr_p), pos_info, ohlcv, state, t
                        )
                    else:
                        is_exit, reason_chandelier = rb.decide_v8_exit(t, curr_p, pos_info, ohlcv)
                        if is_exit:
                            reason = reason_chandelier

                # 🎯 실제 매도 주문 실행
                if is_exit:
                    # ✅ [핵심 버그 수정] 엉뚱하게 다시 구하지 말고 맨 위에서 구한 정확한 수량 재사용!
                    qty = qty_holding 
                    if qty <= 0:
                        print(f"  ❌ {t} 매도 오류: 수량이 0으로 인식됨.")
                        continue
                
                    sell_price = round(curr_p * 0.98, 2)

                    def _us_exit_place():
                        return rb.execute_us_order_direct(
                            rb.kis_api.broker_us, "sell", t, qty, sell_price
                        )

                    fill_exit = rb._idempotent_kis_sell(
                        state,
                        market="US",
                        ticker=t,
                        lane=rb.order_idem.LANE_EXIT,
                        qty=int(qty),
                        fallback_price=float(sell_price),
                        place_order=_us_exit_place,
                        cycle_tag=_buy_cycle_tag,
                    )
                    tag_exit = "♻️" if fill_exit.reused else "🧾"
                    print(
                        f"  {tag_exit} [US EXIT] {t} ok={fill_exit.ok} qty={int(fill_exit.qty)} "
                        f"note={fill_exit.note}"
                    )

                    if fill_exit.ok:
                        px = float(fill_exit.price) if fill_exit.price > 0 else float(sell_price)
                        profit_rate = ((px - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] = int(stats.get("wins", 0)) + 1
                        else:
                            stats["losses"] += 1
                        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                        rb._record_trade_event("US", t, "SELL", qty, price=sell_price, profit_rate=profit_rate, reason=reason)
                        print(f"  ✅ [미장 매도 체결] {us_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                        if strategy_type == "SWING_FIB":
                            rb.send_telegram(
                                f"🚨 [미장 스윙 청산] {t}({us_name})\n"
                                f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                            )
                        else:
                            rb.send_telegram(
                                f"🚨 [미장 추세종료 매도] {t}({us_name})\n"
                                f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                            )
                        def _mut_us_exit(st: dict) -> None:
                            rb.set_cooldown(st, t)
                            rb.set_ticker_cooldown_after_sell(
                                st,
                                t,
                                reason,
                                profit_rate=float(profit_rate),
                                strategy_type=strategy_type,
                                market="US",
                                remaining_qty=0.0,
                            )

                        rb.ledger_apply.persist_position_remove(
                            state, t, context="US EXIT", state_path=rb.STATE_PATH, mutate_fn=_mut_us_exit
                        )
                    else:
                        print(f"  ❌ {us_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")
            except Exception as e:
                print(f"  ❌ [US 매도 루프 예외] {t}: {e}")
                traceback.print_exc()
                continue

        now_us_post = datetime.now(pytz.timezone("US/Eastern"))
        is_us_buy_time_post, _, _ = rb._is_us_buy_window_now(now_us_post)
        if is_us_buy_time_post:
            us_cash, total_us_equity = rb._refresh_us_cash_equity_after_sells()
        else:
            us_bal_est = rb.ensure_dict(rb.bal_read.us_balance_raw(refresh=False))
            us_cash = float(rb.get_us_cash_real(rb.kis_api.broker_us) or 0.0)
            out2 = rb._get_us_output2(us_bal_est)
            us_cash = rb._recover_us_cash_from_output2_if_needed(us_cash, out2)
            us_stock_value = rb._compute_us_stock_value_from_output(us_bal_est, out2)
            total_us_equity = float(us_cash + us_stock_value)
        state["circuit_aux_last_usd_total"] = float(total_us_equity)
        rb.save_state(rb.STATE_PATH, state)
        if is_us_buy_time_post and (
            abs(us_cash - us_cash_snap) >= 0.01
            or abs(total_us_equity - total_us_equity_snap) >= 1.0
        ):
            print(
                f"  📌 [US] 매도 후 예수·총평가 갱신 → 가용 ${us_cash:.2f} · 총자산 ${total_us_equity:.2f} "
                f"(매도단계 전 스냅샷 대비 반영)"
            )

        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not rb.check_mdd_break("US", total_us_equity, state, rb.STATE_PATH):
            print("  -> 🚨 미장 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 미장 Phase4 거시 방어막: 신규 매수 중단. ({macro_reason})")
        elif rb.in_account_circuit_cooldown(state, "US"):
            print("  -> 🚨 미장 Phase5 비중 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # ⏳ [핵심] 미장 매수: NYSE 정규장 마감(16:00 ET) 직전 N분만 (기본 30분 → 15:30~15:59)
            now_ny = datetime.now(pytz.timezone("US/Eastern"))
            is_us_buy_time, _us_buy_start, _us_close = rb._is_us_buy_window_now(now_ny)

            if not is_us_buy_time:
                print(
                    f"  ⏳ [US 매수 대기] 장 마감 {rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE}분 전 구간만 매수 "
                    f"({_us_buy_start.strftime('%H:%M')}~{_us_close.strftime('%H:%M')} ET, "
                    f"현재 {now_ny.strftime('%H:%M')})"
                )
            else:
                ctx.buy_zone_us = True
                if not rb._macro_market_buy_allowed(macro_snap, "US"):
                    print(
                        f"  -> 🚨 미장 Phase4 글로벌 방어막: 신규 매수 중단. "
                        f"({(macro_snap.get('market_buy_block_reason') or {}).get('US', '')})"
                    )
                else:
                    # 지수 급락 체크
                    us_index_change = rb.get_market_index_change("US")
                    print(f"  📊 [S&P500 지수] 변화율: {us_index_change:+.2f}% 날씨는 {weather['US']}")
                    if us_index_change <= rb.INDEX_CRASH_US:
                        print(f"  🚫 [US 매수 중단] S&P500 {us_index_change:+.2f}% 급락 (기준: {rb.INDEX_CRASH_US}%)")
                    else:
                        if weather["US"] == rb.WEATHER_LABEL_BEAR:
                            print(
                                "  📌 [US] BEAR 날씨 — V8 추세 매수만 중단, SWING_FIB 스윙 후보는 계속 분석"
                            )
                        # 2) 미장 타겟: 고베타 유니버스 NDX~90 + S&P 섹터 RR (150, ``us_universe_cache.json``)
                        night_targets = rb.get_top_market_cap_tickers(150)
                        night_targets = rb._sort_buy_targets_by_rs(night_targets, "US")
                        total_us = len(night_targets)
                        print(f"  -> 🇺🇸 미장 유니버스(고베타·섹터분산) {total_us}개 정밀 분석 시작!")

                        for idx, t in enumerate(night_targets, 1):
                            try:
                                us_name = rb.get_us_company_name(t)  # 종목명 미리 조회
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
                                if rb.order_idem.is_buy_inflight(state, "US", t, _buy_cycle_tag):
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

                                # OHLCV: 신호 계산에 필요
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
                                    target_vol=_alpha_target_vol,
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
                                    print(f"  🧹 [미장 영끌 발동] {us_name}({t}): 예산(${target_budget:.2f}) 부족. 지갑에 남은 전액(${us_cash:.2f}) 풀매수 장전!")
                                    target_budget = us_cash
                                if not rb.can_open_new(t, state, max_positions=rb.MAX_POSITIONS_US):
                                    print(f"  ⏭️ {us_name}({t}): 포지션 개수 초과 ({rb.MAX_POSITIONS_US}개) (패스)")
                                    continue

                                curr_p = float(ohlcv[-1]['c'])
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

                                # 시장가 매수 (Phase2 TWAP: 대액 시 USD 분할, 슬라이스마다 101% 지정가)
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
    else:
        rb._log_us_market_closed_or_suppressed()
