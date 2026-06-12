# -*- coding: utf-8 -*-
"""COIN 매수 루프 — ``coin_cycle`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from typing import Any

from execution.market_cycles.context import TradingCycleContext


def _rb():
    import run_bot as rb

    return rb


def run_coin_buy_cycle(
    ctx: TradingCycleContext,
    *,
    coin_weather: str,
    held_coins: Any,
    krw_bal: float,
    total_coin_equity: float,
    alpha_target_vol: float,
) -> float:
    """코인 매수 — Phase4·헷지·지수·스캔·V8/스윙·TWAP (매수 창·MDD 통과 후 호출)."""
    rb = _rb()
    state = ctx.state
    macro_mult = ctx.macro_mult
    macro_snap = ctx.macro_snap
    _buy_cycle_tag = ctx.buy_cycle_tag
    hedge_only = rb._phase4_hedge_only_active(macro_snap, "COIN")

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

    scan_targets = rb._merge_hedge_into_buy_targets(scan_targets, "COIN")
    scan_targets = rb._apply_phase4_hedge_buy_targets(scan_targets, macro_snap, "COIN")
    if not scan_targets:
        if hedge_only:
            print(
                f"  -> 🚨 코인 Phase4 글로벌 방어막: 헷지 후보 없음 — 매수 중단. "
                f"({(macro_snap.get('market_buy_block_reason') or {}).get('COIN', '')})"
            )
        return float(krw_bal)

    coin_index_change = rb.get_market_index_change("COIN")
    print(f"  📊 [BTC 지수] 변화율: {coin_index_change:+.2f}% 날씨는 {coin_weather}")
    if coin_index_change <= rb.INDEX_CRASH_COIN:
        if hedge_only:
            print(
                f"  📌 [COIN 헷지] BTC {coin_index_change:+.2f}% 급락 — "
                f"Phase4 헷지 전용 모드, 지수 차단 예외·매수 검토 계속"
            )
        else:
            print(
                f"  🚫 [COIN 매수 중단] BTC {coin_index_change:+.2f}% 급락 "
                f"(기준: {rb.INDEX_CRASH_COIN}%)"
            )
            return float(krw_bal)

    if coin_weather == rb.WEATHER_LABEL_BEAR:
        print(
            "  📌 [COIN] BEAR 날씨 — V8·SWING_FIB 일반 종목 매수 중단 (헷지만 검토)"
        )

    scan_targets = rb._sort_buy_targets_by_rs(scan_targets, "COIN")
    total_coin = len(scan_targets)
    print(
        f"  -> 🪙 코인 사냥감 {total_coin}개 정밀 분석 시작! "
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
        coin_name = rb.get_coin_name(t)

        if hedge_only and rb._is_hedge_ticker(t, "COIN"):
            _ref = (
                float(live_px_coin)
                if live_px_coin > 0
                else float(ohlcv[-1].get("c", 0) or 0)
            )
            if _ref <= 0:
                print(f"  ⏭️ {coin_name}({t}): [COIN 헷지] 현재가 없음 (패스)")
                continue
            sl_p = float(_ref) * 0.95
            s_name = "HEDGE_PHASE4"
            print(
                f"  🛡️ [HEDGE-BUY] {coin_name}({t}) Phase4 방어 — "
                f"V8/스윙·BEAR·지수급락 예외, 손절 ~{_ref * 0.95:,.4f}"
            )
        else:
            entry_decision = rb.decide_entry_signals(
                ohlcv,
                coin_weather,
                t,
                coin_name,
                idx,
                total_coin,
                market="COIN",
                reference_close=live_px_coin if live_px_coin > 0 else None,
            )
            is_buy = entry_decision.is_buy
            sl_p = entry_decision.sl_p
            s_name = entry_decision.signal_name
            v8_ok = bool(is_buy) and rb._v8_trend_buy_allowed_in_weather(coin_weather)
            if bool(is_buy) and not v8_ok:
                print(
                    f"  ⏭️ {t}: BEAR 시장 — V8 추세 매수 차단"
                )
            if v8_ok:
                print(f"  ✅ [V8-BUY] {t} 진입")
            else:
                sw_ok = entry_decision.swing_ok
                sw_fib = entry_decision.swing_fib
                sw_why = entry_decision.swing_why
                if sw_ok and not rb._swing_fib_buy_allowed_in_weather(coin_weather):
                    print(
                        f"  ⏭️ {coin_name}({t}): BEAR 시장 — SWING_FIB 눌림목 매수 차단 (헷지만 허용)"
                    )
                    continue
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
                    )
                else:
                    _prog = f"[{idx}/{total_coin}]" if total_coin else ""
                    _disp = f"{coin_name}({t})" if coin_name and coin_name != t else t
                    print(f"   🔍 [스윙] {_prog} {_disp} ❌ 패스: {sw_why}")
                    continue

        base_ratio = 1.0 / max(1, int(rb.MAX_POSITIONS_COIN))
        ratio, t_name = rb._position_ratio_with_vol_target(
            base_ratio,
            ohlcv,
            target_vol=alpha_target_vol,
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

        if not rb._can_open_new_respecting_hedge_bypass(
            t, state, "COIN", rb.MAX_POSITIONS_COIN
        ):
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
    return float(krw_bal)
