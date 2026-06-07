# -*- coding: utf-8 -*-
"""시장별 TWAP 매수 실행 — ``run_bot._execute_*_market_buy_twap`` 분리 (로직 동일)."""
from __future__ import annotations

import time

from execution.order_twap import plan_krw_slices, plan_usd_slices


def _rb():
    import run_bot as rb

    return rb


def twap_krw_budget_slices(total_krw: float) -> list:
    rb = _rb()
    if not rb.TWAP_ENABLED:
        return [float(total_krw)]
    return plan_krw_slices(float(total_krw), threshold_krw=rb.TWAP_KRW_THRESHOLD)


def twap_usd_budget_slices(total_usd: float) -> list:
    rb = _rb()
    if not rb.TWAP_ENABLED:
        return [float(total_usd)]
    return plan_usd_slices(float(total_usd), threshold_usd=rb.TWAP_USD_THRESHOLD)


def execute_kr_market_buy_twap(
    t: str,
    kr_name: str,
    target_budget: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    kr_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    """시장가 매수(Phase2 분할). 성공 시 장부 1회 등록. rb.TEST_MODE 시 로그만."""
    rb = _rb()
    cycle_tag = rb.order_idem.cycle_tag_15m_kst()
    if not rb.order_idem.try_acquire_buy_inflight(state, "KR", t, cycle_tag):
        print(f"  ⏭️ [KR TWAP] {kr_name}({t}): 동일 사이클 매수 진행 중(멱등)")
        return False

    slices = twap_krw_budget_slices(target_budget)
    if len(slices) > 1:
        print(
            f"  📉 [Phase2 TWAP KR] {kr_name}({t}) 예산 {int(target_budget):,}원 → {len(slices)}분할 "
            f"(잔여예수 추정 {int(kr_cash_holder[0]):,}원)"
        )

    total_qty = 0
    total_cost = 0.0
    fp = float(curr_p)
    any_fill = False
    qty_before = None
    if not rb.TEST_MODE:
        try:
            qty_before = rb.bal_read.kr_stock_qty(t, refresh=False)
        except Exception:
            qty_before = None

    try:
        for si, krw_slice in enumerate(slices):
            if krw_slice <= 0 or fp <= 0:
                continue
            q = int(float(krw_slice) / fp)
            if q <= 0:
                print(
                    f"  ⏭️ [KR TWAP] 슬라이스 {si + 1}/{len(slices)} 정수주 0 — "
                    f"액면 {int(krw_slice):,}원 < 1주 기준(~{int(fp):,}원)"
                )
                continue
            est = int(q * fp)
            if int(kr_cash_holder[0]) < est:
                print(f"  ⏭️ [KR TWAP] 슬라이스 {si + 1}/{len(slices)} 예수 부족으로 중단")
                break

            if rb.TEST_MODE:
                rb.send_telegram(f"🧪 rb.TEST_MODE KR TWAP {t} ({kr_name}) {si + 1}/{len(slices)} qty={q}")

            def _kr_place():
                return rb.create_market_buy_order_kis(t, q, is_us=False, curr_price=fp)

            def _kr_qty_now():
                try:
                    return rb.bal_read.kr_stock_qty(t, refresh=True)
                except Exception:
                    return None

            fill = rb.order_idem.run_kis_buy_slice_idempotent(
                state,
                market="KR",
                ticker=t,
                slice_index=si,
                qty=q,
                cycle_tag=cycle_tag,
                place_order=_kr_place,
                fallback_price=fp,
                balance_qty_fn=None if rb.TEST_MODE else _kr_qty_now,
                qty_before=qty_before,
                test_mode=rb.TEST_MODE,
            )

            if not fill.ok and not rb.TEST_MODE:
                msg_l = str(fill.note or "").lower()
                if "credentials" in msg_l or "token" in msg_l:
                    print("  🔄 [토큰 오류] 토큰 갱신 후 TWAP 슬라이스 1회 재시도...")
                    rb.refresh_brokers_if_needed(force=True)
                    time.sleep(1)
                    rb.order_idem.pop_order_record(
                        state,
                        rb.order_idem.order_key("KR", t, "buy", cycle_tag, si),
                    )
                    fill = rb.order_idem.run_kis_buy_slice_idempotent(
                        state,
                        market="KR",
                        ticker=t,
                        slice_index=si,
                        qty=q,
                        cycle_tag=cycle_tag,
                        place_order=_kr_place,
                        fallback_price=fp,
                        balance_qty_fn=_kr_qty_now,
                        qty_before=qty_before,
                    )

            tag = "♻️" if fill.reused else "🧾"
            print(
                f"  {tag} [KR BUY TWAP {si + 1}/{len(slices)}] {t} "
                f"ok={fill.ok} qty={int(fill.qty)} note={fill.note}"
            )

            if not fill.ok:
                print(f"  ❌ [KR TWAP] {kr_name}({t}) 슬라이스 {si + 1} 최종 실패: {fill.note}")
                if not rb.TEST_MODE:
                    rb.order_idem.persist_idempotency(state, rb.STATE_PATH)
                break

            fp = float(fill.price) if fill.price > 0 else fp
            total_qty += int(fill.qty)
            total_cost += float(fill.qty) * fp
            kr_cash_holder[0] = float(int(kr_cash_holder[0]) - int(fill.qty * fp))
            any_fill = True
            if not rb.TEST_MODE:
                qty_before = rb.bal_read.kr_stock_qty(t, refresh=True)

            if si < len(slices) - 1 and rb.TWAP_SLICE_DELAY_SEC > 0:
                time.sleep(rb.TWAP_SLICE_DELAY_SEC)
    finally:
        rb.order_idem.release_buy_inflight(state, "KR", t, cycle_tag)

    if not any_fill or total_qty <= 0:
        return False

    wavg = total_cost / total_qty if total_qty else fp
    print(f"  ✅ [국장 매수 체결 TWAP] {kr_name}({t}) | 가중평단 ~{int(wavg):,}원 × {total_qty}주 | 손절가: {int(sl_p):,}원")
    rb.send_telegram(
        f"🎯 [{t_name} 매수 TWAP] {t}({kr_name})\n가중평단: ~{int(wavg):,}원 × {total_qty}주 | 손절가: {int(sl_p):,}원\n전략: {s_name}"
    )
    payload = {
        "buy_p": wavg,
        "sl_p": sl_p,
        "max_p": wavg,
        "tier": s_name,
        "buy_time": time.time(),
        "qty": float(total_qty),
        "entry_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "current_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "strategy_type": str(strategy_type or "TREND_V8"),
        "entry_fib_level": float(entry_fib_level or 0.0),
        "scale_out_done": False,
    }
    rb.persist_position_registration(state, t, payload, context="KR BUY TWAP")
    try:
        rb._record_trade_event(
            "KR", t, "BUY", total_qty, price=wavg, profit_rate=None, reason=s_name, ledger=payload
        )
    except Exception as log_err:
        print(f"  ⚠️ [KR BUY TWAP] 매매내역 기록 실패: {log_err}")
    rb.ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="KR BUY TWAP")
    return True


def execute_us_market_buy_twap(
    t: str,
    us_name: str,
    target_budget_usd: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    us_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    rb = _rb()
    cycle_tag = rb.order_idem.cycle_tag_15m_kst()
    if not rb.order_idem.try_acquire_buy_inflight(state, "US", t, cycle_tag):
        print(f"  ⏭️ [US TWAP] {us_name}({t}): 동일 사이클 매수 진행 중(멱등)")
        return False

    slices = twap_usd_budget_slices(target_budget_usd)
    if len(slices) > 1:
        print(
            f"  📉 [Phase2 TWAP US] {us_name}({t}) 예산 ${target_budget_usd:,.2f} → {len(slices)}분할 "
            f"(현금 ${us_cash_holder[0]:.2f})"
        )

    total_qty = 0
    total_cost = 0.0
    fp = float(curr_p)
    any_fill = False
    qty_before = None
    if not rb.TEST_MODE:
        try:
            qty_before = rb.bal_read.us_stock_qty(t, refresh=False)
        except Exception:
            qty_before = None

    try:
        for si, usd_slice in enumerate(slices):
            if usd_slice <= 0 or fp <= 0:
                continue
            q = int(float(usd_slice) / fp)
            if q <= 0:
                print(
                    f"  ⏭️ [US TWAP] 슬라이스 {si + 1}/{len(slices)} 정수주 0 — "
                    f"${float(usd_slice):.2f} < 1주 기준(~${fp:.2f})"
                )
                continue
            buy_price = round(fp * 1.01, 2)
            est = q * fp
            if us_cash_holder[0] < est * 0.99:
                print(f"  ⏭️ [US TWAP] 슬라이스 {si + 1}/{len(slices)} 달러 예수 부족으로 중단")
                break

            if rb.TEST_MODE:
                rb.send_telegram(f"🧪 rb.TEST_MODE US TWAP {t} ({us_name}) {si + 1}/{len(slices)} qty={q}")

            def _us_place():
                return rb.execute_us_order_direct(rb.kis_api.broker_us, "buy", t, q, buy_price)

            def _us_qty_now():
                try:
                    return rb.bal_read.us_stock_qty(t, refresh=True)
                except Exception:
                    return None

            fill = rb.order_idem.run_kis_buy_slice_idempotent(
                state,
                market="US",
                ticker=t,
                slice_index=si,
                qty=q,
                cycle_tag=cycle_tag,
                place_order=_us_place,
                fallback_price=fp,
                balance_qty_fn=None if rb.TEST_MODE else _us_qty_now,
                qty_before=qty_before,
                test_mode=rb.TEST_MODE,
            )

            if not fill.ok and not rb.TEST_MODE:
                msg_l = str(fill.note or "").lower()
                if "credentials" in msg_l or "token" in msg_l:
                    print("  🔄 [토큰 오류] 미장 TWAP 슬라이스 1회 재시도...")
                    rb.refresh_brokers_if_needed(force=True)
                    time.sleep(1)
                    rb.order_idem.pop_order_record(
                        state,
                        rb.order_idem.order_key("US", t, "buy", cycle_tag, si),
                    )
                    fill = rb.order_idem.run_kis_buy_slice_idempotent(
                        state,
                        market="US",
                        ticker=t,
                        slice_index=si,
                        qty=q,
                        cycle_tag=cycle_tag,
                        place_order=_us_place,
                        fallback_price=fp,
                        balance_qty_fn=_us_qty_now,
                        qty_before=qty_before,
                    )

            tag = "♻️" if fill.reused else "🧾"
            print(
                f"  {tag} [US BUY TWAP {si + 1}/{len(slices)}] {t} "
                f"ok={fill.ok} qty={int(fill.qty)} note={fill.note}"
            )

            if not fill.ok:
                print(f"  ❌ [US TWAP] {us_name}({t}) 슬라이스 실패: {fill.note}")
                if not rb.TEST_MODE:
                    rb.order_idem.persist_idempotency(state, rb.STATE_PATH)
                break

            fp = float(fill.price) if fill.price > 0 else fp
            total_qty += int(fill.qty)
            total_cost += float(fill.qty) * fp
            us_cash_holder[0] = float(us_cash_holder[0] - float(fill.qty) * fp)
            any_fill = True
            if not rb.TEST_MODE:
                qty_before = rb.bal_read.us_stock_qty(t, refresh=True)

            if si < len(slices) - 1 and rb.TWAP_SLICE_DELAY_SEC > 0:
                time.sleep(rb.TWAP_SLICE_DELAY_SEC)
    finally:
        rb.order_idem.release_buy_inflight(state, "US", t, cycle_tag)

    if not any_fill or total_qty <= 0:
        return False

    wavg = total_cost / total_qty if total_qty else fp
    print(f"  ✅ [미장 매수 체결 TWAP] {us_name}({t}) | ~${wavg:.2f} × {total_qty}주 | 손절: ${sl_p:.2f}")
    rb.send_telegram(f"🎯 [S&P500 매수 TWAP] {t}({us_name})\n가중평단: ~${wavg:.2f} × {total_qty}주\n전략: {s_name}")
    payload = {
        "buy_p": wavg,
        "sl_p": sl_p,
        "max_p": wavg,
        "tier": s_name,
        "buy_time": time.time(),
        "qty": float(total_qty),
        "entry_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "current_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "strategy_type": str(strategy_type or "TREND_V8"),
        "entry_fib_level": float(entry_fib_level or 0.0),
        "scale_out_done": False,
    }
    rb.persist_position_registration(state, t, payload, context="US BUY TWAP")
    try:
        rb._record_trade_event(
            "US", t, "BUY", total_qty, price=wavg, profit_rate=None, reason=s_name, ledger=payload
        )
    except Exception as log_err:
        print(f"  ⚠️ [US BUY TWAP] 매매내역 기록 실패: {log_err}")
    rb.ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="US BUY TWAP")
    return True


def execute_coin_market_buy_twap(
    t: str,
    budget_krw: float,
    sl_p: float,
    entry_atr: float,
    s_name: str,
    state: dict,
    krw_bal_holder: list,
    held_coins_mut: list[str],
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    rb = _rb()
    cycle_tag = rb.order_idem.cycle_tag_15m_kst()
    if not rb.order_idem.try_acquire_buy_inflight(state, "COIN", t, cycle_tag):
        print(f"  ⏭️ [COIN TWAP] {t}: 동일 사이클 매수 진행 중(멱등)")
        return False

    slices = twap_krw_budget_slices(budget_krw)
    if len(slices) > 1:
        print(f"  📉 [Phase2 TWAP COIN] {t} 예산 {int(budget_krw):,}원 → {len(slices)}분할")

    spent = 0.0
    filled_base_qty = 0.0
    last_p = float(rb.coin_broker.get_current_price(t) or 0.0)
    any_fill = False
    _min_krw = rb._coin_min_order_krw()
    base_before = None
    if not rb.TEST_MODE:
        try:
            base_before = rb.bal_read.coin_stock_qty(t, refresh=False)
        except Exception:
            base_before = None

    def _coin_qty_now():
        try:
            return rb.bal_read.coin_stock_qty(t, refresh=True)
        except Exception:
            return None

    try:
        for si, krw_slice in enumerate(slices):
            if krw_slice <= 0:
                continue
            if krw_bal_holder[0] < float(krw_slice):
                print(f"  ⏭️ [COIN TWAP] 슬라이스 {si + 1}/{len(slices)} 예산(원화환산) 부족으로 중단")
                break

            if last_p <= 0:
                last_p = float(rb.coin_broker.get_current_price(t) or 0.0)
            if last_p <= 0:
                print(f"  ⏭️ [COIN TWAP] {t}: 현재가 없음 — 슬라이스 중단")
                break

            target_buy_amount = float(min(float(krw_slice), float(krw_bal_holder[0])))
            pay_krw = float(target_buy_amount)

            if not rb.TEST_MODE:
                avail_raw = rb.coin_broker.get_quote_balance_direct()
                if rb.coin_config.is_binance():
                    kpx = float(rb.coin_broker.get_krw_per_usdt() or 0.0) or 1.0
                    available_krw = float(avail_raw or 0) * kpx
                else:
                    available_krw = (
                        float(avail_raw) if avail_raw is not None else float(krw_bal_holder[0])
                    )
                cap_ratio = rb.UPBIT_KRW_AVAILABLE_CAP_RATIO
                safe_ceiling = available_krw * cap_ratio
                pay_krw = float(max(0, min(target_buy_amount, safe_ceiling)))
                if pay_krw < _min_krw:
                    exn = "바이낸스(USDT×환율)" if rb.coin_config.is_binance() else "업비트"
                    print(
                        f"  ⏭️ [COIN TWAP] 슬라이스 {si + 1}/{len(slices)} 스킵 — "
                        f"최종주문액 {pay_krw:,.0f}원 < 최소 {int(_min_krw):,}원 ({exn}) "
                        f"(목표 {target_buy_amount:,.0f}원, 가용·API {available_krw:,.0f}원×{cap_ratio})"
                    )
                    break
                if pay_krw < int(target_buy_amount):
                    print(
                        f"  🛡️ [COIN TWAP] 가용 캡 적용: 목표 {target_buy_amount:,.0f}원 → 최종 {pay_krw:,.0f}원 "
                        f"(가용 {available_krw:,.0f}원×{cap_ratio})"
                    )

            if rb.TEST_MODE:
                if rb.coin_config.is_binance():
                    kpx = float(rb.coin_broker.get_krw_per_usdt() or 0.0) or 1.0
                    usdt_s = float(krw_slice) / kpx
                    rb.send_telegram(
                        f"🧪 rb.TEST_MODE COIN TWAP {t} {si + 1}/{len(slices)} {usdt_s:,.2f} USDT"
                    )
                else:
                    rb.send_telegram(
                        f"🧪 rb.TEST_MODE COIN TWAP {t} {si + 1}/{len(slices)} {int(krw_slice):,}KRW"
                    )

            if rb.coin_config.is_binance():
                kpx = float(rb.coin_broker.get_krw_per_usdt() or 0.0) or 1.0
                spend_usdt = pay_krw / kpx

                def _bn_place(cid: str):
                    return rb.coin_broker.buy_market_budget_krw(
                        t, pay_krw, new_client_order_id=cid
                    )

                fill = rb.order_idem.run_binance_buy_idempotent(
                    state,
                    market="COIN",
                    ticker=t,
                    slice_index=si,
                    cycle_tag=cycle_tag,
                    spend_usdt=spend_usdt,
                    place_order=_bn_place,
                    fallback_price=last_p,
                    test_mode=rb.TEST_MODE,
                )
            else:
                pay_slice = float(krw_slice) if rb.TEST_MODE else pay_krw

                def _up_place():
                    if rb.upbit_api.upbit is None:
                        return None
                    return rb.upbit_api.upbit.buy_market_order(t, int(max(0, pay_slice)))

                fill = rb.order_idem.run_upbit_buy_slice_idempotent(
                    state,
                    market="COIN",
                    ticker=t,
                    slice_index=si,
                    pay_krw=pay_slice,
                    cycle_tag=cycle_tag,
                    place_order=_up_place,
                    fallback_price=last_p,
                    balance_qty_fn=None if rb.TEST_MODE else _coin_qty_now,
                    qty_before=base_before,
                    test_mode=rb.TEST_MODE,
                )

            tag = "♻️" if fill.reused else "🧾"
            print(
                f"  {tag} [COIN BUY TWAP {si + 1}/{len(slices)}] {t} "
                f"ok={fill.ok} qty={fill.qty:.6f} note={fill.note}"
            )

            if not fill.ok:
                if not rb.TEST_MODE:
                    print(
                        f"  ❌ [COIN TWAP] {t} 슬라이스 실패 — 거절(잔고·최소주문·수수료). "
                        f"가용·최소주문·거래소 키를 확인하세요."
                    )
                    rb.order_idem.persist_idempotency(state, rb.STATE_PATH)
                break

            slice_spent = float(krw_slice) if rb.TEST_MODE else pay_krw
            spent += slice_spent
            if float(fill.qty) > 0:
                filled_base_qty += float(fill.qty)
            else:
                filled_base_qty += rb._coin_twap_filled_base_qty(None, slice_spent, t, last_p)
            if float(fill.price) > 0:
                last_p = float(fill.price)

            if rb.TEST_MODE:
                krw_bal_holder[0] = float(krw_bal_holder[0]) - slice_spent
            else:
                after_raw = rb.coin_broker.get_quote_balance_direct()
                if rb.coin_config.is_binance():
                    kpx = float(rb.coin_broker.get_krw_per_usdt() or 0.0) or 1.0
                    krw_bal_holder[0] = float(after_raw or 0) * kpx
                else:
                    krw_bal_holder[0] = (
                        float(after_raw)
                        if after_raw is not None
                        else float(krw_bal_holder[0]) - slice_spent
                    )
                qn = _coin_qty_now()
                if qn is not None:
                    base_before = qn
                np = rb.coin_broker.get_current_price(t)
                if np:
                    last_p = float(np)

            any_fill = True

            if si < len(slices) - 1 and rb.TWAP_SLICE_DELAY_SEC > 0:
                time.sleep(rb.TWAP_SLICE_DELAY_SEC)
    finally:
        rb.order_idem.release_buy_inflight(state, "COIN", t, cycle_tag)

    if not any_fill or spent <= 0 or last_p <= 0:
        return False

    coin_qty = float(filled_base_qty) if filled_base_qty > 0 else rb._coin_twap_filled_base_qty(None, spent, t, last_p)
    coin_name = rb.get_coin_name(t)
    if rb.coin_config.is_binance():
        p_fmt = rb._fmt_telegram_coin_unit_usdt(last_p)
        sl_fmt = rb._fmt_telegram_coin_unit_usdt(sl_p)
        print(f"  ✅ [코인 매수 체결 TWAP] {t}({coin_name}) | {p_fmt} × {coin_qty:.4f} | 손절가: {sl_fmt}")
        rb.send_telegram(
            f"🎯 [코인 TWAP 매수] {t}({coin_name})\n평단: {p_fmt} × {coin_qty:.4f} | 손절: {sl_fmt}\n전략: {s_name}"
        )
    else:
        p_fmt = f"{last_p:,.4f}" if last_p < 100 else f"{int(last_p):,}"
        sl_fmt = f"{sl_p:,.4f}" if sl_p < 100 else f"{int(sl_p):,}"
        print(f"  ✅ [코인 매수 체결 TWAP] {t}({coin_name}) | {p_fmt}원 × {coin_qty:.4f} | 손절가: {sl_fmt}원")
        rb.send_telegram(
            f"🎯 [코인 TWAP 매수] {t}({coin_name})\n평단: {p_fmt}원 × {coin_qty:.4f} | 손절: {sl_fmt}원\n전략: {s_name}"
        )
    payload = {
        "buy_p": last_p,
        "sl_p": sl_p,
        "max_p": last_p,
        "tier": s_name,
        "buy_time": time.time(),
        "qty": float(coin_qty),
        "entry_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "current_atr": float(entry_atr) if float(entry_atr or 0) > 0 else 0.0,
        "strategy_type": str(strategy_type or "TREND_V8"),
        "entry_fib_level": float(entry_fib_level or 0.0),
        "scale_out_done": False,
    }
    rb.persist_position_registration(state, t, payload, context="COIN BUY TWAP")
    try:
        rb._record_trade_event(
            "COIN", t, "BUY", coin_qty, price=last_p, profit_rate=None, reason=s_name, ledger=payload
        )
    except Exception as log_err:
        print(f"  ⚠️ [COIN BUY TWAP] 매매내역 기록 실패: {log_err}")
    rb.ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="COIN BUY TWAP")
    if t not in held_coins_mut:
        held_coins_mut.append(t)
    return True
