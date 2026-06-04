# -*- coding: utf-8 -*-
"""COIN 시장 매매 사이클 — ``run_trading_bot`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from execution.market_cycles.context import TradingCycleContext

import time
from datetime import datetime

import pytz
import traceback


def _rb():
    import run_bot as rb
    return rb


def run_coin_cycle(ctx: TradingCycleContext) -> None:
    rb = _rb()
    state = ctx.state
    weather = ctx.weather
    macro_mult = ctx.macro_mult
    macro_reason = ctx.macro_reason
    macro_snap = ctx.macro_snap
    _buy_cycle_tag = ctx.buy_cycle_tag
    _alpha_target_vol = float(rb.config.get("alpha_target_vol", 0.02))

    if rb.is_market_open("COIN"):
        coin_weather = weather.get('COIN', '☁️ SIDEWAYS')
        print("▶️ [🪙 코인] 매매 엔진 시작...")
        balances = rb.coin_broker.get_balances() or []
        krw_on_book, krw_bal = rb._compute_coin_krw_balances(balances)
        held_coins = rb._extract_held_coins_from_balances(balances)

        total_coin_equity = rb._compute_total_coin_equity_from_balances(balances, float(krw_on_book))
        krw_bal_snap = float(krw_bal)
        total_coin_equity_snap = float(total_coin_equity)

        state["circuit_aux_last_coin_krw"] = float(total_coin_equity)
        rb.save_state(rb.STATE_PATH, state)

        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        positions_count = rb._count_coin_positions_for_sell_loop(balances, state.get("positions", {}))
        print(f"  🔍 [코인 매도 루프] 보유 포지션 {positions_count}개 손익 체크 시작...")
        if positions_count == 0:
            print(f"  ✅ [코인 매도 루프] 매도할 종목 없음 (완료)")
        for b in rb._iter_coin_asset_rows(balances):
            t = rb.coin_broker.held_ticker_row(b)
            if not t:
                continue

            is_exit = False

            if t not in state.get("positions", {}):
                print(f"  ⏭️ {t}: 장부에 없음 (패스)")
                continue
            qty = float(rb._to_float(b.get('balance', 0)))
            if not rb.coin_broker.should_include_coin_balance_row(b):
                print(f"  ⏭️ {t}: 명목 최소 미만 또는 수량 부족 ({qty}) (패스)")
                continue
            curr_p = rb.coin_broker.get_current_price(t)
            if not curr_p:
                print(f"  ⏭️ {t}: 현재가 조회 실패 (패스)")
                continue
            ohlcv = rb.coin_broker.fetch_ohlcv(t, "day", 250)
            if not ohlcv or len(ohlcv) < 20:
                # OHLCV 실패 시 현재가로만 손절 체크
                print(f"  ⚠️  [{t}] OHLCV 데이터 부족, 현재가로 손절만 체크...")
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p)
                sl_p = float(pos_info.get('sl_p', buy_p * 0.9))
                profit_rate_now = rb._calc_profit_rate_pct(float(curr_p), float(buy_p))
            
                # max_p 갱신 (OHLCV 실패 시에도)
                old_max_p = pos_info.get('max_p', buy_p)
                pos_info['max_p'] = max(old_max_p, curr_p)
                if pos_info['max_p'] > old_max_p:
                    print(f"     📈 [{t}] max_p 업데이트: {old_max_p:,.0f} → {pos_info['max_p']:,.0f}")
                state.setdefault("positions", {})[t] = pos_info
                rb.save_state(rb.STATE_PATH, state)
            
                print(f"     📊 {t}: 현재가 {curr_p:,.0f}원 / 손절가 {sl_p:,.0f}원 / 수익률 {profit_rate_now:+.2f}%")
            pos_info = state.get("positions", {}).get(t, {})
            atr_val = rb.get_safe_atr(t, ohlcv)
            rb._update_position_current_atr_if_changed(state, t, pos_info, atr_val)
        
            # 🔄 [완전 동기화] GUI가 장부에 공유한 최신 가격을 최우선으로 사용
            curr_p = rb._resolve_curr_price_with_gui_override(pos_info, float(curr_p))
            # else: curr_p는 이미 위에서 rb.coin_broker.get_current_price로 가져옴
            strategy_type = rb._resolve_sell_loop_strategy_type(pos_info)
            rb._update_position_max_p(state, t, pos_info, float(curr_p))
            pos_info = state.get("positions", {}).get(t, pos_info)
            buy_p = pos_info.get('buy_p', curr_p)
            max_p = pos_info.get('max_p', curr_p)
            now_str, hours_held, trading_h, buy_time_log = rb._compute_holding_time_info(
                pos_info, "COIN"
            )
            profit_rate_now = rb._calc_profit_rate_pct(float(curr_p), float(buy_p))
            if len(ohlcv) < 20:
                exit_line = rb._calc_hard_stop(
                    pos_info,
                    float(buy_p),
                    ohlcv=ohlcv,
                    strategy_type=strategy_type,
                    ticker=t,
                    trading_hours_held=trading_h,
                )
            else:
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

            _exit_tag = "스윙" if strategy_type == "SWING_FIB" else "V8"
            _sw_suffix = (
                rb._format_swing_exit_log_suffix("COIN", pos_info, ohlcv, float(curr_p), float(buy_p))
                if strategy_type == "SWING_FIB" and len(ohlcv) >= 20
                else ""
            )
            curr_fmt, buy_fmt, max_fmt, chan_fmt, hard_fmt = rb._format_coin_price_log_fields(
                float(curr_p), float(buy_p), float(max_p), float(exit_line), float(hard_stop)
            )

            print(
                f"  📊 [COIN 보유] {t} | 현재가: {curr_fmt}원 | 매수가: {buy_fmt}원 | "
                f"최고가: {max_fmt}원 | 매도선({_exit_tag}): {chan_fmt}원 | "
                f"수익률: {profit_rate_now:+.2f}%{_sw_suffix}"
            )

            # 손절가 체크 로그
            if profit_rate_now < 0:
                print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: {curr_fmt} / 손절가: {hard_fmt})")
                if curr_p <= hard_stop:
                    print(f"     ➜ 손절 체크: 현재가 {curr_fmt} ≤ 손절가 {hard_fmt} = 🔴 매도 신호!")

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
                    market="COIN",
                    ticker=t,
                    reference_price=float(curr_p),
                    trading_hours_held=trading_h,
                )
                if sw_action == "HALF":
                    if rb.order_idem.lane_has_filled_sell(
                        state, "COIN", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag
                    ):
                        rb.order_idem.reconcile_ticker_lane(
                            state, "COIN", t, rb.order_idem.LANE_SWING_HALF, _buy_cycle_tag, rb.STATE_PATH
                        )
                        continue
                    sell_q = rb.compute_coin_scale_out_qty(float(qty), float(curr_p))
                    if not sell_q:
                        print(f"  ⏭️ [SWING-SELL] {t} HALF 수량 0 (패스)")
                        continue
                    fill_half = rb._idempotent_coin_sell(
                        state,
                        ticker=t,
                        lane=rb.order_idem.LANE_SWING_HALF,
                        qty=float(sell_q),
                        fallback_price=float(curr_p),
                        cycle_tag=_buy_cycle_tag,
                    )
                    if fill_half.ok:
                        new_half = rb.post_partial_ledger(
                            pos_info,
                            float(sell_q),
                            float(curr_p),
                            float(qty),
                            set_scale_out_done=True,
                        )
                        new_half["strategy_type"] = "SWING_FIB"
                        new_half["entry_fib_level"] = float(pos_info.get("entry_fib_level", 0.0) or 0.0)
                        rb.ledger_apply.persist_position_set(
                            state, t, new_half, context="SWING HALF COIN", state_path=rb.STATE_PATH
                        )
                        rb._record_trade_event(
                            "COIN",
                            t,
                            "SELL",
                            float(sell_q),
                            price=float(curr_p),
                            profit_rate=float(profit_rate_now),
                            reason=f"[SWING-SELL] {sw_reason}",
                        )
                        coin_nm = rb.get_coin_name(t)
                        print(f"  ✅ [SWING-SELL] {t}({coin_nm}) HALF | {sw_reason}")
                        _qty_lbl = (
                            f"{float(sell_q):.8f}".rstrip("0").rstrip(".")
                            + (" USDT" if rb.coin_config.is_binance() else "")
                        )
                        rb._telegram_swing_sell(
                            "COIN",
                            t,
                            name=coin_nm,
                            half=True,
                            qty_label=_qty_lbl,
                            profit_rate=float(profit_rate_now),
                            reason=sw_reason,
                        )
                    else:
                        print(f"  ❌ [SWING-SELL] {t} HALF 실패: {fill_half.note}")
                    continue
                if sw_action == "FULL":
                    fill_full = rb._idempotent_coin_sell(
                        state,
                        ticker=t,
                        lane=rb.order_idem.LANE_SWING_FULL,
                        qty=float(qty),
                        fallback_price=float(curr_p),
                        cycle_tag=_buy_cycle_tag,
                    )
                    if fill_full.ok:
                        p_full = (
                            (float(curr_p) - float(buy_p)) / float(buy_p) * 100
                            if float(buy_p) > 0
                            else 0.0
                        )
                        rb._record_trade_event(
                            "COIN",
                            t,
                            "SELL",
                            qty,
                            price=float(curr_p),
                            profit_rate=float(p_full),
                            reason=f"[SWING-SELL] {sw_reason}",
                        )
                        coin_nm = rb.get_coin_name(t)
                        print(f"  ✅ [SWING-SELL] {t}({coin_nm}) FULL | {sw_reason}")
                        _qty_lbl = (
                            f"{float(qty):.8f}".rstrip("0").rstrip(".")
                            + (" USDT" if rb.coin_config.is_binance() else "")
                        )
                        rb._telegram_swing_sell(
                            "COIN",
                            t,
                            name=coin_nm,
                            half=False,
                            qty_label=_qty_lbl,
                            profit_rate=float(p_full),
                            reason=sw_reason,
                        )
                        def _mut_coin_swing_full(st: dict) -> None:
                            rb.set_cooldown(st, t)
                            rb.set_ticker_cooldown_after_sell(
                                st,
                                t,
                                sw_reason,
                                profit_rate=float(p_full),
                                strategy_type="SWING_FIB",
                                market="COIN",
                                remaining_qty=0.0,
                            )

                        rb.ledger_apply.persist_position_remove(
                            state, t, context="SWING FULL COIN", state_path=rb.STATE_PATH, mutate_fn=_mut_coin_swing_full
                        )
                    else:
                        print(f"  ❌ [SWING-SELL] {t} FULL 실패: {fill_full.note}")
                    continue
                # HOLD: 절반 익절은 check_swing_exit 1.5R HALF만 — V8 Scale-Out 블록 진입 금지

            # V7.1: V8 분할 익절 1차(3×ATR)·2차(6×ATR) — TREND_V8 전용 (SWING_FIB 격리)
            if strategy_type == "TREND_V8":
                so_cont, pos_info = rb._try_v8_scale_out_coin(
                    state,
                    ticker=t,
                    pos_info=pos_info,
                    qty=float(qty),
                    buy_p=float(buy_p),
                    curr_p=float(curr_p),
                    profit_rate_now=float(profit_rate_now),
                    cycle_tag=_buy_cycle_tag,
                )
                if so_cont:
                    continue

            # 매도 결정 로직 (우선순위: 타임스탑 > 하드스탑 > 샹들리에)
            reason = ""

            rb._print_position_hold_status(
                now_str,
                t,
                buy_time_log,
                hours_held,
                trading_hours=trading_h,
                market="COIN",
            )

            ts_exit, ts_reason, ts_exempt = rb._evaluate_time_stop(
                market="COIN",
                strategy_type=strategy_type,
                hours_held=float(trading_h),
                profit_rate_now=float(profit_rate_now),
            )
            if ts_exit:
                is_exit = True
                reason = ts_reason
                print(f"  ⏰ {reason}")
            elif ts_exempt:
                _ts_tag, _ts_min_h, _ts_exempt_pct = rb._time_stop_params("COIN", strategy_type)
                print(
                    f"   ✅ 타임스탑 유예 {_ts_tag} — 보유 {trading_h:.1f}h (≥{_ts_min_h:.0f}h), "
                    f"수익률 {profit_rate_now:+.2f}% ≥ {_ts_exempt_pct:.1f}%"
                )

            # 2. 하드스탑 (SWING_FIB는 check_swing_exit 피보·구름 FULL 전담)
            if not is_exit and profit_rate_now < 0 and strategy_type != "SWING_FIB":
                if curr_p <= hard_stop:
                    is_exit = True
                    reason = "하드스탑 이탈 (손실구간 방어)"
                    print(f"🔴 [하드스탑 발동] {t} - 현재가: {curr_p:,.0f}원 <= 손절가: {hard_stop:,.0f}원. 강제 청산! (is_exit={is_exit})")

            # 3. 수익 구간 트레일링 (스윙: 수익 락만 / V8: 샹들리에)
            if not is_exit and profit_rate_now >= 0:
                if strategy_type == "SWING_FIB" and len(ohlcv) >= 20:
                    is_exit, reason = rb._check_swing_trailing_exit(
                        float(curr_p), pos_info, ohlcv, state, t
                    )
                elif strategy_type != "SWING_FIB":
                    is_exit, reason_chandelier = rb.decide_v8_exit(t, curr_p, pos_info, ohlcv)
                    if is_exit:
                        reason = reason_chandelier

            if is_exit: # 여기서 실제 매도 주문이 나감
                fill_exit = rb._idempotent_coin_sell(
                    state,
                    ticker=t,
                    lane=rb.order_idem.LANE_EXIT,
                    qty=float(qty),
                    fallback_price=float(curr_p),
                    cycle_tag=_buy_cycle_tag,
                )
                tag_exit = "♻️" if fill_exit.reused else "🧾"
                print(
                    f"  {tag_exit} [COIN EXIT] {t} ok={fill_exit.ok} qty={fill_exit.qty:.6f} "
                    f"note={fill_exit.note}"
                )

                if fill_exit.ok:
                    profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                
                    if profit_rate > 0:
                        stats["wins"] = int(stats.get("wins", 0)) + 1
                    else:
                        stats["losses"] += 1
                    
                    stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                
                    rb._record_trade_event("COIN", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                
                    coin_name = rb.get_coin_name(t)
                    print(f"  ✅ [코인 매도 체결] {t}({coin_name}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                    if strategy_type == "SWING_FIB":
                        rb.send_telegram(
                            f"🚨 [코인 스윙 청산] {t}({coin_name})\n"
                            f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                        )
                    else:
                        rb.send_telegram(
                            f"🚨 [코인 추세종료 매도] {t}({coin_name})\n"
                            f"사유: {reason}\n최종 수익률: {profit_rate:+.2f}%"
                        )
                    def _mut_coin_exit(st: dict) -> None:
                        rb.set_cooldown(st, t)
                        rb.set_ticker_cooldown_after_sell(
                            st,
                            t,
                            reason,
                            profit_rate=float(profit_rate),
                            strategy_type=strategy_type,
                            market="COIN",
                            remaining_qty=0.0,
                        )

                    rb.ledger_apply.persist_position_remove(
                        state, t, context="COIN EXIT", state_path=rb.STATE_PATH, mutate_fn=_mut_coin_exit
                    )

                else: # 3번 모두 실패했다면
                    print(f"  ❌ {t} 매도 최종 실패 ({retry_count}회 시도): 거래소 API 오류")

        balances = rb.coin_broker.get_balances() or []
        krw_on_book, krw_bal = rb._compute_coin_krw_balances(balances)
        held_coins = rb._extract_held_coins_from_balances(balances)
        total_coin_equity = rb._compute_total_coin_equity_from_balances(balances, float(krw_on_book))
        state["circuit_aux_last_coin_krw"] = float(total_coin_equity)
        rb.save_state(rb.STATE_PATH, state)
        if (
            abs(float(krw_bal) - krw_bal_snap) >= 100.0
            or abs(float(total_coin_equity) - total_coin_equity_snap) >= 3000.0
        ):
            print(
                f"  📌 [COIN] 매도 후 잔고 갱신 → 주문가능 약 {float(krw_bal):,.0f}원 · "
                f"총평가 {float(total_coin_equity):,.0f}원 (매수·비중·보유패스 기준)"
            )

        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not rb.check_mdd_break("COIN", total_coin_equity, state, rb.STATE_PATH):
            print("  -> 🚨 코인 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 코인 Phase4 거시 방어막: 신규 매수 중단. ({macro_reason})")
        elif rb.in_account_circuit_cooldown(state, "COIN"):
            print("  -> 🚨 코인 Phase5 비중 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # 업비트·바이낸스 동일: KST 일봉(09:00) 직전 N분 창 — 국·미 ``마감 직전 창`` 과 같은 패턴.
            now_coin = datetime.now(pytz.timezone("Asia/Seoul"))
            is_coin_buy_time, _coin_buy_start, _coin_close = rb._is_coin_buy_window_now(now_coin)
            skip_buy = not is_coin_buy_time
            if skip_buy:
                _coin_sched_tag = "BINANCE" if rb.coin_config.is_binance() else "COIN"
                print(
                    f"  ⏳ [{_coin_sched_tag} 매수 대기] 일봉 기준점 직전 {rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE}분만 매수 "
                    f"({_coin_buy_start.strftime('%H:%M')}~{_coin_close.strftime('%H:%M')} KST, "
                    f"현재 {now_coin.strftime('%H:%M')})"
                )

            if not skip_buy:
                ctx.buy_zone_coin = True
                if not rb._macro_market_buy_allowed(macro_snap, "COIN"):
                    print(
                        f"  -> 🚨 코인 Phase4 글로벌 방어막: 신규 매수 중단. "
                        f"({(macro_snap.get('market_buy_block_reason') or {}).get('COIN', '')})"
                    )
                else:
                    # 지수 급락 체크
                    coin_index_change = rb.get_market_index_change("COIN")
                    print(f"  📊 [BTC 지수] 변화율: {coin_index_change:+.2f}% 날씨는 {coin_weather}")
                    if coin_index_change <= rb.INDEX_CRASH_COIN:
                        print(f"  🚫 [COIN 매수 중단] BTC {coin_index_change:+.2f}% 급락 (기준: {rb.INDEX_CRASH_COIN}%)")
                    else:
                        if coin_weather == rb.WEATHER_LABEL_BEAR:
                            print(
                                "  📌 [COIN] BEAR 날씨 — V8 추세 매수만 중단, SWING_FIB 스윙 후보는 계속 분석"
                            )
                        try:
                            if rb.coin_config.is_binance():
                                from api import binance_api as _bna

                                scan_targets = _bna.top_usdt_symbols_by_quote_volume(rb.BINANCE_UNIVERSE_TOP)
                                _ohlcv_pref = rb.coin_broker.run_prefetch_daily_sync(scan_targets, 250)
                            else:
                                scan_targets = []
                                markets = [
                                    m["market"]
                                    for m in rb.requests.get(
                                        "https://api.upbit.com/v1/market/all", timeout=10
                                    ).json()
                                    if m.get("market", "").startswith("KRW-")
                                ]
                                tickers_data = rb.requests.get(
                                    "https://api.upbit.com/v1/ticker?markets=" + ",".join(markets),
                                    timeout=10,
                                ).json()
                                # 업비트 KRW 마켓의 USD/원화 페그 스테이블(KRW-USDT, KRW-DAI 등) 차단:
                                # 1) base 심볼이 알려진 스테이블이면 제외
                                # 2) 24h 고저 스프레드(=`high_price`/`low_price`) 가 종가의 0.5% 미만이면 제외
                                _UPBIT_STABLE_BASES = {
                                    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD",
                                    "USDS", "USDD", "USDE", "PYUSD",
                                }

                                def _upbit_skip(t: dict) -> bool:
                                    try:
                                        mkt = str(t.get("market", "") or "")
                                        if not mkt.startswith("KRW-"):
                                            return True
                                        base = mkt.split("-", 1)[1].upper()
                                        if base in _UPBIT_STABLE_BASES:
                                            return True
                                        last = float(t.get("trade_price") or 0)
                                        high = float(t.get("high_price") or 0)
                                        low = float(t.get("low_price") or 0)
                                        if last > 0 and high > 0 and low > 0:
                                            if (high - low) / last < 0.005:
                                                return True
                                    except Exception:
                                        return False
                                    return False

                                tickers_data = [t for t in tickers_data if not _upbit_skip(t)]
                                scan_targets = [
                                    x["market"]
                                    for x in sorted(
                                        tickers_data, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True
                                    )[: max(1, rb.UPBIT_UNIVERSE_TOP)]
                                ]
                                _ohlcv_pref = {}
                        except Exception:
                            scan_targets = []
                            _ohlcv_pref = {}

                        scan_targets = rb._sort_buy_targets_by_rs(scan_targets, "COIN")

                        print(
                            f"  -> 🪙 코인 실시간 수급 상위 {len(scan_targets)}개 정밀 분석 시작! "
                            f"(매수 판단: V8→스윙, 국·미장과 동일)"
                        )
                        for idx, t in enumerate(scan_targets, 1):
                            if rb.in_ticker_cooldown(state, t):
                                print(
                                    f"  ⏭️ {t}: 매도 후 쿨다운(톱날 방지) 만료 "
                                    f"{rb.ticker_cooldown_human(state, t)} 이전 (패스)"
                                )
                                continue
                            if rb.in_cooldown(state, t):
                                print(f"  ⏭️ {t}: 쿨다운 중 (패스)")
                                continue
                            if t in held_coins:
                                print(f"  ⏭️ {rb.get_coin_name(t)}({t}): 이미 보유중 (패스)")
                                continue
                            if rb.order_idem.is_buy_inflight(state, "COIN", t, _buy_cycle_tag):
                                print(f"  ⏭️ {rb.get_coin_name(t)}({t}): 매수 TWAP 진행 중(멱등 패스)")
                                continue
                            ohlcv = _ohlcv_pref.get(t) if isinstance(_ohlcv_pref, dict) else None
                            if not ohlcv or len(ohlcv) < 20:
                                ohlcv = rb.coin_broker.fetch_ohlcv(t, "day", 250)
                            if not ohlcv or len(ohlcv) < 20:
                                print(f"  ⏭️ {t}: OHLCV 데이터 부족 (패스)")
                                continue

                            strategy_type = "TREND_V8"
                            entry_fib_level = 0.0
                            live_px_coin = float(rb.coin_broker.get_current_price(t) or 0.0)
                            entry_decision = rb.decide_entry_signals(
                                ohlcv,
                                coin_weather,
                                t,
                                rb.get_coin_name(t),
                                idx,
                                len(scan_targets),
                                market="COIN",
                                reference_close=live_px_coin if live_px_coin > 0 else None,
                            )
                            is_buy = entry_decision.is_buy
                            sl_p = entry_decision.sl_p
                            s_name = entry_decision.signal_name
                            v8_ok = bool(is_buy) and rb._v8_trend_buy_allowed_in_weather(coin_weather)
                            if bool(is_buy) and not v8_ok:
                                print(
                                    f"  ⏭️ {t}: BEAR 시장 — V8 신호 통과했으나 추세 매수 차단 (스윙만 허용)"
                                )
                            if v8_ok:
                                print(f"  ✅ [V8-BUY] {t} 진입")
                            else:
                                sw_ok = entry_decision.swing_ok
                                sw_fib = entry_decision.swing_fib
                                sw_why = entry_decision.swing_why
                                if sw_ok:
                                    strategy_type = "SWING_FIB"
                                    entry_fib_level = float(sw_fib)
                                    _sw_o = float(ohlcv[-1].get("o", 0) or 0)
                                    _sw_c = live_px_coin if live_px_coin > 0 else float(ohlcv[-1].get("c", 0) or 0)
                                    sl_p = rb.swing_entry_sl_p(_sw_c, sw_fib)
                                    s_name = "SWING_FIB"
                                    _sw_src = "실시간" if live_px_coin > 0 else "일봉종가"
                                    print(
                                        f"  ✅ [SWING-BUY] {t} entry_fib={entry_fib_level:,.2f} "
                                        f"| 양봉({_sw_src} 시가 {_sw_o:,.0f} < 종가 {_sw_c:,.0f})"
                                        f"{' | BEAR 시장 스윙 예외' if coin_weather == rb.WEATHER_LABEL_BEAR else ''}"
                                    )
                                else:
                                    _cn = rb.get_coin_name(t)
                                    _prog = f"[{idx}/{len(scan_targets)}]" if scan_targets else ""
                                    _disp = f"{_cn}({t})" if _cn and _cn != t else t
                                    print(f"   🔍 [스윙] {_prog} {_disp} ❌ 패스: {sw_why}")
                                    continue

                            base_ratio = 1.0 / max(1, int(rb.MAX_POSITIONS_COIN))
                            ratio, t_name = rb._position_ratio_with_vol_target(
                                base_ratio,
                                ohlcv,
                                target_vol=_alpha_target_vol,
                                ticker=t,
                            )

                            _prospect_atr = rb.atr_pct_from_ohlcv(ohlcv, t)
                            _mkt_heat, _heat_blocked = rb._portfolio_heat_snapshot(
                                state,
                                "COIN",
                                total_coin_equity,
                                lambda tk, _cb=rb.coin_broker: _cb.fetch_ohlcv(tk, "day", 200),
                                extra_weight=float(ratio),
                                extra_atr_pct=float(_prospect_atr or 0.0),
                            )
                            if _heat_blocked:
                                rb._log_portfolio_heat_block("COIN", _mkt_heat, prospective=True)
                                continue

                            budget = total_coin_equity * ratio * macro_mult
                            coin_min_budget = rb._coin_min_order_krw()

                            if budget < coin_min_budget:
                                print(
                                    f"  ⏭️ {t}: [COIN 예산 부족] 배정예산 {int(budget):,}원 < "
                                    f"최소 {int(coin_min_budget):,}원 (총평가 {int(total_coin_equity):,}원×비중·macro, 주문가능 {int(krw_bal):,}원)"
                                )
                                continue
                            if krw_bal < coin_min_budget:
                                print(
                                    f"  ⏭️ {t}: [COIN 예수금 부족] 주문가능 {int(krw_bal):,}원 < 최소 {int(coin_min_budget):,}원 — 매수 불가"
                                )
                                continue
                            if krw_bal < budget:
                                print(f"  🧹 [코인 영끌 발동] {t}: 예산({int(budget):,}원) 부족. 지갑에 남은 전액({int(krw_bal):,}원) 풀매수 장전!")
                                budget = krw_bal

                            if not rb.can_open_new(t, state, max_positions=rb.MAX_POSITIONS_COIN):
                                print(f"  ⏭️ {t}: 포지션 개수 초과 ({rb.MAX_POSITIONS_COIN}개) (패스)")
                                continue

                            if budget < coin_min_budget:
                                print(
                                    f"  ⏭️ {t}: [COIN 예산 부족] 영끌 후 {int(budget):,}원 < 최소 {int(coin_min_budget):,}원 (패스)"
                                )
                                continue

                            if not rb._ai_false_breakout_buy_gate(
                                t,
                                "COIN",
                                strategy_type,
                                rb.AI_FALSE_BREAKOUT_THRESHOLD_COIN,
                                f"{rb.get_coin_name(t)}({t})",
                            ):
                                continue

                            krw_box = [float(krw_bal)]
                            entry_atr = float(rb.get_safe_atr(t, ohlcv) or 0.0)
                            ok_coin_buy = rb._execute_coin_market_buy_twap(
                                t,
                                float(budget),
                                sl_p,
                                entry_atr,
                                s_name,
                                state,
                                krw_box,
                                held_coins,
                                strategy_type=strategy_type,
                                entry_fib_level=entry_fib_level,
                            )
                            if not ok_coin_buy:
                                print(
                                    f"  ⏭️ {t}: [COIN 매수 미체결] 시그널·필터 통과 후 주문 없음 — "
                                    f"예산 {int(budget):,}원, 주문가능 추정 {int(krw_box[0]):,}원 (TWAP·최소주문·업비트 응답 확인)"
                                )
                            else:
                                ctx.buy_fills += 1
                                rb._register_swing_risk_after_buy(state, t, ohlcv, "COIN")
                            krw_bal = float(krw_box[0])
    else:
        print("💤 코인은 점검 또는 데이터 조회 불가 상태입니다.")
