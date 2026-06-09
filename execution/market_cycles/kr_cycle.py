# -*- coding: utf-8 -*-
"""KR 시장 매매 사이클 — ``run_trading_bot`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from execution.market_cycles.context import TradingCycleContext
from execution.market_cycles.kr_buy_cycle import run_kr_buy_cycle

import time
from datetime import datetime

import pytz
import traceback


def _rb():
    import run_bot as rb
    return rb


def run_kr_cycle(ctx: TradingCycleContext) -> None:
    rb = _rb()
    state = ctx.state
    weather = ctx.weather
    macro_mult = ctx.macro_mult
    macro_reason = ctx.macro_reason
    macro_snap = ctx.macro_snap
    _buy_cycle_tag = ctx.buy_cycle_tag
    _alpha_target_vol = float(rb.config.get("alpha_target_vol", 0.02))
    final_targets = ctx.final_targets_kr

    if rb.is_market_open("KR") and not rb.kis_equities_weekend_suppress_window_kst():
        print("▶️ [🇰🇷 국장] 매매 엔진 시작...")
        _, kr_cash, total_kr_equity, kr_output1, held_kr = rb._prepare_kr_market_cycle_inputs(state)
        kr_cash_snap, total_kr_equity_snap = kr_cash, total_kr_equity
        kr_sell_fills = 0
        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        positions_count = rb._count_positions_in_state(held_kr, state.get("positions", {}))
        rb._prefetch_kr_sell_ohlcv_if_needed(kr_output1, held_kr, positions_count)
        for stock in kr_output1:
            t = rb.normalize_ticker(stock.get('pdno', ''))
            if not t:
                continue
            qty = int(rb._to_float(stock.get('hldg_qty', stock.get('t01', stock.get('q', 0)))))
            if qty <= 0 or t not in held_kr:
                continue
            if t not in state.get("positions", {}):
                avg_p = rb._to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', stock.get('prpr', 0))), 0.0)
                if avg_p <= 0:
                    avg_p = rb._to_float(stock.get('prpr', 0), 0.0)
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
                    print(f"  🚨 [{t}] positions 미조회 → 즉시 자동등록 (buy_p={avg_p:,.2f}, sl_p={avg_p*0.9:,.2f})")
                else:
                    print(f"  ⏭️  [{t}] positions 미조회 + 평단/현재가 없음 - 스킵")
                    continue
            try:
                ohlcv = rb.get_cached_ohlcv(t, broker=rb.kis_api.broker_kr)
            
                if not ohlcv or not isinstance(ohlcv, list) or not ohlcv[-1] or 'c' not in ohlcv[-1]:
                    print(f"  ❌ [KR 매도 루프 예외] {t}: OHLCV 데이터 또는 종가(c) 정보 부족. 건너뜁니다.")
                    continue

                pos_info = state.get("positions", {}).get(t, {})
                atr_val = rb.get_safe_atr(t, ohlcv)
                rb._update_position_current_atr_if_changed(state, t, pos_info, atr_val)
            
                from api.kis_parsers import parse_kr_live_price

                curr_p = float(ohlcv[-1]['c'])
                _balance_px = parse_kr_live_price(stock, rb._to_float)
                if _balance_px > 0:
                    curr_p = _balance_px
                curr_p = rb._resolve_curr_price_with_gui_override(pos_info, float(curr_p))

                strategy_type = rb._resolve_sell_loop_strategy_type(pos_info)
                rb._update_position_max_p(state, t, pos_info, float(curr_p))
                pos_info = state.get("positions", {}).get(t, pos_info)
                buy_p = pos_info.get('buy_p', curr_p)
                max_p = pos_info.get('max_p', curr_p)
                now_str, hours_held, trading_h, buy_time_log = rb._compute_holding_time_info(
                    pos_info, "KR"
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
                kr_name = rb.get_kr_company_name(t)
                _exit_tag = "스윙" if strategy_type == "SWING_FIB" else "V8"
                _sw_suffix = (
                    rb._format_swing_exit_log_suffix("KR", pos_info, ohlcv, float(curr_p), float(buy_p))
                    if strategy_type == "SWING_FIB"
                    else ""
                )
                print(
                    f"  📊 [KR 보유] {kr_name}({t}) | 현재가: {int(curr_p):,}원 | 매수가: {int(buy_p):,}원 | "
                    f"최고가: {int(max_p):,}원 | 매도선({_exit_tag}): {int(exit_line):,}원 | "
                    f"수익률: {profit_rate_now:+.2f}%{_sw_suffix}"
                )

                # 손절가 체크 로그
                if profit_rate_now < 0:
                    _stop_lbl = rb._v8_loss_stop_log_label(buy_p, pos_info, hard_stop)
                    print(
                        f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% "
                        f"(현재가: {curr_p:,.0f} / {_stop_lbl}: {hard_stop:,.0f})"
                    )
                    if curr_p <= hard_stop:
                        print(
                            f"     ➜ {_stop_lbl} 체크: 현재가 {curr_p:,.0f} ≤ 기준 {hard_stop:,.0f} = 🔴 매도 신호!"
                        )

                # 수익률 +1% 미만·매수 후 15분: 매도 보류 (손실 구간 포함)
                buy_time = pos_info.get('buy_time', time.time() - 900)
                if rb._new_buy_sell_protection_blocks(profit_rate_now, buy_time):
                    print(
                        f"  ⏭️ {t}: 신규 매수 보호 구간 "
                        f"({rb._new_buy_protection_remaining_sec(buy_time)}초 남음, 수익률 {profit_rate_now:+.2f}%)"
                    )
                    continue

                if strategy_type == "SWING_FIB":
                    sw_action, sw_reason = rb.decide_swing_exit(
                        pos_info,
                        ohlcv,
                        market="KR",
                        ticker=t,
                        reference_price=float(curr_p),
                        trading_hours_held=trading_h,
                    )
                    if sw_action == "HALF":
                        if rb.order_idem.lane_has_filled_sell(
                            state, "KR", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag
                        ):
                            rb.order_idem.reconcile_ticker_lane(
                                state, "KR", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag, rb.STATE_PATH
                            )
                            continue
                        sq = rb.compute_stock_scale_out_qty(int(qty))
                        if not sq:
                            print(f"  ⏭️ [SWING-SELL] {kr_name}({t}) HALF 수량 0 (패스)")
                            continue

                        def _kr_swing_half_place():
                            return rb.create_market_sell_order_kis(
                                t, int(sq), is_us=False, curr_price=float(curr_p)
                            )

                        fill_half = rb._idempotent_kis_sell(
                            state,
                            market="KR",
                            ticker=t,
                            lane=rb.order_idem.LANE_SWING_HALF,
                            qty=int(sq),
                            fallback_price=float(curr_p),
                            place_order=_kr_swing_half_place,
                            cycle_tag=_buy_cycle_tag,
                        )
                        if fill_half.ok:
                            kr_sell_fills += 1
                            new_half = rb.post_partial_ledger(
                                pos_info,
                                float(sq),
                                float(curr_p),
                                float(qty),
                                set_scale_out_done=True,
                            )
                            new_half["strategy_type"] = "SWING_FIB"
                            new_half["entry_fib_level"] = float(pos_info.get("entry_fib_level", 0.0) or 0.0)
                            rb.ledger_apply.persist_position_set(
                                state, t, new_half, context="SWING HALF KR", state_path=rb.STATE_PATH
                            )
                            rb._record_trade_event(
                                "KR",
                                t,
                                "SELL",
                                int(sq),
                                price=float(curr_p),
                                profit_rate=float(profit_rate_now),
                                reason=f"[SWING-SELL] {sw_reason}",
                            )
                            print(f"  ✅ [SWING-SELL] {kr_name}({t}) HALF | {sw_reason}")
                            rb._telegram_swing_sell(
                                "KR",
                                t,
                                name=kr_name,
                                half=True,
                                qty_label=f"{int(sq)}주",
                                profit_rate=float(profit_rate_now),
                                reason=sw_reason,
                            )
                        else:
                            print(
                                f"  ❌ [SWING-SELL] {kr_name}({t}) HALF 실패: {fill_half.note}"
                            )
                        continue
                    if sw_action == "FULL":
                        # 스윙 전량 청산 시그널
                        qty_full = int(rb._to_float(stock.get('hldg_qty', stock.get('t01', stock.get('q', 0)))))
                        if qty_full <= 0:
                            continue

                        def _kr_swing_full_place():
                            return rb.create_market_sell_order_kis(
                                t, qty_full, is_us=False, curr_price=float(curr_p)
                            )

                        fill_full = rb._idempotent_kis_sell(
                            state,
                            market="KR",
                            ticker=t,
                            lane=rb.order_idem.LANE_SWING_FULL,
                            qty=qty_full,
                            fallback_price=float(curr_p),
                            place_order=_kr_swing_full_place,
                            cycle_tag=_buy_cycle_tag,
                        )
                        if fill_full.ok:
                            kr_sell_fills += 1
                            p_full = ((float(curr_p) - float(buy_p)) / float(buy_p) * 100) if float(buy_p) > 0 else 0.0
                            rb._record_trade_event("KR", t, "SELL", qty_full, price=float(curr_p), profit_rate=float(p_full), reason=f"[SWING-SELL] {sw_reason}")
                            print(f"  ✅ [SWING-SELL] {kr_name}({t}) FULL | {sw_reason}")
                            rb._telegram_swing_sell(
                                "KR",
                                t,
                                name=kr_name,
                                half=False,
                                qty_label=f"{qty_full}주",
                                profit_rate=float(p_full),
                                reason=sw_reason,
                            )

                            def _mut_swing_full(st: dict) -> None:
                                rb.set_cooldown(st, t)
                                rb.set_ticker_cooldown_after_sell(
                                    st,
                                    t,
                                    sw_reason,
                                    profit_rate=float(p_full),
                                    strategy_type="SWING_FIB",
                                    market="KR",
                                    remaining_qty=0.0,
                                )

                            rb.ledger_apply.persist_position_remove(
                                state, t, context="SWING FULL KR", state_path=rb.STATE_PATH, mutate_fn=_mut_swing_full
                            )
                        else:
                            print(
                                f"  ❌ [SWING-SELL] {kr_name}({t}) FULL 실패: {fill_full.note}"
                            )
                        continue
                    # HOLD: 절반 익절은 check_swing_exit 1.5R HALF만 — V8 Scale-Out 블록 진입 금지

                # V7.1: V8 분할 익절 1차(3×ATR)·2차(6×ATR) — TREND_V8 전용 (SWING_FIB 격리)
                if strategy_type == "TREND_V8":
                    kr_nm = rb.get_kr_company_name(t)

                    def _kr_so_slice(qq: int):
                        return rb.create_market_sell_order_kis(
                            t, int(qq), is_us=False, curr_price=float(curr_p)
                        )

                    so_cont, pos_info = rb._try_v8_scale_out_kr_us(
                        state,
                        market="KR",
                        ticker=t,
                        pos_info=pos_info,
                        qty=int(qty),
                        buy_p=float(buy_p),
                        curr_p=float(curr_p),
                        profit_rate_now=float(profit_rate_now),
                        cycle_tag=_buy_cycle_tag,
                        is_us=False,
                        place_slice=_kr_so_slice,
                        display_name=kr_nm,
                    )
                    if so_cont:
                        kr_sell_fills += 1
                        continue

                # 매도 결정 로직 (우선순위: 타임스탑 > 하드스탑 > 샹들리에)
                reason = ""
                is_exit = False
                rb._print_position_hold_status(
                    now_str,
                    t,
                    buy_time_log,
                    hours_held,
                    trading_hours=trading_h,
                    market="KR",
                )

                ts_exit, ts_reason, ts_exempt = rb._evaluate_time_stop(
                    market="KR",
                    strategy_type=strategy_type,
                    hours_held=float(trading_h),
                    profit_rate_now=float(profit_rate_now),
                )
                if ts_exit:
                    is_exit = True
                    reason = ts_reason
                    print(f"  ⏰ {reason}")
                elif ts_exempt:
                    _ts_tag, _ts_min_h, _ts_exempt_pct = rb._time_stop_params("KR", strategy_type)
                    print(
                        f"   ✅ 타임스탑 유예 {_ts_tag} — 보유 {trading_h:.1f}h (≥{_ts_min_h:.0f}h), "
                        f"수익률 {profit_rate_now:+.2f}% ≥ {_ts_exempt_pct:.1f}%"
                    )

                # 2. 하드스탑 (SWING_FIB는 check_swing_exit 피보·구름 FULL이 전담)
                if not is_exit and profit_rate_now < 0 and strategy_type != "SWING_FIB":
                    if curr_p <= hard_stop:
                        is_exit = True
                        reason, _exit_log = rb._v8_loss_zone_exit_meta(
                            buy_p, pos_info, hard_stop, curr_p, market="KR"
                        )
                        print(f"{_exit_log} (is_exit={is_exit})")

                # 3. 수익 구간 트레일링 (스윙: 수익 락만 / V8: 샹들리에)
                if not is_exit and profit_rate_now >= 0:
                    if strategy_type == "SWING_FIB":
                        is_exit, reason = rb._check_swing_trailing_exit(
                            float(curr_p), pos_info, ohlcv, state, t
                        )
                    else:
                        is_exit, reason_chandelier = rb.decide_v8_exit(t, curr_p, pos_info, ohlcv)
                        if is_exit:
                            reason = reason_chandelier

                if is_exit: # 여기서 실제 매도 주문이 나감
                    kr_name = rb.get_kr_company_name(t)  # 종목명 미리 조회
                    qty = int(rb._to_float(stock.get('hldg_qty', stock.get('t01', stock.get('q', 0)))))
                    if qty <= 0:
                        continue
                
                    def _kr_exit_place():
                        return rb.create_market_sell_order_kis(
                            t, qty, is_us=False, curr_price=curr_p
                        )

                    fill_exit = rb._idempotent_kis_sell(
                        state,
                        market="KR",
                        ticker=t,
                        lane=rb.order_idem.LANE_EXIT,
                        qty=qty,
                        fallback_price=float(curr_p),
                        place_order=_kr_exit_place,
                        cycle_tag=_buy_cycle_tag,
                    )
                    tag_exit = "♻️" if fill_exit.reused else "🧾"
                    print(
                        f"  {tag_exit} [KR EXIT] {t} ok={fill_exit.ok} qty={int(fill_exit.qty)} "
                        f"note={fill_exit.note}"
                    )

                    if fill_exit.ok:
                        kr_sell_fills += 1
                        profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] = int(stats.get("wins", 0) or 0) + 1
                        else:
                            stats["losses"] = int(stats.get("losses", 0) or 0) + 1
                        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                        rb._record_trade_event("KR", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                        print(f"  ✅ [국장 매도 체결] {kr_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                        if strategy_type == "SWING_FIB":
                            rb.send_telegram(
                                f"🚨 [국장 스윙 청산] {t}({kr_name})\n"
                                f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                            )
                        else:
                            rb.send_telegram(
                                f"🚨 [국장 추세종료 매도] {t}({kr_name})\n"
                                f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                            )
                        def _mut_kr_exit(st: dict) -> None:
                            rb.set_cooldown(st, t)
                            rb.set_ticker_cooldown_after_sell(
                                st,
                                t,
                                reason,
                                profit_rate=float(profit_rate),
                                strategy_type=strategy_type,
                                market="KR",
                                remaining_qty=0.0,
                            )

                        rb.ledger_apply.persist_position_remove(
                            state, t, context="KR EXIT", state_path=rb.STATE_PATH, mutate_fn=_mut_kr_exit
                        )
                    else:
                        print(f"  ❌ {kr_name}({t}) 매도 최종 실패: {fill_exit.note or '응답 없음'}")

            except Exception as e:
                print(f"  ❌ [KR 매도 루프 예외] {t}: {e}")
                traceback.print_exc()
                continue

        now_kr_post = datetime.now(pytz.timezone("Asia/Seoul"))
        is_kr_buy_time_post, _, _ = rb._is_kr_buy_window_now(now_kr_post)
        if rb.is_market_open("KR"):
            if kr_sell_fills > 0:
                kr_cash, total_kr_equity = rb._refresh_kr_cash_equity_after_sells()
                rb._sync_market_display_snapshot_after_sells(
                    "KR", state, int(kr_cash), int(total_kr_equity)
                )
            else:
                kr_cash, total_kr_equity = kr_cash_snap, total_kr_equity_snap
                print(
                    "  ⏭️ [KR] 이번 사이클 매도 체결 없음 — "
                    "사이클 시작 잔고 재사용 (KIS 재조회 생략)"
                )
        else:
            bal_est = rb.ensure_dict(rb.bal_read.kr_balance_raw(refresh=False))
            kr_cash, total_kr_equity = rb.parse_kr_cash_total(
                bal_est.get("output2", []), rb._to_float
            )
        if is_kr_buy_time_post and (
            abs(kr_cash - kr_cash_snap) >= 1 or abs(total_kr_equity - total_kr_equity_snap) >= 1000
        ):
            print(
                f"  📌 [KR] 매도 후 예수·총평가 갱신 → 가용 {kr_cash:,}원 · 총평가 {total_kr_equity:,}원 "
                f"(매도단계 전 스냅샷 대비 반영)"
            )

        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not rb.check_mdd_break("KR", total_kr_equity, state, rb.STATE_PATH):
            print("  -> 🚨 국장 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 국장 Phase4 거시 방어막: 신규 매수 중단. ({macro_reason})")
        elif rb.in_account_circuit_cooldown(state, "KR"):
            print("  -> 🚨 국장 Phase5 비중 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # ⏳ [핵심] 국장 매수: KRX 정규장 마감(15:30 KST) 직전 N분만 (기본 30분 → 15:00~15:29)
            now_kr = datetime.now(pytz.timezone("Asia/Seoul"))
            is_kr_buy_time, _kr_buy_start, _kr_close = rb._is_kr_buy_window_now(now_kr)

            if not is_kr_buy_time:
                print(
                    f"  ⏳ [KR 매수 대기] 장 마감 {rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE}분 전 구간만 매수 "
                    f"({_kr_buy_start.strftime('%H:%M')}~{_kr_close.strftime('%H:%M')} KST, "
                    f"현재 {now_kr.strftime('%H:%M')})"
                )
            else:
                ctx.buy_zone_kr = True
                kr_cash = run_kr_buy_cycle(
                    ctx,
                    held_kr=held_kr,
                    kr_cash=kr_cash,
                    total_kr_equity=total_kr_equity,
                    alpha_target_vol=_alpha_target_vol,
                )
    else:
        rb._log_kr_market_closed_or_suppressed()
