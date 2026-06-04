# -*- coding: utf-8 -*-
"""KR 시장 매매 사이클 — ``run_trading_bot`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from execution.market_cycles.context import TradingCycleContext

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
            
                # GUI 가격이 없을 때 직접 조회한 값을 쓰고, 있으면 GUI 공유값을 우선 적용
                curr_p = float(ohlcv[-1]['c'])
                try:
                    _price_resp = rb.kis_api.broker_kr.fetch_price(t)
                    if _price_resp and _price_resp.get('rt_cd') == '0':
                        _realtime_p = float(_price_resp.get('output', {}).get('stck_prpr', 0))
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
                    print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: {curr_p:,.0f} / 손절가: {hard_stop:,.0f})")
                    if curr_p <= hard_stop:
                        print(f"     ➜ 손절 체크: 현재가 {curr_p:,.0f} ≤ 손절가 {hard_stop:,.0f} = 🔴 매도 신호!")

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
                        reason = "하드스탑 이탈 (손실구간 방어)"
                        print(f"🔴 [하드스탑 발동] {t} - 현재가: {curr_p:,.0f}원 <= 손절가: {hard_stop:,.0f}원. 강제 청산! (is_exit={is_exit})")

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
                        print(f"  ❌ {kr_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")

            except Exception as e:
                print(f"  ❌ [KR 매도 루프 예외] {t}: {e}")
                traceback.print_exc()
                continue

        now_kr_post = datetime.now(pytz.timezone("Asia/Seoul"))
        is_kr_buy_time_post, _, _ = rb._is_kr_buy_window_now(now_kr_post)
        if is_kr_buy_time_post:
            kr_cash, total_kr_equity = rb._refresh_kr_cash_equity_after_sells()
        else:
            bal_est = rb.ensure_dict(rb.bal_read.kr_balance_raw(refresh=False))
            kr_cash, total_kr_equity = rb.parse_kr_cash_total(
                bal_est.get("output2", []), rb._to_float
            )
        state["circuit_aux_last_kr_krw"] = float(total_kr_equity)
        rb.save_state(rb.STATE_PATH, state)
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
                if not rb._macro_market_buy_allowed(macro_snap, "KR"):
                    print(
                        f"  -> 🚨 국장 Phase4 글로벌 방어막: 신규 매수 중단. "
                        f"({(macro_snap.get('market_buy_block_reason') or {}).get('KR', '')})"
                    )
                else:
                    # 지수 급락 체크
                    kr_index_change = rb.get_market_index_change("KR")
                    print(f"  📊 [KOSPI 지수] 변화율: {kr_index_change:+.2f}% 날씨는 {weather['KR']}")
                    if kr_index_change <= rb.INDEX_CRASH_KR:
                        print(f"  🚫 [KR 매수 중단] KOSPI {kr_index_change:+.2f}% 급락 (기준: {rb.INDEX_CRASH_KR}%)")
                    else:
                        if weather["KR"] == rb.WEATHER_LABEL_BEAR:
                            print(
                                "  📌 [KR] BEAR 날씨 — V8 추세 매수만 중단, SWING_FIB 스윙 후보는 계속 분석"
                            )
                        total_kr = len(ctx.final_targets_kr)
                        print(f"  -> 🇰🇷 국장 사냥감 {total_kr}개 정밀 분석 시작!")
                        for idx, t in enumerate(ctx.final_targets_kr, 1):
                            kr_name = rb.get_kr_company_name(t)  # 종목명 미리 조회
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
                            if rb.order_idem.is_buy_inflight(state, "KR", t, _buy_cycle_tag):
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
                                # 국장 매수: KIS 일봉 우선(매도 루프와 동일). yfinance만 쓰면 전일 봉·지연 종가로 양봉 오판 가능.
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
                                        live_px_kr = float(
                                            (_pr.get("output") or {}).get("stck_prpr", 0) or 0
                                        )
                                except Exception:
                                    pass

                                strategy_type = "TREND_V8"
                                entry_fib_level = 0.0
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
                                        f"  ⏭️ {kr_name}({t}): BEAR 시장 — V8 신호 통과했으나 추세 매수 차단 (스윙만 허용)"
                                    )
                                if v8_ok:
                                    print(f"  ✅ [V8-BUY] {kr_name}({t}) 진입")
                                else:
                                    sw_ok = entry_decision.swing_ok
                                    sw_fib = entry_decision.swing_fib
                                    sw_why = entry_decision.swing_why
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
                                            f"{' | BEAR 시장 스윙 예외' if weather['KR'] == rb.WEATHER_LABEL_BEAR else ''}"
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
                                    target_vol=_alpha_target_vol,
                                    ticker=t,
                                )

                                _prospect_atr = rb.atr_pct_from_ohlcv(ohlcv_200, t)
                                _mkt_heat, _heat_blocked = rb._portfolio_heat_snapshot(
                                    state,
                                    "KR",
                                    total_kr_equity,
                                    lambda tk, _b=rb.kis_api.broker_kr: rb.get_cached_ohlcv(
                                        tk, broker=_b
                                    ),
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
                                    print(f"  🧹 [예수금 영끌 발동] {kr_name}({t}): 예산({int(target_budget):,}원) 부족. 지갑에 남은 전액({int(kr_cash):,}원) 풀매수 장전!")
                                    target_budget = kr_cash
                        
                                if not rb.can_open_new(t, state, max_positions=rb.MAX_POSITIONS_KR):
                                    print(
                                        f"  ⏭️ {kr_name}({t}): [MAX_POSITIONS:KR] "
                                        f"국장 한도 {rb.MAX_POSITIONS_KR}개 도달 (패스)"
                                    )
                                    continue

                                # 🚨 [추가 수술] 국장 전용 시가 갭(Gap) 과다 상승 필터 (5%)
                                try:
                                    if ohlcv_200 and len(ohlcv_200) >= 2:
                                        last_close = float(ohlcv_200[-2]['c']) # 어제 종가
                                        today_open = float(ohlcv_200[-1]['o']) # 오늘 시가
                                        if last_close > 0:
                                            gap_ratio = ((today_open - last_close) / last_close) * 100
                                            if gap_ratio >= 5.0:
                                                print(f"  ⏭️ {kr_name}({t}): 갭상승 과다 ({gap_ratio:.2f}%) - 필터링 (패스)")
                                                continue
                                except Exception as gap_err:
                                    print(f"  ⚠️ 갭상승 체크 중 오류: {gap_err}")

                                # 현재가: KIS 실시간 우선, 없으면 일봉 종가
                                curr_p = float(live_px_kr) if live_px_kr > 0 else 0.0
                                if curr_p <= 0 and ohlcv_200 and len(ohlcv_200) > 0:
                                    curr_p = float(ohlcv_200[-1]['c'])
                    
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

                                # Phase 3: 매수 직전 뉴스 악재 필터
                                if not rb._ai_false_breakout_buy_gate(
                                    t,
                                    "KR",
                                    strategy_type,
                                    rb.AI_FALSE_BREAKOUT_THRESHOLD,
                                    f"{kr_name}({t})",
                                ):
                                    continue
                        
                                # 매수 주문 (Phase2 TWAP: 대액 시 분할)
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
    else:
        rb._log_kr_market_closed_or_suppressed()
