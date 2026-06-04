# -*- coding: utf-8 -*-
"""
GUI·heartbeat·텔레용 계좌 스냅샷 단일 진입점.

``run_bot.build_account_snapshot_for_report`` 는 여기로 위임한다.
"""
from __future__ import annotations

from services.account_snapshot import build_account_snapshot_for_report as _core_build_snapshot


def _rb():
    import run_bot as rb

    return rb


def build_ledger_display_snapshot(
    *,
    allow_kis_fetch=None,
    force_kis_labels: bool = False,
) -> dict:
    """장부+시세 라벨, 코인만 거래소 잔고 조회."""
    rb = _rb()
    from api.kis_parsers import parse_kr_cash_total
    from services import ledger_valuation as lv

    st = rb.load_state(rb.STATE_PATH)

    def _kr_p(c, p, b):
        return float(rb.resolve_holding_display_price("KR", c, b, None, p))

    def _us_p(t, p, b):
        return float(rb.resolve_holding_display_price("US", t, b, None, p))

    kr_bal = lv.synthetic_kr_balance_dict(st, resolve_kr_price=_kr_p)
    us_bal = lv.synthetic_us_balance_dict(st, resolve_us_price=_us_p)
    kr_m = rb._calc_kr_holdings_metrics(kr_bal)
    us_m = rb._calc_us_holdings_metrics(us_bal)

    kr_cash_p, kr_total_p = parse_kr_cash_total(kr_bal.get("output2", []), rb._to_float)
    snap_prev = rb.load_last_kis_display_snapshot()
    kr_part = snap_prev.get("kr") if isinstance(snap_prev, dict) else {}
    us_part = snap_prev.get("us") if isinstance(snap_prev, dict) else {}
    kr_cash, kr_total = lv.coalesce_ledger_kis_labels(
        "KR",
        st,
        kr_part if isinstance(kr_part, dict) else {},
        float(kr_m.get("current", 0.0) or 0.0),
        cash_guess=float(kr_cash_p),
        total_guess=float(kr_total_p),
    )
    us_cash_g = float(st.get("last_us_cash_usd", 0) or 0)
    us_hold = float(us_m.get("current", 0.0) or 0.0)
    us_cash, us_total = lv.coalesce_ledger_kis_labels(
        "US",
        st,
        us_part if isinstance(us_part, dict) else {},
        us_hold,
        cash_guess=us_cash_g,
        total_guess=us_cash_g + us_hold,
    )

    krw_bal = 0
    coin_total = 0
    coin_roi = None
    upbit_bals: list = []
    try:
        raw_coin = rb.coin_broker.get_balances() or []
        krw_on, krw_spend = rb._compute_coin_krw_balances(raw_coin)
        coin_total = int(rb._compute_total_coin_equity_from_balances(raw_coin, krw_on))
        krw_bal = int(krw_spend)
        coin_m = rb._calc_coin_holdings_metrics(raw_coin, st.get("positions"))
        coin_roi = coin_m.get("roi")
        upbit_bals = raw_coin
    except Exception as e:
        print(f"  [표시] 코인 잔고 조회 실패 — 직전 스냅샷: {type(e).__name__}: {e}")
        fb = rb.load_last_coin_display_snapshot() or {}
        krw_bal = int(rb._safe_num(fb.get("cash", 0), 0.0))
        coin_total = int(rb._safe_num(fb.get("total", 0), 0.0))
        coin_roi = fb.get("roi")

    weather = rb.get_real_weather(rb.kis_api.broker_kr, rb.kis_api.broker_us)
    return {
        "weather": weather,
        "labels": {
            "kr": {"cash": int(kr_cash), "total": int(kr_total), "roi": kr_m.get("roi")},
            "us": {"cash": float(us_cash), "total": float(us_total), "roi": us_m.get("roi")},
            "coin": {"cash": int(krw_bal), "total": int(coin_total), "roi": coin_roi},
        },
        "holdings": {
            "kr": rb.get_kr_holdings_with_roi(),
            "us": rb.get_us_holdings_with_roi(),
            "coin": rb.get_coin_holdings_with_roi(),
        },
        "balances": {"kr": kr_bal, "us": us_bal, "coin": upbit_bals},
        "ledger_only": True,
    }


def build_account_snapshot_for_report(
    *,
    allow_kis_fetch=None,
    with_backoff=None,
    force_kis_labels: bool = False,
    fresh_balances: bool = False,
    ledger_only: bool = False,
) -> dict:
    """GUI·heartbeat 상단 라벨·보유 스냅샷."""
    rb = _rb()
    from execution.balance_policy import (
        clear_balance_live_sync,
        consume_capital_label_refresh,
        should_use_ledger_only,
    )
    from services import ledger_valuation as _lv

    st = rb.load_state(rb.STATE_PATH)
    if ledger_only or should_use_ledger_only(st, rb.config, force=bool(force_kis_labels)):
        print(
            "  [표시] 장부+시세 — KIS 국·미 잔고 API 생략 "
            "(상단 라벨: last_kis_display_snapshot + 장부 보유평가)"
        )
        return build_ledger_display_snapshot(
            allow_kis_fetch=allow_kis_fetch,
            force_kis_labels=force_kis_labels,
        )

    if force_kis_labels:
        print(
            "  🔁 [KIS 강제 새로고침] 국·미 KIS 실조회 — "
            "예수·총평 라벨·last_kis_display_snapshot 저장 (비장중 포함)"
        )
    elif fresh_balances:
        print("  [표시] KIS·거래소 실조회 — 체결·입출금·always 모드 등")

    bal_refresh = bool(fresh_balances or force_kis_labels)

    def _snapshot_kr_balance():
        return rb.ensure_dict(rb.bal_read.kr_balance_for_report(refresh=bal_refresh))

    def _snapshot_us_balance():
        return rb.ensure_dict(rb.bal_read.us_balance_for_report(refresh=bal_refresh))

    def _snapshot_coin_balances():
        raw = rb.bal_read.coin_balances_for_report(refresh=bal_refresh)
        return raw if isinstance(raw, list) else []

    st_trust = rb.load_state(rb.STATE_PATH)
    trust_capital_labels = consume_capital_label_refresh(st_trust, rb.STATE_PATH)

    deps = {
        "trust_off_hours_live_labels": trust_capital_labels,
        "get_real_weather": rb.get_real_weather,
        "broker_kr": rb.kis_api.broker_kr,
        "broker_us": rb.kis_api.broker_us,
        "load_last_kis_display_snapshot": rb.load_last_kis_display_snapshot,
        "save_last_kis_display_snapshot": rb.save_last_kis_display_snapshot,
        "load_last_coin_display_snapshot": rb.load_last_coin_display_snapshot,
        "save_last_coin_display_snapshot": rb.save_last_coin_display_snapshot,
        "is_weekend_suppress": rb.kis_equities_weekend_suppress_window_kst,
        "get_balance_with_retry": _snapshot_kr_balance,
        "get_us_positions_with_retry": _snapshot_us_balance,
        "get_us_cash_real": rb.get_us_cash_real,
        "to_float": rb._to_float,
        "safe_num": rb._safe_num,
        "calc_kr_holdings_metrics": rb._calc_kr_holdings_metrics,
        "calc_us_holdings_metrics": rb._calc_us_holdings_metrics,
        "calc_coin_holdings_metrics": rb._calc_coin_holdings_metrics,
        "upbit_get_balance": rb._coin_snapshot_get_balance,
        "upbit_get_balances": _snapshot_coin_balances,
        "get_kr_holdings_with_roi": rb.get_kr_holdings_with_roi,
        "get_us_holdings_with_roi": rb.get_us_holdings_with_roi,
        "get_coin_holdings_with_roi": rb.get_coin_holdings_with_roi,
        "is_market_open": rb.is_market_open,
    }
    snap = _core_build_snapshot(
        deps=deps,
        allow_kis_fetch=allow_kis_fetch,
        with_backoff=with_backoff,
        force_kis_labels=force_kis_labels,
    )
    try:
        bal_map = snap.get("balances") if isinstance(snap, dict) else {}
        st_cash = rb.load_state(rb.STATE_PATH)
        touched = False
        if isinstance(bal_map, dict):
            kr_b = bal_map.get("kr")
            us_b = bal_map.get("us")
            if isinstance(kr_b, dict) and kr_b.get("output2") is not None:
                _lv.persist_kr_cash_from_balance(kr_b, st_cash)
                touched = True
            if isinstance(us_b, dict) and (
                us_b.get("output1") is not None or us_b.get("output2") is not None
            ):
                _lv.persist_us_cash_from_balance(us_b, st_cash)
                touched = True
        if touched:
            rb.save_state(rb.STATE_PATH, st_cash)
            if force_kis_labels:
                print(
                    "  🔁 [KIS 강제 새로고침] 장부 예수 캐시 갱신 "
                    f"(KR {int(st_cash.get('last_kr_cash_krw', 0) or 0):,}원 · "
                    f"US ${float(st_cash.get('last_us_cash_usd', 0) or 0):,.2f})"
                )
    except Exception as e:
        print(f"  ⚠️ [snapshot] 장부 예수 캐시 갱신 실패: {type(e).__name__}: {e}")
    if force_kis_labels or fresh_balances:
        st2 = rb.load_state(rb.STATE_PATH)
        clear_balance_live_sync(st2, rb.STATE_PATH)
    return snap
