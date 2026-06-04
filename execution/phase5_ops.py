# -*- coding: utf-8 -*-
"""Phase5 계좌 서킷 — 대기 청산·시장별 비중 판정·청산 실행 (run_bot 오케스트레이션 분리)."""
from __future__ import annotations

from datetime import datetime

import pytz

from execution.circuit_break import (
    estimate_usdkrw,
    evaluate_per_market_share_circuits,
    evaluate_total_account_circuit,
)
from execution.guard import (
    ACCOUNT_CIRCUIT_COOLDOWN_KEY,
    ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY,
    LAST_RESET_WEEK_KEY,
    PEAK_TOTAL_EQUITY_KEY,
    PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
    apply_phase5_share_anchor,
    apply_phase5_trailing_week_and_cooldown,
    get_phase5_peak_total_equity,
    get_phase5_share_anchor,
    in_account_circuit_cooldown,
    in_market_circuit_cooldown,
    load_state,
    save_state,
    set_account_circuit_cooldown,
    set_market_circuit_cooldown,
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
        if isinstance(row, dict):
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


def maybe_run_account_circuit(state: dict) -> None:
    """매 루프: 시장별 포트폴리오 비중 서킷(기본) 또는 레거시 합산 MDD."""
    rb = _rb()
    if not rb.ACCOUNT_CIRCUIT_ENABLED:
        return

    aux_meta = state.get("_phase5_aux_sync") if isinstance(state.get("_phase5_aux_sync"), dict) else {}
    kr_ok = bool(aux_meta.get("kr_ok")) if aux_meta else bool(state.get("circuit_aux_last_kr_krw"))
    us_ok = bool(aux_meta.get("us_ok")) if aux_meta else bool(state.get("circuit_aux_last_usd_total"))
    coin_ok = bool(aux_meta.get("coin_ok")) if aux_meta else bool(state.get("circuit_aux_last_coin_krw"))
    market_ok = {"KR": kr_ok, "US": us_ok, "COIN": coin_ok}

    kr_krw = float(state.get("circuit_aux_last_kr_krw", 0) or 0)
    us_usd = float(state.get("circuit_aux_last_usd_total", 0) or 0)
    coin_krw = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    usdkrw = estimate_usdkrw()
    us_krw = us_usd * usdkrw

    if not any(market_ok.values()):
        print("  ⚠️ [Phase5 서킷] circuit_aux 전부 미확인 — 이번 루프 판정 건너뜀")
        return

    st = load_state(rb.STATE_PATH)
    migrate_legacy_pending_flag(st)

    if rb.ACCOUNT_CIRCUIT_USE_TOTAL:
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

    prune_stale_pending(st, circuits, market_ok)
    try_pending_liquidation()
    st = load_state(rb.STATE_PATH)

    triggered = [
        mk
        for mk in ("KR", "US", "COIN")
        if market_ok.get(mk) and (circuits.get(mk) or {}).get("triggered")
        and not in_market_circuit_cooldown(st, mk)
    ]
    if not triggered:
        return

    for mk in triggered:
        ev = circuits[mk]
        rb.send_telegram(
            f"🚨 [Phase5 {mk} 비중 서킷]\n{ev.get('reason', '')}\n"
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
