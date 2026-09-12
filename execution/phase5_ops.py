# -*- coding: utf-8 -*-
"""Phase5 계좌 서킷 — 대기 청산·시장별 비중 판정·청산 실행 (run_bot 오케스트레이션 분리)."""
from __future__ import annotations

from datetime import datetime

import pytz

from execution.circuit_break import (
    estimate_usdkrw,
    evaluate_per_market_equity_circuits,
    evaluate_per_market_share_circuits,
    evaluate_total_account_circuit,
)
from execution.guard import (
    ACCOUNT_CIRCUIT_COOLDOWN_KEY,
    ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY,
    ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY,
    LAST_RESET_WEEK_KEY,
    PEAK_EQUITY_COIN_UNIT_KEY,
    PEAK_TOTAL_EQUITY_KEY,
    PHASE5_LAST_LOOP_EQUITY_BY_MARKET_KEY,
    PHASE5_MARKET_COOLDOWNS_KEY,
    PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
    PHASE5_POST_BUY_GRACE_SEC,
    apply_phase5_share_anchor,
    apply_phase5_trailing_market_peaks,
    apply_phase5_trailing_week_and_cooldown,
    get_phase5_peak_market_equity,
    get_phase5_peak_total_equity,
    get_phase5_share_anchor,
    in_account_circuit_cooldown,
    in_market_circuit_cooldown,
    load_state,
    market_in_post_buy_grace,
    migrate_coin_peak_unit_if_needed,
    phase5_coin_unit_label,
    save_state,
    set_account_circuit_cooldown,
    set_market_circuit_cooldown,
    sync_account_circuit_mode,
    week_label_seoul,
)


def _rb():
    import run_bot as rb

    return rb


def liquidate_market(state: dict, market: str) -> None:
    """시장 단위 Phase5 전량 청산 — ``KR`` / ``US`` / ``COIN``."""
    rb = _rb()
    mk = str(market or "").strip().upper()
    if rb.TEST_MODE:
        msg = f"🧪 [TEST_MODE] Phase5 {mk} 청산 — 실주문 생략"
        print(f"  {msg}")
        try:
            lines = []
            if mk == "KR" and not rb.kis_equities_weekend_suppress_window_kst():
                bal = rb.ensure_dict(rb.get_balance_with_retry())
                for stock in rb.ensure_list(bal.get("output1")):
                    code = rb.normalize_ticker(stock.get("pdno", ""))
                    qty = int(rb._to_float(stock.get("hldg_qty", 0)))
                    if qty > 0 and code:
                        lines.append(f"KR {code} x{qty}")
            elif mk == "US" and not rb.kis_equities_weekend_suppress_window_kst():
                us_bal = rb.ensure_dict(rb.get_us_positions_with_retry())
                for item in rb.ensure_list(us_bal.get("output1")):
                    c = rb.normalize_ticker(item.get("ovrs_pdno", item.get("pdno", "")))
                    q = int(rb._to_float(item.get("ovrs_cblc_qty", item.get("hldg_qty", 0))))
                    if q > 0 and c:
                        lines.append(f"US {c} x{q}")
            elif mk == "COIN":
                for b in rb.coin_broker.get_balances() or []:
                    if b.get("currency") in ("KRW", "VTHO"):
                        continue
                    if rb.coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
                        continue
                    t = rb.coin_broker.held_ticker_row(b)
                    if not t:
                        continue
                    qf = float(rb._to_float(b.get("balance", 0)))
                    if rb.coin_broker.should_include_coin_balance_row(b):
                        lines.append(f"COIN {t} x{qf}")
            rb.send_telegram(f"{msg}\n대상:\n" + "\n".join(lines[:40]) or "(없음)")
        except Exception as e:
            print(f"  ⚠️ [TEST_MODE] {mk} 청산 시뮬 요약 실패: {e}")
        return

    if mk == "KR":
        if not rb.kis_equities_weekend_suppress_window_kst() and rb.is_market_open("KR"):
            try:
                bal = rb.ensure_dict(rb.get_balance_with_retry())
                for stock in rb.ensure_list(bal.get("output1")):
                    code = rb.normalize_ticker(stock.get("pdno", ""))
                    qty = int(rb._to_float(stock.get("hldg_qty", 0)))
                    if qty <= 0 or not code:
                        continue
                    rb.manual_sell("KR", code, qty, idem_lane=rb.order_idem.LANE_PHASE5)
            except Exception as e:
                print(f"  ⚠️ [Phase5] 국장 전량 청산 루프 예외: {e}")
        else:
            print("  ⏸️ [Phase5] 국장 비장중/점검 — KR 청산은 장 개시 후 재시도")
    elif mk == "US":
        if not rb.kis_equities_weekend_suppress_window_kst() and rb.is_market_open("US"):
            try:
                us_bal = rb.ensure_dict(rb.get_us_positions_with_retry())
                for item in rb.ensure_list(us_bal.get("output1")):
                    c = rb.normalize_ticker(item.get("ovrs_pdno", item.get("pdno", "")))
                    q = int(rb._to_float(item.get("ovrs_cblc_qty", item.get("hldg_qty", 0))))
                    if q <= 0 or not c:
                        continue
                    rb.manual_sell("US", c, q, idem_lane=rb.order_idem.LANE_PHASE5)
            except Exception as e:
                print(f"  ⚠️ [Phase5] 미장 전량 청산 루프 예외: {e}")
        else:
            print("  ⏸️ [Phase5] 미장 비장중/점검 — US 청산은 장 개시 후 재시도")
    elif mk == "COIN":
        try:
            for b in rb.coin_broker.get_balances() or []:
                if b.get("currency") in ("KRW", "VTHO"):
                    continue
                if rb.coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
                    continue
                t = rb.coin_broker.held_ticker_row(b)
                if not t:
                    continue
                qf = float(rb._to_float(b.get("balance", 0)))
                if not rb.coin_broker.should_include_coin_balance_row(b):
                    continue
                rb.manual_sell("COIN", t, qf, idem_lane=rb.order_idem.LANE_PHASE5)
        except Exception as e:
            print(f"  ⚠️ [Phase5] 코인 전량 청산 루프 예외: {e}")


def emergency_liquidate_all(state: dict) -> None:
    for mk in ("KR", "US", "COIN"):
        liquidate_market(state, mk)


def market_has_ledger_positions(state: dict, market: str) -> bool:
    mk = str(market or "").strip().upper()
    pos = state.get("positions", {}) if isinstance(state, dict) else {}
    if not isinstance(pos, dict):
        return False
    for key, row in pos.items():
        if not isinstance(row, dict):
            continue
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        pm = str(row.get("market", "")).strip().upper()
        if pm == mk:
            return True
        k = str(key)
        if mk == "COIN" and (k.startswith("USDT-") or k.startswith("KRW-")):
            return True
        if mk == "KR" and k.isdigit() and len(k) == 6:
            return True
        if mk == "US" and k.isalpha():
            return True
    return False


PHASE5_AI_LLM_COOLDOWN_KEY = "phase5_ai_llm_cooldown_until"


def _phase5_ai_llm_in_cooldown(state: dict, market: str) -> bool:
    """LLM 실패 후 재호출 억제 — 루프마다 수백 건 API 낭비 방지."""
    import time

    mk = str(market or "").strip().upper()
    raw = state.get(PHASE5_AI_LLM_COOLDOWN_KEY)
    if not isinstance(raw, dict):
        return False
    until = raw.get(mk)
    try:
        return float(until or 0) > time.time()
    except (TypeError, ValueError):
        return False


def _set_phase5_ai_llm_cooldown(state: dict, market: str, cooldown_sec: float) -> None:
    import time

    mk = str(market or "").strip().upper()
    sec = max(0.0, float(cooldown_sec or 0))
    if sec <= 0:
        return
    bag = state.setdefault(PHASE5_AI_LLM_COOLDOWN_KEY, {})
    if not isinstance(bag, dict):
        bag = {}
        state[PHASE5_AI_LLM_COOLDOWN_KEY] = bag
    bag[mk] = time.time() + sec


def pending_markets(state: dict) -> list[str]:
    rb = _rb()
    raw = state.get(PHASE5_PENDING_LIQUIDATION_MARKETS_KEY)
    if isinstance(raw, list) and raw:
        return [str(x).strip().upper() for x in raw if str(x).strip()]
    if rb.ACCOUNT_CIRCUIT_USE_TOTAL and bool(state.get("phase5_pending_liquidation")):
        return ["KR", "US", "COIN"]
    return []


def migrate_legacy_pending_flag(state: dict) -> None:
    rb = _rb()
    if rb.ACCOUNT_CIRCUIT_USE_TOTAL:
        return
    if not bool(state.get("phase5_pending_liquidation")):
        return
    if isinstance(state.get(PHASE5_PENDING_LIQUIDATION_MARKETS_KEY), list):
        return
    state["phase5_pending_liquidation"] = False
    save_state(rb.STATE_PATH, state)
    print(
        "  📌 [Phase5] 레거시 '전 시장 대기청산' 플래그 해제 — "
        "시장별 비중 서킷 모드(합산 서킷 잔여 플래그)"
    )


def prune_stale_pending(
    state: dict,
    circuits: dict,
    market_ok: dict[str, bool],
) -> None:
    rb = _rb()
    if rb.ACCOUNT_CIRCUIT_USE_TOTAL:
        return
    pending = pending_markets(state)
    if not pending:
        return
    kept: list[str] = []
    for mk in pending:
        if not market_ok.get(mk):
            kept.append(mk)
            continue
        ev = circuits.get(mk) or {}
        if ev.get("triggered"):
            kept.append(mk)
        else:
            print(
                f"  📌 [Phase5] {mk} 대기 청산 해제 — 현재 비중 서킷 정상 "
                f"(예전 합산·미체결 대기 잔여)"
            )
    if kept != pending:
        set_pending_markets(state, kept)
        save_state(rb.STATE_PATH, state)


def set_pending_markets(state: dict, markets: list[str]) -> None:
    mks = sorted({str(m).strip().upper() for m in markets if str(m).strip()})
    if mks:
        state[PHASE5_PENDING_LIQUIDATION_MARKETS_KEY] = mks
        state["phase5_pending_liquidation"] = True
    else:
        state.pop(PHASE5_PENDING_LIQUIDATION_MARKETS_KEY, None)
        state["phase5_pending_liquidation"] = False


def try_pending_liquidation() -> None:
    rb = _rb()
    st = load_state(rb.STATE_PATH)
    pending = pending_markets(st)
    if not pending:
        return

    print(f"  🔁 [Phase5] 대기 청산 재시도 — {', '.join(pending)}")
    for mk in pending:
        if not market_has_ledger_positions(st, mk):
            print(f"  📌 [Phase5·{mk}] 대기 청산 해제 — 장부 보유 없음")
            continue
        liquidate_market(st, mk)
    st2 = load_state(rb.STATE_PATH)
    still = [mk for mk in pending if market_has_ledger_positions(st2, mk)]
    set_pending_markets(st2, still)
    save_state(rb.STATE_PATH, st2)
    if not still:
        print("  ✅ [Phase5] 대기 청산 완료 — 대기 시장 포지션 정리됨")
        try:
            rb.send_telegram("✅ [Phase5] 대기 청산 완료 — 시장 재개 후 미체결 포지션까지 정리되었습니다.")
        except Exception:
            pass


def _equity_market_session_open(rb, market: str) -> bool:
    """국·미 Phase5/MDD — 장중에만 판정. 코인은 상시."""
    mk = str(market or "").strip().upper()
    if mk == "COIN":
        return True
    if mk not in ("KR", "US"):
        return False
    try:
        if rb.kis_equities_weekend_suppress_window_kst():
            return False
    except Exception:
        pass
    try:
        return bool(rb.is_market_open(mk))
    except Exception:
        return False


def maybe_run_account_circuit(state: dict) -> None:
    """매 루프: 시장별 **잔고(peak_equity)** MDD(기본), 옵션 지수·비중·합산."""
    rb = _rb()
    if not rb.ACCOUNT_CIRCUIT_ENABLED:
        return

    aux_meta = state.get("_phase5_aux_sync") if isinstance(state.get("_phase5_aux_sync"), dict) else {}
    from services import ledger_valuation as lv

    kr_ok = bool(aux_meta.get("kr_ok")) if aux_meta else bool(lv.kis_display_total(state, "KR"))
    us_ok = bool(aux_meta.get("us_ok")) if aux_meta else bool(lv.kis_display_total(state, "US"))
    from api import coin_broker as _cb

    coin_native = float(_cb.circuit_aux_coin_native(state) or 0)
    coin_krw = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    if coin_krw <= 0 and coin_native > 0:
        coin_krw = float(_cb.native_to_krw(coin_native))
    coin_ok = bool(aux_meta.get("coin_ok")) if aux_meta else bool(coin_native > 0 or coin_krw > 0)
    aux_any = kr_ok or us_ok or coin_ok
    market_ok = {"KR": kr_ok, "US": us_ok, "COIN": coin_ok}

    # 국·미는 장중(세션)에만 MDD·서킷 판정 — 휴장 오발동·스팸 로그 방지
    for mk in ("KR", "US"):
        if market_ok.get(mk) and not _equity_market_session_open(rb, mk):
            market_ok[mk] = False

    kr_krw = lv.market_equity_for_risk(state, "KR")
    us_usd = lv.market_equity_for_risk(state, "US")
    if kr_krw <= 0:
        kr_krw = lv.kis_display_total(state, "KR")
    if us_usd <= 0:
        us_usd = lv.kis_display_total(state, "US")
    usdkrw = estimate_usdkrw()
    us_krw = us_usd * usdkrw

    # divergence / KIS 재조회는 장중 판정 시장만
    open_for_div = tuple(mk for mk in ("KR", "US") if _equity_market_session_open(rb, mk))
    if open_for_div:
        lv.log_equity_divergence_if_needed(state, open_for_div)

    if not any(market_ok.values()):
        if not aux_any:
            print("  ⚠️ [Phase5 서킷] circuit_aux 전부 미확인 — 이번 루프 판정 건너뜀")
        return

    st = load_state(rb.STATE_PATH)
    migrate_legacy_pending_flag(st)

    if rb.ACCOUNT_CIRCUIT_USE_TOTAL:
        # 합산 모드는 국·미 장중일 때만 (휴장 스냅샷으로 합산 MDD 오발동 방지)
        if not (
            _equity_market_session_open(rb, "KR") and _equity_market_session_open(rb, "US")
        ):
            return
        sync_account_circuit_mode(st, "total", rb.STATE_PATH)
        st = load_state(rb.STATE_PATH)
        try_pending_liquidation()
        if not (kr_ok and us_ok and coin_ok):
            print(
                "  ⚠️ [Phase5 서킷·합산] aux 불완전("
                f"KR={kr_ok}, US={us_ok}, COIN={coin_ok}) — 합산 판정 건너뜀"
            )
            return
        total = rb._portfolio_total_krw_from_aux(state)
        if total <= 0:
            return
        apply_phase5_trailing_week_and_cooldown(st, float(total), rb.STATE_PATH)
        peak = get_phase5_peak_total_equity(st)
        for _k in (
            PEAK_TOTAL_EQUITY_KEY,
            LAST_RESET_WEEK_KEY,
            ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY,
            ACCOUNT_CIRCUIT_COOLDOWN_KEY,
        ):
            if _k in st:
                state[_k] = st[_k]
        if state.get(ACCOUNT_CIRCUIT_COOLDOWN_KEY) and in_account_circuit_cooldown(st):
            print(
                f"  🛡️ [Phase5·합산] 전역 쿨다운 중 (until={st.get(ACCOUNT_CIRCUIT_COOLDOWN_KEY, '')})"
            )
            return
        ev = evaluate_total_account_circuit(
            peak, total, trigger_drawdown_pct=rb.ACCOUNT_CIRCUIT_MDD_PCT
        )
        print(
            f"  🛡️ [Phase5·합산] {total:,.0f}원 (고점 {peak:,.0f}) DD={ev['drawdown_pct']:.2f}% → "
            f"{'발동' if ev['triggered'] else '정상'} | {ev['reason']}"
        )
        if not ev["triggered"]:
            return
        rb.send_telegram(
            f"🚨 [Phase5 합산 서킷]\n{ev['reason']}\n전 시장 청산 시도 (TEST_MODE={rb.TEST_MODE})"
        )
        set_pending_markets(state, ["KR", "US", "COIN"])
        save_state(rb.STATE_PATH, state)
        emergency_liquidate_all(state)
        st2 = load_state(rb.STATE_PATH)
        still = [mk for mk in ("KR", "US", "COIN") if market_has_ledger_positions(st2, mk)]
        set_pending_markets(st2, still)
        set_account_circuit_cooldown(st2, rb.STATE_PATH, rb.ACCOUNT_CIRCUIT_COOLDOWN_H)
        return

    if rb.ACCOUNT_CIRCUIT_USE_SHARE:
        sync_account_circuit_mode(st, "share", rb.STATE_PATH)
        st = load_state(rb.STATE_PATH)
        _run_share_circuits(
            rb,
            state,
            st,
            kr_krw=kr_krw,
            us_usd=us_usd,
            coin_krw=coin_krw,
            usdkrw=usdkrw,
            us_krw=us_krw,
            market_ok=market_ok,
        )
        return

    # 지수 MDD 청산은 폐지(매수 INDEX_CRASH_* 전용). 잔고 peak_equity MDD만 사용.
    if getattr(rb, "ACCOUNT_CIRCUIT_USE_INDEX", False):
        print(
            "  ⚠️ [Phase5] account_circuit_use_index 무시 — "
            "지수 청산 폐지, 잔고 MDD로 판정 (docs/PHASE5_LIQUIDATION_PLAN.md)"
        )

    sync_account_circuit_mode(st, "per_market_mdd", rb.STATE_PATH)
    st = load_state(rb.STATE_PATH)
    _run_per_market_mdd_circuits(
        rb,
        state,
        st,
        kr_krw=kr_krw,
        us_usd=us_usd,
        coin_native=coin_native,
        market_ok=market_ok,
    )


def _run_share_circuits(
    rb,
    state: dict,
    st: dict,
    *,
    kr_krw: float,
    us_usd: float,
    coin_krw: float,
    usdkrw: float,
    us_krw: float,
    market_ok: dict[str, bool],
) -> None:
    seoul = datetime.now(pytz.timezone("Asia/Seoul"))
    wl = week_label_seoul(seoul)
    anchor = get_phase5_share_anchor(st)
    if not anchor or str(st.get("phase5_share_anchor_week", "")) != wl:
        apply_phase5_share_anchor(
            st,
            kr_krw=kr_krw,
            us_krw=us_krw,
            coin_krw=coin_krw,
            path=rb.STATE_PATH,
            market_ok=market_ok,
        )
        st = load_state(rb.STATE_PATH)
        st["phase5_share_anchor_week"] = wl
        save_state(rb.STATE_PATH, st)
        anchor = get_phase5_share_anchor(st)
    min_by_mk = {mk: rb._account_circuit_min_share_pct(mk) for mk in ("KR", "US", "COIN")}
    circuits = evaluate_per_market_share_circuits(
        kr_krw=kr_krw,
        us_usd=us_usd,
        coin_krw=coin_krw,
        usdkrw=usdkrw,
        market_ok=market_ok,
        min_share_pct_by_market=min_by_mk,
        share_anchor=anchor,
        anchor_min_ratio=rb.ACCOUNT_CIRCUIT_ANCHOR_MIN_RATIO,
    )
    for mk in ("KR", "US", "COIN"):
        if not market_ok.get(mk):
            continue
        ev = circuits.get(mk) or {}
        share = float(ev.get("share_pct", 0) or 0)
        floor = float(ev.get("effective_floor_pct", ev.get("min_share_pct", 0)) or 0)
        cd = in_market_circuit_cooldown(st, mk)
        tag = "쿨다운" if cd else ("발동" if ev.get("triggered") else "정상")
        print(
            f"  🛡️ [Phase5·{mk}] 비중 {share:.1f}% / 하한 {floor:.1f}% → {tag} | {ev.get('reason', '')}"
        )
    _liquidate_triggered_markets(rb, state, st, circuits, market_ok, kind="비중")


def _run_per_market_mdd_circuits(
    rb,
    state: dict,
    st: dict,
    *,
    kr_krw: float,
    us_usd: float,
    coin_native: float,
    market_ok: dict[str, bool],
) -> None:
    """시장별 잔고 고점(peak_equity_*) 대비 MDD — 종목·지수 고점 아님.

    COIN equities = 견적 통화(업비트 원 / 바이낸스 USDT).
    """
    migrate_coin_peak_unit_if_needed(st, float(coin_native), rb.STATE_PATH)
    st = load_state(rb.STATE_PATH)
    equities_all = {"KR": float(kr_krw), "US": float(us_usd), "COIN": float(coin_native)}
    equities_trailing = {mk: v for mk, v in equities_all.items() if market_ok.get(mk)}
    apply_phase5_trailing_market_peaks(st, equities_trailing, rb.STATE_PATH)
    equities = equities_all
    st = load_state(rb.STATE_PATH)
    for _k in (
        "peak_equity_KR",
        "peak_equity_US",
        "peak_equity_COIN",
        PEAK_EQUITY_COIN_UNIT_KEY,
        PHASE5_MARKET_COOLDOWNS_KEY,
        PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
        ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY,
        PHASE5_LAST_LOOP_EQUITY_BY_MARKET_KEY,
        "phase5_pending_liquidation",
    ):
        if _k in st:
            state[_k] = st[_k]
        elif _k in state:
            state.pop(_k, None)
    peaks = {mk: get_phase5_peak_market_equity(st, mk) for mk in ("KR", "US", "COIN")}
    circuits = evaluate_per_market_equity_circuits(
        equities=equities,
        peaks=peaks,
        market_ok=market_ok,
        trigger_drawdown_pct=rb.ACCOUNT_CIRCUIT_MDD_PCT,
    )
    for mk in ("KR", "US", "COIN"):
        if not market_ok.get(mk):
            continue
        if not market_has_ledger_positions(state, mk):
            circuits[mk] = {
                **(circuits.get(mk) or {}),
                "market": mk,
                "triggered": False,
                "reason": "장부 보유 없음 — Phase5·청산 스킵",
            }
            print(f"  📌 [Phase5·{mk}] 장부 보유 0 — 잔고 MDD·청산 스킵")
            continue
        if mk in ("KR", "US") and market_in_post_buy_grace(st, mk):
            print(
                f"  📌 [Phase5·{mk}] 매수 직후 유예({int(PHASE5_POST_BUY_GRACE_SEC // 60)}분) — "
                "이번 루프 MDD 판정 생략"
            )
            circuits[mk] = {
                "triggered": False,
                "drawdown_pct": 0.0,
                "current": equities.get(mk, 0),
                "peak": peaks.get(mk, 0),
                "reason": "매수 직후 유예",
            }
            continue
        ev = circuits.get(mk) or {}
        cur = float(ev.get("current", equities.get(mk, 0)) or 0)
        peak = float(ev.get("peak", peaks.get(mk, 0)) or 0)
        dd = float(ev.get("drawdown_pct", 0) or 0)
        cd = in_market_circuit_cooldown(st, mk)
        tag = "쿨다운" if cd else ("발동" if ev.get("triggered") else "정상")
        if mk == "US":
            unit, suffix = "$", ""
        elif mk == "COIN":
            lbl = phase5_coin_unit_label(st.get(PEAK_EQUITY_COIN_UNIT_KEY))
            unit, suffix = ("", "USDT") if lbl == "USDT" else ("", "원")
        else:
            unit, suffix = "", "원"
        print(
            f"  🛡️ [Phase5·{mk}] {unit}{cur:,.2f}{suffix} (고점 {unit}{peak:,.2f}{suffix}) "
            f"DD={dd:.2f}% → {tag} | {ev.get('reason', '')}"
        )
    _liquidate_triggered_markets(rb, state, st, circuits, market_ok, kind="잔고MDD")


def _run_per_market_index_circuits(
    rb,
    state: dict,
    st: dict,
    *,
    market_ok: dict[str, bool],
) -> None:
    from execution.phase5_index_circuit import evaluate_per_market_index_circuits

    st = load_state(rb.STATE_PATH)
    for _k in (
        PHASE5_MARKET_COOLDOWNS_KEY,
        PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
        ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY,
        "phase5_pending_liquidation",
    ):
        if _k in st:
            state[_k] = st[_k]
        elif _k in state:
            state.pop(_k, None)

    circuits = evaluate_per_market_index_circuits(
        market_ok=market_ok,
        trigger_drawdown_pct=rb.ACCOUNT_CIRCUIT_MDD_PCT,
    )
    for mk in ("KR", "US", "COIN"):
        if not market_ok.get(mk):
            continue
        if not market_has_ledger_positions(state, mk):
            prev = circuits.get(mk) or {}
            circuits[mk] = {
                **prev,
                "market": mk,
                "triggered": False,
                "reason": "장부 보유 없음 — Phase5·청산 스킵",
            }
            print(f"  📌 [Phase5·{mk}] 장부 보유 0 — 지수·청산 스킵")
            continue
        ev = circuits.get(mk) or {}
        sym = str(ev.get("benchmark", "") or "")
        cur = float(ev.get("current", 0) or 0)
        peak = float(ev.get("peak", 0) or 0)
        dd = float(ev.get("drawdown_pct", 0) or 0)
        cd = in_market_circuit_cooldown(st, mk)
        tag = "쿨다운" if cd else ("발동" if ev.get("triggered") else "정상")
        if mk == "KR":
            print(
                f"  🛡️ [Phase5·{mk}] {sym} {cur:,.0f} (고점 {peak:,.0f}) "
                f"DD={dd:.2f}% → {tag} | {ev.get('reason', '')}"
            )
        elif mk == "US":
            print(
                f"  🛡️ [Phase5·{mk}] {sym} ${cur:,.2f} (고점 ${peak:,.2f}) "
                f"DD={dd:.2f}% → {tag} | {ev.get('reason', '')}"
            )
        else:
            print(
                f"  🛡️ [Phase5·{mk}] {sym} ${cur:,.2f} (고점 ${peak:,.2f}) "
                f"DD={dd:.2f}% → {tag} | {ev.get('reason', '')}"
            )
    _liquidate_triggered_markets(rb, state, st, circuits, market_ok, kind="지수")


def _reconfirm_phase5_triggers_before_liquidation(
    rb,
    state: dict,
    st: dict,
    circuits: dict,
    triggered: list[str],
) -> list[str]:
    """청산 직전 2차 판정 — 잔고 MDD(기본) 또는 지수 MDD(옵션)."""
    if getattr(rb, "ACCOUNT_CIRCUIT_USE_INDEX", False):
        return _reconfirm_index_triggers(rb, state, st, circuits, triggered)
    return _reconfirm_equity_triggers(rb, state, st, circuits, triggered)


def _reconfirm_index_triggers(
    rb,
    state: dict,
    st: dict,
    circuits: dict,
    triggered: list[str],
) -> list[str]:
    from execution.phase5_index_circuit import evaluate_index_circuit_for_market

    confirmed: list[str] = []
    for mk in triggered:
        print(f"  ⚠️ [Phase5·{mk}] 1차 지수 발동 — yfinance 재조회 후 2차 판정")
        ev = evaluate_index_circuit_for_market(
            mk,
            trigger_drawdown_pct=rb.ACCOUNT_CIRCUIT_MDD_PCT,
        )
        circuits[mk] = ev
        sym = str(ev.get("benchmark", "") or "")
        if ev.get("triggered"):
            dd = float(ev.get("drawdown_pct", 0) or 0)
            print(
                f"  🛡️ [Phase5·{mk}] 재검증 후 {sym} DD={dd:.2f}% — "
                f"지수 조건 충족 · AI 심사 진입"
            )
            confirmed.append(mk)
        else:
            dd = float(ev.get("drawdown_pct", 0) or 0)
            print(
                f"  📌 [Phase5·{mk}] 재검증 후 {sym} DD={dd:.2f}% — 청산 취소"
            )
    return confirmed


def _reconfirm_equity_triggers(
    rb,
    state: dict,
    st: dict,
    circuits: dict,
    triggered: list[str],
) -> list[str]:
    """KR/US: KIS 재조회 후 MDD면 **다음 루프**에 한 번 더 확인. (루프 내 sleep 없음)

    1회차: 재조회 → 여전히 MDD → pending 저장, 이번 주기 청산 보류
    2회차(다음 주기): 재조회 → 여전히 MDD → AI 심사 / 아니면 취소
    """
    import time as _time

    from execution import balance_read as bal_read
    from services import ledger_valuation as lv

    pending_key = "phase5_reconfirm_pending"
    bag = state.get(pending_key)
    if not isinstance(bag, dict):
        bag = {}
    # st 에도 동기
    st_bag = st.get(pending_key)
    if isinstance(st_bag, dict):
        for k, v in st_bag.items():
            if k not in bag:
                bag[k] = v

    confirmed: list[str] = []
    for mk in triggered:
        if mk not in ("KR", "US"):
            confirmed.append(mk)
            continue

        risk0, snap0, pct0 = lv.equity_divergence_metrics(state, mk)
        peak = get_phase5_peak_market_equity(st, mk)
        unit = "원" if mk == "KR" else "$"
        suffix = "원" if mk == "KR" else ""
        prev = bag.get(mk) if isinstance(bag.get(mk), dict) else None
        is_second = bool(prev)
        trail = list(prev.get("trail") or []) if prev else []
        if not trail:
            trail = [
                f"t0_before_refresh risk={risk0:.2f} snap={snap0:.2f} "
                f"div_pct={pct0:.2f} peak={peak:.2f}"
            ]
        else:
            trail.append(
                f"t_next_cycle_before risk={risk0:.2f} snap={snap0:.2f} "
                f"div_pct={pct0:.2f} peak={peak:.2f}"
            )

        tag = "2회차(다음주기)" if is_second else "1회차"
        print(
            "  ⚠️ [Phase5 재검증] 매도 대금 정산 지연 의심 -> "
            f"API 강제 재조회 ({tag})"
        )
        print(
            f"  ⚠️ [Phase5·{mk}] 발동 — risk {unit}{risk0:,.0f}{suffix} vs snap "
            f"{unit}{snap0:,.0f}{suffix} (diff {pct0:.1f}%) → KIS 재조회"
        )

        try:
            bal_read.invalidate(mk)
        except Exception:
            pass
        if not lv.refresh_kis_display_snapshot_for_market(state, mk, rb.STATE_PATH):
            print(f"  📌 [Phase5·{mk}] KIS 재조회 실패 — 청산 보류")
            circuits[mk] = {
                **(circuits.get(mk) or {}),
                "triggered": False,
                "reason": "KIS 강제 재조회 실패 — 청산 보류",
                "reconfirm_trail": trail + ["refresh=fail"],
            }
            continue

        st = load_state(rb.STATE_PATH)
        risk1 = lv.market_equity_for_risk(state, mk)
        _r, snap1, pct1 = lv.equity_divergence_metrics(state, mk)
        peak = get_phase5_peak_market_equity(st, mk)
        step = "t2_next_cycle" if is_second else "t1_after_refresh"
        trail.append(
            f"{step} risk={risk1:.2f} snap={snap1:.2f} div_pct={pct1:.2f} peak={peak:.2f}"
        )

        peaks = {"KR": 0.0, "US": 0.0, "COIN": 0.0}
        peaks[mk] = peak
        equities = {"KR": 0.0, "US": 0.0, "COIN": 0.0}
        equities[mk] = risk1
        mok = {"KR": False, "US": False, "COIN": False}
        mok[mk] = True
        ev = evaluate_per_market_equity_circuits(
            equities=equities,
            peaks=peaks,
            market_ok=mok,
            trigger_drawdown_pct=rb.ACCOUNT_CIRCUIT_MDD_PCT,
        ).get(mk, {})

        if not ev.get("triggered"):
            bag.pop(mk, None)
            state[pending_key] = bag
            st[pending_key] = bag
            try:
                save_state(rb.STATE_PATH, state)
            except Exception:
                pass
            dd = float(ev.get("drawdown_pct", 0) or 0)
            cur = float(ev.get("current", 0) or 0)
            print(
                f"  📌 [Phase5·{mk}] 재검증 후 DD={dd:.2f}% "
                f"(risk {unit}{cur:,.0f}{suffix}) — 오발동 방지·청산 취소"
            )
            circuits[mk] = {
                **ev,
                "triggered": False,
                "reconfirm_trail": trail,
                "reconfirm_summary": " | ".join(trail),
            }
            continue

        if not is_second:
            # 다음 주기에 한 번 더 확인
            bag[mk] = {
                "trail": trail,
                "ts": _time.time(),
                "peak": peak,
                "dd": float(ev.get("drawdown_pct", 0) or 0),
            }
            state[pending_key] = bag
            st[pending_key] = bag
            try:
                save_state(rb.STATE_PATH, state)
            except Exception:
                pass
            dd = float(ev.get("drawdown_pct", 0) or 0)
            cur = float(ev.get("current", 0) or 0)
            print(
                f"  ⏳ [Phase5·{mk}] 1회 재조회 후에도 DD={dd:.2f}% "
                f"(risk {unit}{cur:,.0f}{suffix}) — **다음 주기**에 재확인 "
                f"(루프 내 대기 없음)"
            )
            circuits[mk] = {
                **ev,
                "triggered": False,
                "reason": "다음 주기 재검증 대기",
                "reconfirm_trail": trail,
                "reconfirm_summary": " | ".join(trail),
                "reconfirm_deferred": True,
            }
            continue

        # 2회차: 여전히 MDD → AI
        bag.pop(mk, None)
        state[pending_key] = bag
        st[pending_key] = bag
        try:
            save_state(rb.STATE_PATH, state)
        except Exception:
            pass
        ev = {
            **ev,
            "reconfirm_trail": trail,
            "reconfirm_summary": " | ".join(trail),
        }
        circuits[mk] = ev
        dd = float(ev.get("drawdown_pct", 0) or 0)
        cur = float(ev.get("current", 0) or 0)
        print(
            f"  🛡️ [Phase5·{mk}] 다음주기 재검증 후에도 DD={dd:.2f}% "
            f"(risk {unit}{cur:,.0f}{suffix}) — AI 심사 진입"
        )
        print(f"  📋 [Phase5·{mk}] 재검증 전후: {ev['reconfirm_summary']}")
        confirmed.append(mk)

    # 발동 목록에 없는 pending 은 정리(회복)
    for mk in list(bag.keys()):
        if mk not in triggered:
            bag.pop(mk, None)
            print(f"  📌 [Phase5·{mk}] 재검증 pending 해제 — 이번 루프 미발동")
    state[pending_key] = bag
    st[pending_key] = bag
    try:
        if bag:
            save_state(rb.STATE_PATH, state)
        elif pending_key in state:
            # empty bag — still save to clear
            save_state(rb.STATE_PATH, state)
    except Exception:
        pass
    return confirmed


def _ai_liquidation_telegram_line(ev: dict) -> str:
    ai = ev.get("ai_liquidation")
    if not isinstance(ai, dict):
        return ""
    score = ai.get("liquidation_score")
    thr = ai.get("threshold")
    short = ai.get("rationale_short") or ai.get("rationale") or ""
    if score is None:
        return ""
    return f"🤖 AI 청산 점수 {score}/{thr} — {short}\n"


def _ai_gate_phase5_liquidation(
    rb,
    state: dict,
    circuits: dict,
    triggered: list[str],
) -> list[str]:
    """지수 2차 통과 후 AI 종합 점수 — 임계 이상만 청산."""
    if not getattr(rb, "PHASE5_AI_LIQUIDATION_ENABLED", True):
        return triggered

    from execution.phase5_ai_liquidation import evaluate_phase5_ai_liquidation

    confirmed: list[str] = []
    thr = int(getattr(rb, "PHASE5_AI_LIQUIDATION_THRESHOLD", 70))
    for mk in triggered:
        if not market_has_ledger_positions(state, mk):
            print(f"  📌 [Phase5·{mk}] AI 심사 스킵 — 장부 보유 없음")
            circuits[mk] = {
                **(circuits.get(mk) or {}),
                "triggered": False,
                "reason": "장부 보유 없음 — 청산 스킵",
            }
            continue
        if _phase5_ai_llm_in_cooldown(state, mk):
            print(f"  📌 [Phase5·{mk}] AI LLM 쿨다운 — API 재호출 생략")
            circuits[mk] = {
                **(circuits.get(mk) or {}),
                "triggered": False,
                "reason": "AI LLM 쿨다운 — 청산 보류",
            }
            continue
        ev = circuits.get(mk) or {}
        print(f"  🤖 [Phase5·{mk}] AI 청산 심사 시작 (임계 {thr}점)")
        ai = evaluate_phase5_ai_liquidation(
            state,
            mk,
            ev,
            threshold=thr,
            provider=getattr(rb, "PHASE5_AI_LIQUIDATION_PROVIDER", "gemini"),
            config=getattr(rb, "config", None),
            max_retries=int(getattr(rb, "PHASE5_AI_LIQUIDATION_MAX_RETRIES", 1)),
            retry_delay_sec=float(getattr(rb, "PHASE5_AI_LIQUIDATION_RETRY_DELAY_SEC", 2.0)),
            gemini_max_model_attempts=int(
                getattr(rb, "PHASE5_AI_GEMINI_MAX_MODEL_ATTEMPTS", 1)
            ),
        )
        score = int(ai.get("liquidation_score", 0))
        if not ai.get("llm_success"):
            cd_sec = float(getattr(rb, "PHASE5_AI_LLM_COOLDOWN_SEC", 3600.0))
            _set_phase5_ai_llm_cooldown(state, mk, cd_sec)
            try:
                save_state(rb.STATE_PATH, state)
            except Exception:
                pass
            print(
                f"  🚫 [Phase5·{mk}] AI 응답 실패({ai.get('attempts', 0)}회) — "
                f"청산 보류 ({ai.get('rationale_short', ai.get('rationale', ''))})"
            )
            circuits[mk] = {
                **ev,
                "triggered": False,
                "reason": f"AI 응답 실패 — 청산 보류",
                "ai_liquidation": ai,
            }
            try:
                rb.send_telegram(
                    f"⚠️ [Phase5 {mk} AI 보류]\n"
                    f"LLM {ai.get('attempts')}회 재시도 후 응답 없음\n"
                    f"다음 루프에서 재판정"
                )
            except Exception:
                pass
            continue

        short = str(ai.get("rationale_short", "") or "")
        if ai.get("proceed"):
            print(
                f"  🤖 [Phase5·{mk}] AI 점수 {score}/{thr} — 청산 진행\n"
                f"      {short}"
            )
            circuits[mk] = {**ev, "ai_liquidation": ai}
            confirmed.append(mk)
        else:
            print(
                f"  🤖 [Phase5·{mk}] AI 점수 {score}/{thr} — 청산 보류\n"
                f"      {short}"
            )
            circuits[mk] = {
                **ev,
                "triggered": False,
                "reason": f"AI 점수 {score}<{thr} — 청산 보류",
                "ai_liquidation": ai,
            }
            try:
                rb.send_telegram(
                    f"📌 [Phase5 {mk} AI 청산 보류]\n"
                    f"점수 {score}/{thr}\n{short}"
                )
            except Exception:
                pass
    return confirmed


def _liquidate_triggered_markets(
    rb,
    state: dict,
    st: dict,
    circuits: dict,
    market_ok: dict[str, bool],
    *,
    kind: str,
) -> None:
    prune_stale_pending(st, circuits, market_ok)
    try_pending_liquidation()
    st = load_state(rb.STATE_PATH)

    triggered = [
        mk
        for mk in ("KR", "US", "COIN")
        if market_ok.get(mk) and (circuits.get(mk) or {}).get("triggered")
        and not in_market_circuit_cooldown(st, mk)
        and market_has_ledger_positions(state, mk)
    ]
    if not triggered:
        return

    triggered = _reconfirm_phase5_triggers_before_liquidation(rb, state, st, circuits, triggered)
    if not triggered:
        return

    triggered = _ai_gate_phase5_liquidation(rb, state, circuits, triggered)
    if not triggered:
        return

    for mk in triggered:
        ev = circuits[mk]
        rb.send_telegram(
            f"🚨 [Phase5 {mk} {kind} 서킷]\n{ev.get('reason', '')}\n"
            f"{_ai_liquidation_telegram_line(ev)}"
            f"{mk} 시장만 청산 시도 (TEST_MODE={rb.TEST_MODE})"
        )
        set_pending_markets(state, list(set(pending_markets(state)) | {mk}))
        save_state(rb.STATE_PATH, state)
        liquidate_market(state, mk)
        st_liq = load_state(rb.STATE_PATH)
        if not market_has_ledger_positions(st_liq, mk):
            pending_now = [m for m in pending_markets(st_liq) if m != mk]
            set_pending_markets(st_liq, pending_now)
        set_market_circuit_cooldown(st_liq, mk, rb.STATE_PATH, rb.ACCOUNT_CIRCUIT_COOLDOWN_H)
        st = load_state(rb.STATE_PATH)
